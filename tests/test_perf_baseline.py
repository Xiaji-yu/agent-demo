"""scripts/perf_baseline.py 的解析与劣化判定单测（纯函数，不跑子进程）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "perf_baseline.py"
_spec = importlib.util.spec_from_file_location("perf_baseline", _SCRIPT)
assert _spec and _spec.loader
perf_baseline = importlib.util.module_from_spec(_spec)
sys.modules["perf_baseline"] = perf_baseline
_spec.loader.exec_module(perf_baseline)


SAMPLE = """
.[metric] split_message_ms=11.000  # 240k 字符切分
.[metric] qq_plain_ms=30.500  # 300k 字符纯文本化
[metric] debouncer_task_delta=0.000  # 任务数增量（应为 0）
9 passed in 3.13s
"""


class TestParseMetrics:
    def test_parses_all_lines(self):
        metrics = perf_baseline.parse_metrics(SAMPLE)
        assert metrics == {
            "split_message_ms": 11.0,
            "qq_plain_ms": 30.5,
            "debouncer_task_delta": 0.0,
        }

    def test_ignores_noise(self):
        assert perf_baseline.parse_metrics("普通日志\n9 passed") == {}
        assert perf_baseline.parse_metrics("") == {}


class TestCompare:
    def test_detects_regression(self):
        base = {"qq_plain_ms": 30.0}
        cur = {"qq_plain_ms": 90.0}  # 3x
        out = perf_baseline.compare(cur, base, threshold=1.5)
        assert [r["key"] for r in out] == ["qq_plain_ms"]
        assert out[0]["ratio"] == pytest.approx(3.0)

    def test_ignores_within_threshold(self):
        out = perf_baseline.compare(
            {"qq_plain_ms": 40.0}, {"qq_plain_ms": 30.0}, threshold=1.5
        )
        assert out == []

    def test_ignores_below_noise_floor(self):
        # 2ms -> 8ms 是 4x，但都在噪音下限之下，不报警
        out = perf_baseline.compare(
            {"fs_list_ms": 8.0}, {"fs_list_ms": 2.0}, floor_ms=5.0
        )
        assert out == []

    def test_mb_floor_is_separate(self):
        # 0.02MB -> 0.5MB 比值虽大，但低于 1.0MB 下限，不报警
        out = perf_baseline.compare(
            {"recent_image_peak_mb": 0.5}, {"recent_image_peak_mb": 0.02}
        )
        assert out == []

    def test_delta_floor(self):
        # 任务数增量 0 -> 0.4 低于 0.5 下限，不报警
        out = perf_baseline.compare(
            {"debouncer_task_delta": 0.4}, {"debouncer_task_delta": 0.0}
        )
        assert out == []

    def test_new_or_missing_baseline_metric_skipped(self):
        out = perf_baseline.compare({"new_metric_ms": 999.0}, {})
        assert out == []

    def test_improvement_not_flagged(self):
        out = perf_baseline.compare({"qq_plain_ms": 5.0}, {"qq_plain_ms": 100.0})
        assert out == []
