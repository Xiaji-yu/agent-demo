"""Markdown 表格 → PNG 渲染（纯 Python，Pillow + 文泉驿中文字体）。

为什么需要：QQ 聊天框不渲染 Markdown——表格以纯文本发出时竖线错位、
观感极差（用户实测反馈）。这里把表格块渲染成图片，投递层以图片消息发出，
其余文本照常走分层。

边界：
- 只识别标准 MD 表格（``| a | b |`` 连续行 + 第二行 ``|---|`` 分隔行）
- 无可用中文字体时返回 None——调用方必须降级为原文本（宁可文本错位，
  也不要发出字体缺失的"豆腐块"图片）
- 单元格内转义竖线 ``\\|`` 不解析（MD 表格里罕见，先不处理）
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# 项目根的 data/fonts/wqy-zenhei.ttc（agentcore/render/table.py → 上两级即项目根）
_DEFAULT_FONT = (
    Path(__file__).resolve().parents[2] / "data" / "fonts" / "wqy-zenhei.ttc"
)

_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_LINE = re.compile(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+(\s*:?-{2,}:?\s*)?\|?\s*$")

# 渲染参数
_BASE_FONT_SIZE = 16
_MIN_FONT_SIZE = 10
_CELL_PAD_X = 12
_CELL_PAD_Y = 8
_MAX_WIDTH = 1600  # 超过则缩小字号（QQ 端超宽图会被压缩得看不清）
_HEADER_BG = "#F0F0F0"
_GRID_COLOR = "#999999"
_HEADER_LINE_COLOR = "#333333"
_TEXT_COLOR = "#1A1A1A"


def split_tables(text: str) -> tuple[list[str], str]:
    """把文本里的 MD 表格块摘出来。

    返回 ``(表格块列表, 去掉表格后的文本)``。没有表格时第二个元素等于原文。
    摘除后压缩多余空行，避免原文出现大段空白。
    """
    if not text:
        return [], text or ""
    lines = text.split("\n")
    tables: list[str] = []
    kept: list[str] = []
    i = 0
    while i < len(lines):
        if _TABLE_LINE.match(lines[i]):
            j = i
            while j < len(lines) and _TABLE_LINE.match(lines[j]):
                j += 1
            block = lines[i:j]
            # 至少两行且第二行是分隔行才算表格（单行 | 不认，防误伤普通文本）
            if len(block) >= 2 and _SEP_LINE.match(block[1]):
                tables.append("\n".join(block))
                i = j
                continue
        kept.append(lines[i])
        i += 1
    rest = "\n".join(kept)
    rest = re.sub(r"\n{3,}", "\n\n", rest).strip()
    return tables, rest


def _parse_rows(table_md: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for lineno, line in enumerate(table_md.split("\n")):
        if lineno == 1:  # 分隔行不进内容
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    return rows


def _load_font(size: int, font_path: Path | None = None) -> ImageFont.FreeTypeFont:
    path = font_path or _DEFAULT_FONT
    return ImageFont.truetype(str(path), size)


def _text_w(font: ImageFont.FreeTypeFont, text: str) -> int:
    if not text:
        return 0
    return font.getbbox(text)[2]


def render_table_png(table_md: str, font_path: Path | None = None) -> bytes | None:
    """把一个 MD 表格块渲染为 PNG bytes；无可用字体返回 None。"""
    rows = _parse_rows(table_md)
    if not rows or not any(cell for row in rows for cell in row):
        return None  # 空表（无单元格内容）不渲染
    try:
        font = _load_font(_BASE_FONT_SIZE, font_path)
    except Exception:
        logger.warning("table render: font unavailable, skip", exc_info=True)
        return None

    n_cols = max(len(r) for r in rows)
    rows = [r + [""] * (n_cols - len(r)) for r in rows]  # 补齐缺列

    # 字号自适应：从基础字号递减，直到总宽不超上限
    for size in range(_BASE_FONT_SIZE, _MIN_FONT_SIZE - 1, -1):
        if size != _BASE_FONT_SIZE:
            font = _load_font(size, font_path)
        col_w = [
            max(_text_w(font, r[c]) for r in rows) + 2 * _CELL_PAD_X
            for c in range(n_cols)
        ]
        total_w = sum(col_w)
        if total_w <= _MAX_WIDTH:
            break
    else:
        # 缩到最小字号仍超宽：截断最后一列之外无解——截断每列文本宽度
        col_w = [min(w, _MAX_WIDTH // n_cols) for w in col_w]
        total_w = sum(col_w)

    line_h = font.size + 2 * _CELL_PAD_Y
    total_h = line_h * len(rows)
    img = Image.new("RGB", (total_w, total_h), "white")
    draw = ImageDraw.Draw(img)

    # 表头底纹 + 表头下粗线
    draw.rectangle([0, 0, total_w, line_h], fill=_HEADER_BG)
    draw.line([(0, line_h), (total_w, line_h)], fill=_HEADER_LINE_COLOR, width=2)
    # 网格线
    x = 0
    for c in range(n_cols):
        draw.line([(x, 0), (x, total_h)], fill=_GRID_COLOR, width=1)
        x += col_w[c]
    draw.line([(total_w, 0), (total_w, total_h)], fill=_GRID_COLOR, width=1)
    y = 0
    for _ in range(len(rows) + 1):
        draw.line([(0, y), (total_w, y)], fill=_GRID_COLOR, width=1)
        y += line_h

    # 文字（左对齐 + 垂直居中；超宽单元格截断加省略号）
    y = 0
    for row in rows:
        x = 0
        for ci, cell in enumerate(row):
            text = cell
            while _text_w(font, text) > col_w[ci] - 2 * _CELL_PAD_X and len(text) > 1:
                text = text[:-1]
            if text != cell:
                text = text[:-1] + "…" if len(text) > 1 else "…"
            draw.text(
                (x + _CELL_PAD_X, y + _CELL_PAD_Y),
                text,
                font=font,
                fill=_TEXT_COLOR,
            )
            x += col_w[ci]
        y += line_h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
