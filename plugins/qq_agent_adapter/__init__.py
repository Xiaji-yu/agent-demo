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
except Exception:
    # P1-5：区分「NoneBot 未初始化（测试/脚本环境，静默跳过）」与真实错误。
    # NoneBot 已初始化却拿不到 driver 时是真实故障，必须显式记录并抛出，
    # 而不是吞成"机器人不回复但日志干净"。
    from nonebot import get_driver as _gd

    try:
        _gd()
    except Exception:
        # 确实未初始化：预期情况，静默跳过插件初始化
        _driver = None
    else:
        logger.exception("qq_agent_adapter 加载失败（NoneBot 已初始化）")
        raise
else:

    @_driver.on_startup
    async def _init_agent():
        _load_plugin_modules()

        import yaml

        from agentcore.embedding import load_embedding_client_from_env
        from agentcore.loop.engine import AgentEngine
        from agentcore.memory.store import InMemoryMemoryStore, PgMemoryStore
        from agentcore.skills.builtin import register_builtin_skills
        from agentcore.skills.installer import SkillInstaller
        from agentcore.skills.permissions import PermissionChecker
        from agentcore.skills.registry import (
            SkillRegistry,
            get_shared_llm_client,
        )

        cfg_path = os.getenv("AGENT_CONFIG", "config.yaml")
        with open(cfg_path, encoding="utf-8") as f:
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

        # 让模块级/包级 `registry` 单例也指向真实实例（供外部 import 消费）。
        # 注意：`agentcore/skills/__init__.py` 用 `from .registry import registry`
        # 遮蔽了子模块名，`import agentcore.skills.registry as X` 会得到实例而非
        # 模块，所以这里用 importlib 取真正的模块对象来赋值（修复原空操作）。
        import importlib

        import agentcore.skills as _skills_pkg

        _skill_mod = importlib.import_module("agentcore.skills.registry")
        _skill_mod.registry = skill_registry
        _skills_pkg.registry = skill_registry

        # 引擎与 prompt 型 skill 共用一个 httpx 连接池（P1-4）
        llm = get_shared_llm_client()

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
        setattr(_driver, "_agent_engine", engine)
