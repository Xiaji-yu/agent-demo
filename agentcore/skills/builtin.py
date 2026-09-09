"""把现有 tools 注册为默认 skill。"""
from agentcore.skills.registry import SkillRegistry
from agentcore.tools.registry import fetch_url, get_weather, calc


def register_builtin_skills(registry: SkillRegistry) -> None:
    registry.register(
        "fetch_url",
        "抓取网页正文内容，返回 markdown 格式文本（用于阅读链接）",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的网址"}
            },
            "required": ["url"],
        },
        permission="public",
    )(fetch_url)

    registry.register(
        "get_weather",
        "查询城市当前天气（通过 wttr.in，无需 key）",
        {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名，如 Beijing"}
            },
            "required": ["city"],
        },
        permission="public",
    )(get_weather)

    registry.register(
        "calc",
        "安全算术计算，仅支持四则运算与括号",
        {
            "type": "object",
            "properties": {
                "expr": {"type": "string", "description": "算术表达式，如 2*(3+4)"}
            },
            "required": ["expr"],
        },
        permission="public",
    )(calc)
