"""群统计配置解析：全部走 env（env > config.yaml > 默认，与仓库惯例一致）。

脏值一律告警后回退默认，不崩启动（照 agentcore/scheduler/push.py 的
_env_flag/_env_int 模式）。这些键**不进 web 写面白名单**（config_write 的 7 键
只覆盖对话/视觉/预算类）——统计开关变更需重启生效，README 已明示。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_FLAG_OFF = {"0", "false", "no", "off"}


def _flag(name: str, default: bool) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in _FLAG_OFF


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退 %d", name, raw, default)
        return default
    return max(minimum, value)


def enabled() -> bool:
    """总开关。**默认关闭**（opt-in）：群消息落库是有意识的隐私让步，必须显式开。"""
    return _flag("AGENT_GROUP_STATS", False)


def flush_seconds() -> int:
    """聚合落库周期，也即榜单/词云的数据滞后上限。"""
    return _int("AGENT_GROUP_STATS_FLUSH_SECONDS", 30, minimum=1)


def retention_days() -> int:
    """保留天数（默认 5 周：够「上周对比」+ 冗余）。"""
    return _int("AGENT_GROUP_STATS_RETENTION_DAYS", 35, minimum=1)


def top_n() -> int:
    """榜单人数（默认 5，与产品截图一致）；钳 1..20 防一张图塞不进。"""
    return min(20, max(1, _int("AGENT_GROUP_STATS_TOP_N", 5, minimum=1)))


def words() -> int:
    """词云词数上限（默认 80）；钳 10..500 防布局爆炸。"""
    return min(500, max(10, _int("AGENT_GROUP_STATS_WORDS", 80, minimum=10)))


def min_msgs() -> int:
    """周报最低群友条数：不足不发，避免新群/死群每周推空气周报。"""
    return _int("AGENT_GROUP_STATS_MIN_MSGS", 20, minimum=0)


def cron() -> str:
    """周报 cron（5 段，时区随 AGENT_SCHEDULER_TZ）。未设 = 周一 9:00；
    显式设空串 = 关闭（不能用 `or` 判空——那会让"设为空"退化成默认值）。"""
    raw = os.getenv("AGENT_GROUP_STATS_CRON")
    if raw is None:
        return "0 9 * * 1"
    return raw.strip()


def cooldown() -> int:
    """同群命令冷却秒数（防刷屏）。"""
    return _int("AGENT_GROUP_STATS_CMD_COOLDOWN", 30, minimum=0)


def review_min_msgs() -> int:
    """LLM 个人点评的最低条数门槛：低于它不调 LLM（省成本，也避免评空气）。"""
    return _int("AGENT_GROUP_STATS_REVIEW_MIN_MSGS", 10, minimum=1)


def review_max_chars() -> int:
    """评语长度上限（默认 80，卡片排版友好）。"""
    return _int("AGENT_GROUP_STATS_REVIEW_MAX_CHARS", 80, minimum=20)


def review_cooldown() -> int:
    """个人点评 skill 的 per-user 冷却秒数（每次触发可能花一次 LLM 调用）。"""
    return _int("AGENT_GROUP_STATS_REVIEW_COOLDOWN", 30, minimum=0)


def weekly_groups() -> frozenset[str]:
    """周报推送白名单（``AGENT_GROUP_STATS_WEEKLY_GROUPS``，逗号分隔）。

    **空 = 一条都不推**（fail-closed，与 AGENT_GROUP_STATS/AGENT_WEB_TOKEN
    同文化：忘记配 = 静默不推，不会误发到不想收的群）。只管推送——采集与
    群命令/skill 按 AGENT_GROUP_STATS_ALLOWED_GROUPS（空集回落 ALLOWED_GROUPS），
    范围窄于聊天范围是正常产品形态。

    条目做 ASCII 数字校验：全角「１２３」之类永远匹配不上真实群号，告警后
    剔除（AGENTS §5「全角数字通过 isdigit」坑的预防性落地）。
    """
    entries = [
        x.strip() for x in os.getenv("AGENT_GROUP_STATS_WEEKLY_GROUPS", "").split(",")
    ]
    out = set()
    for entry in entries:
        if not entry:
            continue
        if not (entry.isascii() and entry.isdigit()):
            logger.warning(
                "AGENT_GROUP_STATS_WEEKLY_GROUPS 含非 QQ 号条目 %r（永远不会命中，已剔除）",
                entry,
            )
            continue
        out.add(entry)
    return frozenset(out)


def extra_stopwords() -> frozenset[str]:
    """部署者追加停用词（逗号分隔），叠加 tokenize.STOPWORDS。"""
    raw = str(os.getenv("AGENT_GROUP_STATS_STOPWORDS") or "")
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def _qq_group_ids(raw: str, label: str) -> frozenset[str]:
    """解析逗号分隔的群号；非 ASCII 数字条目告警剔除。

    全角「１２３」之类 ``isdigit()`` 为真但永远匹配不上真实群号（AGENTS §5
    同类坑），这里用 ASCII 判定兜住，避免白名单静默失效。
    """
    out: set[str] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if not (entry.isascii() and entry.isdigit()):
            logger.warning("%s 含非 QQ 号条目 %r（永远不会命中，已剔除）", label, entry)
            continue
        out.add(entry)
    return frozenset(out)


def allowed_groups() -> frozenset[str]:
    """统计采集范围。

    默认与 ``acl.ALLOWED_GROUPS`` **同一口径**（同一 env 键）；配置
    ``AGENT_GROUP_STATS_ALLOWED_GROUPS`` 可收窄到其中一部分群。收窄列表
    永远不能超出 ``ALLOWED_GROUPS``：越界条目告警后忽略（fail-closed，
    避免 ACL 不认的群被静默采集）。

    这里不 import plugins.acl（分层铁律：agentcore 不碰插件层），按同一套
    解析规则现算；口径一致性由 tests/test_group_stats.py 的锁死用例保证。
    """
    chat_allowed = frozenset(
        x.strip() for x in os.getenv("ALLOWED_GROUPS", "").split(",") if x.strip()
    )
    raw = (os.getenv("AGENT_GROUP_STATS_ALLOWED_GROUPS") or "").strip()
    if not raw:
        return chat_allowed
    wanted = _qq_group_ids(raw, "AGENT_GROUP_STATS_ALLOWED_GROUPS")
    outside = wanted - chat_allowed
    if outside:
        logger.warning(
            "AGENT_GROUP_STATS_ALLOWED_GROUPS 含不在 ALLOWED_GROUPS 的群 %s，已忽略",
            ",".join(sorted(outside)),
        )
    return wanted & chat_allowed


# --------------------------------------------------------------- 原文归档

_ARCHIVE_OFF = {"0", "false", "no", "off"}


def archive_enabled() -> bool:
    """全量群消息原文归档总开关。**默认关闭**（opt-in）。

    与聚合统计（group_msg_daily）是两个独立开关：归档存**消息原文**
    （text/reply_to/forward_id），隐私口径比聚合敏感一个量级，必须显式开。
    """
    raw = str(os.getenv("AGENT_GROUP_ARCHIVE") or "").strip().lower()
    if not raw:
        return False
    return raw not in _ARCHIVE_OFF


def archive_retention_days() -> int:
    """原文归档保留天数（默认 30）。按 day 列每日 prune。"""
    return _int("AGENT_GROUP_ARCHIVE_RETENTION_DAYS", 30, minimum=1)


def archive_flush_seconds() -> int:
    """原文归档批量落库周期，也即 kill -9 时的丢失窗口上限。"""
    return _int("AGENT_GROUP_ARCHIVE_FLUSH_SECONDS", 30, minimum=1)


def archive_buffer_max() -> int:
    """内存缓冲行数上限：超了丢最旧并 ERROR（内存有界，宁可丢不 OOM）。"""
    return _int("AGENT_GROUP_ARCHIVE_BUFFER_MAX", 50000, minimum=1000)
