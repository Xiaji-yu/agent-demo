"""H4 迁移器测试：注册表/校验和/版本守卫/事务/锁/修复 + 备份表清单。

非 PG 用例默认套件即跑；PG 用例在 ``TEST_DATABASE_URL`` 下运行（CI 的 pgvector
service）。PG 用例使用**独立 schema**（search_path）而不是 TRUNCATE public，
避免与其他 PG 套件互相干扰；schema 名由 uuid 生成，不使用任何外部输入拼 DDL。
"""

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from agentcore.memory.migrate import (
    MIGRATION_LOCK_KEY,
    ChecksumMismatchError,
    DestructiveMigrationRequired,
    MigrationLockTimeout,
    SchemaTooNewError,
    checksum_of,
    migrate,
    migration_status,
)
from agentcore.memory.migrations import (
    MIGRATIONS,
    Migration,
    latest_version,
    validate_registry,
)
from agentcore.memory.schema import (
    CONFLICT_TARGET,
    DATA_TABLES,
    NON_DATA_TABLES,
    RESTORE_ORDER,
)
from agentcore.memory.schema_repair import repair_schema

PG_URL = (os.getenv("TEST_DATABASE_URL") or "").strip()
_PG_MARK = pytest.mark.skipif(
    not PG_URL, reason="TEST_DATABASE_URL not set; PG migration tests skipped"
)

BASELINE = MIGRATIONS[0]


# ---------------------------------------------------------------------------
# 注册表与纯函数（无数据库）


def test_production_registry_is_valid():
    validate_registry()
    assert [m.version for m in MIGRATIONS] == [
        f"{i:04d}" for i in range(1, len(MIGRATIONS) + 1)
    ]
    assert latest_version() == MIGRATIONS[-1].version


def _write_migration(directory: Path, version: str, name: str, sql: str) -> Migration:
    path = directory / f"{version}_{name}.sql"
    path.write_text(sql, encoding="utf-8")
    return Migration(version=version, name=name, path=path)


def test_registry_rejects_unregistered_sql_file(tmp_path):
    registered = _write_migration(tmp_path, "0001", "one", "SELECT 1;")
    _write_migration(tmp_path, "0002", "two", "SELECT 2;")
    with pytest.raises(ValueError, match="unregistered"):
        validate_registry((registered,), directory=tmp_path)


def test_registry_rejects_gap_and_duplicate(tmp_path):
    one = _write_migration(tmp_path, "0001", "one", "SELECT 1;")
    three = _write_migration(tmp_path, "0003", "three", "SELECT 3;")
    with pytest.raises(ValueError, match="contiguous"):
        validate_registry((one, three), directory=None)
    two = _write_migration(tmp_path, "0001", "dup", "SELECT 2;")
    with pytest.raises(ValueError, match="duplicate"):
        validate_registry((one, two), directory=None)


def test_checksum_normalizes_newlines_only():
    assert checksum_of("SELECT 1;\n") == checksum_of("SELECT 1;\r\n")
    assert checksum_of("SELECT 1;") != checksum_of("SELECT 2;")


def test_backup_table_registry_includes_group_persona():
    assert "group_persona" in DATA_TABLES
    assert "group_persona" in RESTORE_ORDER
    assert CONFLICT_TARGET["group_persona"] == "group_id"
    assert "schema_migrations" in NON_DATA_TABLES
    assert "schema_migrations" not in DATA_TABLES


def test_pg_dump_excludes_schema_migrations():
    from agentcore.backup import db_backup

    args = db_backup._pg_dump_rest_args(
        {"host": "h", "port": 5432, "user": "u", "database": "db"}
    )
    assert "--exclude-table=schema_migrations" in args
    assert args[-1] == "db", "数据库名必须是最后一个操作数"


# ---------------------------------------------------------------------------
# PG 用例（独立 schema 隔离）


@pytest_asyncio.fixture
async def schema_pool():
    schema = f"mig_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(PG_URL)
    try:
        await admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await admin.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin.close()
    pool = await asyncpg.create_pool(
        PG_URL,
        min_size=1,
        max_size=3,
        server_settings={"search_path": f"{schema},public"},
    )
    try:
        yield pool, schema
    finally:
        await pool.close()
        admin = await asyncpg.connect(PG_URL)
        try:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await admin.close()


@pytest.mark.asyncio
@_PG_MARK
async def test_fresh_database_applies_baseline(schema_pool):
    pool, schema = schema_pool
    result = await migrate(pool, dim=8)
    assert result.applied == ("0001",)
    assert result.current == "0001"

    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('facts') IS NOT NULL")
        assert await conn.fetchval(
            "SELECT checksum FROM schema_migrations WHERE version='0001'"
        ) == checksum_of(BASELINE.path.read_text(encoding="utf-8"))
        tables = {
            r["tablename"]
            for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
            )
        }
    assert tables == set(DATA_TABLES) | set(NON_DATA_TABLES), (
        f"schema 与备份/迁移表清单漂移：{schema}"
    )


@pytest.mark.asyncio
@_PG_MARK
async def test_second_run_is_noop(schema_pool):
    pool, _ = schema_pool
    await migrate(pool, dim=8)
    again = await migrate(pool, dim=8)
    assert again.applied == ()
    assert again.current == "0001"


@pytest.mark.asyncio
@_PG_MARK
async def test_status_reports_applied_and_pending(schema_pool):
    pool, _ = schema_pool
    await migrate(pool, dim=8)
    status = await migration_status(pool, dim=8)
    assert [a.version for a in status.applied] == ["0001"]
    assert status.pending == ()
    assert status.checksum_mismatches == ()
    assert status.current == "0001"


@pytest.mark.asyncio
@_PG_MARK
async def test_checksum_mismatch_is_fatal(schema_pool, tmp_path):
    pool, _ = schema_pool
    await migrate(pool, dim=8)
    forged = _write_migration(tmp_path, "0001", "baseline", "SELECT 1;")
    with pytest.raises(ChecksumMismatchError, match="checksum mismatch"):
        await migrate(pool, dim=8, migrations=(forged,))


@pytest.mark.asyncio
@_PG_MARK
async def test_database_newer_than_code_is_fatal(schema_pool):
    pool, _ = schema_pool
    await migrate(pool, dim=8)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO schema_migrations(version, name, checksum) "
            "VALUES('9999', 'future', 'x')"
        )
    with pytest.raises(SchemaTooNewError, match="newer than this code"):
        await migrate(pool, dim=8)


@pytest.mark.asyncio
@_PG_MARK
async def test_destructive_migration_requires_explicit_flag(schema_pool, tmp_path):
    pool, _ = schema_pool
    await migrate(pool, dim=8)
    destructive = _write_migration(
        tmp_path, "0002", "destructive_probe", "CREATE TABLE destructive_probe(id int);"
    )
    destructive = Migration(
        version=destructive.version,
        name=destructive.name,
        path=destructive.path,
        destructive=True,
    )
    registry = (BASELINE, destructive)
    with pytest.raises(DestructiveMigrationRequired, match="destructive"):
        await migrate(pool, dim=8, migrations=registry)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('destructive_probe')") is None

    result = await migrate(pool, dim=8, migrations=registry, allow_destructive=True)
    assert result.applied == ("0002",)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('destructive_probe')")


@pytest.mark.asyncio
@_PG_MARK
async def test_failed_migration_rolls_back_and_releases_lock(schema_pool, tmp_path):
    pool, _ = schema_pool
    await migrate(pool, dim=8)
    broken = _write_migration(
        tmp_path,
        "0002",
        "broken",
        "CREATE TABLE rollback_probe(id int); SELECT 1/0;",
    )
    with pytest.raises(asyncpg.PostgresError):
        await migrate(pool, dim=8, migrations=(BASELINE, broken))

    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('rollback_probe')") is None
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM schema_migrations WHERE version='0002'"
            )
            == 0
        )
        # 事务失败后 session 级 advisory lock 必须在 finally 释放
        assert await conn.fetchval(
            "SELECT pg_try_advisory_lock($1::bigint)", MIGRATION_LOCK_KEY
        )
        await conn.fetchval("SELECT pg_advisory_unlock($1::bigint)", MIGRATION_LOCK_KEY)


@pytest.mark.asyncio
@_PG_MARK
async def test_concurrent_migration_waits_or_times_out(schema_pool):
    pool, _ = schema_pool
    async with pool.acquire() as holder:
        assert await holder.fetchval(
            "SELECT pg_try_advisory_lock($1::bigint)", MIGRATION_LOCK_KEY
        )
        try:
            with pytest.raises(MigrationLockTimeout, match="another instance"):
                await migrate(pool, dim=8, lock_wait_seconds=0.1)
        finally:
            await holder.fetchval(
                "SELECT pg_advisory_unlock($1::bigint)", MIGRATION_LOCK_KEY
            )
    # 锁释放后可以正常迁移
    assert (await migrate(pool, dim=8)).applied == ("0001",)


@pytest.mark.asyncio
@_PG_MARK
async def test_repair_merges_legacy_sessions(schema_pool):
    pool, _ = schema_pool
    await migrate(pool, dim=8)
    async with pool.acquire() as conn:
        for _ in range(2):
            await conn.execute(
                "INSERT INTO sessions(user_id, group_id, scope) "
                "VALUES('u-legacy', NULL, 'private')"
            )
    repaired = await repair_schema(pool, dim=8)
    assert repaired["sessions_merged"] == 1
    assert repaired["unique_index_ok"] is True
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM sessions WHERE user_id='u-legacy'"
            )
            == 1
        )
        assert await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE indexname='sessions_user_scope_key'"
        )
