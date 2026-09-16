import pytest

from agentcore.skills.search import SearchConfig, get_search_client, search_web


class TestSearchConfig:
    def test_default(self):
        cfg = SearchConfig()
        assert cfg.provider == "bocha"
        assert cfg.max_results == 5

    def test_invalid_provider_fallback(self, monkeypatch):
        monkeypatch.setenv("SEARCH_PROVIDER", "invalid")
        state = get_search_client()
        assert state.cfg.provider == "bocha"


class TestSearchWeb:
    @pytest.mark.asyncio
    async def test_no_api_key(self, monkeypatch):
        monkeypatch.delenv("SEARCH_API_KEY", raising=False)
        # reset cached state
        import agentcore.skills.search as search_mod

        search_mod._state = None
        result = await search_web("test")
        assert "未配置" in result

    @pytest.mark.asyncio
    async def test_max_results_zero(self, monkeypatch):
        monkeypatch.setenv(
            "SEARCH_PROVIDER", "bocha"
        )  # 显式锁定 provider，防止 .env 里的 tavily 触发真实网络
        monkeypatch.setenv("SEARCH_API_KEY", "fake")
        monkeypatch.setenv("SEARCH_MAX_RESULTS", "0")
        import agentcore.skills.search as search_mod

        search_mod._state = None

        async def fake_bocha(cfg, client, query, max_results):
            return f"mock result for max_results={max_results}"

        monkeypatch.setattr(search_mod, "_search_bocha", fake_bocha)
        result = await search_web("test", max_results=0)
        assert "0" in result


class TestClipResults:
    def test_empty(self):
        from agentcore.skills.search import _clip_results

        assert _clip_results([]) == "未找到相关结果。"

    def test_item_truncated(self):
        from agentcore.skills.search import _MAX_ITEM_CHARS, _TRUNC_NOTE, _clip_results

        long = "- " + "x" * (_MAX_ITEM_CHARS + 500)
        out = _clip_results([long])
        assert "…" in out
        assert len(out) <= _MAX_ITEM_CHARS + len(_TRUNC_NOTE) + 2

    def test_total_truncated(self):
        from agentcore.skills.search import _MAX_TOTAL_CHARS, _TRUNC_NOTE, _clip_results

        lines = [f"- item{i}: {'y' * 3000}" for i in range(20)]
        out = _clip_results(lines)
        assert len(out) <= _MAX_TOTAL_CHARS + len(_TRUNC_NOTE)
        assert "截断" in out  # 明确告知 LLM 结果被截断

    def test_url_kept_when_summary_clipped(self):
        from agentcore.skills.search import _clip_results

        url = "https://example.com/very/long/path/" + "z" * 80
        line = f"- 标题: {url}\n  " + "摘要" * 2000
        out = _clip_results([line])
        assert url in out
        assert "…" in out


class TestSearchDate:
    """时效性：搜索结果必须带上发布日期，模型才能核对新旧。

    实测根因：模型内部知识有截止时间，生成的 query 常沿用旧年份
    （带「2025」搜回 2025 年的过时新闻），而返回结构此前把日期字段丢了。
    """

    def test_norm_date_iso_and_plain(self):
        from agentcore.skills.search import _norm_date

        assert _norm_date("2026-09-01T08:30:00Z") == "2026-09-01"
        assert _norm_date("2026-09-01") == "2026-09-01"
        assert _norm_date(None) == ""
        assert _norm_date("") == ""
        assert _norm_date("最近") == ""
        assert _norm_date(20260901) == ""  # 非 ISO 串不猜

    def test_items_tavily_extracts_published_date(self):
        from agentcore.skills.search import _items_tavily

        items = _items_tavily(
            {
                "results": [
                    {
                        "title": "新闻A",
                        "url": "https://a.cn/1",
                        "content": "摘要A",
                        "published_date": "2026-09-10T12:00:00Z",
                    },
                    {"title": "新闻B", "url": "https://b.cn/2", "content": "摘要B"},
                ]
            }
        )
        assert items[0]["date"] == "2026-09-10"
        assert items[1]["date"] == ""

    def test_items_bocha_extracts_date(self):
        from agentcore.skills.search import _items_bocha

        items = _items_bocha(
            {
                "data": {
                    "web_pages": [
                        {"name": "新闻A", "url": "https://a.cn/1", "date": "2026-08-01"}
                    ]
                }
            }
        )
        assert items[0]["date"] == "2026-08-01"

    def test_fmt_item_stamps_date_when_present(self):
        from agentcore.skills.search import _fmt_item

        with_date = _fmt_item(
            {"title": "新闻A", "url": "https://a.cn/1", "date": "2026-09-10"}
        )
        assert "新闻A（2026-09-10）" in with_date
        without = _fmt_item({"title": "新闻B", "url": "https://b.cn/2", "date": ""})
        assert "（" not in without.split(":")[0]
