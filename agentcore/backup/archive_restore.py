"""从「聊天记录归档」回灌数据库——数据库被清空/损坏时的最后一道恢复手段。

归档（`data/archive/messages-*.jsonl`）落在数据库之外，任何针对数据库的误操作
都碰不到它。本模块把归档记录按原样写回数据库：

- 会话按 (user_id, group_id) 复用或新建（归档里的旧 session_id 在清库后已失效）
- 消息**保留原 id**：蒸馏水位线以消息 id 为准，保留 id 才能让水位线继续有效；
  已存在的 id 直接跳过（幂等，可反复执行）
- 结束后把 messages 序列重置到最大 id，避免后续写入撞主键
- 默认 dry-run，真正写入需要显式确认（见 scripts/backup_db.py restore-archive）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agentcore.memory.archive import MessageArchive

logger = logging.getLogger(__name__)


async def restore_from_archive(
    db_url: str,
    archive_dir: str | Path,
    *,
    since_day: str | None = None,
    until_day: str | None = None,
    dry_run: bool = True,
) -> dict:
    """把归档里的消息回灌进数据库。返回统计（会话/消息的新增与跳过数）。"""
    import asyncpg

    archive = MessageArchive(archive_dir)
    # M（REVIEW-a604023..679c9b3）：恢复不能走默认 20 万**读行**上限——超限会静默
    # 截断（dry-run 统计同样偏小）。显式 limit=None 表示不限。
    records = [
        r
        for r in archive.iter_records(
            0, since_day=since_day, until_day=until_day, limit=None
        )
    ]
    if not records:
        return {"status": "empty", "records": 0, "days": []}

    if dry_run:
        return {
            "status": "dry-run",
            "records": len(records),
            "sessions": len({(r.get("user_id"), r.get("group_id")) for r in records}),
            "days": archive.days(since_day=since_day, until_day=until_day),
            "first_id": records[0].get("id"),
            "last_id": records[-1].get("id"),
        }

    conn = await asyncpg.connect(db_url)
    stats = {
        "sessions_created": 0,
        "sessions_reused": 0,
        "messages_inserted": 0,
        "messages_skipped": 0,
    }
    session_cache: dict[tuple[str, str | None], int] = {}
    try:
        async with conn.transaction():
            for rec in records:
                user_id = str(rec.get("user_id") or "unknown")
                group_id = rec.get("group_id")
                key = (user_id, group_id)
                sid = session_cache.get(key)
                if sid is None:
                    sid = await _find_or_create_session(conn, user_id, group_id, stats)
                    session_cache[key] = sid
                msg_id = int(rec.get("id") or 0)
                sql = (
                    "INSERT INTO messages(id, session_id, role, content, tool_calls, tool_call_id) "
                    "VALUES($1,$2,$3,$4,$5::jsonb,$6) ON CONFLICT (id) DO NOTHING"
                )
                tool_calls = rec.get("tool_calls")
                result = await conn.execute(
                    sql,
                    msg_id or None,
                    sid,
                    rec.get("role") or "user",
                    rec.get("content") or "",
                    json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                    rec.get("tool_call_id"),
                )
                if result.endswith(" 1"):
                    stats["messages_inserted"] += 1
                else:
                    stats["messages_skipped"] += 1
            await conn.execute(
                "SELECT setval(pg_get_serial_sequence('messages','id'), "
                "COALESCE((SELECT MAX(id) FROM messages), 1))"
            )
    finally:
        await conn.close()
    stats["status"] = "ok"
    stats["records"] = len(records)
    logger.info("archive restore: %s", stats)
    return stats


async def _find_or_create_session(
    conn, user_id: str, group_id: str | None, stats: dict
) -> int:
    scope = "group" if group_id else "private"
    row = await conn.fetchrow(
        "SELECT id FROM sessions "
        "WHERE user_id=$1 AND ((group_id IS NULL AND $2::text IS NULL) OR group_id=$2) AND scope=$3",
        user_id,
        group_id,
        scope,
    )
    if row:
        stats["sessions_reused"] += 1
        return int(row["id"])
    await conn.execute(
        "INSERT INTO sessions(user_id, group_id, scope) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
        user_id,
        group_id,
        scope,
    )
    row = await conn.fetchrow(
        "SELECT id FROM sessions "
        "WHERE user_id=$1 AND ((group_id IS NULL AND $2::text IS NULL) OR group_id=$2) AND scope=$3",
        user_id,
        group_id,
        scope,
    )
    stats["sessions_created"] += 1
    return int(row["id"])
