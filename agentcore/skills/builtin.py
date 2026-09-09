"""把现有 tools 注册为默认 skill。"""
from agentcore.skills.registry import SkillRegistry
from agentcore.skills.file_sender import register_file_skills
from agentcore.tools.registry import fetch_url, get_weather, calc
from agentcore.skills.search import create_search_skill


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

    # 搜索 skill：若 .env 中配置了 SEARCH_API_KEY，则自动注册
    try:
        import os

        if os.getenv("SEARCH_API_KEY", "").strip():
            manifest, handler = create_search_skill()
            registry.install(manifest, handler=handler)
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("Skip search skill: %s", e)

    # 文件发送 skill：默认注册，但真正发送依赖 NapCat HTTP 配置
    register_file_skills(registry)
