"""把现有 tools 注册为默认 skill。"""
import logging
import os

from agentcore.skills.registry import SkillRegistry
from agentcore.skills.file_sender import register_file_skills
from agentcore.tools.registry import fetch_url, get_weather, calc
from agentcore.skills.search import create_search_skill

logger = logging.getLogger(__name__)


def register_builtin_skills(registry: SkillRegistry) -> None:
    registry.register(
        "fetch_url",
        "抓取网页正文内容。仅当用户明确给出了具体的网址/链接时使用；不要用于搜索热点或自行猜测网址（反爬站会返回 503/429）。",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "用户提供的具体网址"}
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
    search_key = (os.getenv("SEARCH_API_KEY") or "").strip()
    logger.info("Search config: provider=%s key_set=%s", os.getenv("SEARCH_PROVIDER"), bool(search_key))
    if search_key:
        try:
            manifest, handler = create_search_skill()
            registry.install(manifest, handler=handler)
            logger.info("Search skill registered: %s", manifest.name)
        except Exception:
            logger.exception("Skip search skill due to registration failure")

    # 文件发送 skill：默认注册，但真正发送依赖 NapCat HTTP 配置
    register_file_skills(registry)
    logger.info("File skill registered: send_markdown_file")
