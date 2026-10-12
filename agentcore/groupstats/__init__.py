"""群聊发言统计：白名单群的发言榜与词云数据管线。

边界（本包是 agentcore，**不得 import nonebot**）：
- 采集由插件层调用 :func:`record`（plugins/qq_agent_adapter/matcher.py 的
  group_recorder，与群上下文同一个挂点——不新增 matcher）
- 渲染在 agentcore/render（rank.py / cloud.py）
- NoneBot 接线（命令/定时/Web）在 plugins/qq_agent_adapter/stats_route.py

隐私口径（与 group_context「绝不落库」的**有意识决裂**，BACKLOG.md 有记录）：
- opt-in：``AGENT_GROUP_STATS=1`` 才采集，默认关闭
- 仅 ``ALLOWED_GROUPS`` 白名单群；**空集 = 不采集**（fail-closed，M1，
  REVIEW-1e0f5ae..a0a6bb4——与 acl 同键同语义）
- 聚合统计只落数值：每用户每日条数/字数/小时直方图 + 分词词频
- **消息原文仅**在显式开启 ``AGENT_GROUP_ARCHIVE=1`` 时经 raw.py 落库
  （原文归档是独立的 opt-in 功能，同样受白名单约束；本 docstring 旧版
  「原文不落任何存储」与 raw.py 自相矛盾——L7，同报告）
- 合并转发消息整条不采集（转发内容不是本人的发言，见 matcher 挂点）
- retention 默认 35 天，每日清理
"""

from __future__ import annotations

import logging

from agentcore.groupstats.collector import StatsCollector
from agentcore.groupstats.config import (
    allowed_groups,
    archive_buffer_max,
    archive_enabled,
    archive_flush_seconds,
    archive_retention_days,
    cooldown,
    cron,
    enabled,
    extra_stopwords,
    flush_seconds,
    min_msgs,
    retention_days,
    review_cooldown,
    review_max_chars,
    review_min_msgs,
    top_n,
    weekly_groups,
    words,
)
from agentcore.groupstats.raw import (
    KIND_FORWARD,
    KIND_IMAGE,
    RawArchiveBuffer,
    RawArchiveStore,
    classify_kind,
    recompute_window,
    reply_to_of,
)
from agentcore.groupstats.store import (
    InMemoryStatsStore,
    PgStatsStore,
    StatsStore,
    open_store,
)

logger = logging.getLogger(__name__)

__all__ = [
    "StatsCollector",
    "StatsStore",
    "PgStatsStore",
    "InMemoryStatsStore",
    "open_store",
    "configure",
    "record",
    "collector",
    "RawArchiveBuffer",
    "RawArchiveStore",
    "KIND_FORWARD",
    "KIND_IMAGE",
    "classify_kind",
    "recompute_window",
    "reply_to_of",
    "configure_archive",
    "record_raw",
    "archive_buffer",
    "allowed_groups",
    "archive_buffer_max",
    "archive_enabled",
    "archive_flush_seconds",
    "archive_retention_days",
    "cooldown",
    "cron",
    "enabled",
    "extra_stopwords",
    "flush_seconds",
    "min_msgs",
    "retention_days",
    "review_cooldown",
    "review_max_chars",
    "review_min_msgs",
    "top_n",
    "weekly_groups",
    "words",
]

_collector: StatsCollector | None = None
_archive: RawArchiveBuffer | None = None


def configure_archive(store: RawArchiveStore) -> RawArchiveBuffer | None:
    """装配原文归档缓冲（启动期调用一次）。关闭时返回 None。

    调用方负责先 ``archive_enabled`` 判定与 store 的 init（无 PG 时不该走到
    这里——见 __init__.py 的 _init_group_archive）。
    """
    global _archive
    if not archive_enabled():
        _archive = None
        return None
    _archive = RawArchiveBuffer(
        store,
        max_rows=archive_buffer_max(),
        retention_days=archive_retention_days(),
        allowed_groups=allowed_groups(),
    )
    if not allowed_groups():
        # M1：与 configure() 同款提醒——空集 = 不归档（fail-closed）
        logger.warning(
            "群消息原文归档已开启但 ALLOWED_GROUPS 为空：空集 = 不归档"
            "（fail-closed，与聊天面同键同语义），如需归档请配置白名单群"
        )
    logger.info(
        "群消息原文归档已开启：保留 %d 天，flush %ds，缓冲上限 %d 行",
        archive_retention_days(),
        archive_flush_seconds(),
        archive_buffer_max(),
    )
    return _archive


def archive_buffer() -> RawArchiveBuffer | None:
    """当前归档缓冲（未开启/未装配为 None）。"""
    return _archive


def record_raw(
    *,
    group_id: str,
    user_id: str,
    nickname: str,
    message_id: str,
    text: str,
    ts: float,
    has_image: bool = False,
    forward_id: str = "",
    reply_to: str = "",
) -> None:
    """归档一条群消息。**任何异常都不外溢**（归档是增益功能）。

    挂点约定（matcher.group_recorder）：调用方已保证「群消息且非 bot 自己」；
    白名单群过滤在缓冲内做（与 stats collector 同口径，挂点零判断）。

    有意**不查** ``BLOCKED_USERS``（L11，REVIEW-1e0f5ae..a0a6bb4 入档取舍）：
    聊天面的用户黑名单只决定「bot 是否回应 TA」，不改变「TA 的发言是本群
    群数据的一部分」——归档是运营底账（点评/周报的事实源），按群粒度白名单
    采集，不做用户粒度剔除。被拉黑用户的原文随 retention（默认 30 天）到期
    清理。若部署者要求 TA 完全不进归档，只能把该群移出白名单（整群粒度）。
    """
    if not archive_enabled():
        return
    buf = _archive
    if buf is None:
        return
    try:
        buf.record(
            group_id=group_id,
            user_id=user_id,
            nickname=nickname,
            message_id=message_id,
            text=text,
            ts=ts,
            has_image=has_image,
            forward_id=forward_id,
            reply_to=reply_to,
        )
    except Exception:
        logger.warning("group archive record failed", exc_info=True)


def configure(store: StatsStore) -> StatsCollector | None:
    """装配进程内采集器（启动期调用一次）。

    调用方负责先 :func:`enabled` 判定与 store 的 init（含 PG 失败降级），
    这里只做白名单/停用词/retention 的接线。关闭时返回 None。
    """
    global _collector
    if not enabled():
        _collector = None
        logger.info("群聊发言统计已关闭（AGENT_GROUP_STATS 未开启）")
        return None
    _collector = StatsCollector(
        store,
        allowed_groups=allowed_groups(),
        retention_days=retention_days(),
        extra_stopwords=extra_stopwords(),
    )
    if not allowed_groups():
        # M1（REVIEW-1e0f5ae..a0a6bb4）：白名单空集 = 不采集（fail-closed）。
        # 开关开着却一个群都不采，多半是 ALLOWED_GROUPS 没配——显式提醒，
        # 别让部署者对着空数据集排查半天。
        logger.warning(
            "群聊发言统计已开启但 ALLOWED_GROUPS 为空：空集 = 不采集"
            "（fail-closed，与聊天面同键同语义），如需统计数据请配置白名单群"
        )
    logger.info(
        "群聊发言统计已开启：白名单群 %d 个，flush %ds，保留 %d 天",
        len(allowed_groups()),
        flush_seconds(),
        retention_days(),
    )
    return _collector


def collector() -> StatsCollector | None:
    """当前采集器（未开启/未装配为 None）。"""
    return _collector


def record(
    group_id: str,
    user_id: str,
    nickname: str,
    text: str,
    ts: float | None = None,
) -> None:
    """采集一条群消息。**任何异常都不外溢**——统计是增益功能，不能拖垮聊天。

    挂点约定（matcher.group_recorder）：调用方已保证「群消息且非 bot 自己」；
    合并转发的整条排除也在挂点完成（forward 段是消息结构，不属于本包职责）。
    """
    if not enabled():
        return
    c = _collector
    if c is None:
        return
    try:
        c.record(group_id, user_id, nickname, text, ts=ts)
    except Exception:
        logger.warning("group stats record failed", exc_info=True)
