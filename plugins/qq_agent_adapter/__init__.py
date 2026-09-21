"""NoneBot 薄插件：消息层 ↔ agentcore 适配层"""

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

matcher = None
admin = None
music_route = None


def _get_driver():
    from nonebot import get_driver

    return get_driver()


def _superuser_ids() -> list[int]:
    """SUPERUSERS 里可用的 QQ 号（ASCII 数字），已排序去重。

    L3（REVIEW-6ec3f7c..a36ea1d）：原用 ``uid.isdigit()``——全角 ``"１２３"`` 也能
    通过并 ``int()`` 成 123，于是含用户内容的告警/成长提议会被发到**另一个** QQ 号。
    AGENTS.md §5 明确要求 QQ 号用 ``isascii() and isdigit()``。
    """
    from agentcore.workspace.utils import load_superusers

    out: list[int] = []
    for uid in sorted(load_superusers()):
        s = str(uid).strip()
        if s.isascii() and s.isdigit():
            out.append(int(s))
        elif s:
            logger.warning("SUPERUSERS 中 %r 不是 ASCII 数字，已跳过（防发错人）", s)
    return out


async def _notify_superusers(bot, text: str) -> None:
    """私聊推送全部 superuser（embedding 告警与人格成长提议共用）。

    L5：单条发送失败不影响其余 superuser——避免"某个号被封/拉黑"导致整批告警丢失。
    """
    for uid in _superuser_ids():
        try:
            await bot.send_private_msg(user_id=uid, message=text)
        except Exception:
            logger.warning("推送 superuser %s 失败", uid, exc_info=True)


def _embedding_hint_for(exc: BaseException) -> str:
    """按异常类型给**对症**的排障指引（M16）。

    旧文案无论何种失败都写「Ollama 未启动 / CPU 太慢、调大 EMBEDDING_TIMEOUT」——
    对 404（模型名错）或 URL 写反是**错误指引**，会把运维引到错方向。
    """
    import httpx

    text = f"{exc!r}"
    if "404" in text or "not found" in text.lower() or "model" in text.lower():
        return "常见原因：EMBEDDING_MODEL 与服务商实际模型名不一致（或模型未部署）。"
    if isinstance(exc, httpx.UnsupportedProtocol) or "protocol" in text.lower():
        return (
            "常见原因：EMBEDDING_BASE_URL 少了 http:// 或 https://，"
            "或误把 /embeddings 路径也写了进去（客户端会自行拼接）。"
        )
    if "401" in text or "403" in text or "unauthorized" in text.lower():
        return "常见原因：EMBEDDING_API_KEY 无效或已过期。"
    return (
        "常见原因：Ollama 未启动，或本地 CPU 推理太慢导致请求超时"
        "（可调大 EMBEDDING_TIMEOUT、调小 EMBEDDING_BATCH）。"
    )


def _growth_interval_env() -> int:
    """`AGENT_PERSONA_GROWTH_INTERVAL` 解析：>0 为轮数阈值，0/负值表示**关闭**（L5）。

    脏值告警后回退默认 30（而不是静默变成 1 = 最激进档）。
    """
    raw = (os.getenv("AGENT_PERSONA_GROWTH_INTERVAL") or "").strip()
    if not raw:
        return 30
    try:
        return int(raw)
    except ValueError:
        logger.warning("AGENT_PERSONA_GROWTH_INTERVAL=%r 不是整数，回退 30", raw)
        return 30


def _probe_timeout_seconds() -> float:
    """启动期 embedding 探测的墙钟上限（秒）。默认 10s，0 或脏值回退默认。

    M2（REVIEW-6ec3f7c..a36ea1d）：probe 曾是 on_startup 里唯一无上限的 await，
    embedding 不可达时把启动挂 ~15 分钟（与 probe_dim docstring 的"不阻塞启动"相反）。
    """
    raw = (os.getenv("EMBEDDING_PROBE_TIMEOUT") or "").strip()
    if not raw:
        return 10.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("EMBEDDING_PROBE_TIMEOUT=%r 不是数字，回退 10s", raw)
        return 10.0
    if value <= 0:
        logger.warning("EMBEDDING_PROBE_TIMEOUT=%r 非法（须 > 0），回退 10s", raw)
        return 10.0
    return value


def _music_env_ready() -> bool:
    """点歌的 env 前置闸门（依赖探测由 music_route 模块自身完成）。

    这里只查最便宜的两项 env，避免为没配功能的部署白 import 整个音乐模块
    （那会连带 import pysilk）。真正的 pysilk / ffmpeg 探测在
    ``music_route._missing_deps()`` 里，缺了只打日志、不注册 skill。
    """
    return bool(
        (os.getenv("AGENT_MUSIC_API_URL") or "").strip()
        and (os.getenv("NAPCAT_HTTP_URL") or "").strip()
    )


def _merge_music_group_grants(group_skills: dict) -> dict:
    """把点歌群白名单合并进 skill 的 group 授权表（见 ``_init_agent`` 调用处）。

    ``play_music`` 用**非 public** 权限串注册：hardened 部署（config.yaml 里是
    真实 superuser 名单）下未授权用户连 schema 都看不到，白名单群在这里显式拿到
    ``play_music`` 授权。默认部署 ``superusers: ["*"]`` 时人人可见——那种配置下
    权限层不是边界，真正拦人的是 handler 内的群白名单硬闸（两层都要）。
    """
    merged = {k: set(v) for k, v in (group_skills or {}).items()}
    try:
        from agentcore.music.gate import allowed_groups
    except Exception:  # pragma: no cover - agentcore.music 依赖异常时不影响启动
        logger.warning("点歌白名单合并失败（skill 授权可能不完整）", exc_info=True)
        return merged
    for g in allowed_groups():
        merged.setdefault(g, set()).add("play_music")
    return merged


def _load_plugin_modules():
    global matcher, admin, music_route
    if matcher is None:
        import importlib

        _matcher = importlib.import_module(".matcher", __name__)
        _admin = importlib.import_module(".admin", __name__)
        matcher = _matcher
        admin = _admin
    # 点歌是纯增量功能：没配 API / OneBot HTTP 就不导入，于是没有 skill、
    # 消息落给普通聊天。核心（agentcore/skills、matcher、outbound）不受影响。
    if music_route is None and _music_env_ready():
        import importlib

        music_route = importlib.import_module(".music_route", __name__)


def _reminder_tick_seconds() -> int:
    """解析 AGENT_REMINDER_TICK（提醒轮询间隔秒数）。

    L11：非整数值（如 "10s"）warning 并回退 30，不再让启动崩溃；
    结果钳制最小 5 秒（0/负数只会让轮询空转刷日志；scheduler 侧另有
    同值钳制，这里收敛解析源头）。
    """
    raw = (os.getenv("AGENT_REMINDER_TICK") or "").strip()
    default = 30
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("AGENT_REMINDER_TICK=%r 不是整数，回退为 %d 秒", raw, default)
        value = default
    return max(5, value)


async def _init_memory(db_url: str, dim: int):
    """初始化存储：PG 失败回退 InMemoryMemoryStore。

    评审 M3（REVIEW-46c85d1..6ec3f7c）：回退前必须 aclose 失败的 PgMemoryStore
    ——init 中途失败时连接池可能已建成，直接丢弃会让泄漏连接伴随进程终生
    （PgMemoryStore.aclose 对 pool=None 幂等，调用安全）。
    """
    from agentcore.memory.store import InMemoryMemoryStore, PgMemoryStore

    if not db_url:
        return InMemoryMemoryStore()
    memory = PgMemoryStore(db_url, dim=dim)
    try:
        await memory.init()
    except Exception as exc:
        logger.error(
            "PG 初始化失败（%s），降级为内存存储；请检查 DATABASE_URL 与网络；"
            "长期记忆/归档/备份将仅在进程内保留，重启丢失",
            exc,
            exc_info=True,
        )
        try:
            await memory.aclose()
        except Exception:
            logger.warning("关闭初始化失败的 PG 存储时出错", exc_info=True)
        return InMemoryMemoryStore()
    return memory


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
        # M2（REVIEW-6ec3f7c..a36ea1d）起 probe_dim 走**单次尝试**预算，探测失败
        # 即回落配置维度；运行期远程失败**不再降级 hash**，而是响亮失败
        # （4xx 立即抛、临时故障退避重试后抛）。
        embedding = load_embedding_client_from_env()

        # 失败推送：Ollama 未启动 / 远程 embedding 不可达时，私聊提醒管理员
        async def _embedding_error_notify(exc: Exception) -> None:
            try:
                bot = None
                if _driver.bots:
                    bot = next(iter(_driver.bots.values()))
                if bot is None:
                    return
                text = (
                    # 带类型名：httpx.ReadTimeout 等的 str() 是空串，只打 {exc}
                    # 会得到「不可达：」这种没有原因的告警（超时与真的没启动
                    # 需要区分——前者调大 EMBEDDING_TIMEOUT / 调小 EMBEDDING_BATCH）
                    f"⚠️ 向量服务调用失败（{type(exc).__name__}）：{exc!r}\n"
                    + _embedding_hint_for(exc)
                    # M16：旧文案写「长期记忆召回已降级，聊天不受影响」，但
                    # dc46b8f 之后远程失败是"退避重试后响亮失败"，交互路径会
                    # 占用 turn 直到预算耗尽——如实描述影响，别让运维误判。
                    + "\n本次调用已失败：本轮召回/事实抽取跳过（不会写入垃圾向量）。"
                    "交互路径的重试受 EMBEDDING_INTERACTIVE_BUDGET 约束。"
                )
                await _notify_superusers(bot, text)
            except Exception:
                logger.warning("embedding notify push failed", exc_info=True)

        embedding.on_error = _embedding_error_notify

        def _embedding_progress(done: int, total: int) -> None:
            """把大批量嵌入的进度写进 /kb samples 的任务状态。

            与 on_error 同一注入模式（agentcore 不认识插件）。小批量调用由
            `note_embedding_progress` 内部过滤，不会覆盖样本导入的进度。
            """
            if admin is not None:
                admin.note_embedding_progress(done, total)

        embedding.on_progress = _embedding_progress

        try:
            # M2：再套一层墙钟上限做纵深防御。probe_dim 自身已是单次尝试，
            # 但单次仍等于 EMBEDDING_TIMEOUT（默认 30s）——on_startup 不该为
            # 一个可选依赖等这么久。超时按"探测失败"处理，回落配置维度。
            embedding_dim = await asyncio.wait_for(
                embedding.probe_dim(), timeout=_probe_timeout_seconds()
            )
        except TimeoutError:
            embedding_dim = embedding.dim
            logger.warning(
                "embedding probe 超时（>%ss），回退配置维度 dim=%s；不阻塞启动",
                _probe_timeout_seconds(),
                embedding_dim,
            )
        except Exception:
            embedding_dim = embedding.dim
            logger.exception(
                "embedding probe failed, fallback dim=%s; facts recall may degrade",
                embedding_dim,
            )

        db_url = os.getenv("DATABASE_URL", "")
        memory = await _init_memory(db_url, embedding_dim)

        skills_cfg = CONFIG.get("skills", {}) or {}
        skill_default = skills_cfg.get("default_permission", "public")
        nb_superusers = set(_driver.config.superusers or [])
        perms_cfg = skills_cfg.get("permissions", {}) or {}
        checker = PermissionChecker(
            superusers=nb_superusers,
            group_skills=_merge_music_group_grants(perms_cfg.get("groups", {})),
            user_skills=perms_cfg.get("users", {}),
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

        # 点歌 skill（LLM 判断是否放歌）：music_route 已由 _load_plugin_modules
        # 条件导入——env 未配/依赖缺失时它是 None 或内部自行不注册，这里自然
        # 跳过，skill 不出现，消息照常走普通聊天，对核心零影响。
        if music_route is not None:
            music_route.register_music_skill(skill_registry)

        # 引擎与 prompt 型 skill 共用一个 httpx 连接池（P1-4）
        llm = get_shared_llm_client()

        # 主备模型切换/恢复 → 私聊推送主人。与 embedding.on_error 同一注入模式
        # （agentcore 不认识 bot）；冷却与边沿触发都在 LLMClient 内，宿主只负责
        # 组织文案与发送。见 agentcore/llm/client.py::_notify_fallback。
        async def _llm_fallback_notify(ev: dict) -> None:
            try:
                if not _driver.bots:
                    return
                bot = next(iter(_driver.bots.values()))
                kind = ev.get("kind")
                if kind == "switched":
                    cooldown = int(ev.get("cooldown") or 0)
                    text = (
                        "⚠️ 主模型不可用，已切换到备用模型\n"
                        f"主模型：{ev.get('primary_model')}\n"
                        f"备用模型：{ev.get('fallback_model')}\n"
                        f"错误类型：{ev.get('error')}\n"
                        "对话仍在继续（走备用线路）；恢复后我会再通知你。"
                    )
                    if cooldown > 0:
                        text += f"\n（持续故障每 {cooldown // 60} 分钟最多提醒一次）"
                elif kind == "recovered":
                    text = (
                        "✅ 主模型已恢复\n"
                        f"主模型：{ev.get('primary_model')}\n"
                        f"已停用备用模型 {ev.get('fallback_model')}"
                    )
                else:
                    text = f"LLM 主备状态变化：{kind}"
                await _notify_superusers(bot, text)
            except Exception:
                logger.warning("llm fallback notify push failed", exc_info=True)

        llm.on_fallback = _llm_fallback_notify

        from agentcore.personas import PersonaManager

        persona_manager = PersonaManager()

        # 人格成长层（成长型人格）：per-user 对话计数达阈值 → LLM 回顾提议 →
        # 私聊推送管理员确认（确认码）→ 滚动合并写入。通知走 on_proposal 回调
        # （agentcore 不认识 bot，与 embedding.on_error 同一注入模式）。
        #
        # L5（REVIEW-6ec3f7c..a36ea1d）：`AGENT_PERSONA_GROWTH_INTERVAL=0` 表示
        # **关闭该功能**（与仓库惯例一致：AGENT_HELP_IMAGE=0 / *_TIMEOUT=0）。
        # 旧实现用 max(1, ...) 把 0 变成 1 —— 语义反转成"每轮都触发回顾 + 私聊
        # 管理员"，且全仓无任何 env 能关掉它。
        from agentcore.personas.growth import GrowthManager

        growth = None
        if _growth_interval_env() > 0:
            growth = GrowthManager(memory, llm, interval=_growth_interval_env())
        else:
            logger.info("人格成长层已关闭（AGENT_PERSONA_GROWTH_INTERVAL=0）")

        async def _growth_proposal_notify(
            user_id: str, proposal: str, code: str
        ) -> None:
            try:
                if not _driver.bots:
                    return
                bot = next(iter(_driver.bots.values()))
                text = (
                    f"🌱 人格成长提议（用户 {user_id}）：\n{proposal}\n\n"
                    f"如认可请回复：确认成长 {code}（10 分钟内有效）"
                )
                await _notify_superusers(bot, text)
            except Exception:
                logger.warning("growth proposal notify failed", exc_info=True)

        growth.on_proposal = _growth_proposal_notify

        # 记录保全：聊天记录 JSONL 归档（DB 之外，7 天滚动）+ 每日数据库备份。
        # 归档包在 store 外层，因此所有写入路径（对话/重置/工具结果）都会留痕。
        from agentcore.backup import ArchivingStore, MessageArchive

        archive_cfg = CONFIG.get("archive", {}) or {}
        archive = None
        if os.getenv("AGENT_ARCHIVE_ENABLED", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }:
            archive = MessageArchive(
                os.getenv("AGENT_ARCHIVE_DIR", archive_cfg.get("dir", "data/archive")),
                keep_days=int(
                    os.getenv(
                        "AGENT_ARCHIVE_KEEP_DAYS", archive_cfg.get("keep_days", 7)
                    )
                ),
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
            growth=growth,
        )
        # 成长确认命令（「确认成长 XXXXXX」）挂在 admin 模块上
        admin.growth = growth

        scheduler = AgentScheduler()
        if kb.enabled:
            scheduler.add_cron(
                "kb_digest", kb.digest_cron, kb.digest, name="每天从记忆蒸馏知识入库"
            )

        # 定时提醒：注册 LLM 工具 + 每 30 秒检查一次到点提醒
        from agentcore.scheduler.reminder import ReminderService
        from agentcore.skills.reminder_skills import register_reminder_skills

        from .sink import Sink

        sink = Sink()
        register_reminder_skills(skill_registry, memory, sink)
        reminders = ReminderService(memory, sink)
        # 轮询间隔：30 秒意味着提醒最多晚 30 秒送达；想更准时可调小
        # （AGENT_REMINDER_TICK，最小 5 秒；非法值自动回退 30，见 L11）
        tick = _reminder_tick_seconds()
        scheduler.add_interval("reminders", tick, reminders.tick, name="定时提醒投递")

        # 每日数据库备份（连 facts/人格/知识库一起保），并把归档滚动清理接到同一调度
        backup_enabled = os.getenv("AGENT_BACKUP_ENABLED", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        if backup_enabled:
            from agentcore.backup import backup_database

            backup_dir = os.getenv(
                "AGENT_BACKUP_DIR", backup_cfg.get("dir", "data/backups")
            )
            backup_mirror = (
                os.getenv("AGENT_BACKUP_MIRROR_DIR", backup_cfg.get("mirror_dir", ""))
                or None
            )
            backup_keep = int(os.getenv("AGENT_BACKUP_KEEP", backup_cfg.get("keep", 7)))
            backup_cron = os.getenv(
                "AGENT_BACKUP_CRON", backup_cfg.get("cron", "30 3 * * *")
            )
            db_url = os.getenv("DATABASE_URL", "")

            async def _daily_backup():
                if not db_url:
                    logger.info(
                        "backup: 未配置 DATABASE_URL（内存模式），跳过数据库备份"
                    )
                else:
                    await backup_database(
                        db_url, backup_dir, keep=backup_keep, mirror_dir=backup_mirror
                    )
                if archive is not None:
                    await archive.prune_async()

            scheduler.add_cron(
                "db_backup",
                backup_cron,
                _daily_backup,
                name="每日数据库备份 + 归档轮转",
            )
        elif archive is not None:
            scheduler.add_cron(
                "archive_prune", "20 3 * * *", archive.prune_async, name="归档滚动清理"
            )

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
        # H5：NoneBot 的停机钩子按注册顺序**逆序**执行，所以这里也必须"先 flush
        # 再 aclose"（顺序由 lifecycle.shutdown_agent 固定，不依赖注册先后）
        from .lifecycle import shutdown_agent

        deb = None
        if matcher is not None:
            try:
                deb = matcher.get_debouncer()
            except Exception:
                logger.exception("get debouncer for shutdown failed")
        await shutdown_agent(
            debouncer=deb,
            memory=getattr(_driver, "_agent_memory", None),
            scheduler=getattr(_driver, "_agent_scheduler", None),
        )
