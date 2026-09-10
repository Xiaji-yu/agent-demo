#!/usr/bin/env python
"""创建/删除「临时验证库」，供集成测试与端到端验证使用。

**永远不要拿 DATABASE_URL 直接做破坏性验证**——那是生产库。用这个脚本开一个
独立临时库，验证完删掉：

    python scripts/scratch_db.py create            # 建库并打印连接串
    python scripts/scratch_db.py drop              # 删库
    TEST_DATABASE_URL=<打印出来的串> pytest tests/test_pg_store.py

库名必须含 scratch/test（脚本强制校验），避免误删生产库。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import urllib.parse as up

DEFAULT_NAME = "agent_demo_scratch"
SAFE_TOKENS = ("scratch", "test")


def _admin_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        print("缺少 DATABASE_URL（.env 里的管理连接串）", file=sys.stderr)
        raise SystemExit(2)
    return url


def _scratch_url(name: str) -> str:
    p = up.urlparse(_admin_url())
    return p._replace(path="/" + name).geturl()


def _check_name(name: str) -> None:
    low = name.lower()
    if not any(t in low for t in SAFE_TOKENS):
        print(f"拒绝操作：库名 {name!r} 不含 {'/'.join(SAFE_TOKENS)}，"
              "这可能是生产库", file=sys.stderr)
        raise SystemExit(2)
    prod_db = up.urlparse(_admin_url()).path.lstrip("/")
    if name == prod_db:
        print(f"拒绝操作：{name!r} 就是 DATABASE_URL 指向的库", file=sys.stderr)
        raise SystemExit(2)


async def _connect(url: str):
    import asyncpg

    return await asyncpg.connect(url)


async def create(name: str) -> None:
    _check_name(name)
    admin = await _connect(_admin_url())
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    print(_scratch_url(name))


async def drop(name: str) -> None:
    _check_name(name)
    admin = await _connect(_admin_url())
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.close()
    print(f"已删除 {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "drop"])
    parser.add_argument("--name", default=DEFAULT_NAME)
    args = parser.parse_args()
    asyncio.run(create(args.name) if args.action == "create" else drop(args.name))


if __name__ == "__main__":
    main()
