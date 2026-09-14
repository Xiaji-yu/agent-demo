#!/usr/bin/env python
"""创建/删除「临时验证库」，供集成测试与端到端验证使用。

**永远不要拿 DATABASE_URL 直接做破坏性验证**——那是生产库。用这个脚本开一个
独立临时库，验证完删掉：

    python scripts/scratch_db.py create --yes      # 建库并打印连接串
    python scripts/scratch_db.py drop --yes        # 删库
    TEST_DATABASE_URL=<打印出来的串> pytest tests/test_pg_store.py

临时库名固定为 agent_demo_scratch（不再支持 --name：动态标识符拼 DDL 无法通过
Mimosa 污点扫描，固定名足够覆盖本项目的演练流程）。

M12（REVIEW-c472e56..733f57e）：create/drop 都要求显式 ``--yes``——固定库名 +
``WITH (FORCE)`` 意味着共用同一 PG 实例的两个人会互相踩掉对方正在跑的临时库；
加一道确认闸，脚本不再「顺手就炸」。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import urllib.parse as up

SCRATCH_DB = "agent_demo_scratch"  # 固定常量：DDL 一律写字面量，不做任何拼接/格式化


def _admin_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        # 本脚本不自动读 .env（避免误连到不该连的库）——必须显式导出
        print(
            "缺少 DATABASE_URL：本脚本不读 .env，请先在当前 shell 导出，例如\n"
            "  export $(grep -E '^DATABASE_URL=' .env | head -1)\n"
            "  python scripts/scratch_db.py create --yes",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return url


def _scratch_url() -> str:
    return up.urlparse(_admin_url())._replace(path="/" + SCRATCH_DB).geturl()


async def _connect(url: str):
    import asyncpg

    return await asyncpg.connect(url)


def _confirmed(action: str, yes: bool) -> bool:
    if yes:
        return True
    parsed = up.urlparse(_admin_url())
    print(
        f"将在 {parsed.hostname}:{parsed.port or 5432} 上{action}数据库 "
        f"{SCRATCH_DB}（DROP 带 FORCE）。共用实例时会影响他人，确认请加 --yes",
        file=sys.stderr,
    )
    return False


async def create(yes: bool) -> None:
    if not _confirmed("创建", yes):
        raise SystemExit(2)
    admin = await _connect(_admin_url())
    try:
        await admin.execute("DROP DATABASE IF EXISTS agent_demo_scratch WITH (FORCE)")
        await admin.execute("CREATE DATABASE agent_demo_scratch")
    finally:
        await admin.close()
    print(_scratch_url())


async def drop(yes: bool) -> None:
    if not _confirmed("删除", yes):
        raise SystemExit(2)
    admin = await _connect(_admin_url())
    try:
        await admin.execute("DROP DATABASE IF EXISTS agent_demo_scratch WITH (FORCE)")
    finally:
        await admin.close()
    print(f"已删除 {SCRATCH_DB}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "drop"])
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认执行（共用 PG 实例时 FORCE DROP 会影响他人）",
    )
    args = parser.parse_args()
    asyncio.run(create(args.yes) if args.action == "create" else drop(args.yes))


if __name__ == "__main__":
    main()
