"""批量导入 data/kb_samples 下的 .md 文件到公共知识库。用法：

    .venv/bin/python scripts/ingest_kb_samples.py                 # 只导入新文件
    .venv/bin/python scripts/ingest_kb_samples.py --replace       # 同名且内容变化时替换
    .venv/bin/python scripts/ingest_kb_samples.py --prune         # 清理语料已删除的样例来源
    .venv/bin/python scripts/ingest_kb_samples.py --dry-run       # 只打印将要做什么

与 ``/kb samples`` 一致：同名来源按**内容指纹（sha256）**判重——
指纹相同跳过；指纹不同说明语料被改过，默认只提示，加 ``--replace`` 才删旧重灌。
单文件上限 2MB（超限跳过、不计失败）；单来源块数上限默认 200，可用
``AGENT_KB_MAX_CHUNKS_PER_SOURCE`` 调整，超出会打印丢弃块数（评审 H1）。
任一文件导入失败退出码为 1，全部成功为 0。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = PROJECT_ROOT / "data" / "kb_samples"


def _load_config() -> dict:
    import yaml

    cfg_path = os.getenv("AGENT_CONFIG", str(PROJECT_ROOT / "config.yaml"))
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _is_sample_location(location: str) -> bool:
    """僵尸清理只允许动 kb_samples 目录内的来源（防误删 manual/distill）。"""
    try:
        return Path(location).resolve().is_relative_to(SAMPLES_DIR.resolve())
    except (OSError, ValueError):
        return False


async def _prune(kb, sources: list[dict], *, dry_run: bool) -> int:
    removed = 0
    for s in sources:
        if s.get("kind") != "sample":
            continue
        loc = s.get("location") or ""
        if not loc or not _is_sample_location(loc) or Path(loc).is_file():
            continue
        if dry_run:
            print(f"[dry-run] 将删除僵尸来源 #{s['id']} {s['name']}（文件已不存在）")
        else:
            await kb.delete_source(s["id"])
            print(f"🧹 删除僵尸来源 #{s['id']} {s['name']}（文件已不存在）")
        removed += 1
    return removed


async def main(*, replace: bool = False, prune: bool = False, dry_run: bool = False) -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    config = _load_config()

    from agentcore.embedding import load_embedding_client_from_env
    from agentcore.memory.store import InMemoryMemoryStore, PgMemoryStore

    embedding = load_embedding_client_from_env()
    try:
        dim = await embedding.probe_dim()
    except Exception:
        dim = getattr(embedding, "dim", 2048)

    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        memory = PgMemoryStore(db_url, dim=dim)
        await memory.init()
    else:
        memory = InMemoryMemoryStore()

    from agentcore.rag import KnowledgeBase
    from agentcore.rag.ingest import MAX_INGEST_BYTES, content_digest

    kb = KnowledgeBase(memory, embedding, (config.get("rag") or {}).copy())

    files = sorted(SAMPLES_DIR.glob("*.md"))
    if not files:
        print(f"未找到文件：{SAMPLES_DIR}")
        sys.exit(0)

    sources = await kb.list_sources(limit=1000)
    by_name = {s.get("name"): s for s in sources if s.get("name")}

    if prune:
        removed = await _prune(kb, sources, dry_run=dry_run)
        print(f"僵尸清理：{'将删除' if dry_run else '已删除'} {removed} 条")
        # 清理后重新取一次快照，避免后续判重用到已删来源
        sources = await kb.list_sources(limit=1000)
        by_name = {s.get("name"): s for s in sources if s.get("name")}

    imported = skipped = changed_pending = oversized = failed = 0
    dropped_total = 0
    for path in files:
        size = path.stat().st_size
        if size > MAX_INGEST_BYTES:
            print(f"⏭ {path.name}: 超过 2MB 上限，跳过")
            oversized += 1
            continue

        digest = content_digest(path.read_text(encoding="utf-8", errors="replace"))
        old = by_name.get(path.name)
        if old is not None:
            old_digest = ((old.get("meta") or {}) or {}).get("sha256")
            if old_digest == digest:
                print(f"⏭ {path.name}: 内容未变，跳过")
                skipped += 1
                continue
            if not replace:
                # 旧记录没有指纹（历史存量）或语料被改过：默认只提示，不擅自删数据
                why = "无内容指纹（历史存量）" if not old_digest else "内容已变化"
                print(f"⚠ {path.name}: 同名来源{why}，需 --replace 才会更新（当前跳过）")
                changed_pending += 1
                continue
            if dry_run:
                print(f"[dry-run] 将替换 #{old['id']} {path.name}")
                changed_pending += 1
                continue
            await kb.delete_source(old["id"])
            print(f"♻ 已删除旧来源 #{old['id']} {path.name}，准备重灌")

        if dry_run:
            print(f"[dry-run] 将导入 {path.name}（{size} 字节）")
            continue
        try:
            result = await kb.add_file(str(path), kind="sample")
        except Exception as exc:
            print(f"✗ {path.name}: {exc}")
            failed += 1
            continue
        dropped = int(result.get("dropped") or 0)
        dropped_total += dropped
        note = f"，丢弃 {dropped} 块" if dropped else ""
        if result.get("truncated"):
            note += "，正文超 2MB 已截断"
        print(f"✓ {path.name}: 入库 {result['chunks']} 块（切出 {result.get('chunks_total', '?')} 块{note}）")
        imported += 1

    print(
        f"\n汇总：导入 {imported} / 跳过 {skipped} / 待替换 {changed_pending} / "
        f"超限 {oversized} / 失败 {failed}；丢弃块数合计 {dropped_total}"
    )
    if dropped_total:
        print("提示：丢弃来自单来源块数上限，可用 AGENT_KB_MAX_CHUNKS_PER_SOURCE 提高后配合 --replace 重灌")
    if failed:
        sys.exit(1)
    if changed_pending:
        # 有待替换项属于"没做完"，用非零码提醒（2 = 需人工决策）
        sys.exit(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="批量导入 data/kb_samples 下的 .md 到知识库")
    parser.add_argument("--replace", action="store_true", help="同名且内容变化时删旧重灌")
    parser.add_argument("--prune", action="store_true", help="清理语料文件已不存在的样例来源")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    args = parser.parse_args()
    asyncio.run(main(replace=args.replace, prune=args.prune, dry_run=args.dry_run))
