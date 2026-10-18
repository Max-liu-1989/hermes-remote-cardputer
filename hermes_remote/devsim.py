"""模拟板子：连上桥、解帧、把屏画成字符画、按键。

没有硬件时用它验收。协议与真固件完全一致（同样的 9 字节头 + RLE），
所以它跑通就说明“桥推的帧、板子能解出来”这件事是对的。
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
from PIL import Image

from . import protocol, renderer
from .gateway import DEFAULT_PORT  # noqa: F401  (re-export convenience)


def bitmap_to_image(bitmap: bytes, w: int = protocol.SCREEN_W,
                    h: int = protocol.SCREEN_H) -> Image.Image:
    return Image.frombytes("1", (w, h), bitmap)


def palette_to_image(packed: bytes, w: int = protocol.SCREEN_W,
                     h: int = protocol.SCREEN_H) -> Image.Image:
    """4 位调色板打包字节 → RGB 图（demo / 测试看效果用）。"""
    pal = protocol.PALETTE_RGB
    data = bytearray(w * h * 3)
    for p, idx in enumerate(protocol.palette_indices(packed)):
        r, g, b = pal[idx & 0x0F]
        data[p * 3:p * 3 + 3] = bytes((r, g, b))
    return Image.frombytes("RGB", (w, h), bytes(data))


class SimDevice:
    def __init__(self, host: str = "127.0.0.1", port: int = 47823, token: str = "") -> None:
        self.host = host
        self.port = port
        self.token = token
        self.ws = None
        self.frames: list[protocol.ScreenFrame] = []
        self.texts: list[dict] = []
        self._session: aiohttp.ClientSession | None = None
        self._reader: asyncio.Task | None = None
        self._frame_event = asyncio.Event()
        self._authed = asyncio.Event()
        self._auth_error: Exception | None = None

    @property
    def url(self) -> str:
        # token 不再放 URL：连上后第一条消息再认证（与真固件一致）
        return f"ws://{self.host}:{self.port}/ws"

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            self.ws = await self._session.ws_connect(self.url, max_msg_size=1024 * 1024)
        except Exception:
            await self._session.close()
            self._session = None
            raise
        self._reader = asyncio.create_task(self._read_loop())
        await self.send({"t": "auth", "token": self.token})
        authed = asyncio.create_task(self._authed.wait())
        await asyncio.wait({authed, self._reader}, timeout=6.0,
                           return_when=asyncio.FIRST_COMPLETED)
        if not self._authed.is_set():
            authed.cancel()
            err = self._auth_error or PermissionError("device auth rejected")
            await self.close()
            raise err
        authed.cancel()
        await self.send({"t": "hello", "fw": "sim-1.0"})

    async def _read_loop(self) -> None:
        assert self.ws is not None
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                frame = protocol.ScreenFrame.decode(msg.data)
                self.frames.append(frame)
                self._frame_event.set()
            elif msg.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(msg.data)
                if payload.get("t") == "welcome":
                    self._authed.set()
                    continue
                self.texts.append(payload)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                if not self._authed.is_set():
                    self._auth_error = PermissionError(
                        f"device auth rejected (close={getattr(msg, 'data', None)})")
                break

    async def wait_frame(self, timeout: float = 5.0) -> protocol.ScreenFrame | None:
        start = len(self.frames)
        try:
            await asyncio.wait_for(self._wait_more(start), timeout)
        except asyncio.TimeoutError:
            return None
        return self.frames[-1]

    async def _wait_more(self, start: int) -> None:
        while len(self.frames) <= start:
            self._frame_event.clear()
            await self._frame_event.wait()

    async def send(self, obj: dict) -> None:
        assert self.ws is not None
        await self.ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def key(self, k: str) -> None:
        await self.send(protocol.key_frame(k))

    async def upload_voice(self, pcm: bytes, rate: int = 16000) -> dict:
        assert self._session is not None
        async with self._session.post(f"http://{self.host}:{self.port}/stt?token={self.token}&rate={rate}",
                                      data=pcm) as resp:
            return await resp.json()

    async def stream_voice(self, pcm: bytes, rate: int = 16000,
                           chunk: int = 3200) -> None:
        """板子的真实做法：边录边发，每片 4 字节头 + PCM。"""
        assert self.ws is not None
        await self.send({"t": "voice.begin", "rate": rate})
        for i in range(0, len(pcm), chunk):
            await self.ws.send_bytes(protocol.voice_chunk_frame(pcm[i:i + chunk]))
        await self.send({"t": "voice.end", "ms": len(pcm) * 500 // max(rate, 1)})

    @property
    def last_frame(self) -> protocol.ScreenFrame | None:
        return self.frames[-1] if self.frames else None

    def image(self) -> Image.Image | None:
        frame = self.last_frame
        if frame is None:
            return None
        if frame.encoding == protocol.ENC_RLE4:
            return palette_to_image(protocol.decode_frame_palette(frame))
        return bitmap_to_image(protocol.decode_frame_bitmap(frame))

    def ascii(self) -> str:
        img = self.image()
        return renderer.ascii_art(img) if img is not None else "(no frame)"

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws is not None:
            await self.ws.close()
        if self._session is not None:
            await self._session.close()
