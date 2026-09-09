"""NoneBot 薄插件：消息层 ↔ agentcore 适配层"""
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

matcher = None
admin = None


def _get_driver():
    from nonebot import get_driver

    return get_driver()


def _load_plugin_modules():
    global matcher, admin
    if matcher is None:
        import importlib

        _matcher = importlib.import_module(".matcher", __name__)
        _admin = importlib.import_module(".admin", __name__)
        matcher = _matcher
        admin = _admin


try:
    _driver = _get_driver()

    @_driver.on_startup
    async def _init_agent():
        _load_plugin_modules()

        from agentcore.memory.store import PgMemoryStore, InMemoryMemoryStore
        from agentcore.llm.client import LLMClient
        from agentcore.loop.engine import AgentEngine
        from agentcore.skills.registry import SkillRegistry
        from agentcore.skills.permissions import PermissionChecker
        from agentcore.skills.builtin import register_builtin_skills
        from agentcore.skills.installer import SkillInstaller
        from agentcore.embedding import load_embedding_client_from_env
        import yaml

        cfg_path = os.getenv("AGENT_CONFIG", "config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            CONFIG = yaml.safe_load(f) or {}

        # 探测 embedding 实际维度（远程模型以真实输出为准），失败不阻塞启动：
        # 回退到本地配置维度，embedding 调用在 engine 内已静默容错
        embedding = load_embedding_client_from_env()
        try:
            embedding_dim = await embedding.probe_dim()
        except Exception:
            embedding_dim = embedding.dim
            logger.exception(
                "embedding probe failed, fallback dim=%s; facts recall may degrade",
                embedding_dim,
            )

        db_url = os.getenv("DATABASE_URL", "")
        if db_url:
            memory = PgMemoryStore(db_url, dim=embedding_dim)
            await memory.init()
        else:
            memory = InMemoryMemoryStore()

        skills_cfg = CONFIG.get("skills", {}) or {}
        skill_default = skills_cfg.get("default_permission", "public")
        nb_superusers = set(_driver.config.superusers or [])
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

        from agentcore.personas import PersonaManager

        persona_manager = PersonaManager()

        agent_cfg = CONFIG.get("agent", {}) or {}
        engine = AgentEngine(
            llm,
            skill_registry,
            memory,
            agent_cfg,
            embedding=embedding,
            persona_manager=persona_manager,
        )

        matcher.engine = engine
        setattr(_driver, "_agent_memory", memory)
        setattr(_driver, "_agent_embedding", embedding)
        setattr(_driver, "_agent_persona_manager", persona_manager)
except Exception:
    # NoneBot 尚未初始化（如测试环境），跳过插件初始化
    pass
