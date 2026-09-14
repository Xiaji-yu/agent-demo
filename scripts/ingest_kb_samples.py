"""批量导入 data/kb_samples 下的 .md 文件到公共知识库。用法：

    .venv/bin/python scripts/ingest_kb_samples.py                 # 只导入新文件
    .venv/bin/python scripts/ingest_kb_samples.py --replace       # 同名且内容变化时替换
    .venv/bin/python scripts/ingest_kb_samples.py --prune         # 清理语料已删除的样例来源
    .venv/bin/python scripts/ingest_kb_samples.py --dry-run       # 只打印将要做什么

与 ``/kb samples`` 一致：同名来源按**内容指纹（sha256）**判重——
指纹相同跳过；指纹不同说明语料被改过，默认只提示，加 ``--replace`` 才替换。
替换采用**先写新、成功后再删旧**的顺序（评审 H1）：任一步失败旧内容都还在库中，
不会出现"删了旧的、新的没进去"的数据丢失。
**大文件（>2MB 或切块数超上限）自动切块**：在同目录的 ``<文件名>/`` 子目录里写出
``001.md``、``002.md``…（源文件保留），每个块作为独立来源导入，来源名形如
``文件名/001.md``，因此不再受 2MB / 块数上限的截断；超过 32MB 的源文件仍拒绝。
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


def _stale_part_sources(source_file: Path, sources: list[dict]) -> list[dict]:
    """反向迁移（切块 → 不再切块）时要一并清理的旧块来源。

    源文件变小、或 `AGENT_KB_MAX_CHUNKS_PER_SOURCE` 调大之后，文件不再走切块，
    但库里还挂着 ``文件名/00N.md`` 的旧块来源——它们与新整体来源是同一份语料，
    必须一起替换，否则检索里同一份内容出现两次。
    """
    prefix = f"{source_file.name}/"
    return [s for s in sources if str(s.get("name") or "").startswith(prefix)]


async def _process_file(
    kb,
    path: Path,
    old: dict | None,
    *,
    replace: bool,
    dry_run: bool,
    extra_superseded: list[dict] | None = None,
) -> dict:
    """处理单个语料文件：判重 → 写入 → （**成功之后**）删除旧来源。

    返回一份计数增量，键为
    ``imported/skipped/changed_pending/oversized/failed/dropped/cleaned``。

    H1 保证：``add_file`` 抛异常时立即返回，**绝不**删除旧来源——库里留着的仍是
    可用的旧内容；只有新来源成功入库后才删旧（删除本身失败也只是留下重复，
    由打印的提示引导人工清理）。

    ``extra_superseded``：**切块 → 整体**反向迁移时库里残留的 ``文件名/00N.md``
    旧块来源（源文件变小或上限调大后不再切块）。它们与新整体来源同属一份语料，
    整体来源确认在库后必须一并清理，否则检索里同一份内容出现两次。
    """
    from agentcore.rag.ingest import MAX_INGEST_BYTES, content_digest

    size = path.stat().st_size
    if size > MAX_INGEST_BYTES:
        print(f"⏭ {path.name}: 超过 2MB 上限，跳过")
        return {"oversized": 1}

    extras = list(extra_superseded or [])
    old_digest = ((old.get("meta") or {}) or {}).get("sha256") if old else None
    digest = content_digest(path.read_text(encoding="utf-8", errors="replace"))
    unchanged = old is not None and old_digest == digest

    if unchanged and not extras:
        print(f"⏭ {path.name}: 内容未变，跳过")
        return {"skipped": 1}
    if (old is not None or extras) and not replace:
        # 旧记录无指纹（历史存量）/ 语料被改过 / 残留旧块来源：默认只提示，不擅自删数据
        if old is not None and not unchanged:
            why = "无内容指纹（历史存量）" if not old_digest else "内容已变化"
        else:
            why = f"库中仍有 {len(extras)} 个旧块来源（已不再切块）"
        print(f"⚠ {path.name}: 同名来源{why}，需 --replace 才会更新（当前跳过）")
        return {"changed_pending": 1}

    if dry_run:
        if old is not None:
            print(
                f"[dry-run] 将替换 #{old['id']} {path.name}（先写入新来源，成功后再删旧）"
            )
        elif extras:
            print(f"[dry-run] 将导入 {path.name} 并清理 {len(extras)} 个旧块来源")
        else:
            print(f"[dry-run] 将导入 {path.name}（{size} 字节）")
        return {"changed_pending": 1} if (old is not None or extras) else {}

    dropped = 0
    if not unchanged:
        try:
            result = await kb.add_file(str(path), kind="sample")
        except Exception as exc:
            suffix = (
                f"（旧来源 #{old['id']} 未删除，原有内容仍在库中）"
                if old is not None
                else ""
            )
            print(f"✗ {path.name}: {exc}{suffix}")
            return {"failed": 1}
        dropped = int(result.get("dropped") or 0)
        note = f"，丢弃 {dropped} 块" if dropped else ""
        if result.get("truncated"):
            note += "，正文超 2MB 已截断"
        print(
            f"✓ {path.name}: 入库 {result['chunks']} 块（切出 {result.get('chunks_total', '?')} 块{note}）"
        )

    removed = 0
    if old is not None and not unchanged:
        # H1 修复：新来源已入库落地，此刻删旧才安全；删除失败只是留下重复，不丢数据
        try:
            await kb.delete_source(old["id"])
            removed += 1
        except Exception as exc:
            print(
                f"⚠ {path.name}: 新来源已入库，但旧来源 #{old['id']} 删除失败：{exc}\n"
                f"    → 库中可能同时存在两条同名来源，请重跑本脚本或手动 /kb forget #{old['id']}"
            )
        else:
            print(f"♻ 已替换 {path.name}（旧来源 #{old['id']} 已删除）")
    for stale in extras:
        try:
            await kb.delete_source(stale["id"])
            removed += 1
        except Exception as exc:
            print(f"⚠ {path.name}: 旧块来源 #{stale['id']} 删除失败：{exc}")
        else:
            print(f"♻ 已清理不再切块的旧块来源 #{stale['id']}（{stale.get('name')}）")

    outcome: dict = {"imported": 1, "dropped": dropped}
    if removed and unchanged:
        # 只做清理、没有重新写入：单独计数，避免汇总里被读成「导入了新内容」
        outcome["cleaned"] = removed
    return outcome


async def _process_split_source(
    kb, plan: dict, sources: list[dict], *, replace: bool, dry_run: bool = False
) -> dict:
    """处理一个大文件的切块计划：逐块写入，**成功之后**再删被取代的旧来源。

    - 新增切块（对应旧记录不存在）：直接写入，不需要 ``--replace``
    - 已存在同名切块且指纹变化、或曾作为整体导入过：属于替换，需 ``--replace``
    - H1：只要有任一切块写入失败，就**不删**任何旧来源（宁可留重复，不丢数据）

    M4/M7（REVIEW-c472e56..733f57e）：判重与替换以 ``parent.name`` 与
    ``f"{parent.name}/"`` 前缀下的**全部**来源为对象，而不是只看每名最新一条——
    - 源文件变短（块数 3→2）后，多余的旧 ``003.md`` 不在新计划里，只按计划逐条
      对比会永远看不见它（孤儿内容残留且 prune 判活通过）；
    - 部分失败重跑时，同名「最新一条」已是新指纹、更旧的被遮蔽来源会被
      「内容未变，跳过」骗过——必须对所有同名来源做指纹比对。

    返回计数增量，键为 ``imported/imported_parts/skipped/changed_pending/failed``。
    """
    parent = plan["source"]
    units = plan["units"]
    prefix = f"{parent.name}/"
    related: dict[str, list[dict]] = {}
    for s in sources:
        n = s.get("name")
        if n and (n == parent.name or n.startswith(prefix)):
            related.setdefault(n, []).append(s)

    pending: list[dict] = []
    superseded: list[dict] = []
    matched_names: set[str] = set()
    for unit in units:
        olds = related.get(unit["name"], [])
        if any(
            ((s.get("meta") or {}) or {}).get("sha256") == unit["sha256"] for s in olds
        ):
            matched_names.add(unit["name"])  # 该份已在库且内容未变
            # 但同名里可能还压着被遮蔽的旧指纹来源（部分失败重跑的残留）——照样替换
            superseded.extend(
                s
                for s in olds
                if ((s.get("meta") or {}) or {}).get("sha256") != unit["sha256"]
            )
            continue
        pending.append(unit)
        superseded.extend(olds)  # 该份的全部同名旧来源都将被替换
    # 计划外的旧来源：曾作为整体导入的 parent、以及缩块后多余的旧 00N.md
    seen = {id(s) for s in superseded}
    for name, olds in related.items():
        if name in matched_names:
            continue
        superseded.extend(s for s in olds if id(s) not in seen)

    if not pending and not superseded:
        print(f"⏭ {parent.name}: 切块内容未变（{len(units)} 份），跳过")
        return {"skipped": 1}
    if superseded and not replace:
        print(
            f"⚠ {parent.name}: 已切块为 {len(units)} 份"
            f"（{len(superseded)} 个旧来源待替换），需 --replace 才会更新（当前跳过）"
        )
        return {"changed_pending": 1}
    if dry_run:  # pragma: no cover - 当前由调用方在 dry-run 分支提前返回
        print(f"[dry-run] 将导入 {parent.name} 的切块")
        return {"changed_pending": 1} if superseded else {}

    imported_parts = 0
    failures = 0
    for unit in pending:
        try:
            result = await kb.add_file(
                str(unit["path"]), name=unit["name"], kind="sample"
            )
        except Exception as exc:
            failures += 1
            print(f"✗ {unit['name']}: {exc}")
            continue
        imported_parts += 1
        dropped = int(result.get("dropped") or 0)
        note = f"，丢弃 {dropped} 块" if dropped else ""
        print(f"✓ {unit['name']}: 入库 {result.get('chunks')} 块{note}")

    removed = 0
    if failures:
        print(f"⚠ {parent.name}: {failures} 个切块写入失败，保留全部旧来源（不删数据）")
    else:
        for old in superseded:
            try:
                await kb.delete_source(old["id"])
                removed += 1
            except Exception as exc:
                print(f"⚠ {parent.name}: 旧来源 #{old['id']} 删除失败：{exc}")
            else:
                print(f"♻ 已替换旧来源 #{old['id']}（{old.get('name')}）")

    outcome: dict = {}
    if imported_parts:
        outcome["imported"] = 1
        outcome["imported_parts"] = imported_parts
    elif removed:
        # 没有新写入、只清理了被遮蔽/计划外的旧来源：单独计数，别在汇总里
        # 显示成「导入 0 / 跳过 0」（那会让人以为什么都没做）
        outcome["cleaned"] = removed
    if failures:
        outcome["failed"] = 1
    return outcome


async def main(
    *, replace: bool = False, prune: bool = False, dry_run: bool = False
) -> None:
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

    sources = await kb.list_sources(limit=100000)
    by_name = _latest_by_name(sources)

    if prune:
        removed = await _prune(kb, sources, dry_run=dry_run)
        print(f"僵尸清理：{'将删除' if dry_run else '已删除'} {removed} 条")
        # 清理后重新取一次快照，避免后续判重用到已删来源
        sources = await kb.list_sources(limit=100000)
        by_name = _latest_by_name(sources)

    from agentcore.rag.ingest import plan_source_units

    imported = skipped = changed_pending = oversized = failed = 0
    dropped_total = 0
    split_parts = 0
    would_split = 0
    cleaned_total = 0
    for path in files:
        try:
            plan = plan_source_units(
                path,
                max_chars=kb.chunk_chars,
                max_chunks=kb.max_chunks_per_source,
                materialize=not dry_run,
            )
        except ValueError as exc:
            # M5：无法安全切块的文件名（如 ..md）单独报出，不拖垮整次批量导入
            print(f"⏭ {path.name}: {exc}，跳过")
            oversized += 1
            continue
        if plan["oversized"]:
            print(f"⏭ {path.name}: {plan['reason']}，跳过")
            oversized += 1
            continue
        if plan["split"]:
            if dry_run:
                print(
                    f"[dry-run] 将把 {path.name} 切块为 {plan.get('expected_parts')} 份"
                    "（同目录同名子目录，源文件保留）"
                )
                would_split += 1
                continue
            outcome = await _process_split_source(kb, plan, sources, replace=replace)
        else:
            # 反向迁移：曾经切块、现在不再切块时，库里残留的 `文件名/00N.md`
            # 旧块来源要随整体来源一起替换（否则同一份语料在检索里出现两次）
            extras = _stale_part_sources(path, sources)
            outcome = await _process_file(
                kb,
                path,
                by_name.get(path.name),
                replace=replace,
                dry_run=dry_run,
                extra_superseded=extras,
            )
        imported += outcome.get("imported", 0)
        split_parts += outcome.get("imported_parts", 0)
        skipped += outcome.get("skipped", 0)
        changed_pending += outcome.get("changed_pending", 0)
        oversized += outcome.get("oversized", 0)
        failed += outcome.get("failed", 0)
        dropped_total += outcome.get("dropped", 0)
        cleaned_total += outcome.get("cleaned", 0)

    print(
        f"\n汇总：导入 {imported} / 跳过 {skipped} / 待替换 {changed_pending} / "
        f"超限 {oversized} / 失败 {failed}；"
        f"切块份数 {split_parts}；清理重复来源 {cleaned_total}；"
        f"丢弃块数合计 {dropped_total}"
    )
    if would_split:
        print(f"（dry-run）另有 {would_split} 个大文件将被自动切块")
    if dropped_total:
        print(
            "提示：丢弃来自单来源块数上限，可用 AGENT_KB_MAX_CHUNKS_PER_SOURCE 提高后配合 --replace 重灌"
        )
    if failed:
        sys.exit(1)
    if changed_pending:
        # 有待替换项属于"没做完"，用非零码提醒（2 = 需人工决策）
        sys.exit(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="批量导入 data/kb_samples 下的 .md 到知识库"
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="同名且内容变化时替换（先写新，成功后再删旧）",
    )
    parser.add_argument(
        "--prune", action="store_true", help="清理语料文件已不存在的样例来源"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    args = parser.parse_args()
    asyncio.run(main(replace=args.replace, prune=args.prune, dry_run=args.dry_run))
