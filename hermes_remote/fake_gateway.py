"""一个“假后端”：实现 tui_gateway 那套 JSON-RPC 的一个子集。

用途只有两个：`hermes-remote demo` 和自动化测试。它不是给真人用的，
也永远不该被当成“我们支持了 Hermes 之外的后端”。

它发出的事件形状与 ``tui_gateway/contracts/events.py`` 对齐：
``{"jsonrpc":"2.0","method":"event","params":{"type":..., "session_id":..., ...}}``，
询问帧与 ``contracts/server_requests.py`` 对齐（``clarify`` / ``approval``，id 形如 srq-xxx）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from aiohttp import WSMsgType, web

log = logging.getLogger("hermes_remote.fake_gateway")


class FakeGateway:
    def __init__(self, *, session_title: str = "Fake session") -> None:
        self.session_id = "sess-" + uuid.uuid4().hex[:8]
        self.session_title = session_title
        self.clients: set[web.WebSocketResponse] = set()
        self.seq = 0
        self.last_seen = 0
        self.open_requests: dict[str, dict] = {}
        self.answers: list[tuple[str, dict]] = []
        self._app = web.Application()
        self._app.router.add_get("/api/ws", self._ws)
        self._runner: web.AppRunner | None = None
        self.port = 0
        self.capabilities_seen = False

    # ── 生命周期 ────────────────────────────────────────────────────────────
    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception:
                pass
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/ws?token=fake"

    # ── WS ──────────────────────────────────────────────────────────────────
    async def _ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self.clients.add(ws)
        await ws.send_str(json.dumps({
            "jsonrpc": "2.0", "method": "event",
            "params": {"type": "gateway.ready", "payload": {"replay_epoch": "epoch-1"}},
        }))
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                frame = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            await self._handle(ws, frame)
        self.clients.discard(ws)
        return ws

    async def _reply(self, ws, rid, result=None, error=None):
        frame = {"jsonrpc": "2.0", "id": rid}
        if error:
            frame["error"] = error
        else:
            frame["result"] = result or {}
        await ws.send_str(json.dumps(frame, ensure_ascii=False))

    async def _handle(self, ws, frame: dict) -> None:
        if "method" not in frame and "id" in frame:
            # 客户端对我们询问的回答
            self.answers.append((str(frame.get("id")), frame.get("result") or {}))
            self.open_requests.pop(str(frame.get("id")), None)
            return
        method = frame.get("method")
        rid = frame.get("id")
        params = frame.get("params") or {}
        if method == "client.capabilities":
            self.capabilities_seen = bool(params.get("server_requests"))
            await self._reply(ws, rid, {"server_requests": ["approval", "clarify"],
                                        "declines_not_shown": True})
        elif method == "session.list":
            await self._reply(ws, rid, {"sessions": [
                {"id": self.session_id, "title": self.session_title,
                 "preview": "", "started_at": 0, "message_count": 1, "source": "fake"},
            ]})
        elif method == "session.activate":
            await self._reply(ws, rid, self.snapshot())
        elif method == "session.events.since":
            self.last_seen = int(params.get("last_seen") or 0)
            await self._reply(ws, rid, {"events": [], "latest_seq": self.seq,
                                        "truncated": False, "count": 0, "epoch": "epoch-1",
                                        "open_requests": list(self.open_requests.values())})
        elif method == "approval.pending":
            await self._reply(ws, rid, {"approvals": []})
        elif method == "request.answer":
            self.answers.append((str(params.get("id")), params.get("result") or {}))
            await self._reply(ws, rid, {"status": "accepted"})
        elif method == "session.interrupt":
            await self._reply(ws, rid, {"interrupted": True})
        else:
            await self._reply(ws, rid, error={"code": -32601, "message": f"no such method {method}"})

    def snapshot(self, status: str = "idle", running: bool = False) -> dict:
        return {
            "session_id": self.session_id,
            "message_count": 0,
            "messages": [],
            "info": {"title": self.session_title},
            "status": status,
            "running": running,
            "turn_started_at": None,
            "inflight": None,
            "pending_approval": None,
            "open_requests": list(self.open_requests.values()),
            "todo_state": None,
        }

    # ── 发事件 / 询问 ───────────────────────────────────────────────────────
    async def emit(self, etype: str, **payload) -> None:
        self.seq += 1
        frame = {"jsonrpc": "2.0", "method": "event",
                 "params": {"type": etype, "session_id": self.session_id,
                            "seq": self.seq, **payload}}
        for ws in list(self.clients):
            try:
                await ws.send_str(json.dumps(frame, ensure_ascii=False))
            except Exception:
                self.clients.discard(ws)

    async def ask(self, method: str, params: dict, *, timeout: float = 30.0) -> dict | None:
        rid = f"srq-{uuid.uuid4().hex[:12]}"
        self.open_requests[rid] = {"id": rid, "method": method, "params": params}
        frame = {"jsonrpc": "2.0", "id": rid, "method": method,
                 "params": {"session_id": self.session_id, **params}}
        for ws in list(self.clients):
            await ws.send_str(json.dumps(frame, ensure_ascii=False))
        before = len(self.answers)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if len(self.answers) > before:
                return self.answers[-1][1]
            await asyncio.sleep(0.02)
        self.open_requests.pop(rid, None)
        await self.emit("request.cancel", id=rid, method=method, reason="timeout")
        return None
