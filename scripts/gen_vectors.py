#!/usr/bin/env python3
"""生成跨语言测试向量：把 Python 编码器算出的 RLE 表烧进 C++ 测试。

两边（hermes_remote/protocol.py 与 firmware/include/frame_codec.h）都按同一条
公式构造位图，C++ 侧把自己的编码结果与这里生成的字节逐字节比较，
再用两侧的解码器各自还原。这样“板子和桥对帧的理解不一致”会立刻暴露。

跑：python scripts/gen_vectors.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_remote import protocol


def pattern_bitmap() -> bytes:
    out = bytearray()
    for i in range(protocol.RAW_LEN):
        row = i // 30
        if row % 7 == 0:
            out.append(0xFF)
        elif (i % 30) < 10:
            out.append(0xAA)
        else:
            out.append(0x00)
    return bytes(out)


def main() -> int:
    bitmap = pattern_bitmap()
    assert len(bitmap) == protocol.RAW_LEN
    frame = protocol.build_frame(bitmap, seq=0x1234)
    wire = frame.encode()
    payload = frame.payload
    target = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "firmware", "test", "test_frame_codec", "vectors.h")
    lines = [
        "// 由 scripts/gen_vectors.py 生成，请勿手改。",
        "// 位图公式：row=i/30; row%7==0 → 0xFF; 否则 (i%30)<10 → 0xAA; 否则 0x00",
        f"static const uint8_t kVectorSeqHi = 0x{wire[1]:02X};",
        f"static const uint8_t kVectorSeqLo = 0x{wire[2]:02X};",
        f"static const uint16_t kVectorPayloadLen = {len(payload)};",
        "static const uint8_t kVectorPayload[] = {",
    ]
    for i in range(0, len(payload), 16):
        chunk = ", ".join(f"0x{b:02X}" for b in payload[i:i + 16])
        lines.append(f"  {chunk},")
    lines.append("};")
    lines.append(f"static const uint16_t kVectorFrameLen = {len(wire)};")
    lines.append("static const uint8_t kVectorHeader[] = {")
    lines.append("  " + ", ".join(f"0x{b:02X}" for b in wire[:9]) + ",")
    lines.append("};")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {target}: payload={len(payload)} frame={len(wire)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
