"""发言榜卡片渲染：数据进、PNG 出（WebUI 黑白点阵风格）。

与 ``overview.py``（戳一戳概览）、``table.py``（markdown 表格）同属「本机渲染」
路线：不依赖浏览器，Pillow 直接画。风格 token 全部来自 ``style.py``，与
``/agent-web`` 内嵌 CSS 的 ``:root`` 一一对应——改 web 样式时两边一起看。

版式对齐产品截图：
- 标题「周榜 | 10/5 ~ 10/11」+ 45° 斜纹分隔（web 的 .hatch）
- 每行：名次徽章（前三黑底纸字，其余墨描边）+ 昵称 + 「N 条 (P%)」
  + NEW 徽章 + 名次变化箭头（▲n 升 / ▼n 降 / — 平；上周未上榜显示 NEW）
- 尾行：合计条数 + 最热时段

无中文字体 / 绘制失败返回 None——调用方（stats_route）必须降级为文本，
与 render_overview_png 的约定一致。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from PIL import Image, ImageDraw

from agentcore.render.fonts import ensure_font_probed
from agentcore.render.fonts import font as _font
from agentcore.render.style import (
    INK,
    INK_SOFT,
    LINE_SOFT,
    MUTED,
    PAPER,
    PAPER_2,
    hatch,
    pill,
)

logger = logging.getLogger(__name__)

W = 720
PAD = 32
GAP = 14

TITLE_SIZE = 30
ROW_NAME_SIZE = 21
ROW_META_SIZE = 18
FOOT_SIZE = 17
BADGE_SIZE = 16

TITLE_LINE_H = 52
ROW_H = 62
FOOT_H = 56

# 箭头与字形：wqy-zenhei/Noto 均覆盖 U+25B2/25BC（启动期探测缺字形时调用方
# 已整体降级文本，这里不再逐字形判断）
UP, DOWN, FLAT = "▲", "▼", "—"


@dataclass(frozen=True)
class RankRow:
    """榜单一行（ranking.build_leaderboard 的 Entry 补齐昵称后转来）。"""

    rank: int
    nickname: str
    msgs: int
    pct: float
    is_new: bool = False
    delta: int | None = None  # 名次变化：正=升；None=上周未上榜


def _fit(draw: ImageDraw.ImageDraw, text: str, f, max_w: int) -> str:
    """把 text 截到 max_w 像素内（二分），末尾省略号。"""
    if max_w <= 0 or draw.textlength(text, font=f) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid] + "…", font=f) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def _badge(
    draw: ImageDraw.ImageDraw,
    img: Image.Image,
    x: int,
    y: int,
    text: str,
    *,
    filled: bool,
) -> int:
    """名次/NEW 徽章：返回徽章右边界 x。filled=黑底纸字，否则墨描边。"""
    f = _font(BADGE_SIZE, bold=True)
    tw = draw.textlength(text, font=f)
    w = int(tw) + 18
    h = 26
    box = (x, y, x + w, y + h)
    if filled:
        draw.rounded_rectangle(box, radius=6, fill=INK)
        fg = PAPER
    else:
        draw.rounded_rectangle(box, radius=6, outline=INK, width=1)
        fg = INK
    draw.text((x + w / 2, y + h / 2), text, font=f, fill=fg, anchor="mm")
    return x + w


def render_rank_png(
    title: str,
    rows: list[RankRow],
    *,
    total: int = 0,
    peak_text: str = "",
) -> bytes | None:
    """渲染发言榜卡片；无字体/异常返回 None。rows 为空时调用方应走文案分支。"""
    if not ensure_font_probed():
        return None
    if not rows:
        return None
    try:
        return _render(title, rows, total, peak_text)
    except Exception:
        logger.warning("rank render failed", exc_info=True)
        return None


def _render(title: str, rows: list[RankRow], total: int, peak_text: str) -> bytes:
    height = PAD + TITLE_LINE_H + GAP + len(rows) * ROW_H + GAP + FOOT_H + PAD
    img = Image.new("RGB", (W, height), PAPER)
    draw = ImageDraw.Draw(img)

    # 标题 + 斜纹分隔（overview 同款：塞在 GAP 里，不另占高度）
    f_title = _font(TITLE_SIZE, bold=True)
    draw.text((PAD, PAD), title, font=f_title, fill=INK, anchor="la")
    sep_y = PAD + TITLE_LINE_H + (GAP - 6) // 2
    hatch(img, (PAD, sep_y, W - PAD, sep_y + 6), step=5)

    # 行区（交替纸色面板，web 的 zebra 语义）
    f_name = _font(ROW_NAME_SIZE, bold=True)
    f_meta = _font(ROW_META_SIZE)
    y = PAD + TITLE_LINE_H + GAP
    for i, row in enumerate(rows):
        if i % 2 == 1:
            draw.rectangle((PAD, y, W - PAD, y + ROW_H), fill=PAPER_2)
        draw.line([(PAD, y), (W - PAD, y)], fill=LINE_SOFT)
        cy = y + ROW_H // 2

        badge_x = _badge(
            draw, img, PAD + 12, cy - 13, str(row.rank), filled=row.rank <= 3
        )
        # 名次变化箭头：放在徽章右侧，等宽字体感的小字
        arrow_x = badge_x + 8
        if row.delta is None:
            pass  # NEW 徽章在右侧统一表达
        elif row.delta > 0:
            draw.text(
                (arrow_x, cy), f"{UP}{row.delta}", font=f_meta, fill=INK, anchor="lm"
            )
        elif row.delta < 0:
            draw.text(
                (arrow_x, cy),
                f"{DOWN}{-row.delta}",
                font=f_meta,
                fill=INK_SOFT,
                anchor="lm",
            )
        else:
            draw.text((arrow_x, cy), FLAT, font=f_meta, fill=MUTED, anchor="lm")

        # 昵称（箭头与 NEW 徽章之间截断）
        name_x = arrow_x + 34
        right_w = 190 if row.is_new else 150
        max_name_w = (W - PAD - 12 - right_w) - name_x
        name = _fit(draw, row.nickname or str(row.rank), f_name, max_name_w)
        draw.text((name_x, cy), name, font=f_name, fill=INK, anchor="lm")

        # 右侧：条数百分比 + NEW 徽章
        meta = f"{row.msgs} 条 ({row.pct:.0f}%)"
        meta_w = draw.textlength(meta, font=f_meta)
        if row.is_new:
            new_w = draw.textlength("NEW", font=_font(BADGE_SIZE, bold=True)) + 18
            draw.text(
                (W - PAD - 12 - new_w - 10 - meta_w, cy),
                meta,
                font=f_meta,
                fill=INK_SOFT,
                anchor="lm",
            )
            pill(
                draw,
                (
                    int(W - PAD - 12 - new_w),
                    int(cy - 13),
                    int(W - PAD - 12),
                    int(cy + 13),
                ),
                "NEW",
                _font(BADGE_SIZE, bold=True),
                filled=True,
            )
        else:
            draw.text(
                (W - PAD - 12 - meta_w, cy),
                meta,
                font=f_meta,
                fill=INK_SOFT,
                anchor="lm",
            )
        y += ROW_H
    draw.line([(PAD, y), (W - PAD, y)], fill=LINE_SOFT)

    # 尾行：合计 + 最热时段
    f_foot = _font(FOOT_SIZE)
    fy = y + GAP + FOOT_H // 2
    foot = f"合计 {total} 条"
    if peak_text:
        foot += f" · 最热闹的时段：{peak_text}"
    draw.text((PAD + 12, fy), foot, font=f_foot, fill=INK_SOFT, anchor="lm")
    draw.line(
        [(PAD, y + GAP), (PAD, y + GAP + FOOT_H)],
        fill=INK,
        width=3,
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
