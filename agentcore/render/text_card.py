"""通用文字卡片渲染：把纯文本 / 歌词 / 长文渲染成项目风格长图（PNG 出）。

与 ``overview.py`` / ``rank.py`` / ``review.py`` 共用 ``style.py`` 的绘制语言：
纸色底、1px 墨线直角面板、45° 斜纹、黑白状态表达（与 ``/agent-web`` CSS 对应）。

    页眉：项目标记 + 名称 / 右侧时间
    标题（可选）
    正文面板：CJK 贪心换行，左侧 3px 墨条
    页脚：来源说明 / 字数或「已截断」

**门禁**（防止一段三万字把 Pillow 拖死——同步渲染无法被超时取消）：

- 输入超过 ``MAX_TEXT_CHARS_SOFT``：按行截断，``truncated=True`` / ``reason="too_long"``
- 画布超过 ``MAX_IMAGE_HEIGHT``：按行截断，``truncated=True`` / ``reason="too_large"``
- 无中文字体 / 空内容 / 绘制异常：``png=None`` + 明确 ``reason``，调用方降级文本

**不做的事**：不解析 Markdown/HTML、不执行任何内容、不联网。缺字形的 emoji
由字体渲染成方框（与其它渲染图一致），调用方不要据此判断内容丢失。

纯绘制模块：不 import nonebot、不碰网络。
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

from PIL import Image, ImageDraw

from agentcore import tz
from agentcore.render.fonts import ensure_font_probed
from agentcore.render.fonts import font as _font
from agentcore.render.style import INK, INK_SOFT, MUTED, PAPER, hatch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 门禁
MAX_TEXT_CHARS_SOFT = 6000  # 单张图完整渲染的软上限（约 60–80 行）
MAX_TEXT_CHARS_HARD = 12000  # skill 入口硬上限：超过直接拒绝，提示摘要/分段
MAX_IMAGE_HEIGHT = 4000  # 画布高度上限（手机长图可读范围）
MAX_PIXELS = 8_000_000  # 兜底：900×4000 远低于此，防未来改宽后失控

# ---------------------------------------------------------------- 画布与排版
W = 900
PAD = 32
GAP = 14
HEADER_H = 26
TITLE_SIZE = 30
TITLE_LINE_H = 44
BODY_SIZE = 19
BODY_LINE_H = 32
BODY_PAD_X = 24
BODY_PAD_Y = 22
FOOT_SIZE = 13
FOOT_LINE_H = 18

_CONTROL_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class TextCardResult:
    """渲染结果：png 为 None 时 reason 说明降级原因。"""

    png: bytes | None
    truncated: bool = False
    reason: str = ""
    chars: int = 0


def render_text_card_png(
    body: str,
    title: str = "",
    footer: str = "",
    *,
    now: float | None = None,
) -> TextCardResult:
    """把 ``body`` 渲染成 PNG 长图；任何失败都返回空 png + reason，不抛异常。"""
    text = _clean(body)
    if not text:
        return TextCardResult(None, reason="empty")

    if not ensure_font_probed():
        return TextCardResult(None, reason="no_font")

    total_chars = len(text)
    truncated = False
    reason = ""
    if total_chars > MAX_TEXT_CHARS_SOFT:
        text = _truncate_chars(text, MAX_TEXT_CHARS_SOFT)
        truncated = True
        reason = "too_long"

    try:
        png, cut_by_height = _render(
            text, title.strip(), footer.strip(), now or tz.now().timestamp()
        )
    except Exception:
        logger.warning("text card render failed", exc_info=True)
        return TextCardResult(None, reason="render_error", chars=total_chars)

    if cut_by_height:
        truncated = True
        reason = reason or "too_large"
    return TextCardResult(png, truncated=truncated, reason=reason, chars=total_chars)


def _clean(text: str) -> str:
    """控制字符清洗 + 统一换行；与 engine/fs 的清洗口径一致（保留 \\n）。"""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _CONTROL_RE.sub("", t).replace("\t", "    ")
    return t.strip()


def _truncate_chars(text: str, limit: int) -> str:
    """按字符截断，尽量落在最近的换行处，避免切半个词。"""
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit * 0.8:
        cut = cut[:nl]
    return cut.rstrip()


def _wrap(draw: ImageDraw.ImageDraw, text: str, f, max_w: int) -> list[str]:
    """CJK 贪心换行：显式换行保留；无空格长串（URL/英文）按字符切。"""
    lines: list[str] = []
    for raw in text.split("\n"):
        if not raw:
            lines.append("")
            continue
        cur = ""
        for ch in raw:
            if cur and draw.textlength(cur + ch, font=f) > max_w:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)
    return lines or [""]


def _fit(draw: ImageDraw.ImageDraw, text: str, f, max_w: int) -> str:
    if draw.textlength(text, font=f) <= max_w:
        return text
    cur = text
    while cur and draw.textlength(cur + "…", font=f) > max_w:
        cur = cur[:-1]
    return (cur + "…") if cur else ""


def _clock(ts: float) -> str:
    return tz.fmt_ts(ts, "%Y-%m-%d %H:%M")


def _render(text: str, title: str, footer: str, now: float) -> tuple[bytes, bool]:
    f_body = _font(BODY_SIZE)
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    lines = _wrap(probe, text, f_body, W - 2 * PAD - 2 * BODY_PAD_X)

    has_title = bool(title)
    title_h = (TITLE_LINE_H + GAP + 6) if has_title else 0
    fixed = PAD + HEADER_H + GAP + title_h + BODY_PAD_Y * 2 + GAP + FOOT_LINE_H + PAD
    max_body_lines = max(1, (MAX_IMAGE_HEIGHT - fixed) // BODY_LINE_H)
    cut_by_height = len(lines) > max_body_lines
    if cut_by_height:
        lines = lines[:max_body_lines]

    body_h = BODY_PAD_Y * 2 + len(lines) * BODY_LINE_H
    # 高度由截断后的行数决定；max_lines 已经保证不越界，这里再抛是防截断逻辑被改坏
    # （不要用 min() 掩盖：那样行数没截断时图会被静默裁掉，truncated 标记却仍是 True）
    height = fixed + len(lines) * BODY_LINE_H
    if height > MAX_IMAGE_HEIGHT:
        raise ValueError("text card height exceeds MAX_IMAGE_HEIGHT")
    if W * height > MAX_PIXELS:  # pragma: no cover - 当前常量下不可达
        raise ValueError("text card exceeds MAX_PIXELS")

    img = Image.new("RGB", (W, height), PAPER)
    draw = ImageDraw.Draw(img)

    # 页眉：左侧 4px 墨方块 + 名称（用几何标记而非 ◆ 字形，避免字体缺字）
    draw.rectangle((PAD, PAD + 4, PAD + 4, PAD + 8), fill=INK)
    draw.text(
        (PAD + 12, PAD),
        "AGENT-DEMO",
        font=_font(14, bold=True),
        fill=INK,
        anchor="la",
    )
    draw.text(
        (W - PAD, PAD + 1),
        _clock(now),
        font=_font(12),
        fill=MUTED,
        anchor="ra",
    )
    hatch_y = PAD + HEADER_H + (GAP - 6) // 2
    hatch(img, (PAD, hatch_y, W - PAD, hatch_y + 6), step=5)

    y = PAD + HEADER_H + GAP
    if has_title:
        y += 6  # 标题与页眉斜纹条留出呼吸间距
        draw.text(
            (PAD, y),
            _fit(probe, title, _font(TITLE_SIZE, bold=True), W - 2 * PAD),
            font=_font(TITLE_SIZE, bold=True),
            fill=INK,
            anchor="la",
        )
        y += TITLE_LINE_H + GAP

    draw.rectangle((PAD, y, W - PAD, y + body_h), fill=PAPER, outline=INK, width=1)
    draw.rectangle((PAD, y, PAD + 3, y + body_h), fill=INK)
    cy = y + BODY_PAD_Y - 2
    for line in lines:
        draw.text(
            (PAD + BODY_PAD_X, cy),
            line,
            font=f_body,
            fill=INK_SOFT,
            anchor="la",
        )
        cy += BODY_LINE_H
    y += body_h + GAP

    f_foot = _font(FOOT_SIZE)
    left = footer or "agent-demo · text card"
    draw.text(
        (PAD, y),
        _fit(probe, left, f_foot, W - 2 * PAD - 120),
        font=f_foot,
        fill=MUTED,
        anchor="la",
    )
    right = "已截断" if cut_by_height else f"{len(text)} 字"
    draw.text((W - PAD, y), right, font=f_foot, fill=MUTED, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), cut_by_height
