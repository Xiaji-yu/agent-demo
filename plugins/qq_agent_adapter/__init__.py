"""NoneBot 薄插件：消息层 ↔ agentcore 适配层"""
import os
from nonebot import get_driver
from . import matcher, admin

driver = get_driver()


@driver.on_startup
async def _init_agent():
    from agentcore.memory.store import PgMemoryStore, InMemoryMemoryStore
    from agentcore.llm.client import LLMClient
    from agentcore.loop.engine import AgentEngine
    from agentcore.tools.registry import registry as tool_registry
    import yaml

    cfg_path = os.getenv("AGENT_CONFIG", "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        CONFIG = yaml.safe_load(f) or {}

    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        memory = PgMemoryStore(db_url)
        await memory.init()
    else:
        memory = InMemoryMemoryStore()

    llm = LLMClient()
    engine = AgentEngine(llm, tool_registry, memory, CONFIG.get("agent", {}))

    matcher.engine = engine
    admin.engine = engine
    admin.memory = memory
