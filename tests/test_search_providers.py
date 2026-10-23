"""多搜索源配置（search_providers.py）：校验 / 迁移 / load-save / 0600。

与 test_llm_providers.py 同口径，但按搜索场景简化（无 active、format 取代
protocol、无 temperature/max_tokens）。
"""

import stat

import pytest

from agentcore.skills import search_providers as sp
from agentcore.skills.search_providers import (
    SearchProvider,
    SearchProviderConfig,
    SearchProviderConfigError,
)


@pytest.fixture
def providers_file(tmp_path, monkeypatch):
    p = tmp_path / "search-providers.json"
    monkeypatch.setenv("AGENT_SEARCH_PROVIDERS_FILE", str(p))
    return p


class TestValidate:
    def test_valid(self):
        p = sp.validate_provider(
            {"id": "bocha", "name": "博查", "format": "bocha", "api_key": "k"}
        )
        assert p.format == "bocha" and p.api_key == "k"

    def test_missing_key(self):
        with pytest.raises(SearchProviderConfigError, match="api_key"):
            sp.validate_provider({"name": "x", "format": "bocha"})

    def test_bad_format(self):
        with pytest.raises(SearchProviderConfigError, match="format"):
            sp.validate_provider({"name": "x", "format": "google", "api_key": "k"})

    def test_bad_base_url(self):
        with pytest.raises(SearchProviderConfigError, match="base_url"):
            sp.validate_provider(
                {"name": "x", "format": "bocha", "api_key": "k", "base_url": "ftp://x"}
            )

    def test_public_no_key(self):
        p = SearchProvider(
            id="a",
            name="a",
            format="bocha",
            base_url="https://api.bocha.ai/v1",
            api_key="sk-secret",
        )
        pub = p.public()
        assert "api_key" not in pub and "sk-secret" not in str(pub)
        assert pub["key_set"] is True
        assert pub["endpoint_host"] == "api.bocha.ai"
        # admin_view 补完整 endpoint（不含 key）
        assert p.admin_view()["endpoint"] == "https://api.bocha.ai/v1"
        assert "api_key" not in p.admin_view()

    def test_stepfun_format_valid(self):
        p = sp.validate_provider(
            {"name": "StepFun", "format": "stepfun", "api_key": "sk-sf"}
        )
        assert p.format == "stepfun"


class TestMigrate:
    def test_from_env_with_key(self, monkeypatch):
        monkeypatch.setenv("SEARCH_PROVIDER", "bocha")
        monkeypatch.setenv("SEARCH_API_KEY", "sk-x")
        monkeypatch.setenv("SEARCH_BASE_URL", "https://proxy.example.com/v1")
        cfg = sp.migrate_from_env()
        assert len(cfg.providers) == 1
        assert cfg.providers[0].format == "bocha"
        assert cfg.providers[0].base_url == "https://proxy.example.com/v1"

    def test_from_env_no_key_empty(self, monkeypatch):
        monkeypatch.setenv("SEARCH_PROVIDER", "bocha")
        monkeypatch.delenv("SEARCH_API_KEY", raising=False)
        cfg = sp.migrate_from_env()
        assert cfg.providers == []  # 未配置搜索不生成源

    def test_invalid_provider_fallback(self, monkeypatch):
        monkeypatch.setenv("SEARCH_PROVIDER", "invalid")
        monkeypatch.setenv("SEARCH_API_KEY", "k")
        cfg = sp.migrate_from_env()
        assert cfg.providers[0].format == "bocha"


class TestLoadSave:
    def test_round_trip_and_0600(self, providers_file):
        cfg = SearchProviderConfig(
            providers=[
                SearchProvider(id="bocha", name="博查", format="bocha", api_key="k1"),
                SearchProvider(
                    id="tavily", name="Tavily", format="tavily", api_key="k2"
                ),
            ]
        )
        sp.save(cfg)
        assert stat.S_IMODE(providers_file.stat().st_mode) == 0o600
        loaded = sp.load()
        assert len(loaded.providers) == 2
        assert loaded.by_id("tavily").format == "tavily"

    def test_load_missing_raises(self, providers_file):
        with pytest.raises(FileNotFoundError):
            sp.load()

    def test_load_corrupt_raises(self, providers_file):
        providers_file.write_text("{bad json", encoding="utf-8")
        with pytest.raises(SearchProviderConfigError):
            sp.load()

    def test_save_duplicate_id_rejected(self, providers_file):
        cfg = SearchProviderConfig(
            providers=[
                SearchProvider(id="a", name="a", format="bocha", api_key="k"),
                SearchProvider(id="a", name="b", format="bocha", api_key="k"),
            ]
        )
        with pytest.raises(SearchProviderConfigError, match="重复"):
            sp.save(cfg)

    def test_backup_created(self, providers_file):
        sp.save(
            SearchProviderConfig(
                providers=[
                    SearchProvider(id="a", name="a", format="bocha", api_key="k")
                ]
            )
        )
        sp.save(
            SearchProviderConfig(
                providers=[
                    SearchProvider(id="a", name="a2", format="bocha", api_key="k")
                ]
            )
        )
        bks = list(
            (providers_file.parent / "backups").glob("search-providers.json.*.bak")
        )
        assert len(bks) >= 1


class TestEnsureMigrated:
    def test_migrate_creates_file(self, providers_file, monkeypatch):
        monkeypatch.setenv("SEARCH_PROVIDER", "bocha")
        monkeypatch.setenv("SEARCH_API_KEY", "sk-x")
        cfg = sp.ensure_migrated()
        assert providers_file.exists()
        assert len(cfg.providers) == 1

    def test_no_key_no_file(self, providers_file, monkeypatch):
        monkeypatch.delenv("SEARCH_API_KEY", raising=False)
        cfg = sp.ensure_migrated()
        assert not providers_file.exists()  # 未配置搜索不落盘
        assert cfg.providers == []

    def test_idempotent(self, providers_file, monkeypatch):
        monkeypatch.setenv("SEARCH_API_KEY", "sk-x")
        sp.ensure_migrated()
        # 改 env 后再次 ensure：文件已存在，不覆盖
        monkeypatch.setenv("SEARCH_API_KEY", "sk-y")
        cfg = sp.ensure_migrated()
        assert cfg.providers[0].api_key == "sk-x"  # 保留首次迁移的值


def test_empty_env_falls_back_to_default_path(monkeypatch):
    """M18（REVIEW-1b16fa7..9c2d3c1）：env 空串（`KEY=` 的 dotenv 注入形态）
    回落默认路径——旧实现 `os.getenv(ENV, default)` 空串不回落 → Path("") = "."
    → load 抛 IsADirectoryError 而非 FileNotFoundError，回落分支误判。"""
    from pathlib import Path

    monkeypatch.setenv("AGENT_SEARCH_PROVIDERS_FILE", "")
    assert sp.providers_path() == Path("data/search-providers.json")
    monkeypatch.setenv("AGENT_SEARCH_PROVIDERS_FILE", "   ")
    assert sp.providers_path() == Path("data/search-providers.json")
