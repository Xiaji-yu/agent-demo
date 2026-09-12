"""性能 / 资源基线测试（延迟 + 有界性 + 内存泄漏）。

运行方式（默认跳过，避免拖慢日常套件、避免机器抖动造成误报）::

    RUN_PERF=1 .venv/bin/python -m pytest tests/test_perf.py -s

阈值刻意宽松（数倍于实测），用途只有一个：**抓 O(n²) 回归与无界增长**。
量化基准与劣化对比见 ``scripts/perf_baseline.py``（解析本文件输出的 ``[metric]`` 行）。

指标行格式固定：``[metric] key=value``；``scripts/perf_baseline.py`` 依赖它，
改动格式需同步该脚本与其单测。
"""

from __future__ import annotations

import asyncio
import gc
import os
import time
import tracemalloc

import pytest

pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(
        os.getenv("RUN_PERF") != "1", reason="性能测试默认跳过：设 RUN_PERF=1 启用"
    ),
]

_METRICS: dict[str, float] = {}
_UNITS: dict[str, str] = {}


def _record(key: str, value: float, unit: str = "") -> None:
    _METRICS[key] = float(value)
    _UNITS[key] = unit


@pytest.fixture(autouse=True)
def _emit_metrics():
    """每个用例结束后输出机器可读指标行（供基线存档脚本解析）。"""
    _METRICS.clear()
    _UNITS.clear()
    yield
    for key, value in _METRICS.items():
        print(f"[metric] {key}={value:.3f}  # {_UNITS.get(key, '')}")


def _measure(label: str, fn, *args, unit: str = "", **kwargs):
    """执行并计时，返回 (结果, 秒)；同时把毫秒值记为指标 ``label``。"""
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    cost = time.perf_counter() - start
    _record(label, cost * 1000, unit)
    return result, cost


def _peak_mb() -> float:
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return max(cur, peak) / 1024 / 1024


# ---------------------------------------------------------------------------
# 延迟
# ---------------------------------------------------------------------------
def test_split_message_large_text_latency():
    """~240k 字符长回复切分：线性实现应在亚秒级。"""
    from plugins.qq_agent_adapter.outbound import split_message

    text = "这是一句用于性能测试的中文句子。" * 15000
    chunks, cost = _measure(
        "split_message_ms", split_message, text, unit="240k 字符切分"
    )
    print(
        f"\n[perf] split_message: {len(text)} 字符 -> {len(chunks)} 段, {cost * 1000:.0f} ms"
    )
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) > len(text) * 0.9  # 不丢内容
    assert cost < 5.0


def test_qq_plain_large_markdown_latency():
    """长 markdown 纯文本化（含代码块保护）应在线性时间完成。"""
    from plugins.qq_agent_adapter.matcher import _qq_plain

    block = "**加粗** `code` [链接](https://example.com)\n> 引用\n- 列表\n```python\n# 代码\nx = 1\n```\n"
    text = block * 4000
    out, cost = _measure("qq_plain_ms", _qq_plain, text, unit="300k 字符纯文本化")
    print(
        f"\n[perf] _qq_plain: {len(text)} 字符 -> {len(out)} 字符, {cost * 1000:.0f} ms"
    )
    assert "# 代码" in out  # 代码块保护仍生效
    assert cost < 5.0


def test_merge_parts_latency_many_images():
    """防抖合并：大量 part × 大量图片时不应出现明显二次复杂度。"""
    from plugins.qq_agent_adapter.pipeline import merge_parts

    parts = [
        {
            "text": f"消息{i}" * 20,
            "images": [f"data:image/jpeg;base64,{i}-{j}" for j in range(20)],
        }
        for i in range(500)
    ]
    (text, images), cost = _measure(
        "merge_parts_ms", merge_parts, parts, unit="500 part / 1 万图合并"
    )
    print(
        f"\n[perf] merge_parts: 500 parts / 10000 图 -> {len(text)} 字符, {len(images)} 图, {cost * 1000:.0f} ms"
    )
    assert len(images) <= 4
    assert cost < 3.0


def test_local_embedding_latency():
    """本地 hash 降级 embedding：长文本 × 多条应保持线性。"""
    from agentcore.embedding.client import EmbeddingClient

    client = EmbeddingClient()
    texts = ["中文性能测试样本" * 200 for _ in range(40)]
    vecs, cost = _measure(
        "local_embed_ms",
        lambda: [client._local_embed(t) for t in texts],
        unit="40×1600 字符本地向量",
    )
    print(f"\n[perf] local_embed: 40 × {len(texts[0])} 字符 -> {cost * 1000:.0f} ms")
    assert len(vecs) == 40 and len(vecs[0]) == 2048
    assert cost < 5.0


def test_workspace_fs_resolve_latency(tmp_path):
    """沙箱路径解析是每次 fs_* 调用的必经路径，量级应在微秒级。"""
    from agentcore.workspace.fs import WorkspaceFS

    fs = WorkspaceFS(tmp_path)
    _, cost = _measure(
        "fs_resolve_ms",
        lambda: [fs.resolve("a/b/notes.md") for _ in range(20000)],
        unit="2 万次路径解析",
    )
    print(f"\n[perf] fs.resolve ×20000: {cost * 1000:.0f} ms")
    assert cost < 5.0


@pytest.mark.asyncio
async def test_fs_list_latency_many_files(tmp_path):
    """目录列举 800 个文件的耗时。"""
    from agentcore.workspace.fs import WorkspaceFS

    root = tmp_path / "ws"
    root.mkdir()
    for i in range(800):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")
    fs = WorkspaceFS(root)
    start = time.perf_counter()
    out = await asyncio.wait_for(fs.list("."), timeout=10)
    cost = time.perf_counter() - start
    _record("fs_list_ms", cost * 1000, "800 文件目录列举")
    print(f"\n[perf] fs.list 800 文件: {cost * 1000:.0f} ms")
    assert out.count("\n") >= 700
    assert cost < 2.0


# ---------------------------------------------------------------------------
# 有界性 / 内存泄漏
# ---------------------------------------------------------------------------
def test_group_context_bounded_memory():
    """群上下文缓冲：2 万条写入后必须仍有界（只留 max_lines 条）。"""
    from plugins.qq_agent_adapter.group_context import GroupContextBuffer

    buf = GroupContextBuffer(max_lines=10)
    gc.collect()
    tracemalloc.start()
    for i in range(20000):
        buf.record("g1", "某人", f"这是第 {i} 条群消息内容", message_id=str(i))
    peak = _peak_mb()
    rows, snap_cost = _measure(
        "group_context_snapshot_ms", buf.snapshot, "g1", unit="快照耗时"
    )
    _record("group_context_peak_mb", peak, "2 万条写入后驻留峰值")
    print(
        f"\n[perf] group_context: 20000 条写入后驻留峰值 {peak:.2f} MB, snapshot {snap_cost * 1000:.1f} ms"
    )
    assert len(rows) <= 10
    assert peak < 5.0  # 有界：写多少都不该线性增长
    assert snap_cost < 0.1


def test_recent_image_buffer_bounded_memory():
    """最近图片缓冲：写入量远超容量时仍受 max_entries 约束。"""
    from plugins.qq_agent_adapter.pipeline import RecentImageBuffer

    buf = RecentImageBuffer(ttl=3600, max_entries=32, max_images=2)
    gc.collect()
    tracemalloc.start()
    for i in range(20000):
        buf.put(f"key{i}", [f"data:image/jpeg;base64,{i}", f"https://x/{i}.jpg"])
    peak = _peak_mb()
    _record("recent_image_peak_mb", peak, "2 万次写入后驻留峰值")
    print(
        f"\n[perf] recent_image_buffer: 20000 次写入后驻留峰值 {peak:.2f} MB, 条目 {len(buf)}"
    )
    assert len(buf) <= 32
    assert peak < 5.0


@pytest.mark.asyncio
async def test_debouncer_no_task_leak():
    """防抖器：高频 push/cancel 后不应残留后台任务（任务泄漏 = 定时器泄漏）。"""
    from plugins.qq_agent_adapter.debounce import Debouncer

    baseline = len([t for t in asyncio.all_tasks() if not t.done()])
    deb = Debouncer(delay=30)

    async def runner(parts):  # pragma: no cover - 窗口内全部被取消
        raise AssertionError("窗口被反复重置，runner 不应执行")

    for i in range(500):
        await deb.push("k", {"t": i}, runner)
    await deb.cancel_all()
    for _ in range(3):
        await asyncio.sleep(0)

    after = len([t for t in asyncio.all_tasks() if not t.done()])
    _record("debouncer_task_delta", after - baseline, "任务数增量（应为 0）")
    print(f"\n[perf] debouncer: 500 次 push/cancel 后活跃任务 {baseline} -> {after}")
    assert after <= baseline + 1
