"""轻量数据库迁移 runner（asyncpg 直连，不引入 Alembic/SQLAlchemy）。

核心保证：
- **advisory lock**：多实例同时启动/CLI 与 bot 并发时，迁移串行执行；
- **checksum**：已应用迁移的 SQL 不可偷改，否则启动拒绝（防止 schema 与账本不一致）；
- **版本守卫**：数据库版本高于代码（降级）或账本出现空洞 → 拒绝运行；
- **每迁移一个事务**：失败只回滚本迁移，账本行与 SQL 同事务提交；
- **破坏性迁移默认不执行**：需要 ``allow_destructive=True``（CLI 显式开关）。

修复类操作（去重会话 / 唯一索引 / vector 维度 / HNSW）不在这里，见
``agentcore/memory/schema_repair.py``：它们不是 schema 版本，必须每次启动可重试。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass

from agentcore.memory.migrations import (
    MIGRATIONS,
    Migration,
    latest_version,
    validate_registry,
)

logger = logging.getLogger(__name__)

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_ms INTEGER
);
"""

# "AGENTMG" 的 ASCII 十六进制；落在 PostgreSQL bigint 正区间
MIGRATION_LOCK_KEY = 0x4147454E545F4D47
LOCK_WAIT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.2


class MigrationError(RuntimeError):
    """迁移失败的基类：启动路径必须 fail-closed（降级到内存比不启动更危险）。"""


class MigrationRegistryError(MigrationError):
    pass


class MigrationLockTimeout(MigrationError):
    pass


class SchemaTooNewError(MigrationError):
    pass


class ChecksumMismatchError(MigrationError):
    pass


class DestructiveMigrationRequired(MigrationError):
    pass


class SchemaStateError(MigrationError):
    pass


@dataclass(frozen=True)
class AppliedMigration:
    version: str
    name: str
    checksum: str
    applied_at: object
    duration_ms: int | None


@dataclass(frozen=True)
class MigrationResult:
    applied: tuple[str, ...]
    current: str | None
    latest: str | None


@dataclass(frozen=True)
class MigrationStatus:
    applied: tuple[AppliedMigration, ...]
    pending: tuple[Migration, ...]
    unknown: tuple[str, ...]
    checksum_mismatches: tuple[str, ...]
    current: str | None
    latest: str | None


def checksum_of(sql: str) -> str:
    """迁移校验和：按 UTF-8 文本计算，先把 CRLF 归一成 LF。

    只归一换行：避免 Windows 检出（autocrlf）与 Linux 部署对同一份 SQL
    得到不同摘要，又保留"任何真实内容修改都会变"的性质。
    """
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _read_sql(migration: Migration, dim: int) -> tuple[str, str]:
    """返回（原始 SQL, 替换 {dim} 后可执行 SQL）。校验和永远针对原始 SQL。"""
    raw = migration.path.read_text(encoding="utf-8")
    return raw, raw.replace("{dim}", str(int(dim)))


def _validate(migrations: tuple[Migration, ...]) -> dict[str, Migration]:
    try:
        # 生产注册表的杂散文件检查在 import 时已完成；这里跳过目录扫描，
        # 以便测试传入 tmp 目录里的自定义迁移集。
        validate_registry(migrations, directory=None)
    except ValueError as exc:
        raise MigrationRegistryError(str(exc)) from exc
    return {m.version: m for m in migrations}


async def _acquire_lock(conn, wait_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        if await conn.fetchval(
            "SELECT pg_try_advisory_lock($1::bigint)", MIGRATION_LOCK_KEY
        ):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(LOCK_POLL_SECONDS)


async def _release_lock(conn) -> None:
    try:
        await conn.fetchval("SELECT pg_advisory_unlock($1::bigint)", MIGRATION_LOCK_KEY)
    except Exception:
        # 连接断开时 PostgreSQL 会自动释放 session 级锁；这里只留痕，不影响退出
        logger.warning("failed to release migration advisory lock", exc_info=True)


async def migrate(
    pool,
    *,
    dim: int,
    allow_destructive: bool = False,
    migrations: tuple[Migration, ...] = MIGRATIONS,
    lock_wait_seconds: float = LOCK_WAIT_SECONDS,
) -> MigrationResult:
    """把数据库升级到代码声明的最高版本；返回本次实际应用的版本。

    失败一律抛 ``MigrationError`` 子类：调用方（启动路径）不得静默降级。
    """
    known = _validate(migrations)
    try:
        async with pool.acquire() as conn:
            if not await _acquire_lock(conn, lock_wait_seconds):
                raise MigrationLockTimeout(
                    "another instance is migrating (advisory lock held); "
                    "wait for it to finish and retry"
                )
            try:
                await conn.execute(SCHEMA_MIGRATIONS_DDL)
                rows = await conn.fetch(
                    "SELECT version, name, checksum, applied_at, duration_ms "
                    "FROM schema_migrations ORDER BY version"
                )
                applied = {r["version"]: r for r in rows}

                unknown = sorted(set(applied) - set(known))
                if unknown:
                    raise SchemaTooNewError(
                        f"database schema {unknown[-1]} is newer than this code "
                        f"(latest known {latest_version(migrations)}); upgrade the code "
                        "or restore a backup from a matching release"
                    )

                expected_prefix = tuple(m.version for m in migrations[: len(applied)])
                if tuple(sorted(applied)) != expected_prefix:
                    raise SchemaStateError(
                        "schema_migrations has a gap or non-prefix history: "
                        f"applied={sorted(applied)} expected_prefix={expected_prefix}"
                    )

                for version, row in applied.items():
                    raw, _ = _read_sql(known[version], dim)
                    if row["checksum"] != checksum_of(raw):
                        raise ChecksumMismatchError(
                            f"migration {version} checksum mismatch: applied SQL was "
                            "edited. Never edit an applied migration — add a new one "
                            "instead."
                        )

                newly: list[str] = []
                for migration in migrations:
                    if migration.version in applied:
                        continue
                    if migration.destructive and not allow_destructive:
                        raise DestructiveMigrationRequired(
                            f"migration {migration.version}_{migration.name} is "
                            "destructive; back up first, then run: "
                            "python scripts/migrate.py up --allow-destructive --yes"
                        )
                    raw, sql = _read_sql(migration, dim)
                    started = time.monotonic()
                    async with conn.transaction():
                        await conn.execute("SET LOCAL lock_timeout = '30s'")
                        await conn.execute(sql)
                        await conn.execute(
                            "INSERT INTO schema_migrations"
                            "(version, name, checksum, duration_ms) "
                            "VALUES($1, $2, $3, $4)",
                            migration.version,
                            migration.name,
                            checksum_of(raw),
                            int((time.monotonic() - started) * 1000),
                        )
                    newly.append(migration.version)
                    logger.info(
                        "migration applied: %s_%s", migration.version, migration.name
                    )

                versions = tuple(sorted(set(applied) | set(newly)))
                return MigrationResult(
                    applied=tuple(newly),
                    current=versions[-1] if versions else None,
                    latest=latest_version(migrations),
                )
            finally:
                await _release_lock(conn)
    except MigrationError:
        raise
    except Exception as exc:
        # H1（REVIEW-ee39c8e..e55cab5）：DDL 的语法/权限/文件读取错误原本以裸异常
        # 抛出，启动路径把它当"普通故障"降级内存 → 记忆看似持久化、重启即丢。
        # 迁移路径的任何失败都必须带 MigrationError 的语义，由调用方 fail-closed。
        raise MigrationError(f"migration failed: {exc}") from exc


async def migration_status(
    pool, *, dim: int, migrations: tuple[Migration, ...] = MIGRATIONS
) -> MigrationStatus:
    """只读状态：不改库、不取锁，供 ``scripts/migrate.py status`` 使用。"""
    known = _validate(migrations)
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT to_regclass('schema_migrations') IS NOT NULL"
        )
        rows = (
            await conn.fetch(
                "SELECT version, name, checksum, applied_at, duration_ms "
                "FROM schema_migrations ORDER BY version"
            )
            if exists
            else []
        )
    applied = tuple(
        AppliedMigration(
            version=r["version"],
            name=r["name"],
            checksum=r["checksum"],
            applied_at=r["applied_at"],
            duration_ms=r["duration_ms"],
        )
        for r in rows
    )
    applied_versions = {a.version for a in applied}
    mismatches = tuple(
        a.version
        for a in applied
        if a.version in known
        and checksum_of(_read_sql(known[a.version], dim)[0]) != a.checksum
    )
    return MigrationStatus(
        applied=applied,
        pending=tuple(m for m in migrations if m.version not in applied_versions),
        unknown=tuple(sorted(applied_versions - set(known))),
        checksum_mismatches=mismatches,
        current=max(applied_versions) if applied_versions else None,
        latest=latest_version(migrations),
    )
