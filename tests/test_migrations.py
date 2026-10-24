"""H4 迁移器测试：注册表/校验和/版本守卫/事务/锁/修复 + 备份表清单。

非 PG 用例默认套件即跑；PG 用例在 ``TEST_DATABASE_URL`` 下运行（CI 的 pgvector
service）。PG 用例使用**独立 schema**（search_path）而不是 TRUNCATE public，
避免与其他 PG 套件互相干扰；schema 名由 uuid 生成，不使用任何外部输入拼 DDL。
"""

import os
import re
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from agentcore.memory.migrate import (
    MIGRATION_LOCK_KEY,
    ChecksumMismatchError,
    DestructiveMigrationRequired,
    MigrationError,
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
    STATS_TABLES,
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


def test_stats_tables_match_embedded_ddl():
    """M7（REVIEW-ee39c8e..e55cab5）：stats 四表仍是内嵌自建（AGENTS §4 的登记
    例外），至少锁住「内嵌 DDL 实际建的表 == STATS_TABLES」——新增表忘登记会
    让备份/恢复与"所有表都要有归属"的检查静默漏掉它。"""
    from agentcore.groupstats import raw as raw_archive
    from agentcore.groupstats import store as stats_store

    pattern = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", re.IGNORECASE)
    declared = set(pattern.findall(stats_store.DDL)) | set(
        pattern.findall(raw_archive.DDL)
    )
    assert declared == set(STATS_TABLES)
    assert set(STATS_TABLES).isdisjoint(set(DATA_TABLES) | set(NON_DATA_TABLES))


def test_pg_dump_excludes_schema_migrations():
    from agentcore.backup import db_backup

    args = db_backup._pg_dump_rest_args(
        {"host": "h", "port": 5432, "user": "u", "database": "db"}
    )
    assert "--exclude-table=schema_migrations" in args
    assert args[-1] == "db", "数据库名必须是最后一个操作数"


# ---------------------------------------------------------------------------
# H1（REVIEW-ee39c8e..e55cab5）：执行期异常必须归入 MigrationError


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """最小 asyncpg 连接替身：只在 ``fail_on`` 命中的 SQL 上爆炸。"""

    def __init__(self, *, fail_on: str):
        self.fail_on = fail_on
        self.executed: list[str] = []

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            return True
        if "SUM(n - 1)" in sql:
            return 0
        return None

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        # 只让 facts 的维度不一致，kb_chunks 保持对齐，避免重复触发
        return {"t": "vector(4)" if args and args[0] == "facts" else "vector(8)"}

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, sql, *args):
        self.executed.append(sql)
        if self.fail_on in sql:
            raise RuntimeError("DDL typo failed")


class _FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.closed = False

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Acquire()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_migration_sql_error_is_wrapped_as_migration_error():
    """DDL 语法/权限错误原本以裸 RuntimeError 抛出 → 启动路径降级内存。

    回归断言：``migrate`` 必须抛 ``MigrationError`` 且保留原始异常链。
    """
    pool = _FakePool(_FakeConn(fail_on="CREATE EXTENSION IF NOT EXISTS vector"))
    with pytest.raises(MigrationError, match="migration failed") as excinfo:
        await migrate(pool, dim=8)
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "DDL typo failed" in str(excinfo.value.__cause__)


@pytest.mark.asyncio
async def test_repair_vector_sql_error_is_wrapped_as_migration_error():
    """vector 维度的 TRUNCATE/ALTER 报错同样不能被当成"普通故障"降级内存。"""
    pool = _FakePool(_FakeConn(fail_on="TRUNCATE TABLE facts"))
    with pytest.raises(MigrationError, match="schema repair failed") as excinfo:
        await repair_schema(pool, dim=8, allow_destructive=True)
    assert isinstance(excinfo.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_pg_store_init_propagates_migration_error_and_closes_pool(monkeypatch):
    """启动路径实证：``PgMemoryStore.init`` 必须抛 MigrationError（不是裸异常），
    并把已建成的池关掉——否则 ``_init_memory`` 会静默回退内存。"""
    import agentcore.memory.store as store_module

    pool = _FakePool(_FakeConn(fail_on="CREATE EXTENSION IF NOT EXISTS vector"))

    async def fake_create_pool(*args, **kwargs):
        return pool

    monkeypatch.setattr(store_module.asyncpg, "create_pool", fake_create_pool)
    store = store_module.PgMemoryStore("postgresql://x/y", dim=8)
    with pytest.raises(MigrationError):
        await store.init()
    assert store.pool is None
    assert pool.closed is True


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


# ==========================================================================
# REVIEW-ee39c8e..e55cab5 M4 / L2 / L3
#
# CLI 与运行时维度同源、TRUNCATE+ALTER 原子、repair 持锁、status 退出码、
# create 不要求 DATABASE_URL。全部为纯 Python 用例（不依赖 PG）。
# ==========================================================================


def _load_migrate_cli():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "migrate.py"
    spec = importlib.util.spec_from_file_location("migrate_cli_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_dim_matches_runtime_probe(monkeypatch):
    """M4：CLI 维度必须来自 provider 探测（与运行时 probe_dim 同源）。"""
    import asyncio

    import agentcore.embedding as embedding

    cli = _load_migrate_cli()

    class _Client:
        dim = 2048

        async def probe_dim(self):
            return 1024

    monkeypatch.setattr(embedding, "load_embedding_client_from_env", lambda: _Client())
    assert asyncio.run(cli._resolve_dim()) == 1024


def test_resolve_dim_falls_back_to_config_on_probe_error(monkeypatch):
    import asyncio

    import agentcore.embedding as embedding

    cli = _load_migrate_cli()

    class _Client:
        dim = 2048

        async def probe_dim(self):
            raise RuntimeError("unreachable")

    monkeypatch.setattr(embedding, "load_embedding_client_from_env", lambda: _Client())
    monkeypatch.setenv("EMBEDDING_DIM", "512")
    assert asyncio.run(cli._resolve_dim()) == 512


def test_status_exits_nonzero_on_unknown_ledger(monkeypatch):
    """L2：库比代码新时 status 必须非零退出（原实现只打印）。"""
    import asyncio

    from agentcore.memory.migrate import MigrationStatus

    cli = _load_migrate_cli()
    status = MigrationStatus(
        applied=(),
        pending=(),
        unknown=("0009",),
        checksum_mismatches=(),
        current="0009",
        latest="0001",
    )

    async def fake_migration_status(_pool, *, dim):
        assert dim >= 1
        return status

    class _Pool:
        async def close(self):
            pass

    async def fake_pool(_url):
        return _Pool()

    monkeypatch.setattr(cli, "migration_status", fake_migration_status)
    monkeypatch.setattr(cli, "_pool", fake_pool)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli._status("postgresql://u:p@127.0.0.1:5432/db"))
    assert exc.value.code == 1


def test_status_exits_nonzero_on_gap(monkeypatch):
    """L2：账本非前缀（空洞）同样非零退出。"""
    import asyncio

    from agentcore.memory.migrate import AppliedMigration, MigrationStatus

    cli = _load_migrate_cli()
    status = MigrationStatus(
        applied=(AppliedMigration("0002", "x", "sum", None, 1),),
        pending=(MIGRATIONS[0],),
        unknown=(),
        checksum_mismatches=(),
        current="0002",
        latest=MIGRATIONS[-1].version,
    )

    async def fake_migration_status(_pool, *, dim):
        return status

    class _Pool:
        async def close(self):
            pass

    async def fake_pool(_url):
        return _Pool()

    monkeypatch.setattr(cli, "migration_status", fake_migration_status)
    monkeypatch.setattr(cli, "_pool", fake_pool)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli._status("postgresql://u:p@127.0.0.1:5432/db"))
    assert exc.value.code == 1


def test_create_does_not_require_database_url(monkeypatch):
    """L3：create 是纯文件操作，无 DATABASE_URL 也必须能跑。"""
    import sys

    cli = _load_migrate_cli()
    created = []
    monkeypatch.setattr(cli, "_create", lambda name: created.append(name))
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)

    def boom(*_a, **_k):
        raise AssertionError("create 不得解析 DATABASE_URL")

    monkeypatch.setattr(cli, "_db_url", boom)
    monkeypatch.setattr(sys, "argv", ["migrate.py", "create", "0002_x"])
    cli.main()
    assert created == ["0002_x"]


class _TxCM:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.in_tx = True
        return self

    async def __aexit__(self, *_exc):
        self.conn.in_tx = False
        return False


class _DimConn:
    """只实现 _ensure_vector_dim 用到的协议。"""

    def __init__(self, fail_on: str = ""):
        self.in_tx = False
        self.executed: list[tuple[bool, str]] = []
        self.fail_on = fail_on

    def transaction(self):
        return _TxCM(self)

    async def fetchrow(self, _sql, *_args):
        return {"t": "vector(1024)"}

    async def execute(self, sql, *_args):
        self.executed.append((self.in_tx, sql))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("alter failed")


@pytest.mark.asyncio
async def test_vector_migration_truncate_and_alter_share_transaction():
    """M4：TRUNCATE 与 ALTER 必须同一事务——ALTER 失败时不得留下空表。"""
    from agentcore.memory.schema_repair import _ensure_vector_dim

    conn = _DimConn(fail_on="ALTER TABLE")
    with pytest.raises(RuntimeError, match="alter failed"):
        await _ensure_vector_dim(conn, "facts", 8, allow_destructive=True)

    by_sql = {sql: in_tx for in_tx, sql in conn.executed}
    assert by_sql["TRUNCATE TABLE facts"] is True, "TRUNCATE 必须在事务内"
    assert any(sql.startswith("ALTER TABLE facts") for sql in by_sql)


class _AsyncCM:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        return False


class _NoLockPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AsyncCM(self.conn)


class _NoLockConn:
    async def fetchval(self, sql, *_args):
        if "pg_try_advisory_lock" in sql:
            return False
        return None

    async def execute(self, sql, *_args):
        raise AssertionError(f"拿不到锁时不得执行修复 SQL：{sql}")


@pytest.mark.asyncio
async def test_repair_skips_when_lock_held(monkeypatch):
    """M4/L14：repair 必须与迁移器共用 advisory lock；拿不到就跳过 DDL。"""
    import agentcore.memory.schema_repair as repair_mod

    monkeypatch.setattr(repair_mod, "LOCK_WAIT_SECONDS", 0.0)
    result = await repair_schema(_NoLockPool(_NoLockConn()), dim=8)
    assert result["skipped"] is True
    assert result["vector_migrated"] == []
