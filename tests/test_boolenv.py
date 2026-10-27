"""布尔 env 统一口径（agentcore/boolenv.py）的行为锁。

来源：L7（REVIEW-ee39c8e..e55cab5）——仓史上各处自己写
``raw not in {"0","false","no","off"}``，``"False\n"``/``"OFF"`` 靠
``.strip().lower()`` 早就收掉了，但 ``disabled``/``n`` 这类**认不出的值会静默
按开启**：运维以为自己关了某个开关、实际开着，日志里一行记录都没有。

语义必须与旧写法完全一致（认出的值照旧判、空值照旧取默认），只多一件事：
认不出的值告警一次后回退默认。这里逐条锁死，包括「同一错值不刷屏」。
"""

from __future__ import annotations

import pytest

from agentcore import boolenv


@pytest.fixture(autouse=True)
def _reset_warned():
    boolenv._warned.clear()
    yield
    boolenv._warned.clear()


class TestParseBool:
    @pytest.mark.parametrize(
        "raw,want",
        [
            ("1", True),
            ("true", True),
            ("TRUE", True),
            (" True\n", True),
            ("yes", True),
            ("on", True),
            ("ON", True),
            ("0", False),
            ("false", False),
            ("False", False),
            (" false\n", False),
            ("no", False),
            ("off", False),
            ("OFF", False),
            # 认不出 / 空 → None（调用方决定默认）
            ("", None),
            ("   ", None),
            (None, None),
            ("disabled", None),
            ("n", None),
            ("2", None),
        ],
    )
    def test_tri_state(self, raw, want):
        assert boolenv.parse_bool(raw) is want


class TestEnvFlag:
    def test_recognized_values_match_legacy_semantics(self):
        """与旧写法逐值等价——这是本重构不可破坏的契约。"""
        legacy_off = {"0", "false", "no", "off"}
        for raw in ("0", "false", "no", "off", "1", "true", "yes", "on"):
            legacy = raw.strip().lower() not in legacy_off
            assert boolenv.env_flag("X", raw, False) is legacy, raw

    def test_empty_uses_default(self):
        assert boolenv.env_flag("X", "", True) is True
        assert boolenv.env_flag("X", None, False) is False

    def test_dirty_value_falls_back_to_default(self):
        assert boolenv.env_flag("X", "disabled", True) is True
        assert boolenv.env_flag("X", "disabled", False) is False

    def test_dirty_value_warns_once(self, caplog):
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="agentcore.boolenv"):
            for _ in range(5):
                boolenv.env_flag("AGENT_MUSIC_ENABLED", "disabled", True)
        hits = [r for r in caplog.records if "不是布尔值" in r.getMessage()]
        assert len(hits) == 1, f"同一错值只报一次，实际 {len(hits)} 条"
        assert "AGENT_MUSIC_ENABLED" in hits[0].getMessage()

    def test_distinct_dirty_values_each_warn(self, caplog):
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="agentcore.boolenv"):
            boolenv.env_flag("X", "disabled", True)
            boolenv.env_flag("X", "n", True)
        assert len([r for r in caplog.records if "不是布尔值" in r.getMessage()]) == 2

    def test_same_value_different_key_each_warn(self, caplog):
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="agentcore.boolenv"):
            boolenv.env_flag("A", "disabled", True)
            boolenv.env_flag("B", "disabled", True)
        assert len([r for r in caplog.records if "不是布尔值" in r.getMessage()]) == 2


class TestEnvBool:
    def test_reads_os_environ(self, monkeypatch):
        monkeypatch.setenv("AGENT_BOOLENV_PROBE", "off")
        assert boolenv.env_bool("AGENT_BOOLENV_PROBE", True) is False
        monkeypatch.setenv("AGENT_BOOLENV_PROBE", "on")
        assert boolenv.env_bool("AGENT_BOOLENV_PROBE", False) is True
        monkeypatch.delenv("AGENT_BOOLENV_PROBE", raising=False)
        assert boolenv.env_bool("AGENT_BOOLENV_PROBE", True) is True

    def test_dirty_environ_value_warns(self, monkeypatch, caplog):
        import logging as _logging

        monkeypatch.setenv("AGENT_BOOLENV_PROBE", "disabled")
        with caplog.at_level(_logging.WARNING, logger="agentcore.boolenv"):
            assert boolenv.env_bool("AGENT_BOOLENV_PROBE", False) is False
        assert "AGENT_BOOLENV_PROBE" in caplog.text
