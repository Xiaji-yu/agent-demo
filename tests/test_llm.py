"""agentcore/llm/client：配置解析兜底。

评审 M6（REVIEW-46c85d1..6ec3f7c）：数值 env 空值曾令 _LLMCfg()（模块级单例，
import 即构造）抛 ValueError → bot 根本起不来。空值/脏值必须回落默认。
"""


class TestLLMConfigFallback:
    def test_empty_temperature_falls_back_to_default(self, monkeypatch):
        import agentcore.llm.client as lc

        monkeypatch.setenv("LLM_TEMPERATURE", "")
        cfg = lc._LLMCfg()
        assert cfg.temperature == 0.7

    def test_empty_max_tokens_falls_back_to_default(self, monkeypatch):
        import agentcore.llm.client as lc

        monkeypatch.setenv("LLM_MAX_TOKENS", "")
        cfg = lc._LLMCfg()
        assert cfg.max_tokens == 1024

    def test_dirty_values_fall_back(self, monkeypatch):
        import agentcore.llm.client as lc

        monkeypatch.setenv("LLM_TEMPERATURE", "hot")
        monkeypatch.setenv("LLM_MAX_TOKENS", "lots")
        cfg = lc._LLMCfg()
        assert cfg.temperature == 0.7
        assert cfg.max_tokens == 1024

    def test_valid_values_still_used(self, monkeypatch):
        """守卫兜底没收紧过头：合法值照常生效。"""
        import agentcore.llm.client as lc

        monkeypatch.setenv("LLM_TEMPERATURE", "0.3")
        monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
        cfg = lc._LLMCfg()
        assert cfg.temperature == 0.3
        assert cfg.max_tokens == 2048
