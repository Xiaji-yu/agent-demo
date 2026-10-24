"""主动联系（nudge）：bot 在私聊里**主动开口**的那一句话。

与 M7 定时内容推送（``agentcore/scheduler/push.py``）的分界：

- push = 运维通知。配置里写死 cron 与 prompt，到点生成正文投递，**不写历史**、
  不带人格与记忆，本质是"公告板"。
- nudge = 社交行为。它要知道你们上次聊到什么、你多久没说话、现在是不是该安静，
  生成后**写回对话历史**——否则你回一句"什么热点？"它完全接不住。

设计来源（2026-10-23 私聊日志复盘）：13 天 83 条私聊，71% 集中在 3 天、
14 天中 5 天零私聊、最长空档 119.8h、35% 的消息发生在 00:00–05:59，facts 里
有"需要上晚班 / 会通宵 vibe coding"。因此它是**沉默检测器而不是闹钟**：
倒计时从你最后一条消息起算，你一发话就重置；同时用静默时段 + 晚睡保护 +
活跃聊天保护把"不打扰"做成硬约束。

三条不变式（改这个文件前先对照）：

1. **不重发**：投递结果不确定（``uncertain``）时既不写历史也不重发——与
   reminder/push 同口径，``Sink`` 的三态语义是唯一判据。
2. **最多 3 条**：连续未回复的主动消息上限 3 条，第 3 条固定是第 14 天的
   告别（"我先安静一阵"），之后停到你先开口。绝不出现第 4 条。
   **只有普通搭话与告别计入 ``unanswered``**：爆点/事件是"有由头的一次性
   消息"，不占告别名额（否则 2 条新闻就能让 bot 突然告别，3 条新闻就直接
   静默、连告别都没有）。
3. **写回只认"已送达"**：只有 ``sent`` 才 ``append_message(assistant)``，
   且 bot 自己的话不进入事实抽取、不推进摘要水位。

状态存放：复用 ``schedules`` 表（``action='nudge'``，``user_id=__nudge__``），
**无 DDL 变更**，因此备份/恢复自动覆盖；每次 tick 后用
``store.schedule_set_params`` 整体替换 params 里的状态字典。
配置放在 ``data/nudge.json``（0600、原子写、每 tick 按 mtime 热读），
与 llm-providers / search-providers 同模式——因为 Web 要能改它。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentcore import tz
from agentcore.budget import get_budget, route_context
from agentcore.diagnostics import record as _diag_record
from agentcore.safety import fence_untrusted
from agentcore.scheduler.events import (
    DEFAULT_EVENT_WORDS,
    EVENT_FOLLOWUP_TASK,
    EVENT_RESULT_TASK,
    EVENT_TASK,
    extract_birthday,
    extract_events,
    triggers_for,
)
from agentcore.scheduler.hotspot import (
    BREAKING_TASK,
    HOT_TASK,
    candidate_block,
    pick_category,
    rule_prefilter,
    score_candidates,
    week_kind,
)
from agentcore.scheduler.push import parse_target
from agentcore.skills.search import search_items

logger = logging.getLogger(__name__)

OWNER = "__nudge__"
ACTION_NUDGE = "nudge"

# 真人 QQ 号是 ASCII 数字，不会与这个系统用户名撞名
DEFAULT_CONFIG_FILE = "data/nudge.json"
DEFAULT_HOTSPOT_FILE = "data/nudge-hotspot.json"
DEFAULT_NICKNAME = "夏祭"

# 未回复上限（含告别那一条）
MAX_UNANSWERED = 3
# 默认静默时段：23:30–10:00（跨零点）。来源：用户常在 00–06 点活跃，
# 08:00 解除静默大概率是他刚睡下 3–4 小时（日志实测）
DEFAULT_QUIET_START = "23:30"
DEFAULT_QUIET_END = "10:00"
# 晚睡保护：最后一条消息晚于该时刻 → 最早发送时间再推迟 late_night_extra
DEFAULT_LATE_NIGHT_AFTER = "01:00"
DEFAULT_LATE_NIGHT_EXTRA = 6 * 3600
# 活跃聊天保护：你最后一条消息后这么久内绝不主动（含爆点）
DEFAULT_MIN_IDLE = 30 * 60
# 第 1 条：最后一条消息后 6–24h 随机；第 2 条：第 1 条后 +48h；
# 第 3 条：第 14 天的告别
DEFAULT_FIRST_MIN = 6 * 3600
DEFAULT_FIRST_MAX = 24 * 3600
DEFAULT_SECOND_DELAY = 48 * 3600
DEFAULT_GOODBYE_DAYS = 14

_MAX_CHARS = 60
_MAX_RECENT_TEXTS = 5
_STATE_KEY_GUARD = 200  # params 体积护栏：异常膨胀时告警（不清数据）

_NORMAL_TASK = (
    "写一条自然的搭话：可以分享一个你刚想到的点、接一下你们最近聊过的话题，"
    "或者随便问一句他今天怎么样。可以提问也可以不提问，看话题合不合适。"
)
_GOODBYE_TASK = (
    "你有一段时间没等到他回复了。写一句简短的告别：让他知道你会先安静一阵，"
    "想聊了他随时开口。语气要轻，不要抱怨、不要质问、不要撒娇过头。"
)

# 称呼只从"第一人称"事实里取：facts 里同时存在「用户昵称为夏祭」与
# 「用户提及的夏祭是其所在群的群成员」——后者是第三人称，匹配上就会把
# 群成员当成用户本人（2026-10-23  facts 实测）。
_NICKNAME_RES = [
    re.compile(p)
    for p in (
        r"用户昵称为([^，。；、\s\"'“”‘’]{1,12})",
        r"用户希望被称呼为([^，。；、\s\"'“”‘’]{1,12})",
        r"用户称呼自己为([^，。；、\s\"'“”‘’]{1,12})",
        r"用户要求称呼(?:我|自己)为([^，。；、\s\"'“”‘’]{1,12})",
        r"叫我([^，。；、\s\"'“”‘’]{1,12})",
    )
]
# 「在忙 / 别烦」类回复 → 暂停 24h。刻意保守：只列明确说法，
# 不匹配"我不忙"（没有对应词条），误判代价是少发一条而非多发一条。
_BUSY_RE = re.compile(
    r"(在忙|正忙|忙着呢|没空|没时间|别烦|别打扰|不想聊|别发了|先不聊|"
    r"稍后聊|晚点聊|在开会|工作中|别找我)"
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")


# ---------------------------------------------------------------------------
# 配置


def _env_flag(env, name: str) -> bool | None:
    """布尔 env：``0/false/no/off`` 为关，``1/true/yes/on`` 为开，空/脏值 None。"""
    raw = str(env.get(name) or "").strip().lower()
    if not raw:
        return None
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    logger.warning("%s=%r 不是布尔，已忽略（按配置文件取值）", name, raw)
    return None


def _env_str(env, name: str) -> str:
    return str(env.get(name) or "").strip()


def _coerce_bool(raw, default: bool, *, name: str) -> bool:
    """配置文件里的布尔：容忍 ``"false"``/``"0"`` 这类字符串。

    不能直接用 ``bool(raw)``：JSON 里写成 ``"enabled": "false"`` 时 ``bool``
    为 True——这是"手滑就打开主动发言"的危险方向，必须按字面解析。
    """
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int | float) and raw in (0, 1):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    logger.warning("nudge: %s=%r 不是布尔，回退 %s", name, raw, default)
    return default


def _parse_hhmm(value, default: str) -> str:
    """``HH:MM`` 归一；非法值告警并回退默认（启动不能被一个笔误炸掉）。"""
    text = str(value or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", text)
    if not m:
        if value not in (None, ""):
            logger.warning("nudge: 时刻 %r 不是 HH:MM，回退 %s", value, default)
        return default
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        logger.warning("nudge: 时刻 %r 越界，回退 %s", value, default)
        return default
    return f"{hh:02d}:{mm:02d}"


def _minutes(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def _clamp_int(value, default: int, *, minimum: int, maximum: int, name: str) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        if value not in (None, ""):
            logger.warning("nudge: %s=%r 不是整数，回退 %s", name, value, default)
        return default
    if out < minimum or out > maximum:
        logger.warning("nudge: %s=%s 超出 [%s,%s]，已钳制", name, out, minimum, maximum)
        return max(minimum, min(maximum, out))
    return out


@dataclass
class NudgeConfig:
    """主动联系的全部可调参数（``data/nudge.json`` 的 schema）。"""

    enabled: bool = False
    targets: list[str] = field(default_factory=list)
    quiet_start: str = DEFAULT_QUIET_START
    quiet_end: str = DEFAULT_QUIET_END
    late_night_after: str = DEFAULT_LATE_NIGHT_AFTER
    late_night_extra_seconds: int = DEFAULT_LATE_NIGHT_EXTRA
    min_idle_seconds: int = DEFAULT_MIN_IDLE
    first_min_seconds: int = DEFAULT_FIRST_MIN
    first_max_seconds: int = DEFAULT_FIRST_MAX
    second_delay_seconds: int = DEFAULT_SECOND_DELAY
    goodbye_after_days: int = DEFAULT_GOODBYE_DAYS
    max_unanswered: int = MAX_UNANSWERED
    busy_pause_seconds: int = 24 * 3600
    # 当日普通搭话条数上限；0 = 不限（与 push.daily_cap_per_target 同口径）
    daily_cap_normal: int = 1
    max_daily_total: int = 3
    max_chars: int = _MAX_CHARS
    nickname: str = ""
    tick_seconds: int = 600
    retry_delay_seconds: int = 300
    max_retry_delay_seconds: int = 86400
    max_failures: int = 5
    # ---- 热点（P2）----
    hot_topics: bool = False
    hot_poll_hours: int = 4
    hot_tech_queries: list[str] = field(
        default_factory=lambda: ["AI 大模型", "人工智能 新品", "芯片 科技", "开源 模型"]
    )
    hot_general_queries: list[str] = field(
        default_factory=lambda: ["今日热点", "国内新闻", "科技 新闻"]
    )
    # 领域词：命中即算 tech 类；重大词：命中算 general 但值得看
    hot_domain_terms: list[str] = field(
        default_factory=lambda: [
            "后端",
            "安全",
            "漏洞",
            "CVE",
            "AI",
            "大模型",
            "运维",
            "容器",
            "Kubernetes",
            "前端",
            "芯片",
            "开源",
            "数据库",
            "云",
            "API",
        ]
    )
    hot_major_terms: list[str] = field(
        default_factory=lambda: [
            "宕机",
            "断网",
            "泄露",
            "收购",
            "发布",
            "上线",
            "监管",
            "裁员",
            "漏洞",
            "攻击",
            "封锁",
            "涨价",
            "停服",
        ]
    )
    # 禁区：命中即丢（政治/伤亡/医疗/荐股/塌房一类）
    hot_banned_terms: list[str] = field(
        default_factory=lambda: [
            "政治",
            "选举",
            "总统",
            "战争",
            "空袭",
            "死亡",
            "遇难",
            "地震",
            "疫情",
            "确诊",
            "荐股",
            "涨停",
            "暴跌",
            "塌房",
            "离婚",
            "分手",
        ]
    )
    # 自媒体/聚合站黑名单（按域名子串匹配）
    hot_source_blacklist: list[str] = field(default_factory=list)
    hot_break_score: int = 8
    hot_candidate_score: int = 6
    # ≥9 分可把当天上限从 3 破例到 4
    hot_override_score: int = 9
    breaking_override_cap: int = 4
    hot_candidate_ttl_hours: int = 12
    hot_dedupe_days: int = 90
    hot_break_dedupe_hours: int = 24
    hot_max_results: int = 5
    hot_max_polls_per_day: int = 15
    hot_tech_weight: float = 0.7
    hot_general_weight: float = 0.5
    # ---- 记忆事件（P3）----
    memory_events: bool = False
    event_lookahead_days: int = 60
    event_max_pending: int = 20
    event_followup_hours: int = 24
    # 事件词表（Web 可改）：键来自 DEFAULT_EVENT_WORDS，未收录的词按"约会"处理
    event_words: list[str] = field(default_factory=lambda: list(DEFAULT_EVENT_WORDS))

    def quiet_window(self) -> tuple[int, int]:
        return _minutes(self.quiet_start), _minutes(self.quiet_end)


_KNOWN_KEYS = set(NudgeConfig().__dataclass_fields__)


def _coerce_list(raw, name: str, *, limit: int = 20, max_len: int = 40) -> list[str]:
    """字符串列表收敛：只留非空串、去重、限长（防一份配置把 prompt 撑爆）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning("nudge: %s 不是列表，已忽略", name)
        return []
    out: list[str] = []
    for item in raw[:limit]:
        text = _CONTROL_CHAR_RE.sub("", str(item or "")).strip()[:max_len]
        if text and text not in out:
            out.append(text)
    return out


def _coerce_float(raw, default: float, *, low: float, high: float, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        if raw not in (None, ""):
            logger.warning("nudge: %s=%r 不是数字，回退 %s", name, raw, default)
        return default
    if not (low <= value <= high):
        logger.warning("nudge: %s=%s 超出 [%s,%s]，已钳制", name, value, low, high)
        return max(low, min(high, value))
    return value


_INT_SPECS = (
    ("late_night_extra_seconds", 0, 24 * 3600),
    ("min_idle_seconds", 0, 24 * 3600),
    ("first_min_seconds", 60, 30 * 24 * 3600),
    ("first_max_seconds", 60, 30 * 24 * 3600),
    ("second_delay_seconds", 3600, 30 * 24 * 3600),
    ("goodbye_after_days", 1, 365),
    ("max_unanswered", 1, 10),
    ("busy_pause_seconds", 0, 30 * 24 * 3600),
    ("daily_cap_normal", 0, 50),
    ("max_daily_total", 1, 50),
    ("max_chars", 10, 500),
    ("tick_seconds", 30, 24 * 3600),
    ("retry_delay_seconds", 30, 86400),
    ("max_retry_delay_seconds", 60, 7 * 86400),
    ("max_failures", 1, 50),
    ("hot_poll_hours", 1, 24),
    ("hot_break_score", 1, 10),
    ("hot_candidate_score", 1, 10),
    ("hot_override_score", 1, 10),
    ("breaking_override_cap", 1, 20),
    ("hot_candidate_ttl_hours", 1, 72),
    ("hot_dedupe_days", 1, 3650),
    ("hot_break_dedupe_hours", 1, 720),
    ("hot_max_results", 1, 20),
    ("hot_max_polls_per_day", 1, 200),
    ("event_lookahead_days", 1, 365),
    ("event_max_pending", 1, 100),
    ("event_followup_hours", 0, 168),
)
_FLOAT_SPECS = (
    ("hot_tech_weight", 0.0, 1.0),
    ("hot_general_weight", 0.0, 1.0),
)
_BOOL_FIELDS = ("enabled", "hot_topics", "memory_events")
_HHMM_FIELDS = ("quiet_start", "quiet_end", "late_night_after")
# (字段名, 条数上限, 单条长度上限, 空结果是否回退默认)
_LIST_SPECS = (
    ("hot_tech_queries", 20, 40, True),
    ("hot_general_queries", 20, 40, True),
    ("hot_domain_terms", 40, 40, False),
    ("hot_major_terms", 40, 40, False),
    ("hot_banned_terms", 60, 40, False),
    ("hot_source_blacklist", 40, 80, False),
)


def _coerce_scalars(cfg: NudgeConfig, raw: dict) -> None:
    for name in _BOOL_FIELDS:
        setattr(cfg, name, _coerce_bool(raw.get(name), getattr(cfg, name), name=name))
    for name in _HHMM_FIELDS:
        setattr(cfg, name, _parse_hhmm(raw.get(name), getattr(cfg, name)))
    for name, minimum, maximum in _INT_SPECS:
        setattr(
            cfg,
            name,
            _clamp_int(
                raw.get(name),
                getattr(cfg, name),
                minimum=minimum,
                maximum=maximum,
                name=name,
            ),
        )
    for name, low, high in _FLOAT_SPECS:
        setattr(
            cfg,
            name,
            _coerce_float(
                raw.get(name), getattr(cfg, name), low=low, high=high, name=name
            ),
        )


def _coerce_lists(cfg: NudgeConfig, raw: dict) -> None:
    for name, limit, max_len, keep_default_on_empty in _LIST_SPECS:
        value = _coerce_list(raw.get(name), name, limit=limit, max_len=max_len)
        if keep_default_on_empty and not value:
            value = list(getattr(cfg, name))
        setattr(cfg, name, value)


def _fix_pair_order(cfg: NudgeConfig) -> None:
    """成对阈值：顺序反了就互换（配置笔误不该让功能静默失效）。"""
    if cfg.first_max_seconds < cfg.first_min_seconds:
        logger.warning(
            "nudge: first_max(%s) < first_min(%s)，已互换",
            cfg.first_max_seconds,
            cfg.first_min_seconds,
        )
        cfg.first_min_seconds, cfg.first_max_seconds = (
            cfg.first_max_seconds,
            cfg.first_min_seconds,
        )
    if cfg.hot_candidate_score > cfg.hot_break_score:
        logger.warning(
            "nudge: hot_candidate_score(%s) > hot_break_score(%s)，已互换",
            cfg.hot_candidate_score,
            cfg.hot_break_score,
        )
        cfg.hot_candidate_score, cfg.hot_break_score = (
            cfg.hot_break_score,
            cfg.hot_candidate_score,
        )


def _coerce_targets(raw: dict) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for item in raw.get("targets") or []:
        target = str(item or "").strip()
        parsed = parse_target(target)
        if parsed is None:
            logger.warning("nudge: 目标 %r 非法（应为 private:<QQ号>），已跳过", target)
            continue
        kind, _ident = parsed
        if kind != "private":
            # v1 只做私聊：群主动发言是另一套 ACL 与体验问题，先不做
            logger.warning("nudge: 暂不支持群目标 %r，已跳过", target)
            continue
        if target in seen:
            continue
        seen.add(target)
        targets.append(target)
    return targets


def _coerce(raw: dict) -> NudgeConfig:
    """把任意 dict 收敛成 NudgeConfig：未知键告警、越界钳制、目标去重校验。"""
    unknown = sorted(set(raw) - _KNOWN_KEYS)
    if unknown:
        logger.warning("nudge: 配置含未知键 %s（已忽略，可能是拼写错误）", unknown)
    cfg = NudgeConfig()
    _coerce_scalars(cfg, raw)
    _coerce_lists(cfg, raw)
    _fix_pair_order(cfg)

    raw_words = raw.get("event_words")
    if raw_words is None:
        cfg.event_words = list(cfg.event_words)
    else:
        # 显式空列表 = 关闭全部事件词；不能用 ``or`` 回退默认（否则关不掉）
        cfg.event_words = _coerce_list(raw_words, "event_words", limit=60, max_len=12)

    nickname = str(raw.get("nickname") or "").strip()
    cfg.nickname = _CONTROL_CHAR_RE.sub("", nickname)[:12]
    cfg.targets = _coerce_targets(raw)
    return cfg


def default_config() -> NudgeConfig:
    return NudgeConfig()


def load_nudge_config(
    path: str | Path | None = None,
    *,
    config: dict | None = None,
    env=None,
) -> NudgeConfig:
    """读配置：env 总开关 > ``data/nudge.json`` > ``config.yaml`` 的 nudge 段 > 内置默认。

    文件缺失/损坏都**不抛异常**：回退到 config.yaml seed 或默认值并告警——
    主动联系是增益功能，不能因为它把 bot 启动搞崩。
    """
    env = os.environ if env is None else env
    cfg_path = Path(
        _env_str(env, "AGENT_NUDGE_CONFIG") or str(path or DEFAULT_CONFIG_FILE)
    )
    raw: dict = {}
    seed = (config or {}).get("nudge") if isinstance(config, dict) else None
    if isinstance(seed, dict):
        raw = dict(seed)
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if seed:
            logger.info("nudge: %s 不存在，先用 config.yaml 的 nudge 段", cfg_path)
    except OSError:
        logger.warning("nudge: 读取 %s 失败，用默认配置", cfg_path, exc_info=True)
    else:
        try:
            loaded = json.loads(text)
        except Exception:
            logger.warning(
                "nudge: %s 不是合法 JSON，已忽略该文件（用默认/seed 配置）", cfg_path
            )
        else:
            if isinstance(loaded, dict):
                raw = loaded
            else:
                logger.warning("nudge: %s 顶层不是对象，已忽略", cfg_path)

    cfg = _coerce(raw)
    tick = _env_str(env, "AGENT_NUDGE_TICK")
    if tick:
        # 运维旋钮：检查间隔与 enabled 一样走 env（其余参数在配置文件里）
        cfg.tick_seconds = _clamp_int(
            tick,
            cfg.tick_seconds,
            minimum=30,
            maximum=24 * 3600,
            name="AGENT_NUDGE_TICK",
        )
    switch = _env_flag(env, "AGENT_NUDGE_ENABLED")
    if switch is not None:
        if switch != cfg.enabled:
            logger.info(
                "nudge: AGENT_NUDGE_ENABLED 覆盖配置 enabled=%s → %s",
                cfg.enabled,
                switch,
            )
        cfg.enabled = switch
    return cfg


def save_nudge_config(path: str | Path, cfg: NudgeConfig) -> bool:
    """原子写配置（.part + os.replace，0600）。失败只告警并返回 False。"""
    target = Path(path)
    payload = {
        "enabled": cfg.enabled,
        "targets": cfg.targets,
        "quiet_start": cfg.quiet_start,
        "quiet_end": cfg.quiet_end,
        "late_night_after": cfg.late_night_after,
        "late_night_extra_seconds": cfg.late_night_extra_seconds,
        "min_idle_seconds": cfg.min_idle_seconds,
        "first_min_seconds": cfg.first_min_seconds,
        "first_max_seconds": cfg.first_max_seconds,
        "second_delay_seconds": cfg.second_delay_seconds,
        "goodbye_after_days": cfg.goodbye_after_days,
        "max_unanswered": cfg.max_unanswered,
        "busy_pause_seconds": cfg.busy_pause_seconds,
        "daily_cap_normal": cfg.daily_cap_normal,
        "max_daily_total": cfg.max_daily_total,
        "max_chars": cfg.max_chars,
        "nickname": cfg.nickname,
        "tick_seconds": cfg.tick_seconds,
        "retry_delay_seconds": cfg.retry_delay_seconds,
        "max_retry_delay_seconds": cfg.max_retry_delay_seconds,
        "max_failures": cfg.max_failures,
        "hot_topics": cfg.hot_topics,
        "hot_poll_hours": cfg.hot_poll_hours,
        "hot_tech_queries": cfg.hot_tech_queries,
        "hot_general_queries": cfg.hot_general_queries,
        "hot_domain_terms": cfg.hot_domain_terms,
        "hot_major_terms": cfg.hot_major_terms,
        "hot_banned_terms": cfg.hot_banned_terms,
        "hot_source_blacklist": cfg.hot_source_blacklist,
        "hot_break_score": cfg.hot_break_score,
        "hot_candidate_score": cfg.hot_candidate_score,
        "hot_override_score": cfg.hot_override_score,
        "breaking_override_cap": cfg.breaking_override_cap,
        "hot_candidate_ttl_hours": cfg.hot_candidate_ttl_hours,
        "hot_dedupe_days": cfg.hot_dedupe_days,
        "hot_break_dedupe_hours": cfg.hot_break_dedupe_hours,
        "hot_max_results": cfg.hot_max_results,
        "hot_max_polls_per_day": cfg.hot_max_polls_per_day,
        "hot_tech_weight": cfg.hot_tech_weight,
        "hot_general_weight": cfg.hot_general_weight,
        "memory_events": cfg.memory_events,
        "event_lookahead_days": cfg.event_lookahead_days,
        "event_max_pending": cfg.event_max_pending,
        "event_followup_hours": cfg.event_followup_hours,
        "event_words": cfg.event_words,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = Path(f"{target}.part")
        part.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.chmod(part, 0o600)
        os.replace(part, target)
        return True
    except OSError:
        logger.warning("nudge: 配置写入失败（%s）", target, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# 纯函数：时间守卫 / 文本截断


def _local(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(float(ts), tz.zoneinfo())


def in_quiet(cfg: NudgeConfig, ts: float) -> bool:
    """该时刻是否落在静默时段内（支持 23:30→10:00 这类跨零点窗口）。

    ``quiet_start == quiet_end`` 视为**没有静默时段**。按 ``cur >= start or
    cur < end`` 判定会把相等算成"全天静默"，而 Web 上把两个时刻填成一样正是
    用户表达"不要静默"的最自然方式；误判的后果是主动消息永远不发、``next_run``
    被一圈圈推到几天后。
    """
    cur = _local(ts).hour * 60 + _local(ts).minute
    start, end = cfg.quiet_window()
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def quiet_end_ts(cfg: NudgeConfig, ts: float) -> float:
    """ts 所在（或下一个）静默窗口的结束时刻。"""
    now = _local(ts)
    end_h, end_m = divmod(_minutes(cfg.quiet_end), 60)
    candidate = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    return candidate.timestamp()


def after_late_night(cfg: NudgeConfig, ts: float) -> bool:
    """该时刻是否落在"凌晨窗口"内（``late_night_after`` → ``quiet_end``）。

    语义是"你刚熬到这么晚"，所以窗口右边界是静默结束：12:00 说话不算晚睡，
    23:50 说话则由静默时段本身挡住（不需要再叠一层保护）。
    """
    cur = _local(ts).hour * 60 + _local(ts).minute
    start = _minutes(cfg.late_night_after)
    end = _minutes(cfg.quiet_end)
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def earliest_allowed(cfg: NudgeConfig, ts: float, last_user: float | None) -> float:
    """把计划时刻推到"允许发送"的最早位置。

    两条约束可能互相触发（晚睡保护的 6 小时落点仍在静默时段内），因此用有界
    循环反复推后而不是一次性判断——上限 6 次足以覆盖任何合法配置组合。
    """
    for _ in range(6):
        if in_quiet(cfg, ts):
            ts = quiet_end_ts(cfg, ts)
            continue
        if (
            last_user is not None
            and after_late_night(cfg, last_user)
            and ts < last_user + cfg.late_night_extra_seconds
        ):
            ts = last_user + cfg.late_night_extra_seconds
            continue
        break
    return ts


def clip_at_sentence(text: str, limit: int) -> str:
    """按句子边界截断到 limit 字；找不到边界才硬截（不切半句）。

    与 persona growth 的 `_clip_at_sentence` 同款问题：主动消息被裸切片成
    "我昨天看到个特别…" 比长一点更难读。
    """
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    window = text[:limit]
    cut = -1
    for i, ch in enumerate(window):
        if ch in "。！？!?，,":
            cut = i
    if cut >= max(1, int(limit * 0.5)):
        return window[: cut + 1].strip()
    return window.strip()


def nickname_from_facts(facts) -> str:
    """从长期事实里取称呼；取不到返回空串（由调用方回落默认值）。

    ``store.list_facts`` 的契约是 **``list[str]``**（事实正文，最新优先）；
    dict 形态（``{"content": ...}``）也容忍——2026-10-23 线上实测：按 dict
    读会 ``AttributeError: 'str' object has no attribute 'get'``，
    把一次主动搭话整个炸掉（`/nudge run` 直接报错给用户）。
    """
    for item in facts or []:
        if isinstance(item, dict):
            content = str(item.get("content") or "")
        else:
            content = str(item or "")
        content = _CONTROL_CHAR_RE.sub("", content).strip()
        if not content:
            continue
        for pattern in _NICKNAME_RES:
            m = pattern.search(content)
            if m:
                name = m.group(1).strip("。，、；：！? ")
                if name:
                    return name[:12]
    return ""


def is_busy_reply(text: str) -> bool:
    return bool(_BUSY_RE.search(text or ""))


# ---------------------------------------------------------------------------
# 服务


class NudgeService:
    """到点判断"该不该主动"，生成并投递一句话。

    由 ``AgentScheduler`` 按 ``tick_seconds`` 周期调用 ``tick()``；
    每条用户私聊由插件层调用 ``note_user_message()`` 重置倒计时。
    """

    def __init__(
        self,
        store,
        sink,
        engine,
        *,
        llm=None,
        config: NudgeConfig | None = None,
        config_path: str | Path | None = None,
        on_alert=None,
        rng: random.Random | None = None,
        seed: dict | None = None,
    ) -> None:
        self.store = store
        self.sink = sink
        self.engine = engine
        # 评分用裸 LLMClient（与 PushService 同款入参）；未显式给时从引擎上取
        self.llm = llm if llm is not None else getattr(engine, "llm", None)
        self.on_alert = on_alert
        self._rng = rng or random.Random()
        # config.yaml 的 nudge 段：优先级是「文件 > seed > 默认」。文件缺失/损坏
        # 时才用 seed；热读也必须带上它，否则同一个坏文件在启动时用 seed、
        # 第一个 tick 就掉回内置默认
        self._seed = seed
        self._config_path = Path(
            config_path
            or os.getenv("AGENT_NUDGE_CONFIG", "").strip()
            or DEFAULT_CONFIG_FILE
        )
        self._config: NudgeConfig = config or default_config()
        self._cfg_mtime: float | None = None
        # asyncio.Lock 懒加载（构造可能在无事件循环处发生）：串行化 tick 与
        # run_now——调度器只保证定时任务不重入，手动 `/nudge run` 不在其中
        self._lock: asyncio.Lock | None = None
        self._hot: dict = {
            "last_poll": 0.0,
            "polls": {},
            "pending": [],
            "mentioned": {},
        }
        self._load_hot()

    # ---------- 配置 ----------

    def _cfg(self) -> NudgeConfig:
        """按 mtime 热读配置；内容没变就不重新解析（Web 改完即时生效）。

        文件不存在时**保留当前配置**：启动时那份可能来自 config.yaml 的 seed 或
        env 覆盖，此时若按"读文件"回落成内置默认，seed 部署会被静默丢掉。
        """
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError:
            return self._config
        if mtime == self._cfg_mtime:
            return self._config
        self._config = load_nudge_config(self._config_path, config=self._seed)
        self._cfg_mtime = mtime
        return self._config

    def reload_config(self) -> NudgeConfig:
        """强制重读（Web 写完后调用，避免 mtime 精度问题）。"""
        self._cfg_mtime = None
        return self._cfg()

    def enabled_now(self) -> bool:
        """当前**生效**的开关值（``AGENT_NUDGE_ENABLED`` 覆盖之后）。"""
        return bool(self._cfg().enabled)

    def _lock_obj(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def set_enabled(self, enabled: bool) -> bool:
        """开关主动联系（``/nudge on|off``）：落盘 + 立即重读。"""
        cfg = self._cfg()
        cfg.enabled = bool(enabled)
        ok = save_nudge_config(self._config_path, cfg)
        if ok:
            self.reload_config()
        return ok

    # ---------- 目标行 ----------

    async def _nudge_rows(self, include_disabled: bool = False) -> list[dict]:
        rows = await self.store.schedule_list(OWNER, include_disabled=True)
        return [
            r
            for r in rows
            if r.get("action") == ACTION_NUDGE
            and (include_disabled or r.get("enabled"))
        ]

    async def register_targets(self) -> dict:
        """把配置里的目标同步成调度行（幂等）。

        与 push 的 register_jobs 同策略：配置里删掉的目标**停用**其残留行
        （否则照旧投递）；已停用的行不因配置没变而复活。
        """
        stats = {"added": 0, "kept": 0, "removed": 0}
        cfg = self._cfg()
        rows = await self._nudge_rows(include_disabled=True)
        by_target = {str(r.get("target")): r for r in rows}
        for target in cfg.targets:
            row = by_target.get(target)
            if row is None:
                await self.store.schedule_add(
                    kind="once",
                    target=target,
                    message="",
                    user_id=OWNER,
                    cron="",
                    next_run=None,
                    action=ACTION_NUDGE,
                    params={"unanswered": 0},
                )
                stats["added"] += 1
                logger.info("nudge: 新目标 %s 已登记", target)
            else:
                stats["kept"] += 1
        for target, row in by_target.items():
            if target not in cfg.targets and row.get("enabled"):
                await self.store.schedule_mark_fired(str(row["id"]), None)
                stats["removed"] += 1
                logger.info("nudge: 目标 %s 已从配置移除，停用其调度行", target)
        return stats

    def _row_for(self, rows: list[dict], target: str) -> dict | None:
        return next((r for r in rows if str(r.get("target")) == target), None)

    # ---------- 热点状态（全局，不随目标重复） ----------

    def _hot_path(self) -> Path:
        env = (os.getenv("AGENT_NUDGE_HOTSPOT_FILE") or "").strip()
        return Path(env) if env else Path(DEFAULT_HOTSPOT_FILE)

    def _load_hot(self) -> None:
        """读回候选池 / 已提记录；损坏则当空处理（最坏是多提一次旧话题）。"""
        self._hot = {"last_poll": 0.0, "polls": {}, "pending": [], "mentioned": {}}
        try:
            raw = json.loads(self._hot_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            logger.warning("nudge: 热点状态文件损坏，按空处理", exc_info=True)
            return
        if not isinstance(raw, dict):
            return
        mentioned = {
            str(k): float(v)
            for k, v in (raw.get("mentioned") or {}).items()
            if isinstance(v, int | float)
        }
        self._hot = {
            "last_poll": float(raw.get("last_poll") or 0),
            "polls": raw.get("polls") if isinstance(raw.get("polls"), dict) else {},
            "pending": [c for c in raw.get("pending") or [] if isinstance(c, dict)],
            "mentioned": mentioned,
        }

    def _save_hot(self) -> None:
        """原子写（.part + os.replace，0600）；顺手清理过期记录。"""
        cfg = self._cfg()
        now = time.time()
        # 只留去重窗口内的"提到过"，防止字典无限增长
        keep_from = now - cfg.hot_dedupe_days * 86400
        self._hot["mentioned"] = {
            h: ts for h, ts in self._hot.get("mentioned", {}).items() if ts >= keep_from
        }
        path = self._hot_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            part = Path(f"{path}.part")
            part.write_text(json.dumps(self._hot, ensure_ascii=False), encoding="utf-8")
            os.chmod(part, 0o600)
            os.replace(part, path)
        except OSError:
            logger.warning("nudge: 热点状态写入失败（%s）", path, exc_info=True)

    def _prune_pending(self, cfg: NudgeConfig, now: float) -> None:
        """丢掉超过 TTL 的候选；``found_at`` 缺失按"刚刚发现"处理。

        线上实测（2026-10-23）：缺 ``found_at`` 时旧实现按 0（纪元）算，
        于是一次 tick 就把整个候选池清空——爆点还没发就"神秘消失"。
        """
        ttl = cfg.hot_candidate_ttl_hours * 3600
        kept = []
        for cand in self._hot.get("pending", []):
            found = float(cand.get("found_at") or 0)
            if found <= 0:
                found = now
            if now - found < ttl:
                kept.append(cand)
        self._hot["pending"] = kept

    async def _maybe_poll(self, cfg: NudgeConfig, now: float) -> None:
        """到点就检索一轮：规则筛 → LLM 打分 → 入候选池。"""
        if not cfg.hot_topics:
            return
        if self.llm is None:
            # 没有 LLM 就无法评分，而规则筛出来的候选不能直接拿去打扰人
            logger.warning("nudge: 热点监测没有可用 LLM，已跳过本轮轮询")
            return
        # 预算硬闸门：打分也是一次真实的 LLM 调用，预算用尽时连搜索配额一起省
        blocked, reason = get_budget().chat_blocked()
        if blocked:
            logger.info("nudge: 预算闸门命中，跳过热点轮询（%s）", reason)
            return
        if now - float(self._hot.get("last_poll") or 0) < cfg.hot_poll_hours * 3600:
            return
        day = tz.day_key(now)
        polls = self._hot.get("polls") or {}
        if not isinstance(polls, dict) or polls.get("day") != day:
            polls = {"day": day, "n": 0}
        if int(polls.get("n") or 0) >= cfg.hot_max_polls_per_day:
            return

        week = week_kind(now)
        category = pick_category(
            self._rng, week, cfg.hot_tech_weight, cfg.hot_general_weight
        )
        queries = (
            cfg.hot_tech_queries if category == "tech" else cfg.hot_general_queries
        )
        items: list[dict] = []
        for query in queries[:2]:  # 每轮最多 2 个查询，省搜索配额
            try:
                items.extend(await search_items(query, cfg.hot_max_results))
            except Exception:
                logger.warning("hotspot: 检索 %r 失败", query, exc_info=True)
        polls["n"] = int(polls.get("n") or 0) + 1
        self._hot["polls"] = polls
        self._hot["last_poll"] = now
        if not items:
            self._save_hot()
            return

        passed = rule_prefilter(
            items,
            domain_terms=cfg.hot_domain_terms,
            major_terms=cfg.hot_major_terms,
            banned_terms=cfg.hot_banned_terms,
            source_blacklist=cfg.hot_source_blacklist,
            mentioned=self._hot.get("mentioned", {}),
            pending_hashes={c.get("hash") for c in self._hot.get("pending", [])},
            break_dedupe_seconds=cfg.hot_break_dedupe_hours * 3600,
            now=now,
        )
        scored = await score_candidates(self.llm, passed) if passed else []
        added = 0
        for cand in scored:
            score = int(cand.get("score") or 0)
            if score >= cfg.hot_break_score:
                cand["kind"] = "breaking"
            elif score >= cfg.hot_candidate_score:
                cand["kind"] = "candidate"
            else:
                continue  # 低于备选线：不值得拿来开场
            cand["found_at"] = now
            self._hot["pending"].append(cand)
            added += 1
        self._prune_pending(cfg, now)
        self._save_hot()
        logger.info(
            "hotspot: 轮询 %s周/%s类，检索 %d 条 → 规则过 %d → 入选 %d（候选池 %d）",
            week,
            category,
            len(items),
            len(passed),
            added,
            len(self._hot["pending"]),
        )
        _diag_record(
            "nudge_hotspot_poll",
            week=week,
            category=category,
            items=len(items),
            passed=len(passed),
            added=added,
        )

    def _take_breaking(self, cfg: NudgeConfig, now: float) -> dict | None:
        """取分数最高、仍新鲜、24h 内没提过的爆点。"""
        best = None
        for cand in self._hot.get("pending", []):
            if cand.get("kind") != "breaking":
                continue
            if not self._fresh(cand, cfg, now):
                continue
            if now - float(self._hot["mentioned"].get(cand.get("hash") or "", 0)) < (
                cfg.hot_break_dedupe_hours * 3600
            ):
                continue
            if best is None or int(cand.get("score") or 0) > int(
                best.get("score") or 0
            ):
                best = cand
        return best

    def _take_candidate(self, cfg: NudgeConfig, now: float) -> dict | None:
        """取一个普通热点候选（按本周类别权重抽；没有就 None）。"""
        pool = [
            c
            for c in self._hot.get("pending", [])
            if c.get("kind") == "candidate" and self._fresh(c, cfg, now)
        ]
        # 90 天去重：提到过的 topic 不再当普通候选（爆点仍可按 24h 规则再提）
        pool = [
            c
            for c in pool
            if now - float(self._hot["mentioned"].get(c.get("hash") or "", 0))
            >= cfg.hot_dedupe_days * 86400
        ]
        if not pool:
            return None
        week = week_kind(now)
        want = pick_category(
            self._rng, week, cfg.hot_tech_weight, cfg.hot_general_weight
        )
        same = [c for c in pool if c.get("category") == want]
        return self._rng.choice(same or pool)

    def _fresh(self, cand: dict, cfg: NudgeConfig, now: float) -> bool:
        """时效：只认今天/昨天（Tavily 只给日期，没有小时）。"""
        date = str(cand.get("date") or "")[:10]
        return date in (tz.day_key(now), tz.day_key(now - 86400))

    def _consume(self, cand: dict, now: float) -> None:
        """发过了：记入 mentioned 并移出候选池。"""
        digest = str(cand.get("hash") or "")
        if digest:
            self._hot["mentioned"][digest] = now
        self._hot["pending"] = [
            c for c in self._hot.get("pending", []) if c.get("hash") != digest
        ]
        self._save_hot()

    def hotspot_status(self) -> dict:
        """热点监测状态（``/nudge`` 展示用）。"""
        return {
            "enabled": bool(self._cfg().hot_topics),
            "last_poll": float(self._hot.get("last_poll") or 0),
            "pending": len(self._hot.get("pending", [])),
            "breaking": sum(
                1 for c in self._hot.get("pending", []) if c.get("kind") == "breaking"
            ),
            "week": week_kind(time.time()),
            "polls_today": int((self._hot.get("polls") or {}).get("n") or 0),
        }

    # ---------- 记忆事件（P3） ----------

    def _event_words(self, cfg: NudgeConfig) -> dict:
        """配置里的事件词 → 类型；未收录的词按"约会"处理（仍受提前量表约束）。"""
        words = {}
        for word in cfg.event_words:
            words[word] = DEFAULT_EVENT_WORDS.get(word, "appointment")
        return words

    async def _scan_events_for(self, row: dict, cfg: NudgeConfig, now: float) -> None:
        """tick 入口：按目标行取 session 后扫描，并把 events 写回 params。"""
        target = str(row.get("target") or "")
        parsed = parse_target(target)
        if parsed is None or parsed[0] != "private":
            return
        user_id = str(parsed[1])
        session_id = await self.store.resolve_session(user_id, None)
        state = dict(row.get("params") or {})
        await self._scan_events(cfg, state, user_id, session_id, now)
        await self._save(str(row["id"]), state, row.get("next_run"))
        # 同步回传入的 row：tick 里 _process 用的是循环开局取到的那份，
        # 不同步的话它看到的是扫描前的旧 params（刚结案的事件会再发一次）
        row["params"] = state

    async def _scan_events(
        self, cfg: NudgeConfig, state: dict, user_id: str, session_id: str, now: float
    ) -> None:
        """增量扫你自己的消息，抽未来事件；生日每天从 facts 里查一次。

        走 ``get_history_window``（带 ts）而不是 ``get_session_messages_between``
        （不带 ts）："明天"要相对**说话那天**解析，没有时间戳就只能相对现在，
         bot 停机几天后再补扫就会整体错位。
        """
        if not cfg.memory_events:
            return
        events = [e for e in state.get("events") or [] if isinstance(e, dict)]
        last = int(state.get("last_scan_msg_id") or 0)
        newest = last
        try:
            window = await self.store.get_history_window(session_id, limit=200)
        except Exception:
            logger.warning("nudge: 事件扫描取历史失败", exc_info=True)
            window = []
        words = self._event_words(cfg)
        # 去重集合必须在循环外初始化：窗口里没有**新**用户消息（沉默检测的常态）
        # 时循环体一次都不执行，放在循环内会让生日分支引用未定义变量（每天首个
        # tick 直接抛 UnboundLocalError，且状态落不了盘 → 整天每个 tick 都抛）
        done_ids = set(state.get("events_done") or [])
        open_ids = {str(e.get("id") or "") for e in events}
        for msg in window:
            mid = int(msg.get("id") or 0)
            if mid <= last:
                continue
            newest = max(newest, mid)
            if msg.get("role") != "user":
                continue
            text = str(msg.get("content") or "")
            if not text.strip():
                continue
            said_at = float(msg.get("ts") or now)
            for index, ev in enumerate(
                extract_events(
                    text,
                    said_at,
                    event_words=words,
                    lookahead_days=cfg.event_lookahead_days,
                )
            ):
                # 一条消息可能同时提到两件事（"明天面试，后天出差"）：只用
                # e{mid} 会让第二件事与第一件撞 id，被当成重复丢掉且永不补扫
                eid = f"e{mid}" if index == 0 else f"e{mid}.{index}"
                if eid in done_ids or eid in open_ids:
                    # 已结案，或已经在池里等着：同一条消息不必每轮重复入池
                    continue
                open_ids.add(eid)
                events.append(
                    {
                        "id": eid,
                        "kind": ev["kind"],
                        "at": ev["at"],
                        "quote": ev["quote"],
                        "source_msg_id": mid,
                        "triggers": triggers_for(ev["kind"], ev["at"]),
                        "asked": 0,
                        "last_asked_at": 0,
                        "followed_up": False,
                        "done": False,
                    }
                )
        state["last_scan_msg_id"] = newest

        # 生日：只认 facts 里"生日是X月X日"这种明确句式，每天查一次
        day = tz.day_key(now)
        if state.get("birthday_day") != day:
            state["birthday_day"] = day
            try:
                facts = await self.store.list_facts(user_id, limit=200)
            except Exception:
                logger.warning("nudge: facts 读取失败，跳过生日", exc_info=True)
                facts = []
            found = extract_birthday(facts, now)
            # id 带年份：常量 "birthday" 一旦进了 events_done 就再也不会被加回来，
            # 生日祝福会变成"一辈子只发生日一次"
            bid = f"birthday-{_local(found['at']).year}" if found is not None else ""
            if (
                found is not None
                and bid not in done_ids
                and not any(
                    e.get("kind") == "birthday" and e.get("at") == found["at"]
                    for e in events
                )
            ):
                events.append(
                    {
                        "id": bid,
                        "kind": "birthday",
                        "at": found["at"],
                        "quote": found["quote"],
                        "source_msg_id": 0,
                        "triggers": triggers_for("birthday", found["at"]),
                        "asked": 0,
                        "last_asked_at": 0,
                        "followed_up": False,
                        "done": False,
                    }
                )
        state["events"], state["events_done"] = self._prune_events(
            cfg, events, now, state.get("events_done")
        )

    @staticmethod
    def _prune_events(
        cfg: NudgeConfig,
        events: list[dict],
        now: float,
        done_ids: list[str] | None = None,
    ) -> tuple[list[dict], list[str]]:
        """丢完成/过期两年的，按时间升序截到上限。

        完成的事件要把 id 记进 ``events_done``：否则下一轮扫描会从**同一条用户
        消息**里再抽一次，"已经问过了"永远不生效（线上会变成每天都问）。
        """
        done = list(done_ids or [])[-50:]
        kept = []
        for ev in events:
            eid = str(ev.get("id") or "")
            at = float(ev.get("at") or 0)
            if ev.get("done") or (at and at < now - 2 * 86400):
                # 完成或过期：记下 id，避免下一轮扫描从同一条消息再抽一次
                # （事情过去两天以上还问，就是翻旧账）
                if eid and eid not in done:
                    done.append(eid)
                continue
            kept.append(ev)
        kept.sort(key=lambda e: float(e.get("at") or 0))
        return kept[: cfg.event_max_pending], done[-50:]

    def _take_event(
        self, cfg: NudgeConfig, state: dict, now: float, last_user: float | None
    ) -> tuple[dict | None, bool]:
        """取一个"该问了"的事件；返回 (事件, 是否追问)。

        优先级：到触发时刻的 > 该追问的。你已经在事件之后开过口 → 直接标记完成
        （不用再追问）。
        """
        due: tuple[float, dict] | None = None
        follow: tuple[float, dict] | None = None
        for ev in state.get("events") or []:
            if ev.get("done"):
                continue
            triggers = [float(t) for t in (ev.get("triggers") or []) if t]
            asked_at = float(ev.get("last_asked_at") or 0)
            if asked_at and last_user is not None and last_user > asked_at:
                ev["done"] = True  # 你回过了
                continue
            if triggers and triggers[0] <= now:
                if due is None or triggers[0] < due[0]:
                    due = (triggers[0], ev)
            elif (
                asked_at
                and not ev.get("followed_up")
                and cfg.event_followup_hours > 0
                and now - asked_at >= cfg.event_followup_hours * 3600
            ):
                if follow is None or asked_at < follow[0]:
                    follow = (asked_at, ev)
        if due is not None:
            return due[1], False
        if follow is not None:
            return follow[1], True
        return None, False

    def _consume_event(self, ev: dict, followup: bool, now: float) -> None:
        """问过了：弹掉用掉的触发点；追问只算一次，之后完成。

        **触发点用尽不等于结案**：否则所有单触发点事件（会议/出差/体检/截止…）
        问完就 done，``event_followup_hours`` 那条"次日再问一次"永远走不到
        （``_take_event`` 跳过 done）。留到追问一次、或 ``at`` 过后 2 天由
        ``_prune_events`` 清理。
        """
        if followup:
            ev["followed_up"] = True
            ev["done"] = True
            return
        triggers = [float(t) for t in (ev.get("triggers") or []) if t]
        ev["triggers"] = triggers[1:]
        ev["asked"] = int(ev.get("asked") or 0) + 1
        ev["last_asked_at"] = now

    def event_status(self, state: dict) -> list[dict]:
        """待问事件（``/nudge`` 展示用）。"""
        out = []
        for ev in state.get("events") or []:
            if ev.get("done"):
                continue
            triggers = [float(t) for t in (ev.get("triggers") or []) if t]
            out.append(
                {
                    "kind": ev.get("kind") or "",
                    "at": float(ev.get("at") or 0),
                    "quote": str(ev.get("quote") or "")[:30],
                    "next_trigger": triggers[0] if triggers else 0,
                    "asked": int(ev.get("asked") or 0),
                    "followed_up": bool(ev.get("followed_up")),
                }
            )
        return out

    # ---------- 用户消息 ----------

    async def note_user_message(
        self, user_id: str, text: str = "", ts: float | None = None
    ) -> None:
        """用户开口了：重置未回复计数，必要时按"在忙"暂停，并把倒计时拨回原点。

        在插件层的 handler 里调用，必须**不阻塞回复**——内部全部 try 兜底。
        停用的行（配错人 / 连续投递失败）也会在这里恢复：你开口是唯一能证明
        "这个目标还活着"的信号，否则"等你先开口"就成了永久关闭。
        """
        now = time.time() if ts is None else float(ts)
        cfg = self._cfg()
        if not cfg.enabled:
            return
        target = f"private:{user_id}"
        rows = await self._nudge_rows(include_disabled=True)
        row = self._row_for(rows, target)
        if row is None:
            return
        state = dict(row.get("params") or {})
        state["unanswered"] = 0
        state["paused_until_user"] = False
        # 与 tick 的 reset 分支共用 last_user_seen 去重，避免同一条消息被两处
        # 各自重排一次定时器（两次随机窗口无意义地叠加）
        if now > float(state.get("last_user_seen") or 0):
            state["last_user_seen"] = now
        if is_busy_reply(text) and cfg.busy_pause_seconds > 0:
            state["pause_until"] = now + cfg.busy_pause_seconds
            logger.info(
                "nudge: %s 说了「%s」，暂停 %d 小时",
                target,
                _CONTROL_CHAR_RE.sub("", text)[:20],
                cfg.busy_pause_seconds // 3600,
            )
        nxt = earliest_allowed(cfg, now + self._first_delay(cfg), last_user=now)
        try:
            await self._save(str(row["id"]), state, nxt)
            if not row.get("enabled"):
                if target in cfg.targets:
                    # 停用行（配错人 / 连续投递失败）在你开口后应恢复；
                    # 配置里已删除的目标不复活，否则会给不在 targets 里的人发消息
                    await self.store.schedule_set_enabled(str(row["id"]), True)
                    logger.info("nudge: %s 因用户开口恢复调度行", target)
                else:
                    logger.info("nudge: %s 已不在配置 targets 里，保持停用", target)
        except Exception:
            logger.warning("nudge: note_user_message 落库失败", exc_info=True)

    # ---------- 主循环 ----------

    async def tick(self, now: float | None = None) -> dict:
        """调度入口：同一进程内串行（定时 tick 与手动试发不会同时跑）。"""
        async with self._lock_obj():
            return await self._tick(now)

    async def _tick(self, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        out = {
            "targets": 0,
            "sent": 0,
            "skipped": 0,
            "failed": 0,
            "disabled": 0,
        }
        cfg = self._cfg()
        if not cfg.enabled:
            return out
        # 热点轮询与目标无关：关掉主动联系时也不查（省搜索配额）
        try:
            self._prune_pending(cfg, now)
            await self._maybe_poll(cfg, now)
        except Exception:
            logger.warning("nudge: 热点轮询失败（不影响主动联系本身）", exc_info=True)
        try:
            await self.register_targets()
        except Exception:
            logger.warning("nudge: 目标同步失败，本轮跳过", exc_info=True)
            return out
        rows = await self._nudge_rows()
        out["targets"] = len(rows)
        for row in rows:
            try:
                if cfg.memory_events:
                    await self._scan_events_for(row, cfg, now)
                outcome = await self._process(row, now, cfg)
            except Exception:
                logger.exception("nudge: process failed for %s", row.get("target"))
                outcome = "failed"
            if outcome == "sent":
                out["sent"] += 1
            elif outcome == "failed":
                out["failed"] += 1
            elif outcome in ("disabled", "no_session", "bad_target"):
                # 后两个是"目标本身有问题"：与投递失败一样需要人介入，计入停用
                out["disabled"] += 1
            else:
                out["skipped"] += 1
        if out["sent"] or out["failed"]:
            logger.info(
                "nudge: targets=%d sent=%d skipped=%d failed=%d disabled=%d",
                out["targets"],
                out["sent"],
                out["skipped"],
                out["failed"],
                out["disabled"],
            )
        return out

    async def _process(
        self, row: dict, now: float, cfg: NudgeConfig, *, force: bool = False
    ) -> str:
        target = str(row.get("target") or "")
        sid = str(row["id"])
        state = dict(row.get("params") or {})
        parsed = parse_target(target)
        if parsed is None:
            await self._disable(sid, target, f"目标不合法：{target!r}", state=state)
            return "bad_target"
        _, ident = parsed
        user_id = str(ident)
        session_id = await self.store.resolve_session(user_id, None)
        last_user = await self.store.last_user_message_ts(session_id)

        # 1) 你发了新消息 → 计数清零，倒计时从这条消息重新起算。
        #    用 last_user_seen 去重：否则每个 tick 都会遇到"最后一条消息比上次
        #    发送时间晚"，把已经排好的下一次无限往后推，第一条永远发不出去。
        seen = float(state.get("last_user_seen") or 0)
        last_sent = float(state.get("last_sent_at") or 0)
        # force（/nudge run）= 人工试发：不吃"你刚说过话 → 重新排期"这条分支，
        # 否则手动触发在用户刚开口时只会返回 reset、什么都不发
        if last_user is not None and last_user > seen and not force:
            state["last_user_seen"] = last_user
            state["unanswered"] = 0
            state["paused_until_user"] = False
            if last_user > last_sent:
                state["last_reply_at"] = last_user
            nxt = earliest_allowed(
                cfg, last_user + self._first_delay(cfg), last_user=last_user
            )
            await self._save(sid, state, nxt)
            return "reset"

        if state.get("paused_until_user"):
            state["last_skip_reason"] = "paused_until_user"
            await self._save(sid, state, None)
            return "paused_user"
        pause_until = float(state.get("pause_until") or 0)
        if pause_until and now < pause_until:
            state["last_skip_reason"] = "busy_pause"
            await self._save(sid, state, None)
            return "paused"

        unanswered = int(state.get("unanswered") or 0)
        if unanswered >= cfg.max_unanswered:
            state["last_skip_reason"] = f"unanswered_{unanswered}"
            await self._save(sid, state, None)
            return "stopped"

        # 2) 时间守卫（静默 / 晚睡 / 你还在聊）
        reason = self._time_guard(cfg, now, last_user)
        if reason and not force:
            state["last_skip_reason"] = reason
            due = row.get("next_run")
            nxt = (
                due
                if (due is not None and due > now)
                else earliest_allowed(cfg, now + 60, last_user)
            )
            await self._save(sid, state, nxt)
            return reason

        # 3) 事件抢占（优先级最高）：到点就来问，同样不受"6–24h 第一次"约束，
        #    但静默/晚睡/活跃保护照旧。时间守卫不过就不消费，下一个 tick 重试。
        events_before = json.dumps(
            state.get("events"), sort_keys=True, ensure_ascii=False
        )
        event, followup = (
            self._take_event(cfg, state, now, last_user)
            if cfg.memory_events
            else (None, False)
        )
        events_dirty = (
            json.dumps(state.get("events"), sort_keys=True, ensure_ascii=False)
            != events_before
        )
        if event is not None:
            kind, cand = "event", event
            if followup:
                instruction = EVENT_FOLLOWUP_TASK
            elif now >= float(event.get("at") or 0):
                # 触发点落在事件之后（自定义提前量 / 错过补跑）：问结果，
                # 而不是问"准备得怎么样"
                instruction = EVENT_RESULT_TASK
            else:
                instruction = EVENT_TASK
        else:
            # 4) 爆点抢占：与事件同规则（凌晨的爆点排到早上 10 点）
            breaking = self._take_breaking(cfg, now) if cfg.hot_topics else None
            if breaking is not None:
                kind, cand, instruction = "breaking", breaking, BREAKING_TASK
            else:
                kind, cand, instruction = "normal", None, None

        # 4) 计划时刻（None = 还没排过：新目标或已被 stop 清空）
        due = row.get("next_run")
        if due is None:
            if last_user is None:
                await self._disable(
                    sid,
                    target,
                    "目标没有会话记录（配错人了？；对方先私聊一次会自动恢复）",
                    state=state,
                )
                return "no_session"
            nxt = earliest_allowed(
                cfg, now + self._first_delay(cfg), last_user=last_user
            )
            await self._save(sid, state, nxt)
            return "scheduled"
        if due > now and not force and kind not in ("breaking", "event"):
            # _take_event 可能刚把"你已经回过了"的事件标记成完成——不落盘的话
            # 下一轮又会问一次（实测）
            if events_dirty:
                await self._save(sid, state, due)
            return "scheduled"

        # 5) 第三条固定是告别：没到第 14 天就只等着（爆点/事件不占用告别名额）
        if kind not in ("breaking", "event") and unanswered == cfg.max_unanswered - 1:
            goodbye_at = self._goodbye_at(cfg, last_user if last_user else last_sent)
            if now < goodbye_at and not force:
                await self._save(
                    sid, state, earliest_allowed(cfg, goodbye_at, last_user)
                )
                return "waiting_goodbye"
            kind = "goodbye"
        elif kind == "normal":
            # 普通搭话：优先拿一个热点候选当引子（没有就纯搭话）
            cand = self._take_candidate(cfg, now)
            if cand is not None:
                instruction = HOT_TASK

        # 5) 当日配额（daily_cap_normal=0 表示不限，与 push 的封顶口径一致）
        #    普通 1 条 + 爆点 1 条，总上限 3；爆点评分 ≥9 可把上限破例到 4
        counts = self._counts(state, now)
        total = sum(counts.values())
        score = (
            int(cand.get("score") or 0)
            if kind == "breaking" and cand is not None
            else 0
        )
        override = kind == "breaking" and score >= cfg.hot_override_score
        # 事件与爆点同属"有由头"：当天各只算一次，不占普通名额
        limit = (
            max(cfg.max_daily_total, cfg.breaking_override_cap)
            if override
            else cfg.max_daily_total
        )
        if (
            kind == "normal"
            and cfg.daily_cap_normal > 0
            and counts.get("normal", 0) >= cfg.daily_cap_normal
        ):
            state["last_skip_reason"] = "daily_cap"
            await self._save(sid, state, self._next_midnight(now))
            return "capped"
        if kind in ("breaking", "event") and counts.get(kind, 0) >= 1 and not override:
            # 当天已经为这个由头说过一次：等到明天
            state["last_skip_reason"] = f"{kind}_cap"
            await self._save(sid, state, self._next_midnight(now))
            return "capped"
        if total >= limit:
            state["last_skip_reason"] = "daily_cap_total"
            await self._save(sid, state, self._next_midnight(now))
            return "capped"

        # 6) 预算硬闸门：跳过并顺延到次日，不停用、不告警（与 push 同口径）
        blocked, reason = get_budget().chat_blocked()
        if blocked:
            state["last_skip_reason"] = "budget"
            await self._save(sid, state, self._next_midnight(now))
            return "budget"

        # 7) 生成（热点/爆点/事件的外部内容必须过围栏，且只允许转述）
        if kind == "event":
            extra = fence_untrusted(
                "他自己的原话",
                str(cand.get("quote") or "") if cand is not None else "",
                "用户此前的发言",
            )
        else:
            extra = candidate_block(cand) if cand else ""
        text = await self._generate(
            cfg, state, kind, user_id, extra_data=extra, instruction=instruction
        )

        # 7.5) 生成期间你开口了 → 这次生成作废（不发、不写历史、不计数）。
        #      守卫是在生成**之前**判的，而 LLM 调用可能几十秒；不复查就会在
        #      你刚说完话时收到一条主动消息，并且覆盖掉 note_user_message 刚
        #      写好的重置（丢更新）。
        if not force and await self._user_spoke_since(session_id, last_user):
            fresh = self._row_for(await self._nudge_rows(include_disabled=True), target)
            state = dict((fresh or row).get("params") or {})
            fresh_user = await self.store.last_user_message_ts(session_id)
            base = fresh_user if fresh_user is not None else (last_user or now)
            state["last_user_seen"] = base
            state["unanswered"] = 0
            state["paused_until_user"] = False
            state["last_skip_reason"] = "active"
            nxt = earliest_allowed(cfg, base + self._first_delay(cfg), last_user=base)
            await self._save(sid, state, nxt)
            logger.info("nudge: %s 生成期间用户开口，丢弃本次生成（不发送）", target)
            return "active"

        if not text:
            state["last_skip_reason"] = "empty_generation"
            await self._save(sid, state, self._retry_at(cfg, now, hours=1))
            return "empty"

        # 8) 投递
        outcome = await self.sink.send_once(target, text)
        if outcome == "uncertain":
            # 结果不确定：不写历史（可能没送达）、不重发、不计未回复，
            # 但消耗当日配额并记住这句话，避免下次原样再发一遍
            logger.warning("nudge: %s delivery uncertain, not resending", target)
            state["last_uncertain_at"] = now
            state["recent_texts"] = self._push_recent(state, text)
            counts[kind] = counts.get(kind, 0) + 1
            state["counts"] = {"day": tz.day_key(now), **counts}
            state["last_skip_reason"] = "uncertain"
            await self._save(
                sid,
                state,
                earliest_allowed(
                    cfg, now + self._first_delay(cfg), last_user=last_user
                ),
            )
            _diag_record("nudge_uncertain", target=target)
            return "uncertain"
        if outcome == "failed":
            return await self._handle_failure(sid, state, target, cfg, now)

        # 9) 已送达：写回历史 + 记账
        try:
            await self.store.append_message(session_id, "assistant", text)
        except Exception:
            # 历史写回失败不影响"已经发出去"这个事实，但必须留痕：
            # 否则下次对话接不上，而日志里看不出原因
            logger.exception("nudge: 历史写回失败（%s）", target)
        if kind == "event" and cand is not None:
            self._consume_event(cand, followup, now)
        elif cand is not None:
            self._consume(cand, now)
        state["last_sent_at"] = now
        state["last_kind"] = kind
        state["last_text"] = text
        state["failures"] = 0
        state["recent_texts"] = self._push_recent(state, text)
        counts[kind] = counts.get(kind, 0) + 1
        state["counts"] = {"day": tz.day_key(now), **counts}
        state["last_skip_reason"] = ""
        if kind in ("normal", "goodbye"):
            # 只有普通搭话/告别计入"连续未回复"。爆点/事件是"有由头"的一次性
            # 消息：让它们占名额，2 条新闻就会换来一句告别、3 条就直接静默
            # （连告别都没有），与 AC 的"第 3 条固定是第 14 天告别"冲突。
            state["unanswered"] = unanswered + 1
        if kind == "goodbye":
            state["unanswered"] = cfg.max_unanswered
            state["paused_until_user"] = True
        if kind in ("normal", "goodbye"):
            nxt = self._next_after(cfg, state, now, last_user)
        else:
            # 爆点/事件不改变普通搭话的节奏：保留原来排好的下一次
            nxt = due
            if nxt is None:
                nxt = earliest_allowed(
                    cfg, now + self._first_delay(cfg), last_user=last_user
                )
        await self._save(sid, state, nxt)
        logger.info(
            "[nudge] %s | kind=%s | unanswered=%s | text=%s",
            target,
            kind,
            state["unanswered"],
            text[:200],
        )
        _diag_record("nudge_sent", target=target, mode=kind)
        return "sent"

    # ---------- 内部 ----------

    async def _user_spoke_since(self, session_id: str, last_user: float | None) -> bool:
        """生成期间是否又收到用户消息（复查失败按"没说话"处理，宁可发也不误杀）。"""
        try:
            current = await self.store.last_user_message_ts(session_id)
        except Exception:
            logger.warning("nudge: 复查用户活跃失败", exc_info=True)
            return False
        if current is None:
            return False
        return last_user is None or float(current) > float(last_user)

    def _time_guard(self, cfg: NudgeConfig, now: float, last_user: float | None) -> str:
        if in_quiet(cfg, now):
            return "quiet"
        if (
            last_user is not None
            and after_late_night(cfg, last_user)
            and now < last_user + cfg.late_night_extra_seconds
        ):
            return "late_night"
        if (
            last_user is not None
            and cfg.min_idle_seconds > 0
            and now - last_user < cfg.min_idle_seconds
        ):
            return "active"
        return ""

    def _first_delay(self, cfg: NudgeConfig) -> float:
        return self._rng.uniform(cfg.first_min_seconds, cfg.first_max_seconds)

    def _goodbye_at(self, cfg: NudgeConfig, base: float) -> float:
        return float(base) + cfg.goodbye_after_days * 86400

    def _next_after(
        self, cfg: NudgeConfig, state: dict, now: float, last_user: float | None
    ) -> float | None:
        """发完一条后，排下一条的位置。None = 不再主动（等你开口）。"""
        n = int(state.get("unanswered") or 0)
        if n <= 1:
            # 第一条已发 → 第二条在 +48h
            return earliest_allowed(
                cfg, now + cfg.second_delay_seconds, last_user=last_user
            )
        if n == 2:
            # 第二条已发 → 第三条是第 14 天的告别
            return earliest_allowed(
                cfg,
                self._goodbye_at(cfg, last_user if last_user is not None else now),
                last_user=last_user,
            )
        return None

    def _retry_at(self, cfg: NudgeConfig, now: float, *, hours: int) -> float:
        return earliest_allowed(cfg, now + hours * 3600, last_user=None)

    @staticmethod
    def _next_midnight(now: float) -> float:
        """次日 00:05——日配额/日预算的顺延目标。

        配满是**按天**的条件，逐小时重试只是白跑 DB；预算闸门同样按日恢复。
        """
        local = _local(now) + dt.timedelta(days=1)
        return local.replace(hour=0, minute=5, second=0, microsecond=0).timestamp()

    @staticmethod
    def _counts(state: dict, now: float) -> dict:
        day = tz.day_key(now)
        raw = state.get("counts") or {}
        if not isinstance(raw, dict) or raw.get("day") != day:
            return {}
        return {
            k: int(v)
            for k, v in raw.items()
            if k != "day" and isinstance(v, int) and v > 0
        }

    @staticmethod
    def _push_recent(state: dict, text: str) -> list[str]:
        recent = [t for t in (state.get("recent_texts") or []) if t]
        recent.insert(0, text)
        return recent[:_MAX_RECENT_TEXTS]

    async def _generate(
        self,
        cfg: NudgeConfig,
        state: dict,
        kind: str,
        user_id: str,
        *,
        extra_data: str = "",
        instruction: str | None = None,
    ) -> str:
        nickname = cfg.nickname
        if not nickname:
            try:
                # 刻意**不按 session 过滤**：称呼是"你这个人"的属性，而它很可能
                # 是在某个群里说的一句「叫我夏祭」——按私聊会话过滤会一条都取不到。
                # 这里只读第一人称句式提取一个称呼，不外泄其它会话的事实
                # （进 prompt 的 facts 仍由 engine._recall_facts 按会话作用域取）。
                facts = await self.store.list_facts(user_id, limit=200)
            except Exception:
                logger.warning("nudge: facts 读取失败，用默认称呼", exc_info=True)
                facts = []
            try:
                nickname = nickname_from_facts(facts) or DEFAULT_NICKNAME
            except Exception:
                # 称呼解析绝不能让整条主动消息失败：退化成默认值照发
                logger.warning("nudge: 称呼解析异常，用默认称呼", exc_info=True)
                nickname = DEFAULT_NICKNAME
        context = {
            "user_id": user_id,
            "group_id": None,
            "platform": "qq",
            "nickname": nickname,
        }
        task = instruction or (_GOODBYE_TASK if kind == "goodbye" else _NORMAL_TASK)
        with route_context("nudge"):
            text = await self.engine.generate_proactive(
                context,
                instruction=task,
                extra_data=extra_data,
                recent_texts=state.get("recent_texts") or [],
                max_tokens=cfg.max_chars,
            )
        return clip_at_sentence(text, cfg.max_chars)

    async def _handle_failure(
        self,
        sid: str,
        state: dict,
        target: str,
        cfg: NudgeConfig,
        now: float,
    ) -> str:
        fails = int(state.get("failures") or 0) + 1
        state["failures"] = fails
        if fails >= cfg.max_failures:
            await self._disable(sid, target, f"连续 {fails} 次投递失败", state=state)
            return "disabled"
        delay = min(
            cfg.retry_delay_seconds * (2 ** (fails - 1)), cfg.max_retry_delay_seconds
        )
        state["last_skip_reason"] = "delivery_failed"
        await self._save(sid, state, earliest_allowed(cfg, now + delay, last_user=None))
        logger.warning(
            "nudge: %s delivery failed (%d/%d), retry in %ss",
            target,
            fails,
            cfg.max_failures,
            delay,
        )
        _diag_record("nudge_failed", target=target, fails=fails)
        return "failed"

    async def _disable(
        self, sid: str, target: str, reason: str, *, state: dict | None = None
    ) -> None:
        """停用调度行并把原因写进 params（``/nudge why`` 与 Web 才查得到）。

        停用后仍可在两种情况下恢复：目标本人发来私聊（``note_user_message``
        会把行重新启用），或人工改库。配置里删掉的目标不会被复活。
        """
        logger.error("nudge: target %s disabled: %s", target, reason)
        _diag_record("nudge_disabled", target=target, reason=reason[:40])
        if state is not None:
            state["disabled_reason"] = reason[:120]
            state["last_skip_reason"] = "disabled"
            try:
                await self.store.schedule_set_params(sid, state)
            except Exception:
                logger.warning("nudge: 停用原因落库失败", exc_info=True)
        await self.store.schedule_mark_fired(sid, None)
        await self._alert(f"⚠️ 主动联系已停用：{target}\n原因：{reason}")

    async def _alert(self, text: str) -> None:
        if self.on_alert is None:
            return
        try:
            await self.on_alert(text)
        except Exception:
            logger.warning("nudge: alert notify failed", exc_info=True)

    async def _save(self, sid: str, state: dict, next_run: float | None) -> None:
        if len(state) > _STATE_KEY_GUARD:
            logger.warning(
                "nudge: 状态键过多（%d），可能有地方在往 params 里堆数据", len(state)
            )
        await self.store.schedule_set_params(sid, state)
        # next_run=None 表示"这一轮不排下一次"：**不能**走 schedule_mark_fired(None)，
        # 那是停用语义——行一被停用，note_user_message 就再也找不到它，
        # 用户开口后无法恢复（暂停/止损都会变成永久关闭）。
        if next_run is not None:
            await self.store.schedule_mark_fired(sid, next_run)

    # ---------- 管理面 ----------

    async def status(self) -> list[dict]:
        """每个目标的状态（``/nudge`` 与 Web 只读页共用）。"""
        rows = await self._nudge_rows(include_disabled=True)
        out = []
        for row in rows:
            params = row.get("params") or {}
            counts = params.get("counts") or {}
            out.append(
                {
                    "target": str(row.get("target") or ""),
                    "enabled": bool(row.get("enabled")),
                    "next_run": row.get("next_run"),
                    "last_kind": params.get("last_kind") or "",
                    "last_text": params.get("last_text") or "",
                    "last_sent_at": params.get("last_sent_at") or 0,
                    "unanswered": int(params.get("unanswered") or 0),
                    "pause_until": params.get("pause_until") or 0,
                    "paused_until_user": bool(params.get("paused_until_user")),
                    "last_skip_reason": params.get("last_skip_reason") or "",
                    "disabled_reason": params.get("disabled_reason") or "",
                    "counts": counts if isinstance(counts, dict) else {},
                    "failures": int(params.get("failures") or 0),
                }
            )
        out.sort(key=lambda r: r["target"])
        return out

    async def pause(self, hours: float) -> int:
        """暂停所有目标 hours 小时；返回作用的目标数。"""
        until = time.time() + max(0.0, hours) * 3600
        rows = await self._nudge_rows()
        for row in rows:
            state = dict(row.get("params") or {})
            state["pause_until"] = until
            state["last_skip_reason"] = "manual_pause"
            await self._save(str(row["id"]), state, row.get("next_run"))
        return len(rows)

    async def run_now(self, kind: str = "normal") -> str:
        """立即触发一次（``/nudge run``，调试用）；返回结果串。

        与 tick 共用同一把锁：手动试发若和定时任务同时读同一行 state，会各发
        一条（调度器的 ``max_instances=1`` 只管定时任务本身）。
        """
        async with self._lock_obj():
            return await self._run_now(kind)

    async def _run_now(self, kind: str = "normal") -> str:
        cfg = self._cfg()
        if not cfg.targets:
            return "没有配置目标（data/nudge.json 的 targets）"
        target = cfg.targets[0]
        rows = await self._nudge_rows(include_disabled=True)
        row = self._row_for(rows, target)
        if row is None:
            await self.register_targets()
            rows = await self._nudge_rows(include_disabled=True)
            row = self._row_for(rows, target)
        if row is None:
            return f"目标 {target} 登记失败"
        state = dict(row.get("params") or {})
        # 手动触发要绕过"你刚发过消息"的 reset 分支，把已见水位推到当下
        state["last_user_seen"] = time.time()
        if kind == "goodbye":
            state["unanswered"] = max(cfg.max_unanswered - 1, 0)
            await self._save(str(row["id"]), state, time.time())
        else:
            state["unanswered"] = 0
            state["paused_until_user"] = False
            state["pause_until"] = 0
            await self._save(str(row["id"]), state, time.time())
        rows = await self._nudge_rows(include_disabled=True)
        row = self._row_for(rows, target) or row
        outcome = await self._process(row, time.time(), cfg, force=True)
        if outcome == "sent":
            # 试发要能当"测试证明"用：把这次发送造成的后果一并回执
            # （字数 / 未回复计数 / 下次最早时间），否则只回"已发送"毫无信息量
            cur = (
                self._row_for(await self._nudge_rows(include_disabled=True), target)
                or {}
            )
            params = cur.get("params") or {}
            nxt = cur.get("next_run")
            if not cur.get("enabled"):
                # 停用行也能手动试发，但不能给出"下次还会来"的假承诺
                when = "目标已停用，本次仅手动试发；对方发消息后自动恢复"
            elif params.get("paused_until_user"):
                # 告别之后是"等你先开口"，给个时间点会让人以为还会再来
                when = "已暂停，等你先开口"
            else:
                when = f"下次最早 {tz.fmt_ts(nxt)}" if nxt else "等你开口"
            return (
                f"已发送（{len(str(params.get('last_text') or ''))} 字，"
                f"未回复 {int(params.get('unanswered') or 0)}/{cfg.max_unanswered}，"
                f"{when}）"
            )
        labels = {
            "sent": "已发送",
            "failed": "投递失败（已顺延重试）",
            "uncertain": "结果不确定，未重发",
            "disabled": "已停用（见日志）",
            "empty": "生成内容为空，本次跳过",
            "budget": "预算硬闸门命中，本次跳过",
            "capped": "已达当日上限，本次跳过",
            "quiet": "当前在静默时段",
            "late_night": "晚睡保护中",
            "active": "你刚还在聊，不打扰",
            "paused": "暂停中",
            "paused_user": "已暂停，等你先开口",
            "stopped": "连续未回复已达上限，已停止",
            "no_session": "目标没有会话记录",
            "bad_target": "目标不合法",
        }
        return labels.get(outcome, f"结果：{outcome}")

    async def events_all(self) -> list[dict]:
        """所有目标的待问事件（Web 只读页用）。

        刻意**不带 ``quote``**：那是用户原话。Web 响应体的约定是"不含消息正文"
        （唯一例外是日志查看面），而这里只是状态列表；正文留在 QQ 命令侧与
        生成 prompt 里用。
        """
        out = []
        for row in await self._nudge_rows(include_disabled=True):
            for ev in self.event_status(row.get("params") or {}):
                item = {k: v for k, v in ev.items() if k != "quote"}
                item["target"] = str(row.get("target") or "")
                out.append(item)
        return out
