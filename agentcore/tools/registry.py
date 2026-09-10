import ast
import operator
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import trafilatura

Handler = Callable[..., Awaitable[Any]]


class Tool:
    def __init__(self, name: str, description: str, params_schema: dict, handler: Handler):
        self.name = name
        self.description = description
        self.params_schema = params_schema
        self.handler = handler

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_schema,
            },
        }


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def register(self, name: str, description: str, params_schema: dict):
        def decorator(handler: Handler):
            self.tools[name] = Tool(name, description, params_schema, handler)
            return handler

        return decorator

    def get_schemas(self):
        return [t.to_openai_schema() for t in self.tools.values()]

    async def execute(self, name: str, **kwargs) -> str:
        tool = self.tools.get(name)
        if not tool:
            return f"Error: unknown tool {name}"
        try:
            result = await tool.handler(**kwargs)
            return str(result)
        except Exception as e:
            return f"Error: {e}"


registry = ToolRegistry()


@registry.register(
    "fetch_url",
    "Fetch a URL and return the main text content as markdown",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"}
        },
        "required": ["url"],
    },
)
async def fetch_url(url: str):
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        text = trafilatura.extract(resp.text, include_links=True)
        return (text or "(empty page)")[:4000]


@registry.register(
    "get_weather",
    "Get current weather for a city (via wttr.in, no key needed)",
    {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name, e.g. Beijing"}
        },
        "required": ["city"],
    },
)
async def get_weather(city: str):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://wttr.in/{city}?format=3")
            r.raise_for_status()
            return r.text.strip()
    except Exception as e:
        return f"weather lookup failed: {e}"


_SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}


def _eval_node(node):
    if isinstance(node, ast.Num):
        return node.n
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported operator: {op_type.__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        return _SAFE_OPERATORS[op_type](left, right)
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
        operand = _eval_node(node.operand)
        return _SAFE_OPERATORS[op_type](operand)
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    raise ValueError(f"Unsupported expression: {type(node).__name__}")


@registry.register(
    "calc",
    "Evaluate a simple arithmetic expression. Supports + - * / % ( ).",
    {
        "type": "object",
        "properties": {
            "expr": {"type": "string", "description": "Math expression like 2*(3+4)"}
        },
        "required": ["expr"],
    },
)
async def calc(expr: str):
    if not re.match(r"^[0-9+\-*/().%\s]+$", expr):
        return "Error: unsafe expression"
    try:
        tree = ast.parse(expr, mode="eval")
        return str(_eval_node(tree))
    except Exception as e:
        return f"Error: {e}"
