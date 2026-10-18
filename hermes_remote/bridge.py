"""桥的核心：把后端状态压成一块屏，把按键翻译成回答。

生命周期：``Bridge.run()`` 起设备服务 + 发现广播 + 后端连接 + 1Hz 心跳。
所有外部输入（后端事件、后端询问、按键、语音）都落在这一个对象上，
所以“屏上写什么”和“按下这个键会发生什么”在一个地方就能读全。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass

from . import protocol, renderer
from .audit import AuditLog
from .device_server import DeviceHub
from .gateway import GatewayClient, GatewayError, RpcError, build_url
from .safety import (approval_choice_label, approval_key_map, approval_result,
                     clarify_result)
from .stt import TranscriptionUnavailable
from .state import (CONFIRM_WINDOW_S, PAGE_PROMPT, PAGE_REPLY, PAGE_VOICE, BridgeState,
                    PendingRequest, SessionRow, View, _is_bot_session)

log = logging.getLogger("hermes_remote.bridge")

#: 回复提示音的"闲置门槛"：这段时间内按过键就不响（人在桌前，不需要叼一声）
IDLE_BEEP_S = 300.0


@dataclass
class BridgeConfig:
    backend_url: str = ""
    lan_port: int = 47823
    device_token: str = ""
    session_id: str | None = None
    render_hz: float = 1.0
    #: 整份快照每 30 秒拉一次就够（状态主要靠事件与 open_requests 轮询）；
    #: 频繁 session.activate 会和桌面 App 抢“当前客户端”。
    snapshot_refresh_s: float = 30.0
    #: 询问会通过 WS 推送过来（网关已修）；这里 1 秒一轮是兜底 + 用 open_requests 对账，
    #: 顺便把“Mac 上已经答过、事件没收到”的幽灵卡清掉。
    event_poll_s: float = 1.0
    beacon: bool = True
    keep_awake: bool = True


class Bridge:
    def __init__(self, cfg: BridgeConfig, *, transcriber=None,
                 audit: AuditLog | None = None,
                 gateway: GatewayClient | None = None,
                 rediscover=None) -> None:
        self.cfg = cfg
        self.state = BridgeState()
        self.fonts = renderer.Fonts()
        self.audit = audit or AuditLog()
        self._rediscover = rediscover
        self.seq = 0
        self._last_view_key: str | None = None
        #: 上一次"点亮背光"时的内容指纹：同一张卡重复刷新不再唤醒
        self._last_wake_key: str | None = None
        self._last_sent_at: float = 0.0
        self._last_device_count: int = 0
        self._queue: list[PendingRequest] = []
        #: 最近作答过的 request id → 时间戳。用来挡住两类复活：
        #: 快照 replay 把已经答过的请求再塞回来；轮询每 3 秒重复上报同一个请求。
        self._answered: dict[str, float] = {}
        self._missing_pending = 0
        self._announced_id: str | None = None
        self._attached_id: str = ""          # 当前挂着的会话（跟着桌面端走）
        self._manual_until: float = 0.0      # 用户在板子上手动切过会话：这段时间别自动抢
        self._last_follow_at: float = 0.0
        self._render_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.gateway = gateway or GatewayClient(
            cfg.backend_url or build_url(),
            on_request=self.on_server_request,
            on_event=self.on_event,
            on_state=self.on_backend_state,
        )
        self.hub = DeviceHub(cfg.device_token or secrets.token_hex(16),
                             on_key=self.on_key,
                             on_voice_upload=self.on_voice_upload,
                             on_voice_state=self.on_voice_state)
        self.transcriber = transcriber
        self.beacon = None
        self.sleep = None
        self._last_snapshot_at = 0.0
        self._last_event_poll_at = 0.0
        self._status_at = 0.0
        self._interrupt_armed_at: float | None = None

    # ── 启动/停止 ───────────────────────────────────────────────────────────
    def _warm_stt(self) -> None:
        """后台把语音模型跑热：首次加载要几十秒，不能在用户按下侧键后才开始。"""
        import threading

        def run() -> None:
            try:
                ok, why = self.transcriber.warmup()
                log.info("stt warmup: %s (%s)", ok, why)
            except Exception as exc:            # noqa: BLE001
                log.warning("stt warmup failed: %s", exc)

        threading.Thread(target=run, name="stt-warmup", daemon=True).start()

    async def start(self) -> None:
        from .power import SleepGuard

        self.sleep = SleepGuard(self.cfg.keep_awake).start()
        port = await self.hub.start(port=self.cfg.lan_port)
        log.info("bridge up: device port %d, backend %s", port,
                 self.cfg.backend_url.split("?")[0])
        self._warm_stt()          # 语音模型后台预热，几十秒的加载别等按键那一下
        if self.cfg.beacon:
            from .beacon import Beacon

            self.beacon = Beacon(port=port, host_token=self.cfg.device_token)
            await self.beacon.start()
        self._tasks.append(asyncio.create_task(self.gateway.run(), name="gateway"))
        self._tasks.append(asyncio.create_task(self._ticker(), name="ticker"))

    async def run(self) -> None:
        await self.start()
        await self._stop.wait()
        await self.stop()

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        if self.beacon is not None:
            await self.beacon.stop()
        if self.sleep is not None:
            self.sleep.stop()
        await self.hub.stop()

    def request_stop(self) -> None:
        self._stop.set()

    # ── 后端连接状态 ────────────────────────────────────────────────────────
    async def on_backend_state(self, connected: bool) -> None:
        self.state.connected_backend = connected
        if connected:
            try:
                await self._attach_default_session()
            except GatewayError as exc:
                log.warning("attach failed: %s", exc)
        else:
            # 断开时把“已上膛”作废：重连后旧确认不得继续生效
            req = self.state.pending
            if req is not None and req.confirm_armed:
                req.confirm_armed = False
                req.confirm_choice = None
                req.confirm_digest = ""
            if self._rediscover is not None:
                # 桌面 App 重启 → 后端端口变了。断线时重找一次，找到就换目标。
                try:
                    new_url = self._rediscover()
                except Exception:
                    new_url = None
                if new_url and new_url != self.gateway.url:
                    log.info("backend moved; retargeting %s", new_url.split("?")[0])
                    self.gateway.url = new_url
        await self.render_now()

    def _session_rows(self, live: list[dict]) -> list[SessionRow]:
        """板子上的会话列表：过滤 bot/群聊，并把"我们当前挂的"钉在第一位。

        钉在第一位是为了永远能一键切回来——群里几个 bot 会话一直 working，
        不钉的话自己的对话会被挤到后面甚至挤出列表。
        """
        rows = [r for r in live if not _is_bot_session(r)]
        rows.sort(key=lambda r: 0 if str(r.get("id") or "") == self._attached_id else 1)
        return [SessionRow(id=str(r.get("id")), title=str(r.get("title") or ""),
                           status=str(r.get("status") or "")) for r in rows[:6]]

    async def _attach_default_session(self) -> None:
        sid = self.cfg.session_id
        if not sid:
            # 先找“活着的”会话：桌面 App 的后端只让 attach 活动列表里的短 id。
            live = [r for r in await self.gateway.active_sessions() if not _is_bot_session(r)]
            live.sort(key=lambda r: float(r.get("last_active") or 0), reverse=True)
            if live:
                # 启动时优先挂"正在跑"的会话，否则挂最近活跃的
                running = [r for r in live if str(r.get("status") or "") == "working"]
                pick = (running or live)[0]
                sid = str(pick.get("id") or "")
                self.state.set_sessions(self._session_rows(live))
            else:
                rows = await self.gateway.list_sessions(limit=8)
                if rows:
                    sid = str(rows[0].get("id"))
                    self.state.set_sessions([
                        SessionRow(id=str(r.get("id")), title=str(r.get("title") or ""))
                        for r in rows[:6]
                    ])
        if not sid:
            return
        snap = await self.gateway.attach(sid)
        self.state.apply_snapshot(snap)
        self._absorb_open_requests(snap)
        self._last_snapshot_at = time.time()
        self._attached_id = sid
        log.info("attached session %s status=%s", sid, snap.get("status"))

    async def _follow_active_session(self) -> None:
        """桌面端新开/切换会话时，板子跟着走。

        以前只在连上后端时挂一次会话，于是桌面上开了新对话，板子还停在上一个——
        标题、状态、输出全是旧的，人以为板子卡了。
        用 ``session.active_list`` 判（轻量）；只有真的换了会话才 ``attach`` 一次，
        避免周期性 activate 抢“当前客户端”、把 ticker 卡死（那是之前踩过的坑）。
        """
        now = time.time()
        if now < self._manual_until or now - self._last_follow_at < 5.0:
            return
        self._last_follow_at = now
        live = [r for r in await self.gateway.active_sessions() if not _is_bot_session(r)]
        if not live:
            return
        live.sort(key=lambda r: float(r.get("last_active") or 0), reverse=True)
        log.debug("active_list: %s",
                  [(r.get("id"), str(r.get("title") or "")[:12], r.get("status"))
                   for r in live[:4]])
        # 注意：我们自己 attach 时也会调 session.activate，会刷新 last_active——
        # 只按“最近活跃”会自我强化、在会话之间乱跳。所以先看谁在真正跑：
        #   1) 有会话在 working → 跟它（那才是正在发生的事）
        #   2) 没有 working、而我们还在列表里 → 不动（宁可稳）
        #   3) 我们不在列表里（会话被关掉）→ 跟最活跃的
        ours = self._attached_id
        working = [r for r in live if str(r.get("status") or "") == "working"]
        if working:
            top = str(working[0].get("id") or "")
        elif ours and any(str(r.get("id") or "") == ours for r in live):
            return
        else:
            top = str(live[0].get("id") or "")
        if not top or top == self._attached_id:
            return
        self.state.set_sessions(self._session_rows(live))
        try:
            snap = await asyncio.wait_for(self.gateway.attach(top), timeout=5.0)
        except (GatewayError, asyncio.TimeoutError) as exc:
            log.debug("follow session %s failed: %s", top, exc)
            return
        self.state.apply_snapshot(snap)
        self._absorb_open_requests(snap)
        self._last_snapshot_at = time.time()
        self._attached_id = top
        log.info("desktop switched session → following %s", top)

    # ── 后端询问 ────────────────────────────────────────────────────────────
    async def on_server_request(self, frame: dict) -> None:
        req = PendingRequest(id=str(frame.get("id")), method=str(frame.get("method")),
                             params=frame.get("params") or {},
                             session_id=str((frame.get("params") or {}).get("session_id") or ""))
        if self._is_answered(req.id):
            log.info("ignore already-answered request %s", req.id)
            return
        if self.state.pending is not None and self.state.pending.id == req.id:
            return
        if any(q.id == req.id for q in self._queue):
            return
        log.info("request %s %s", req.method, req.id)
        self.audit.write("request.received", request_id=req.id, method=req.method,
                         session_id=req.session_id,
                         tool=req.params.get("tool_name"),
                         command=str(req.params.get("command") or "")[:200])
        if self.state.pending is None:
            self.state.pending = req
            self._adopt_question_pages(req)
            self.state.dismiss_reply()      # 新请求优先，别让回复页挡住卡片
            self.state.input_text = None
            self._missing_pending = 0
        elif len(self._queue) < 8:
            self._queue.append(req)
        else:
            log.warning("pending queue full, dropping %s", req.id)
        await self.render_now()

    async def on_event(self, etype: str, payload: dict) -> None:
        sid = payload.get("session_id")
        if sid and self.state.active and sid != self.state.active.id:
            return  # 只盯当前会话，别的会话的事件不抢屏
        self.state.apply_event(etype, payload)
        if etype == "message.complete":
            text = str(payload.get("text") or payload.get("content") or "").strip()
            if text:
                self.state.set_reply(text, self._reply_pages(text))
                # 回复只有在"人不在桌边"时才响：最近 IDLE_BEEP_S 内碰过按键就不打扰
                if time.time() - self.state.last_key_at >= IDLE_BEEP_S:
                    await self.hub.broadcast_text({"t": "notify", "kind": "reply"})
        if etype in ("request.cancel", "approval.cancelled"):
            # Mac 上先处理掉了：排队里的同一个 id 也必须一起撤，否则会“补发”一个死请求
            rid = str(payload.get("id") or payload.get("request_id") or "")
            if rid:
                self._mark_answered(rid)
                before = len(self._queue)
                self._queue = [q for q in self._queue if q.id != rid]
                if before != len(self._queue):
                    log.info("dropped %d queued copy of cancelled %s", before - len(self._queue), rid)
        if etype in ("message.delta", "reasoning.delta", "thinking.delta"):
            return  # 逐字流不进屏，省带宽也省眼
        if etype in ("request.cancel", "approval.cancelled", "status.update",
                     "tool.start", "tool.complete", "message.complete"):
            await self.render_now()

    # ── 按键 ────────────────────────────────────────────────────────────────
    async def on_key(self, key: str) -> None:
        now = time.time()
        self.state.last_key_at = now
        view = self.state.build_view(now)
        if key.startswith("char:"):
            await self._on_char(key.split(":", 1)[1])
            return
        if key.startswith("voice:"):
            # 外部端点送进来的转写文本（/stt + voice.confirm 这条老路）：
            # 一律先上屏预览，按 enter 才发。不能因为来源不同就跳过“人看一眼”这一步。
            await self._stage_voice_text(key.split(":", 1)[1])
            return
        if view.page == PAGE_VOICE:
            await self._on_voice_key(key)
            return
        if view.page == PAGE_PROMPT and self.state.pending is not None:
            await self._on_prompt_key(key, now)
            return
        await self._on_status_key(key, now)

    async def _on_char(self, ch: str) -> None:
        """键盘打字：只有开放式提问（没有选项）才走这条路。"""
        req = self.state.pending
        if req is None or req.method != "clarify" or req.choices:
            return
        self.state.input_text = (self.state.input_text or "") + ch
        await self.render_now(force=True)

    async def _stage_voice_text(self, text: str) -> None:
        req = self.state.pending
        if not text.strip():
            self.state.note("没听清，再说一次")
            await self.render_now()
            return
        if req is None or req.method != "clarify":
            self.state.note("现在没有要回答的问题")
            await self.render_now()
            return
        self.state.voice_preview = text.strip()
        self.state.note("按 enter 才发出")
        self.audit.write("voice.staged", chars=len(text), source="device-endpoint")
        await self.render_now()

    def _question_pages(self, text: str) -> list[list[str]]:
        """问题正文按屏宽折行、每页 2 行（选项行要留位置）。"""
        f = self.fonts.compact          # 必须与渲染时同一字号，否则会被二次截断
        max_w = renderer.W - 16          # 留边距：折出来的行到渲染器还要再量一次
        lines: list[str] = []
        for para in str(text).split("\n"):
            para = para.strip()
            if not para:
                continue
            lines.extend(renderer.wrap_text(para, f, max_w, max_lines=99))
        return [lines[i:i + 2] for i in range(0, len(lines), 2)] or [[str(text)]]

    def _option_pages(self, choices: list) -> list[list[str]]:
        """把选项排成"每页最多 2 行"：能并排就并排，太长就一行一个。"""
        f = self.fonts.compact_small
        max_w = renderer.W - 12
        items = [f"[{i + 1}]{str(c)}" for i, c in enumerate(choices)]
        lines: list[str] = []
        buf = ""
        for it in items:
            cand = f"{buf}  {it}" if buf else it
            if buf and f.getlength(cand) <= max_w:
                buf = cand
            else:
                if buf:
                    lines.append(buf)
                buf = it
        if buf:
            lines.append(buf)
        return [lines[i:i + 2] for i in range(0, len(lines), 2)] or [[""]]

    def _adopt_question_pages(self, req) -> None:
        if req is not None and req.method == "clarify":
            self.state.q_pages = self._question_pages(str(req.question.get("question") or ""))
            self.state.q_page = 0
            self.state.opt_pages = self._option_pages(req.choices) if req.choices else []
            self.state.opt_page = 0

    def _reply_pages(self, text: str) -> list[list[str]]:
        """把回复按屏幕宽度折行、5 行一页（用紧凑页的 15px 字体量）。"""
        f = self.fonts.compact
        max_w = renderer.W - 14
        lines: list[str] = []
        for para in text.split("\n"):
            para = para.strip()
            if not para:
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            lines.extend(renderer.wrap_text(para, f, max_w, max_lines=99))
        while lines and lines[-1] == "":
            lines.pop()
        return [lines[i:i + 5] for i in range(0, len(lines), 5)] or [[""]]

    async def _on_status_key(self, key: str, now: float) -> None:
        if getattr(self.state.build_view(now), "page", "") == PAGE_REPLY:
            # 回复页：fn 往下翻、del 往回翻、enter/esc 收起；1-3 让给会话切换
            pages = len(self.state.reply_pages or [])
            if key == "fn1":
                self.state.reply_page = min(pages - 1, self.state.reply_page + 1)
                await self.render_now(force=True)
                return
            if key == "back":
                self.state.reply_page = max(0, self.state.reply_page - 1)
                await self.render_now(force=True)
                return
            if key == "space":
                if self.state.cycle_reply(1):
                    await self.render_now(force=True)
                return
            if key in ("enter", "esc"):
                self.state.dismiss_reply()
                await self.render_now(force=True)
                return
            if key in ("1", "2", "3"):
                self.state.dismiss_reply()      # 收起后继续走会话切换
            else:
                await self.render_now(force=True)
                return
        if key in ("1", "2", "3", "4", "5"):
            idx = int(key[-1]) - 1
            if 0 <= idx < len(self.state.sessions):
                self.state.active_index = idx
                picked = self.state.sessions[idx]
                self.state.scroll = 0
                self.state.dismiss_reply()
                self.state.reply_text = ""      # 换了会话，上一条回复就不再是"上一条"了
                self.state.note(f"已切到 {idx + 1}/{len(self.state.sessions)} "
                                f"{picked.short_title}")
                await self._re_attach_active()
            else:
                self.state.note("没有这个会话")
        elif key == "fn1":
            # fn = 往更早翻（状态页的正文可以滚）
            if self.state.scroll_by(1):
                self.state.note(f"往前 {self.state.scroll} 页")
            else:
                self.state.note("已经是最早的了")
        elif key == "back":
            # del = 往更新翻，到 0 就是最新
            if self.state.scroll_by(-1):
                self.state.note("最新" if not self.state.scroll
                                else f"往前 {self.state.scroll} 页")
            else:
                self.state.note("已经是最新")
        elif key in ("enter", "space"):
            if self.state.scroll:
                self.state.scroll = 0
                self.state.note("已回最新")
            elif self.state.reply_text:
                # 收起后还能回来：重新打开上一条回复（从第 1 页开始）
                self.state.reply_at = time.time()
                self.state.reply_page = 0
                self.state.note("打开上一条回复")
            else:
                self.state.note("已刷新")
                await self._refresh_snapshot()
        elif key in ("esc", "power"):
            # 打断是危险动作：5 秒内连按两次 esc 才算
            if self._interrupt_armed_at and now - self._interrupt_armed_at <= 5.0:
                self._interrupt_armed_at = None
                sid = self.state.active.id if self.state.active else ""
                if sid:
                    await self.gateway.interrupt(sid)
                    self.audit.write("session.interrupt", session_id=sid, via="device")
                    self.state.note("已发送打断")
            else:
                self._interrupt_armed_at = now
                self.state.note("再按一次打断")
        elif key == "mic":
            await self.hub.broadcast_text({"t": "record", "ms": 10000})
            self.state.note("请按住侧键说话")
        await self.render_now()

    async def _on_prompt_key(self, key: str, now: float) -> None:
        req = self.state.pending
        assert req is not None
        if req.is_stale:
            self.state.note("请求已过期，去 Mac 上处理")
            await self.render_now()
            return
        if key == "space":
            # 待批队列里换一张：当前这张排到队尾，下一张浮上来（内容仍由后端给，不重解释）
            if self._queue:
                cur = self.state.pending
                self.state.pending = self._queue.pop(0)
                if cur is not None:
                    self._queue.append(cur)
                self._adopt_question_pages(self.state.pending)
                self.state.input_text = None
                self.state.note("换下一张待批")
            await self.render_now(force=True)
            return
        if key in ("esc",):
            if req.method == "clarify":
                await self._answer(clarify_result({}))
            else:
                await self._answer(approval_result(req.params, "deny"))
            return
        if req.method == "clarify":
            if self.state.input_text is not None:
                if key == "back":
                    self.state.input_text = self.state.input_text[:-1]
                    await self.render_now(force=True)
                    return
                if key == "enter":
                    said = self.state.input_text.strip()
                    self.state.input_text = None
                    if said:
                        await self._answer(clarify_result(
                            {str(req.question.get("qid") or "q1"): said}))
                    else:
                        await self.render_now(force=True)
                    return
                if key == "esc":
                    self.state.input_text = None
                    await self.render_now(force=True)
                    return
            if key in ("fn1", "back"):
                # 正文有多页先翻正文；否则翻选项（>4 个时）
                step = 1 if key == "fn1" else -1
                nq, no = len(self.state.q_pages), max(1, len(self.state.opt_pages))
                if nq > 1 and (self.state.q_page < nq - 1 if step > 0 else self.state.q_page > 0):
                    # 正文还没翻到头：先翻正文
                    self.state.q_page = max(0, min(nq - 1, self.state.q_page + step))
                elif no > 1:
                    # 正文到了最后一页：接着翻选项（循环）
                    self.state.opt_page = (self.state.opt_page + step) % no
                elif nq > 1:
                    self.state.q_page = max(0, min(nq - 1, self.state.q_page + step))
                await self.render_now(force=True)
                return
            if key == "mic":
                await self.hub.broadcast_text({"t": "record", "ms": 10000})
                return
            choices = req.choices
            if key in ("1", "2", "3", "4"):
                idx = int(key) - 1
                if idx >= len(choices):
                    self.state.note("没有这个选项")
                    await self.render_now()
                    return
                qid = str(req.question.get("qid") or f"q{req.q_index + 1}")
                req.answers[qid] = choices[idx]
                if req.q_index + 1 < len(req.questions):
                    req.q_index += 1
                    await self.render_now()
                    return
                await self._answer(clarify_result(req.answers))
            return
        # approval：屏上与按键共用 req.approval（ApprovalFacts），不再各自解析 params
        facts = req.approval
        choices = req.choices

        if key in ("fn1", "back"):
            if facts.page_count > 1:
                step = 1 if key == "fn1" else -1
                req.page = max(0, min(facts.page_count - 1, req.page + step))
                req.seen_pages.add(req.page)
            await self.render_now()
            return

        if key not in ("1", "2", "3", "4"):
            return
        # 固定键位：1=同意一次、2=拒绝、3=本会话、4=永久。绝不按后端返回顺序编号
        keymap = approval_key_map(choices)
        choice = keymap.get(key)
        if choice is None:
            self.state.note("没有这个选项")
            await self.render_now()
            return

        # 命令缺失 / 为空 / 类型不对 / 长到屏上放不下：设备端只能拒绝
        if not facts.valid:
            if choice == "deny":
                await self._answer(approval_result(req.params, choice))
            else:
                self.state.note("内容不完整，只能拒绝")
                await self.render_now()
            return

        # 长命令必须翻完才能批；拒绝永远可用（整改建议：拒绝操作应明确可用）
        if choice != "deny":
            req.seen_pages.add(min(req.page, facts.page_count - 1))
            if len(req.seen_pages) < facts.page_count:
                self.state.note(f"还有 {facts.page_count - len(req.seen_pages)} 页没看")
                await self.render_now()
                return

        if facts.dangerous and choice != "deny":
            armed = (req.confirm_armed and req.confirm_choice == choice
                     and req.confirm_digest == facts.digest
                     and now - req.confirm_at <= CONFIRM_WINDOW_S)
            if armed:
                await self._answer(approval_result(req.params, choice))
            else:
                req.confirm_armed = True
                req.confirm_choice = choice
                req.confirm_at = now
                req.confirm_digest = facts.digest  # 绑定这次请求的内容：内容一变，旧确认失效
                self.state.note(f"再按一次 {key} 确认{approval_choice_label(choice)}")
                await self.render_now()
            return
        await self._answer(approval_result(req.params, choice))

    async def _on_voice_key(self, key: str) -> None:
        if key in ("enter", "confirm"):
            text = self.state.voice_preview or ""
            await self._submit_voice_text(text)
        elif key in ("esc", "cancel"):
            self.state.voice_preview = None
            self.state.voice_token = None
            self.state.note("已放弃这句话")
            await self.render_now()
        elif key == "mic":
            self.state.voice_preview = None
            self.state.voice_token = None
            await self.hub.broadcast_text({"t": "record", "ms": 10000})
            await self.render_now()

    async def _submit_voice_text(self, text: str) -> None:
        req = self.state.pending
        self.state.voice_preview = None
        self.state.voice_token = None
        said = text.strip()
        if not said:
            self.state.note("没听清，再说一次")
            await self.render_now()
            return
        if req is not None and req.method == "clarify":
            if req.is_stale:
                self.state.note("请求已过期，去 Mac 上处理")
                await self.render_now()
                return
            qid = str(req.question.get("qid") or "q1")
            await self._answer(clarify_result({qid: said}))
            return
        if req is not None:
            # 审批不能靠嘴批：那是安全底线，只能按键
            self.state.confirm("先处理这张卡", "审批只能用按键", style="danger")
            await self.render_now()
            return
        # 没有待答问题：把这句话当成一条新消息发给助手（桌面端敲字走的也是 prompt.submit）
        sid = self.state.active.id if self.state.active else ""
        if not sid or not self.gateway.connected:
            self.state.confirm("没连上", "桥或后端断开", style="danger")
            await self.render_now()
            return
        try:
            await self.gateway.call("prompt.submit",
                                    {"session_id": sid, "text": said}, timeout=20)
        except GatewayError as exc:
            log.warning("prompt.submit failed: %s", exc)
            self.state.confirm("没发出去", str(exc)[:34], style="danger")
        else:
            self.audit.write("voice.prompt_submitted", session_id=sid, chars=len(said))
            self.state.confirm("已发给助手 √", said[:30])
        await self.render_now()

    # ── 作答 ────────────────────────────────────────────────────────────────
    async def _answer(self, result: dict) -> None:
        """把答案真的送到后端，然后才敢在屏上/审计里说“已发送”。

        顺序是刻意的：先走 ``request.answer`` RPC（后端会回 ``ok`` / ``expired``，
        能确定请求还在）；只有老后端不认这个方法时才退回响应帧。
        以前直接用响应帧，重连后 id 已经失效也照写“已发送”，那是假成功。
        """
        req = self.state.pending
        if req is None:
            return
        if req.is_stale:
            self._mark_answered(req.id)
            self.state.confirm("请求已过期", "去 Mac 上处理", style="danger")
            self._next_pending()
            await self.render_now()
            return
        delivered, status = await self._deliver(req, result)
        if not delivered:
            if status == "expired":
                self._mark_answered(req.id)
                self.audit.write("request.expired", request_id=req.id, method=req.method,
                                 session_id=req.session_id, age_s=round(req.age, 1))
                self.state.confirm("后端说已过期", "去 Mac 上处理", style="danger")
                self._next_pending()
            else:
                self.audit.write("request.delivery_failed", request_id=req.id,
                                 method=req.method, error=str(status)[:160])
                self.state.confirm("没送到", str(status)[:34], style="danger")
            await self.render_now()
            return
        self.audit.write("request.answered", request_id=req.id, method=req.method,
                         session_id=req.session_id, via="device", delivery=status,
                         choice=result.get("choice"),
                         answers=result.get("answers"),
                         command=str(req.params.get("command") or "")[:200],
                         age_s=round(req.age, 1))
        self.state.confirm(*self._confirm_words(req, result))
        self._mark_answered(req.id)
        self._next_pending()
        await self.render_now()

    @staticmethod
    def _confirm_words(req: PendingRequest, result: dict) -> tuple[str, str]:
        """按键生效后屏上写什么——必须说清“成没成、做了什么”。"""
        tool = str(req.params.get("tool_name") or "工具")
        if req.method == "clarify":
            answers = result.get("answers") or {}
            picked = next(iter(answers.values()), "")
            return "已作答 √", str(picked)[:32]
        choice = str(result.get("choice") or "")
        return {
            "deny": ("已拒绝 √", f"{tool} · 命令未执行"),
            "once": ("已同意一次 √", f"{tool} · 只这一次"),
            "session": ("本会话已同意 √", f"{tool} · 本会话有效"),
            "always": ("已永久同意 √", f"{tool} · 之后不再问"),
        }.get(choice, ("已发送 √", tool))

    async def _deliver(self, req: PendingRequest, result: dict) -> tuple[bool, str]:
        """返回 (是否送达, 状态)。状态会进审计，方便事后对账。"""
        try:
            res = await self.gateway.answer_via_rpc(req.id, result)
        except RpcError as exc:
            if exc.code in (-32601, -32602):  # 老后端没有 request.answer
                try:
                    await self.gateway.send_response(req.id, result)
                    return True, "response-frame"
                except GatewayError as send_exc:
                    return False, str(send_exc)
            return False, exc.message or str(exc)
        except GatewayError as exc:
            return False, str(exc)
        status = str((res or {}).get("status") or "").lower()
        if status in ("expired", "missing", "not_found"):
            return False, "expired"
        return True, status or "ok"

    def _mark_answered(self, rid: str) -> None:
        if not rid:
            return
        now = time.time()
        self._answered[rid] = now
        # 只留最近 15 分钟，防止长时间运行时无界增长
        for old, at in list(self._answered.items()):
            if now - at > 900:
                self._answered.pop(old, None)

    def _is_answered(self, rid: str) -> bool:
        at = self._answered.get(rid)
        return at is not None and time.time() - at <= 900

    def _next_pending(self) -> None:
        self._missing_pending = 0
        while self._queue:
            nxt = self._queue.pop(0)
            if nxt.is_stale or self._is_answered(nxt.id):
                continue
            self.state.pending = nxt
            self._adopt_question_pages(nxt)
            return
        self.state.pending = None

    # ── 语音上传 ────────────────────────────────────────────────────────────
    async def on_voice_state(self, kind: str) -> None:
        """录音 / 识别中 / 出错：板子上要有明确状态，不能按住键一片死寂。"""
        if kind == "recording":
            self.state.recording, self.state.transcribing = True, False
        elif kind == "transcribing":
            self.state.recording, self.state.transcribing = False, True
        elif kind == "done":
            self.state.recording = self.state.transcribing = False
        elif kind.startswith("error:"):
            self.state.recording = self.state.transcribing = False
            self.state.confirm("识别失败", kind.split(":", 1)[1][:34] or "未知原因",
                               style="danger")
        await self.render_now(force=True)

    async def on_voice_upload(self, pcm: bytes, rate: int) -> str:
        if self.transcriber is None:
            raise TranscriptionUnavailable("语音转写没启用")
        # 用当前会话上下文做热词：最近回复 + 最近工具目标（专有名词/路径最容易听错）
        ctx = " ".join([self.state.last_reply or ""] +
                       [str(e.get("target") or "") for e in self.state.recent[-3:]] +
                       [str(e.get("summary") or "")[:40] for e in self.state.recent[-3:]])
        text = await asyncio.to_thread(self.transcriber.transcribe, pcm, rate=rate,
                                       prompt_extra=ctx)
        self.state.voice_preview = text.strip()
        self.audit.write("voice.transcribed", chars=len(text), pcm_bytes=len(pcm))
        await self.render_now()
        return text

    # ── 渲染 ────────────────────────────────────────────────────────────────
    async def _announce_pending(self) -> None:
        """新请求出现时让板子响一声：屏在桌上、人不在旁边，没声音就白等。"""
        req = self.state.pending
        rid = req.id if req is not None else None
        if rid == self._announced_id:
            return
        self._announced_id = rid
        if rid is not None:
            log.info("notify device: new request %s", rid)
            await self.hub.broadcast_text({"t": "notify", "kind": "request"})

    async def render_now(self, *, force: bool = False, wake: bool = True) -> None:
        await self._announce_pending()
        # 屏上"待批 N"用：当前这张卡 + 队列里的
        self.state.queue_len = (1 if self.state.pending is not None else 0) + len(self._queue)
        # 电量/信号来自板子的心跳（hub.last_hello）：以前从来没往 state 里写过，
        # 所以底栏永远不显示——预览图里的数值是手填的，掩盖了这个 bug。
        hello = self.hub.last_hello or {}
        if hello.get("batt") is not None:
            self.state.device_battery = int(hello["batt"])
        if hello.get("rssi") is not None:
            self.state.device_rssi = int(hello["rssi"])
        if hello.get("chg") is not None:
            self.state.device_charging = int(hello["chg"])
        async with self._render_lock:
            view = self.state.build_view()
            key = self._view_signature(view)
            now = time.time()
            if not force and key == self._last_view_key:
                # 画面没变就别重发：板子每收一帧都要整屏重画，1Hz 重发就是 1Hz 闪屏。
                # 只在有设备在线时每 5 秒补一帧心跳（板子的帧超时是 10 秒）。
                if self.hub.device_count == 0 or now - self._last_sent_at < 5.0:
                    return
            self._last_view_key = key
            self.seq = (self.seq + 1) & 0xFFFF
            try:
                # 只有"要你动手/看一眼"的页面才点亮背光：审批卡、提问、回复、按键回声。
                # 而且只在**内容真的变了**时点亮一次：同一张卡每 5 秒的保活帧不唤醒，
                # 否则一张卡挂着 5 分钟，屏幕就亮 5 分钟。
                important = view.page in ("prompt", "reply", "confirm", "voice")
                want_wake = wake and important and key != self._last_wake_key
                if want_wake:
                    self._last_wake_key = key
                elif view.page == "status" and self.state.pending is None:
                    # 回到状态页就是"没有待办"：清掉指纹，下次有重要页面再点亮
                    self._last_wake_key = None
                frame = renderer.frame_for(view, self.seq, self.fonts, wake=want_wake)
            except Exception:
                log.exception("render failed")
                return
            data = frame.encode()
            log.debug("render page=%s seq=%d bytes=%d devices=%d",
                      view.page, self.seq, len(data), self.hub.device_count)
            self._last_sent_at = now
            await self.hub.broadcast_frame(data)

    @staticmethod
    def _view_signature(view: View) -> str:
        return "|".join([view.page, view.title, view.bar, view.footer, view.hint] +
                        [f"{ln.style}:{ln.text}" for ln in view.lines])

    # ── 定时器 ──────────────────────────────────────────────────────────────
    async def _ticker(self) -> None:
        interval = 1.0 / max(self.cfg.render_hz, 0.2)
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            try:
                if self.gateway.connected:
                    # 只轮询事件：不再周期性 session.activate（那会反复抢“当前客户端”，
                    # 一次 40KB 快照也会把 ticker 卡住几秒，板子就以为断线了）。
                    if now - self._last_event_poll_at >= self.cfg.event_poll_s:
                        await self._poll_events()
                    # 桌面端开了新对话就跟着换（轻量判，真换了才 attach 一次）
                    if now - self._last_follow_at >= 5.0:
                        await self._follow_active_session()
                devices = self.hub.device_count
                if devices != self._last_device_count:
                    self._last_device_count = devices
                    await self.render_now(force=True)   # 有设备刚连上/断开：立刻给一帧
                else:
                    # 周期刷新（信号/电量/时长变化）只更新画面，不点亮背光
                    await self.render_now(wake=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ticker step failed")

    async def _refresh_snapshot(self) -> None:
        sid = self.state.active.id if self.state.active else None
        if not sid:
            return
        try:
            snap = await self.gateway.attach(sid)
        except GatewayError as exc:
            log.debug("snapshot refresh failed: %s", exc)
            # 失败也要推后下一次：否则每秒重试一次，日志被刷爆之外没有好处
            self._last_snapshot_at = time.time()
            return
        self.state.apply_snapshot(snap)
        self._last_snapshot_at = time.time()

    async def _poll_events(self) -> None:
        sid = self.state.active.id if self.state.active else None
        if not sid:
            return
        try:
            res = await self.gateway.events_since(sid, self.gateway.last_seen)
        except GatewayError:
            return
        self._last_event_poll_at = time.time()
        for entry in res.get("open_requests") or []:
            # 每 3 秒都会把“所有还开着的请求”重报一遍；去重靠 on_server_request 里的
            # pending / queue / answered 三处检查，否则队列会越积越长。
            await self.on_server_request({"id": entry.get("id"), "method": entry.get("method"),
                                          "params": entry.get("params") or {}})
        # 注意：这份 open_requests **不含** clarify/approval 这类 srq- 服务端请求，
        # 拿它撤卡会把刚推上来的卡片 2 秒内误删（真机"卡片一闪而过"）。撤卡只靠
        # cancel 事件 / 作答 / STALE_AFTER_S 超时。

    def _absorb_open_requests(self, snap: dict) -> None:
        """把快照里“还开着的请求”补齐到屏上（队列最多 8 个，先到先答）。"""
        for entry in snap.get("open_requests") or []:
            if not isinstance(entry, dict):
                continue
            rid = str(entry.get("id") or "")
            method = str(entry.get("method") or "")
            if not rid or method not in ("clarify", "approval"):
                continue
            if self._is_answered(rid):
                continue
            if self.state.pending is not None and self.state.pending.id == rid:
                continue
            if any(q.id == rid for q in self._queue):
                continue
            req = PendingRequest(id=rid, method=method, params=entry.get("params") or {},
                                 session_id=str((entry.get("params") or {}).get("session_id") or ""),
                                 created_at=float(entry.get("created_at") or time.time()))
            if self.state.pending is None:
                self.state.pending = req
                self._adopt_question_pages(req)
            elif len(self._queue) < 8:
                self._queue.append(req)

    def _reconcile_pending(self, snap: dict) -> None:
        """已停用：open_requests 不含 srq- 服务端请求，用它撤卡必然误杀。

        留这个空实现只为兼容旧调用点；撤卡的唯一依据是 cancel 事件/作答/超时。
        """
        return

    async def _re_attach_active(self) -> None:
        sid = self.state.active.id if self.state.active else None
        if not sid or not self.gateway.connected:
            return
        try:
            snap = await self.gateway.attach(sid)
            self.state.apply_snapshot(snap)
            self._absorb_open_requests(snap)
            # 用户在板子上手动挑的会话，10 分钟内不被“跟随桌面端”抢回去
            self._attached_id = sid
            self._manual_until = time.time() + 600.0
        except GatewayError as exc:
            log.debug("attach failed: %s", exc)
        await self.render_now(force=True)
