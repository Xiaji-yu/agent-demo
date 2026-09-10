"""基础工具 skill：安全计算器 + 天气查询。

从旧的 `agentcore/tools/registry.py` 迁移而来（双轨制收敛），行为保持，
但增强了：计算器支持常用数学函数白名单；天气支持未来几天预报。
"""
from __future__ import annotations

import ast
import logging
import math
import operator
import re

import httpx

logger = logging.getLogger(__name__)

# 只放行这些运算与函数，其余一律拒绝（杜绝 ast 逃逸）
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "pow": pow,
}
_CONSTS = {"pi": math.pi, "e": math.e}
_SAFE_EXPR_RE = re.compile(r"^[0-9a-zA-Z_+\-*/().,%\s]+$")


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"不支持的常量类型：{type(node.value).__name__}")
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ValueError(f"未知的标识符：{node.id}")
    if isinstance(node, ast.BinOp):
        fn = _BIN_OPS.get(type(node.op))
        if fn is None:
            raise ValueError(f"不支持的运算符：{type(node.op).__name__}")
        return fn(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        fn = _UNARY_OPS.get(type(node.op))
        if fn is None:
            raise ValueError(f"不支持的一元运算符：{type(node.op).__name__}")
        return fn(_eval_node(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            name = getattr(node.func, "id", type(node.func).__name__)
            raise ValueError(f"不支持的函数：{name}")
        if node.keywords:
            raise ValueError("不支持关键字参数")
        return _FUNCS[node.func.id](*[_eval_node(a) for a in node.args])
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval_node(e) for e in node.elts]
    raise ValueError(f"不支持的表达式：{type(node).__name__}")


def evaluate(expr: str) -> str:
    """计算表达式；返回结果字符串或 "Error: ..."。"""
    expr = (expr or "").strip()
    if not expr:
        return "Error: 表达式为空"
    if len(expr) > 200:
        return "Error: 表达式过长"
    normalized = expr.replace("，", ",").replace("×", "*").replace("÷", "/")
    if not _SAFE_EXPR_RE.match(normalized):
        return "Error: 表达式含不支持的字符"
    try:
        tree = ast.parse(normalized, mode="eval")
        value = _eval_node(tree)
    except Exception as e:
        return f"Error: {e}"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.10g}"
    return str(value)


async def get_weather_text(city: str, days: int = 0) -> str:
    """查天气。days<=0 只给当前；days>0 附带未来几天预报（wttr.in，无需 key）。"""
    city = (city or "").strip()
    if not city:
        return "请提供城市名（中英文均可，如 北京 / Beijing）"
    if len(city) > 40:
        return "城市名过长"
    days = max(0, min(int(days or 0), 3))
    url = f"https://wttr.in/{city}"
    params = {"format": "j1"} if days else {"format": "3"}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(
                url, params=params, headers={"User-Agent": "curl/8 (agent-demo)"}
            )
            resp.raise_for_status()
    except Exception as e:
        return f"天气查询失败：{type(e).__name__}"

    if not days:
        return resp.text.strip()[:400]

    try:
        data = resp.json()
    except Exception:
        return resp.text.strip()[:400]
    cur = (data.get("current_condition") or [{}])[0]
    area = ((data.get("nearest_area") or [{}])[0].get("areaName") or [{}])
    name = area[0].get("value") if isinstance(area, list) and area else city
    lines = [
        f"{name} 当前：{cur.get('weatherDesc', [{}])[0].get('value', '?')} "
        f"{cur.get('temp_C', '?')}℃（体感 {cur.get('FeelsLikeC', '?')}℃）"
        f" 湿度 {cur.get('humidity', '?')}% 风 {cur.get('windspeedKmph', '?')}km/h"
    ]
    for day in (data.get("weather") or [])[: days + 1]:
        desc = (day.get("hourly") or [{}])[4].get("weatherDesc", [{}])[0].get("value", "")
        lines.append(
            f"{day.get('date', '?')}：{desc} {day.get('mintempC', '?')}~{day.get('maxtempC', '?')}℃"
        )
    return "\n".join(lines)


def register_basic_skills(registry) -> None:
    @registry.register(
        "calc",
        "安全算术计算。支持 + - * / // % ** 与括号，以及 sqrt/abs/round/min/max/log/floor/ceil 等函数和 pi/e 常量。",
        {
            "type": "object",
            "properties": {
                "expr": {"type": "string", "description": "算术表达式，如 2*(3+4) 或 sqrt(16)+pi"}
            },
            "required": ["expr"],
        },
        permission="public",
    )
    async def calc_skill(expr: str) -> str:
        return evaluate(expr)

    @registry.register(
        "get_weather",
        "查询城市天气（wttr.in，无需 key）。days>0 时附带未来几天预报。",
        {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名，中英文均可，如 北京 / Beijing"},
                "days": {"type": "integer", "description": "附带未来几天预报，0~3，默认 0"},
            },
            "required": ["city"],
        },
        permission="public",
    )
    async def weather_skill(city: str, days: int = 0) -> str:
        return await get_weather_text(city, days)
