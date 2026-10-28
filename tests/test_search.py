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

    # ---- 新增预装格式（2026-10-29）：baidu / zhipu / serper / exa ----
    # 每格式锁三件事：默认端点、鉴权/请求体、响应字段映射与日期归一。
    # 来源：各官方文档（千帆 /v2/ai_search/web_search、智谱 /paas/v4/web_search、
    # serper.dev /search、exa /search）。

    @pytest.mark.asyncio
    async def test_baidu_uses_default_endpoint_and_payload(self):
        """百度格式：默认端点 qianfan.baidubce.com，Bearer，messages 形态请求。"""
        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(
            id="baidu", name="百度", format="baidu", api_key="bce-v3/ALTAK-x/y"
        )
        posted = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "references": [
                        {
                            "title": "标题A",
                            "url": "https://x.cn/a",
                            "content": "摘要A",
                            "date": "2026-03-20 11:00:00",
                        }
                    ]
                }

        class FakeClient:
            async def post(self, url, headers=None, json=None, **kw):
                posted["url"] = url
                posted["headers"] = headers
                posted["json"] = json
                return FakeResp()

        out = await search._search_baidu(provider, FakeClient(), "q", 3, "zh-CN")
        assert posted["url"] == "https://qianfan.baidubce.com/v2/ai_search/web_search"
        # key 形如 bce-v3/ALTAK-.../...（含 /），整串进 Bearer
        assert posted["headers"]["Authorization"] == "Bearer bce-v3/ALTAK-x/y"
        assert posted["json"] == {
            "messages": [{"role": "user", "content": "q"}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": 3}],
        }
        assert "标题A" in out and "https://x.cn/a" in out
        assert "2026-03-20" in out  # "YYYY-MM-DD HH:MM:SS" 归一为日期

    @pytest.mark.asyncio
    async def test_baidu_error_body_raises(self):
        """百度 200 + 错误体（code/message）必须判失败——否则 fallback 失效。

        错误体刻意带上 references: []：只靠「结果列表存在」判成功的实现会
        放过这种形态，必须由 code 检查拦下（变异可检出，防恒真）。
        """

        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(id="baidu", name="百度", format="baidu", api_key="k")

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"code": "InvalidParameter", "message": "bad", "references": []}

        class FakeClient:
            async def post(self, url, **kw):
                return FakeResp()

        with pytest.raises(ValueError):
            await search._search_baidu(provider, FakeClient(), "q", 3, "zh-CN")

    @pytest.mark.asyncio
    async def test_zhipu_uses_default_endpoint_and_payload(self):
        """智谱格式：默认端点 open.bigmodel.cn，Bearer，search_query 形态。"""
        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(
            id="zhipu", name="智谱", format="zhipu", api_key="sk-zp"
        )
        posted = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "search_result": [
                        {
                            "title": "标题A",
                            "link": "https://x.cn/a",
                            "content": "摘要A",
                            "publish_date": "2026-03-20 11:00:00",
                        }
                    ]
                }

        class FakeClient:
            async def post(self, url, headers=None, json=None, **kw):
                posted["url"] = url
                posted["headers"] = headers
                posted["json"] = json
                return FakeResp()

        out = await search._search_zhipu(provider, FakeClient(), "q", 3, "zh-CN")
        assert posted["url"] == "https://open.bigmodel.cn/api/paas/v4/web_search"
        assert posted["headers"]["Authorization"] == "Bearer sk-zp"
        assert posted["json"] == {
            "search_query": "q",
            "search_engine": "search_std",
            "search_intent": False,
            "count": 3,
        }
        assert "标题A" in out and "https://x.cn/a" in out
        assert "2026-03-20" in out  # publish_date 归一为日期

    @pytest.mark.asyncio
    async def test_serper_uses_default_endpoint_and_payload(self):
        """serper 格式：默认端点 google.serper.dev，Bearer，请求 {q, num}。"""
        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(
            id="serper", name="Serper", format="serper", api_key="sk-sp"
        )
        posted = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "organic": [
                        {
                            "title": "标题A",
                            "link": "https://x.cn/a",
                            "snippet": "摘要A",
                            "date": "3 days ago",
                        }
                    ]
                }

        class FakeClient:
            async def post(self, url, headers=None, json=None, **kw):
                posted["url"] = url
                posted["headers"] = headers
                posted["json"] = json
                return FakeResp()

        out = await search._search_serper(provider, FakeClient(), "q", 3, "zh-CN")
        assert posted["url"] == "https://google.serper.dev/search"
        assert posted["headers"]["Authorization"] == "Bearer sk-sp"
        assert posted["json"] == {"q": "q", "num": 3}
        assert "标题A" in out and "https://x.cn/a" in out
        assert "3 days ago" not in out  # 非 ISO 日期不猜，不进输出

    @pytest.mark.asyncio
    async def test_exa_uses_default_endpoint_and_payload(self):
        """exa 格式：默认端点 api.exa.ai，x-api-key 头（非 Bearer），{query, numResults}。"""
        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(id="exa", name="Exa", format="exa", api_key="sk-exa")
        posted = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "results": [
                        {
                            "title": "标题A",
                            "url": "https://x.cn/a",
                            "text": "摘要A",
                            "publishedDate": "2026-03-20T00:00:00Z",
                        }
                    ]
                }

        class FakeClient:
            async def post(self, url, headers=None, json=None, **kw):
                posted["url"] = url
                posted["headers"] = headers
                posted["json"] = json
                return FakeResp()

        out = await search._search_exa(provider, FakeClient(), "q", 3, "zh-CN")
        assert posted["url"] == "https://api.exa.ai/search"
        assert posted["headers"]["x-api-key"] == "sk-exa"
        assert "Authorization" not in posted["headers"]  # exa 不走 Bearer
        assert posted["json"] == {"query": "q", "numResults": 3}
        assert "标题A" in out and "https://x.cn/a" in out
        assert "2026-03-20" in out

    @pytest.mark.asyncio
    async def test_search_items_one_dispatches_new_formats(self):
        """_search_items_one 新格式分派：baidu 不掉进 bocha 的 else 分支。

        回归锚点：新格式漏分派时会被 else 按 bocha 请求/解析——请求体错误、
        解析必失败（bocha 判 data 字段），用例以「正确解析出 title」锁分派。
        """
        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(id="baidu", name="百度", format="baidu", api_key="k")
        posted = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "references": [
                        {"title": "T", "url": "https://x.cn/a", "content": "S"}
                    ]
                }

        class FakeClient:
            async def post(self, url, headers=None, json=None, **kw):
                posted["url"] = url
                posted["json"] = json
                return FakeResp()

        items = await search._search_items_one(provider, FakeClient(), "q", 2, "zh-CN")
        assert posted["url"] == "https://qianfan.baidubce.com/v2/ai_search/web_search"
        assert "search_source" in posted["json"]  # baidu 形态，不是 bocha 的 count
        assert items == [
            {"title": "T", "url": "https://x.cn/a", "snippet": "S", "date": ""}
        ]

    @pytest.mark.asyncio
    async def test_firecrawl_uses_default_endpoint_and_payload(self):
        """firecrawl 格式：默认端点 api.firecrawl.dev/v2/search，Bearer，{query, limit}。"""
        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(
            id="firecrawl", name="Firecrawl", format="firecrawl", api_key="fc-key"
        )
        posted = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "success": True,
                    "data": {
                        "web": [
                            {
                                "title": "标题A",
                                "url": "https://x.cn/a",
                                "description": "摘要A",
                                "position": 1,
                            }
                        ],
                        "news": [],  # 分组响应：只取 web 组
                    },
                }

        class FakeClient:
            async def post(self, url, headers=None, json=None, **kw):
                posted["url"] = url
                posted["headers"] = headers
                posted["json"] = json
                return FakeResp()

        out = await search._search_firecrawl(provider, FakeClient(), "q", 3, "zh-CN")
        assert posted["url"] == "https://api.firecrawl.dev/v2/search"
        assert posted["headers"]["Authorization"] == "Bearer fc-key"
        assert posted["json"] == {"query": "q", "limit": 3}
        assert "标题A" in out and "https://x.cn/a" in out
        assert "摘要A" in out  # description 映射进 snippet

    @pytest.mark.asyncio
    async def test_firecrawl_success_false_raises(self):
        """firecrawl 200 + success:false 判失败（官方 SDK 同口径）。

        错误体刻意带 data.web 列表：只靠「列表存在」判成功的实现会放过
        这种形态，必须由 success 检查拦下（变异可检出，防恒真）。
        """

        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(
            id="firecrawl", name="Firecrawl", format="firecrawl", api_key="k"
        )

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"success": False, "data": {"web": []}}

        class FakeClient:
            async def post(self, url, **kw):
                return FakeResp()

        with pytest.raises(ValueError):
            await search._search_firecrawl(provider, FakeClient(), "q", 3, "zh-CN")

    @pytest.mark.asyncio
    async def test_search_items_one_dispatches_firecrawl(self):
        """_search_items_one 的 firecrawl 分派：请求/解析都按 v2 形态，不掉进 bocha else。"""
        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(
            id="firecrawl", name="Firecrawl", format="firecrawl", api_key="k"
        )
        posted = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "success": True,
                    "data": {
                        "web": [
                            {"title": "T", "url": "https://x.cn/a", "description": "S"}
                        ]
                    },
                }

        class FakeClient:
            async def post(self, url, headers=None, json=None, **kw):
                posted["url"] = url
                posted["json"] = json
                return FakeResp()

        items = await search._search_items_one(provider, FakeClient(), "q", 2, "zh-CN")
        assert posted["url"] == "https://api.firecrawl.dev/v2/search"
        assert posted["json"] == {"query": "q", "limit": 2}
        assert items == [
            {"title": "T", "url": "https://x.cn/a", "snippet": "S", "date": ""}
        ]

    def test_env_provider_accepts_new_formats(self, monkeypatch):
        """env 回落路径认新格式：SEARCH_PROVIDER=baidu 不再回落 bocha。"""
        monkeypatch.setenv("SEARCH_PROVIDER", "baidu")
        monkeypatch.setenv("SEARCH_API_KEY", "k")
        cfg = search._load_search_config()
        assert cfg.provider == "baidu"


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
        """L6（REVIEW-1b16fa7..9c2d3c1）：条数统一夹紧 [1, 20]。

        旧实现把 LLM 传入的 0/负值原样透传给上游 API（count=-3 只会换回
        4xx）；现在夹紧到 1（与 _env_max_results 同口径）。
        """
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
        assert "max_results=1" in result  # 0 夹紧到 1，不透传
        result = await search_web("test", max_results=-3)
        assert "max_results=1" in result
        result = await search_web("test", max_results=99)
        assert "max_results=20" in result  # 上限夹紧


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


class TestReviewSearchFixes:
    """REVIEW-1b16fa7..9c2d3c1 搜索侧修复回归（M4/M7/M8/L5/L7）。"""

    @pytest.mark.asyncio
    async def test_m4_multi_uses_providers_not_env(self, monkeypatch):
        """M4：纯 JSON 配置（env 无 SEARCH_API_KEY）下 search_multi 不得误报
        「未配置」——旧实现按 env 判空，工具循环里该技能必失败。"""
        import asyncio  # noqa: F401  # 与慢路径用例共用解释器语义

        from agentcore.skills.search_providers import SearchProvider

        monkeypatch.delenv("SEARCH_API_KEY", raising=False)
        monkeypatch.setattr(search, "search_enabled", lambda: True)
        p1 = SearchProvider(id="p1", name="p1", format="bocha", api_key="k1")
        monkeypatch.setattr(search, "_load_providers", lambda: [p1])

        async def fake_items(q, n):
            return [{"title": "T", "url": "https://x/1", "snippet": "S"}]

        monkeypatch.setattr(search, "search_items", fake_items)
        out = await search.search_multi(["q1"])
        assert "未配置" not in out
        assert "https://x/1" in out

    @pytest.mark.asyncio
    async def test_m7_http200_error_body_triggers_fallback(self, monkeypatch):
        """M7：200 + 错误体（{"error": ...}）必须抛异常切下一个源，而不是当
        「成功空结果」——容错在最常见故障形态下失效。"""
        from agentcore.skills.search_providers import SearchProvider

        p1 = SearchProvider(id="p1", name="p1", format="tavily", api_key="k1")
        p2 = SearchProvider(id="p2", name="p2", format="tavily", api_key="k2")
        monkeypatch.setattr(search, "_load_providers", lambda: [p1, p2])
        monkeypatch.setattr(search, "search_enabled", lambda: True)
        calls = []

        async def fake_items_one(provider, client, query, max_results, lang):
            calls.append(provider.id)
            if provider.id == "p1":
                raise ValueError("上游返回异常响应（缺 results 字段或含 error）")
            return [{"title": "T", "url": "https://x/2", "snippet": "S"}]

        monkeypatch.setattr(search, "_search_items_one", fake_items_one)
        out = await search.search_items("q")
        assert calls == ["p1", "p2"]
        assert out[0]["url"] == "https://x/2"

    @pytest.mark.asyncio
    async def test_m7_bocha_missing_data_raises(self):
        """M7：bocha 200 + {"error": ...} 在 _search_bocha 内即判失败（抛异常）。"""
        from agentcore.skills.search_providers import SearchProvider

        provider = SearchProvider(id="p1", name="p1", format="bocha", api_key="k")

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"error": "quota exceeded"}

        class FakeClient:
            async def post(self, url, **kw):
                return FakeResp()

        with pytest.raises(ValueError):
            await search._search_bocha(provider, FakeClient(), "q", 3, "zh-CN")

    @pytest.mark.asyncio
    async def test_m8_total_timeout_caps_wall_clock(self, monkeypatch):
        """M8：串行 fallback 有全局墙钟——两个源都慢挂时总时长受预算约束，
        而不是 N×单源 timeout（跑在工具循环热路径）。"""
        import asyncio

        from agentcore.skills.search_providers import SearchProvider

        monkeypatch.setenv("AGENT_SEARCH_TOTAL_TIMEOUT_S", "0.3")
        monkeypatch.setattr(search, "search_enabled", lambda: True)
        p1 = SearchProvider(id="p1", name="p1", format="bocha", api_key="k1")
        p2 = SearchProvider(id="p2", name="p2", format="bocha", api_key="k2")
        monkeypatch.setattr(search, "_load_providers", lambda: [p1, p2])

        async def slow_one(provider, client, query, max_results, lang):
            await asyncio.sleep(5)  # 单源 15s timeout 拦不住的慢
            return "never"

        monkeypatch.setattr(search, "_search_one", slow_one)
        out = await search.search_web("q")
        assert "搜索超时" in out

    def test_l5_env_fallback_accepts_stepfun(self, monkeypatch):
        """L5：env 回落层也认 stepfun（迁移/web 写面都认，唯独回落静默改
        bocha——请求格式随之错、必然 4xx）。"""
        monkeypatch.setenv("SEARCH_PROVIDER", "stepfun")
        monkeypatch.setenv("SEARCH_API_KEY", "k")
        search._state = None
        try:
            cfg = search._load_search_config()
            assert cfg.provider == "stepfun"
            providers = search._env_single_provider()
            assert providers[0].format == "stepfun"
        finally:
            search._state = None

    @pytest.mark.asyncio
    async def test_l7_all_fail_hides_endpoint_url(self, monkeypatch):
        """L7：全挂报错只回异常类型名——str(e) 含自配端点 URL/内网 host，
        进 prompt 后模型可能向用户复述内网拓扑。"""
        import httpx

        from agentcore.skills.search_providers import SearchProvider

        p1 = SearchProvider(
            id="p1",
            name="p1",
            format="bocha",
            base_url="http://10.0.0.9:9999",
            api_key="k1",
        )
        monkeypatch.setattr(search, "_load_providers", lambda: [p1])
        monkeypatch.setattr(search, "search_enabled", lambda: True)

        async def boom(provider, client, query, max_results, lang):
            raise httpx.HTTPStatusError(
                "502",
                request=httpx.Request("POST", provider.base_url),
                response=httpx.Response(502),
            )

        monkeypatch.setattr(search, "_search_one", boom)
        out = await search.search_web("q")
        assert "10.0.0.9" not in out
        assert "HTTPStatusError" in out
