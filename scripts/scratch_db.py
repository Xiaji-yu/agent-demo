#!/usr/bin/env python
"""创建/删除「临时验证库」，供集成测试与端到端验证使用。

**永远不要拿 DATABASE_URL 直接做破坏性验证**——那是生产库。用这个脚本开一个
独立临时库，验证完删掉：

    python scripts/scratch_db.py create            # 建库并打印连接串
    python scripts/scratch_db.py drop              # 删库
    TEST_DATABASE_URL=<打印出来的串> pytest tests/test_pg_store.py

临时库名固定为 agent_demo_scratch（不再支持 --name：动态标识符拼 DDL 无法通过
Mimosa 污点扫描，固定名足够覆盖本项目的演练流程）。
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
        print("缺少 DATABASE_URL（.env 里的管理连接串）", file=sys.stderr)
        raise SystemExit(2)
    return url


def _scratch_url() -> str:
    return up.urlparse(_admin_url())._replace(path="/" + SCRATCH_DB).geturl()


async def _connect(url: str):
    import asyncpg

    return await asyncpg.connect(url)


async def create() -> None:
    admin = await _connect(_admin_url())
    try:
        await admin.execute("DROP DATABASE IF EXISTS agent_demo_scratch WITH (FORCE)")
        await admin.execute("CREATE DATABASE agent_demo_scratch")
    finally:
        await admin.close()
    print(_scratch_url())


async def drop() -> None:
    admin = await _connect(_admin_url())
    try:
        await admin.execute("DROP DATABASE IF EXISTS agent_demo_scratch WITH (FORCE)")
    finally:
        await admin.close()
    print(f"已删除 {SCRATCH_DB}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "drop"])
    args = parser.parse_args()
    asyncio.run(create() if args.action == "create" else drop())


if __name__ == "__main__":
    main()
