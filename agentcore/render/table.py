"""Markdown 表格 → PNG 渲染（纯 Python，Pillow + 中文字体候选链）。

为什么需要：QQ 聊天框不渲染 Markdown——表格以纯文本发出时竖线错位、
观感极差（用户实测反馈）。这里把表格块渲染成图片，投递层以图片消息发出，
其余文本照常走分层。

边界与护栏（评审 REVIEW-46c85d1..6ec3f7c M-1/H-1）：
- 只识别标准 MD 表格（``| a | b |`` 连续行 + 第二行 ``|---|`` 分隔行）
- **规模护栏**：列数 > _MAX_COLS 整表拒绝（渲染返回 None 降级纯文本）；
  行数 > _MAX_ROWS 截断并注明；单元格 > _MAX_CELL_CHARS 截断加省略号——
  零护栏时 1601 列会 ``Image.new((0,h))`` 崩溃、单格 1000 字渲染 24.6s
- 无可用中文字体时返回 None——调用方必须降级为原文本（宁可文本错位，
  也不要发出字体缺失的"豆腐块"图片）；进程内首次渲染时探测字体链并打
  **一条**汇总 WARNING（不再每条含表回复刷屏）
- 单元格内转义竖线 ``\\|`` 不解析（MD 表格里罕见，先不处理）
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# 字体候选链（与 plugins/qq_agent_adapter/help_render.py 的 _FONT_CANDIDATES
# 同一思路，评审 L-3）：仓库字体优先（editable 安装/克隆即到位），再回落
# 常见系统路径（wheel 安装时 parents[2] 不再指向项目根，data/fonts 不在包里，
# 但部署机通常装有系统字体）
_FONT_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "data" / "fonts" / "wqy-zenhei.ttc",
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    Path("/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc"),
    Path("/usr/local/share/fonts/wqy-zenhei.ttc"),
]

_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_SEP_LINE = re.compile(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+(\s*:?-{2,}:?\s*)?\|?\s*$")

# 渲染参数
_BASE_FONT_SIZE = 16
_MIN_FONT_SIZE = 10
_CELL_PAD_X = 12
_CELL_PAD_Y = 8
_MAX_WIDTH = 1600  # 超过则缩小字号（QQ 端超宽图会被压缩得看不清）
_HEADER_BG_DARK = "#2C3E50"  # 表头深色底（层次感；旧实现浅灰底+黑字过于素）
_HEADER_TEXT = "#FFFFFF"  # 表头白字
_ZEBRA_BG = "#F5F6FA"  # 斑马纹：数据行隔行浅底，长表可读性
_TITLE_COLOR = "#2C3E50"  # `#` 标题行文字/左侧色条
_TITLE_GAP = 14  # 标题区每行附加间距（像素）
_GRID_COLOR = "#999999"
_HEADER_LINE_COLOR = "#333333"
_TEXT_COLOR = "#1A1A1A"

# 规模护栏（评审 M-1）
_MAX_ROWS = 100  # 行数上限，超出截断并注明
_MAX_COLS = 20  # 列数上限，超出整表降级纯文本（列宽计算是主要耗时来源）
_MAX_CELL_CHARS = 200  # 单元格字符上限，超出截断加省略号（杜绝 O(n²) 逐字符截断）


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


_TITLE_RE = re.compile(r"^#{1,6}\s+(.*)$")


def _parse_rows(table_md: str) -> tuple[list[str], list[list[str]]]:
    """解析 MD 表格块：返回 ``(标题行, 表格行)``。

    ``#`` 开头的行作为标题——`/usage` 统计表带 `#`/`##` 小节标题，混进表格
    会渲染成孤立的单格行；标题在 render_table_png 里渲染为独立标题条。
    分隔行（``|---|``）按正则剔除（此前按 lineno==1，标题混入后行号漂移）。
    """
    titles: list[str] = []
    rows: list[list[str]] = []
    for line in table_md.split("\n"):
        s = line.strip()
        if not s:
            continue
        m = _TITLE_RE.match(s)
        if m:
            titles.append(m.group(1).strip())
            continue
        if _SEP_LINE.match(s):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        rows.append(cells)
    return titles, rows


def _load_font(size: int, font_path: Path | None = None):
    """加载字体：显式 font_path 不回落（调用方意图）；None 走候选链。"""
    paths = [font_path] if font_path is not None else _FONT_CANDIDATES
    last_err: Exception | None = None
    for p in paths:
        try:
            return ImageFont.truetype(str(p), size)
        except Exception as e:  # 文件缺失/损坏都试下一个
            last_err = e
    raise last_err or OSError("no font available")


# 进程内字体探测（评审 L-4：字体缺失时每条含表回复刷 warning，运维无从得知
# 功能其实没生效——改为首次探测打一条汇总日志）
_font_probe_done = False
_font_ok = False


def ensure_font_probed() -> bool:
    """首次调用探测字体链并打一条汇总 WARNING（之后缓存），返回是否可用。"""
    global _font_probe_done, _font_ok
    if _font_probe_done:
        return _font_ok
    _font_probe_done = True
    try:
        _load_font(_BASE_FONT_SIZE)
        _font_ok = True
    except Exception:
        _font_ok = False
        logger.warning(
            "table-to-image: 候选字体链全部不可用，表格将降级为纯文本。候选：%s",
            ", ".join(str(p) for p in _FONT_CANDIDATES),
        )
    return _font_ok


def _text_w(font: ImageFont.FreeTypeFont, text: str) -> int:
    if not text:
        return 0
    return font.getbbox(text)[2]


def _fit_text(font: ImageFont.FreeTypeFont, text: str, max_w: int) -> str:
    """把 text 截到 max_w 像素内（二分，log n 次测量），末尾省略号。

    评审 M-1：旧实现逐字符 ``text[:-1]`` + 每次全量 getbbox 复测是 O(n²)，
    单格 1000 字渲染 24.6s。入口已有 _MAX_CELL_CHARS 预截断，这里二分兜底。
    """
    if _text_w(font, text) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_w(font, text[:mid] + "…") <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + "…") if lo > 0 else "…"


def render_table_png(table_md: str, font_path: Path | None = None) -> bytes | None:
    """把一个 MD 表格块渲染为 PNG bytes；无可用字体/超规模返回 None。"""
    if font_path is None and not ensure_font_probed():
        return None
    titles, rows = _parse_rows(table_md)
    if not rows or not any(cell for row in rows for cell in row):
        return None  # 空表（无单元格内容）不渲染

    n_cols = max(len(r) for r in rows)
    if n_cols > _MAX_COLS:
        logger.warning("表格列数 %d 超过上限 %d，降级为纯文本", n_cols, _MAX_COLS)
        return None

    # 行数护栏：截断并注明（评审 M-1）
    orig_rows = len(rows)
    if orig_rows > _MAX_ROWS:
        rows = rows[:_MAX_ROWS]
        note = f"（共 {orig_rows} 行，仅显示前 {_MAX_ROWS} 行）"
        if n_cols >= 2:
            rows.append(["…", note] + [""] * (n_cols - 2))
        else:  # 单列表格：注明并进第一格
            rows.append([f"… {note}"])

    # 单元格长度护栏（杜绝 O(n²) 逐字符截断的输入放大）
    rows = [
        [c[:_MAX_CELL_CHARS] + ("…" if len(c) > _MAX_CELL_CHARS else "") for c in r]
        for r in rows
    ]
    rows = [r + [""] * (n_cols - len(r)) for r in rows]  # 补齐缺列

    try:
        font = _load_font(_BASE_FONT_SIZE, font_path)
    except Exception:
        logger.warning("table render: font unavailable, skip")
        return None

    # 字号自适应：基础字号测一轮，超宽按比例缩一次字号重测（两轮封顶，
    # 评审 H-1：旧实现 16→10 七轮全量重算是 7.3s 阻塞的主因之一）
    col_w = [
        max(_text_w(font, r[c]) for r in rows) + 2 * _CELL_PAD_X for c in range(n_cols)
    ]
    total_w = sum(col_w)
    if total_w > _MAX_WIDTH:
        scaled = max(_MIN_FONT_SIZE, int(_BASE_FONT_SIZE * _MAX_WIDTH / total_w))
        if scaled != _BASE_FONT_SIZE:
            try:
                font = _load_font(scaled, font_path)
            except Exception:
                logger.warning("table render: font reload failed, skip")
                return None
            col_w = [
                max(_text_w(font, r[c]) for r in rows) + 2 * _CELL_PAD_X
                for c in range(n_cols)
            ]
            total_w = sum(col_w)
    if total_w > _MAX_WIDTH:
        # 最小字号仍超宽：等比分列宽（列数已 ≤ _MAX_COLS，不会出现除零）
        scale = _MAX_WIDTH / total_w
        col_w = [max(1, int(w * scale)) for w in col_w]
        total_w = sum(col_w)

    line_h = font.size + 2 * _CELL_PAD_Y
    # 标题区（`#` 行）：深色文字 + 左侧色条，字号比正文大 6px
    try:
        title_font = _load_font(font.size + 6, font_path)
    except Exception:
        title_font = font
    title_line_h = title_font.size + _TITLE_GAP
    titles_h = title_line_h * len(titles)
    total_h = titles_h + line_h * len(rows)
    img = Image.new("RGB", (total_w, total_h), "white")
    draw = ImageDraw.Draw(img)

    # 标题条（左对齐 + 左侧深色色条；超宽二分截断）
    y = 0
    for t in titles:
        draw.rectangle([0, y + 3, 4, y + 3 + title_font.size], fill=_TITLE_COLOR)
        draw.text(
            (_CELL_PAD_X + 4, y),
            _fit_text(title_font, t, total_w - _CELL_PAD_X * 2),
            font=title_font,
            fill=_TITLE_COLOR,
        )
        y += title_line_h

    # 表头：深色底 + 白字 + 下粗线
    y0 = titles_h
    draw.rectangle([0, y0, total_w, y0 + line_h], fill=_HEADER_BG_DARK)
    draw.line(
        [(0, y0 + line_h), (total_w, y0 + line_h)],
        fill=_HEADER_LINE_COLOR,
        width=2,
    )
    # 斑马纹（数据行隔行浅底）
    y = y0 + line_h
    for ri in range(1, len(rows)):
        if ri % 2 == 0:
            draw.rectangle([0, y, total_w, y + line_h], fill=_ZEBRA_BG)
        y += line_h
    # 网格线
    x = 0
    for c in range(n_cols):
        draw.line([(x, y0), (x, total_h)], fill=_GRID_COLOR, width=1)
        x += col_w[c]
    draw.line([(total_w, y0), (total_w, total_h)], fill=_GRID_COLOR, width=1)
    y = y0
    for _ in range(len(rows) + 1):
        draw.line([(0, y), (total_w, y)], fill=_GRID_COLOR, width=1)
        y += line_h
    # 外框（深色 2px 压住网格，整体更挺）
    draw.rectangle([0, y0, total_w - 1, total_h - 1], outline=_HEADER_BG_DARK, width=2)

    # 文字（表头白字 + 数据行深字；左对齐 + 垂直居中；超宽二分截断加省略号）
    y = y0
    for ri, row in enumerate(rows):
        x = 0
        for ci, cell in enumerate(row):
            draw.text(
                (x + _CELL_PAD_X, y + _CELL_PAD_Y),
                _fit_text(font, cell, col_w[ci] - 2 * _CELL_PAD_X),
                font=font,
                fill=_HEADER_TEXT if ri == 0 else _TEXT_COLOR,
            )
            x += col_w[ci]
        y += line_h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
