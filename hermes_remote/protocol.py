"""屏幕帧与设备协议的编解码。

桥（Mac，Python）与板子（ESP32-S3，C++）之间只有两样东西：
  * S 帧：二进制屏帧，9 字节头 + RLE 位图，桥→板。
  * K 帧：很小的 JSON 文本帧，板→桥（按键、心跳、语音上传通知）。

固件里有一份逐字节等价的实现：``firmware/include/frame_codec.h``，
两侧的编解码用例是同一条向量（见 tests/test_protocol.py 与
firmware/test/test_frame_codec/test_main.cpp）。

帧头 9 字节（大端）::

    0      type        0x01 = 屏幕帧
    1..2   seq         uint16，回绕；板子用它丢弃迟到的旧帧
    3      width       240
    4      height      135
    5      encoding    0 = RLE1（默认），1 = RAW（整帧裸位图，调试用）
    6..7   payload_len uint16
    8      flags       bit0 = 全屏刷新，bit1..7 保留，必须为 0
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SCREEN_W = 240
SCREEN_H = 135

TYPE_SCREEN = 0x01

ENC_RLE1 = 0
ENC_RAW = 1
ENC_RLE4 = 2   # 4 位调色板：每字节两个像素

FLAG_FULL_REFRESH = 0x01
#: 让板子重置"闲置变暗"计时。周期性刷新（信号/电量/时长变化）不带这个位，
#: 否则每分钟都在"唤醒"，背光永远暗不下来。
FLAG_WAKE = 0x02

HEADER = struct.Struct(">BHBBBHB")
HEADER_LEN = HEADER.size  # 9
assert HEADER_LEN == 9

# 一帧裸位图：240*135/8 = 4050 字节
RAW_LEN = (SCREEN_W * SCREEN_H) // 8
# 4 位调色板裸帧：240*135/2 = 16200 字节（每字节两个像素）
RAW_LEN4 = (SCREEN_W * SCREEN_H) // 2

#: 调色板必须与 firmware/include/frame_codec.h 的 PALETTE565 一一对应。
#: 0 黑 1 白 2 红 3 橙 4 绿 5 中灰 6 深灰 7 蓝；8..15 预留（填黑）。
PALETTE_RGB = (
    (0, 0, 0),        # 0
    (255, 255, 255),  # 1
    (255, 0, 0),      # 2
    (255, 165, 0),    # 3
    (0, 255, 0),      # 4
    (128, 128, 128),  # 5
    (64, 64, 64),     # 6
    (0, 120, 255),    # 7
) + ((0, 0, 0),) * 8
PALETTE_INDEX = {rgb: i for i, rgb in enumerate(PALETTE_RGB[:8])}

BLACK, WHITE, RED, ORANGE, GREEN, GREY, DARK_GREY, BLUE = range(8)


class FrameError(ValueError):
    """帧不合法（长度、类型、宽度、RLE 越界）。"""


@dataclass(frozen=True)
class ScreenFrame:
    seq: int
    width: int
    height: int
    encoding: int
    flags: int
    payload: bytes

    @property
    def is_full_refresh(self) -> bool:
        return bool(self.flags & FLAG_FULL_REFRESH)

    def encode(self) -> bytes:
        if len(self.payload) > 0xFFFF:
            raise FrameError(f"payload too large: {len(self.payload)}")
        head = HEADER.pack(TYPE_SCREEN, self.seq & 0xFFFF, self.width, self.height,
                           self.encoding, len(self.payload), self.flags & 0xFF)
        return head + self.payload

    @classmethod
    def decode(cls, data: bytes) -> "ScreenFrame":
        if len(data) < HEADER_LEN:
            raise FrameError(f"short frame: {len(data)} < {HEADER_LEN}")
        ftype, seq, width, height, enc, plen, flags = HEADER.unpack_from(data, 0)
        if ftype != TYPE_SCREEN:
            raise FrameError(f"unknown frame type 0x{ftype:02x}")
        if len(data) < HEADER_LEN + plen:
            raise FrameError(f"truncated payload: {len(data) - HEADER_LEN} < {plen}")
        if enc not in (ENC_RLE1, ENC_RAW, ENC_RLE4):
            raise FrameError(f"unknown encoding {enc}")
        return cls(seq=seq, width=width, height=height, encoding=enc, flags=flags,
                   payload=bytes(data[HEADER_LEN:HEADER_LEN + plen]))


def rle_encode(bitmap: bytes) -> bytes:
    """把 MSB-first 位图压成 (count, value) 对。run 跨行连续。

    最坏情况 2 字节/字节（0101 交替）→ 8100 字节，仍在 uint16 内。
    """
    if not bitmap:
        return b""
    out = bytearray()
    cur = bitmap[0]
    run = 1
    for byte in bitmap[1:]:
        if byte == cur and run < 255:
            run += 1
            continue
        out += bytes((run, cur))
        cur, run = byte, 1
    out += bytes((run, cur))
    return bytes(out)


def rle_decode(payload: bytes, out_len: int) -> bytes:
    """还原 RLE。严格校验：不够长 / 超出期望长度都报错，避免板子画花屏。"""
    if len(payload) % 2:
        raise FrameError("odd RLE payload length")
    out = bytearray()
    for i in range(0, len(payload), 2):
        count = payload[i]
        value = payload[i + 1]
        if count == 0:
            raise FrameError("zero-length run")
        if len(out) + count > out_len:
            raise FrameError("RLE overruns bitmap length")
        out += bytes((value,)) * count
    if len(out) != out_len:
        raise FrameError(f"RLE underrun: {len(out)} != {out_len}")
    return bytes(out)


def pack_bitmap(rows: list[bytes]) -> bytes:
    """把 PIL 的 1-bit 图（每行 bytes-per-row）拼成连续 MSB-first 位图。"""
    row_bytes = (SCREEN_W + 7) // 8
    out = bytearray()
    for row in rows:
        if len(row) < row_bytes:
            raise FrameError("short bitmap row")
        out += row[:row_bytes]
    return bytes(out)


def build_frame(bitmap: bytes, seq: int, *, encoding: int = ENC_RLE1,
                flags: int = FLAG_FULL_REFRESH) -> ScreenFrame:
    if len(bitmap) != RAW_LEN:
        raise FrameError(f"bitmap must be {RAW_LEN} bytes, got {len(bitmap)}")
    payload = rle_encode(bitmap) if encoding == ENC_RLE1 else bitmap
    return ScreenFrame(seq=seq & 0xFFFF, width=SCREEN_W, height=SCREEN_H,
                       encoding=encoding, flags=flags, payload=payload)


def decode_frame_bitmap(frame: ScreenFrame) -> bytes:
    expected = (frame.width * frame.height) // 8
    if frame.encoding == ENC_RAW:
        if len(frame.payload) != expected:
            raise FrameError("raw payload length mismatch")
        return frame.payload
    return rle_decode(frame.payload, expected)


def build_frame4(packed: bytes, seq: int, *, flags: int = FLAG_FULL_REFRESH) -> ScreenFrame:
    """4 位调色板帧：``packed`` 是 W*H/2 字节，每字节两个调色板索引（高四位=左）。"""
    if len(packed) != RAW_LEN4:
        raise FrameError(f"packed palette must be {RAW_LEN4} bytes, got {len(packed)}")
    return ScreenFrame(seq=seq & 0xFFFF, width=SCREEN_W, height=SCREEN_H,
                       encoding=ENC_RLE4, flags=flags, payload=rle_encode(packed))


def decode_frame_palette(frame: ScreenFrame) -> bytes:
    """解回 W*H/2 字节的调色板索引（测试与 demo 用）。"""
    if frame.encoding != ENC_RLE4:
        raise FrameError("frame is not palette encoded")
    return rle_decode(frame.payload, RAW_LEN4)


def palette_indices(packed: bytes) -> list[int]:
    """把打包的字节展开成逐像素索引（自左向右、自上而下）。"""
    out: list[int] = []
    for byte in packed:
        out.append(byte >> 4)
        out.append(byte & 0x0F)
    return out


# ── K 帧（板 → 桥） ──────────────────────────────────────────────────────────

# ── 板 → 桥的语音分片（二进制） ───────────────────────────────────────────────
#
# 板子没有 PSRAM，5 秒 16k/16bit 是 160KB，一次性收完再 POST 很容易 OOM。
# 所以录音边录边发：每 200ms 一片，4 字节头（type 0x02 + uint16 长度）＋ PCM。
# 桥在内存里攒起来再转写，板子上永远只有一小片。

TYPE_VOICE = 0x02
VOICE_HEADER = struct.Struct(">BH")
VOICE_HEADER_LEN = VOICE_HEADER.size  # 3 字节

#: 桥侧一条语音最多攒这么多字节（16k/16bit 约 30 秒）
MAX_VOICE_BYTES = 3_840_000    # 120s @16k/16bit（原来 30s 太短）


def voice_chunk_frame(pcm: bytes) -> bytes:
    if len(pcm) > 0xFFFF:
        raise FrameError("voice chunk too large")
    return VOICE_HEADER.pack(TYPE_VOICE, len(pcm)) + pcm


def parse_binary_frame(data: bytes) -> tuple[int, bytes]:
    """返回 (type, payload)；只有语音分片走二进制。"""
    if len(data) < VOICE_HEADER_LEN:
        raise FrameError("short binary frame")
    ftype, plen = VOICE_HEADER.unpack_from(data, 0)
    if ftype != TYPE_VOICE:
        raise FrameError(f"unknown binary frame type 0x{ftype:02x}")
    if len(data) < VOICE_HEADER_LEN + plen:
        raise FrameError("truncated voice chunk")
    return ftype, data[VOICE_HEADER_LEN:VOICE_HEADER_LEN + plen]


KEY_NAMES = frozenset({
    "1", "2", "3", "4", "5",
    "enter", "esc", "space", "back",
    "left", "right", "up", "down",
    "fn1", "fn2", "fn3",
    "mic", "mic.stop", "confirm", "cancel",
    "power",
})


def key_frame(key: str) -> dict:
    if key not in KEY_NAMES:
        raise ValueError(f"unknown key {key!r}")
    return {"t": "key", "k": key}


def heartbeat_frame(*, rssi: int, battery: int, uptime_s: int, fw: str) -> dict:
    return {"t": "hb", "rssi": int(rssi), "batt": int(battery),
            "up": int(uptime_s), "fw": fw}


def parse_device_frame(raw: str | bytes) -> dict:
    """解析板子发来的 K 帧；不认识就抛 ValueError（桥只记日志，不断连）。"""
    import json

    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("device frame is not utf-8") from exc
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("device frame is not json") from exc
    if not isinstance(obj, dict) or "t" not in obj:
        raise ValueError("device frame missing type")
    kind = obj["t"]
    if kind == "key":
        key = obj.get("k")
        if key not in KEY_NAMES:
            raise ValueError(f"unknown key {key!r}")
        return obj
    if kind == "hb":
        return obj
    if kind in ("voice.upload", "voice.confirm", "voice.cancel", "hello",
                "voice.begin", "voice.end", "auth"):
        return obj
    if kind == "log":
        return obj
    if kind == "char":
        c = obj.get("c")
        if not isinstance(c, str) or len(c) != 1 or not (32 <= ord(c) <= 126):
            raise ValueError(f"bad char {c!r}")
        return obj
    raise ValueError(f"unknown device frame type {kind!r}")
