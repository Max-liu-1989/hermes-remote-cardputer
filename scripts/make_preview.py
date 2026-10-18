#!/usr/bin/env python3
"""把四种屏拼成一张对比图 + 生成一份验证记录，便于交给不跑代码的人看。

跑：python scripts/make_preview.py
产物：screens/preview.png、logs/verification.txt
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from hermes_remote import renderer  # noqa: E402
from hermes_remote.cli import _sample_views  # noqa: E402

SCALE = 2
PAD = 12
CAPTION_H = 18


def build_preview() -> Path:
    fonts = renderer.Fonts()
    views = _sample_views()
    tile_w = 240 * SCALE
    tile_h = 135 * SCALE + CAPTION_H
    cols = 2
    rows = (len(views) + cols - 1) // cols
    sheet = Image.new("L", (cols * (tile_w + PAD) + PAD, rows * (tile_h + PAD) + PAD), 235)
    draw = ImageDraw.Draw(sheet)
    caption_font = renderer.Fonts()._load(13)
    captions = {"status": "① 状态：在看它干活", "clarify": "② 问题：按 1 选，或按住侧键说话",
                "approval": "③ 审批：会话+工具+命令原文，危险按两次",
                "voice": "④ 语音预览：enter 才发"}
    for i, (name, view) in enumerate(views):
        img = renderer.render(view, fonts).convert("L").resize(
            (240 * SCALE, 135 * SCALE), Image.NEAREST)
        col, row = i % cols, i // cols
        x = PAD + col * (tile_w + PAD)
        y = PAD + row * (tile_h + PAD)
        sheet.paste(img, (x, y))
        draw.rectangle([x, y, x + tile_w, y + 135 * SCALE], outline=120)
        draw.text((x + 2, y + 135 * SCALE + 2), captions.get(name, name), font=caption_font, fill=20)
    out = ROOT / "screens" / "preview.png"
    out.parent.mkdir(exist_ok=True)
    sheet.save(out)
    return out


def main() -> int:
    preview = build_preview()
    lines = []
    lines.append(f"验证记录（生成于 {time.strftime('%Y-%m-%d %H:%M %Z')}）")
    lines.append("")
    for label, cmd in (
        ("单元 + 端到端测试", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]),
        ("源码演示（假后端 + 模拟板子）", [sys.executable, "-m", "hermes_remote.cli", "demo", "--workdir", "."]),
    ):
        lines.append(f"── {label} ──")
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        tail = [ln for ln in (proc.stdout + proc.stderr).splitlines()
                if ln.startswith(("Ran ", "OK", "FAILED", "■", "→", "共推送", "全部通过"))]
        lines.extend(tail[-14:])
        lines.append(f"[exit={proc.returncode}]")
        lines.append("")
    lines.append(f"预览图：{preview.relative_to(ROOT)}")
    out = ROOT / "logs" / "verification.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
