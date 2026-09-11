"""知识库服务门面：把 store / embedding / 配置组装成 engine、admin、scheduler 用的接口。

配置来自 config.yaml 的 `rag:` 段（可被环境变量覆盖）：
    rag:
      enabled: true
      top_k: 4              # 每次注入多少条检索结果
      threshold: 0.3        # 相似度下限
      chunk_chars: 600      # 切块目标长度
      digest_cron: "0 3 * * *"  # 每天蒸馏时间（5 段 cron）
      digest_batch: 200     # 每次最多处理多少条新消息
      max_entries: 8        # 每次蒸馏最多沉淀多少条知识
      min_chars: 200        # 新内容少于该长度则跳过本次蒸馏
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from agentcore.budget import get_budget
from agentcore.rag.distill import distill_from_memory, summarize
from agentcore.rag.ingest import ingest_file, ingest_text
from agentcore.rag.retriever import format_block, retrieve

logger = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": True,
    "top_k": 4,
    "threshold": 0.3,
    "chunk_chars": 600,
    "digest_cron": "0 3 * * *",
    "digest_batch": 200,
    "max_entries": 8,
    "min_chars": 200,
    "distill_max_tokens": 2048,
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class KnowledgeBase:
    def __init__(self, store, embedding, config: dict | None = None, llm=None):
        cfg = dict(DEFAULTS)
        cfg.update(config or {})
        self.store = store
        self.embedding = embedding
        self.llm = llm
        self.enabled = _env_bool("AGENT_KB_ENABLED", bool(cfg["enabled"]))
        self.top_k = int(cfg["top_k"])
        self.threshold = float(cfg["threshold"])
        self.chunk_chars = int(cfg["chunk_chars"])
        self.digest_cron = os.getenv("AGENT_KB_DIGEST_CRON", str(cfg["digest_cron"]))
        self.digest_batch = int(cfg["digest_batch"])
        self.max_entries = int(cfg["max_entries"])
        self.min_chars = int(cfg["min_chars"])
        # 推理型模型会把预算耗在 reasoning 上 → 蒸馏需要更大的输出上限
        self.distill_max_tokens = int(cfg["distill_max_tokens"])
        # L10：手动 /kb digest 与 cron 可能同时触发，蒸馏全程持锁防双跑重复入库
        self._digest_lock = asyncio.Lock()

    # ---------- 检索（engine 用） ----------
    async def retrieve(self, query: str) -> list[dict]:
        if not self.enabled:
            return []
        return await retrieve(
            self.store, self.embedding, query, top_k=self.top_k, threshold=self.threshold
        )

    def format_block(self, hits: list[dict]) -> str:
        return format_block(hits)

    # ---------- 摄取（管理命令用） ----------
    async def add_text(self, text: str, name: str, kind: str = "manual") -> dict:
        return await ingest_text(
            self.store, self.embedding, text, name=name, kind=kind, max_chars=self.chunk_chars
        )

    async def add_file(self, path: str, name: str | None = None, kind: str = "file") -> dict:
        return await ingest_file(
            self.store, self.embedding, path, name=name, kind=kind, max_chars=self.chunk_chars
        )

    # ---------- 蒸馏（定时任务 / 手动触发） ----------
    async def digest(self) -> dict:
        if not self.enabled:
            return {"status": "skipped", "reason": "knowledge base disabled"}
        if self.llm is None:
            return {"status": "skipped", "reason": "llm unavailable"}
        # M7 成本预算：硬闸开启且当日超预算时跳过蒸馏（知识成长让位于预算）
        blocked, _ = get_budget().chat_blocked()
        if blocked:
            return {"status": "skipped", "reason": "daily budget exceeded"}
        async with self._digest_lock:
            try:
                result = await distill_from_memory(
                    self.llm,
                    self.store,
                    self.embedding,
                    batch=self.digest_batch,
                    max_entries=self.max_entries,
                    min_chars=self.min_chars,
                    max_tokens=self.distill_max_tokens,
                )
            except Exception:
                logger.exception("knowledge distillation failed")
                return {"status": "error", "reason": "exception (see logs)"}
            logger.info("kb digest: %s", summarize(result))
            return result

    # ---------- 管理 ----------
    async def list_sources(self, limit: int = 20) -> list[dict]:
        return await self.store.kb_list_sources(limit=limit)

    async def delete_source(self, source_id: str) -> int:
        return await self.store.kb_delete_source(source_id)

    async def stats(self) -> dict:
        return await self.store.kb_stats()

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "top_k": self.top_k,
            "threshold": self.threshold,
            "digest_cron": self.digest_cron,
            "embedding": "on" if self.embedding is not None else "off",
        }
