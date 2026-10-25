"""业务数据表的单一事实源（备份 / 恢复 / 迁移共用）。

为什么单独成模块：``_PGDATA_TABLES`` / ``_RESTORE_ORDER`` / ``_CONFLICT_TARGET``
原先散在 ``agentcore/backup/db_backup.py``，新增 ``group_persona`` 时漏登记，
导致群人格在备份/恢复后静默丢失。表清单集中在这里，并由
``tests/test_migrations.py`` 对比真实 schema 防再次漂移。

``schema_migrations`` 是部署元数据，**刻意不进**数据表清单：备份时排除，
恢复后由启动期的迁移 runner 按当前代码重建（见 ``agentcore/memory/migrate.py``）。
群统计 / 原文归档（``agentcore/groupstats``）体积大且可从归档重建，同样不在
备份范围，单独列在 ``STATS_TABLES`` 以免被"所有表都要备份"的检查误伤。
"""

DATA_TABLES = (
    "sessions",
    "messages",
    "facts",
    "kb_sources",
    "kb_chunks",
    "schedules",
    "user_state",
    "group_persona",
)

# 恢复顺序：先父表后子表（messages/facts 有指向 sessions 的外键）
RESTORE_ORDER = (
    "sessions",
    "user_state",
    "group_persona",
    "messages",
    "facts",
    "kb_sources",
    "kb_chunks",
    "schedules",
)

# 自增 id 的表：恢复后要重设 sequence，避免后续 INSERT 撞主键
SEQUENCE_TABLES = (
    "sessions",
    "messages",
    "facts",
    "kb_sources",
    "kb_chunks",
    "schedules",
)

# ON CONFLICT 冲突目标 = 主键列；未列出的表主键是 id
CONFLICT_TARGET = {"user_state": "user_id", "group_persona": "group_id"}

NON_DATA_TABLES = ("schema_migrations",)

STATS_TABLES = (
    "group_msg_daily",
    "group_word_daily",
    "group_reviews",
    "group_messages_raw",
)
