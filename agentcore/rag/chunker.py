"""文本切块：按标题/段落聚合，超长段落再按句子边界切分（带少量重叠）。

知识库检索的粒度由这里决定：块太大→召回噪声多、注入 prompt 占位多；
太小→语义不完整。默认目标 600 字、重叠 80 字。
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"(?m)^(#{1,6}\s+.+)$")
_SENTENCE_END = "。！？；\n!?;"


def _split_paragraphs(text: str) -> list[str]:
    """按空行分段，标题行单独成段。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _HEADING_RE.sub(r"\n\n\1\n\n", text)
    parts = [p.strip() for p in re.split(r"\n\s*\n", text)]
    return [p for p in parts if p]


def _split_long(par: str, max_chars: int, overlap: int) -> list[str]:
    """把超长段落按句子边界切开，保留 overlap 个字符的重叠。"""
    out: list[str] = []
    start = 0
    n = len(par)
    while start < n:
        end = min(start + max_chars, n)
        if end >= n:
            piece = par[start:].strip()
            if piece:
                out.append(piece)
            break
        # 在 [start + max_chars//2, end] 区间内回退到最近的句末
        cut = -1
        floor = max(start + max_chars // 2, start + 1)
        for i in range(end, floor - 1, -1):
            if par[i - 1] in _SENTENCE_END:
                cut = i
                break
        if cut == -1:
            cut = par.rfind(" ", floor, end)
        if cut <= start:
            cut = end
        piece = par[start:cut].strip()
        if piece:
            out.append(piece)
        start = max(cut - overlap, start + 1)  # 保证前进，不会死循环
    return out


def chunk_text(text: str, max_chars: int = 600, overlap: int = 80) -> list[str]:
    """把文本切成若干块；空白输入返回空列表。"""
    if not text or not text.strip():
        return []
    max_chars = max(100, int(max_chars))
    overlap = max(0, min(int(overlap), max_chars // 2))
    chunks: list[str] = []
    buf = ""

    def flush():
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for par in _split_paragraphs(text):
        if len(par) > max_chars:
            flush()
            chunks.extend(_split_long(par, max_chars, overlap))
            continue
        if not buf:
            buf = par
        elif len(buf) + len(par) + 2 <= max_chars:
            buf = f"{buf}\n\n{par}"
        else:
            flush()
            buf = par
    flush()
    return chunks
