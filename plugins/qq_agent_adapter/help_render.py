"""帮助菜单图片渲染：用 Pillow 把指令菜单画成 WebUI 黑白点阵风格图发到 QQ。

- 依赖 Pillow 与系统中文字体（按候选列表探测）；任一不可用即返回 None，
  调用方（admin.handle_help）自动退回纯文本帮助。
- `AGENT_HELP_IMAGE=0` 可整体关闭图片形态。
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from .help_menu import FOOTER as _FOOTER
from .help_menu import SECTIONS as _SECTIONS

_TRUE = {"1", "true", "yes", "on"}

_FONT_CANDIDATES = (
    # 项目本地字体（优先，基于源码文件位置定位，不依赖 CWD）
    str(Path(__file__).resolve().parents[2] / "data" / "fonts" / "wqy-zenhei.ttc"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

# 配色/纹理统一走 WebUI（/agent-web）黑白点阵 token（agentcore/render/style.py，
# 在 _render 内惰性导入——本模块对 PIL 是可选依赖，顶层 import 会破坏降级路径）。
# 旧实现是浅底 + 蓝色圆角头卡；2026-09 随 web 总览 612afa3 统一改黑白点阵。
#
# 菜单数据（_SECTIONS / _FOOTER）来自 help_menu 单一事实源，与 admin 的纯文本
# 帮助同源——历史上两份菜单各写一套，图片菜单漏过 /词云、/memory 等一整批
# 命令（2026-10-26）。

_WIDTH = 880
_PAD = 36


def image_enabled() -> bool:
    # L7（REVIEW-c472e56..733f57e）：显式设了值（含空串）就按值生效——
    # 此前 `or "1"` 会把 `AGENT_HELP_IMAGE=`（空值）变成开启。
    raw = os.getenv("AGENT_HELP_IMAGE")
    if raw is None:
        return True
    return raw.strip().lower() in _TRUE


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                from PIL import ImageFont

                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return None


def render_help_image() -> bytes | None:
    """渲染帮助菜单卡片，返回 PNG 字节。

    未开启、缺 Pillow、缺中文字体时返回 None，调用方退回纯文本；渲染主体
    异常也在本函数内吞掉并返回 None，绝不影响命令回复（L5，
    REVIEW-c472e56..733f57e：兜底自 admin.py 收拢到本函数，任何新调用点
    不必再自带 try/except）。
    """
    if not image_enabled():
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    try:
        return _render(Image, ImageDraw)
    except Exception:
        # 字体加载成功但绘制/编码阶段仍可能失败（如极端内存、Pillow 版本差异）
        return None


def _render(Image, ImageDraw) -> bytes | None:
    from agentcore.render.style import INK, INK_SOFT, LINE_SOFT, MUTED, PAPER, hatch

    title_f = _load_font(38)
    if title_f is None:  # 无中文字体
        return None
    head_f = _load_font(30)
    body_f = _load_font(25)
    small_f = _load_font(20)

    pad = _PAD
    brand_h = 24
    title_h = 48
    hatch_h = 8
    head_h = 44
    row_h = 44
    sec_gap = 26

    height = pad + brand_h + 10 + title_h + 12 + hatch_h + 22
    for _, rows in _SECTIONS:
        height += head_h + row_h * len(rows) + sec_gap
    height += 14 + 28 + pad

    img = Image.new("RGB", (_WIDTH, height), PAPER)
    draw = ImageDraw.Draw(img)

    # 页眉品牌行（web .hdr__brand 的 ◆ + 等宽小字）。◆ 用多边形画而不是字体
    # 字形：WQY 不保证收录 U+25C6，别赌豆腐块。
    brand_top = pad
    d = 5
    cx, cy = pad + d, brand_top + 12
    draw.polygon([(cx, cy - d), (cx + d, cy), (cx, cy + d), (cx - d, cy)], fill=INK)
    draw.text(
        (pad + 2 * d + 8, brand_top + 2), "AGENT-DEMO · HELP", font=small_f, fill=MUTED
    )
    draw.text(
        (_WIDTH - pad, brand_top + 2),
        "QQ 机器人",
        font=small_f,
        fill=MUTED,
        anchor="ra",
    )

    # 大标题 + 斜纹分隔条（web 的 .gate__title + .hatch）
    title_y = brand_top + brand_h + 10
    draw.text((pad, title_y), "使用帮助", font=title_f, fill=INK)
    hatch_y = title_y + title_h + 12
    hatch(img, (pad, hatch_y, _WIDTH - pad, hatch_y + hatch_h), step=5)

    y = hatch_y + hatch_h + 22
    for head, rows in _SECTIONS:
        # 节头：墨色标题 + 1px 墨线下划（web .sec-head 的 border-bottom）
        draw.text((pad, y), head, font=head_f, fill=INK)
        draw.line(
            (pad, y + head_h - 8, _WIDTH - pad, y + head_h - 8), fill=INK, width=1
        )
        y += head_h
        desc_x = (
            pad
            + 8
            + max(int(draw.textlength(cmd, font=body_f)) for cmd, _ in rows)
            + 32
        )
        for i, (cmd, desc) in enumerate(rows):
            draw.text((pad + 8, y), cmd, font=body_f, fill=INK)
            draw.text((desc_x, y), desc, font=body_f, fill=INK_SOFT)
            if i < len(rows) - 1:
                # 行间细线（web .row 的 border-bottom，末行不留）
                draw.line(
                    (pad, y + row_h - 6, _WIDTH - pad, y + row_h - 6),
                    fill=LINE_SOFT,
                    width=1,
                )
            y += row_h
        y += sec_gap

    draw.text((pad, height - pad - 28), _FOOTER, font=small_f, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
