"""多 provider 存储：校验 / 迁移 / 落 agent.json / 0600 / 永不回显 key / 回滚。

2026-10-28 需求③后，provider 配置存在 ``config/agent.json`` 的 ``LLM_PROVIDERS``
数组里（活跃位 ``LLM_ACTIVE``），不再是独立 JSON 文件。本文件覆盖：

- ``validate_provider`` 的字段校验（与存储介质无关）
- ``migrate_from_env`` 的 env → provider 列表推导
- ``save`` 只补丁两个键、**不破坏 agent.json 其余键与键序**
- ``load`` 无数组时抛 ``FileNotFoundError``（让调用方回落 env）
- 旧独立文件的一次性迁移（不覆盖、不删除旧文件）
- 0600 / 备份 / symlink fail-closed / 写后校验回滚
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcore.llm import providers as ps


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    """清掉会改变路径解析的两个 env，避免跨用例与开发者本机污染。"""
    for key in ("AGENT_CONFIG_JSON", "AGENT_LLM_PROVIDERS_FILE"):
        monkeypatch.delenv(key, raising=False)
    yield
    for key in ("AGENT_CONFIG_JSON", "AGENT_LLM_PROVIDERS_FILE"):
        os.environ.pop(key, None)


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


def _write_agent_json(path: Path, **extra) -> Path:
    """造一份只有旧键（无 LLM_PROVIDERS）的 agent.json。"""
    data = {"_说明": "备注键", "LLM_BASE_URL": "http://x", "HOST": "0.0.0.0", **extra}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return path


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
                {
                    "name": "x",
                    "protocol": "openai",
                    "endpoint": "ftp://h",
                    "model": "m",
                },
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
                    "id": "-x",
                },
                "id",
            ),
            (
                {
                    "name": "x",
                    "protocol": "openai",
                    "endpoint": "http://h",
                    "model": "m",
                    "id": "a" * 33,
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
        assert models[0] == "m1"
        assert "m2" in models and "fb" in models
        assert cfg.active == cfg.providers[0].id

    def test_ids_unique_and_slugged(self):
        cfg = ps.migrate_from_env(_env_cfg())
        ids = [p.id for p in cfg.providers]
        assert len(ids) == len(set(ids))
        assert all(ps._ID_RE.match(i) for i in ids)

    def test_ensure_migrated_uses_existing_array(self, tmp_path):
        """agent.json 已有数组 → 原样返回，绝不用 env 覆盖。"""
        path = tmp_path / "agent.json"
        ps.save(_store_cfg(), path)
        data = json.loads(path.read_text(encoding="utf-8"))
        data[ps.PROVIDERS_KEY][0]["model"] = "zzz"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        second = ps.ensure_migrated(_env_cfg(), path)

        assert second.providers[0].model == "zzz", "已存在的配置不得被覆盖"

    def test_env_fallback_does_not_write(self, tmp_path):
        """无数组且无旧文件 → 从 env 派生，但**不落盘**（agent.json 保持干净）。"""
        path = _write_agent_json(tmp_path / "agent.json")

        cfg = ps.ensure_migrated(_env_cfg(), path)

        assert [p.model for p in cfg.providers][0] == "m1"
        assert ps.PROVIDERS_KEY not in json.loads(path.read_text(encoding="utf-8"))

    def test_legacy_file_migrated_once(self, tmp_path, monkeypatch):
        """旧独立文件 → 并入 agent.json；旧文件不删、数组不被二次覆盖。"""
        legacy = tmp_path / "old-providers.json"
        legacy.write_text(
            json.dumps(
                {
                    "active": "old",
                    "providers": [
                        {
                            "id": "old",
                            "name": "Old",
                            "protocol": "openai",
                            "base_url": "http://old",
                            "model": "m-old",
                            "api_key": "k-old",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("AGENT_LLM_PROVIDERS_FILE", str(legacy))
        path = _write_agent_json(tmp_path / "agent.json")

        cfg = ps.ensure_migrated(None, path)

        assert [p.id for p in cfg.providers] == ["old"]
        merged = json.loads(path.read_text(encoding="utf-8"))
        assert [p["id"] for p in merged[ps.PROVIDERS_KEY]] == ["old"]
        assert merged[ps.ACTIVE_KEY] == "old"
        assert legacy.is_file(), "旧文件只读不删（用户可能想留底）"
        # 其余键没被抹掉
        assert merged["_说明"] == "备注键" and merged["HOST"] == "0.0.0.0"


def _store_cfg():
    return ps.ProviderConfig(
        active="a",
        providers=[
            ps.Provider("a", "A", "openai", "http://a", "m-a", "secret-a"),
            ps.Provider("b", "B", "anthropic", "http://b", "m-b", "secret-b"),
        ],
    )


class TestStore:
    def _cfg(self):
        return _store_cfg()

    def test_roundtrip_0600_and_public_no_key(self, tmp_path):
        path = tmp_path / "agent.json"
        ps.save(self._cfg(), path)
        if os.name == "posix":
            assert ps.restore_mode(path) == 0o600, "含 key 的文件必须 0600"
        loaded = ps.load(path)
        assert [p.id for p in loaded.providers] == ["a", "b"]
        assert loaded.active == "a"
        blob = str([p.public() for p in loaded.providers])
        assert "secret-a" not in blob and "secret-b" not in blob
        assert loaded.providers[0].public()["key_set"] is True

    def test_save_preserves_other_keys_and_order(self, tmp_path):
        """只补丁两个键：其余键与键序都必须原样保留。"""
        path = _write_agent_json(tmp_path / "agent.json", LLM_MODEL="step-1")

        ps.save(self._cfg(), path)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["_说明"] == "备注键"
        assert data["LLM_BASE_URL"] == "http://x"
        assert data["HOST"] == "0.0.0.0"
        assert data["LLM_MODEL"] == "step-1"
        assert [p["id"] for p in data[ps.PROVIDERS_KEY]] == ["a", "b"]
        assert data[ps.ACTIVE_KEY] == "a"
        # 新键追加在末尾，不动原有键序
        keys = list(data)
        assert keys[:4] == ["_说明", "LLM_BASE_URL", "HOST", "LLM_MODEL"]
        assert set(keys[4:]) == {ps.PROVIDERS_KEY, ps.ACTIVE_KEY}

    def test_save_updates_existing_keys_in_place(self, tmp_path):
        """数组已存在时原位更新，不新增重复键。"""
        path = tmp_path / "agent.json"
        ps.save(self._cfg(), path)
        before = list(json.loads(path.read_text(encoding="utf-8")))

        cfg = self._cfg()
        cfg.active = "b"
        ps.save(cfg, path)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert list(data) == before, "键序不变、不重复"
        assert data[ps.ACTIVE_KEY] == "b"

    def test_load_without_array_raises_not_found(self, tmp_path):
        """没有 LLM_PROVIDERS = 没有多 provider 配置 → FileNotFoundError（回落 env）。"""
        path = _write_agent_json(tmp_path / "agent.json")
        with pytest.raises(FileNotFoundError):
            ps.load(path)

    def test_load_bad_json_raises_config_error(self, tmp_path):
        path = tmp_path / "agent.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ps.ProviderConfigError, match="invalid"):
            ps.load(path)

    def test_load_non_object_raises(self, tmp_path):
        path = tmp_path / "agent.json"
        path.write_text("[1,2]", encoding="utf-8")
        with pytest.raises(ps.ProviderConfigError, match="not_object"):
            ps.load(path)

    def test_backup_written(self, tmp_path):
        path = tmp_path / "agent.json"
        ps.save(self._cfg(), path)
        cfg = self._cfg()
        cfg.active = "b"
        ps.save(cfg, path)
        backups = list((tmp_path / "backups").glob("agent.json.*.bak"))
        assert backups
        if os.name == "posix":
            assert ps.restore_mode(backups[0]) == 0o600, "备份含 key，必须 0600"

    def test_symlink_fail_closed(self, tmp_path, symlinks_supported):
        target = tmp_path / "real.json"
        ps.save(self._cfg(), target)
        path = tmp_path / "link.json"
        path.symlink_to(target)
        with pytest.raises(ps.ProviderConfigError, match="symlink"):
            ps.save(self._cfg(), path)

    def test_verify_failure_rolls_back(self, tmp_path, monkeypatch):
        path = tmp_path / "agent.json"
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
        cfg = self._cfg()
        cfg.providers[1].id = "a"
        with pytest.raises(ps.ProviderConfigError, match="重复"):
            ps.save(cfg, tmp_path / "agent.json")

    def test_active_must_exist(self, tmp_path):
        cfg = self._cfg()
        cfg.active = "nope"
        with pytest.raises(ps.ProviderConfigError, match="active"):
            ps.save(cfg, tmp_path / "agent.json")

    def test_empty_providers_rejected_on_load(self, tmp_path):
        path = tmp_path / "agent.json"
        path.write_text(
            json.dumps({ps.PROVIDERS_KEY: [], ps.ACTIVE_KEY: "a"}), encoding="utf-8"
        )
        with pytest.raises(ps.ProviderConfigError, match="empty"):
            ps.load(path)


class TestPaths:
    def test_agent_json_path_default(self):
        assert ps.agent_json_path() == Path("config/agent.json")

    def test_agent_json_path_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_CONFIG_JSON", "custom/agent.json")
        assert ps.agent_json_path() == Path("custom/agent.json")

    def test_agent_json_path_blank_env_falls_back(self, monkeypatch):
        """M18 同型：env 空串/纯空白必须回落默认，不能变成 Path("") = "."。"""
        for raw in ("", "   "):
            monkeypatch.setenv("AGENT_CONFIG_JSON", raw)
            assert ps.agent_json_path() == Path("config/agent.json")

    def test_legacy_path_none_when_unset(self):
        assert ps.legacy_path() is None

    def test_legacy_path_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_LLM_PROVIDERS_FILE", "data/state/llm-providers.json")
        assert ps.legacy_path() == Path("data/state/llm-providers.json")
