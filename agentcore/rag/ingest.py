"""知识摄取：把文本/文件切块、向量化后写入公共知识库。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agentcore.rag.chunker import chunk_text
from agentcore.rag.sanitize import scrub_pii

logger = logging.getLogger(__name__)

MAX_INGEST_BYTES = 2 * 1024 * 1024  # 单个文本/文件最多摄取 2MB
MAX_CHUNKS_PER_SOURCE = 200         # 单个来源最多切块数（防一次灌爆知识库）


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
) -> dict:
    """摄取一段文本。返回 {source_id, chunks}；文本为空返回 chunks=0。"""
    if embedding is None:
        raise RuntimeError("embedding client unavailable")
    body = (text or "").strip()
    if not body:
        return {"source_id": None, "chunks": 0}
    if len(body.encode("utf-8")) > MAX_INGEST_BYTES:
        body = body[: MAX_INGEST_BYTES // 4]
        logger.warning("ingest: text truncated to %d chars", len(body))
    if scrub:
        # 管理员投喂的内容同样过一遍 PII 掩码：公共库不存身份标识。
        # 大文本的 CPU 段放线程池，避免卡住事件循环（本地 embedding 服务场景）
        body = await asyncio.to_thread(scrub_pii, body)

    chunks = (
        await asyncio.to_thread(chunk_text, body, max_chars=max_chars)
    )[:MAX_CHUNKS_PER_SOURCE]
    if not chunks:
        return {"source_id": None, "chunks": 0}
    embeddings = await embedding.embed_many(chunks)
    source_id = await store.kb_add_source(
        name=name, kind=kind, location=location, meta={"chunks": len(chunks)}
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
    logger.info("ingest: source=%s kind=%s chunks=%d", source_id, kind, written)
    return {"source_id": source_id, "chunks": written}


async def ingest_file(
    store,
    embedding,
    path: str | Path,
    *,
    name: str | None = None,
    kind: str = "file",
    max_chars: int = 600,
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
    )
