"""scripts/seed_dev_data.py 的单测：库名闸门、确定性、时间分布。

真正写库不在单测里跑（会碰 PostgreSQL）；这里锁的是「会不会误写生产库」与
「同一 seed 是否复现同一批数据」——这两条错了，开发库就会变成第二个生产副本，
或者每次 seed 出来的分布都不一样、无法复现问题。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "seed_dev_data", ROOT / "scripts" / "seed_dev_data.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


seed = _load()


def _build(**over):
    kwargs = {
        "seed": 7,
        "users": 5,
        "sessions_per_user": 2,
        "facts": 20,
        "messages": 30,
        "kb_sources": 2,
        "kb_chunks": 10,
        "days": 365,
        "dim": 8,
        "now": 1_800_000_000.0,
    }
    kwargs.update(over)
    return seed.build_dataset(**kwargs)


class TestGuard:
    def test_rejects_production_like_names(self):
        for name in ("qqagent", "agent-demo", "postgres"):
            url = f"postgresql://u:p@127.0.0.1:5432/{name}"
            assert seed.guard_url(url), f"{name} 必须被拒绝"

    def test_allows_dev_names(self):
        for name in ("qqagent_dev", "agent_demo_scratch", "qqagent_test"):
            url = f"postgresql://u:p@127.0.0.1:5432/{name}"
            assert seed.guard_url(url) is None, f"{name} 应该放行"

    def test_requires_database_name(self):
        assert seed.guard_url("postgresql://u:p@127.0.0.1:5432/")

    def test_empty_url_refused(self):
        assert seed.guard_url("")


class TestDataset:
    def test_same_seed_is_reproducible(self):
        a = _build()
        b = _build()
        assert a["facts"] == b["facts"]
        assert a["messages"] == b["messages"]
        assert [s["user_id"] for s in a["sessions"]] == [
            s["user_id"] for s in b["sessions"]
        ]

    def test_different_seed_changes_data(self):
        a = _build()
        b = _build(seed=8)
        assert (a["facts"][0]["content"], a["facts"][0]["embedding"]) != (
            b["facts"][0]["content"],
            b["facts"][0]["embedding"],
        )

    def test_dim_respected(self):
        data = _build(dim=16)
        for f in data["facts"]:
            assert len(f["embedding"].strip("[]").split(",")) == 16

    def test_ages_stay_within_span(self):
        data = _build(days=100)
        for f in data["facts"]:
            age = (data["now"] - f["created_at"]) / 86400
            assert 0 <= age <= 100.001

    def test_tiers_cover_all_three(self):
        data = _build(facts=200, days=720)
        tiers = seed.tier_counts(data["facts"], data["now"])
        assert sum(tiers.values()) == 200
        assert all(v > 0 for v in tiers.values()), tiers
