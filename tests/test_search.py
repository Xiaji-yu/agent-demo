import pytest

import agentcore.skills.search as search
from agentcore.skills.search import SearchConfig, get_search_client, search_web


class TestSearchConfig:
    def test_default(self):
        cfg = SearchConfig()
        assert cfg.provider == "bocha"
        assert cfg.max_results == 5

    def test_invalid_provider_fallback(self, monkeypatch):
        monkeypatch.setenv("SEARCH_PROVIDER", "invalid")
        # reset cached state：_state 是模块级缓存，先前的用例（如调过
        # register_builtin_skills 的测试）可能已按真实 env 填充过 provider
        search._state = None
        try:
            state = get_search_client()
            assert state.cfg.provider == "bocha"
        finally:
            search._state = None


class TestSearchBaseUrl:
    """SEARCH_BASE_URL 自定义端点 + 多搜索源 fallback 容错（2026-10-21）。

    留空用 provider 默认端点；多源按顺序 fallback——一个失败切下一个。
    """

    def test_load_reads_base_url(self, monkeypatch):
        monkeypatch.setenv("SEARCH_PROVIDER", "bocha")
        monkeypatch.setenv("SEARCH_API_KEY", "k")
        monkeypatch.setenv("SEARCH_BASE_URL", "https://proxy.example.com/v1/web-search")
        search._state = None
        try:
            cfg = search._load_search_config()
            assert cfg.base_url == "https://proxy.example.com/v1/web-search"
        finally:
            search._state = None

    @pytest.mark.asyncio
    async def test_bocha_uses_custom_endpoint(self):
        """设了 base_url，_search_bocha 发到自定义端点而非硬编码默认。"""
        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(
            id="bocha",
            name="bocha",
            format="bocha",
            base_url="https://proxy.example.com/v1/web-search",
            api_key="k",
        )
        posted = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"web_pages": []}}

        class FakeClient:
            async def post(self, url, **kw):
                posted["url"] = url
                return FakeResp()

        await search._search_bocha(provider, FakeClient(), "q", 3, "zh-CN")
        assert posted["url"] == "https://proxy.example.com/v1/web-search"

    @pytest.mark.asyncio
    async def test_default_endpoint_when_unset(self):
        """未设 base_url 时用硬编码默认端点（回归：不破坏默认行为）。"""
        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(id="bocha", name="bocha", format="bocha", api_key="k")
        posted = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"web_pages": []}}

        class FakeClient:
            async def post(self, url, **kw):
                posted["url"] = url
                return FakeResp()

        await search._search_bocha(provider, FakeClient(), "q", 3, "zh-CN")
        assert posted["url"] == "https://api.bocha.ai/v1/web-search"

    @pytest.mark.asyncio
    async def test_fallback_to_next_provider(self, monkeypatch):
        """多源 fallback：第一个源失败，自动切第二个成功。"""
        from agentcore.skills.search_providers import SearchProvider

        p1 = SearchProvider(id="p1", name="p1", format="bocha", api_key="k1")
        p2 = SearchProvider(id="p2", name="p2", format="bocha", api_key="k2")
        monkeypatch.setattr(search, "_load_providers", lambda: [p1, p2])
        monkeypatch.setattr(search, "search_enabled", lambda: True)
        calls = []

        async def fake_one(provider, client, query, max_results, lang):
            calls.append(provider.id)
            if provider.id == "p1":
                raise RuntimeError("p1 挂了")
            return "p2 的结果"

        monkeypatch.setattr(search, "_search_one", fake_one)
        result = await search.search_web("q")
        assert result == "p2 的结果"
        assert calls == ["p1", "p2"]

    @pytest.mark.asyncio
    async def test_all_providers_fail(self, monkeypatch):
        """全部源失败 → 返回失败提示（不抛异常）。"""
        from agentcore.skills.search_providers import SearchProvider

        p1 = SearchProvider(id="p1", name="p1", format="bocha", api_key="k1")
        monkeypatch.setattr(search, "_load_providers", lambda: [p1])
        monkeypatch.setattr(search, "search_enabled", lambda: True)

        async def fake_one(provider, client, query, max_results, lang):
            raise RuntimeError("boom")

        monkeypatch.setattr(search, "_search_one", fake_one)
        result = await search.search_web("q")
        assert "所有搜索源均失败" in result

    @pytest.mark.asyncio
    async def test_stepfun_uses_default_endpoint_and_bearer(self):
        """stepfun 格式：默认端点 api.stepfun.com/v1/search，Bearer 鉴权，请求 {query, n}。"""
        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(
            id="stepfun", name="StepFun", format="stepfun", api_key="sk-sf"
        )
        posted = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "results": [
                        {
                            "url": "https://x.cn/a",
                            "title": "标题A",
                            "snippet": "摘要A",
                            "time": "2026-03-20T00:00:00",
                        }
                    ]
                }

        class FakeClient:
            async def post(self, url, headers=None, json=None, **kw):
                posted["url"] = url
                posted["headers"] = headers
                posted["json"] = json
                return FakeResp()

        out = await search._search_stepfun(provider, FakeClient(), "q", 3, "zh-CN")
        assert posted["url"] == "https://api.stepfun.com/v1/search"
        assert posted["headers"]["Authorization"] == "Bearer sk-sf"
        assert posted["json"] == {"query": "q", "n": 3}
        assert "标题A" in out and "https://x.cn/a" in out
        assert "2026-03-20" in out  # time 归一为日期

    def test_items_stepfun_parses_fields(self):
        from agentcore.skills.search import _items_stepfun

        items = _items_stepfun(
            {
                "results": [
                    {
                        "url": "https://x.cn/a",
                        "title": "T",
                        "snippet": "S",
                        "time": "2026-03-20T00:00:00",
                        "content": "完整内容",
                    }
                ]
            }
        )
        assert items[0]["title"] == "T"
        assert items[0]["url"] == "https://x.cn/a"
        assert items[0]["snippet"] == "S"  # 优先 snippet
        assert items[0]["date"] == "2026-03-20"


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

        async def fake_bocha(provider, client, query, max_results, lang):
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


class TestEnvMaxResultsFallback:
    """SEARCH_MAX_RESULTS 脏值不炸 get_search_client（重审 P2 回归）。"""

    def test_dirty_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SEARCH_MAX_RESULTS", "abc")
        cfg = search._load_search_config()
        assert cfg.max_results == 5

    def test_value_clamped(self, monkeypatch):
        monkeypatch.setenv("SEARCH_MAX_RESULTS", "999")
        assert search._load_search_config().max_results == 20
        monkeypatch.setenv("SEARCH_MAX_RESULTS", "0")
        assert search._load_search_config().max_results == 1
        monkeypatch.setenv("SEARCH_MAX_RESULTS", "12")
        assert search._load_search_config().max_results == 12


class TestSearchEnabledSwitch:
    """SEARCH_ENABLED=0：即时生效，且不得触碰 client/key。"""

    @pytest.mark.asyncio
    async def test_disabled_short_circuits_without_client(self, monkeypatch):
        from agentcore.skills import search

        monkeypatch.setenv("SEARCH_ENABLED", "0")
        monkeypatch.setenv("SEARCH_API_KEY", "k")

        def boom():
            raise AssertionError("关闭时不得获取搜索客户端")

        monkeypatch.setattr(search, "get_search_client", boom)
        assert "已关闭" in await search.search_web("x")
        assert "已关闭" in await search.search_multi(["x"])
        assert await search.search_items("x") == []

    def test_default_enabled(self, monkeypatch):
        from agentcore.skills import search

        monkeypatch.delenv("SEARCH_ENABLED", raising=False)
        assert search.search_enabled() is True
