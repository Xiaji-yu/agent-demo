"""多 provider 存储：迁移 / 校验 / 0600 / 永不回显 key / 写后校验回滚。"""

from types import SimpleNamespace

import pytest

from agentcore.llm import providers as ps


def _entry(model, base="", key=""):
    return SimpleNamespace(model=model, base_url=base, api_key=key)


def _env_cfg():
    return SimpleNamespace(
        model="m1",
        base_url="http://main",
        api_key="k-main",
        active=_entry("m1"),
        models=(_entry("m2", "http://h2", "k2"), _entry("m1")),
        fallback_model="fb",
        fallback_base_url="http://fb",
        fallback_api_key="k-fb",
    )


class TestValidate:
    def test_enabled_string_is_parsed(self):
        base = {
            "name": "x",
            "protocol": "openai",
            "endpoint": "http://h",
            "model": "m",
        }
        assert ps.validate_provider({**base, "enabled": "false"}).enabled is False
        assert ps.validate_provider({**base, "enabled": "0"}).enabled is False
        assert ps.validate_provider({**base, "enabled": "true"}).enabled is True

    def test_ok(self):
        p = ps.validate_provider(
            {
                "id": "main",
                "name": "Main",
                "protocol": "anthropic",
                "endpoint": "https://api.anthropic.com/",
                "model": "claude-x",
                "api_key": "sk-ant",
                "temperature": "0.3",
                "max_tokens": "512",
            }
        )
        assert p.base_url == "https://api.anthropic.com"
        assert p.temperature == 0.3 and p.max_tokens == 512

    @pytest.mark.parametrize(
        "bad,reason",
        [
            (
                {"name": "x", "protocol": "grpc", "endpoint": "http://h", "model": "m"},
                "protocol",
            ),
            (
                {"name": "x", "protocol": "openai", "endpoint": "h", "model": "m"},
                "http",
            ),
            (
                {
                    "name": "x",
                    "protocol": "openai",
                    "endpoint": "http://h",
                    "model": "",
                },
                "model",
            ),
            (
                {
                    "name": "x",
                    "protocol": "openai",
                    "endpoint": "http://h",
                    "model": "m",
                    "temperature": "9",
                },
                "temperature",
            ),
            (
                {
                    "name": "x",
                    "protocol": "openai",
                    "endpoint": "http://h",
                    "model": "m",
                    "max_tokens": "0",
                },
                "max_tokens",
            ),
            (
                {
                    "id": "BAD ID",
                    "name": "x",
                    "protocol": "openai",
                    "endpoint": "http://h",
                    "model": "m",
                },
                "id",
            ),
        ],
    )
    def test_rejects(self, bad, reason):
        with pytest.raises(ps.ProviderConfigError, match=reason):
            ps.validate_provider(bad)


class TestMigration:
    def test_migrates_main_pool_and_fallback(self):
        cfg = ps.migrate_from_env(_env_cfg())
        models = [p.model for p in cfg.providers]
        assert models == ["m1", "m2", "fb"], "主 + 池 + 独立备用线，按此顺序"
        assert cfg.active == cfg.providers[0].id
        # 池条目自带端点/key 要保留
        assert cfg.providers[1].base_url == "http://h2"
        assert cfg.providers[1].api_key == "k2"

    def test_ids_unique_and_slugged(self):
        cfg = ps.migrate_from_env(_env_cfg())
        ids = [p.id for p in cfg.providers]
        assert len(ids) == len(set(ids))
        assert all(ps._ID_RE.match(i) for i in ids)

    def test_ensure_migrated_idempotent(self, tmp_path):
        path = tmp_path / "p.json"
        ps.ensure_migrated(_env_cfg(), path)
        path.write_text(
            path.read_text(encoding="utf-8").replace("m1", "zzz"), encoding="utf-8"
        )
        second = ps.ensure_migrated(_env_cfg(), path)
        assert second.providers[0].model == "zzz", "已存在的文件不得被覆盖"


class TestEnvDrift:
    """`.env` 与 provider 文件不一致时的启动告警（2026-10-28 部署反馈）。

    迁移是一次性的：文件一旦生成，`chat()` 只走 provider 链，env 的
    LLM_BASE_URL/API_KEY/MODEL 从此被忽略。用户改完 .env 重启发现没生效，
    过去没有任何线索——现在启动期按字段名告警（值不进日志）。
    """

    def _seed(self, tmp_path, cfg=None):
        path = tmp_path / "p.json"
        ps.ensure_migrated(cfg or _env_cfg(), path)
        return ps.load(path), path

    def _env(self, monkeypatch, *, key="k-main", model="m1", base="http://main"):
        for name, value in (
            ("LLM_MODEL", model),
            ("LLM_BASE_URL", base),
            ("LLM_API_KEY", key),
        ):
            monkeypatch.setenv(name, value)

    def test_reports_changed_field(self, tmp_path, monkeypatch):
        file_cfg, _ = self._seed(tmp_path)
        self._env(monkeypatch, key="k-new")
        changed = _env_cfg()
        changed.api_key = "k-new"
        assert ps.env_drift(file_cfg, changed) == ["api_key"]

    def test_reports_every_changed_field(self, tmp_path, monkeypatch):
        file_cfg, _ = self._seed(tmp_path)
        self._env(monkeypatch, key="k-new", model="m9", base="http://new")
        changed = _env_cfg()
        changed.api_key, changed.model, changed.base_url = "k-new", "m9", "http://new"
        changed.active = _entry("m9")
        assert ps.env_drift(file_cfg, changed) == ["model", "base_url", "api_key"]

    def test_ignores_placeholder_env(self, tmp_path, monkeypatch):
        """`...` 是「没填」，不是「填了个叫 ... 的值」：web 里配 key、.env 留占位
        是正常部署，不能每次启动都告警（噪音会把真告警淹掉）。"""
        file_cfg, _ = self._seed(tmp_path)
        self._env(monkeypatch, key="...", model="...", base="...")
        # 迁移源也必须跟着是占位值，否则测不出「按 env 原值过滤」这条
        placeholder = _env_cfg()
        placeholder.model = placeholder.base_url = placeholder.api_key = "..."
        placeholder.active = _entry("...")
        assert ps.env_drift(file_cfg, placeholder) == []

    def test_ignores_unset_env(self, tmp_path, monkeypatch):
        """env 全没设时 `_CFG` 是代码默认值（step-1-flash 之类），与文件不同
        属正常——不设 guard 的话每次启动都会误报「LLM_MODEL 不一致」。"""
        file_cfg, _ = self._seed(tmp_path)
        for name in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        defaulted = _env_cfg()
        defaulted.model, defaulted.base_url, defaulted.api_key = (
            "code-default-model",
            "",
            "",
        )
        defaulted.active = _entry("code-default-model")
        assert ps.env_drift(file_cfg, defaulted) == []

    def test_ensure_migrated_logs_warning_without_secret(
        self, tmp_path, monkeypatch, caplog
    ):
        _, path = self._seed(tmp_path)
        self._env(monkeypatch, key="k-new-secret-value")
        changed = _env_cfg()
        changed.api_key = "k-new-secret-value"
        with caplog.at_level("WARNING", logger="agentcore.llm.providers"):
            ps.ensure_migrated(changed, path)
        text = caplog.text
        assert "不一致" in text and "LLM_API_KEY" in text
        assert "删掉该文件" in text, "告警必须给出可执行的正门/退路"
        assert "k-new-secret-value" not in text, "日志不得出现 key 值"

    def test_no_warning_when_consistent(self, tmp_path, monkeypatch, caplog):
        _, path = self._seed(tmp_path)
        self._env(monkeypatch)
        with caplog.at_level("WARNING", logger="agentcore.llm.providers"):
            ps.ensure_migrated(_env_cfg(), path)
        assert "不一致" not in caplog.text


class TestStore:
    def _cfg(self):
        return ps.ProviderConfig(
            active="a",
            providers=[
                ps.Provider("a", "A", "openai", "http://a", "m-a", "secret-a"),
                ps.Provider("b", "B", "anthropic", "http://b", "m-b", "secret-b"),
            ],
        )

    def test_roundtrip_0600_and_public_no_key(self, tmp_path):
        path = tmp_path / "p.json"
        ps.save(self._cfg(), path)
        assert ps.restore_mode(path) == 0o600
        loaded = ps.load(path)
        assert [p.id for p in loaded.providers] == ["a", "b"]
        blob = str([p.public() for p in loaded.providers])
        assert "secret-a" not in blob and "secret-b" not in blob
        assert loaded.providers[0].public()["key_set"] is True

    def test_backup_written(self, tmp_path):
        path = tmp_path / "p.json"
        ps.save(self._cfg(), path)
        cfg = self._cfg()
        cfg.active = "b"
        ps.save(cfg, path)
        backups = list((tmp_path / "backups").glob("p.json.*.bak"))
        assert backups
        assert ps.restore_mode(backups[0]) == 0o600, "备份含 key，必须 0600"

    def test_symlink_fail_closed(self, tmp_path, symlinks_supported):
        target = tmp_path / "real.json"
        ps.save(self._cfg(), target)
        path = tmp_path / "link.json"
        path.symlink_to(target)
        with pytest.raises(ps.ProviderConfigError, match="symlink"):
            ps.save(self._cfg(), path)

    def test_verify_failure_rolls_back(self, tmp_path, monkeypatch):
        path = tmp_path / "p.json"
        ps.save(self._cfg(), path)
        original = path.read_text(encoding="utf-8")
        real_load = ps.load

        def broken(p=None):
            cfg = real_load(p)
            cfg.active = "other"
            return cfg

        monkeypatch.setattr(ps, "load", broken)
        cfg = self._cfg()
        cfg.active = "b"
        with pytest.raises(ps.ProviderConfigError, match="rolled_back"):
            ps.save(cfg, path)
        assert path.read_text(encoding="utf-8") == original

    def test_duplicate_id_rejected(self, tmp_path):
        path = tmp_path / "p.json"
        cfg = self._cfg()
        cfg.providers[1].id = "a"
        with pytest.raises(ps.ProviderConfigError, match="重复"):
            ps.save(cfg, path)

    def test_active_must_exist(self, tmp_path):
        cfg = self._cfg()
        cfg.active = "nope"
        with pytest.raises(ps.ProviderConfigError, match="active"):
            ps.save(cfg, tmp_path / "p.json")


def test_empty_env_falls_back_to_default_path(monkeypatch):
    """M18（REVIEW-1b16fa7..9c2d3c1，存量同型随本轮一并修）：env 空串回落
    默认路径（与 totp._secret_path / search_providers.providers_path 同修法）。"""
    from pathlib import Path

    monkeypatch.setenv("AGENT_LLM_PROVIDERS_FILE", "")
    assert ps.providers_path() == Path("data/llm-providers.json")
