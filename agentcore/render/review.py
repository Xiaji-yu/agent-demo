"""个人群周报卡片渲染：事实 + 群话题词背景 + LLM 评语（PNG 出）。

与 ``rank.py``（发言榜）同一套绘制语言：纸色底、1px 墨线、45° 斜纹分隔、
直角面板（``agentcore/render/style.py`` token，与 /agent-web CSS 对应）。
差异只在内容组织——这是给**一个人**看的卡：

    标题（我的群周报 | 区间）
    事实面板：条数/字数 · 群内排名 · 活跃天数 · 最热时段
    话题词行：群近期热门词（背景，墨阶 pill）
    评语面板：LLM 点评（CJK 贪心换行；为空则整块省略 = 纯事实卡）

约定与 rank/overview 一致：无中文字体 / 异常 / 事实全空返回 None，
调用方降级为纯文本。
"""

from __future__ import annotations

import io
import logging

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

W = 640
PAD = 32
GAP = 14

TITLE_SIZE = 27
LABEL_SIZE = 16
VALUE_SIZE = 19
WORD_SIZE = 15
COMMENT_SIZE = 18

TITLE_LINE_H = 50
FACT_ROW_H = 40
FACT_PAD = 16
COMMENT_LINE_H = 30
COMMENT_PAD = 18


def _wrap(draw: ImageDraw.ImageDraw, text: str, f, max_w: int) -> list[str]:
    """CJK 贪心换行（按字符切，标点不悬挂——评语短，简单策略够用）。"""
    lines: list[str] = []
    cur = ""
    for ch in text or "":
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if draw.textlength(cur + ch, font=f) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines or [""]


def _fact_row(
    draw: ImageDraw.ImageDraw, x: int, y: int, label: str, value: str
) -> None:
    f_label = _font(LABEL_SIZE)
    f_value = _font(VALUE_SIZE, bold=True)
    draw.text((x, y), label, font=f_label, fill=MUTED, anchor="la")
    draw.text((x + 96, y), value, font=f_value, fill=INK, anchor="la")


def render_review_png(
    title: str,
    facts: dict,
    comment: str = "",
    *,
    words: list[str] | None = None,
    words_label: str = "群话题",
) -> bytes | None:
    """渲染个人周报卡。facts 至少含 msgs（全空/无字体/异常 → None）。

    ``words_label``：话题词行的标签——展示本人高频词时传「你的高频词」，
    展示群话题背景时用默认「群话题」。
    """
    if not ensure_font_probed():
        return None
    if not facts or not int(facts.get("msgs") or 0):
        return None
    try:
        return _render(title, facts, comment or "", words or [], words_label)
    except Exception:
        logger.warning("review render failed", exc_info=True)
        return None


def _render(
    title: str, facts: dict, comment: str, words: list[str], words_label: str
) -> bytes:
    f_comment = _font(COMMENT_SIZE)
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    comment_lines = (
        _wrap(probe, comment, f_comment, W - 2 * PAD - 2 * COMMENT_PAD)
        if comment
        else []
    )
    word_rows = 1 if words else 0

    height = (
        PAD
        + TITLE_LINE_H
        + GAP
        + FACT_PAD * 2
        + FACT_ROW_H * 3
        + (GAP + 34 + word_rows * 34 if words else 0)
        + (
            GAP + COMMENT_PAD * 2 + len(comment_lines) * COMMENT_LINE_H
            if comment_lines
            else 0
        )
        + PAD
    )
    img = Image.new("RGB", (W, height), PAPER)
    draw = ImageDraw.Draw(img)

    # 标题 + 斜纹分隔
    draw.text(
        (PAD, PAD), title, font=_font(TITLE_SIZE, bold=True), fill=INK, anchor="la"
    )
    sep_y = PAD + TITLE_LINE_H + (GAP - 6) // 2
    hatch(img, (PAD, sep_y, W - PAD, sep_y + 6), step=5)

    y = PAD + TITLE_LINE_H + GAP
    # 事实面板（直角面板 + 左侧墨条，rank 尾行同款 accents）
    panel_h = FACT_PAD * 2 + FACT_ROW_H * 3
    draw.rectangle((PAD, y, W - PAD, y + panel_h), fill=PAPER_2)
    draw.rectangle((PAD, y, PAD + 3, y + panel_h), fill=INK)
    rank = int(facts.get("rank") or 0)
    users = int(facts.get("users") or 0)
    rank_text = f"第 {rank} 名 / {users} 人" if rank and users else "—"
    peak = str(facts.get("peak_text") or "数据不足")
    _fact_row(
        draw,
        PAD + FACT_PAD,
        y + FACT_PAD,
        "发言",
        f"{facts.get('msgs') or 0} 条 · {facts.get('chars') or 0} 字",
    )
    _fact_row(draw, PAD + FACT_PAD, y + FACT_PAD + FACT_ROW_H, "群内排名", rank_text)
    _fact_row(
        draw,
        PAD + FACT_PAD,
        y + FACT_PAD + FACT_ROW_H * 2,
        "活跃",
        f"{facts.get('days') or 0} 天 · 最热 {peak}",
    )
    y += panel_h

    # 话题词行（群背景，非本人数据——pill 用描边样式与事实区分）
    if words:
        y += GAP
        draw.text(
            (PAD, y + 17), words_label, font=_font(LABEL_SIZE), fill=MUTED, anchor="lm"
        )
        x = PAD + 76
        f_word = _font(WORD_SIZE)
        for w in words[:10]:
            tw = draw.textlength(str(w), font=f_word)
            box_w = int(tw) + 16
            if x + box_w > W - PAD:
                break  # 一行放不下就到此为止（不换行、不溢出）
            pill(draw, (x, y, x + box_w, y + 30), str(w), f_word)
            x += box_w + 8
        y += 34

    # 评语面板
    if comment_lines:
        y += GAP
        box_h = COMMENT_PAD * 2 + len(comment_lines) * COMMENT_LINE_H
        draw.rectangle((PAD, y, W - PAD, y + box_h), outline=INK, width=1)
        draw.text(
            (PAD + COMMENT_PAD, y + COMMENT_PAD - 4),
            "本周锐评",
            font=_font(LABEL_SIZE, bold=True),
            fill=INK,
            anchor="la",
        )
        cy = y + COMMENT_PAD + 18
        for line in comment_lines:
            draw.text(
                (PAD + COMMENT_PAD, cy),
                line,
                font=f_comment,
                fill=INK_SOFT,
                anchor="la",
            )
            cy += COMMENT_LINE_H
        y += box_h
    draw.line([(PAD, y + 2), (W - PAD, y + 2)], fill=LINE_SOFT)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
