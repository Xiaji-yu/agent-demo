"""NoneBot 薄插件：消息层 ↔ agentcore 适配层"""
import os
from pathlib import Path

matcher = None
admin = None
_driver = None
_initialized = False


def _get_driver():
    global _driver
    if _driver is None:
        from nonebot import get_driver

        _driver = get_driver()
    return _driver


def _load_plugin_modules():
    global matcher, admin
    if matcher is None:
        from . import matcher as _matcher, admin as _admin

        matcher = _matcher
        admin = _admin


def _ensure_initialized():
    global _initialized
    if _initialized:
        return
    try:
        _load_plugin_modules()
        driver = _get_driver()
    except Exception:
        # NoneBot 尚未初始化（如测试环境），跳过插件初始化
        _initialized = True
        return

    @driver.on_startup
    async def _init_agent():
        from agentcore.memory.store import PgMemoryStore, InMemoryMemoryStore
        from agentcore.llm.client import LLMClient
        from agentcore.loop.engine import AgentEngine
        from agentcore.skills.registry import SkillRegistry
        from agentcore.skills.permissions import PermissionChecker
        from agentcore.skills.builtin import register_builtin_skills
        from agentcore.skills.installer import SkillInstaller
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

        skills_cfg = CONFIG.get("skills", {}) or {}
        skill_default = skills_cfg.get("default_permission", "public")
        nb_superusers = set(driver.config.superusers or [])
        checker = PermissionChecker(
            superusers=nb_superusers,
            group_skills=skills_cfg.get("permissions", {}).get("groups", {}),
            user_skills=skills_cfg.get("permissions", {}).get("users", {}),
            default_permission=skill_default,
        )

        skill_registry = SkillRegistry(permission_checker=checker)
        register_builtin_skills(skill_registry)

        skills_dir = os.getenv("AGENT_SKILLS_DIR", "data/skills")
        installer = SkillInstaller(skills_dir=Path(skills_dir))
        for manifest in installer.list_manifests():
            skill_registry.install(manifest)

        import agentcore.skills.registry as _skill_mod

        _skill_mod.registry = skill_registry

        llm = LLMClient()
        engine = AgentEngine(llm, skill_registry, memory, CONFIG.get("agent", {}))

        matcher.engine = engine
        setattr(driver, "_agent_memory", memory)

    _initialized = True


# 确保 NoneBot 加载插件时完成初始化
_ensure_initialized()
