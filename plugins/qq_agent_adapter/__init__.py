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

        # 记录保全：聊天记录 JSONL 归档（DB 之外，7 天滚动）+ 每日数据库备份。
        # 归档包在 store 外层，因此所有写入路径（对话/重置/工具结果）都会留痕。
        from agentcore.backup import ArchivingStore, MessageArchive

        archive_cfg = CONFIG.get("archive", {}) or {}
        archive = None
        if (os.getenv("AGENT_ARCHIVE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}):
            archive = MessageArchive(
                os.getenv("AGENT_ARCHIVE_DIR", archive_cfg.get("dir", "data/archive")),
                keep_days=int(os.getenv("AGENT_ARCHIVE_KEEP_DAYS", archive_cfg.get("keep_days", 7))),
            )
            memory = ArchivingStore(memory, archive)

        # M5：公共知识库（全局、脱敏）+ 每天从记忆蒸馏入库
        from agentcore.rag import KnowledgeBase
        from agentcore.scheduler import AgentScheduler

        agent_cfg = CONFIG.get("agent", {}) or {}
        kb_cfg = CONFIG.get("rag", {}) or {}
        backup_cfg = CONFIG.get("backup", {}) or {}

        kb = KnowledgeBase(memory, embedding, kb_cfg, llm=llm)
        engine = AgentEngine(
            llm,
            skill_registry,
            memory,
            agent_cfg,
            embedding=embedding,
            persona_manager=persona_manager,
            kb=kb,
        )

        scheduler = AgentScheduler()
        if kb.enabled:
            scheduler.add_cron("kb_digest", kb.digest_cron, kb.digest, name="每天从记忆蒸馏知识入库")

        # 定时提醒：注册 LLM 工具 + 每 30 秒检查一次到点提醒
        from agentcore.scheduler.reminder import ReminderService
        from agentcore.skills.reminder_skills import register_reminder_skills

        from .sink import Sink

        sink = Sink()
        register_reminder_skills(skill_registry, memory, sink)
        reminders = ReminderService(memory, sink)
        scheduler.add_interval("reminders", 30, reminders.tick, name="定时提醒投递")

        # 每日数据库备份（连 facts/人格/知识库一起保），并把归档滚动清理接到同一调度
        backup_enabled = os.getenv("AGENT_BACKUP_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
        if backup_enabled:
            from agentcore.backup import backup_database

            backup_dir = os.getenv("AGENT_BACKUP_DIR", backup_cfg.get("dir", "data/backups"))
            backup_mirror = os.getenv("AGENT_BACKUP_MIRROR_DIR", backup_cfg.get("mirror_dir", "")) or None
            backup_keep = int(os.getenv("AGENT_BACKUP_KEEP", backup_cfg.get("keep", 7)))
            backup_cron = os.getenv("AGENT_BACKUP_CRON", backup_cfg.get("cron", "30 3 * * *"))
            db_url = os.getenv("DATABASE_URL", "")

            async def _daily_backup():
                if not db_url:
                    logger.info("backup: 未配置 DATABASE_URL（内存模式），跳过数据库备份")
                else:
                    await backup_database(
                        db_url, backup_dir, keep=backup_keep, mirror_dir=backup_mirror
                    )
                if archive is not None:
                    await archive.prune_async()

            scheduler.add_cron("db_backup", backup_cron, _daily_backup, name="每日数据库备份 + 归档轮转")
        elif archive is not None:
            scheduler.add_cron("archive_prune", "20 3 * * *", archive.prune_async, name="归档滚动清理")

        scheduler.start()

        matcher.engine = engine
        setattr(_driver, "_agent_memory", memory)
        setattr(_driver, "_agent_archive", archive)
        setattr(_driver, "_agent_embedding", embedding)
        setattr(_driver, "_agent_persona_manager", persona_manager)
        setattr(_driver, "_agent_engine", engine)
        setattr(_driver, "_agent_kb", kb)
        setattr(_driver, "_agent_scheduler", scheduler)
        setattr(_driver, "_agent_sink", sink)

    @_driver.on_shutdown
    async def _shutdown_agent():
        sched = getattr(_driver, "_agent_scheduler", None)
        if sched is not None:
            try:
                sched.shutdown(wait=False)
            except Exception:
                logger.exception("scheduler shutdown failed")
