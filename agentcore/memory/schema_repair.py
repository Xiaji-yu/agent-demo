"""Schema 的**修复**步骤（与版本化迁移分离，每次启动都可重跑）。

为什么不做成迁移：
- 合并历史重复 sessions 是数据修复，不是结构版本；上次失败后必须还能重试；
- vector 维度来自运行期 embedding 探测，不是固定的 schema 版本，且 TRUNCATE+ALTER
  会丢数据，必须由 ``AGENT_MIGRATE_VECTOR=1`` / CLI 显式放行；
- HNSW 索引依赖 pgvector 版本，建不出来只降级，不应阻塞 schema 基线。

顺序不能变：先合并重复会话，再建唯一索引；否则老库的重复行会让启动失败。
"""

from __future__ import annotations

import logging
import os
import re

from agentcore.memory.migrate import (
    LOCK_WAIT_SECONDS,
    MigrationError,
    _acquire_lock,
    _release_lock,
)

logger = logging.getLogger(__name__)

_VECTOR_RE = re.compile(r"^vector\((\d+)\)$")

# 维度迁移 SQL 用常量表而非拼接：表名只来自下方白名单键，维度经 int() 收敛后替换占位符
_VECTOR_DIM_MIGRATE_SQL = {
    "facts": (
        "TRUNCATE TABLE facts",
        "ALTER TABLE facts ALTER COLUMN embedding TYPE vector({dim})",
    ),
    "kb_chunks": (
        "TRUNCATE TABLE kb_chunks",
        "ALTER TABLE kb_chunks ALTER COLUMN embedding TYPE vector({dim})",
    ),
}

# hnsw 索引单独建（老版本 pgvector 可能不支持 hnsw，失败降级为仅 btree，不阻塞启动）
_HNSW_INDEXES = [
    "CREATE INDEX IF NOT EXISTS facts_embedding_idx ON facts USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx ON kb_chunks USING hnsw (embedding vector_cosine_ops)",
]

# 重复会话合并用：每组 (user_id, COALESCE(group_id,''), scope) 保留最早的一行
_RANKED_SESSIONS_CTE = (
    "WITH ranked AS ("
    " SELECT id, MIN(id) OVER (PARTITION BY user_id, COALESCE(group_id, ''), scope) AS keep_id"
    " FROM sessions"
    ") "
)


def _vector_dim_of(col_type: str | None) -> int | None:
    """从 pg format_type 文本（如 vector(1024)）解析维度；无法解析返回 None。"""
    if not col_type:
        return None
    m = _VECTOR_RE.match(col_type)
    return int(m.group(1)) if m else None


def _vector_migration_enabled() -> bool:
    """破坏性迁移需要显式开启：AGENT_MIGRATE_VECTOR=1。默认关闭，只告警。"""
    return (os.getenv("AGENT_MIGRATE_VECTOR") or "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _ensure_vector_dim(
    conn, table: str, dim: int, *, allow_destructive: bool | None = None
) -> bool:
    """维度不一致时按开关 TRUNCATE+ALTER；返回是否真的改过列类型。"""
    if allow_destructive is None:
        allow_destructive = _vector_migration_enabled()
    row = await conn.fetchrow(
        "SELECT format_type(atttypid, atttypmod) AS t "
        "FROM pg_attribute WHERE attrelid=$1::regclass AND attname='embedding'",
        table,
    )
    if not row:
        return False  # 无 embedding 列（表刚建或不存在）
    cur = _vector_dim_of(row["t"] or "")
    if cur is None or cur == dim:
        return False
    if not allow_destructive:
        logger.error(
            "embedding dim mismatch: %s.embedding is vector(%s) but runtime wants vector(%s). "
            "Destructive migration skipped. facts/kb_chunks writes will fail until dims align. "
            "To allow TRUNCATE+ALTER (DATA LOSS), set AGENT_MIGRATE_VECTOR=1 "
            "(or run scripts/migrate.py repair --allow-destructive).",
            table,
            cur,
            dim,
        )
        return False
    logger.warning(
        "DESTRUCTIVE migration %s.embedding vector(%s)->vector(%s): TRUNCATE + ALTER (data loss). "
        "Set AGENT_MIGRATE_VECTOR=0 to disable.",
        table,
        cur,
        dim,
    )
    trunc_sql, alter_sql = _VECTOR_DIM_MIGRATE_SQL[table]
    # M4：TRUNCATE 与 ALTER 必须同一事务 + lock_timeout——旧实现两条裸执行，
    # ALTER 失败时表已被清空而类型未变（显式授权下的数据丢失 + 半完成状态）。
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '30s'")
        await conn.execute(trunc_sql)
        await conn.execute(alter_sql.replace("{dim}", str(int(dim))))
    return True


async def _dedupe_sessions(conn) -> int:
    """合并重复会话，返回删除的行数。

    历史 bug 遗留：私聊的 `group_id` 为 NULL，而旧 resolve_session 用 `group_id = $2`
    比较（NULL 恒不成立）→ 每条私聊消息都新建一行 session，历史被劈成 N 份、每份 1 条。
    这里把 messages/facts 的 session_id 指向每组最早的那一行，再删除多余行：
    既修复被劈开的历史，也让唯一索引能够建立。

    L1：下面的「重复计数」与 _RANKED_SESSIONS_CTE 的 PARTITION 都必须与唯一索引
    （sessions_user_scope_key 按 COALESCE(group_id,'')）完全同口径——否则历史脏数据
    里 `group_id=NULL` 与 `''` 并存时，按原值分组计数判「无重复」→ 不合并 → 唯一索引
    建不起来 → P0-3 并发保护失效且每次启动报错不收敛。
    """
    # 先把私聊行的 group_id 统一成规范形态 NULL：COALESCE 口径下 '' 与 NULL 同组，
    # 若合并后存活的是 '' 行，_find_session 按 `group_id IS NULL` 查私聊会查不到
    # （resolve_session 二次插行又撞唯一索引）→ 必须在建索引/合并前归一
    await conn.execute(
        "UPDATE sessions SET group_id=NULL WHERE group_id='' AND scope='private'"
    )
    dup = await conn.fetchval(
        "SELECT COALESCE(SUM(n - 1), 0) FROM ("
        "  SELECT count(*) AS n FROM sessions GROUP BY user_id, COALESCE(group_id, ''), scope"
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


async def _ensure_session_unique_index(conn) -> bool:
    """建 sessions 唯一索引（并发保护）。失败只告警，不阻塞机器人启动。"""
    try:
        # lock_timeout：别让一次建索引无限期排在别人的长事务后面
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '30s'")
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS sessions_user_scope_key "
                "ON sessions(user_id, COALESCE(group_id, ''), scope)"
            )
        return True
    except Exception:
        logger.error(
            "failed to create sessions unique index: resolve_session still works but "
            "without concurrency protection (duplicate session rows present?)",
            exc_info=True,
        )
        return False


async def repair_schema(
    pool, *, dim: int, allow_destructive: bool | None = None
) -> dict:
    """跑全部修复步骤，返回摘要。dedupe/索引/HNSW 失败各有降级分支；vector 维度的
    SQL 报错说明 schema 不可用，统一抛 ``MigrationError`` 交给 init 的 fail-closed。

    M4：整个修复序列持有与迁移器同一把 advisory lock——多实例同时启动时，
    并发的 dedupe/DDL 会互相踩（唯一索引建失败、重复合并交叉 UPDATE）。
    拿不到锁说明另一个实例正在修复，本次直接跳过（结果里 ``skipped=True``）。

    H1（REVIEW-ee39c8e..e55cab5）：dedupe / 唯一索引 / HNSW 有各自的容错分支，
    其余执行期异常（vector 维度的 SQL、取锁失败等）统一包成 ``MigrationError``
    ——裸异常会被启动路径当成普通故障降级内存，掩盖 schema 实际不可用。
    """
    try:
        async with pool.acquire() as conn:
            if not await _acquire_lock(conn, LOCK_WAIT_SECONDS):
                logger.error(
                    "schema repair skipped: another instance holds the migration lock"
                )
                return {
                    "sessions_merged": 0,
                    "unique_index_ok": False,
                    "vector_migrated": [],
                    "hnsw_failed": 0,
                    "skipped": True,
                }
            try:
                merged = 0
                try:
                    merged = await _dedupe_sessions(conn)
                except Exception:
                    logger.exception(
                        "sessions dedupe failed; will try creating the index anyway"
                    )
                unique_index_ok = await _ensure_session_unique_index(conn)

                vector_migrated = []
                for table in ("facts", "kb_chunks"):
                    if await _ensure_vector_dim(
                        conn, table, dim, allow_destructive=allow_destructive
                    ):
                        vector_migrated.append(table)

                hnsw_failed = 0
                for idx_sql in _HNSW_INDEXES:
                    try:
                        async with conn.transaction():
                            await conn.execute("SET LOCAL lock_timeout = '30s'")
                            await conn.execute(idx_sql)
                    except Exception:
                        hnsw_failed += 1
                        logger.warning(
                            "hnsw index creation failed (pgvector too old?), fallback to btree only: %s",
                            idx_sql[:60],
                        )
            finally:
                await _release_lock(conn)
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(f"schema repair failed: {exc}") from exc

    return {
        "sessions_merged": merged,
        "unique_index_ok": unique_index_ok,
        "vector_migrated": vector_migrated,
        "hnsw_failed": hnsw_failed,
        "skipped": False,
    }
