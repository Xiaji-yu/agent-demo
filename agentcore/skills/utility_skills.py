"""实用工具 skill：时间日期 / 单位换算 / 随机抽签。

全部为本地纯计算：不联网、无依赖、不触碰文件系统，因此风险极低、可精确测试。
中文场景做了适配（今天/明天/昨天、星期几、常用单位）。
"""

from __future__ import annotations

import datetime as dt
import random
import re

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_REL_DAYS = {
    "今天": 0,
    "今日": 0,
    "明天": 1,
    "明日": 1,
    "后天": 2,
    "大后天": 3,
    "昨天": -1,
    "前天": -2,
}
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%m-%d", "%m/%d")


# ---------- 时间日期 ----------
def parse_date(text: str, today: dt.date | None = None) -> dt.date:
    """解析日期：相对词 / 多种格式 / 纯数字时间戳。失败抛 ValueError。"""
    today = today or dt.date.today()
    s = (text or "").strip()
    if not s:
        raise ValueError("日期为空")
    if s in _REL_DAYS:
        return today + dt.timedelta(days=_REL_DAYS[s])
    if re.fullmatch(r"\d{10}", s):  # 秒级时间戳
        return dt.datetime.fromtimestamp(int(s)).date()
    m = re.fullmatch(r"(\d+)\s*(天|日)(后|前)", s)
    if m:
        # group(3) 才是「后/前」，group(2) 是「天/日」
        n = int(m.group(1)) * (1 if m.group(3) == "后" else -1)
        return today + dt.timedelta(days=n)
    # 带时间的写法：只取日期部分
    s2 = re.sub(r"[ T]\d{1,2}:\d{2}(:\d{2})?$", "", s).strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed = dt.datetime.strptime(s2, fmt).date()
        except ValueError:
            continue
        if "%Y" not in fmt:  # 缺年份 → 补当前年
            parsed = parsed.replace(year=today.year)
        return parsed
    raise ValueError(f"无法识别日期：{text}")


def now_text(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    week = _WEEKDAYS[now.weekday()]
    return (
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{week}）\n"
        f"ISO：{now.isoformat(timespec='seconds')}\n"
        f"时间戳：{int(now.timestamp())}\n"
        f"今年第 {now.timetuple().tm_yday} 天，第 {now.isocalendar().week} 周"
    )


def date_calc_text(
    mode: str, a: str = "", b: str = "", days: int = 0, today: dt.date | None = None
) -> str:
    """mode: weekday / add / between / parse"""
    today = today or dt.date.today()
    mode = (mode or "").strip().lower()
    try:
        if mode == "weekday":
            d = parse_date(a, today)
            return f"{d.isoformat()} 是{_WEEKDAYS[d.weekday()]}"
        if mode == "add":
            d = parse_date(a, today)
            result = d + dt.timedelta(days=int(days))
            return (
                f"{d.isoformat()} {'+' if int(days) >= 0 else '-'} {abs(int(days))} 天 = "
                f"{result.isoformat()}（{_WEEKDAYS[result.weekday()]}）"
            )
        if mode == "between":
            d1, d2 = parse_date(a, today), parse_date(b, today)
            delta = (d2 - d1).days
            return f"{d1.isoformat()} 到 {d2.isoformat()} 相差 {delta} 天（约 {abs(delta) / 30.44:.1f} 个月）"
        if mode == "parse":
            d = parse_date(a, today)
            return f"{a} → {d.isoformat()}（{_WEEKDAYS[d.weekday()]}）"
    except ValueError as e:
        return f"错误：{e}"
    return "错误：mode 只能是 weekday / add / between / parse"


# ---------- 单位换算 ----------
# 每类以「基准单位」为 1，其他单位给出与基准的比例
_LINEAR_UNITS = {
    "length": {
        "m": 1.0,
        "km": 1000.0,
        "cm": 0.01,
        "mm": 0.001,
        "mi": 1609.344,
        "yard": 0.9144,
        "ft": 0.3048,
        "inch": 0.0254,
        "里": 500.0,
    },
    "mass": {
        "kg": 1.0,
        "g": 0.001,
        "mg": 1e-6,
        "t": 1000.0,
        "lb": 0.45359237,
        "oz": 0.028349523125,
        "斤": 0.5,
        "两": 0.05,
    },
    "data": {
        # M（REVIEW-a604023..679c9b3）：bit 原先映射为 "B"（1:1）→ 换算错 8 倍。
        # 这里作为独立单位接入：1 bit = 1/8 byte。
        "bit": 0.125,
        "B": 1.0,
        "KB": 1024.0,
        "MB": 1024.0**2,
        "GB": 1024.0**3,
        "TB": 1024.0**4,
        "PB": 1024.0**5,
        "KiB": 1024.0,
        "MiB": 1024.0**2,
        "GiB": 1024.0**3,
    },
    "time": {"s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0, "week": 604800.0},
    "speed": {"m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704, "knot": 0.514444},
    "area": {
        "m2": 1.0,
        "km2": 1e6,
        "cm2": 1e-4,
        "公顷": 10000.0,
        "亩": 666.6666666666666,
    },
}
_UNIT_ALIASES = {
    # 英文全称/复数 → 规范单位
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "kilometer": "km",
    "kilometers": "km",
    "kilometre": "km",
    "kilometres": "km",
    "centimeter": "cm",
    "centimeters": "cm",
    "millimeter": "mm",
    "millimeters": "mm",
    "mile": "mi",
    "miles": "mi",
    "foot": "ft",
    "feet": "ft",
    "inch": "inch",
    "inches": "inch",
    "yard": "yard",
    "yards": "yard",
    "gram": "g",
    "grams": "g",
    "kilogram": "kg",
    "kilograms": "kg",
    "kilo": "kg",
    "kilos": "kg",
    "milligram": "mg",
    "milligrams": "mg",
    "ton": "t",
    "tons": "t",
    "tonne": "t",
    "tonnes": "t",
    "pound": "lb",
    "pounds": "lb",
    "ounce": "oz",
    "ounces": "oz",
    "byte": "B",
    "bytes": "B",
    "bit": "bit",
    "bits": "bit",
    "second": "s",
    "seconds": "s",
    "sec": "s",
    "minute": "min",
    "minutes": "min",
    "hour": "h",
    "hours": "h",
    "hr": "h",
    "day": "d",
    "days": "d",
    "week": "week",
    "weeks": "week",
    "square meter": "m2",
    "square meters": "m2",
    "square kilometer": "km2",
    "square kilometers": "km2",
    # 中文别名
    "米": "m",
    "千米": "km",
    "公里": "km",
    "厘米": "cm",
    "毫米": "mm",
    "英里": "mi",
    "英尺": "ft",
    "英寸": "inch",
    "码": "yard",
    "克": "g",
    "千克": "kg",
    "公斤": "kg",
    "毫克": "mg",
    "吨": "t",
    "磅": "lb",
    "盎司": "oz",
    "秒": "s",
    "分钟": "min",
    "分": "min",
    "小时": "h",
    "时": "h",
    "天": "d",
    "日": "d",
    "周": "week",
    "字节": "B",
    "兆": "MB",
    "吉": "GB",
    "平方米": "m2",
    "平方公里": "km2",
    "平方厘米": "cm2",
    "公顷": "公顷",
    "亩": "亩",
}
_TEMP_UNITS = {
    "c",
    "celsius",
    "摄氏",
    "摄氏度",
    "℃",
    "f",
    "fahrenheit",
    "华氏",
    "华氏度",
    "℉",
    "k",
    "kelvin",
    "开",
    "开尔文",
}


def _norm_unit(u: str) -> str:
    u = (u or "").strip()
    if u in _UNIT_ALIASES:
        return _UNIT_ALIASES[u]
    low = u.lower()
    if low in _UNIT_ALIASES:
        return _UNIT_ALIASES[low]
    # M（REVIEW-a604023..679c9b3）：大小写不一致（MB/mb、GB/gb、KM/km）原先既不归一、
    # 报错还写成"不是同一类单位"，属误导。这里按已知单位做一次大小写不敏感匹配。
    for table in _LINEAR_UNITS.values():
        for known in table:
            if known.lower() == low:
                return known
    # 温度单位同样退化为小写匹配
    return low if low in {"c", "f", "k", "celsius", "fahrenheit", "kelvin"} else u


def _to_celsius(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit in ("c", "celsius", "摄氏", "摄氏度", "℃"):
        return value
    if unit in ("f", "fahrenheit", "华氏", "华氏度", "℉"):
        return (value - 32) * 5 / 9
    return value - 273.15  # kelvin


def _from_celsius(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit in ("c", "celsius", "摄氏", "摄氏度", "℃"):
        return value
    if unit in ("f", "fahrenheit", "华氏", "华氏度", "℉"):
        return value * 9 / 5 + 32
    return value + 273.15


def convert(value: float, from_unit: str, to_unit: str) -> str:
    fu, tu = _norm_unit(from_unit), _norm_unit(to_unit)
    if fu in _TEMP_UNITS or tu in _TEMP_UNITS:
        if fu not in _TEMP_UNITS or tu not in _TEMP_UNITS:
            return "错误：温度单位不能与其它类别混用"
        return f"{value:g} {from_unit} = {_from_celsius(_to_celsius(float(value), fu), tu):.4g} {to_unit}"
    for category, table in _LINEAR_UNITS.items():
        if fu in table and tu in table:
            result = float(value) * table[fu] / table[tu]
            return f"{value:g} {from_unit} = {result:.6g} {to_unit}（{category}）"
    known = ", ".join(sorted({u for t in _LINEAR_UNITS.values() for u in t}))
    if fu not in {u for t in _LINEAR_UNITS.values() for u in t}:
        return f"错误：不认识单位 {from_unit!r}。可用：{known}、温度单位 C/F/K"
    return f"错误：{from_unit!r} 与 {to_unit!r} 不是同一类单位（不能跨类别换算）"


# ---------- 随机 ----------
def _rng() -> random.Random:
    return random.SystemRandom()


def dice_text(notation: str) -> str:
    m = re.fullmatch(
        r"\s*(\d{1,2})\s*[dD]\s*(\d{1,4})\s*([+-]\s*\d{1,4})?\s*", notation or ""
    )
    if not m:
        return "错误：骰子格式如 2d6、1d20+3"
    count, faces = int(m.group(1)), int(m.group(2))
    if count < 1 or faces < 2:
        return "错误：骰子数量与面数不合法"
    rng = _rng()
    rolls = [rng.randint(1, faces) for _ in range(count)]
    bonus = int(re.sub(r"\s+", "", m.group(3))) if m.group(3) else 0
    total = sum(rolls) + bonus
    detail = " + ".join(str(r) for r in rolls)
    if len(rolls) > 1:
        detail += f" = {sum(rolls)}"
    if bonus:
        detail += f"，加值 {bonus:+d}"
    return f"{notation.strip()} → {total}（骰子 {detail}）"


def random_text(
    mode: str, items=None, count: int = 1, low: int = 1, high: int = 100
) -> str:
    rng = _rng()
    mode = (mode or "").strip().lower()
    if mode == "number":
        if int(low) > int(high):
            low, high = high, low
        return f"随机数（{low}~{high}）：{rng.randint(int(low), int(high))}"
    if mode == "coin":
        return "抛硬币：" + rng.choice(["正面", "反面"])
    if mode == "dice":
        return dice_text(
            str(items[0]) if isinstance(items, list | tuple) and items else "1d6"
        )
    if mode == "pick":
        pool = [str(i) for i in (items or []) if str(i).strip()]
        if not pool:
            return "错误：请提供候选列表"
        if len(pool) > 200:
            return "错误：候选过多（最多 200 项）"
        count = max(1, min(int(count or 1), len(pool)))
        chosen = rng.sample(pool, count)
        return (
            "抽签结果："
            + "、".join(chosen)
            + (f"（从 {len(pool)} 项中抽 {count} 项）" if len(pool) > 1 else "")
        )
    return "错误：mode 只能是 pick / number / dice / coin"


def register_utility_skills(registry) -> None:
    @registry.register(
        "now",
        "获取当前日期时间、星期、时间戳（本地时区）。用户问「现在几点/今天几号/今天星期几」时用。",
        {"type": "object", "properties": {}},
        permission="public",
    )
    async def now_skill() -> str:
        return now_text()

    @registry.register(
        "date_calc",
        "日期计算。mode=weekday 查某天星期几；mode=add 在日期上加减天数；"
        "mode=between 算两个日期相差几天；mode=parse 规范化日期。"
        "日期可写 2026-09-10、9-10、今天/明天/昨天、3天后。",
        {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["weekday", "add", "between", "parse"],
                },
                "a": {"type": "string", "description": "日期 a"},
                "b": {"type": "string", "description": "日期 b（mode=between 用）"},
                "days": {
                    "type": "integer",
                    "description": "加减天数（mode=add 用，可为负）",
                },
            },
            "required": ["mode"],
        },
        permission="public",
    )
    async def date_calc_skill(
        mode: str, a: str = "", b: str = "", days: int = 0
    ) -> str:
        return date_calc_text(mode, a, b, days)

    @registry.register(
        "unit_convert",
        "单位换算：长度、重量、数据大小、时间、速度、面积、温度。"
        "单位可用英文或中文（如 km/公里、kg/千克、GB、℃/华氏）。",
        {
            "type": "object",
            "properties": {
                "value": {"type": "number", "description": "数值"},
                "from_unit": {"type": "string", "description": "原单位，如 km"},
                "to_unit": {"type": "string", "description": "目标单位，如 mile"},
            },
            "required": ["value", "from_unit", "to_unit"],
        },
        permission="public",
    )
    async def unit_convert_skill(value: float, from_unit: str, to_unit: str) -> str:
        return convert(value, from_unit, to_unit)

    @registry.register(
        "random",
        "随机：从候选里抽签（mode=pick）、随机整数（mode=number）、掷骰子（mode=dice，"
        "items 传如 2d6）、抛硬币（mode=coin）。",
        {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["pick", "number", "dice", "coin"]},
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "候选列表 / 骰子表达式",
                },
                "count": {"type": "integer", "description": "抽几个，默认 1"},
                "low": {"type": "integer", "description": "随机数下界"},
                "high": {"type": "integer", "description": "随机数上界"},
            },
            "required": ["mode"],
        },
        permission="public",
    )
    async def random_skill(
        mode: str, items=None, count: int = 1, low: int = 1, high: int = 100
    ) -> str:
        return random_text(mode, items, count, low, high)
