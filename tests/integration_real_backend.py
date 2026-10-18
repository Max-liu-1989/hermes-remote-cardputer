#!/usr/bin/env python3
"""对真实 Hermes 后端的集成验证（M0 + M1 的最硬证据）。

它不使用假后端：起的是仓库里真正的 ``hermes_cli.web_server``（桌面 App /
dashboard 用的同一个后端），真的 ``/api/ws``，真的 ``server_requests``。
后端那边的 clarify/approval 由测试进程直接调用 ``server_requests.send`` 注入，
所以不需要模型 key、不烧 token，也能验证“桥能不能挂上、请求能不能到、答案能不能回去”。

跑法（需要能 import hermes 的 Python，通常是 Hermes 自己的 venv）：
    /opt/hermes/.venv/bin/python tests/integration_real_backend.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, "/opt/hermes")  # 让本脚本能 import 真实后端

os.environ.setdefault("HERMES_DASHBOARD_SESSION_TOKEN", "integration-token-xyz")
os.environ.setdefault("HERMES_TEST_ISOLATION", "1")

import uvicorn  # noqa: E402
from hermes_cli.web_server import app  # noqa: E402
from tui_gateway import server_requests  # noqa: E402

from hermes_remote.devsim import SimDevice  # noqa: E402
from hermes_remote.bridge import Bridge, BridgeConfig  # noqa: E402
from hermes_remote.audit import AuditLog  # noqa: E402
from hermes_remote.gateway import GatewayClient  # noqa: E402
from hermes_remote.stt import FakeTranscriber  # noqa: E402

PORT = 9481
TOKEN = "integration-token-xyz"
DEVICE_TOKEN = "itest-device-token-0123456789abcdef"


async def wait_for(predicate, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


def start_backend() -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 30
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.1)
    assert server.started, "backend did not start"
    return server


async def main() -> int:
    server = start_backend()
    results: dict[str, object] = {}
    try:
        # 1) 先单独建一个会话（不碰正在跑的 agent 会话）
        gw = GatewayClient(f"ws://127.0.0.1:{PORT}/api/ws?token={TOKEN}")
        task = asyncio.create_task(gw.run())
        assert await gw.wait_ready(10), "no gateway.ready"
        created = await gw.call("session.create", {"title": "remote-approval-itest", "source": "itest"})
        sid = (created or {}).get("session_id") or (created or {}).get("id")
        assert sid, f"session.create failed: {created}"
        print(f"[1] 真实后端已连上，新建会话 sid={sid}")
        print(f"    后端声明支持的 server requests: {', '.join(gw.server_requests[:6])}…")
        gw.close()
        task.cancel()

        # 2) 桥挂上去（第二订阅者），模拟板子连接
        cfg = BridgeConfig(backend_url=f"ws://127.0.0.1:{PORT}/api/ws?token={TOKEN}",
                           lan_port=0, device_token=DEVICE_TOKEN, session_id=sid,
                           render_hz=4.0, snapshot_refresh_s=60, event_poll_s=1.0, beacon=False)
        bridge = Bridge(cfg, transcriber=FakeTranscriber("就用第二订阅者这条"),
                        audit=AuditLog(os.path.join(HERE, "..", "logs", "itest-audit.jsonl")))
        await bridge.start()
        dev = SimDevice(port=bridge.hub.port, token=DEVICE_TOKEN)
        await asyncio.sleep(0.5)
        await dev.connect()
        assert await wait_for(lambda: bridge.gateway.connected), "bridge not connected"
        assert await wait_for(lambda: bridge.state.active is not None), "bridge did not attach"
        print(f"[2] 桥以第二订阅者身份挂上会话 {bridge.state.active.id}（桌面 App 不受影响）")

        # 3) 真实后端发一个 clarify，桥收下并推屏
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def ask():
            res = server_requests.send(
                "clarify", sid,
                {"questions": [{"qid": "q1", "question": "挂到桌面后端还是另起一个？",
                                "choices": ["挂上去", "另起一个"]}]},
                timeout=25)
            loop.call_soon_threadsafe(fut.set_result, res)

        threading.Thread(target=ask, daemon=True).start()
        assert await wait_for(lambda: bridge.state.pending is not None), "clarify never reached the bridge"
        frame = await dev.wait_frame(3)
        print(f"[3] 收到真实 clarify 帧 id={bridge.state.pending.id}，已推屏 "
              f"({len(frame.encode())} 字节 RLE)")
        print(f"    屏上标题：{bridge.state.build_view().title} / "
              f"提示：{bridge.state.build_view().footer}")

        # 4) 板子按 1，后端必须收到原文
        await dev.key("1")
        backend_result = await asyncio.wait_for(fut, 25)
        print(f"[4] 后端拿到答案：{backend_result}")
        assert backend_result == {"answers": {"q1": "挂上去"}}, backend_result

        # 5) 真实 approval 请求（危险命令必须两次确认）
        fut2 = loop.create_future()

        def ask_approval():
            res = server_requests.send(
                "approval", sid,
                {"request_id": "ap-itest", "tool_name": "terminal",
                 "command": "rm -rf /opt/data/tmp/itest-build", "description": "清理构建目录",
                 "choices": ["once", "deny"]},
                timeout=25)
            loop.call_soon_threadsafe(fut2.set_result, res)

        threading.Thread(target=ask_approval, daemon=True).start()
        assert await wait_for(lambda: bridge.state.pending is not None), "approval never arrived"
        view = bridge.state.build_view()
        print(f"[5] 收到真实 approval：{view.lines[0].text} / {view.lines[1].text}")
        await dev.key("1")
        await asyncio.sleep(0.3)
        assert not fut2.done(), "危险命令一次就放行了"
        await dev.key("1")
        approval_result = await asyncio.wait_for(fut2, 25)
        print(f"[6] 两次确认后后端拿到：{approval_result}")
        assert approval_result == {"choice": "once"}, approval_result

        # 6) 收尾：板子断开、桥停止
        await dev.close()
        await bridge.stop()
        print("\n全部通过：真实后端 + 真实 WS + 真实 server_requests + 桥 + 模拟板子")
        print(f"审计记录：{os.path.normpath(os.path.join(HERE, '..', 'logs', 'itest-audit.jsonl'))}")
        return 0
    finally:
        server.should_exit = True
        await asyncio.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
