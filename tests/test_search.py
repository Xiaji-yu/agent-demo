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
