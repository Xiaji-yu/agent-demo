"""把人工挑选的 MediaWiki 页面清单同步进公共知识库（kind="wiki"）。用法：

    .venv/bin/python scripts/sync_wiki_subset.py                 # 同步全部站点
    .venv/bin/python scripts/sync_wiki_subset.py --site prts     # 只同步一个站点
    .venv/bin/python scripts/sync_wiki_subset.py --dry-run       # 只打印将做什么
    .venv/bin/python scripts/sync_wiki_subset.py --prune         # 清理清单外的 wiki 来源

页面清单：``data/wiki_subset/<site>.txt``（站点 id 与 wiki_lookup.SITES 一致），
一行一个页面标题，``#`` 开头是注释。站点表内置 prts（明日方舟）/blhx（碧蓝
航线），wikitext→Markdown 转换复用 ``agentcore.skills.wiki_lookup``。

判重与替换：来源名 ``wiki:<site>:<标题>``，按**转换后正文的 sha256** 判重
（页面编辑一次 revid 必变，但没改到正文就不必重灌；指纹口径与 kb_samples 一致）。
内容变了**自动替换**——先写新、成功后删旧（对齐 ingest_kb_samples 的 H1 顺序）。
这里不做「需 --replace 人工确认」：wiki 来源的全部内容都可从站点再生，没有
用户独创数据，自动替换正是「免维护」目标的一部分（``--dry-run`` 可预览）。

``--prune`` 才清理「清单里已没有」的旧来源（默认不动：清缩小清单不该静默删库），
且只删 kind="wiki" 且名前缀匹配的来源，绝不碰 manual/distill/sample。

**有意不 scrub_pii**：公开游戏 wiki 的数值（HP 45000 之类 5 位以上数字）会被
数字掩码破坏；内容来自部署者挑选的公开页面而非聊天，PII 风险由清单把关，
检索注入时的不可信围栏不受影响。

任一页面写入失败退出码 1（失败页保留旧来源，其余页面继续）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUBSET_DIR = PROJECT_ROOT / "data" / "wiki_subset"
BATCH = 20  # 每次带正文的多页批查请求的标题数（对服务器友好）


def _exc_brief(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else f"{type(exc).__name__}: {exc!r}"


def read_titles(path: Path) -> list[str]:
    """读页面清单：一行一个标题，# 注释，去重保序。"""
    if not path.is_file():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        title = line.strip()
        if not title or title.startswith("#") or title in seen:
            continue
        seen.add(title)
        out.append(title)
    return out


async def fetch_pages(site: str, titles: list[str], *, api_get=None) -> dict[str, dict]:
    """批量抓取页面当前 wikitext，返回 {请求标题: page}。

    - 重定向自动解开：MediaWiki ``redirects=1`` 会把页面以**最终标题**返回，
      这里按 ``query.redirects`` 建别名——请求标题与最终标题都能查到同一条
      记录（记录内 ``title`` 始终是最终标题，来源名按最终标题入库）。
    - 缺页不进返回值，由 sync_site 按清单逐个报告。
    - api_get 可注入（测试用），缺省用 wiki_lookup 的限速/退避实现。
    """
    if api_get is None:
        from agentcore.skills.wiki_lookup import _api_get

        api_get = _api_get
    from agentcore.skills.wiki_lookup import SITES

    base = SITES[site]["base"]
    pages: dict[str, dict] = {}
    for i in range(0, len(titles), BATCH):
        chunk = titles[i : i + BATCH]
        data = await api_get(
            site,
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "titles": "|".join(chunk),
                "redirects": "1",
                "prop": "revisions",
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
            },
        )
        q = data.get("query") or {}
        for p in q.get("pages") or []:
            title = p.get("title") or ""
            if "missing" in p or not title:
                continue
            rev = (p.get("revisions") or [{}])[0]
            content = ((rev.get("slots") or {}).get("main") or {}).get("content")
            pages[title] = {
                "title": title,
                "pageid": p.get("pageid"),
                "revid": rev.get("revid"),
                "ts": rev.get("timestamp"),
                "wikitext": content,
                "url": base + urllib.parse.quote(title, safe=""),
            }
        for r in q.get("redirects") or []:
            frm, to = r.get("from"), r.get("to")
            if frm and to and to in pages:
                pages.setdefault(frm, pages[to])
    return pages


def _wiki_sources_of_site(sources: list[dict], site: str) -> dict[str, list[dict]]:
    """某站点的全部 wiki 来源，按来源名分组（同名可能多条，历史遗留）。"""
    prefix = f"wiki:{site}:"
    grouped: dict[str, list[dict]] = {}
    for s in sources:
        name = str(s.get("name") or "")
        if name.startswith(prefix) and s.get("kind") == "wiki":
            grouped.setdefault(name, []).append(s)
    return grouped


async def sync_site(
    kb,
    site: str,
    titles: list[str],
    *,
    dry_run: bool = False,
    prune: bool = False,
    fetcher=None,
) -> dict:
    """同步一个站点的页面清单到 KB，返回计数增量。

    键：imported / replaced / skipped / missing / empty / failed / pruned。
    """
    from agentcore.rag.ingest import content_digest
    from agentcore.skills.wiki_lookup import render_wikitext

    if fetcher is None:
        fetcher = fetch_pages

    counts = {
        "imported": 0,
        "replaced": 0,
        "skipped": 0,
        "missing": 0,
        "empty": 0,
        "failed": 0,
        "pruned": 0,
    }
    if not titles:
        return counts

    # 先抓取：prune 的判活口径是**最终标题**（清单里的重定向请求标题在站上
    # 解析成最终标题后入库，按请求标题判活会把同一来源误判成清单外删掉）
    pages = await fetcher(site, titles)

    sources = await kb.list_sources(limit=100000)
    grouped = _wiki_sources_of_site(sources, site)

    # --prune：最终标题不在本次清单解析结果里的来源
    if prune:
        wanted = set()
        for t in titles:
            rec = pages.get(t)
            final = (rec or {}).get("title") or t
            wanted.add(f"wiki:{site}:{final}")
        stale = [
            s for name, olds in grouped.items() if name not in wanted for s in olds
        ]
        for s in stale:
            if dry_run:
                print(f"[dry-run] 将删除清单外来源 #{s['id']} {s['name']}")
            else:
                try:
                    await kb.delete_source(s["id"])
                except Exception as exc:
                    print(
                        f"⚠ 清单外来源 #{s['id']} {s['name']} 删除失败：{_exc_brief(exc)}"
                    )
                    continue
                print(f"🧹 已删除清单外来源 #{s['id']} {s['name']}")
            counts["pruned"] += 1
        if stale:
            # 删除后重取快照，避免判重/替换用到已删来源
            sources = await kb.list_sources(limit=100000)
            grouped = _wiki_sources_of_site(sources, site)

    for title in titles:
        rec = pages.get(title)
        if rec is None:
            print(f"⚠ 站点上不存在（清单里请删除或改名）：{title}")
            counts["missing"] += 1
            continue
        name = f"wiki:{site}:{title}"
        final_title = rec.get("title") or title
        if title != final_title:
            # 请求标题是重定向：按最终标题入库，避免同名来源在两次运行间漂移
            print(f"↪ {title} → 重定向到 {final_title}，按最终标题入库")
            name = f"wiki:{site}:{final_title}"
        olds = grouped.get(name, [])
        try:
            md = await asyncio.to_thread(
                render_wikitext, rec.get("wikitext") or "", title
            )
        except Exception as exc:
            counts["failed"] += 1
            print(f"✗ {name}: wikitext 转换失败 {_exc_brief(exc)}")
            continue
        if not md.strip():
            counts["empty"] += 1
            print(f"⏭ {name}: 转换后正文为空，跳过")
            continue
        digest = content_digest(md)
        unchanged = any(
            ((s.get("meta") or {}) or {}).get("sha256") == digest for s in olds
        )
        superseded = [
            s for s in olds if ((s.get("meta") or {}) or {}).get("sha256") != digest
        ]
        if unchanged and not superseded:
            print(f"⏭ {name}: 内容未变，跳过")
            counts["skipped"] += 1
            continue
        if dry_run:
            action = "替换" if olds else "导入"
            print(f"[dry-run] 将{action} {name}（{len(md)} 字符）")
            counts["imported"] += 1
            continue
        try:
            result = await kb.add_text(
                md,
                name=name,
                kind="wiki",
                location=rec.get("url") or "",
                extra_meta={
                    "site": site,
                    "title": name.split(":", 2)[2],
                    "pageid": rec.get("pageid"),
                    "revid": rec.get("revid"),
                },
                scrub=False,  # 游戏数值不能被数字掩码破坏；见模块 docstring
            )
        except Exception as exc:
            counts["failed"] += 1
            suffix = "（旧来源未删除，原有内容仍在库中）" if olds else ""
            print(f"✗ {name}: {_exc_brief(exc)}{suffix}")
            continue
        chunks = int(result.get("chunks") or 0)
        dropped = int(result.get("dropped") or 0)
        note = f"，丢弃 {dropped} 块" if dropped else ""
        if olds:
            counts["replaced"] += 1
            print(f"♻ {name}: 入库 {chunks} 块{note}，替换旧来源")
        else:
            counts["imported"] += 1
            print(f"✓ {name}: 入库 {chunks} 块{note}")
        # H1 顺序：新来源已入库才删旧；删除失败只是留重复，不丢数据
        for old in superseded:
            try:
                await kb.delete_source(old["id"])
            except Exception as exc:
                print(
                    f"⚠ {name}: 旧来源 #{old['id']} 删除失败（留重复，不丢数据）："
                    f"{_exc_brief(exc)}"
                )
                continue
    return counts


async def main(*, site: str = "", dry_run: bool = False, prune: bool = False) -> None:
    # 与 backup_db / ingest_kb_samples 同语义：shell 显式导出优先，.env 只补缺
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    import yaml

    cfg_path = os.getenv("AGENT_CONFIG", str(PROJECT_ROOT / "config.yaml"))
    with open(cfg_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    from agentcore.embedding import load_embedding_client_from_env
    from agentcore.memory.store import InMemoryMemoryStore, PgMemoryStore
    from agentcore.skills.wiki_lookup import enabled_sites

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

    sites = [site.strip().lower()] if site else enabled_sites()
    if not sites:
        print("没有启用的站点（AGENT_WIKI_SITES=none？）")
        sys.exit(0)

    if not os.getenv("EMBEDDING_BASE_URL"):
        print(
            "⚠ 未配置 EMBEDDING_BASE_URL：将用本地 hash 降级向量（语义召回弱）。\n"
            "  正式灌库建议先配云端/本地 embedding 服务（见 .env.example 的 Embedding 段）。"
        )
    if dry_run:
        print("（dry-run）只打印将执行的动作\n")

    total = {
        k: 0
        for k in (
            "imported",
            "replaced",
            "skipped",
            "missing",
            "empty",
            "failed",
            "pruned",
        )
    }
    any_titles = False
    for sid in sites:
        list_path = SUBSET_DIR / f"{sid}.txt"
        titles = read_titles(list_path)
        if not titles:
            print(f"— {sid}: 无清单（{list_path}），跳过")
            continue
        any_titles = True
        print(f"=== {sid}: {len(titles)} 个页面 ===")
        counts = await sync_site(kb, sid, titles, dry_run=dry_run, prune=prune)
        for k, v in counts.items():
            total[k] += v
        print(
            f"— {sid} 汇总：导入 {counts['imported']} / 替换 {counts['replaced']} / "
            f"未变 {counts['skipped']} / 缺页 {counts['missing']} / 失败 {counts['failed']}"
        )
    print(
        f"\n总计：导入 {total['imported']} / 替换 {total['replaced']} / 未变 {total['skipped']} / "
        f"缺页 {total['missing']} / 空正文 {total['empty']} / 清单外清理 {total['pruned']} / "
        f"失败 {total['failed']}"
    )
    if not any_titles:
        print(
            f"提示：还没有任何清单。创建 {SUBSET_DIR}/<site>.txt（一行一个页面标题）后重跑。"
        )
    if total["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="把 data/wiki_subset/<site>.txt 清单里的 wiki 页面同步进知识库"
    )
    parser.add_argument("--site", default="", help="只同步指定站点（缺省全部启用站点）")
    parser.add_argument(
        "--prune", action="store_true", help="清理清单里已没有的 wiki 来源"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    args = parser.parse_args()
    asyncio.run(main(site=args.site, dry_run=args.dry_run, prune=args.prune))
