import json
import logging
import os
import re
import time
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
-- 注意：sessions 的唯一索引不在这里建——旧库可能存在重复行（历史 bug 遗留），
-- 直接建会让整个 init 抛错、机器人无法启动。改由 init() 先合并重复会话再建（见
-- _dedupe_sessions / _ensure_session_unique_index）。
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


# 重复会话合并用：每组 (user_id, COALESCE(group_id,''), scope) 保留最早的一行
_RANKED_SESSIONS_CTE = (
    "WITH ranked AS ("
    " SELECT id, MIN(id) OVER (PARTITION BY user_id, COALESCE(group_id, ''), scope) AS keep_id"
    " FROM sessions"
    ") "
)


async def _dedupe_sessions(conn) -> int:
    """合并重复会话，返回删除的行数。

    历史 bug 遗留：私聊的 `group_id` 为 NULL，而旧 resolve_session 用 `group_id = $2`
    比较（NULL 恒不成立）→ 每条私聊消息都新建一行 session，历史被劈成 N 份、每份 1 条。
    这里把 messages/facts 的 session_id 指向每组最早的那一行，再删除多余行：
    既修复被劈开的历史，也让唯一索引能够建立。
    """
    dup = await conn.fetchval(
        "SELECT COALESCE(SUM(n - 1), 0) FROM ("
        "  SELECT count(*) AS n FROM sessions GROUP BY user_id, group_id, scope"
        ") t"
    )
    dup = int(dup or 0)
    if dup <= 0:
        return 0
    await conn.execute(
        _RANKED_SESSIONS_CTE
        + "UPDATE messages m SET session_id = r.keep_id FROM ranked r "
        "WHERE m.session_id = r.id AND r.id <> r.keep_id"
    )
    await conn.execute(
        _RANKED_SESSIONS_CTE
        + "UPDATE facts f SET session_id = r.keep_id FROM ranked r "
        "WHERE f.session_id = r.id AND r.id <> r.keep_id"
    )
    await conn.execute(
        _RANKED_SESSIONS_CTE
        + "DELETE FROM sessions s USING ranked r WHERE s.id = r.id AND r.id <> r.keep_id"
    )
    logger.warning(
        "sessions: merged %s duplicate row(s) into their earliest session "
        "(legacy NULL-group_id bug); messages/facts repointed",
        dup,
    )
    return dup


async def _ensure_session_unique_index(conn) -> None:
    """建 sessions 唯一索引（并发保护）。失败只告警，不阻塞机器人启动。"""
    try:
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS sessions_user_scope_key "
            "ON sessions(user_id, COALESCE(group_id, ''), scope)"
        )
    except Exception:
        logger.error(
            "failed to create sessions unique index: resolve_session still works but "
            "without concurrency protection (duplicate session rows present?)",
            exc_info=True,
        )


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


def _as_json_dict(value) -> dict:
    """JSONB 字段宽容解析为 dict（asyncpg 默认返回文本）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
            return out if isinstance(out, dict) else {}
        except Exception:
            logger.warning("JSONB dict deserialize failed, treated as empty")
    return {}


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

    # ---------- M5 公共知识库（KB，全局、已脱敏） ----------
    @abstractmethod
    async def kb_add_source(
        self, name: str, kind: str, location: str = "", meta: dict | None = None
    ) -> str:
        """新建一个知识来源，返回其 id。kind 例如 manual/distill/file。"""
        raise NotImplementedError

    @abstractmethod
    async def kb_add_chunks(
        self, source_id: str, chunks: list[str], embeddings: list[list[float]]
    ) -> int:
        """写入切块与向量；返回写入条数。"""
        raise NotImplementedError

    @abstractmethod
    async def kb_search(
        self, query_embedding: list[float], top_k: int = 4, threshold: float = 0.0
    ) -> list[dict]:
        """按向量相似度检索知识块。返回 [{"chunk","score","source_id","source_name","kind"}]。"""
        raise NotImplementedError

    @abstractmethod
    async def kb_list_sources(self, limit: int = 50) -> list[dict]:
        """列出知识来源（按时间倒序）：[{id,name,kind,location,created_at,chunks,meta}]。"""
        raise NotImplementedError

    @abstractmethod
    async def kb_delete_source(self, source_id: str) -> int:
        """删除一个来源及其全部知识块，返回删除的块数。"""
        raise NotImplementedError

    @abstractmethod
    async def kb_stats(self) -> dict:
        """知识库规模统计：{sources, chunks}。"""
        raise NotImplementedError

    # ---------- 蒸馏用：消息水位线 ----------
    @abstractmethod
    async def latest_message_id(self) -> int:
        """当前最大消息 id（蒸馏水位线的起点）。"""
        raise NotImplementedError

    @abstractmethod
    async def messages_after(self, after_id: int, limit: int = 200) -> list[dict]:
        """取 id 大于 after_id 的消息（正序），用于增量蒸馏。

        返回 [{"id","session_id","role","content"}]；不返回用户/群标识，避免把身份
        信息带进蒸馏输入。
        """
        raise NotImplementedError

    @abstractmethod
    async def kb_last_digest_watermark(self) -> int:
        """上一次成功蒸馏处理到的消息 id；从未蒸馏过返回 0。"""
        raise NotImplementedError


class InMemoryMemoryStore(BaseMemoryStore):
    """M0 可用：无需数据库，进程内存储。"""

    def __init__(self):
        self.sessions: dict[str, str] = {}
        self.messages: dict[str, list[dict]] = {}
        self.facts: dict[str, list[dict]] = {}
        self.user_personas: dict[str, str | None] = {}
        self.kb_sources: dict[str, dict] = {}
        self.kb_chunks: list[dict] = []
        self._next_id = 1
        self._next_msg_id = 1
        self._next_kb_id = 1

    async def init(self) -> None:
        pass

    async def get_history(self, session_id: str, limit: int = 20) -> list[dict]:
        # 只投影模型需要的字段（内部 id 不能出现在发给 LLM 的消息里）
        out = []
        for m in list(self.messages.get(session_id, []))[-limit:]:
            item = {"role": m["role"], "content": m["content"]}
            if m.get("tool_calls"):
                item["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                item["tool_call_id"] = m["tool_call_id"]
            out.append(item)
        return out

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
                "id": self._next_msg_id,
                "role": role,
                "content": content,
                "tool_calls": tool_calls,
                "tool_call_id": tool_call_id,
            }
        )
        self._next_msg_id += 1

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

    # ---------- M5 公共知识库（内存实现） ----------
    async def kb_add_source(
        self, name: str, kind: str, location: str = "", meta: dict | None = None
    ) -> str:
        sid = str(self._next_kb_id)
        self._next_kb_id += 1
        self.kb_sources[sid] = {
            "id": sid,
            "name": name,
            "kind": kind,
            "location": location,
            "meta": dict(meta or {}),
            "created_at": time.time(),
        }
        return sid

    async def kb_add_chunks(
        self, source_id: str, chunks: list[str], embeddings: list[list[float]]
    ) -> int:
        n = 0
        for idx, chunk in enumerate(chunks):
            emb = embeddings[idx] if idx < len(embeddings) else []
            self.kb_chunks.append(
                {
                    "source_id": source_id,
                    "chunk": chunk,
                    "embedding": list(emb),
                    "chunk_idx": idx,
                }
            )
            n += 1
        return n

    async def kb_search(
        self, query_embedding: list[float], top_k: int = 4, threshold: float = 0.0
    ) -> list[dict]:
        scored = []
        for c in self.kb_chunks:
            src = self.kb_sources.get(c["source_id"])
            if src is None:
                continue
            sim = _cosine_sim(query_embedding, c["embedding"])
            if sim >= threshold:
                scored.append(
                    {
                        "chunk": c["chunk"],
                        "score": sim,
                        "source_id": c["source_id"],
                        "source_name": src["name"],
                        "kind": src["kind"],
                    }
                )
        scored.sort(key=lambda it: it["score"], reverse=True)
        return scored[:top_k]

    async def kb_list_sources(self, limit: int = 50) -> list[dict]:
        srcs = sorted(self.kb_sources.values(), key=lambda s: s["created_at"], reverse=True)
        out = []
        for s in srcs[:limit]:
            out.append(
                {
                    **s,
                    "chunks": sum(1 for c in self.kb_chunks if c["source_id"] == s["id"]),
                }
            )
        return out

    async def kb_delete_source(self, source_id: str) -> int:
        before = len(self.kb_chunks)
        self.kb_chunks = [c for c in self.kb_chunks if c["source_id"] != str(source_id)]
        self.kb_sources.pop(str(source_id), None)
        return before - len(self.kb_chunks)

    async def kb_stats(self) -> dict:
        return {"sources": len(self.kb_sources), "chunks": len(self.kb_chunks)}

    async def latest_message_id(self) -> int:
        return self._next_msg_id - 1

    async def messages_after(self, after_id: int, limit: int = 200) -> list[dict]:
        rows = [
            {"id": m["id"], "session_id": sid, "role": m["role"], "content": m["content"]}
            for sid, msgs in self.messages.items()
            for m in msgs
            if m["id"] > after_id
        ]
        rows.sort(key=lambda r: r["id"])
        return rows[:limit]

    async def kb_last_digest_watermark(self) -> int:
        watermarks = [
            int(s["meta"].get("last_message_id") or 0)
            for s in self.kb_sources.values()
            if s["kind"] == "distill"
        ]
        return max(watermarks) if watermarks else 0


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
            # P0-3：先合并历史重复会话，再建唯一索引——旧库直接建索引会因重复行失败，
            # 那样整个 init 抛错、机器人起不来（升级路径必须容错）
            try:
                await _dedupe_sessions(conn)
            except Exception:
                logger.exception("sessions dedupe failed; will try creating the index anyway")
            await _ensure_session_unique_index(conn)
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

    # ---------- M5 公共知识库（pgvector 实现） ----------
    async def kb_add_source(
        self, name: str, kind: str, location: str = "", meta: dict | None = None
    ) -> str:
        async with self.pool.acquire() as conn:
            sid = await conn.fetchval(
                "INSERT INTO kb_sources(name, kind, location, meta) VALUES($1,$2,$3,$4::jsonb) RETURNING id",
                name,
                kind,
                location or "",
                json.dumps(meta or {}),
            )
        return str(sid)

    async def kb_add_chunks(
        self, source_id: str, chunks: list[str], embeddings: list[list[float]]
    ) -> int:
        rows = [
            (
                int(source_id),
                chunk,
                _fmt_vector(embeddings[idx] if idx < len(embeddings) else []),
                idx,
            )
            for idx, chunk in enumerate(chunks)
        ]
        if not rows:
            return 0
        async with self.pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO kb_chunks(source_id, chunk, embedding, chunk_idx) "
                "VALUES($1,$2,$3::vector,$4)",
                rows,
            )
        return len(rows)

    async def kb_search(
        self, query_embedding: list[float], top_k: int = 4, threshold: float = 0.0
    ) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.chunk, c.source_id, s.name AS source_name, s.kind, "
                "       1 - (c.embedding <=> $1::vector) AS score "
                "FROM kb_chunks c JOIN kb_sources s ON s.id = c.source_id "
                "ORDER BY c.embedding <=> $1::vector LIMIT $2",
                _fmt_vector(query_embedding),
                int(top_k),
            )
        out = []
        for r in rows:
            score = float(r["score"]) if r["score"] is not None else 0.0
            if score >= threshold:
                out.append(
                    {
                        "chunk": r["chunk"],
                        "score": score,
                        "source_id": str(r["source_id"]),
                        "source_name": r["source_name"],
                        "kind": r["kind"],
                    }
                )
        return out

    async def kb_list_sources(self, limit: int = 50) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT s.id, s.name, s.kind, s.location, s.meta, s.created_at, "
                "       (SELECT count(*) FROM kb_chunks c WHERE c.source_id = s.id) AS chunks "
                "FROM kb_sources s ORDER BY s.id DESC LIMIT $1",
                int(limit),
            )
        return [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "kind": r["kind"],
                "location": r["location"],
                "meta": _as_json_dict(r["meta"]),
                "created_at": r["created_at"].timestamp() if r["created_at"] else 0.0,
                "chunks": int(r["chunks"] or 0),
            }
            for r in rows
        ]

    async def kb_delete_source(self, source_id: str) -> int:
        async with self.pool.acquire() as conn:
            deleted = await conn.fetchval(
                "WITH d AS (DELETE FROM kb_chunks WHERE source_id=$1::int RETURNING 1) "
                "SELECT count(*) FROM d",
                int(source_id),
            )
            await conn.execute("DELETE FROM kb_sources WHERE id=$1::int", int(source_id))
        return int(deleted or 0)

    async def kb_stats(self) -> dict:
        async with self.pool.acquire() as conn:
            sources = await conn.fetchval("SELECT count(*) FROM kb_sources")
            chunks = await conn.fetchval("SELECT count(*) FROM kb_chunks")
        return {"sources": int(sources or 0), "chunks": int(chunks or 0)}

    async def latest_message_id(self) -> int:
        async with self.pool.acquire() as conn:
            val = await conn.fetchval("SELECT COALESCE(MAX(id), 0) FROM messages")
        return int(val or 0)

    async def messages_after(self, after_id: int, limit: int = 200) -> list[dict]:
        # 不 join sessions/sessions.user_id：蒸馏输入里不应带上身份信息
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, session_id, role, content FROM messages "
                "WHERE id > $1 ORDER BY id ASC LIMIT $2",
                int(after_id),
                int(limit),
            )
        return [
            {
                "id": int(r["id"]),
                "session_id": str(r["session_id"]),
                "role": r["role"],
                "content": r["content"],
            }
            for r in rows
        ]

    async def kb_last_digest_watermark(self) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT meta FROM kb_sources WHERE kind='distill' ORDER BY id DESC LIMIT 1"
            )
        if not row:
            return 0
        return int(_as_json_dict(row["meta"]).get("last_message_id") or 0)

    async def aclose(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None
