"""桥 ↔ Hermes 后端：JSON-RPC over WebSocket（只连 127.0.0.1）。

对应源码里桌面 App / dashboard 用的同一条通道（``/api/ws``）：
  * 连上后先收 ``event gateway.ready``；
  * 立刻发 ``client.capabilities {server_requests: true}``——不发的话后端会
    认为这个客户端不支持 clarify/approval，请求根本不会送到我们这里
    （tui_gateway/server_requests.py 的 ``_unanswerable``）；
  * ``session.activate`` 以“第二订阅者”身份挂上会话，不顶掉桌面 App；
  * 后端的询问是服务端发起的 JSON-RPC 请求（``clarify`` / ``approval``），
    回答就是一条带同一个 ``id`` 的响应帧。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import websockets
from websockets.asyncio.client import connect

log = logging.getLogger("hermes_remote.gateway")

SERVER_REQUEST_METHODS = frozenset({"clarify", "approval"})
DEFAULT_PORT = 9119


def build_url(port: int = DEFAULT_PORT, token: str = "", host: str = "127.0.0.1",
              path: str = "/api/ws") -> str:
    qs = urlencode({"token": token}) if token else ""
    return f"ws://{host}:{port}{path}" + (f"?{qs}" if qs else "")


class GatewayError(RuntimeError):
    pass


class RpcError(GatewayError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"rpc error {code}: {message}")
        self.code = code
        self.message = message


class GatewayClient:
    """一个常驻连接；断了自动重连，重连后重新挂会话并补拉事件。"""

    def __init__(self, url: str, *,
                 on_request: Callable[[dict], Awaitable[None]] | None = None,
                 on_event: Callable[[str, dict], Awaitable[None]] | None = None,
                 on_state: Callable[[bool], Awaitable[None]] | None = None) -> None:
        self.url = url
        self.on_request = on_request
        self.on_event = on_event
        self.on_state = on_state
        self.ws = None
        self._next_id = 1
        self._pending: dict[int | str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self.connected = False
        self.last_seen: int = 0
        self.session_id: str | None = None
        self.server_requests: list[str] = []
        self._reader: asyncio.Task | None = None
        self._callbacks: set[asyncio.Task] = set()
        self._closed = False
        self._ready = asyncio.Event()

    # ── 连接 ────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        """常驻：连不上就退避重试，断开就重连。直到 ``close()``。"""
        delay = 0.5
        while not self._closed:
            try:
                await self._connect_once()
                delay = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("gateway disconnected: %s", exc)
            finally:
                await self._mark_disconnected()
            if self._closed:
                return
            sleep_for = min(delay, 8.0) * (0.8 + random.random() * 0.4)
            log.info("gateway reconnect in %.1fs", sleep_for)
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, 8.0)

    async def _connect_once(self) -> None:
        async with connect(self.url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
            self.ws = ws
            log.info("gateway connected %s", self.url.split("?")[0])
            self._reader = asyncio.create_task(self._read_loop(ws))
            # 0.22.x 的网关只把 ``gateway.ready`` 发给第一个连上的客户端（通常是桌面端），
            # 后连的客户端收不到 —— 若等 ready 再声明能力，``client.capabilities`` 永远发不出去，
            # 后端于是按「老客户端」处理，审批/提问一律不推。读循环起来后立刻声明。
            try:
                res = await self.call("client.capabilities", {"server_requests": True}, timeout=5)
                self.server_requests = list((res or {}).get("server_requests") or [])
                log.info("client.capabilities ok (%d 类服务端请求)", len(self.server_requests))
            except Exception as exc:  # 老后端不认这个方法也要继续
                log.warning("client.capabilities failed: %s", exc)
            try:
                await self._reader
            finally:
                self.ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(GatewayError("gateway connection closed"))
                self._pending.clear()

    async def _mark_disconnected(self) -> None:
        was = self.connected
        self.connected = False
        self._ready.clear()
        if was and self.on_state:
            await self.on_state(False)

    def close(self) -> None:
        self._closed = True
        if self.ws is not None:
            with contextlib.suppress(Exception):
                asyncio.create_task(self.ws.close())

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ── 收帧 ────────────────────────────────────────────────────────────────
    def _spawn(self, coro) -> None:
        """回调一律起独立 task：读循环必须继续收帧，否则 call() 会自己等自己。"""
        task = asyncio.create_task(coro)
        self._callbacks.add(task)
        task.add_done_callback(self._callbacks.discard)

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            method = frame.get("method")
            params = frame.get("params") or {}
            frame_id = frame.get("id")
            if method == "event" and isinstance(params, dict):
                await self._handle_event(params)
            elif method and frame_id is None:
                # 老后端的一种写法：请求不带 id
                await self._handle_server_request(frame)
            elif method and isinstance(frame_id, str) and frame_id.startswith("srq-"):
                # 正常的服务端请求：同时带 id 和 method（clarify / approval / sudo / …）。
                # 之前这里写的是 `method and "id" not in frame`，于是所有 srq- 请求都被丢掉，
                # 只能靠轮询 open_requests 偶尔兜到。
                await self._handle_server_request(frame)
            elif frame_id is not None and method is None:
                key: int | str = frame_id
                if isinstance(frame_id, str) and frame_id.isdigit():
                    key = int(frame_id)
                fut = self._pending.pop(key, None)
                if fut and not fut.done():
                    if "error" in frame:
                        err = frame["error"] or {}
                        fut.set_exception(RpcError(int(err.get("code") or -1),
                                                   str(err.get("message") or "")))
                    else:
                        fut.set_result(frame.get("result"))

    async def _handle_event(self, params: dict) -> None:
        etype = params.get("type")
        if not etype:
            return
        seq = params.get("seq")
        if isinstance(seq, int) and seq > self.last_seen:
            self.last_seen = seq
        if etype == "gateway.ready":
            self._spawn(self._on_ready(params.get("payload") or {}))
            return
        payload = params.get("payload")
        if not isinstance(payload, dict):
            payload = {k: v for k, v in params.items() if k != "type"}
        if self.on_event:
            self._spawn(self.on_event(str(etype), payload))

    async def _on_ready(self, payload: dict) -> None:
        self.connected = True
        self._ready.set()
        try:
            res = await self.call("client.capabilities", {"server_requests": True}, timeout=5)
            self.server_requests = list((res or {}).get("server_requests") or [])
        except Exception as exc:  # 老后端不认这个方法也要继续
            log.warning("client.capabilities failed: %s", exc)
        if self.on_state:
            self._spawn(self.on_state(True))

    async def _handle_server_request(self, frame: dict) -> None:
        method = frame.get("method")
        params = frame.get("params") or {}
        if method not in SERVER_REQUEST_METHODS:
            # 我们不处理的询问（sudo/secret/window.read…）：明确回一个错误，
            # 让后端立刻走别的客户端或失败，而不是等满超时。
            await self.send_response(frame.get("id"), error={"code": -32601,
                                                             "message": f"hermes-remote: no handler for {method}"})
            return
        if self.on_request:
            self._spawn(self.on_request({"id": frame.get("id"), "method": method, "params": params}))

    # ── 发帧 ────────────────────────────────────────────────────────────────
    async def _send(self, obj: dict) -> None:
        if self.ws is None:
            raise GatewayError("gateway not connected")
        async with self._send_lock:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))

    async def call(self, method: str, params: dict | None = None, *, timeout: float = 20.0) -> Any:
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                              "params": params or {}})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def send_response(self, request_id: str | None, result: dict | None = None,
                            error: dict | None = None) -> None:
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            frame["error"] = error
        else:
            frame["result"] = result or {}
        await self._send(frame)

    async def approval_pending(self, session_id: str) -> list[dict]:
        """0.22.x：待批审批不进 srq 帧，只能问这个（内部是 gateway 的审批队列）。"""
        res = await self.call("approval.pending", {"session_id": session_id}) or {}
        rows = res.get("approvals")
        return [dict(r) for r in rows] if isinstance(rows, list) else []

    async def approval_respond(self, session_id: str, execution_generation: int,
                               prompt_id: str, choice: str) -> dict:
        """回答 canonical 协议下的待批请求（``prompt_id`` 来自 resume 快照的 ``pending``）。"""
        return await self.call("approval.respond", {
            "session_id": session_id,
            "execution_generation": execution_generation,
            "prompt_id": prompt_id,
            "choice": choice,
        }) or {}

    async def clarify_respond(self, session_id: str, execution_generation: int,
                              prompt_id: str, answer: str) -> dict:
        """回答 canonical 协议下的 clarify（``answer`` 为空串 = 跳过）。"""
        return await self.call("clarify.respond", {
            "session_id": session_id,
            "execution_generation": execution_generation,
            "prompt_id": prompt_id,
            "answer": answer,
        }) or {}

    async def answer_via_rpc(self, request_id: str, result: dict) -> dict:
        """没收到帧时（例如桥重启后只看到 open_requests）用 RPC 代答。"""
        return await self.call("request.answer", {"id": request_id, "result": result})

    # ── 会话 ────────────────────────────────────────────────────────────────
    async def list_sessions(self, limit: int = 8) -> list[dict]:
        res = await self.call("session.list", {"limit": limit})
        return list((res or {}).get("sessions") or [])

    async def active_sessions(self, current: str = "") -> list[dict]:
        """本进程里**活着**的会话。

        桌面 App 的后端里，``session.activate`` 只认活动列表里的 ``id``（短哈希，例如
        ``432cd0bc``），不认 ``session.list`` 的数据库 id（``20261018_122435_7a2cca``）——
        拿后者去 activate 会得到 ``4001 session not found``。所以挂会话要先用这个。
        """
        params: dict = {}
        if current:
            params["current_session_id"] = current
        try:
            res = await self.call("session.active_list", params, timeout=10)
        except GatewayError:
            return []
        rows = list((res or {}).get("sessions") or [])
        return [r for r in rows if isinstance(r, dict)]

    async def attach(self, session_id: str) -> dict:
        # 0.22.x 起网关把 ``session.activate`` 换成了 ``session.resume``（activate 回 -32601
        # unknown method）；老网关反过来。两个都试，先新的。
        last: GatewayError | None = None
        for method in ("session.resume", "session.activate"):
            try:
                res = await self.call(method, {"session_id": session_id,
                                               "omit_messages": True})
            except GatewayError as exc:
                last = exc
                if getattr(exc, "code", None) == -32601:  # 这个方法这版没有，换另一个
                    continue
                raise
            self.session_id = session_id
            return res or {}
        raise last or GatewayError(-1, "attach failed")

    async def events_since(self, session_id: str, last_seen: int) -> dict:
        res = await self.call("session.events.since",
                              {"session_id": session_id, "last_seen": last_seen}) or {}
        # 0.22.x 回 ``latest_seq`` + ``truncated``/``snapshot_required``：服务端只保留最近一段事件，
        # 从 0 开始拉永远被判「太旧」，于是 count 恒为 0、实时事件也永远追不上。第一次拿到
        # latest_seq 就跳过去，之后靠事件帧里的 seq 递增。
        latest = res.get("latest_seq")
        if isinstance(latest, int) and latest > self.last_seen:
            self.last_seen = latest
        return res

    async def interrupt(self, session_id: str) -> dict:
        try:
            return await self.call("session.interrupt", {"session_id": session_id}) or {}
        except GatewayError:
            return {}

    async def pending_approvals(self, session_id: str) -> list[dict]:
        try:
            res = await self.call("approval.pending", {"session_id": session_id})
            return list((res or {}).get("approvals") or [])
        except GatewayError:
            return []

    @staticmethod
    def is_websockets_available() -> bool:
        return websockets is not None
