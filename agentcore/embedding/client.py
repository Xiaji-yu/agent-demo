"""Embedding 客户端：优先 OpenAI 兼容 /embeddings API（EMBEDDING_* 配置），
否则降级为本地确定性 hash embedding，保证功能可用且无外部依赖。"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re

import httpx

logger = logging.getLogger(__name__)

DEFAULT_DIM = 2048  # 与 facts 表 vector(2048) 一致


class EmbeddingClient:
    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        dim: int = DEFAULT_DIM,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or "text-embedding-3-small"
        self.dim = int(dim or DEFAULT_DIM)
        self._remote = bool(self.base_url and self.api_key)
        if self._remote:
            logger.info("Embedding: remote API %s model=%s", self.base_url, self.model)
        else:
            logger.info("Embedding: local fallback dim=%s (配置 EMBEDDING_BASE_URL/API_KEY/MODEL 启用语义向量)", self.dim)

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_many([text]))[0]

    async def probe_dim(self) -> int:
        """探测并设置实际向量维度（远程模型以真实输出为准，本地用配置 dim）。"""
        if self._remote:
            # 直接走底层调用：此时 self.dim 还是配置值，若经过 embed_many 会打出
            # 一条“模型维度与 runtime 不一致”的误导告警（其实只是尚未探测）
            vecs = await self._remote_embed(["ping"])
            if vecs:
                self.dim = len(vecs[0])
        return self.dim

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._remote:
            vecs = await self._remote_embed(texts)
            if vecs:
                real_dim = len(vecs[0])
                if real_dim != self.dim:
                    # DB 列维度在 init 时已固定，这里只告警不静默改维度，
                    # 避免运行期维度漂移导致 save_fact 全部失败。
                    logger.warning(
                        "embedding model returned dim=%s but runtime dim=%s; "
                        "re-run with matching config / AGENT_MIGRATE_VECTOR=1 if schema needs change",
                        real_dim,
                        self.dim,
                    )
            return vecs
        return [self._local_embed(t) for t in texts]

    async def _remote_embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data") or []
            ordered = sorted(items, key=lambda it: it.get("index", 0))
            return [list(it["embedding"]) for it in ordered]

    # ---------- 本地降级：字符/双字符 bag hashing ----------
    # 说明：无 EMBEDDING_API_KEY 时的兜底方案，仅近似「词面重叠」，
    # 不具备语义泛化能力；要真正的语义召回请配置 OpenAI 兼容 embedding 服务。
    def _local_embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        s = re.sub(r"\s+", "", text or "").lower()
        chars = list(s)
        if not chars:
            chars = ["<empty>"]
        for ch in chars:
            self._add_token(vec, ch)
        for i in range(len(chars) - 1):
            self._add_token(vec, chars[i] + chars[i + 1])
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    @staticmethod
    def _add_token(vec: list[float], token: str) -> None:
        h = int(hashlib.blake2b(token.encode("utf-8"), digest_size=8).hexdigest(), 16)
        idx = h % len(vec)
        sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
        vec[idx] += sign


def load_embedding_client_from_env() -> EmbeddingClient:
    return EmbeddingClient(
        base_url=os.getenv("EMBEDDING_BASE_URL", ""),
        api_key=os.getenv("EMBEDDING_API_KEY", ""),
        model=os.getenv("EMBEDDING_MODEL", ""),
        dim=int(os.getenv("EMBEDDING_DIM", str(DEFAULT_DIM))),
    )
