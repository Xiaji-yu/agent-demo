"""agentcore/config_json.py 单测：JSON 配置加载进 os.environ。

存在的理由：`.env` 这类 dotfile 在某些 NAS 上建不住（文件管理器反复把它建成目录，
而 Docker 的单文件 bind mount 在源不存在时会自动补一个目录，然后报
`not a directory`）。改成「整个 config/ 目录挂进容器 + 里面放 agent.json」之后，
dotfile 和单文件挂载两类坑同时消失。

锁定的语义：
* 键名与 `.env` 完全一致，下游 `os.getenv` 无感；
* `_` 开头的键是备注，忽略；
* 值统一转成字符串（JSON 数字/布尔/数组 → env 字符串）；
* **不覆盖已存在的 environ**（真实环境变量 > JSON > .env 的优先级模型）；
* 文件缺失/读不懂都不抛异常，只带病报告并列出缺失必填键——启动不能因此崩，
  但要响（2026-10-12 同类事故：静默比响故障更难查）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentcore import config_json

_REQUIRED = config_json.REQUIRED_KEYS


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """本用例集会写真实 os.environ，逐个清理防跨用例泄漏。"""
    keys = _REQUIRED + (
        "AGENT_TEST_JSON_KEY",
        "AGENT_CONFIG_JSON",
        "AGENT_ENV_FILE",
        "HOST",
        "PORT",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in _REQUIRED + ("AGENT_TEST_JSON_KEY", "AGENT_ENV_FILE", "HOST", "PORT"):
        os.environ.pop(key, None)


def _write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class TestToEnvValue:
    def test_string_passthrough(self):
        assert config_json.to_env_value("abc") == "abc"

    def test_numbers_and_bools_become_strings(self):
        assert config_json.to_env_value(8080) == "8080"
        assert config_json.to_env_value(0.7) == "0.7"
        # 布尔走 env_flag 认得的 true/false，不是 Python 的 True
        assert config_json.to_env_value(True) == "true"
        assert config_json.to_env_value(False) == "false"

    def test_list_and_dict_are_json_encoded(self):
        assert config_json.to_env_value(["1", "2"]) == '["1", "2"]'
        assert json.loads(config_json.to_env_value({"a": 1})) == {"a": 1}

    def test_none_means_unset(self):
        assert config_json.to_env_value(None) is None


class TestLoad:
    def test_missing_file_does_not_raise_and_reports(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        report = config_json.load()
        assert report.source is None
        assert report.loaded == ()
        assert report.error is None
        assert set(report.missing_required) == set(_REQUIRED)

    def test_loads_keys_into_environ(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_json(
            tmp_path / "config" / "agent.json",
            {"HOST": "0.0.0.0", "PORT": 8080, "AGENT_TEST_JSON_KEY": "from-json"},
        )

        report = config_json.load()

        assert os.environ["HOST"] == "0.0.0.0"
        assert os.environ["PORT"] == "8080"  # 数字转字符串
        assert os.environ["AGENT_TEST_JSON_KEY"] == "from-json"
        assert set(report.loaded) == {"HOST", "PORT", "AGENT_TEST_JSON_KEY"}

    def test_underscore_keys_are_notes_and_ignored(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_json(
            tmp_path / "config" / "agent.json",
            {"_note": "这是备注", "_readme": "x", "AGENT_TEST_JSON_KEY": "v"},
        )

        report = config_json.load()

        assert os.environ["AGENT_TEST_JSON_KEY"] == "v"
        assert "_note" not in os.environ
        assert "_note" not in report.loaded

    def test_does_not_override_existing_environ(self, tmp_path, monkeypatch):
        """优先级：真实环境变量 > JSON。load_dotenv 的 override=False 同模型。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AGENT_TEST_JSON_KEY", "from-shell")
        _write_json(
            tmp_path / "config" / "agent.json", {"AGENT_TEST_JSON_KEY": "from-json"}
        )

        report = config_json.load()

        assert os.environ["AGENT_TEST_JSON_KEY"] == "from-shell"
        assert "AGENT_TEST_JSON_KEY" in report.skipped_existing

    def test_json_wins_over_dotenv_because_it_runs_first(self, tmp_path, monkeypatch):
        """bot.py 的顺序：capture → JSON → load_dotenv。JSON 先写进 environ，
        load_dotenv(override=False) 就盖不掉它——JSON > .env。"""
        monkeypatch.chdir(tmp_path)
        from dotenv import load_dotenv

        _write_json(
            tmp_path / "config" / "agent.json", {"AGENT_TEST_JSON_KEY": "from-json"}
        )
        (tmp_path / ".env").write_text(
            "AGENT_TEST_JSON_KEY=from-dotenv\n", encoding="utf-8"
        )

        config_json.load()
        load_dotenv(tmp_path / ".env")  # override=False

        assert os.environ["AGENT_TEST_JSON_KEY"] == "from-json"

    def test_broken_json_reports_error_and_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bad = tmp_path / "config" / "agent.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{ 这不是 json", encoding="utf-8")

        report = config_json.load()

        assert report.error
        assert report.loaded == ()
        assert set(report.missing_required) == set(_REQUIRED)

    def test_non_object_json_reports_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / "config" / "agent.json", ["not", "an", "object"])

        report = config_json.load()

        assert report.error
        assert report.loaded == ()

    def test_null_value_is_skipped_not_set_to_null(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_json(
            tmp_path / "config" / "agent.json",
            {"AGENT_TEST_JSON_KEY": None, "HOST": "0.0.0.0"},
        )

        report = config_json.load()

        assert "AGENT_TEST_JSON_KEY" not in os.environ
        assert "AGENT_TEST_JSON_KEY" not in report.loaded

    def test_explicit_path_argument_wins(self, tmp_path, monkeypatch):
        elsewhere = _write_json(
            tmp_path / "elsewhere.json", {"AGENT_TEST_JSON_KEY": "from-elsewhere"}
        )
        monkeypatch.chdir(tmp_path)

        report = config_json.load(elsewhere)

        assert os.environ["AGENT_TEST_JSON_KEY"] == "from-elsewhere"
        assert report.source == elsewhere

    def test_env_var_overrides_default_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        custom = _write_json(
            tmp_path / "custom.json", {"AGENT_TEST_JSON_KEY": "from-custom"}
        )
        monkeypatch.setenv("AGENT_CONFIG_JSON", str(custom))

        report = config_json.load()

        assert os.environ["AGENT_TEST_JSON_KEY"] == "from-custom"
        assert report.source == custom

    def test_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_json(tmp_path / "config" / "agent.json", {"AGENT_TEST_JSON_KEY": "v"})
        first = config_json.load()
        second = config_json.load()
        assert first.loaded == ("AGENT_TEST_JSON_KEY",)
        # 第二次：键已在 environ 里，不重复写，归到 skipped_existing
        assert second.loaded == ()
        assert second.skipped_existing == ("AGENT_TEST_JSON_KEY",)


class TestApplyEnvFile:
    """web 受控写面落点：config_write 写、这里读，两边必须指向同一文件。"""

    def test_missing_file_is_a_noop(self, tmp_path):
        assert config_json.apply_env_file(tmp_path / "nope.env") == ()

    def test_none_is_a_noop(self):
        assert config_json.apply_env_file(None) == ()

    def test_overrides_json_derived_keys(self, tmp_path, monkeypatch):
        """web 的显式修改要赢过 JSON 基础配置。"""
        monkeypatch.chdir(tmp_path)
        _write_json(
            tmp_path / "config" / "agent.json", {"AGENT_TEST_JSON_KEY": "from-json"}
        )
        config_json.load()
        web = tmp_path / "config" / "agent.env"
        web.write_text("AGENT_TEST_JSON_KEY=from-web\n", encoding="utf-8")

        assert config_json.apply_env_file(web) == ("AGENT_TEST_JSON_KEY",)
        assert os.environ["AGENT_TEST_JSON_KEY"] == "from-web"

    def test_never_overrides_real_env(self, tmp_path, monkeypatch):
        """真实环境变量（出生快照里的）最高优先，web 文件盖不掉。"""
        from agentcore import env_snapshot

        monkeypatch.setattr(env_snapshot, "_snapshot", None)
        monkeypatch.setenv("AGENT_TEST_JSON_KEY", "from-shell")
        env_snapshot.capture()  # 模拟 bot.py 启动时抓的快照
        web = tmp_path / "agent.env"
        web.write_text("AGENT_TEST_JSON_KEY=from-web\n", encoding="utf-8")

        assert config_json.apply_env_file(web) == ()
        assert os.environ["AGENT_TEST_JSON_KEY"] == "from-shell"
        monkeypatch.setattr(env_snapshot, "_snapshot", None)

    def test_applies_keys_json_did_not_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        web = tmp_path / "agent.env"
        web.write_text("AGENT_TEST_JSON_KEY=from-web\n", encoding="utf-8")
        assert config_json.apply_env_file(web) == ("AGENT_TEST_JSON_KEY",)
        assert os.environ["AGENT_TEST_JSON_KEY"] == "from-web"


class TestWebEnvPath:
    """web 受控写面落点的**单一事实源**（2026-10-28）。

    此前 bot.py 自己算一份（默认 ``config/agent.env``）、``config_write`` 另算一份
    （默认 ``.env``）：同一个 ``AGENT_ENV_FILE`` 两处默认值不同，容器里靠 compose
    显式设值才没暴露。落点不一致的故障形态是「设置页显示成功、重启后不生效」——
    排查成本极高，所以把口径收成一个函数，两边都问它。
    """

    def test_default_is_config_agent_env(self, monkeypatch):
        monkeypatch.delenv("AGENT_ENV_FILE", raising=False)
        assert config_json.web_env_path() == Path("config/agent.env")

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("AGENT_ENV_FILE", " /tmp/custom.env ")
        assert config_json.web_env_path() == Path("/tmp/custom.env")

    def test_config_write_reads_the_same_path(self, monkeypatch):
        """plugin 侧不允许再自带一份默认值——回归守卫。"""
        from plugins.qq_agent_adapter import config_write

        monkeypatch.delenv("AGENT_ENV_FILE", raising=False)
        assert config_write.env_file_path() == config_json.web_env_path()
        monkeypatch.setenv("AGENT_ENV_FILE", "elsewhere.env")
        assert config_write.env_file_path() == config_json.web_env_path()


class TestMissingRequired:
    def test_placeholder_and_empty_count_as_missing(self, monkeypatch):
        """两种占位口径都要认：.env.example 的 `...` 与 compose_init 的 `换成...`。"""
        monkeypatch.setenv("LLM_API_KEY", "...")
        monkeypatch.setenv("SUPERUSERS", "")
        assert set(config_json.missing_required()) == set(_REQUIRED)

    def test_chinese_placeholder_prefix_counts_as_missing(self, monkeypatch):
        """compose_init 生成的占位是「换成你的key」这种自解释形态。"""
        monkeypatch.setenv("LLM_API_KEY", "换成你的key")
        assert "LLM_API_KEY" in config_json.missing_required()

    def test_filled_keys_drop_out(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-real")
        monkeypatch.setenv("SUPERUSERS", "12345")
        monkeypatch.setenv("ALLOWED_GROUPS", "111")
        monkeypatch.setenv("ONEBOT_ONEV11_ACCESS_TOKEN", "tok")
        monkeypatch.setenv("NAPCAT_HTTP_URL", "http://192.168.1.2:3000")
        assert config_json.missing_required() == ()

    def test_required_keys_are_documented_in_env_example(self):
        """新 env 必须登记进 .env.example，否则 test_repo_guards 会红。"""
        root = Path(__file__).resolve().parent.parent
        text = (root / ".env.example").read_text(encoding="utf-8")
        assert "AGENT_CONFIG_JSON" in text, (
            "AGENT_CONFIG_JSON 未登记进 .env.example（仓库不变量守卫要求）"
        )


# ---- 变异复核（2026-10-28）----
# 1. to_env_value 对 bool 返回 "True" → test_numbers_and_bools_become_strings 红
#    （env_flag 只认 true/false，Python 形态会被当认不出的值告警后取默认）
# 2. load 覆盖已存在 environ → test_does_not_override_existing_environ 红
# 3. 不跳过 _ 前缀键 → test_underscore_keys_are_notes_and_ignored 红
# 4. None 值写成 "null" → test_null_value_is_skipped_not_set_to_null 红
# 5. 坏 JSON 抛异常 → test_broken_json_reports_error_and_does_not_raise 红
# 6. 文件缺失时抛异常 → test_missing_file_does_not_raise_and_reports 红
# 7. missing_required 不识别 "..." 占位 → test_placeholder_and_empty_count_as_missing 红
