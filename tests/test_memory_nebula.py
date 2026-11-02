"""记忆星云：确定性布局 + 分层抽样配额。

为什么单独成文：布局是纯函数（计数 → 3D 坐标），最容易写成"看起来能动就行"。
这里钉住三条真正会出事的性质——**确定性**（刷新不能换一张图）、**全源覆盖**
（预算不够时不能让末尾的星系整块消失）、**零正文**（输入只有计数）。
"""

from __future__ import annotations

import json
import math

import pytest

from agentcore.memory.nebula import build_payload
from agentcore.memory.store import InMemoryMemoryStore, _nebula_quotas


def inventory(**over):
    base = {
        "facts": {
            "total": 3,
            "by_user": [
                {"key": "u1", "count": 2, "ids": [101, 102]},
                {"key": "u2", "count": 1, "ids": [103]},
            ],
        },
        "kb": {
            "total": 120,
            "sources": 3,
            "sampled": 30,
            "by_source": [
                {
                    "source_id": "1",
                    "count": 100,
                    "sampled": 20,
                    "ids": list(range(1, 21)),
                },
                {
                    "source_id": "2",
                    "count": 15,
                    "sampled": 7,
                    "ids": list(range(21, 28)),
                },
                {
                    "source_id": "3",
                    "count": 5,
                    "sampled": 3,
                    "ids": list(range(28, 31)),
                },
            ],
        },
        "sessions": {
            "total": 2,
            "by_session": [
                {"id": "1", "messages": 40, "scope": "private"},
                {"id": "2", "messages": 3, "scope": "group"},
            ],
        },
        "messages": {"total": 43},
        "user_state": 2,
        "group_persona": 1,
        "dim": 1024,
    }
    base.update(over)
    return base


class TestQuotas:
    def test_every_source_gets_at_least_one_when_budget_allows(self):
        """81 个源、每源上限 40、总预算 3000：没有任何源被整块丢掉。"""
        totals = [5000] * 80 + [900]
        quotas = _nebula_quotas(totals, 40, 3000)
        assert sum(quotas) == 3000
        assert all(q >= 1 for q in quotas), "末尾的源必须至少分到 1 个点"
        assert all(q <= 40 for q in quotas)

    def test_quota_respects_each_source_total(self):
        quotas = _nebula_quotas([5, 0, 3, 100], 40, 100)
        assert quotas[1] == 0, "空源不该分到配额"
        assert quotas[0] == 5 and quotas[2] == 3, "不能超过该源自己的条数"
        assert sum(quotas) == 48

    def test_non_positive_budget_is_empty_not_full(self):
        assert _nebula_quotas([1, 2, 3], 40, 0) == [0, 0, 0]
        assert _nebula_quotas([1, 2, 3], 0, 100) == [0, 0, 0]
        assert _nebula_quotas([], 40, 100) == []

    def test_tiny_budget_still_covers_prefix(self):
        quotas = _nebula_quotas([10, 10, 10], 40, 2)
        assert quotas == [1, 1, 0]


class TestLayout:
    def test_payload_shape_and_counts(self):
        d = build_payload(inventory(), generated_at=1.0)
        assert d["generated_at"] == 1.0
        assert [t["key"] for t in d["tiers"]] == ["facts", "kb", "session"]
        assert d["tiers"][0]["count"] == 3
        assert d["tiers"][1]["count"] == 120
        # 3 facts + 30 抽样知识块 + 2 会话 + 轨道点（40 钳到 18，3 条）
        assert len(d["points"]) == 3 + 30 + 2 + 18 + 3
        assert d["db"]["kb_chunks"] == 120
        assert d["db"]["dim"] == 1024

    def test_every_tier_cluster_is_on_screen(self):
        d = build_payload(inventory(), generated_at=1.0)
        shown = {}
        for p in d["points"]:
            if p[3] == 3:
                continue
            shown.setdefault(p[4], 0)
            shown[p[4]] += 1
        for idx, cluster in enumerate(d["clusters"]):
            assert shown.get(idx, 0) > 0, f"簇 {cluster['label']} 一个点都没有"

    def test_coordinates_are_normalised(self):
        d = build_payload(inventory(), generated_at=1.0)
        for p in d["points"]:
            assert all(-1.0 <= v <= 1.0 for v in p[:3]), p
            assert p[3] in (0, 1, 2, 3)

    def test_layout_is_deterministic(self):
        a = build_payload(inventory(), generated_at=7.0)
        b = build_payload(inventory(), generated_at=7.0)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_layout_does_not_depend_on_generated_at(self):
        a = build_payload(inventory(), generated_at=1.0)
        b = build_payload(inventory(), generated_at=999.0)
        assert a["points"] == b["points"]
        assert a["clusters"] == b["clusters"]

    def test_empty_inventory_degrades_to_empty_payload(self):
        d = build_payload(
            {
                "facts": {"total": 0, "by_user": []},
                "kb": {"total": 0, "sources": 0, "sampled": 0, "by_source": []},
                "sessions": {"total": 0, "by_session": []},
                "messages": {"total": 0},
                "user_state": 0,
                "group_persona": 0,
                "dim": 1024,
            },
            generated_at=1.0,
        )
        assert d["points"] == []
        assert d["clusters"] == []
        assert [t["count"] for t in d["tiers"]] == [0, 0, 0]

    def test_labels_never_contain_raw_user_id(self):
        """用户 QQ 号不能直接上图：标签只留不可逆的 4 位哈希前缀。"""
        inv = inventory(
            facts={
                "total": 1,
                "by_user": [{"key": "123456789", "count": 1, "ids": [7]}],
            }
        )
        d = build_payload(inv, generated_at=1.0)
        labels = " ".join(c["label"] for c in d["clusters"])
        assert "123456789" not in labels
        assert "用户 #" in labels

    def test_payload_carries_no_content_fields(self):
        """布局层只认计数：把正文塞进输入也会被丢掉（结构上没有出口）。"""
        inv = inventory()
        inv["facts"]["by_user"][0]["content"] = "这是不该出现的正文"
        d = build_payload(inv, generated_at=1.0)
        assert "不该出现" not in json.dumps(d, ensure_ascii=False)


class TestInventory:
    @pytest.mark.asyncio
    async def test_memory_store_counts_only(self):
        store = InMemoryMemoryStore()
        sid = await store.resolve_session("u1", None)
        await store.append_message(sid, "user", "CANARY-正文", None, None)
        await store.save_fact("u1", "用户住在北京", [0.1, 0.2], session_id=sid)
        await store.kb_add_source("资料", "text")
        await store.kb_add_chunks("1", ["a", "b", "c"], [[1.0], [1.0], [1.0]])
        inv = await store.nebula_inventory(per_source_limit=2, max_chunks=10)
        text = json.dumps(inv, ensure_ascii=False)
        assert "CANARY" not in text and "北京" not in text, "聚合结果不得包含正文"
        assert inv["facts"]["total"] == 1
        assert inv["kb"]["total"] == 3
        assert inv["kb"]["sampled"] == 2
        assert inv["messages"]["total"] == 1
        assert inv["dim"] == 0

    @pytest.mark.asyncio
    async def test_layout_from_memory_store_keeps_all_sources(self):
        store = InMemoryMemoryStore()
        for i in range(5):
            await store.kb_add_source(f"源{i}", "text")
            await store.kb_add_chunks(str(i + 1), [f"c{j}" for j in range(4)], [[]] * 4)
        inv = await store.nebula_inventory(per_source_limit=1, max_chunks=3)
        d = build_payload(inv, generated_at=1.0)
        kb_clusters = [c for c in d["clusters"] if c["tier"] == 1]
        assert len(kb_clusters) == 3, "预算 3 时只上线 3 个源（每源 1 个点）"
        assert all(c["shown"] == 1 for c in kb_clusters)
        assert math.isclose(sum(c["shown"] for c in kb_clusters), 3)
