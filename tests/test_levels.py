"""AGENT_PERMISSION_LEVEL（low/medium/high）解析与判定的主题测试。

来源：2026-10 权限三级收束（目标白名单制 → 级别制）。
契约：缺省=medium；脏值告警一次后回退；at_least 单调；非法级别名拒绝。
"""

import logging

import pytest

from agentcore.skills import levels as L


@pytest.fixture(autouse=True)
def _reset_warned(monkeypatch):
    monkeypatch.setattr(L, "_warned_bad_values", set())
    yield


class TestCurrentLevel:
    def test_default_medium(self, monkeypatch):
        monkeypatch.delenv(L.LEVEL_ENV, raising=False)
        assert L.current_level() == "medium"

    def test_valid_values(self, monkeypatch):
        for value in ("low", "medium", "high", " HIGH ", "Medium"):
            monkeypatch.setenv(L.LEVEL_ENV, value)
            assert L.current_level() == value.strip().lower(), value

    def test_dirty_value_falls_back_with_single_warning(self, monkeypatch, caplog):
        monkeypatch.setenv(L.LEVEL_ENV, "ultra")
        with caplog.at_level(logging.WARNING, logger=L.__name__):
            assert L.current_level() == "medium"
            assert L.current_level() == "medium"
            assert L.current_level() == "medium"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "同一脏值只告警一次（避免每次调用刷屏）"

    def test_empty_value_is_default_not_dirty(self, monkeypatch, caplog):
        monkeypatch.setenv(L.LEVEL_ENV, "")
        with caplog.at_level(logging.WARNING, logger=L.__name__):
            assert L.current_level() == "medium"
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


class TestAtLeast:
    @pytest.mark.parametrize(
        ("level", "low", "medium", "high"),
        [
            ("low", True, False, False),
            ("medium", True, True, False),
            ("high", True, True, True),
        ],
    )
    def test_monotonic(self, monkeypatch, level, low, medium, high):
        monkeypatch.setenv(L.LEVEL_ENV, level)
        assert L.at_least("low") is low
        assert L.at_least("medium") is medium
        assert L.at_least("high") is high

    def test_unknown_level_name_rejected(self, monkeypatch):
        monkeypatch.setenv(L.LEVEL_ENV, "medium")
        with pytest.raises(ValueError):
            L.at_least("ultra")


class TestNormalize:
    def test_normalize(self):
        assert L.normalize_level(" low ") == "low"
        assert L.normalize_level("") is None
        assert L.normalize_level("prod") is None
        assert L.normalize_level(None) is None
