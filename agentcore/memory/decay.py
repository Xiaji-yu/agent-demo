"""长期事实的时间维度：排序权重与保留率。

**为什么是两条曲线，不要合并成一条**：

- :func:`ranking_weight` 只决定「同一批事实在 prompt 里的先后」。它必须**温和**：
  相似度才是主信号，时间最多做 40% 的微调；否则一条低相关的新事实会把高相关的
  旧事实挤下去（facts 里同时存在「最近在学 X」与「用户是程序员」）。
- :func:`retention` 描述「这条记忆还剩下多少」。它必须**陡**，否则永远淘汰不掉
  任何东西。当前阶段（P0）冷区只用于标记与观察日志：既不删除，也不移出召回池。

**常数写死、不进 env/yaml/web**：这套参数是产品判断（一次提及该记多久），做成
可配置只会让线上「为什么它忘了」变得无法归因。

时间一律以「秒」为单位传入（与 ``facts.created_at`` 一致），缺时间戳按中性处理。
"""

from __future__ import annotations

import math
import time

# ---- 排序权重：宽限期内完全不变，之后每半年减半，最多降到 60% ----
# 宽限期存在的理由：刚记住的事情不该被当成「旧」；下限存在的理由：
# 姓名/城市/职业这类语义记忆不随时间失效，不能靠时间把它们排到队尾。
RANKING_GRACE_DAYS = 30.0
RANKING_HALF_LIFE_DAYS = 180.0
RANKING_FLOOR = 0.60

# ---- 保留率：R(t) = exp(-t / S)，S 是「记忆强度」（天） ----
RETENTION_INITIAL_DAYS = 30.0
RETENTION_GROWTH = 2.0  # 每次重新确认（P1 才接写入侧）
RETENTION_MAX_DAYS = 730.0

# ---- 分档阈值 ----
HOT_MIN = 0.5
COLD_MAX = 0.1

DAY_SECONDS = 86400.0

# 召回时先按余弦多取几倍候选，衰减才有机会改变「谁入选」而不只是「谁排前」。
# 超过上限就不再超额：top_k 本身是调用方的上限承诺，不能因为重排把结果截短。
CANDIDATE_FACTOR = 3
MAX_CANDIDATES = 200


def age_days(created_at: float | None, now: float | None = None) -> float | None:
    """把 epoch 秒换算成「多少天前」；``None`` 原样返回（旧数据没有时间戳）。"""
    if created_at is None:
        return None
    current = time.time() if now is None else float(now)
    return max(0.0, (current - float(created_at)) / DAY_SECONDS)


def ranking_weight(age: float | None) -> float:
    """排序权重：宽限期内 1.0，之后半衰，触底 ``RANKING_FLOOR``。

    ``None``（缺时间戳的旧数据/测试替身）按中性 1.0 —— 缺信息时不做惩罚。
    """
    if age is None:
        return 1.0
    if age <= RANKING_GRACE_DAYS:
        return 1.0
    decayed = 0.5 ** ((age - RANKING_GRACE_DAYS) / RANKING_HALF_LIFE_DAYS)
    return max(RANKING_FLOOR, decayed)


def retention(age: float | None, strength_days: float | None = None) -> float:
    """保留率 R = exp(-age / S)。缺时间戳按刚写入算（1.0）。"""
    if age is None:
        return 1.0
    strength = float(strength_days or RETENTION_INITIAL_DAYS)
    if strength <= 0:
        strength = RETENTION_INITIAL_DAYS
    return math.exp(-float(age) / strength)


def tier(age: float | None, strength_days: float | None = None) -> str:
    """三档标签：``hot`` / ``warm`` / ``cold``（边界含在热/温一侧）。"""
    r = retention(age, strength_days)
    if r >= HOT_MIN:
        return "hot"
    if r < COLD_MAX:
        return "cold"
    return "warm"


def candidate_limit(top_k: int) -> int:
    """按余弦预取的候选条数。

    ``top_k`` 本身已经是调用方的上限承诺，所以超额结果超过 ``MAX_CANDIDATES``
    时直接放弃超额（返回 ``top_k``）——宁可少一次重排机会，也不能把结果截短。
    """
    if top_k <= 0:
        return 0
    extra = top_k * CANDIDATE_FACTOR
    return extra if extra <= MAX_CANDIDATES else top_k
