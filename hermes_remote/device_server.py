"""桥面向板子的局域网服务：屏帧下行 + 按键上行 + 语音上传。

只有一条自己定的协议（见 protocol.py），不依赖 Hermes 的接口形状。
端口 47823（HTTP + WebSocket 同一个口），UDP 47824 做发现（beacon.py）。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
from typing import Awaitable, Callable

from aiohttp import WSMsgType, web

from . import protocol
from .stt import TranscriptionUnavailable

log = logging.getLogger("hermes_remote.device")

LAN_PORT = 47823
MAX_PCM_BYTES = 1024 * 1024  # 32 秒 16k/16bit，够用且不会被灌爆


class DeviceHub:
    def __init__(self, token: str, *,
                 on_key: Callable[[str], Awaitable[None]],
                 on_voice_upload: Callable[[bytes, int], Awaitable[str]],
                 on_voice_state: Callable[[str], Awaitable[None]] | None = None) -> None:
        self.token = token
        self.on_key = on_key
        self.on_voice_upload = on_voice_upload
        self.on_voice_state = on_voice_state
        self.clients: set[web.WebSocketResponse] = set()
        self.last_frame: bytes | None = None
        self.last_hello: dict | None = None
        #: 一次性语音 token → (文本, 生成时间)；只保留最后一句，且 120 秒后失效
        self.voice_tokens: dict[str, tuple[str, float]] = {}
        self._voice: bytearray | None = None
        self._voice_owner: web.WebSocketResponse | None = None
        self._voice_rate: int = 16000
        self._voice_started: float = 0.0
        #: 连上后等 auth 帧的秒数（测试里调小）
        self.auth_timeout: float = 10.0
        self._app = web.Application()
        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_post("/stt", self._stt_handler)
        self._app.router.add_get("/healthz", self._health)
        self._runner: web.AppRunner | None = None
        self.port: int = LAN_PORT

    # ── 生命周期 ────────────────────────────────────────────────────────────
    async def start(self, host: str = "0.0.0.0", port: int | None = None) -> int:
        self.port = port if port is not None else self.port
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, self.port)
        await site.start()
        sockets = getattr(getattr(site, "_server", None), "sockets", None)
        if sockets:
            self.port = sockets[0].getsockname()[1]  # port=0 → 取系统分配的端口
        log.info("device hub listening on %s:%d", host, self.port)
        return self.port

    async def stop(self) -> None:
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception:
                pass
        self.clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @property
    def device_count(self) -> int:
        return len(self.clients)

    # ── 鉴权 ────────────────────────────────────────────────────────────────
    def _authorized(self, request: web.Request) -> bool:
        given = request.query.get("token") or request.headers.get("X-Device-Token") or ""
        if not given or not self.token:
            return False
        return hmac.compare_digest(given.encode(), self.token.encode())

    # ── WebSocket ───────────────────────────────────────────────────────────
    async def _ws_handler(self, request: web.Request) -> web.StreamResponse:
        pre_authed = self._authorized(request)  # 兼容老的 ?token= / X-Device-Token
        ws = web.WebSocketResponse(heartbeat=40, max_msg_size=MAX_PCM_BYTES)
        await ws.prepare(request)
        if not pre_authed:
            # 新固件把 token 放在第一条 {"t":"auth"} 里：URL 不进访问日志、不进浏览器历史
            if not await self._await_auth(ws, timeout=self.auth_timeout):
                await ws.close(code=4401, message=b"bad token")
                return ws
            await ws.send_str(json.dumps({"t": "welcome", "proto": 1}))
        self.clients.add(ws)
        log.info("device connected %s (%d online)", request.remote, len(self.clients))
        try:
            if self.last_frame:
                await ws.send_bytes(self.last_frame)
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_text(msg.data, ws)
                elif msg.type == WSMsgType.BINARY:
                    await self._handle_binary(msg.data, ws)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
            if self._voice_owner is ws:
                self._voice = None
                self._voice_owner = None
            log.info("device disconnected (%d online, close=%s)", len(self.clients),
                     getattr(ws, "close_code", None))
        return ws

    async def _voice_state(self, kind: str) -> None:
        """把录音/转写阶段推给桥（板子上要看得见进度，不能按住键没反应）。"""
        if self.on_voice_state is None:
            return
        try:
            await self.on_voice_state(kind)
        except Exception:  # pragma: no cover - 桥侧异常不该拖垮连接
            log.debug("on_voice_state(%s) failed", kind, exc_info=True)

    async def _await_auth(self, ws: web.WebSocketResponse, timeout: float = 10.0) -> bool:
        """连上后第一条必须是 {"t":"auth","token":...}；超时或不对就断开。"""
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        if msg.type != WSMsgType.TEXT:
            return False
        try:
            frame = protocol.parse_device_frame(msg.data)
        except ValueError:
            return False
        if frame.get("t") != "auth":
            return False
        given = str(frame.get("token") or "")
        return bool(given) and hmac.compare_digest(given.encode(), self.token.encode())

    async def _handle_text(self, raw: str, ws: web.WebSocketResponse) -> None:
        try:
            frame = protocol.parse_device_frame(raw)
        except ValueError as exc:
            log.warning("bad device frame: %s", exc)
            await ws.send_str(json.dumps({"t": "error", "msg": str(exc)}, ensure_ascii=False))
            return
        kind = frame["t"]
        if kind == "key":
            await self.on_key(frame["k"])
        elif kind == "hb":
            self.last_hello = {"rssi": frame.get("rssi"), "batt": frame.get("batt"),
                               "uptime": frame.get("up"), "fw": frame.get("fw"),
                               "at": time.time()}
        elif kind == "log":
            # 板子自报状态（背光暗/亮等）：看不到屏幕时这是唯一可靠证据
            log.info("device log: %s", frame.get("m"))
        elif kind == "hello":
            self.last_hello = {"fw": frame.get("fw"), "at": time.time()}
            await ws.send_str(json.dumps({"t": "welcome", "proto": 1}))
        elif kind == "auth":
            # 已经用 ?token= 过鉴权的连接再发一次也无害
            await ws.send_str(json.dumps({"t": "welcome", "proto": 1}))
        elif kind == "voice.begin":
            if self._voice_owner is not None and self._voice_owner is not ws:
                await ws.send_str(json.dumps({"t": "voice.error", "msg": "another device is recording"}))
                return
            self._voice = bytearray()
            self._voice_owner = ws
            self._voice_rate = int(frame.get("rate") or 16000)
            self._voice_started = time.time()
            await ws.send_str(json.dumps({"t": "voice.recording"}))
            await self._voice_state("recording")
        elif kind == "voice.end":
            pcm = bytes(self._voice or b"")
            self._voice = None
            self._voice_owner = None
            await self._voice_state("transcribing")
            if not pcm:
                await self._voice_state("error:empty audio")
                await ws.send_str(json.dumps({"t": "voice.error", "msg": "empty audio"}))
                return
            if self.on_voice_upload is None:
                await self._voice_state("error:stt disabled")
                await ws.send_str(json.dumps({"t": "voice.error", "msg": "stt disabled"}))
                return
            try:
                text = await self.on_voice_upload(pcm, self._voice_rate)
            except Exception as exc:
                log.exception("streamed transcription failed")
                await self._voice_state(f"error:{exc}")
                await ws.send_str(json.dumps({"t": "voice.error", "msg": str(exc)},
                                             ensure_ascii=False))
                return
            await self._voice_state("done")
            await ws.send_str(json.dumps({"t": "voice.text", "text": text},
                                         ensure_ascii=False))
        elif kind == "voice.upload":
            await ws.send_str(json.dumps({"t": "voice.ack"}))
        elif kind == "voice.confirm":
            entry = self.voice_tokens.get(str(frame.get("token")))
            if entry is not None:
                text, at = entry
                if time.time() - at <= 120:
                    self.voice_tokens.clear()
                    await self.on_key(f"voice:{text}")
                else:
                    self.voice_tokens.clear()
                    await ws.send_str(json.dumps({"t": "voice.error", "msg": "stale token"}))
        elif kind == "voice.cancel":
            await self.on_key("cancel")

    async def _handle_binary(self, data: bytes, ws: web.WebSocketResponse) -> None:
        try:
            ftype, payload = protocol.parse_binary_frame(data)
        except protocol.FrameError as exc:
            log.warning("bad binary frame: %s", exc)
            return
        if ftype == protocol.TYPE_VOICE:
            if self._voice_owner is not None and self._voice_owner is not ws:
                log.warning("voice chunk from a non-recording client ignored")
                return
            if self._voice is None:
                self._voice = bytearray()
                self._voice_owner = ws
            if len(self._voice) + len(payload) > protocol.MAX_VOICE_BYTES:
                log.warning("voice buffer overflow, dropping stream")
                self._voice = None
                self._voice_owner = None
                await ws.send_str(json.dumps({"t": "voice.error", "msg": "too long"}))
                return
            self._voice += payload

    async def broadcast_text(self, obj: dict) -> int:
        """给板子发一条小 JSON（例如让它开始录音）。"""
        text = json.dumps(obj, ensure_ascii=False)
        dead = []
        for ws in list(self.clients):
            try:
                # 一定要有超时：某个客户端不读，send 会一直挂着，把整个渲染循环拖死
                await asyncio.wait_for(ws.send_str(text), timeout=3.0)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
        return len(self.clients)

    async def broadcast_frame(self, frame_bytes: bytes) -> int:
        self.last_frame = frame_bytes
        dead = []
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send_bytes(frame_bytes), timeout=3.0)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
        return len(self.clients)

    # ── 语音 ────────────────────────────────────────────────────────────────
    async def _stt_handler(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return web.Response(status=401, text="bad token")
        if self.on_voice_upload is None:
            return web.json_response({"error": "stt disabled"}, status=503)
        pcm = await request.read()
        if not pcm:
            return web.json_response({"error": "empty audio"}, status=400)
        if len(pcm) > MAX_PCM_BYTES:
            return web.json_response({"error": "audio too long"}, status=413)
        rate = int(request.query.get("rate", "16000"))
        try:
            text = await self.on_voice_upload(pcm, rate)
        except TranscriptionUnavailable as exc:
            return web.json_response({"error": str(exc)}, status=503)
        except Exception as exc:  # 转写炸了不能把桥带下去
            log.exception("transcription failed")
            return web.json_response({"error": f"transcribe failed: {exc}"}, status=500)
        token = secrets.token_hex(8)
        self.voice_tokens = {token: (text, time.time())}  # 只保留最后一句，120 秒内有效
        return web.json_response({"text": text, "token": token, "rate": rate})

    async def _health(self, request: web.Request) -> web.StreamResponse:
        # 不带 token 只回一个“活着”，设备在线数/固件版本/帧大小这些不给外网看
        if not self._authorized(request):
            return web.json_response({"ok": True})
        return web.json_response({
            "ok": True,
            "devices": len(self.clients),
            "last_hello": self.last_hello,
            "proto": 1,
            "frame": len(self.last_frame) if self.last_frame else 0,
        })
