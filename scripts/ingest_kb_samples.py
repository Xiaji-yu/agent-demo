"""批量导入 data/kb_samples 下的 .md 文件到公共知识库。用法：

    .venv/bin/python scripts/ingest_kb_samples.py                 # 只导入新文件
    .venv/bin/python scripts/ingest_kb_samples.py --replace       # 同名且内容变化时替换
    .venv/bin/python scripts/ingest_kb_samples.py --prune         # 清理语料已删除的样例来源
    .venv/bin/python scripts/ingest_kb_samples.py --dry-run       # 只打印将要做什么

与 ``/kb samples`` 一致：同名来源按**内容指纹（sha256）**判重——
指纹相同跳过；指纹不同说明语料被改过，默认只提示，加 ``--replace`` 才替换。
替换采用**先写新、成功后再删旧**的顺序（评审 H1）：任一步失败旧内容都还在库中，
不会出现"删了旧的、新的没进去"的数据丢失。
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
            removed += 1
            continue
        # M3 起 delete_source 也受 AGENT_KB_ENABLED 门控：关闭态下这里会抛，
        # 逐条兜住以免整个脚本中途带栈退出、连汇总都打不出来
        try:
            await kb.delete_source(s["id"])
        except Exception as exc:
            print(f"⚠ 僵尸来源 #{s['id']} {s['name']} 删除失败：{exc}")
            continue
        print(f"🧹 删除僵尸来源 #{s['id']} {s['name']}（文件已不存在）")
        removed += 1
    return removed


def _latest_by_name(sources: list[dict]) -> dict:
    """同名来源取**最新**一条（list_sources 最新在前，setdefault 保留首个）。

    历史遗留可能同名多条；直接 ``{name: s}`` 会让最旧的一条覆盖最新的，
    导致判重依据错位（评审 L 级）。
    """
    by_name: dict = {}
    for s in sources:
        name = s.get("name")
        if name:
            by_name.setdefault(name, s)
    return by_name


async def _process_file(kb, path: Path, old: dict | None, *, replace: bool, dry_run: bool) -> dict:
    """处理单个语料文件：判重 → 写入 → （**成功之后**）删除旧来源。

    返回一份计数增量，键为 ``imported/skipped/changed_pending/oversized/failed/dropped``。

    H1 保证：``add_file`` 抛异常时立即返回，**绝不**删除旧来源——库里留着的仍是
    可用的旧内容；只有新来源成功入库后才删旧（删除本身失败也只是留下重复，
    由打印的提示引导人工清理）。
    """
    from agentcore.rag.ingest import MAX_INGEST_BYTES, content_digest

    size = path.stat().st_size
    if size > MAX_INGEST_BYTES:
        print(f"⏭ {path.name}: 超过 2MB 上限，跳过")
        return {"oversized": 1}

    digest = content_digest(path.read_text(encoding="utf-8", errors="replace"))
    if old is not None:
        old_digest = ((old.get("meta") or {}) or {}).get("sha256")
        if old_digest == digest:
            print(f"⏭ {path.name}: 内容未变，跳过")
            return {"skipped": 1}
        if not replace:
            # 旧记录没有指纹（历史存量）或语料被改过：默认只提示，不擅自删数据
            why = "无内容指纹（历史存量）" if not old_digest else "内容已变化"
            print(f"⚠ {path.name}: 同名来源{why}，需 --replace 才会更新（当前跳过）")
            return {"changed_pending": 1}

    if dry_run:
        if old is not None:
            print(f"[dry-run] 将替换 #{old['id']} {path.name}（先写入新来源，成功后再删旧）")
            return {"changed_pending": 1}
        print(f"[dry-run] 将导入 {path.name}（{size} 字节）")
        return {}

    try:
        result = await kb.add_file(str(path), kind="sample")
    except Exception as exc:
        suffix = f"（旧来源 #{old['id']} 未删除，原有内容仍在库中）" if old is not None else ""
        print(f"✗ {path.name}: {exc}{suffix}")
        return {"failed": 1}

    dropped = int(result.get("dropped") or 0)
    note = f"，丢弃 {dropped} 块" if dropped else ""
    if result.get("truncated"):
        note += "，正文超 2MB 已截断"
    print(f"✓ {path.name}: 入库 {result['chunks']} 块（切出 {result.get('chunks_total', '?')} 块{note}）")

    if old is not None:
        # H1 修复：新来源已入库落地，此刻删旧才安全；删除失败只是留下重复，不丢数据
        try:
            await kb.delete_source(old["id"])
        except Exception as exc:
            print(
                f"⚠ {path.name}: 新来源已入库，但旧来源 #{old['id']} 删除失败：{exc}\n"
                f"    → 库中可能同时存在两条同名来源，请重跑本脚本或手动 /kb forget #{old['id']}"
            )
        else:
            print(f"♻ 已替换 {path.name}（旧来源 #{old['id']} 已删除）")

    return {"imported": 1, "dropped": dropped}


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

    kb = KnowledgeBase(memory, embedding, (config.get("rag") or {}).copy())

    files = sorted(SAMPLES_DIR.glob("*.md"))
    if not files:
        print(f"未找到文件：{SAMPLES_DIR}")
        sys.exit(0)

    sources = await kb.list_sources(limit=1000)
    by_name = _latest_by_name(sources)

    if prune:
        removed = await _prune(kb, sources, dry_run=dry_run)
        print(f"僵尸清理：{'将删除' if dry_run else '已删除'} {removed} 条")
        # 清理后重新取一次快照，避免后续判重用到已删来源
        sources = await kb.list_sources(limit=1000)
        by_name = _latest_by_name(sources)

    imported = skipped = changed_pending = oversized = failed = 0
    dropped_total = 0
    for path in files:
        outcome = await _process_file(
            kb, path, by_name.get(path.name), replace=replace, dry_run=dry_run
        )
        imported += outcome.get("imported", 0)
        skipped += outcome.get("skipped", 0)
        changed_pending += outcome.get("changed_pending", 0)
        oversized += outcome.get("oversized", 0)
        failed += outcome.get("failed", 0)
        dropped_total += outcome.get("dropped", 0)

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
    parser.add_argument("--replace", action="store_true", help="同名且内容变化时替换（先写新，成功后再删旧）")
    parser.add_argument("--prune", action="store_true", help="清理语料文件已不存在的样例来源")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    args = parser.parse_args()
    asyncio.run(main(replace=args.replace, prune=args.prune, dry_run=args.dry_run))
