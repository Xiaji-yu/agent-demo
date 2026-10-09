"""scripts/sync_wiki_subset.py（wiki 精选子集同步）的主题测试。

来源：2026-10 wiki 外挂方案（Phase 2 精选子集入库）。锁定六个面：
1. 新页导入的来源名/kind/location/extra_meta/scrub=False（数值不被掩码）；
2. sha256 判重：内容未变跳过、变了替换；
3. H1 顺序：**先写新、成功后才删旧**；写新失败保留旧来源；
4. --prune 只删 kind="wiki" 且名前缀匹配的清单外来源（manual/distill/他站不碰）；
5. 重定向页按最终标题入库，且 prune 不误删重定向来源；
6. dry-run 零写入零删除。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agentcore.rag.ingest import content_digest
from agentcore.skills.wiki_lookup import render_wikitext

ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "sync_wiki_subset_cli", ROOT / "scripts" / "sync_wiki_subset.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_script()


class _FakeKB:
    """记录调用顺序的假知识库（够 sync_site 用的最小面）。"""

    def __init__(
        self,
        sources=(),
        *,
        add_error: Exception | None = None,
        delete_error: Exception | None = None,
    ):
        self.sources = list(sources)
        self.calls: list[tuple] = []
        self._add_error = add_error
        self._delete_error = delete_error
        self._next_id = 100

    async def add_text(
        self, text, name, kind="manual", *, location="", extra_meta=None, scrub=True
    ):
        self.calls.append(("add", name, kind, location, dict(extra_meta or {}), scrub))
        if self._add_error is not None:
            raise self._add_error
        sid = str(self._next_id)
        self._next_id += 1
        self.sources.append(
            {
                "id": sid,
                "name": name,
                "kind": kind,
                "location": location,
                "meta": extra_meta,
            }
        )
        return {"source_id": sid, "chunks": 2, "chunks_total": 2, "dropped": 0}

    async def list_sources(self, limit: int = 20):  # noqa: ARG002
        return list(self.sources)

    async def delete_source(self, source_id):
        self.calls.append(("delete", source_id))
        if self._delete_error is not None:
            raise self._delete_error
        self.sources = [s for s in self.sources if s["id"] != source_id]
        return 1


def _page(title: str, wikitext: str, *, pageid=1, revid=11) -> dict:
    return {
        "title": title,
        "pageid": pageid,
        "revid": revid,
        "ts": "2026-10-10T00:00:00Z",
        "wikitext": wikitext,
        "url": f"https://prts.wiki/w/{title}",
    }


def _digest_of(wikitext: str, title: str) -> str:
    return content_digest(render_wikitext(wikitext, title))


def _fetcher(pages: dict[str, dict]):
    async def fetch(site, titles):  # noqa: ARG001
        return {t: p for t, p in pages.items()}

    return fetch


# ------------------------------------------------------------ 导入与判重 ----


class TestImport:
    @pytest.mark.asyncio
    async def test_new_page_imported_with_wiki_kind_and_meta(self, mod):
        kb = _FakeKB()
        fetch = _fetcher(
            {"阿米娅": _page("阿米娅", "==天赋==\n指挥战术", pageid=7, revid=42)}
        )
        counts = await mod.sync_site(kb, "prts", ["阿米娅"], fetcher=fetch)
        assert counts["imported"] == 1 and counts["failed"] == 0
        (call,) = kb.calls
        assert call[0] == "add"
        assert call[1] == "wiki:prts:阿米娅"
        assert call[2] == "wiki"
        assert call[3] == "https://prts.wiki/w/阿米娅"
        assert call[4]["site"] == "prts"
        assert call[4]["pageid"] == 7 and call[4]["revid"] == 42
        assert call[5] is False, "wiki 数值内容绝不能过 scrub_pii（5 位数字会被掩码）"

    @pytest.mark.asyncio
    async def test_unchanged_page_skipped(self, mod):
        wikitext = "==天赋==\n指挥战术"
        kb = _FakeKB(
            sources=[
                {
                    "id": "9",
                    "name": "wiki:prts:阿米娅",
                    "kind": "wiki",
                    "meta": {"sha256": _digest_of(wikitext, "阿米娅")},
                }
            ]
        )
        fetch = _fetcher({"阿米娅": _page("阿米娅", wikitext)})
        counts = await mod.sync_site(kb, "prts", ["阿米娅"], fetcher=fetch)
        assert counts["skipped"] == 1
        assert kb.calls == [], "内容未变不得有任何写/删动作"

    @pytest.mark.asyncio
    async def test_changed_page_replaced(self, mod):
        kb = _FakeKB(
            sources=[
                {
                    "id": "9",
                    "name": "wiki:prts:阿米娅",
                    "kind": "wiki",
                    "meta": {"sha256": "stale-digest"},
                }
            ]
        )
        fetch = _fetcher({"阿米娅": _page("阿米娅", "==天赋==\n新版本内容", revid=43)})
        counts = await mod.sync_site(kb, "prts", ["阿米娅"], fetcher=fetch)
        assert counts["replaced"] == 1
        kinds = [c[0] for c in kb.calls]
        assert kinds == ["add", "delete"], "必须先写新、成功后再删旧"
        assert kb.calls[1][1] == "9"

    @pytest.mark.asyncio
    async def test_add_failure_keeps_old_source(self, mod):
        kb = _FakeKB(
            sources=[
                {
                    "id": "9",
                    "name": "wiki:prts:阿米娅",
                    "kind": "wiki",
                    "meta": {"sha256": "stale"},
                }
            ],
            add_error=RuntimeError("embedding 炸了"),
        )
        fetch = _fetcher({"阿米娅": _page("阿米娅", "新内容")})
        counts = await mod.sync_site(kb, "prts", ["阿米娅"], fetcher=fetch)
        assert counts["failed"] == 1
        assert ("delete", "9") not in kb.calls, "写新失败绝不能删旧来源"

    @pytest.mark.asyncio
    async def test_missing_page_reported_not_failed(self, mod):
        kb = _FakeKB()
        fetch = _fetcher({})  # 站上查无此页
        counts = await mod.sync_site(kb, "prts", ["不存在的页"], fetcher=fetch)
        assert counts["missing"] == 1 and counts["failed"] == 0
        assert kb.calls == []

    @pytest.mark.asyncio
    async def test_empty_conversion_skipped(self, mod):
        kb = _FakeKB()
        fetch = _fetcher({"空页": _page("空页", "<references/>")})
        counts = await mod.sync_site(kb, "prts", ["空页"], fetcher=fetch)
        assert counts["empty"] == 1
        assert kb.calls == []


# --------------------------------------------------------------- prune ----


class TestPrune:
    @pytest.mark.asyncio
    async def test_stale_source_removed_only_with_flag(self, mod):
        wikitext = "现行内容"
        stale = {
            "id": "5",
            "name": "wiki:prts:旧页面",
            "kind": "wiki",
            "meta": {"sha256": "x"},
        }
        fetch = _fetcher({"阿米娅": _page("阿米娅", wikitext)})
        kb = _FakeKB(sources=[stale])
        counts = await mod.sync_site(kb, "prts", ["阿米娅"], fetcher=fetch)
        assert counts["pruned"] == 0
        assert kb.sources, "默认不得删除清单外来源"

        kb = _FakeKB(sources=[dict(stale)])
        counts = await mod.sync_site(kb, "prts", ["阿米娅"], fetcher=fetch, prune=True)
        assert counts["pruned"] == 1
        assert ("delete", "5") in kb.calls

    @pytest.mark.asyncio
    async def test_prune_never_touches_other_kinds_or_sites(self, mod):
        fetch = _fetcher({"阿米娅": _page("阿米娅", "内容")})
        kb = _FakeKB(
            sources=[
                {"id": "1", "name": "wiki:prts:旧页", "kind": "manual", "meta": {}},
                {"id": "2", "name": "wiki:blhx:旧页", "kind": "wiki", "meta": {}},
                {"id": "3", "name": "手动笔记", "kind": "wiki", "meta": {}},
            ]
        )
        await mod.sync_site(kb, "prts", ["阿米娅"], fetcher=fetch, prune=True)
        deletes = [c[1] for c in kb.calls if c[0] == "delete"]
        assert deletes == [], "manual 来源/他站来源/无前缀来源一律不删"

    @pytest.mark.asyncio
    async def test_prune_respects_redirect_final_title(self, mod):
        """清单里写重定向名时，按最终标题判活，不误删已入库的最终标题来源。"""
        rec = _page("阿米娅", "内容")  # 站上返回的最终标题
        fetch = _fetcher({})  # 直接给「请求标题=别名」的形态
        fetch = _redirect_fetcher({"阿米娅的别名": rec})
        kb = _FakeKB(
            sources=[
                {
                    "id": "7",
                    "name": "wiki:prts:阿米娅",
                    "kind": "wiki",
                    "meta": {"sha256": _digest_of("内容", "阿米娅")},
                }
            ]
        )
        counts = await mod.sync_site(
            kb, "prts", ["阿米娅的别名"], fetcher=fetch, prune=True
        )
        assert counts["pruned"] == 0
        assert ("delete", "7") not in kb.calls


def _redirect_fetcher(pages: dict[str, dict]):
    """模拟 fetch_pages 的重定向别名行为：请求标题 → 最终标题记录。"""

    async def fetch(site, titles):  # noqa: ARG001
        return dict(pages)

    return fetch


# ------------------------------------------------------------- 重定向 ----


class TestRedirect:
    @pytest.mark.asyncio
    async def test_ingested_under_final_title(self, mod):
        kb = _FakeKB()
        # 清单里的「亚米娅」在站上重定向到「阿米娅」：请求标题 → 最终标题记录
        fetch = _redirect_fetcher(
            {"亚米娅": _page("阿米娅", "内容", pageid=3, revid=9)}
        )
        counts = await mod.sync_site(kb, "prts", ["亚米娅"], fetcher=fetch)
        assert counts["imported"] == 1
        assert kb.calls[0][1] == "wiki:prts:阿米娅", "必须按最终标题入库"

    @pytest.mark.asyncio
    async def test_missing_when_no_alias(self, mod):
        kb = _FakeKB()
        counts = await mod.sync_site(
            kb, "prts", ["亚米娅"], fetcher=_redirect_fetcher({})
        )
        assert counts["missing"] == 1


# ------------------------------------------------------------- dry-run ----


class TestDryRun:
    @pytest.mark.asyncio
    async def test_no_write_no_delete(self, mod):
        kb = _FakeKB(
            sources=[
                {
                    "id": "9",
                    "name": "wiki:prts:阿米娅",
                    "kind": "wiki",
                    "meta": {"sha256": "stale"},
                },
                {
                    "id": "8",
                    "name": "wiki:prts:旧页",
                    "kind": "wiki",
                    "meta": {"sha256": "x"},
                },
            ]
        )
        fetch = _fetcher({"阿米娅": _page("阿米娅", "改过的新内容")})
        counts = await mod.sync_site(
            kb, "prts", ["阿米娅"], fetcher=fetch, dry_run=True, prune=True
        )
        assert kb.calls == [], "dry-run 零写入零删除"
        assert counts["imported"] >= 1 and counts["pruned"] >= 1


# ----------------------------------------------------------- fetch_pages ----


class TestFetchPages:
    @pytest.mark.asyncio
    async def test_redirect_alias_and_missing(self, mod):
        async def api_get(site, params):  # noqa: ARG001
            titles = params["titles"].split("|")
            if "R页" in titles:
                return {
                    "query": {
                        "redirects": [{"from": "R页", "to": "正式页"}],
                        "pages": [
                            {
                                "title": "正式页",
                                "pageid": 3,
                                "revisions": [
                                    {"revid": 5, "slots": {"main": {"content": "正文"}}}
                                ],
                            }
                        ],
                    }
                }
            return {"query": {"pages": [{"title": titles[0], "missing": True}]}}

        pages = await mod.fetch_pages("prts", ["R页", "不存在的页"], api_get=api_get)
        assert "R页" in pages and "正式页" in pages
        assert pages["R页"]["title"] == "正式页"
        assert "不存在的页" not in pages

    @pytest.mark.asyncio
    async def test_batching_over_batch_size(self, mod):
        seen: list[list[str]] = []

        async def api_get(site, params):  # noqa: ARG001
            chunk = params["titles"].split("|")
            seen.append(chunk)
            return {"query": {"pages": []}}

        mod2 = mod
        old_batch = mod2.BATCH
        try:
            mod2.BATCH = 2
            await mod2.fetch_pages("prts", ["a", "b", "c", "d", "e"], api_get=api_get)
        finally:
            mod2.BATCH = old_batch
        assert [len(c) for c in seen] == [2, 2, 1]
