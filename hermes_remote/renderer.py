"""把 View 画成 240×135 的 4 位调色板图，再压成 S 帧。

板子上一个字体都没有：中文、断行、省略号、配色全部在 Mac 上完成。
改界面只要改这个文件，存盘即生效，不用重烧固件。

配色（索引见 protocol.PALETTE_RGB，固件里是同一张表）：
  * 顶栏：正常=白底黑字、提问=橙底黑字、危险=红底白字、运行=绿底黑字
  * 正文：普通=黑、标签=蓝、危险=红、成功=绿、提示=深灰
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import protocol
from .state import View, ViewLine

W, H = protocol.SCREEN_W, protocol.SCREEN_H
HEADER_H = 24
FOOTER_H = 22
LINE_H = 20        # 17px 字体配 20px 行距
# 紧凑页（状态页）：字小一号、行距收紧，正文从 4 行变 5 行
DENSE_HEADER_H = 20
DENSE_FOOTER_H = 19
DENSE_LINE_H = 18

_WHITE = protocol.PALETTE_RGB[protocol.WHITE]
_BLACK = protocol.PALETTE_RGB[protocol.BLACK]
_RED = protocol.PALETTE_RGB[protocol.RED]
_ORANGE = protocol.PALETTE_RGB[protocol.ORANGE]
_GREEN = protocol.PALETTE_RGB[protocol.GREEN]
_GREY = protocol.PALETTE_RGB[protocol.GREY]
_DARK_GREY = protocol.PALETTE_RGB[protocol.DARK_GREY]
_BLUE = protocol.PALETTE_RGB[protocol.BLUE]

#: 按平台顺序找一个能显示中文的字体；都找不到就退回 Pillow 内置字体（只有英文）
_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def find_font_path() -> str | None:
    env = os.environ.get("HERMES_REMOTE_FONT")
    if env and Path(env).exists():
        return env
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return path
    return None


class Fonts:
    """16px 以下在 240×135 上就是糊的；正文 17px + Hiragino W6（简体黑体粗一级）。

    PingFang.ttc 在这台机器上 Pillow 打不开（cannot open resource），
    以前静默退回 STHeiti Medium（Heiti TC，繁体字面）→ 小字号笔画发虚。
    """

    #: (路径, ttc 索引, 用途)：索引 2 = Hiragino Sans GB W6
    _PREFERRED = (
        ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2),
        ("/System/Library/Fonts/STHeiti Medium.ttc", 1),   # Heiti SC
        ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
    )

    def __init__(self) -> None:
        self.path, self.index = self._pick()
        self.header = self._load(15)
        self.body = self._load(17)
        self.small = self._load(13)
        # 紧凑页用
        self.header_small = self._load(13)
        self.compact = self._load(15)
        self.compact_small = self._load(13)
        #: 纯 ASCII 行（命令/路径）用等宽：字符对齐、边缘更硬，扫读快
        self.mono = self._load_mono(13)

    def _load_mono(self, size: int):
        for path in ("/System/Library/Fonts/Menlo.ttc",
                     "/System/Library/Fonts/Monaco.ttf"):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return self._load(size)

    def _pick(self) -> tuple[str | None, int]:
        env = os.environ.get("HERMES_REMOTE_FONT")
        if env and Path(env).exists():
            return env, 0
        for path, index in self._PREFERRED:
            try:
                ImageFont.truetype(path, 17, index=index)
                return path, index
            except OSError:
                continue
        return find_font_path(), 0

    def _load(self, size: int) -> ImageFont.FreeTypeFont:
        if self.path:
            try:
                return ImageFont.truetype(self.path, size, index=self.index)
            except OSError:
                pass
        try:
            return ImageFont.load_default(size=size)  # Pillow >= 10
        except TypeError:  # pragma: no cover - very old Pillow
            return ImageFont.load_default()


def _has_cjk(text: str) -> bool:
    return any(ord(ch) > 0x2E80 for ch in text)


def _text_width(font: ImageFont.FreeTypeFont, text: str) -> int:
    if not text:
        return 0
    bbox = font.getbbox(text)
    return int(bbox[2] - bbox[0])


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int,
              max_lines: int) -> list[str]:
    """按像素宽度断行：中文逐字断，英文尽量整词断；超出行数加省略号。"""
    text = " ".join((text or "").split())
    if not text:
        return []
    lines: list[str] = []
    cur = ""
    for ch in text:
        candidate = cur + ch
        if _text_width(font, candidate) <= max_width:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
        cur = "" if ch == " " else ch
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines:
        # 比较要口径一致：两边都按“去掉空格后的字符数”算。
        # 以前一边去掉空格、一边没去，长行被静默截断且不加省略号（"…upload" → "uploa"）。
        consumed = len("".join(lines).replace(" ", ""))
        remainder = len(text.replace(" ", "")) > consumed
        if remainder or _text_width(font, lines[-1]) > max_width:
            last = lines[-1]
            while last and _text_width(font, last + "…") > max_width:
                last = last[:-1]
            lines[-1] = last + "…"
    return lines


#: 正文样式 → (用哪个字号, 文字颜色, 左边颜色竖条, 是否画色块)
_STYLE = {
    "body": ("body", _BLACK, None, None),
    "label": ("body", _BLACK, None, "square"),  # 工具行：蓝色小方块 + 黑字（蓝字在白底上发虚）
    "danger": ("body", _RED, _RED, None),
    "ok": ("body", _GREEN, None, None),
    "dim": ("small", _DARK_GREY, None, None),
    "sub": ("small", _DARK_GREY, None, "tick"),  # 结果行：缩进 + 画出来的短横
    "opt": ("small", _BLACK, None, None),       # 选项行：黑字（绿字白底看不清）
}


def render(view: View, fonts: Fonts | None = None) -> Image.Image:
    fonts = fonts or Fonts()
    img = Image.new("RGB", (W, H), _WHITE)
    draw = ImageDraw.Draw(img)

    # 紧凑页（状态页）：字小一号、行距收紧，正文多一行
    dense = bool(getattr(view, "dense", False))
    header_h = DENSE_HEADER_H if dense else HEADER_H
    footer_h = DENSE_FOOTER_H if dense else FOOTER_H
    line_h = DENSE_LINE_H if dense else LINE_H
    max_lines = 5 if dense else 4
    f_header = fonts.header_small if dense else fonts.header
    f_body = fonts.compact if dense else fonts.body
    f_dim = fonts.compact_small if dense else fonts.small
    f_footer = fonts.compact_small if dense else fonts.small

    # 顶栏：颜色就是状态，不用读字也知道现在该看它
    bar = view.bar
    if bar == "danger":
        band, band_fg = _RED, _WHITE
    elif bar == "alert":
        band, band_fg = _ORANGE, _BLACK
    elif bar == "ok":
        band, band_fg = _GREEN, _BLACK
    else:
        band, band_fg = _WHITE, _BLACK
    draw.rectangle([0, 0, W - 1, header_h - 1], fill=band)
    if bar == "normal":
        draw.line([0, header_h - 1, W - 1, header_h - 1], fill=_DARK_GREY)

    title = view.title or "Hermes"
    tag = "危险" if bar == "danger" else ""
    title_room = W - 12 - (_text_width(f_header, tag) + 16 if tag else 0)
    if _text_width(f_header, title) > title_room:
        # 给省略号本身也留位置，否则 "…" 会顶到右边的标签上
        room = max(10, title_room - _text_width(f_header, "…"))
        title = title[:_fit_chars(title, f_header, room)] + "…"
    draw.text((6, 2 if dense else 3), title, font=f_header, fill=band_fg)
    if tag:
        draw.text((W - 8 - _text_width(f_header, tag), 2 if dense else 3), tag,
                  font=f_header, fill=band_fg)

    # 正文
    y = header_h + (2 if dense else 4)
    body_bottom = H - footer_h - (1 if dense else 2)
    for line in view.lines[:max_lines]:
        size_key, color, stripe, bullet = _STYLE.get(line.style, _STYLE["body"])
        font = f_body if size_key == "body" else f_dim
        # 纯 ASCII（命令、路径）走等宽字体；含中文的行走中文字体
        if not _has_cjk(line.text):
            font = fonts.mono
        step = line_h if (dense or size_key == "body") else LINE_H - 4
        # 层级靠缩进：工具行顶格（前面有色块），结果行缩进对齐到工具名后
        if bullet == "square":
            x_text = 16
        elif bullet == "tick":
            x_text = 28            # 短横占 15..21，留 6px 空隙
        else:
            x_text = 6
        x_min = x_text
        max_w = W - x_text - 6
        wrapped = wrap_text(line.text, font, max_w,
                            max_lines=(2 if line.style != "label" else 1) if line.wrap else 1)
        block_top = y
        for piece in wrapped:
            if y + step > body_bottom:
                break
            if bullet == "square":
                draw.rectangle([7, y + 6, 12, y + 11], fill=_BLUE)
            elif bullet == "tick":
                draw.line([15, y + 10, 21, y + 10], fill=_DARK_GREY, width=1)
            draw.text((x_text if not stripe else x_min + 4, y), piece, font=font, fill=color)
            y += step
        if stripe and y > block_top:
            draw.rectangle([2, block_top + 2, 5, y - 6], fill=stripe)
        y += 0 if dense else 2      # 紧凑页不加行间空隙：加了就少一行（5 行正好卡在边界）

    # 底部提示条：灰底黑字，和正文分开
    draw.rectangle([0, H - footer_h, W - 1, H - 1], fill=_GREY)
    draw.line([0, H - footer_h, W - 1, H - footer_h], fill=_DARK_GREY)
    footer = view.footer or ""
    if _text_width(f_footer, footer) > W - 12:
        cut = _fit_chars(footer, f_footer, W - 12)
        footer = footer[:cut] + "…"
    draw.text((6, H - footer_h + (2 if dense else 3)), footer, font=f_footer, fill=_BLACK)
    return img


def _fit_chars(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> int:
    for i in range(len(text), 0, -1):
        if _text_width(font, text[:i]) <= max_width:
            return i
    return 0


_nearest_cache: dict[tuple[int, int, int], int] = {}

#: 只在这些索引里挑最近色（跳过 5/6 两个灰）：抗锯齿的过渡像素直接落到纯色上，
#: 否则灰边会把笔画糊成一团——小屏上最明显的“看不清”就是这个。
_INK_INDICES = (protocol.BLACK, protocol.WHITE, protocol.RED, protocol.ORANGE,
                protocol.GREEN, protocol.BLUE)


def _nearest_index(rgb: tuple[int, int, int]) -> int:
    hit = protocol.PALETTE_INDEX.get(rgb)
    if hit is not None:
        return hit          # 纯色（包括灰带/灰字）原样保留
    cached = _nearest_cache.get(rgb)
    if cached is not None:
        return cached
    r, g, b = rgb
    # 中性灰（黑白字的抗锯齿边）走 4 级灰阶：纯黑/深灰/中灰/白。
    # 以前一律硬切到纯色，小字号笔画会被切出豁口——这就是"边缘不清晰"的主因。
    if max(rgb) - min(rgb) < 24:
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        best = (protocol.WHITE if lum > 200 else
                protocol.GREY if lum > 120 else
                protocol.DARK_GREY if lum > 48 else protocol.BLACK)
    else:
        best, best_d = protocol.BLACK, None
        for i in _INK_INDICES:
            pal = protocol.PALETTE_RGB[i]
            d = sum((a - b) ** 2 for a, b in zip(rgb, pal))
            if best_d is None or d < best_d:
                best, best_d = i, d
    _nearest_cache[rgb] = best
    return best


def palette_bytes(img: Image.Image) -> bytes:
    """量化成调色板索引并按 4 位打包：每字节两个像素（高四位=左）。"""
    if img.mode != "RGB":
        img = img.convert("RGB")
    data = img.tobytes()
    out = bytearray(protocol.RAW_LEN4)
    for p in range(W * H):
        i = p * 3
        idx = _nearest_index((data[i], data[i + 1], data[i + 2]))
        if p & 1:
            out[p >> 1] |= idx
        else:
            out[p >> 1] |= idx << 4
    return bytes(out)


def frame_for(view: View, seq: int, fonts: Fonts | None = None, *,
              wake: bool = True) -> protocol.ScreenFrame:
    flags = protocol.FLAG_FULL_REFRESH | (protocol.FLAG_WAKE if wake else 0)
    return protocol.build_frame4(palette_bytes(render(view, fonts)), seq, flags=flags)


def ascii_art(img: Image.Image | None = None, view: View | None = None,
              fonts: Fonts | None = None, *, invert: bool = False) -> str:
    """把屏帧打成终端里的字符画，用于 demo / 测试 / 没有板子时看效果。"""
    if img is None:
        if view is None:
            raise ValueError("need img or view")
        img = render(view, fonts)
    width = 120
    small = img.convert("L").resize((width, int(width * H / W)), Image.LANCZOS)
    px = small.load()
    chars = " .:-=+*#%@"
    rows = []
    for y in range(small.height):
        row = []
        for x in range(width):
            v = px[x, y]
            if invert:
                v = 255 - v
            row.append(chars[(255 - v) * (len(chars) - 1) // 255])
        rows.append("".join(row))
    return "\n".join(rows)
