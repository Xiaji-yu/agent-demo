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
        assert list((tmp_path / "backups").glob("p.json.*.bak"))

    def test_symlink_fail_closed(self, tmp_path):
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
