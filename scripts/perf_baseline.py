"""性能基准存档与劣化对比（延迟/驻留内存）。

用法::

    # 跑 perf 测试并与 perf/baseline.json 对比（检出劣化则退出码 1）
    python scripts/perf_baseline.py

    # 用当前实测结果覆盖基线（换机器/有意优化后需重新存档）
    python scripts/perf_baseline.py --update

    # 自定义劣化倍数阈值与噪声下限
    python scripts/perf_baseline.py --threshold 2.0 --floor-ms 5

原理：``tests/test_perf.py`` 在 ``RUN_PERF=1`` 下输出固定的 ``[metric] key=value`` 行，
本脚本解析这些行并与基线比较（**只用于抓缓慢劣化，不作为精确基准**）。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "perf" / "baseline.json"
PERF_TEST = "tests/test_perf.py"

_METRIC_RE = re.compile(r"\[metric\]\s+([A-Za-z0-9_]+)=([0-9.]+)")

# 低于该绝对值的毫秒指标不参与劣化判定（亚毫秒/个位数毫秒抖动过大）
_DEFAULT_FLOOR_MS = 5.0
# 内存类指标（MB）与计数类指标的噪声下限
_FLOOR_BY_SUFFIX = {"_mb": 1.0, "_delta": 0.5}


def parse_metrics(output: str) -> dict[str, float]:
    """从 pytest 输出中解析 ``[metric] key=value`` 行。"""
    metrics: dict[str, float] = {}
    for key, value in _METRIC_RE.findall(output or ""):
        try:
            metrics[key] = float(value)
        except ValueError:  # pragma: no cover - 正则已限定数字
            continue
    return metrics


def _floor_for(key: str, floor_ms: float) -> float:
    for suffix, floor in _FLOOR_BY_SUFFIX.items():
        if key.endswith(suffix):
            return floor
    return floor_ms


def compare(
    current: dict[str, float],
    baseline: dict[str, float],
    threshold: float = 1.5,
    floor_ms: float = _DEFAULT_FLOOR_MS,
) -> list[dict]:
    """返回劣化项列表；每条含 key/base/cur/ratio/floor。

    判定规则：仅当 ``cur > base * threshold`` **且基线、本次都高于该指标的噪声下限**
    才算劣化——避免个位数毫秒抖动，以及"本来可忽略的指标（如 2ms→8ms、0.02MB→0.5MB）"
    因比值大而报假警。
    """
    regressions: list[dict] = []
    for key, cur in sorted(current.items()):
        base = baseline.get(key)
        if base is None or base <= 0:
            continue  # 新指标或基线缺失：不判定
        floor = _floor_for(key, floor_ms)
        if base < floor or cur <= floor:
            continue  # 基线或本次处于噪声区，比值不可信
        if cur > base * threshold:
            regressions.append(
                {
                    "key": key,
                    "base": base,
                    "cur": cur,
                    "ratio": cur / base,
                    "floor": floor,
                }
            )
    return regressions


def run_perf_tests() -> tuple[int, str]:
    """以 RUN_PERF=1 运行性能测试，返回 (退出码, 输出)。"""
    env = {**os.environ, "RUN_PERF": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", PERF_TEST, "-s", "-q"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def load_baseline() -> dict[str, float]:
    if not BASELINE_PATH.is_file():
        return {}
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"基线文件无法解析：{exc}")
        return {}
    metrics = data.get("metrics")
    return metrics if isinstance(metrics, dict) else {}


def save_baseline(metrics: dict[str, float]) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "note": "tests/test_perf.py 实测；机器不同数字不可直接横向比较",
        "metrics": {k: round(v, 3) for k, v in sorted(metrics.items())},
    }
    BASELINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _print_table(metrics: dict[str, float], baseline: dict[str, float]) -> None:
    print(f"{'指标':<28}{'基线':>12}{'本次':>12}{'比值':>8}")
    for key in sorted(set(metrics) | set(baseline)):
        cur = metrics.get(key)
        base = baseline.get(key)
        cur_s = f"{cur:.3f}" if cur is not None else "-"
        base_s = f"{base:.3f}" if base is not None else "-"
        ratio_s = f"{cur / base:.2f}x" if (cur is not None and base) else "-"
        print(f"{key:<28}{base_s:>12}{cur_s:>12}{ratio_s:>8}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="性能基准存档与劣化对比")
    parser.add_argument("--update", action="store_true", help="用当前实测覆盖基线")
    parser.add_argument("--threshold", type=float, default=1.5, help="劣化倍数阈值")
    parser.add_argument(
        "--floor-ms", type=float, default=_DEFAULT_FLOOR_MS, help="毫秒指标噪声下限"
    )
    parser.add_argument("--dry-run", action="store_true", help="不跑测试，只看现有基线")
    args = parser.parse_args(argv)

    baseline = load_baseline()

    if args.dry_run:
        print(f"基线：{BASELINE_PATH}（{len(baseline)} 项）")
        _print_table({}, baseline)
        return 0

    code, output = run_perf_tests()
    metrics = parse_metrics(output)
    if code != 0 or not metrics:
        print("性能测试未通过或未产出指标：")
        print(output[-2000:])
        return 1

    _print_table(metrics, baseline)

    if args.update:
        save_baseline(metrics)
        print(f"\n已更新基线：{BASELINE_PATH}")
        return 0

    if not baseline:
        print(f"\n无基线文件（{BASELINE_PATH}）。先运行 --update 建立基线。")
        return 2

    regressions = compare(metrics, baseline, args.threshold, args.floor_ms)
    if regressions:
        print(f"\n检出 {len(regressions)} 项劣化（阈值 {args.threshold}x）：")
        for r in regressions:
            print(
                f"  - {r['key']}: {r['base']:.3f} -> {r['cur']:.3f}"
                f"（{r['ratio']:.2f}x，噪声下限 {r['floor']}）"
            )
        return 1

    print(f"\n无劣化（阈值 {args.threshold}x，噪声下限 {args.floor_ms}ms）。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
