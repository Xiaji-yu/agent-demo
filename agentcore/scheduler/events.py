"""记忆事件（nudge P3）：从**你自己发过的消息**里确定性抽取"未来的事"。

为什么不让 LLM 抽：事件触发是唯一会替你做决定的主动联系（"明天要面试"
→ 明早来问结果）。LLM 抽取有两个致命偏差——把"上周面试了"当未来事件、
把"明天把 bug 修完"这种随口一说当承诺。这里全部用确定性规则，宁可漏：

1. 时间词与事件词必须**同一小句**共现（"，。！？；" 切句）；
2. 时间词只认**日粒度**（明天/周X/月日/YYYY-MM-DD）；"10 分钟后"归提醒管；
3. 过去时/否定/周期性表述一律拒（昨天、已经、结束了、取消了、不去了、每天…）；
4. 只向前看 ``event_lookahead_days`` 天，超期丢弃。

提前量按事件类型分档（面试/考试/答辩 = 前一晚 + 当天早上；出差/会议 = 当天早上；
截止/上线 = 前 24h；生日 = 前一天 + 当天）。追问只一次：问过没回，次日同时段
再问一次，之后丢弃——与"宁可漏"一致。
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from agentcore import tz

logger = logging.getLogger(__name__)

# 事件词 → 类型（键即可配置化：Web 上改的是这个列表）
DEFAULT_EVENT_WORDS = {
    "面试": "interview",
    "考试": "exam",
    "答辩": "defense",
    "出差": "travel",
    "开会": "meeting",
    "会议": "meeting",
    "约": "appointment",
    "见面": "appointment",
    "体检": "checkup",
    "复诊": "checkup",
    "截止": "deadline",
    "上线": "launch",
    "发布": "launch",
    "比赛": "contest",
    "婚礼": "wedding",
    "搬家": "moving",
    "签合同": "signing",
    "开庭": "court",
    "缴费": "payment",
    "验收": "review",
    "评审": "review",
}

# 提前量：[(相对天数, "HH:MM")]——面试类给两次，其余一次
LEAD_BY_KIND = {
    "interview": [(-1, "20:00"), (0, "08:30")],
    "exam": [(-1, "20:00"), (0, "08:30")],
    "defense": [(-1, "20:00"), (0, "08:30")],
    "deadline": [(-1, "20:00")],
    "launch": [(-1, "20:00")],
    "birthday": [(-1, "20:00"), (0, "09:00")],
}
DEFAULT_LEAD = [(0, "08:30")]

# 日粒度时间词
_DAY_OFFSET_WORDS = {
    "今天": 0,
    "今晚": 0,
    "明早": 1,
    "明天": 1,
    "明日": 1,
    "后天": 2,
    "大后天": 3,
}
_WEEK_RE = re.compile(r"(下下周|下周|本周|这周|周|星期)([一二三四五六日天1-7])")
_WEEK_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
    "1": 0,
    "2": 1,
    "3": 2,
    "4": 3,
    "5": 4,
    "6": 5,
    "7": 6,
}
_MD_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
_YMD_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})\s*[日号]?")
_CLOCK_RE = re.compile(
    r"(早上|上午|中午|下午|傍晚|晚上|凌晨)?\s*(\d{1,2})\s*(?:[点:：时])\s*(\d{1,2})?\s*分?"
)
_PERIOD_HINT = {
    "早上": 0,
    "上午": 0,
    "凌晨": 0,
    "中午": 12,
    "下午": 12,
    "傍晚": 12,
    "晚上": 12,
}

# 过去/否定/周期标记：出现在时间词**前面 10 字**内即拒（"昨天加班到很晚，
# 明天还要面试"里"昨天"离"明天"远，不该连坐）
_PAST_NEAR_RE = re.compile(r"(上周|上周五|昨天|昨晚|昨夜|前天|去年|上个月|月前|年前)")
# 全局拒绝： anywhere in the clause
_PAST_ANY_RE = re.compile(
    r"(已经|结束了|完事了|完成了|取消了|不去了|没去|推了|改期|延期|爽约)"
)
_RECURRING_RE = re.compile(r"(每天|每日|天天|每周|每月|常年|经常|习惯性)")
_NEGATION_RE = re.compile(r"(不|没|别|无)[^，。！？；]{0,3}$")

_CLAUSE_SPLIT = re.compile(r"[，。！？；!?;\n]")
_QUOTE_MAX = 30


def _clauses(text: str) -> list[tuple[int, str]]:
    """切句并保留偏移，供"同句共现"判定与引用截取。"""
    out: list[tuple[int, str]] = []
    start = 0
    for m in _CLAUSE_SPLIT.finditer(text or ""):
        out.append((start, (text or "")[start : m.start()]))
        start = m.end()
    if text and start < len(text):
        out.append((start, text[start:]))
    return [(off, c) for off, c in out if c.strip()]


def _resolve_day(clause: str, now: dt.datetime) -> dt.date | None:
    """从小句里解析"哪一天"；解析不出返回 None。"""
    for word, offset in _DAY_OFFSET_WORDS.items():
        if word in clause:
            return (now + dt.timedelta(days=offset)).date()
    m = _WEEK_RE.search(clause)
    if m:
        delta_days = int(m.group(1) in ("下周", "下下周")) * 7
        target = _WEEK_MAP[m.group(2)]
        base = now + dt.timedelta(days=delta_days)
        days_ahead = (target - base.weekday()) % 7
        if days_ahead == 0 and delta_days == 0:
            days_ahead = 7  # "周三"说在今天之后，未指明则推一周
        return (base + dt.timedelta(days=days_ahead)).date()
    m = _YMD_RE.search(clause)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _MD_RE.search(clause)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            return None
        if candidate < now.date():
            candidate = dt.date(year + 1, month, day)
        return candidate
    return None


def _resolve_clock(clause: str) -> tuple[int, int]:
    """小句里的时刻；没有则按默认（早上 9 点）。"""
    m = _CLOCK_RE.search(clause)
    if not m:
        return 9, 0
    hint, hh, mm = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    if not m.group(3) and "半" in clause[m.end() : m.end() + 2]:
        mm = 30
    if hint and hh < 12 and _PERIOD_HINT.get(hint) == 12:
        hh += 12
    if hint == "中午" and hh < 12:
        hh = 12
    if hh > 23 or mm > 59:
        return 9, 0
    return hh, mm


def _event_kind(clause: str, words: dict) -> str | None:
    for word, kind in words.items():
        if word and word in clause:
            return kind
    return None


def _quote_of(text: str, start: int, clause: str) -> str:
    """引用用户原话（≤30 字）：整句太短就整句，太长截到事件词附近。"""
    quote = clause.strip()
    if len(quote) <= _QUOTE_MAX:
        return quote
    return quote[:_QUOTE_MAX].strip()


def extract_events(
    text: str,
    now_ts: float,
    *,
    event_words: dict | None = None,
    lookahead_days: int = 60,
) -> list[dict]:
    """从一条用户消息里抽未来事件；返回 [{kind, at, quote}]（按时间升序）。"""
    words = event_words or DEFAULT_EVENT_WORDS
    now = dt.datetime.fromtimestamp(float(now_ts), tz.zoneinfo())
    out: list[dict] = []
    seen: set[tuple] = set()
    clauses = _clauses(text)
    for index, (offset, clause) in enumerate(clauses):
        if _RECURRING_RE.search(clause):
            continue  # "每天 9 点提醒我"是周期习惯，不是一次性事件
        kind = _event_kind(clause, words)
        if kind is None:
            continue
        day = _resolve_day(clause, now)
        if day is None:
            continue
        # 时间词**前面** 10 字内的过去标记才连坐："昨天加班到很晚，明天还要面试"
        # 里的"昨天"离"明天"远，不该把明天的面试一起毙掉
        m = _WEEK_RE.search(clause) or _YMD_RE.search(clause) or _MD_RE.search(clause)
        if m is None:
            for word in _DAY_OFFSET_WORDS:
                idx = clause.find(word)
                if idx >= 0:
                    m = re.match(r"", clause)  # 占位：用偏移即可
                    break
        probe = m.start() if m is not None else 0
        if _PAST_NEAR_RE.search(clause[max(0, probe - 10) : probe]):
            continue
        # 否定/过去标记常落在**下一句**（"明天面试，已经结束了"）：
        # 只看当前小句会漏掉整整一半的口语表达
        if _PAST_ANY_RE.search(clause) or (
            index + 1 < len(clauses) and _PAST_ANY_RE.search(clauses[index + 1][1])
        ):
            continue
        hh, mm = _resolve_clock(clause)
        try:
            when = dt.datetime.combine(day, dt.time(hh, mm), tzinfo=tz.zoneinfo())
        except ValueError:
            continue
        at = when.timestamp()
        if at <= now_ts:
            continue  # 已过去：追问没有意义
        if at - now_ts > lookahead_days * 86400:
            continue
        key = (kind, day, hh, mm)
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "at": at, "quote": _quote_of(text, offset, clause)})
    out.sort(key=lambda e: e["at"])
    return out


def triggers_for(kind: str, at: float) -> list[float]:
    """事件的"该来问"时刻列表（升序）。"""
    local = dt.datetime.fromtimestamp(float(at), tz.zoneinfo())
    out: list[float] = []
    for day_offset, hhmm in LEAD_BY_KIND.get(kind, DEFAULT_LEAD):
        hh, mm = (int(x) for x in hhmm.split(":"))
        try:
            when = (local + dt.timedelta(days=day_offset)).replace(
                hour=hh, minute=mm, second=0, microsecond=0
            )
        except ValueError:
            continue
        out.append(when.timestamp())
    return sorted(out)


# 必须挂在"用户/我"上：facts 里同时存在「用户的朋友生日是3月5日」这类第三人称
# 记录，宽松匹配会把别人的生日当成主人的（变异复核抓到的漏网）
_BIRTHDAY_RE = re.compile(r"(?:用户|我)(?:的)?生日[是为]?\s*(\d{1,2})\s*月\s*(\d{1,2})")


def extract_birthday(facts, now_ts: float) -> dict | None:
    """从长期事实里解析生日（只认"生日是X月X日"这类明确句式）。

    ``facts`` 是 ``store.list_facts`` 的形状（``list[str]``，最新优先）。
    取不到返回 None——没有可靠来源就不发生日祝福，绝不猜。
    """
    now = dt.datetime.fromtimestamp(float(now_ts), tz.zoneinfo())
    for item in facts or []:
        content = str(item or "")
        m = _BIRTHDAY_RE.search(content)
        if not m:
            continue
        month, day = int(m.group(1)), int(m.group(2))
        try:
            candidate = dt.date(now.year, month, day)
        except ValueError:
            continue
        if candidate < now.date():
            candidate = dt.date(now.year + 1, month, day)
        when = dt.datetime.combine(candidate, dt.time(9, 0), tzinfo=tz.zoneinfo())
        return {
            "kind": "birthday",
            "at": when.timestamp(),
            "quote": content.strip()[:_QUOTE_MAX],
        }
    return None


# 生成指令：只允许围绕用户原话，不允许扩写（外部/记忆内容 = 不可信数据）
EVENT_TASK = (
    "你自己之前提过一件事（见下方围栏，是他原话）。现在到点了，"
    "自然地提一句、问一句进展。**只围绕这句原话**，不要补充细节、"
    "不要替他下结论，也不要提「我记着 / 根据记录 / 你之前说过」这类话。"
)

EVENT_FOLLOWUP_TASK = (
    "你上次问过这件事（见下方围栏，是他原话），他没回。再自然地问一句，"
    "一句就够：不要抱怨、不要重复整段、不要用「你怎么不回我」。"
)
