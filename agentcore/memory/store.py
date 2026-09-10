import json
import logging
import os
import re
from abc import ABC, abstractmethod

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
CREATE TABLE IF NOT EXISTS user_state (
    user_id TEXT PRIMARY KEY,
    persona TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tool_call_id TEXT;
-- P0-2：常用查询路径的索引（避免全表扫描/全表距离计算）
CREATE INDEX IF NOT EXISTS messages_session_id_idx ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS facts_user_id_idx ON facts(user_id);
CREATE INDEX IF NOT EXISTS kb_chunks_source_id_idx ON kb_chunks(source_id);
-- P0-3：sessions 的会话唯一键（private 会话 group_id 为 NULL，用 COALESCE 规避
-- 唯一索引对 NULL 不去重的语义）；resolve_session 依赖它 + ON CONFLICT 防并发竞态
CREATE UNIQUE INDEX IF NOT EXISTS sessions_user_scope_key ON sessions(user_id, COALESCE(group_id, ''), scope);
"""

# hnsw 索引单独建（老版本 pgvector 可能不支持 hnsw，失败降级为仅 btree，不阻塞启动）
_HNSW_INDEXES = [
    "CREATE INDEX IF NOT EXISTS facts_embedding_idx ON facts USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx ON kb_chunks USING hnsw (embedding vector_cosine_ops)",
]

_VECTOR_RE = re.compile(r"^vector\((\d+)\)$")


def _vector_dim_of(col_type: str | None) -> int | None:
    """从 pg format_type 文本（如 vector(1024)）解析维度；无法解析返回 None。"""
    if not col_type:
        return None
    m = _VECTOR_RE.match(col_type)
    return int(m.group(1)) if m else None


def _vector_migration_enabled() -> bool:
    """破坏性迁移需要显式开启：AGENT_MIGRATE_VECTOR=1。默认关闭，只告警。"""
    return (os.getenv("AGENT_MIGRATE_VECTOR") or "0").strip().lower() in {"1", "true", "yes", "on"}


async def _ensure_vector_dim(conn, table: str, dim: int) -> None:
    """若已存在的 vector 列维度与期望不一致：开启 AGENT_MIGRATE_VECTOR=1 时清空该表并改列类型（有数据丢失风险）。"""
    row = await conn.fetchrow(
        "SELECT format_type(atttypid, atttypmod) AS t "
        "FROM pg_attribute WHERE attrelid=$1::regclass AND attname='embedding'",
        table,
    )
    if not row:
        return  # 无 embedding 列（表刚建或不存在）
    cur = _vector_dim_of(row["t"] or "")
    if cur is None or cur == dim:
        return
    if not _vector_migration_enabled():
        logger.error(
            "embedding dim mismatch: %s.embedding is vector(%s) but runtime wants vector(%s). "
            "Destructive migration skipped. facts/kb_chunks writes will fail until dims align. "
            "To allow TRUNCATE+ALTER (DATA LOSS), set AGENT_MIGRATE_VECTOR=1.",
            table, cur, dim,
        )
        return
    logger.warning(
        "DESTRUCTIVE migration %s.embedding vector(%s)->vector(%s): TRUNCATE + ALTER (data loss). "
        "Set AGENT_MIGRATE_VECTOR=0 to disable.",
        table, cur, dim,
    )
    await conn.execute(f"TRUNCATE TABLE {table}")
    await conn.execute(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({dim})")


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _fmt_vector(embedding: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


def _session_scope_sql(session_id: str | None, first_index: int = 4) -> tuple[str, list]:
    """facts 的会话作用域 SQL 片段与参数。

    - session_id 为 None：不追加条件（跨全部会话，供管理/工具类调用）
    - session_id 给定：追加 ``AND session_id=$N``，把召回/列举限制在该会话内
      （= 该用户在该群或私聊的对话），避免不同群聊的长期记忆互相串味
    - 给定但无法解析为整数：fail-closed 返回 ``AND FALSE``（宁可少记，不可串味）
    """
    if session_id is None:
        return "", []
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        logger.warning("facts scope: unparseable session_id=%r, returning no facts", session_id)
        return "AND FALSE ", []
    return f"AND session_id=${first_index} ", [sid]


def _deserialize_tool_calls(value):
    """asyncpg 读 JSONB 返回文本，需反序列化为数组；已是 list 则原样返回。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            logger.warning("tool_calls JSONB deserialize failed, dropped")
            return None
    return value


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
    async def resolve_session(self, user_id: str, group_id: str | None) -> str:
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
        session_id: str | None = None,
    ) -> list[dict]:
        """按向量相似度召回与 query 相关的事实。返回 [{"content","score","source"}]。

        session_id 给定时只在该会话（= 该用户在该群/私聊的对话）范围内召回，
        避免不同群聊之间的记忆互相串味；None 表示不限会话。
        """
        raise NotImplementedError

    @abstractmethod
    async def list_facts(
        self, user_id: str, limit: int = 100, session_id: str | None = None
    ) -> list[str]:
        """列出该用户的事实内容（session_id 给定时限定在该会话内）。"""
        raise NotImplementedError

    # ---------- 用户级偏好状态（人格选择等） ----------
    @abstractmethod
    async def set_user_persona(self, user_id: str, persona_name: str | None) -> None:
        """记录该用户当前选择的人格；None 表示恢复默认。"""
        raise NotImplementedError

    @abstractmethod
    async def get_user_persona(self, user_id: str) -> str | None:
        """读取该用户当前选择的人格名；未设置返回 None。"""
        raise NotImplementedError


class InMemoryMemoryStore(BaseMemoryStore):
    """M0 可用：无需数据库，进程内存储。"""

    def __init__(self):
        self.sessions: dict[str, str] = {}
        self.messages: dict[str, list[dict]] = {}
        self.facts: dict[str, list[dict]] = {}
        self.user_personas: dict[str, str | None] = {}
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

    async def resolve_session(self, user_id: str, group_id: str | None) -> str:
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
        """保存事实。同一会话内内容重复则跳过；不同会话可各存一份（作用域隔离）。"""
        facts = self.facts.setdefault(user_id, [])
        scope = str(session_id or "")
        for f in facts:
            if f["content"] == content and str(f.get("session_id") or "") == scope:
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
        session_id: str | None = None,
    ) -> list[dict]:
        facts = self._facts_in_scope(user_id, session_id)
        scored = []
        for f in facts:
            sim = _cosine_sim(query_embedding, f["embedding"])
            if sim >= threshold:
                scored.append(
                    {"content": f["content"], "score": sim, "source": f.get("source", "")}
                )
        scored.sort(key=lambda it: it["score"], reverse=True)
        return scored[:top_k]

    async def list_facts(
        self, user_id: str, limit: int = 100, session_id: str | None = None
    ) -> list[str]:
        return [f["content"] for f in self._facts_in_scope(user_id, session_id)][:limit]

    def _facts_in_scope(self, user_id: str, session_id: str | None) -> list[dict]:
        """会话作用域过滤：session_id 给定时只取该会话内的事实。"""
        facts = self.facts.get(user_id, [])
        if session_id is None:
            return list(facts)
        return [f for f in facts if str(f.get("session_id") or "") == str(session_id)]

    # ---------- 用户级偏好（内存实现） ----------
    async def set_user_persona(self, user_id: str, persona_name: str | None) -> None:
        self.user_personas[user_id] = persona_name or None

    async def get_user_persona(self, user_id: str) -> str | None:
        return self.user_personas.get(user_id)


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
            for idx_sql in _HNSW_INDEXES:
                try:
                    await conn.execute(idx_sql)
                except Exception:
                    logger.warning(
                        "hnsw index creation failed (pgvector too old?), fallback to btree only: %s",
                        idx_sql[:60],
                    )

    async def resolve_session(self, user_id: str, group_id: str | None) -> str:
        scope = "group" if group_id else "private"
        async with self.pool.acquire() as conn:
            row = await self._find_session(conn, user_id, group_id, scope)
            if row:
                return str(row["id"])
            # P0-3：并发下两行可能同时 INSERT；靠唯一索引 + ON CONFLICT DO NOTHING
            # 保证只落一行，随后重新 SELECT 拿到已存在的 id
            await conn.execute(
                "INSERT INTO sessions(user_id, group_id, scope) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                user_id,
                group_id,
                scope,
            )
            row = await self._find_session(conn, user_id, group_id, scope)
            if row:
                return str(row["id"])
            raise RuntimeError("resolve_session: session insert succeeded but lookup failed")

    @staticmethod
    async def _find_session(conn, user_id: str, group_id: str | None, scope: str):
        """NULL 安全地按 (user_id, group_id, scope) 查会话（group_id = NULL 用 IS NULL）。"""
        return await conn.fetchrow(
            "SELECT id FROM sessions "
            "WHERE user_id=$1 AND ((group_id IS NULL AND $2::text IS NULL) OR group_id=$2) AND scope=$3",
            user_id,
            group_id,
            scope,
        )

    async def get_history(self, session_id: str, limit: int = 20) -> list[dict]:
        async with self.pool.acquire() as conn:
            # P0-1：取「最近的 limit 条」再正序返回（与内存实现 [-limit:] 语义一致）
            rows = await conn.fetch(
                "SELECT role, content, tool_calls, tool_call_id FROM messages "
                "WHERE session_id=$1 ORDER BY id DESC LIMIT $2",
                int(session_id),
                limit,
            )
            result = []
            for r in reversed(rows):
                item = {"role": r["role"], "content": r["content"]}
                tc = _deserialize_tool_calls(r["tool_calls"])
                if tc:
                    item["tool_calls"] = tc
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
                # 去重按会话作用域：同一句话在不同群聊可各存一份
                "SELECT 1 FROM facts WHERE user_id=$1 AND content=$2 "
                "AND session_id IS NOT DISTINCT FROM $3::int LIMIT 1",
                user_id,
                content,
                int(session_id) if session_id else None,
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
        session_id: str | None = None,
    ) -> list[dict]:
        # session_id 给定 → 只在本会话（该用户在该群/私聊的对话）范围内召回，
        # 防止不同群聊的长期记忆互相串味
        scope_sql, params = _session_scope_sql(session_id)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content, source, 1 - (embedding <=> $2::vector) AS score "
                f"FROM facts WHERE user_id=$1 {scope_sql}"
                "ORDER BY embedding <=> $2::vector LIMIT $3",
                user_id,
                _fmt_vector(query_embedding),
                int(top_k),
                *params,
            )
        result = []
        for r in rows:
            score = float(r["score"]) if r["score"] is not None else 0.0
            if score >= threshold:
                result.append({"content": r["content"], "score": score, "source": r["source"] or ""})
        return result

    async def list_facts(
        self, user_id: str, limit: int = 100, session_id: str | None = None
    ) -> list[str]:
        scope_sql, params = _session_scope_sql(session_id, first_index=3)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT content FROM facts WHERE user_id=$1 {scope_sql}"
                "ORDER BY id DESC LIMIT $2",
                user_id,
                int(limit),
                *params,
            )
        return [r["content"] for r in rows]

    # ---------- 用户级偏好（PG 实现） ----------
    async def set_user_persona(self, user_id: str, persona_name: str | None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_state(user_id, persona) VALUES($1,$2) "
                "ON CONFLICT (user_id) DO UPDATE SET persona=$2, updated_at=NOW()",
                user_id,
                persona_name,
            )

    async def get_user_persona(self, user_id: str) -> str | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT persona FROM user_state WHERE user_id=$1",
                user_id,
            )

    async def aclose(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None
