#!/usr/bin/env python
"""数据库 schema 迁移 / 修复 CLI。

    python scripts/migrate.py status                     # 只读：已应用/待应用/缺口
    python scripts/migrate.py up                         # 应用非破坏性迁移 + 修复步骤
    python scripts/migrate.py up --allow-destructive --yes
    python scripts/migrate.py repair                     # 只跑修复步骤（去重/索引/维度/HNSW）
    python scripts/migrate.py repair --allow-destructive --yes
    python scripts/migrate.py create 0002_add_something  # 生成迁移模板（需手工注册）

默认使用 DATABASE_URL；--url 可指向临时库。破坏性操作必须同时给
--allow-destructive 与 --yes，并先做备份（scripts/backup_db.py backup）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from agentcore.memory.migrate import (  # noqa: E402
    MigrationError,
    migrate,
    migration_status,
)
from agentcore.memory.migrations import MIGRATIONS_DIR, latest_version  # noqa: E402
from agentcore.memory.schema_repair import repair_schema  # noqa: E402

_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_TEMPLATE = """-- {version}_{name}
-- 注意：已应用的迁移不可修改；结构变更请新增文件并在 MIGRATIONS 注册。
-- 不要写 BEGIN/COMMIT：runner 为每个文件包一层事务。
"""


def _db_url(raw: str | None) -> str:
    url = (raw or os.getenv("DATABASE_URL") or "").strip()
    if not url:
        print("缺少 DATABASE_URL（或用 --url 指定）", file=sys.stderr)
        raise SystemExit(2)
    return url


def _target_label(url: str) -> str:
    """只打印 host/db，绝不回显密码。"""
    import urllib.parse as up

    p = up.urlparse(url)
    return f"{p.hostname or '?'}:{p.port or 5432}/{p.path.lstrip('/') or '?'}"


def _dim() -> int:
    raw = (os.getenv("EMBEDDING_DIM") or "").strip()
    try:
        value = int(raw) if raw else 2048
    except ValueError:
        print(f"⚠ EMBEDDING_DIM={raw!r} 不是整数，使用 2048")
        value = 2048
    return max(1, min(value, 100000))


async def _pool(url: str):
    return await asyncpg.create_pool(url, min_size=1, max_size=2)


async def _status(url: str) -> None:
    pool = await _pool(url)
    try:
        status = await migration_status(pool, dim=_dim())
    finally:
        await pool.close()
    print(f"database: {_target_label(url)}")
    print(f"current : {status.current or '(none)'}")
    print(f"latest  : {status.latest or '(none)'}")
    if status.applied:
        for row in status.applied:
            bad = row.version in status.checksum_mismatches
            print(
                f"applied : {row.version}_{row.name}  {row.applied_at}  "
                f"checksum {'MISMATCH' if bad else 'ok'}"
            )
    print("pending : " + (", ".join(m.version for m in status.pending) or "(none)"))
    if status.unknown:
        print("unknown : " + ", ".join(status.unknown) + "  (数据库比代码新！)")
    if status.checksum_mismatches:
        print(
            "❌ 已应用迁移被修改："
            + ", ".join(status.checksum_mismatches)
            + " —— 恢复原文件，或新增迁移修正",
            file=sys.stderr,
        )
        raise SystemExit(1)


async def _up(url: str, allow_destructive: bool) -> None:
    pool = await _pool(url)
    try:
        result = await migrate(pool, dim=_dim(), allow_destructive=allow_destructive)
        repaired = await repair_schema(
            pool, dim=_dim(), allow_destructive=allow_destructive
        )
    finally:
        await pool.close()
    if result.applied:
        print("✅ 已应用：" + ", ".join(result.applied))
    else:
        print("✅ 无待应用迁移")
    print(
        "   修复：合并会话 {sessions_merged} | 唯一索引 {unique_index_ok} | "
        "维度迁移 {vector_migrated} | HNSW 失败 {hnsw_failed}".format(**repaired)
    )
    print(f"   current={result.current or '(none)'} latest={result.latest}")


async def _repair(url: str, allow_destructive: bool) -> None:
    pool = await _pool(url)
    try:
        repaired = await repair_schema(
            pool, dim=_dim(), allow_destructive=allow_destructive
        )
    finally:
        await pool.close()
    print("✅ 修复完成：" + str(repaired))


def _create(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        print("迁移名只允许小写字母/数字/下划线", file=sys.stderr)
        raise SystemExit(2)
    latest = latest_version()
    version = f"{int(latest) + 1:04d}" if latest else "0001"
    path = MIGRATIONS_DIR / f"{version}_{name}.sql"
    if path.exists():
        print(f"文件已存在：{path}", file=sys.stderr)
        raise SystemExit(1)
    path.write_text(_TEMPLATE.format(version=version, name=name), encoding="utf-8")
    print(f"✅ 已生成：{path}")
    print(
        "   下一步：在 agentcore/memory/migrations/__init__.py 的 MIGRATIONS 里注册\n"
        f'   _migration("{version}", "{name}")，并重启/运行 up。'
    )


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=None, help="数据库 URL（默认 DATABASE_URL）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", parents=[common], help="查看迁移状态（只读）")

    p_up = sub.add_parser("up", parents=[common], help="应用迁移并运行修复步骤")
    p_up.add_argument("--allow-destructive", action="store_true")
    p_up.add_argument("--yes", action="store_true", help="确认破坏性操作")

    p_repair = sub.add_parser("repair", parents=[common], help="只运行 schema 修复步骤")
    p_repair.add_argument("--allow-destructive", action="store_true")
    p_repair.add_argument("--yes", action="store_true", help="确认破坏性操作")

    p_create = sub.add_parser("create", parents=[common], help="生成新的迁移文件")
    p_create.add_argument("name")

    args = parser.parse_args()
    url = _db_url(args.url)

    if args.cmd == "create":
        _create(args.name)
        return
    if args.cmd == "status":
        print(f"target  : {_target_label(url)}")
        asyncio.run(_status(url))
        return

    if args.allow_destructive and not args.yes:
        print(
            "拒绝执行：--allow-destructive 会清空 dimension 不匹配的表，"
            "请先备份，再加 --yes 确认",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"target  : {_target_label(url)}")
    try:
        if args.cmd == "up":
            asyncio.run(_up(url, args.allow_destructive))
        else:
            asyncio.run(_repair(url, args.allow_destructive))
    except MigrationError as exc:
        print(f"❌ 迁移失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
