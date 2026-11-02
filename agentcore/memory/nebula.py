"""记忆星云：把只读聚合**布局成 3D 坐标**（B 方案：结构化分簇）。

设计边界（第一版刻意做的取舍）：

- **不用 embedding**：短期记忆没有向量列，且 1024 维投影只保留约 26% 的
  近邻结构（2026-11-02 对生产库实测），画出来会让"邻近"看起来比实际更确定。
  这一版只按「类型 → 子簇」摆放：三层各有天区，簇中心按斐波那契球冠分布，
  簇内点按哈希播种的高斯散布。语义投影留给 A 阶段做开关。
- **确定性**：所有位置只由 (tier, 簇 key, 序号) 派生，不掺时间/随机源——
  刷新前后同一批数据长在同一处，否则用户会以为记忆在漂移。
- **零正文**：本模块只接收计数与标识（``BaseMemoryStore.nebula_inventory``），
  不接触 content / embedding / group_id。

坐标统一落在 [-1, 1]，前端只做等轴投影，不需要再算。
"""

from __future__ import annotations

import hashlib
import math
import random
import time
from typing import Any

# tier, 中文名, 天区中心方向, 球冠张角, 半径
_TIERS = (
    ("facts", "长期事实", (0.10, 0.80, 0.28), 0.62, 0.58),
    ("kb", "知识库", (-0.55, -0.18, -0.46), 0.98, 0.86),
    ("session", "会话", (0.60, -0.62, 0.24), 0.50, 0.56),
)
_ORBIT = 3  # points 里 tier=3 表示"会话时间轨道"上的点

# 会话轨道上限：再多也只是把同一个会话糊成一团光斑
_ORBIT_MAX = 18
_GOLDEN = 2.399963229728653


def _norm(v: tuple[float, float, float]) -> tuple[float, float, float]:
    m = math.sqrt(sum(x * x for x in v)) or 1.0
    return (v[0] / m, v[1] / m, v[2] / m)


def _cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _basis(d: tuple[float, float, float]):
    """以 d 为极轴的右手正交基（d 本身 + 两个切向）。"""
    d = _norm(d)
    ref = (0.0, 1.0, 0.0) if abs(d[1]) < 0.9 else (1.0, 0.0, 0.0)
    u = _norm(_cross(ref, d))
    v = _cross(d, u)
    return d, u, v


def _rng(*parts: str) -> random.Random:
    h = hashlib.blake2b("|".join(parts).encode("utf-8"), digest_size=8).digest()
    return random.Random(int.from_bytes(h, "big"))


def _short_hash(text: str, width: int = 4) -> str:
    """不可逆短标识：用户 QQ 号不能直接上图（web 面已经只出计数，别再放号）。"""
    h = hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()
    return h[:width]


def _cluster_centers(count: int, tier: int):
    """球冠上的斐波那契分布：簇不重叠，且数量变化时只有末尾几个挪位。"""
    _, _, base_dir, spread, radius = _TIERS[tier]
    d, u, v = _basis(base_dir)
    out = []
    for i in range(count):
        t = (i + 0.5) / max(1, count)
        r = spread * math.sqrt(t)
        th = i * _GOLDEN
        p = (
            d[0] + r * (math.cos(th) * u[0] + math.sin(th) * v[0]),
            d[1] + r * (math.cos(th) * u[1] + math.sin(th) * v[1]),
            d[2] + r * (math.cos(th) * u[2] + math.sin(th) * v[2]),
        )
        p = _norm(p)
        out.append((p[0] * radius, p[1] * radius, p[2] * radius))
    return out


def _spread_of(total: int, spacing: float) -> float:
    """簇的视觉半径。

    上限取 ``0.34 * 簇间距``：不这么钳住，源多的层（71 个知识源）簇内散布会
    超过簇间距，整层糊成一团——「类型 → 子簇」的结构就看不见了（2026-11-02
    真实数据预览实测）。簇间距按本层半径、张角与簇数估算。
    """
    return min(
        0.34 * spacing,
        0.030 + 0.020 * math.log10(1 + max(0, total)),
    )


def _facts_clusters(inv: dict) -> list[dict]:
    rows = inv.get("facts", {}).get("by_user") or []
    return [
        {
            "key": f"u:{r['key']}",
            "label": "用户 #" + _short_hash(str(r["key"])),
            "size": int(r["count"]),
            "shown": len(r.get("ids") or []),
            "ids": [int(i) for i in (r.get("ids") or [])],
        }
        for r in rows
    ]


def _kb_clusters(inv: dict) -> list[dict]:
    rows = inv.get("kb", {}).get("by_source") or []
    return [
        {
            "key": f"k:{r['source_id']}",
            "label": "源 #" + str(r["source_id"]),
            "size": int(r["count"]),
            "shown": len(r.get("ids") or []),
            "ids": [int(i) for i in (r.get("ids") or [])],
        }
        for r in rows
    ]


def _session_clusters(inv: dict) -> list[dict]:
    rows = inv.get("sessions", {}).get("by_session") or []
    return [
        {
            "key": f"s:{r['id']}",
            "label": "会话 #" + str(r["id"]),
            "size": int(r["messages"]),
            "shown": min(int(r["messages"]), _ORBIT_MAX),
            "ids": [],
            "id": int(r["id"]) if str(r["id"]).isdigit() else 0,
            "scope": str(r.get("scope") or "private"),
        }
        for r in rows
    ]


def build_payload(
    inventory: dict,
    *,
    generated_at: float | None = None,
    cache_ttl: float = 60.0,
    per_source_limit: int = 40,
    max_chunks: int = 3000,
) -> dict[str, Any]:
    """聚合结果 → 星云布局。纯函数（同输入必得同输出），无 IO。"""
    tier_clusters = [
        _facts_clusters(inventory),
        _kb_clusters(inventory),
        _session_clusters(inventory),
    ]
    centers = [_cluster_centers(len(c), i) for i, c in enumerate(tier_clusters)]

    clusters: list[dict] = []
    points: list[list] = []
    layer_points = [0, 0, 0]
    for tier, rows in enumerate(tier_clusters):
        # 本层的簇间距估算：球冠张角 × 半径 / √簇数
        spacing = (
            _TIERS[tier][3] * _TIERS[tier][4] / math.sqrt(max(1, len(rows)))
            if rows
            else 0.0
        )
        for idx, row in enumerate(rows):
            cx, cy, cz = centers[tier][idx]
            cid = len(clusters)
            clusters.append(
                {
                    "tier": tier,
                    "label": row["label"],
                    "size": row["size"],
                    "shown": row["shown"],
                    "x": round(cx, 4),
                    "y": round(cy, 4),
                    "z": round(cz, 4),
                    **({"scope": row["scope"]} if "scope" in row else {}),
                }
            )
            rng = _rng(row["key"])
            sigma = _spread_of(row["size"], spacing)
            ids = row.get("ids") or []
            # 会话层：每个簇只有一颗星（会话节点），消息不单独落点——
            # 它们由下面的时间轨道表达，否则 3 千条消息会把整个天区糊死。
            for i in range(1 if tier == 2 else row["shown"]):
                if tier == 2:
                    item_id = int(row.get("id") or 0)
                else:
                    item_id = int(ids[i]) if i < len(ids) else 0
                points.append(
                    [
                        cx + rng.gauss(0.0, sigma),
                        cy + rng.gauss(0.0, sigma),
                        cz + rng.gauss(0.0, sigma),
                        tier,
                        cid,
                        item_id,
                    ]
                )
                layer_points[tier] += 1
            if tier == 2 and row["shown"]:
                # 时间轨道：内圈 = 最近，一圈一圈往外。角度用黄金角错开，
                # 避免多个会话的轨道在视觉上对齐成假结构。
                for k in range(row["shown"]):
                    th = k * _GOLDEN
                    rad = 0.030 + 0.009 * k
                    points.append(
                        [
                            cx + math.cos(th) * rad,
                            cy + math.sin(th) * rad * 0.72,
                            cz + math.sin(th * 1.7) * 0.012,
                            _ORBIT,
                            cid,
                            int(row.get("id") or 0),
                        ]
                    )

    # 统一缩放进 [-1, 1]：三层半径本来就按比例设好，这里只压掉高斯尾巴
    maxabs = max((abs(v) for p in points for v in p[:3]), default=0.0)
    if maxabs > 1.0:
        scale = 1.0 / maxabs
        for p in points:
            p[0] = round(p[0] * scale, 3)
            p[1] = round(p[1] * scale, 3)
            p[2] = round(p[2] * scale, 3)
        for c in clusters:
            c["x"] = round(c["x"] * scale, 4)
            c["y"] = round(c["y"] * scale, 4)
            c["z"] = round(c["z"] * scale, 4)
    else:
        for p in points:
            p[0] = round(p[0], 3)
            p[1] = round(p[1], 3)
            p[2] = round(p[2], 3)

    kb = inventory.get("kb", {})
    return {
        "generated_at": float(
            generated_at if generated_at is not None else time.time()
        ),
        "cache_ttl": float(cache_ttl),
        "tiers": [
            {
                "key": key,
                "label": label,
                "count": sum(row["size"] for row in rows),
                "shown": layer_points[i],
                "clusters": len(rows),
            }
            for i, ((key, label, *_rest), rows) in enumerate(
                zip(_TIERS, tier_clusters, strict=True)
            )
        ],
        "clusters": clusters,
        "points": points,
        "sampling": {
            "per_source_limit": int(per_source_limit),
            "max_chunks": int(max_chunks),
            "shown": int(kb.get("sampled") or 0),
        },
        "db": {
            "facts": int(inventory.get("facts", {}).get("total") or 0),
            "kb_chunks": int(kb.get("total") or 0),
            "kb_sources": int(kb.get("sources") or 0),
            "messages": int(inventory.get("messages", {}).get("total") or 0),
            "sessions": int(inventory.get("sessions", {}).get("total") or 0),
            "user_state": int(inventory.get("user_state") or 0),
            "group_persona": int(inventory.get("group_persona") or 0),
            "dim": int(inventory.get("dim") or 0),
        },
    }
