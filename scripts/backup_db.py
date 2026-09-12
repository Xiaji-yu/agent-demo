#!/usr/bin/env python
"""数据库备份/恢复 CLI。

    python scripts/backup_db.py backup              # 备份一次（自动选 pg_dump / JSONL）
    python scripts/backup_db.py backup --strategy jsonl
    python scripts/backup_db.py list                # 列出已有备份
    python scripts/backup_db.py verify <file>       # 全量校验：完整解压/逐行解析 + SHA256
    python scripts/backup_db.py restore <file> --yes   # 恢复（会改写目标库！）

⚠️ restore 会向 DATABASE_URL 指向的库写入数据；必须显式 --yes。
   .sql.gz 恢复（DROP+CREATE 覆盖）执行前会自动做一次 pre-restore 快照，
   快照失败则中止恢复。建议先 verify，再在一个独立库里演练一遍。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from agentcore.backup import (  # noqa: E402
    backup_database,
    list_backups,
    restore_database,
    restore_from_archive,
    verify_backup,
)


def _db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        print("缺少 DATABASE_URL", file=sys.stderr)
        raise SystemExit(2)
    return url


def cmd_backup(args) -> None:
    mirror = args.mirror or os.getenv("AGENT_BACKUP_MIRROR_DIR") or None
    result = asyncio.run(
        backup_database(
            _db_url(),
            args.dir,
            keep=args.keep,
            strategy=args.strategy,
            mirror_dir=mirror,
        )
    )
    print(f"✅ 备份完成：{result['path']}")
    print(
        f"   方式：{result['strategy']} | 大小：{result['bytes'] / 1024:.1f} KB"
        + (f" | 行数：{result.get('rows')}" if result.get("rows") is not None else "")
    )
    if result.get("pruned"):
        print(f"   轮转删除：{result['pruned']}")
    if mirror:
        if result.get("mirrored"):
            print(f"   异地副本：{result['mirror_path']}")
        else:
            print(f"   ⚠️ 异地副本失败：{mirror}（本地备份仍然有效，请检查该路径）")


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
    """全量校验：gzip 完整解压读完 / JSONL 全行解析 + 有 .sha256 sidecar 时核对摘要。"""
    path = Path(args.file)
    if not path.is_file():
        print(f"文件不存在：{path}", file=sys.stderr)
        raise SystemExit(2)
    result = verify_backup(path)
    if not result["ok"]:
        print(
            f"❌ 校验失败：{path.name}（{result.get('error') or '未知原因'}）",
            file=sys.stderr,
        )
        raise SystemExit(1)
    checksum_note = {
        "verified": "SHA256 一致",
        "missing": "无 .sha256 sidecar（旧备份，仅做全量可读性检查）",
        "mismatch": "校验和不符",
    }[result["checksum"]]
    print(f"✅ 可读（{result['kind']}）：{path.name} | 校验和：{checksum_note}")
    for table, n in sorted((result.get("tables") or {}).items()):
        print(f"   {table:<12} {n} 行")


def cmd_restore_archive(args) -> None:
    """从聊天记录归档回灌数据库（数据库被清空时的最后手段）。"""
    if not args.dry_run and not args.yes:
        print(
            "拒绝执行：该操作会向目标库写入消息，请先 --dry-run 查看，再加 --yes",
            file=sys.stderr,
        )
        raise SystemExit(2)
    result = asyncio.run(
        restore_from_archive(
            _db_url(),
            args.archive_dir,
            since_day=args.since,
            until_day=args.until,
            dry_run=args.dry_run,
        )
    )
    if result["status"] == "empty":
        print(f"归档目录没有记录：{args.archive_dir}")
        return
    if result["status"] == "dry-run":
        print(
            f"（演练）将回灌 {result['records']} 条消息 / {result['sessions']} 个会话"
        )
        print(f"   日期：{', '.join(result['days']) or '(未知)'}")
        print(f"   消息 id 区间：{result['first_id']} ~ {result['last_id']}")
        print("   确认无误后加 --yes 执行")
        return
    print(
        f"✅ 归档回灌完成：新增消息 {result['messages_inserted']} 条"
        f"（跳过已存在 {result['messages_skipped']} 条）"
        f"，会话新建 {result['sessions_created']} 个 / 复用 {result['sessions_reused']} 个"
    )


def cmd_restore(args) -> None:
    if not args.yes:
        print("拒绝执行：restore 会向目标库写入数据，请确认后加 --yes", file=sys.stderr)
        raise SystemExit(2)
    # M7：.sql.gz 恢复 = DROP+CREATE 覆盖现有库，执行前自动做一次即时快照
    # （tag=pre-restore，落到同一备份目录）——这是操作者唯一的反悔手段。
    # JSONL 恢复是幂等 DO NOTHING 追加，不覆盖数据，无需快照。
    if str(args.file).endswith(".sql.gz") and not args.dry_run:
        try:
            snap = asyncio.run(
                backup_database(_db_url(), args.dir, keep=args.keep, tag="pre-restore")
            )
        except Exception as exc:
            print(f"❌ 恢复前快照失败，已中止恢复：{exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(f"已先做恢复前快照：{snap['path']}")
    try:
        result = asyncio.run(
            restore_database(_db_url(), args.file, dry_run=args.dry_run)
        )
    except RuntimeError as exc:
        # M4：恢复有失败行时 fail-loud，绝不打印 ✅
        print(f"❌ 恢复失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"✅ 恢复结束：{result}")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dir", default=os.getenv("AGENT_BACKUP_DIR", "data/backups"))
    parser.add_argument(
        "--keep", type=int, default=int(os.getenv("AGENT_BACKUP_KEEP", "7"))
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_backup = sub.add_parser("backup", help="备份一次")
    p_backup.add_argument(
        "--strategy", choices=["auto", "pg_dump", "jsonl"], default="auto"
    )
    p_backup.add_argument(
        "--mirror", default=None, help="异地镜像目录（默认为 AGENT_BACKUP_MIRROR_DIR）"
    )
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

    p_arch = sub.add_parser(
        "restore-archive", help="从聊天记录归档回灌消息（最后手段）"
    )
    p_arch.add_argument(
        "--archive-dir", default=os.getenv("AGENT_ARCHIVE_DIR", "data/archive")
    )
    p_arch.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD")
    p_arch.add_argument("--until", default=None, help="结束日期 YYYY-MM-DD")
    p_arch.add_argument("--yes", action="store_true", help="确认写入")
    p_arch.add_argument("--dry-run", action="store_true", help="只统计不写入")
    p_arch.set_defaults(func=cmd_restore_archive)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
