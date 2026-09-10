#!/usr/bin/env python
"""数据库备份/恢复 CLI。

    python scripts/backup_db.py backup              # 备份一次（自动选 pg_dump / JSONL）
    python scripts/backup_db.py backup --strategy jsonl
    python scripts/backup_db.py list                # 列出已有备份
    python scripts/backup_db.py verify <file>       # 只读校验备份可读、条目数
    python scripts/backup_db.py restore <file> --yes   # 恢复（会改写目标库！）

⚠️ restore 会向 DATABASE_URL 指向的库写入数据；必须显式 --yes。
   建议先 verify，再在一个独立库里演练一遍。
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from agentcore.backup import (  # noqa: E402
    backup_database,
    list_backups,
    restore_database,
)


def _db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        print("缺少 DATABASE_URL", file=sys.stderr)
        raise SystemExit(2)
    return url


def cmd_backup(args) -> None:
    result = asyncio.run(
        backup_database(_db_url(), args.dir, keep=args.keep, strategy=args.strategy)
    )
    print(f"✅ 备份完成：{result['path']}")
    print(f"   方式：{result['strategy']} | 大小：{result['bytes'] / 1024:.1f} KB"
          + (f" | 行数：{result.get('rows')}" if result.get("rows") is not None else ""))
    if result.get("pruned"):
        print(f"   轮转删除：{result['pruned']}")


def cmd_list(args) -> None:
    items = list_backups(args.dir)
    if not items:
        print("暂无备份")
        return
    print(f"共 {len(items)} 份备份（{args.dir}）：")
    for i in items:
        import time

        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(i["mtime"]))
        print(f"  {i['name']:<34} {i['kind']:<8} {i['bytes'] / 1024:>9.1f} KB  {when}")


def cmd_verify(args) -> None:
    path = Path(args.file)
    if not path.is_file():
        print(f"文件不存在：{path}", file=sys.stderr)
        raise SystemExit(2)
    if path.name.endswith(".sql.gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            head = "".join(fh.readline() for _ in range(5))
        print(f"✅ 可读（pg_dump SQL）：{path.name}")
        print("".join(f"   {ln}" for ln in head.splitlines()[:5]))
        return
    counts: dict[str, int] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "__meta__" in rec:
                continue
            counts[rec["table"]] = counts.get(rec["table"], 0) + 1
    print(f"✅ 可读（JSONL）：{path.name}")
    for table, n in sorted(counts.items()):
        print(f"   {table:<12} {n} 行")


def cmd_restore(args) -> None:
    if not args.yes:
        print("拒绝执行：restore 会向目标库写入数据，请确认后加 --yes", file=sys.stderr)
        raise SystemExit(2)
    result = asyncio.run(restore_database(_db_url(), args.file, dry_run=args.dry_run))
    print(f"✅ 恢复结束：{result}")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=os.getenv("AGENT_BACKUP_DIR", "data/backups"))
    parser.add_argument("--keep", type=int, default=int(os.getenv("AGENT_BACKUP_KEEP", "7")))
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_backup = sub.add_parser("backup", help="备份一次")
    p_backup.add_argument("--strategy", choices=["auto", "pg_dump", "jsonl"], default="auto")
    p_backup.set_defaults(func=cmd_backup)

    sub.add_parser("list", help="列出备份").set_defaults(func=cmd_list)

    p_verify = sub.add_parser("verify", help="校验备份可读")
    p_verify.add_argument("file")
    p_verify.set_defaults(func=cmd_verify)

    p_restore = sub.add_parser("restore", help="从备份恢复（危险）")
    p_restore.add_argument("file")
    p_restore.add_argument("--yes", action="store_true", help="确认执行")
    p_restore.add_argument("--dry-run", action="store_true", help="只统计不写入")
    p_restore.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
