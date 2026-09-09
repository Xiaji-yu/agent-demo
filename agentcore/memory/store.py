import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

DDL_TEMPLATE = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    group_id TEXT,
    scope TEXT NOT NULL DEFAULT 'private',
    policy TEXT NOT NULL DEFAULT 'per_user',
    summary TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls JSONB,
    tool_call_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS facts (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding vector({dim}),
    source TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS kb_sources (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    location TEXT NOT NULL,
    meta JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES kb_sources(id),
    chunk TEXT NOT NULL,
    embedding vector({dim}),
    chunk_idx INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS schedules (
    id SERIAL PRIMARY KEY,
    cron TEXT NOT NULL,
    action TEXT NOT NULL,
    params JSONB,
    target TEXT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tool_call_id TEXT;
"""

_VECTOR_RE = re.compile(r"^vector\((\d+)\)$")


async def _ensure_vector_dim(conn, table: str, dim: int) -> None:
    """若已存在的 vector 列维度与期望不一致，清空该表并迁移列类型。"""
    row = await conn.fetchrow(
        "SELECT format_type(atttypid, atttypmod) AS t "
        "FROM pg_attribute WHERE attrelid=$1::regclass AND attname='embedding'",
        table,
    )
    if not row:
        return  # 无 embedding 列（表刚建或不存在）
    m = _VECTOR_RE.match(row["t"] or "")
    if m and int(m.group(1)) == dim:
        return
    logger.warning("migrating %s.embedding to vector(%s), old=%s", table, dim, row["t"])
    await conn.execute(f"TRUNCATE TABLE {table}")
    await conn.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({dim})")


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _fmt_vector(embedding: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


class BaseMemoryStore(ABC):
    @abstractmethod
    async def init(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_history(self, session_id: str, limit: int = 20) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls=None,
        tool_call_id: str | None = None,
    ):
        raise NotImplementedError

    @abstractmethod
    async def resolve_session(self, user_id: str, group_id: Optional[str]) -> str:
        raise NotImplementedError

    # ---------- M4 长期记忆（facts） ----------
    @abstractmethod
    async def save_fact(
        self,
        user_id: str,
        content: str,
        embedding: list[float],
        source: str = "",
        session_id: str | None = None,
    ) -> bool:
        """保存一条长期事实；若内容已存在则跳过并返回 False，新增返回 True。"""
        raise NotImplementedError

    @abstractmethod
    async def recall_facts(
        self,
        user_id: str,
        query_embedding: list[float],
        top_k: int = 5,
        threshold: float = 0.0,
    ) -> list[dict]:
        """按向量相似度召回与 query 相关的事实。返回 [{"content","score","source"}]。"""
        raise NotImplementedError

    @abstractmethod
    async def list_facts(self, user_id: str, limit: int = 100) -> list[str]:
        """列出该用户已保存的全部事实内容。"""
        raise NotImplementedError


class InMemoryMemoryStore(BaseMemoryStore):
    """M0 可用：无需数据库，进程内存储。"""

    def __init__(self):
        self.sessions: dict[str, str] = {}
        self.messages: dict[str, list[dict]] = {}
        self.facts: dict[str, list[dict]] = {}
        self._next_id = 1

    async def init(self) -> None:
        pass

    async def get_history(self, session_id: str, limit: int = 20) -> list[dict]:
        return list(self.messages.get(session_id, []))[-limit:]

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls=None,
        tool_call_id: str | None = None,
    ):
        if session_id not in self.messages:
            self.messages[session_id] = []
        self.messages[session_id].append(
            {
                "role": role,
                "content": content,
                "tool_calls": tool_calls,
                "tool_call_id": tool_call_id,
            }
        )

    async def resolve_session(self, user_id: str, group_id: Optional[str]) -> str:
        key = f"{user_id}:{group_id or 'private'}"
        if key not in self.sessions:
            self.sessions[key] = str(self._next_id)
            self._next_id += 1
        return self.sessions[key]

    # ---------- M4 长期记忆（内存实现） ----------
    async def save_fact(
        self,
        user_id: str,
        content: str,
        embedding: list[float],
        source: str = "",
        session_id: str | None = None,
    ) -> bool:
        facts = self.facts.setdefault(user_id, [])
        for f in facts:
            if f["content"] == content:
                return False
        facts.append(
            {
                "content": content,
                "embedding": list(embedding),
                "source": source,
                "session_id": session_id,
            }
        )
        return True

    async def recall_facts(
        self,
        user_id: str,
        query_embedding: list[float],
        top_k: int = 5,
        threshold: float = 0.0,
    ) -> list[dict]:
        facts = self.facts.get(user_id, [])
        scored = []
        for f in facts:
            sim = _cosine_sim(query_embedding, f["embedding"])
            if sim >= threshold:
                scored.append(
                    {"content": f["content"], "score": sim, "source": f.get("source", "")}
                )
        scored.sort(key=lambda it: it["score"], reverse=True)
        return scored[:top_k]

    async def list_facts(self, user_id: str, limit: int = 100) -> list[str]:
        return [f["content"] for f in self.facts.get(user_id, [])][:limit]


class PgMemoryStore(BaseMemoryStore):
    """M1+：PostgreSQL + pgvector 持久化。"""

    def __init__(self, db_url: str, dim: int = 2048):
        self.db_url = db_url
        self.pool = None
        self.dim = int(dim or 2048)

    async def init(self) -> None:
        self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=5)
        async with self.pool.acquire() as conn:
            await conn.execute(DDL_TEMPLATE.format(dim=self.dim))
            for table in ("facts", "kb_chunks"):
                await _ensure_vector_dim(conn, table, self.dim)

    async def resolve_session(self, user_id: str, group_id: Optional[str]) -> str:
        scope = "group" if group_id else "private"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM sessions WHERE user_id=$1 AND group_id=$2 AND scope=$3",
                user_id,
                group_id,
                scope,
            )
            if row:
                return str(row["id"])
            return str(
                await conn.fetchval(
                    "INSERT INTO sessions(user_id, group_id, scope) VALUES($1,$2,$3) RETURNING id",
                    user_id,
                    group_id,
                    scope,
                )
            )

    async def get_history(self, session_id: str, limit: int = 20) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content, tool_calls, tool_call_id FROM messages WHERE session_id=$1 ORDER BY id ASC LIMIT $2",
                int(session_id),
                limit,
            )
            result = []
            for r in rows:
                item = {"role": r["role"], "content": r["content"]}
                if r["tool_calls"]:
                    item["tool_calls"] = r["tool_calls"]
                if r["tool_call_id"]:
                    item["tool_call_id"] = r["tool_call_id"]
                result.append(item)
            return result

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls=None,
        tool_call_id: str | None = None,
    ):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages(session_id, role, content, tool_calls, tool_call_id) VALUES($1,$2,$3,$4,$5)",
                int(session_id),
                role,
                content,
                json.dumps(tool_calls) if tool_calls is not None else None,
                tool_call_id,
            )

    # ---------- M4 长期记忆（pgvector 实现） ----------
    async def save_fact(
        self,
        user_id: str,
        content: str,
        embedding: list[float],
        source: str = "",
        session_id: str | None = None,
    ) -> bool:
        async with self.pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM facts WHERE user_id=$1 AND content=$2 LIMIT 1",
                user_id,
                content,
            )
            if exists:
                return False
            await conn.execute(
                "INSERT INTO facts(user_id, session_id, content, embedding, source) VALUES($1,$2,$3,$4::vector,$5)",
                user_id,
                int(session_id) if session_id else None,
                content,
                _fmt_vector(embedding),
                source or "",
            )
            return True

    async def recall_facts(
        self,
        user_id: str,
        query_embedding: list[float],
        top_k: int = 5,
        threshold: float = 0.0,
    ) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content, source, 1 - (embedding <=> $2::vector) AS score "
                "FROM facts WHERE user_id=$1 "
                "ORDER BY embedding <=> $2::vector LIMIT $3",
                user_id,
                _fmt_vector(query_embedding),
                int(top_k),
            )
        result = []
        for r in rows:
            score = float(r["score"]) if r["score"] is not None else 0.0
            if score >= threshold:
                result.append({"content": r["content"], "score": score, "source": r["source"] or ""})
        return result

    async def list_facts(self, user_id: str, limit: int = 100) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content FROM facts WHERE user_id=$1 ORDER BY id DESC LIMIT $2",
                user_id,
                int(limit),
            )
        return [r["content"] for r in rows]

    async def aclose(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None
