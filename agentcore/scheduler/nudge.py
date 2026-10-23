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
3. **写回只认"已送达"**：只有 ``sent`` 才 ``append_message(assistant)``，
   且 bot 自己的话不进入事实抽取、不推进摘要水位。

状态存放：复用 ``schedules`` 表（``action='nudge'``，``user_id=__nudge__``），
**无 DDL 变更**，因此备份/恢复自动覆盖；每次 tick 后用
``store.schedule_set_params`` 整体替换 params 里的状态字典。
配置放在 ``data/nudge.json``（0600、原子写、每 tick 按 mtime 热读），
与 llm-providers / search-providers 同模式——因为 Web 要能改它。
"""

from __future__ import annotations

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
from agentcore.scheduler.push import parse_target

logger = logging.getLogger(__name__)

OWNER = "__nudge__"
ACTION_NUDGE = "nudge"

# 真人 QQ 号是 ASCII 数字，不会与这个系统用户名撞名
DEFAULT_CONFIG_FILE = "data/nudge.json"
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
    # 热点/事件在 P2/P3 落地，schema 先占位（缺失按关闭处理）
    hot_topics: bool = False
    memory_events: bool = False

    def quiet_window(self) -> tuple[int, int]:
        return _minutes(self.quiet_start), _minutes(self.quiet_end)


_KNOWN_KEYS = set(NudgeConfig().__dataclass_fields__)


def _coerce(raw: dict) -> NudgeConfig:
    """把任意 dict 收敛成 NudgeConfig：未知键告警、越界钳制、目标去重校验。"""
    unknown = sorted(set(raw) - _KNOWN_KEYS)
    if unknown:
        logger.warning("nudge: 配置含未知键 %s（已忽略，可能是拼写错误）", unknown)
    cfg = NudgeConfig()
    cfg.enabled = bool(raw.get("enabled", cfg.enabled))
    cfg.quiet_start = _parse_hhmm(raw.get("quiet_start"), cfg.quiet_start)
    cfg.quiet_end = _parse_hhmm(raw.get("quiet_end"), cfg.quiet_end)
    cfg.late_night_after = _parse_hhmm(
        raw.get("late_night_after"), cfg.late_night_after
    )
    cfg.late_night_extra_seconds = _clamp_int(
        raw.get("late_night_extra_seconds"),
        cfg.late_night_extra_seconds,
        minimum=0,
        maximum=24 * 3600,
        name="late_night_extra_seconds",
    )
    cfg.min_idle_seconds = _clamp_int(
        raw.get("min_idle_seconds"),
        cfg.min_idle_seconds,
        minimum=0,
        maximum=24 * 3600,
        name="min_idle_seconds",
    )
    cfg.first_min_seconds = _clamp_int(
        raw.get("first_min_seconds"),
        cfg.first_min_seconds,
        minimum=60,
        maximum=30 * 24 * 3600,
        name="first_min_seconds",
    )
    cfg.first_max_seconds = _clamp_int(
        raw.get("first_max_seconds"),
        cfg.first_max_seconds,
        minimum=60,
        maximum=30 * 24 * 3600,
        name="first_max_seconds",
    )
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
    cfg.second_delay_seconds = _clamp_int(
        raw.get("second_delay_seconds"),
        cfg.second_delay_seconds,
        minimum=3600,
        maximum=30 * 24 * 3600,
        name="second_delay_seconds",
    )
    cfg.goodbye_after_days = _clamp_int(
        raw.get("goodbye_after_days"),
        cfg.goodbye_after_days,
        minimum=1,
        maximum=365,
        name="goodbye_after_days",
    )
    cfg.max_unanswered = _clamp_int(
        raw.get("max_unanswered"),
        cfg.max_unanswered,
        minimum=1,
        maximum=10,
        name="max_unanswered",
    )
    cfg.busy_pause_seconds = _clamp_int(
        raw.get("busy_pause_seconds"),
        cfg.busy_pause_seconds,
        minimum=0,
        maximum=30 * 24 * 3600,
        name="busy_pause_seconds",
    )
    cfg.daily_cap_normal = _clamp_int(
        raw.get("daily_cap_normal"),
        cfg.daily_cap_normal,
        minimum=0,
        maximum=50,
        name="daily_cap_normal",
    )
    cfg.max_daily_total = _clamp_int(
        raw.get("max_daily_total"),
        cfg.max_daily_total,
        minimum=1,
        maximum=50,
        name="max_daily_total",
    )
    cfg.max_chars = _clamp_int(
        raw.get("max_chars"), cfg.max_chars, minimum=10, maximum=500, name="max_chars"
    )
    cfg.tick_seconds = _clamp_int(
        raw.get("tick_seconds"),
        cfg.tick_seconds,
        minimum=30,
        maximum=24 * 3600,
        name="tick_seconds",
    )
    cfg.retry_delay_seconds = _clamp_int(
        raw.get("retry_delay_seconds"),
        cfg.retry_delay_seconds,
        minimum=30,
        maximum=86400,
        name="retry_delay_seconds",
    )
    cfg.max_retry_delay_seconds = _clamp_int(
        raw.get("max_retry_delay_seconds"),
        cfg.max_retry_delay_seconds,
        minimum=60,
        maximum=7 * 86400,
        name="max_retry_delay_seconds",
    )
    cfg.max_failures = _clamp_int(
        raw.get("max_failures"),
        cfg.max_failures,
        minimum=1,
        maximum=50,
        name="max_failures",
    )
    cfg.hot_topics = bool(raw.get("hot_topics", cfg.hot_topics))
    cfg.memory_events = bool(raw.get("memory_events", cfg.memory_events))
    nickname = str(raw.get("nickname") or "").strip()
    cfg.nickname = _CONTROL_CHAR_RE.sub("", nickname)[:12]

    targets: list[str] = []
    seen: set[str] = set()
    for item in raw.get("targets") or []:
        target = str(item or "").strip()
        if parse_target(target) is None:
            logger.warning("nudge: 目标 %r 非法（应为 private:<QQ号>），已跳过", target)
            continue
        kind, ident = parse_target(target)
        if kind != "private":
            # v1 只做私聊：群主动发言是另一套 ACL 与体验问题，先不做
            logger.warning("nudge: 暂不支持群目标 %r，已跳过", target)
            continue
        if target in seen:
            continue
        seen.add(target)
        targets.append(target)
    cfg.targets = targets
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
        "memory_events": cfg.memory_events,
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
    """该时刻是否落在静默时段内（支持 23:30→10:00 这类跨零点窗口）。"""
    cur = _local(ts).hour * 60 + _local(ts).minute
    start, end = cfg.quiet_window()
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
        config: NudgeConfig | None = None,
        config_path: str | Path | None = None,
        on_alert=None,
        rng: random.Random | None = None,
    ) -> None:
        self.store = store
        self.sink = sink
        self.engine = engine
        self.on_alert = on_alert
        self._rng = rng or random.Random()
        self._config_path = Path(
            config_path
            or os.getenv("AGENT_NUDGE_CONFIG", "").strip()
            or DEFAULT_CONFIG_FILE
        )
        self._config: NudgeConfig = config or default_config()
        self._cfg_mtime: float | None = None
        self._lock = None  # asyncio.Lock 懒加载（构造可能在无事件循环处发生）

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
        self._config = load_nudge_config(self._config_path)
        self._cfg_mtime = mtime
        return self._config

    def reload_config(self) -> NudgeConfig:
        """强制重读（Web 写完后调用，避免 mtime 精度问题）。"""
        self._cfg_mtime = None
        return self._cfg()

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

    # ---------- 用户消息 ----------

    async def note_user_message(
        self, user_id: str, text: str = "", ts: float | None = None
    ) -> None:
        """用户开口了：重置未回复计数，必要时按"在忙"暂停，并把倒计时拨回原点。

        在插件层的 handler 里调用，必须**不阻塞回复**——内部全部 try 兜底。
        """
        now = time.time() if ts is None else float(ts)
        cfg = self._cfg()
        if not cfg.enabled:
            return
        target = f"private:{user_id}"
        rows = await self._nudge_rows()
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
        except Exception:
            logger.warning("nudge: note_user_message 落库失败", exc_info=True)

    # ---------- 主循环 ----------

    async def tick(self, now: float | None = None) -> dict:
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
        try:
            await self.register_targets()
        except Exception:
            logger.warning("nudge: 目标同步失败，本轮跳过", exc_info=True)
            return out
        rows = await self._nudge_rows()
        out["targets"] = len(rows)
        for row in rows:
            try:
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
        parsed = parse_target(target)
        if parsed is None:
            await self._disable(sid, target, f"目标不合法：{target!r}")
            return "bad_target"
        _, ident = parsed
        user_id = str(ident)
        session_id = await self.store.resolve_session(user_id, None)
        state = dict(row.get("params") or {})
        last_user = await self.store.last_user_message_ts(session_id)

        # 1) 你发了新消息 → 计数清零，倒计时从这条消息重新起算。
        #    用 last_user_seen 去重：否则每个 tick 都会遇到"最后一条消息比上次
        #    发送时间晚"，把已经排好的下一次无限往后推，第一条永远发不出去。
        seen = float(state.get("last_user_seen") or 0)
        last_sent = float(state.get("last_sent_at") or 0)
        if last_user is not None and last_user > seen:
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

        # 3) 计划时刻（None = 还没排过：新目标或已被 stop 清空）
        due = row.get("next_run")
        if due is None:
            if last_user is None:
                await self._disable(sid, target, "目标没有会话记录（配错人了？）")
                return "no_session"
            nxt = earliest_allowed(
                cfg, now + self._first_delay(cfg), last_user=last_user
            )
            await self._save(sid, state, nxt)
            return "scheduled"
        if due > now and not force:
            return "scheduled"

        # 4) 第三条固定是告别：没到第 14 天就只等着
        kind = "normal"
        if unanswered == cfg.max_unanswered - 1:
            goodbye_at = self._goodbye_at(cfg, last_user if last_user else last_sent)
            if now < goodbye_at and not force:
                await self._save(
                    sid, state, earliest_allowed(cfg, goodbye_at, last_user)
                )
                return "waiting_goodbye"
            kind = "goodbye"

        # 5) 当日配额（daily_cap_normal=0 表示不限，与 push 的封顶口径一致）
        counts = self._counts(state, now)
        total = sum(counts.values())
        if (
            kind == "normal"
            and cfg.daily_cap_normal > 0
            and counts.get("normal", 0) >= cfg.daily_cap_normal
        ):
            state["last_skip_reason"] = "daily_cap"
            await self._save(sid, state, self._next_midnight(now))
            return "capped"
        if total >= cfg.max_daily_total:
            state["last_skip_reason"] = "daily_cap_total"
            await self._save(sid, state, self._next_midnight(now))
            return "capped"

        # 6) 预算硬闸门：跳过并顺延到次日，不停用、不告警（与 push 同口径）
        blocked, reason = get_budget().chat_blocked()
        if blocked:
            state["last_skip_reason"] = "budget"
            await self._save(sid, state, self._next_midnight(now))
            return "budget"

        # 7) 生成
        text = await self._generate(cfg, state, kind, user_id)
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
            counts["normal"] = counts.get("normal", 0) + 1
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
        state["last_sent_at"] = now
        state["last_kind"] = kind
        state["last_text"] = text
        state["failures"] = 0
        state["recent_texts"] = self._push_recent(state, text)
        counts[kind] = counts.get(kind, 0) + 1
        state["counts"] = {"day": tz.day_key(now), **counts}
        state["unanswered"] = unanswered + 1
        state["last_skip_reason"] = ""
        if kind == "goodbye":
            state["unanswered"] = cfg.max_unanswered
            state["paused_until_user"] = True
        await self._save(sid, state, self._next_after(cfg, state, now, last_user))
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
        instruction = _GOODBYE_TASK if kind == "goodbye" else _NORMAL_TASK
        with route_context("nudge"):
            text = await self.engine.generate_proactive(
                context,
                instruction=instruction,
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
            await self._disable(sid, target, f"连续 {fails} 次投递失败")
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

    async def _disable(self, sid: str, target: str, reason: str) -> None:
        logger.error("nudge: target %s disabled: %s", target, reason)
        _diag_record("nudge_disabled", target=target, reason=reason[:40])
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
        """立即触发一次（``/nudge run``，调试用）；返回结果串。"""
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
            cur = self._row_for(await self._nudge_rows(), target) or {}
            params = cur.get("params") or {}
            nxt = cur.get("next_run")
            if params.get("paused_until_user"):
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
