"""知识摄取：把文本/文件切块、向量化后写入公共知识库。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

from agentcore.rag.chunker import chunk_text
from agentcore.rag.sanitize import scrub_pii

logger = logging.getLogger(__name__)

MAX_INGEST_BYTES = 2 * 1024 * 1024  # 单个文本/文件最多摄取 2MB
MAX_CHUNKS_PER_SOURCE = 200         # 单个来源默认最多切块数（防一次灌爆知识库）
MAX_CHUNKS_ENV = "AGENT_KB_MAX_CHUNKS_PER_SOURCE"


def _coerce_positive(raw, *, label: str, default: int) -> int:
    """把外部来的上限值收敛成正整数；脏值一律告警并回退默认（M2）。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r 不是整数，回退默认 %d", label, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%s 非法（须 > 0），回退默认 %d", label, value, default)
        return default
    return value


def max_chunks_per_source() -> int:
    """单来源块数上限：可被 ``AGENT_KB_MAX_CHUNKS_PER_SOURCE`` 覆盖。

    默认 200 对长文档会**静默砍尾**（评审 REVIEW-bbd8913..f6dffcc.md 的 H1），
    因此这里要求上限可配置，并让调用方知道丢弃了多少。
    """
    raw = (os.getenv(MAX_CHUNKS_ENV) or "").strip()
    if not raw:
        return MAX_CHUNKS_PER_SOURCE
    return _coerce_positive(raw, label=MAX_CHUNKS_ENV, default=MAX_CHUNKS_PER_SOURCE)


def content_digest(text: str) -> str:
    """语料内容指纹（strip 后的原始文本 sha256），用于判重与"内容变更检测"。"""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


async def ingest_text(
    store,
    embedding,
    text: str,
    *,
    name: str,
    kind: str = "manual",
    location: str = "",
    max_chars: int = 600,
    scrub: bool = True,
    max_chunks: int | None = None,
) -> dict:
    """摄取一段文本。返回 ``{source_id, chunks, chunks_total, dropped, sha256, truncated}``；
    文本为空返回 ``chunks=0``。

    超限时**不再静默丢弃**：``dropped`` 报告被丢弃的块数并打 WARNING（H1 修复）。
    """
    if embedding is None:
        raise RuntimeError("embedding client unavailable")
    body = (text or "").strip()
    if not body:
        return {"source_id": None, "chunks": 0, "chunks_total": 0, "dropped": 0, "sha256": "", "truncated": False}

    # 指纹基于 strip 后的原始输入（截断/脱敏之前），脚本可用同一算法独立计算
    digest = content_digest(body)

    truncated = False
    if len(body.encode("utf-8")) > MAX_INGEST_BYTES:
        body = body[: MAX_INGEST_BYTES // 4]
        truncated = True
        logger.warning("ingest: %s 超过 %d 字节上限，截断到 %d 字符", name, MAX_INGEST_BYTES, len(body))

    if scrub:
        # 管理员投喂的内容同样过一遍 PII 掩码：公共库不存身份标识。
        # 大文本的 CPU 段放线程池，避免卡住事件循环（本地 embedding 服务场景）
        body = await asyncio.to_thread(scrub_pii, body)

    # M2：显式传入的 max_chunks 同样要收敛——负值会让 all_chunks[:-5] 静默砍掉尾部，
    # 甚至切出 0 块而调用方误以为成功
    if max_chunks is None:
        limit = max_chunks_per_source()
    else:
        limit = _coerce_positive(
            max_chunks, label="max_chunks", default=MAX_CHUNKS_PER_SOURCE
        )
    all_chunks = await asyncio.to_thread(chunk_text, body, max_chars=max_chars)
    chunks = all_chunks[:limit]
    dropped = len(all_chunks) - len(chunks)
    if dropped > 0:
        # 评审 H1：此前是 [:200] 静默砍尾，调用方无从察觉；现在必须留下可观测告警
        logger.warning(
            "ingest: %s 切出 %d 块，超过单来源上限 %d，丢弃 %d 块（尾部内容不入库；"
            "可用 %s 调整上限）",
            name,
            len(all_chunks),
            limit,
            dropped,
            MAX_CHUNKS_ENV,
        )
    if not chunks:
        # M2：这里必须回报真实计数——此前写死 chunks_total=0、dropped=0，
        # 与上面刚打出的「丢弃 N 块」WARNING 自相矛盾，调用方无从判断
        return {
            "source_id": None, "chunks": 0, "chunks_total": len(all_chunks),
            "dropped": dropped, "sha256": digest, "truncated": truncated,
        }
    embeddings = await embedding.embed_many(chunks)
    source_id = await store.kb_add_source(
        name=name,
        kind=kind,
        location=location,
        meta={
            "chunks": len(chunks),
            "chunks_total": len(all_chunks),
            "dropped": dropped,
            "sha256": digest,
            "truncated": truncated,
        },
    )
    try:
        written = await store.kb_add_chunks(source_id, chunks, embeddings)
    except Exception:
        # L12：写块失败时回滚来源行，避免留下 0-chunk 孤儿（对齐 distill 的模式）
        try:
            await store.kb_delete_source(source_id)
        except Exception:
            logger.exception("ingest: rollback of source %s failed", source_id)
        raise
    logger.info(
        "ingest: source=%s kind=%s chunks=%d total=%d dropped=%d",
        source_id, kind, written, len(all_chunks), dropped,
    )
    return {
        "source_id": source_id,
        "chunks": written,
        "chunks_total": len(all_chunks),
        "dropped": dropped,
        "sha256": digest,
        "truncated": truncated,
    }


async def ingest_file(
    store,
    embedding,
    path: str | Path,
    *,
    name: str | None = None,
    kind: str = "file",
    max_chars: int = 600,
    scrub: bool = True,
    max_chunks: int | None = None,
) -> dict:
    """摄取一个本地文本文件（限工作区内路径由调用方保证）。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    if p.stat().st_size > MAX_INGEST_BYTES:
        raise ValueError(f"文件过大（>{MAX_INGEST_BYTES // 1024}KB）")
    text = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
    return await ingest_text(
        store,
        embedding,
        text,
        name=name or p.name,
        kind=kind,
        location=str(p),
        max_chars=max_chars,
        scrub=scrub,
        max_chunks=max_chunks,
    )
