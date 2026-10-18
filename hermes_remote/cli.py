"""命令行入口：run / doctor / demo / provision / frames / audit。

`hermes-remote run` 是常驻进程；其余是排查和交付辅助。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

from . import protocol, renderer
from .audit import AuditLog
from .bridge import Bridge, BridgeConfig
from .config import DEVICE_TOKEN_PATH, Config, load_config, resolve_backend_url, save_config
from .desktop_discovery import find_desktop_backend
from .state import BridgeState, PendingRequest, SessionRow, View, ViewLine

log = logging.getLogger("hermes_remote.cli")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ── run ─────────────────────────────────────────────────────────────────────

async def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config).expanduser() if args.config else None)
    url = resolve_backend_url(cfg, cli_url=args.url or "", cli_token=args.token or "")
    bcfg = BridgeConfig(backend_url=url,
                        lan_port=args.lan_port or cfg.lan_port,
                        device_token=cfg.device_token,
                        session_id=args.session or cfg.session_id,
                        render_hz=args.render_hz,
                        beacon=not args.no_beacon,
                        keep_awake=not args.no_keep_awake)
    if args.print_token:
        print(cfg.device_token)
        return 0
    from .stt import make_transcriber

    def rediscover() -> str | None:
        """桌面 App 重启后端口会变：断线时重新找一遍，找到就换 URL。"""
        try:
            found = find_desktop_backend()
        except Exception as exc:  # 发现失败不能把桥带下去
            log.debug("rediscover failed: %s", exc)
            return None
        if not found:
            return None
        return f"{found.url}?token={found.token}"

    if args.rediscover and not args.url:
        found_url = rediscover()
        if found_url:
            bcfg.backend_url = found_url
            print("已自动发现桌面后端（token 不回显）")
        else:
            print("没找到桌面 App 的后端：先自己起 `hermes dashboard` 或用 --url/--token")

    bridge = Bridge(bcfg, transcriber=make_transcriber(), audit=AuditLog(args.audit or None),
                    rediscover=rediscover if args.rediscover else None)
    print(f"device token: {cfg.device_token[:8]}…（完整值见 ~/.hermes-remote/device.token）")
    print(f"backend: {bcfg.backend_url.split('?')[0]}")
    print(f"LAN: ws://<this-mac-ip>:{bcfg.lan_port}/ws   UDP 发现端口 47824")
    stop = asyncio.Event()

    def _stop(*_a):
        stop.set()
        bridge.request_stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass
    await bridge.start()
    try:
        await asyncio.wait_for(stop.wait(), timeout=None)
    finally:
        await bridge.stop()
    return 0


# ── doctor ──────────────────────────────────────────────────────────────────

async def _cmd_doctor(args: argparse.Namespace) -> int:
    from .gateway import GatewayClient, GatewayError

    cfg = load_config(Path(args.config).expanduser() if args.config else None)
    url = resolve_backend_url(cfg, cli_url=args.url or "", cli_token=args.token or "")
    checks: list[tuple[str, bool, str]] = []

    font = renderer.find_font_path()
    checks.append(("中文字体", bool(font), font or "没找到中文字体，屏上中文会是方块（设 HERMES_REMOTE_FONT）"))

    try:
        from PIL import Image  # noqa: F401
        import websockets  # noqa: F401
        import aiohttp  # noqa: F401
        checks.append(("Python 依赖", True, "pillow / websockets / aiohttp 就绪"))
    except Exception as exc:
        checks.append(("Python 依赖", False, str(exc)))

    from .stt import make_transcriber

    tr = make_transcriber()
    ok, why = tr.preflight()
    checks.append(("语音转写", ok, why if not ok else f"model={getattr(tr, 'model_name', 'fake')}"))

    # 端口可绑
    try:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", cfg.lan_port))
        s.close()
        checks.append(("局域网端口", True, f"{cfg.lan_port} 可绑定"))
    except OSError as exc:
        checks.append(("局域网端口", False, f"{cfg.lan_port} 绑定失败：{exc}"))

    # 后端
    backend_ok = False
    detail = ""
    if not cfg.backend_token and not args.token:
        detail = "没有 token：设 HERMES_REMOTE_BACKEND_TOKEN 或在 config.json 里写 backend_token"
    else:
        client = GatewayClient(url)
        task = asyncio.create_task(client.run())
        try:
            if await client.wait_ready(8):
                rows = await client.list_sessions(limit=5)
                backend_ok = True
                detail = f"已连接；最近会话 {len(rows)} 个；capabilities={','.join(client.server_requests[:4])}…"
            else:
                detail = "8 秒内没收到 gateway.ready（端口/token 不对，或者不是 Hermes 后端）"
        except GatewayError as exc:
            detail = str(exc)
        finally:
            client.close()
            task.cancel()
    checks.append(("Hermes 后端", backend_ok, detail))

    if sys.platform == "darwin":
        from .power import SleepGuard, pmset_summary

        checks.append(("睡眠抑制", SleepGuard().available(), "caffeinate 可用" if SleepGuard().available()
                       else "找不到 caffeinate"))
        checks.append(("电源设置", True, pmset_summary()))

    width = max(len(name) for name, _, _ in checks)
    failed = 0
    for name, ok, note in checks:
        mark = "OK  " if ok else "FAIL"
        failed += 0 if ok else 1
        print(f"[{mark}] {name.ljust(width)}  {note}")
    print()
    print("结论：" + ("全部通过，可以 hermes-remote run" if failed == 0
                     else f"{failed} 项没过，先处理上面标 FAIL 的"))
    return 0 if failed == 0 else 1


# ── demo：没有板子也能看完整链路 ─────────────────────────────────────────────

async def _cmd_demo(args: argparse.Namespace) -> int:
    from .devsim import SimDevice
    from .fake_gateway import FakeGateway
    from .stt import FakeTranscriber

    gw = FakeGateway(session_title="演示会话 · 改 tui_gateway/server.py")
    port = await gw.start()
    tr = FakeTranscriber("这个方案不行，换个思路")
    cfg = BridgeConfig(backend_url=gw.url, lan_port=args.lan_port, device_token="demo-token",
                       render_hz=2.0, snapshot_refresh_s=30, event_poll_s=1, beacon=False)
    audit_file = Path(args.workdir) / "audit-demo.jsonl"
    bridge = Bridge(cfg, transcriber=tr, audit=AuditLog(audit_file),
                    gateway=None)
    await bridge.start()
    dev = SimDevice(port=args.lan_port, token="demo-token")
    await asyncio.sleep(0.6)
    await dev.connect()

    def show(title: str) -> None:
        print("\n" + "─" * 78)
        print(f"■ {title}")
        print("─" * 78)
        print(dev.ascii())

    try:
        await gw.emit("status.update", kind="status", text="正在读 tui_gateway/server.py")
        await gw.emit("tool.start", tool_id="t1", name="read_file",
                      args={"path": "tui_gateway/server.py"})
        await asyncio.sleep(0.4)
        await dev.wait_frame(2)
        show("状态页：它正在干活")

        # 1) clarify
        task = asyncio.create_task(gw.ask("clarify", {"questions": [
            {"qid": "q1", "question": "要不要把桥挂到桌面 App 的后端上？", "choices": ["挂上去", "另起一个"]}]}))
        await asyncio.sleep(0.5)
        await dev.wait_frame(2)
        show("问题页：按 1 = 挂上去")
        await dev.key("1")
        ans = await asyncio.wait_for(task, 10)
        print(f"\n→ 后端收到 clarify 答案：{ans}")

        # 2) 危险审批，两次确认
        task = asyncio.create_task(gw.ask("approval", {
            "request_id": "ap-1", "tool_name": "terminal",
            "command": "rm -rf /opt/data/tmp/build && git push --force origin main",
            "description": "清理构建目录并强推", "choices": ["once", "deny"]}))
        await asyncio.sleep(0.5)
        await dev.wait_frame(2)
        show("审批页：危险命令，命令原文必须在屏上")
        await dev.key("1")
        await asyncio.sleep(0.4)
        await dev.wait_frame(2)
        show("第一次按 1 只是“上膛”，还要再按一次")
        await dev.key("1")
        ans = await asyncio.wait_for(task, 10)
        print(f"\n→ 后端收到 approval 答案：{ans}")

        # 3) 语音
        task = asyncio.create_task(gw.ask("clarify", {"questions": [
            {"qid": "q1", "question": "语音这块怎么做？", "choices": None}]}))
        await asyncio.sleep(0.4)
        await dev.wait_frame(2)
        await dev.upload_voice(b"\x00\x01" * 8000)  # 0.5 秒假音频
        await asyncio.sleep(0.4)
        await dev.wait_frame(2)
        show("语音预览：转出来的字先给你看，enter 才发")
        await dev.key("enter")
        ans = await asyncio.wait_for(task, 10)
        print(f"\n→ 后端收到语音作答：{ans}")

        # 4) Mac 先答，板子卡片要收掉
        task = asyncio.create_task(gw.ask("clarify", {"questions": [
            {"qid": "q1", "question": "这条会被 Mac 先答", "choices": ["A", "B"]}]}))
        await asyncio.sleep(0.4)
        await dev.wait_frame(2)
        rid = next(iter(gw.open_requests))
        gw.answers.append((rid, {"answers": {"q1": "A"}}))
        gw.open_requests.pop(rid, None)
        await gw.emit("request.cancel", id=rid, method="clarify", reason="answered-elsewhere")
        await asyncio.sleep(0.5)
        await dev.wait_frame(2)
        show("Mac 上先按了：板子的卡片 1 秒内消失")
        await asyncio.wait_for(task, 10)

        print("\n" + "═" * 78)
        print("审计日志（~/…/audit-demo.jsonl 同样几行）：")
        for rec in audit_file.read_text(encoding="utf-8").splitlines():
            entry = json.loads(rec)
            print("  " + json.dumps({k: entry[k] for k in ("event", "method", "choice", "answers")
                                     if k in entry}, ensure_ascii=False))
        print(f"\n共推送 {len(dev.frames)} 帧；最后一帧 {len(dev.last_frame.encode())} 字节"
              f"（裸位图 {protocol.RAW_LEN} 字节）")
        return 0
    finally:
        await dev.close()
        await bridge.stop()
        await gw.stop()


# ── frames：把几种屏导出成 PNG，交文档/评审用 ────────────────────────────────

def _sample_views() -> list[tuple[str, View]]:
    st = BridgeState()
    st.connected_backend = True
    st.sessions = [SessionRow(id="s1", title="改 tui_gateway/server.py", status="working",
                              running=True, turn_started_at=time.time() - 252)]
    st.status_note = "edit tui_gateway/server.py"
    st.last_output = "tests 42 passed, 0 failed"
    st.device_battery = 87
    st.device_rssi = -52
    status = st.build_view()

    st2 = BridgeState()
    st2.connected_backend = True
    st2.sessions = [SessionRow(id="s1", title="改 tui_gateway/server.py", status="waiting")]
    st2.pending = PendingRequest(id="srq-1", method="clarify", params={"questions": [
        {"qid": "q1", "question": "这一步要不要直接改后端？", "choices": ["改", "不改", "先问一下"]}]})
    clarify = st2.build_view()

    st3 = BridgeState()
    st3.connected_backend = True
    st3.sessions = [SessionRow(id="s1", title="清理构建目录")]
    st3.pending = PendingRequest(id="srq-2", method="approval", params={
        "tool_name": "terminal",
        "command": "rm -rf /opt/data/tmp/build && git push --force origin main",
        "choices": ["once", "deny"]})
    approval = st3.build_view()

    st4 = BridgeState()
    st4.connected_backend = True
    st4.voice_preview = "这个方案不行，换个思路"
    voice = st4.build_view()
    return [("status", status), ("clarify", clarify), ("approval", approval), ("voice", voice)]


def _cmd_discover(args: argparse.Namespace) -> int:
    """找到桌面 App 的后端（pid → 监听端口 → token），写进 0600 配置。"""
    found = find_desktop_backend()
    if found is None:
        print("没找到桌面 App 的后端：确认桌面 App 正在运行，且是它自己起的后端。")
        print("退路：自己起一个 `HERMES_DASHBOARD_SESSION_TOKEN=xxx hermes dashboard`，"
              "然后 hermes-remote run --url ws://127.0.0.1:<port>/api/ws --token xxx")
        return 1
    print(f"桌面后端 pid={found.pid} → 127.0.0.1:{found.port}（token 已读到，不显示）")
    if args.print:
        return 0
    path = Path(args.config).expanduser() if args.config else None
    cfg = load_config(path)
    cfg.backend_url = found.url
    cfg.backend_token = found.token
    saved = save_config(cfg, path)
    print(f"已写入 {saved}（0600）。直接跑：hermes-remote run --rediscover")
    return 0


def _cmd_rotate_token(args: argparse.Namespace) -> int:
    """换一个设备 token：旧 token 立即失效，板子必须重新配网。"""
    import secrets

    token = secrets.token_hex(16)
    path = Path(args.path).expanduser() if args.path else DEVICE_TOKEN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    print(f"新设备 token 前 8 位：{token[:8]}…（完整值 {path}，0600）")
    print("旧 token 立即失效：板子要重新 provision 才能再连；桥重启后生效。")
    return 0


def _cmd_frames(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fonts = renderer.Fonts()
    for name, view in _sample_views():
        img = renderer.render(view, fonts)
        frame = protocol.build_frame4(renderer.palette_bytes(img), 1)
        path = out / f"screen-{name}.png"
        img.save(path)
        (out / f"screen-{name}.rle").write_bytes(frame.encode())
        print(f"{path}  view={protocol.SCREEN_W}x{protocol.SCREEN_H}  rle={len(frame.payload)}B")
    return 0


# ── provision：串口写 SSID / 密码 / token 进 NVS ─────────────────────────────

def _cmd_provision(args: argparse.Namespace) -> int:
    from .provision import provision_serial

    cfg = load_config()
    token = cfg.device_token
    password = args.password
    if not password and args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        import getpass

        password = getpass.getpass("WiFi 密码（输入不回显、不写进 shell history）：")
    if not password:
        print("没有密码：开放式网络请显式传 --password ''", file=sys.stderr)
        return 2
    try:
        provision_serial(args.port, ssid=args.ssid, password=password,
                         token=token, bridge_host=args.bridge_host, bridge_port=args.lan_port,
                         wait_ready_s=args.wait_ready)
    except Exception as exc:
        print(f"串口写入失败：{exc}", file=sys.stderr)
        return 1
    print("已写入板子 NVS。密码只在串口这一条线上走，不落盘、不回显。")
    return 0


# ── audit ───────────────────────────────────────────────────────────────────

def _cmd_audit(args: argparse.Namespace) -> int:
    log = AuditLog(args.audit)
    for rec in log.tail(args.n):
        print(json.dumps(rec, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermes-remote", description="Cardputer-Adv ↔ Hermes 遥控桥")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", default="")
        sp.add_argument("--url", default="", help="ws://127.0.0.1:PORT/api/ws")
        sp.add_argument("--token", default="", help="后端会话 token")

    run = sub.add_parser("run", help="常驻运行桥")
    common(run)
    run.add_argument("--lan-port", type=int, default=0)
    run.add_argument("--session", default="")
    run.add_argument("--render-hz", type=float, default=1.0)
    run.add_argument("--no-beacon", action="store_true")
    run.add_argument("--no-keep-awake", action="store_true", help="不启动 caffeinate 睡眠抑制")
    run.add_argument("--audit", default="")
    run.add_argument("--rediscover", action="store_true",
                     help="启动/断线时自动发现桌面 App 的后端端口（桌面 App 重启会换端口）")
    run.add_argument("--print-token", action="store_true", help="打印设备 token 后退出（配网用）")
    run.set_defaults(func=lambda a: asyncio.run(_cmd_run(a)))

    disc = sub.add_parser("discover", help="找到桌面 App 的后端，把 url+token 写进配置")
    common(disc)
    disc.add_argument("--print", action="store_true", help="只报告找到没找到，不写配置")
    disc.set_defaults(func=_cmd_discover)

    rot = sub.add_parser("rotate-token", help="换设备 token（旧 token 立即失效，需重新配网）")
    rot.add_argument("--path", default="", help="device.token 路径（默认 ~/.hermes-remote/device.token）")
    rot.set_defaults(func=_cmd_rotate_token)

    doc = sub.add_parser("doctor", help="自检：后端、端口、字体、语音")
    common(doc)
    doc.set_defaults(func=lambda a: asyncio.run(_cmd_doctor(a)))

    demo = sub.add_parser("demo", help="无硬件全链路演示（假后端 + 模拟板子）")
    demo.add_argument("--lan-port", type=int, default=47923)
    demo.add_argument("--workdir", default=".")
    demo.set_defaults(func=lambda a: asyncio.run(_cmd_demo(a)))

    fr = sub.add_parser("frames", help="导出几种屏的 PNG / RLE，评审用")
    fr.add_argument("--out", default="./screens")
    fr.set_defaults(func=_cmd_frames)

    prov = sub.add_parser("provision", help="串口把 WiFi 和 token 写进板子")
    prov.add_argument("--port", required=True, help="/dev/cu.usbmodem1101 之类")
    prov.add_argument("--ssid", required=True)
    prov.add_argument("--password", default="", help="不给就交互式输入（避免进 shell history）")
    prov.add_argument("--password-stdin", action="store_true", help="从 stdin 读密码")
    prov.add_argument("--bridge-host", default="")
    prov.add_argument("--lan-port", type=int, default=47823)
    prov.add_argument("--wait-ready", type=float, default=20.0,
                      help="等固件 READY 横幅的秒数（默认 20；配合手动断电复位可以调长）")
    prov.set_defaults(func=_cmd_provision)

    au = sub.add_parser("audit", help="看最近的远程作答记录")
    au.add_argument("-n", type=int, default=20)
    au.add_argument("--audit", default="")
    au.set_defaults(func=_cmd_audit)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
