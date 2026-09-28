"""WebUI（/agent-web）黑白点阵风格的共享绘制语言。

所有 Pillow 渲染图（表格 / 帮助菜单 / 本机概览）共用这一套 token，与
``plugins/qq_agent_adapter/web.py`` 内嵌 CSS 的 ``:root`` 一一对应：

    纸色底 + 1px 墨线 + 直角面板 + 45° 斜纹 + 黑白状态点（警示红仅用于错误）

历史：渲染图原本各有各的主题（表格深蓝表头+斑马纹、概览跟随 dashboard 的暗紫
仪表盘、帮助菜单蓝色圆角卡）；2026-09 按 612afa3 的 web 总览新风格统一改黑
白点阵。改 web 样式时两边要一起看。

Pillow 没有 alpha 混合与图案填充：半透明用"已知底色预混实色"（见 overview 的
``_blend``），斜纹/斜纹点用 L 蒙版贴实色（:func:`hatch`）。
"""

from __future__ import annotations

from PIL import Image, ImageDraw

RGB = tuple[int, int, int]

# web.py :root —— 改 web 配色时同步这里
PAPER = (0xF5, 0xF4, 0xF1)  # --paper 底色
PAPER_2 = (0xEF, 0xEE, 0xEA)  # --paper-2 面板头/进度条轨道
INK = (0x11, 0x11, 0x11)  # --ink 主文字/线条
INK_SOFT = (0x3A, 0x3A, 0x3A)  # --ink-soft 次级文字
MUTED = (0x8A, 0x87, 0x82)  # --muted 弱文字
LINE_SOFT = (0xCB, 0xC9, 0xC3)  # --line-soft 行分隔细线
ALERT = (0xB2, 0x3C, 0x2A)  # --alert 仅错误；状态一律黑白表达

# 曲线/数据系列色：webui 无彩色，主线墨色、副线弱灰（如网速的下载/上传）
SERIES = INK
SERIES_ALT = MUTED


def hatch(
    img: Image.Image,
    box: tuple[int, int, int, int],
    color: RGB = INK,
    step: int = 5,
    width: int = 1,
) -> None:
    """45° 斜纹填充（web 的 ``.hatch`` / ``.chip--hatch`` / ``.dot--off`` 纹理）。

    box 为整图像素坐标；内部转 L 蒙版画斜线后把实色贴过去，不污染框外像素。
    """
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    mask = Image.new("L", (w, h), 0)
    mdraw = ImageDraw.Draw(mask)
    start = -h
    while start < w:
        mdraw.line([(start, h), (start + h, 0)], fill=255, width=width)
        start += step
    solid = Image.new("RGB", (w, h), color)
    img.paste(solid, (x0, y0), mask)


def status_dot(
    draw: ImageDraw.ImageDraw,
    img: Image.Image,
    cx: float,
    cy: float,
    r: float,
    *,
    on: bool = True,
) -> None:
    """状态点（web 的 ``.dot``）：on=实心墨点，off=斜纹空心——不用红绿表达状态。"""
    box = (cx - r, cy - r, cx + r, cy + r)
    if on:
        draw.ellipse(box, fill=INK)
        return
    draw.ellipse(box, outline=INK, width=1)
    hatch(img, (cx - r + 1, cy - r + 1, cx + r - 1, cy + r - 1), step=3)


def pill(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font,  # ImageFont.FreeTypeFont
    *,
    filled: bool = False,
) -> None:
    """胶囊标签（web 的 ``.chip``）：1px 墨描边 999px 圆角；filled=黑底纸字。

    文字水平垂直居中（anchor="mm"）。调用方保证 box 高度 ≥ 字高，否则截断。
    """
    x0, y0, x1, y1 = box
    radius = (y1 - y0) // 2
    if filled:
        draw.rounded_rectangle(box, radius=radius, fill=INK)
        fg = PAPER
    else:
        draw.rounded_rectangle(box, radius=radius, outline=INK, width=1)
        fg = INK
    draw.text(((x0 + x1) / 2, (y0 + y1) / 2), text, font=font, fill=fg, anchor="mm")
