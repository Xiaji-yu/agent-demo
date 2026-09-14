"""知识摄取：把文本/文件切块、向量化后写入公共知识库。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path

from agentcore.rag.chunker import chunk_text
from agentcore.rag.sanitize import scrub_pii

logger = logging.getLogger(__name__)

MAX_INGEST_BYTES = 2 * 1024 * 1024  # 单个文本/文件最多摄取 2MB
MAX_CHUNKS_PER_SOURCE = 200  # 单个来源默认最多切块数（防一次灌爆知识库）
MAX_CHUNKS_ENV = "AGENT_KB_MAX_CHUNKS_PER_SOURCE"
# 自动切块的源文件硬上限：再大就拒绝，避免一条命令写出成百上千份副本
MAX_SPLIT_SOURCE_BYTES = 32 * 1024 * 1024


def _coerce_positive(raw, *, label: str, default: int) -> int:
    """把外部来的上限值收敛成正整数；脏值一律告警并回退默认（M2）。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r 不是整数，回退默认 %d", label, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%s 非法（须 > 0），回退默认 %d", label, value, default)
        return default
    return value


def max_chunks_per_source() -> int:
    """单来源块数上限：可被 ``AGENT_KB_MAX_CHUNKS_PER_SOURCE`` 覆盖。

    默认 200 对长文档会**静默砍尾**（评审 REVIEW-bbd8913..f6dffcc.md 的 H1），
    因此这里要求上限可配置，并让调用方知道丢弃了多少。
    """
    raw = (os.getenv(MAX_CHUNKS_ENV) or "").strip()
    if not raw:
        return MAX_CHUNKS_PER_SOURCE
    return _coerce_positive(raw, label=MAX_CHUNKS_ENV, default=MAX_CHUNKS_PER_SOURCE)


def content_digest(text: str) -> str:
    """语料内容指纹（strip 后的原始文本 sha256），用于判重与"内容变更检测"。"""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _resolve_chunk_limit(max_chunks: int | None) -> int:
    """块数上限：显式值优先（同样要收敛脏值），否则读环境/默认。"""
    if max_chunks is None:
        return max_chunks_per_source()
    return _coerce_positive(
        max_chunks, label="max_chunks", default=MAX_CHUNKS_PER_SOURCE
    )


def split_dir(path: str | Path) -> Path:
    """大文件的切块目录：与源文件**同目录**、以源文件主名（stem）命名的子目录。"""
    p = Path(path)
    return p.parent / p.stem


_PART_RE = re.compile(r"\d{3}\.md")


def _check_split_target(p: Path) -> None:
    """切块落盘前的安全检查（M5，REVIEW-c472e56..733f57e）。

    - stem 为空/``.``/``..`` 时 `split_dir` 会折叠成父目录本身（如 ``..md`` 的
      stem 是 ``.``），块文件会**摊平写进顶层**并打破「产物只在 <stem>/ 子目录」
      的扫描不变量（下轮把块当新源再入库，内容翻倍）——必须拒绝；
    - 目标目录已存在且含非 ``NNN.md`` 文件（可能是用户自己的目录/文件）时
      拒绝——``mkdir(exist_ok=True)`` + ``write_text`` 会静默覆盖用户数据。
    """
    if p.stem in ("", ".", ".."):
        raise ValueError(
            f"源文件名 {p.name!r} 无法安全切块（stem={p.stem!r} 会摊平写入父目录）；"
            "请先重命名源文件"
        )
    out_dir = split_dir(p)
    if out_dir.is_dir():
        foreign = [f.name for f in out_dir.iterdir() if not _PART_RE.fullmatch(f.name)]
        if foreign:
            raise ValueError(
                f"切块目录 {out_dir} 已存在且含非切块文件（如 {foreign[0]!r}），"
                "拒绝写入以免覆盖既有内容；请清理或重命名该目录后重试"
            )


def _part_name(index: int) -> str:
    return f"{index:03d}.md"


def _group_chunks(
    all_chunks: list[str], *, max_chunks: int, max_bytes: int
) -> list[list[str]]:
    """按「每份块数」与「每份字节」双重上限把块分组。

    安全性：``chunk_text`` 产出的每块长度都 ≤ ``max_chars``，因此把同一组的块用
    空行拼接后重新切块**只会合并、不会新增**——每份重新切出的块数必然 ≤ 组内块数。
    """
    groups: list[list[str]] = []
    cur: list[str] = []
    cur_bytes = 0
    for ch in all_chunks:
        size = len(ch.encode("utf-8")) + 2  # 拼接时补的 "\n\n"
        if cur and (len(cur) >= max_chunks or cur_bytes + size > max_bytes):
            groups.append(cur)
            cur = []
            cur_bytes = 0
        cur.append(ch)
        cur_bytes += size
    if cur:
        groups.append(cur)
    return groups


def _write_groups(
    p: Path, groups: list[list[str]], *, max_chars: int, overlap: int, limit: int
) -> dict:
    """把分组后的文本写成 ``<stem>/NNN.md``（源文件保留），返回块文件清单。"""
    _check_split_target(p)  # M5：stem 异常/目录含外部文件时拒绝，绝不摊平或覆盖
    out_dir = split_dir(p)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[dict] = []
    for index, group in enumerate(groups, start=1):
        body = "\n\n".join(group).strip() + "\n"
        part_path = out_dir / _part_name(index)
        part_path.write_text(body, encoding="utf-8")
        part_chunks = len(chunk_text(body, max_chars=max_chars, overlap=overlap))
        if part_chunks > limit:  # pragma: no cover - 防御性
            logger.warning(
                "ingest: 切块 %s 重新切出 %d 块，超过单来源上限 %d——请调大 max_chars",
                part_path,
                part_chunks,
                limit,
            )
        parts.append(
            {
                "path": part_path,
                "name": part_path.name,
                "chunks": part_chunks,
                "sha256": content_digest(body),
            }
        )
    # M4（REVIEW-c472e56..733f57e）：清掉超出新份数的陈旧块文件——源文件变短
    # 后旧 00N.md 若留在盘上，_prune 的 is_file() 判活会通过，孤儿内容清不掉；
    # 且下轮扫描（摊平场景）或目录迁移时还可能被当源文件再入库。
    stale = sorted(
        d for d in out_dir.glob("[0-9][0-9][0-9].md") if int(d.stem) > len(groups)
    )
    for d in stale:
        try:
            d.unlink()
            logger.info("ingest: 清理陈旧切块 %s（新计划只有 %d 份）", d, len(groups))
        except OSError as exc:
            logger.warning("ingest: 清理陈旧切块 %s 失败：%s", d, exc)
    logger.info(
        "ingest: %s 切块为 %d 份（目录 %s，源文件保留）", p.name, len(parts), out_dir
    )
    return {"dir": out_dir, "parts": parts, "total_chunks": sum(map(len, groups))}


def plan_source_units(
    path: str | Path,
    *,
    max_chars: int = 600,
    max_chunks: int | None = None,
    overlap: int = 80,
    materialize: bool = True,
) -> dict:
    """规划单个源文件的导入单元；大文件会**自动切块落盘**。

    判定为大文件：字节数 > ``MAX_INGEST_BYTES`` 或切块数 > 单来源上限。

    返回：
    - ``units``：``[{"path","name","sha256","chunks"}]``；小文件是源文件本身
      （``name`` = 文件名），大文件是各块文件（``name`` = ``文件名/块文件名``）
    - ``split``：是否走了切块；``dir``：切块目录
    - ``materialize=False`` 时只计算不落盘，``units`` 为空、给出 ``expected_parts``
    - ``oversized``：超过 ``MAX_SPLIT_SOURCE_BYTES`` 的硬上限（拒绝切块）
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    limit = _resolve_chunk_limit(max_chunks)
    size = p.stat().st_size
    if size > MAX_SPLIT_SOURCE_BYTES:
        return {
            "source": p,
            "split": False,
            "dir": None,
            "units": [],
            "oversized": True,
            "reason": f"超过自动切块上限 {MAX_SPLIT_SOURCE_BYTES // (1024 * 1024)}MB",
            "total_chunks": 0,
        }

    text = p.read_text(encoding="utf-8", errors="replace")
    all_chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
    if size <= MAX_INGEST_BYTES and len(all_chunks) <= limit:
        return {
            "source": p,
            "split": False,
            "dir": None,
            "units": [
                {
                    "path": p,
                    "name": p.name,
                    "sha256": content_digest(text),
                    "chunks": len(all_chunks),
                }
            ],
            "oversized": False,
            "reason": None,
            "total_chunks": len(all_chunks),
        }

    groups = _group_chunks(
        all_chunks, max_chunks=limit, max_bytes=MAX_INGEST_BYTES * 4 // 5
    )
    _check_split_target(p)  # M5：预检阶段也响亮拒绝（stem 异常/目录冲突）
    if not materialize:
        # L8（REVIEW-c472e56..733f57e）：预检不再「只算份数」——按与 _write_groups
        # 完全相同的拼接规则给出份级 path/name/sha256，但**不落盘**。此前
        # materialize=True 的预检会在不启动导入的分支里留下孤儿块文件。
        # sha256 与后续 materialize 落盘的内容一致，可用于先判重后落盘。
        preview: list[dict] = []
        for index, group in enumerate(groups, start=1):
            body = "\n\n".join(group).strip() + "\n"
            preview.append(
                {
                    "path": split_dir(p) / _part_name(index),
                    "name": f"{p.name}/{_part_name(index)}",
                    "sha256": content_digest(body),
                    "chunks": len(
                        chunk_text(body, max_chars=max_chars, overlap=overlap)
                    ),
                }
            )
        return {
            "source": p,
            "split": True,
            "dir": split_dir(p),
            "units": preview,
            "oversized": False,
            "reason": None,
            "expected_parts": len(groups),
            "materialized": False,
            "total_chunks": len(all_chunks),
        }
    info = _write_groups(p, groups, max_chars=max_chars, overlap=overlap, limit=limit)
    units = [
        {
            "path": part["path"],
            "name": f"{p.name}/{part['name']}",
            "sha256": part["sha256"],
            "chunks": part["chunks"],
        }
        for part in info["parts"]
    ]
    return {
        "source": p,
        "split": True,
        "dir": info["dir"],
        "units": units,
        "oversized": False,
        "reason": None,
        "expected_parts": len(groups),
        "materialized": True,
        "total_chunks": info["total_chunks"],
    }


def scan_samples_units(
    samples_dir: str | Path,
    *,
    max_chars: int = 600,
    max_chunks: int | None = None,
    overlap: int = 80,
    materialize: bool = True,
) -> dict:
    """扫描样例目录**顶层** ``*.md``，产出导入单元（大文件先切块落盘）。

    切块产物位于 ``<stem>/`` 子目录，不在顶层 glob 范围内，因此不会被当作源文件
    重复规划。返回 ``{"sources","units","splits","oversized","error"}``。
    """
    d = Path(samples_dir)
    if not d.is_dir():
        return {
            "sources": [],
            "units": [],
            "splits": [],
            "oversized": [],
            "error": f"样例目录不存在：{d}",
        }
    files = sorted(d.glob("*.md"))
    if not files:
        return {
            "sources": [],
            "units": [],
            "splits": [],
            "oversized": [],
            "error": f"目录里没有 .md 文件：{d}",
        }

    sources: list[dict] = []
    units: list[dict] = []
    splits: list[dict] = []
    oversized: list[str] = []
    for f in files:
        try:
            plan = plan_source_units(
                f,
                max_chars=max_chars,
                max_chunks=max_chunks,
                overlap=overlap,
                materialize=materialize,
            )
        except ValueError as exc:
            # M5：无法安全切块的文件（如名为 ..md）单独报出，不拖垮整次扫描
            logger.warning("ingest: %s 无法规划导入：%s", f.name, exc)
            oversized.append(f"{f.name}（无法安全切块：{exc}）")
            continue
        if plan["oversized"]:
            oversized.append(f.name)
            continue
        units.extend(plan["units"])
        sources.append(
            {
                "source": f,
                "name": f.name,
                "split": plan["split"],
                "dir": plan["dir"],
                "units": plan["units"],
            }
        )
        if plan["split"]:
            splits.append(
                {
                    "source": f.name,
                    "dir": str(plan["dir"]),
                    "parts": plan.get("expected_parts", len(plan["units"])),
                }
            )
    return {
        "sources": sources,
        "units": units,
        "splits": splits,
        "oversized": oversized,
        "error": None,
    }


async def ingest_text(
    store,
    embedding,
    text: str,
    *,
    name: str,
    kind: str = "manual",
    location: str = "",
    max_chars: int = 600,
    scrub: bool = True,
    max_chunks: int | None = None,
) -> dict:
    """摄取一段文本。返回 ``{source_id, chunks, chunks_total, dropped, sha256, truncated}``；
    文本为空返回 ``chunks=0``。

    超限时**不再静默丢弃**：``dropped`` 报告被丢弃的块数并打 WARNING（H1 修复）。
    """
    if embedding is None:
        raise RuntimeError("embedding client unavailable")
    body = (text or "").strip()
    if not body:
        return {
            "source_id": None,
            "chunks": 0,
            "chunks_total": 0,
            "dropped": 0,
            "sha256": "",
            "truncated": False,
        }

    # 指纹基于 strip 后的原始输入（截断/脱敏之前），脚本可用同一算法独立计算
    digest = content_digest(body)

    truncated = False
    if len(body.encode("utf-8")) > MAX_INGEST_BYTES:
        body = body[: MAX_INGEST_BYTES // 4]
        truncated = True
        logger.warning(
            "ingest: %s 超过 %d 字节上限，截断到 %d 字符",
            name,
            MAX_INGEST_BYTES,
            len(body),
        )

    if scrub:
        # 管理员投喂的内容同样过一遍 PII 掩码：公共库不存身份标识。
        # 大文本的 CPU 段放线程池，避免卡住事件循环（本地 embedding 服务场景）
        body = await asyncio.to_thread(scrub_pii, body)

    # M2：显式传入的 max_chunks 同样要收敛——负值会让 all_chunks[:-5] 静默砍掉尾部，
    # 甚至切出 0 块而调用方误以为成功
    if max_chunks is None:
        limit = max_chunks_per_source()
    else:
        limit = _coerce_positive(
            max_chunks, label="max_chunks", default=MAX_CHUNKS_PER_SOURCE
        )
    all_chunks = await asyncio.to_thread(chunk_text, body, max_chars=max_chars)
    chunks = all_chunks[:limit]
    dropped = len(all_chunks) - len(chunks)
    if dropped > 0:
        # 评审 H1：此前是 [:200] 静默砍尾，调用方无从察觉；现在必须留下可观测告警
        logger.warning(
            "ingest: %s 切出 %d 块，超过单来源上限 %d，丢弃 %d 块（尾部内容不入库；"
            "可用 %s 调整上限）",
            name,
            len(all_chunks),
            limit,
            dropped,
            MAX_CHUNKS_ENV,
        )
    if not chunks:
        # M2：这里必须回报真实计数——此前写死 chunks_total=0、dropped=0，
        # 与上面刚打出的「丢弃 N 块」WARNING 自相矛盾，调用方无从判断
        return {
            "source_id": None,
            "chunks": 0,
            "chunks_total": len(all_chunks),
            "dropped": dropped,
            "sha256": digest,
            "truncated": truncated,
        }
    embeddings = await embedding.embed_many(chunks)
    source_id = await store.kb_add_source(
        name=name,
        kind=kind,
        location=location,
        meta={
            "chunks": len(chunks),
            "chunks_total": len(all_chunks),
            "dropped": dropped,
            "sha256": digest,
            "truncated": truncated,
        },
    )
    try:
        written = await store.kb_add_chunks(source_id, chunks, embeddings)
    except Exception:
        # L12：写块失败时回滚来源行，避免留下 0-chunk 孤儿（对齐 distill 的模式）
        try:
            await store.kb_delete_source(source_id)
        except Exception:
            logger.exception("ingest: rollback of source %s failed", source_id)
        raise
    logger.info(
        "ingest: source=%s kind=%s chunks=%d total=%d dropped=%d",
        source_id,
        kind,
        written,
        len(all_chunks),
        dropped,
    )
    return {
        "source_id": source_id,
        "chunks": written,
        "chunks_total": len(all_chunks),
        "dropped": dropped,
        "sha256": digest,
        "truncated": truncated,
    }


async def ingest_file(
    store,
    embedding,
    path: str | Path,
    *,
    name: str | None = None,
    kind: str = "file",
    max_chars: int = 600,
    scrub: bool = True,
    max_chunks: int | None = None,
) -> dict:
    """摄取一个本地文本文件（限工作区内路径由调用方保证）。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    if p.stat().st_size > MAX_INGEST_BYTES:
        raise ValueError(f"文件过大（>{MAX_INGEST_BYTES // 1024}KB）")
    text = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
    return await ingest_text(
        store,
        embedding,
        text,
        name=name or p.name,
        kind=kind,
        location=str(p),
        max_chars=max_chars,
        scrub=scrub,
        max_chunks=max_chunks,
    )


async def ingest_file_smart(
    store,
    embedding,
    path: str | Path,
    *,
    name: str | None = None,
    kind: str = "file",
    max_chars: int = 600,
    scrub: bool = True,
    max_chunks: int | None = None,
) -> dict:
    """单文件智能导入：小文件整份入库；大文件自动切块后**逐块入库**。

    大文件的切块文件落在同目录的 ``<stem>/`` 子目录里（源文件保留），每个块作为
    独立来源写库（来源名 ``<文件名>/<块文件名>``），因此不会再触发 2MB / 块数上限
    的截断或丢弃。

    M6（REVIEW-c472e56..733f57e）：切块路径带**指纹判重与替换**——同名同 sha256
    的来源跳过（重跑/失败重试不再成倍复制）；同名异指纹先写新、成功后删旧；
    任一块写入失败即停，保留全部旧来源（与脚本 ``_process_split_source`` 同语义）。

    返回在 ``ingest_file`` 的字段之上增加 ``split`` / ``dir`` / ``parts`` /
    ``imported`` / ``skipped`` / ``replaced``；``split=False`` 时与
    ``ingest_file`` 结果等价。
    """
    p = Path(path)
    plan = await asyncio.to_thread(
        plan_source_units,
        p,
        max_chars=max_chars,
        max_chunks=max_chunks,
        materialize=True,
    )
    if plan["oversized"]:
        raise ValueError(f"文件过大：{plan['reason']}")

    if not plan["split"]:
        result = await ingest_file(
            store,
            embedding,
            p,
            name=name,
            kind=kind,
            max_chars=max_chars,
            scrub=scrub,
            max_chunks=max_chunks,
        )
        result["split"] = False
        return result

    # L11：显式传入的 name 作为切块来源名的父级前缀（此前被静默忽略）
    parent_name = name or p.name
    existing: dict[str, list[dict]] = {}
    try:
        for s in await store.kb_list_sources(limit=100000):
            existing.setdefault(str(s.get("name") or ""), []).append(s)
    except Exception:
        logger.warning(
            "ingest: 判重取来源列表失败，按「全部新来源」处理", exc_info=True
        )

    results: list[dict] = []
    skipped = replaced = 0
    for unit in plan["units"]:
        unit_name = f"{parent_name}/{unit['path'].name}"
        olds = existing.get(unit_name, [])
        if any(
            ((s.get("meta") or {}) or {}).get("sha256") == unit["sha256"] for s in olds
        ):
            skipped += 1  # 内容未变：重跑幂等
            continue
        # 先写新：ingest_file 抛错即中止，下面统一走「失败保旧」
        results.append(
            await ingest_file(
                store,
                embedding,
                unit["path"],
                name=unit_name,
                kind=kind,
                max_chars=max_chars,
                scrub=scrub,
                max_chunks=max_chunks,
            )
        )
        # 后删旧：仅在新份成功落库后替换同名旧来源
        for old in olds:
            try:
                await store.kb_delete_source(old["id"])
                replaced += 1
            except Exception:
                logger.warning(
                    "ingest: 切块 %s 的旧来源 #%s 删除失败（留重复，不丢数据）",
                    unit_name,
                    old.get("id"),
                    exc_info=True,
                )
    return {
        "source_id": None,
        "split": True,
        "dir": str(plan["dir"]),
        "parts": len(plan["units"]),
        "imported": len(results),
        "skipped": skipped,
        "replaced": replaced,
        "chunks": sum(int(r.get("chunks") or 0) for r in results),
        "chunks_total": sum(int(r.get("chunks_total") or 0) for r in results),
        "dropped": sum(int(r.get("dropped") or 0) for r in results),
        "sha256": content_digest(p.read_text(encoding="utf-8", errors="replace")),
        "truncated": any(bool(r.get("truncated")) for r in results),
        "part_results": results,
    }
