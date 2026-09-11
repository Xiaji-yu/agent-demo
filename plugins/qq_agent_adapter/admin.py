from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.exception import FinishedException

from agentcore.skills.catalog import CATALOG
from agentcore.skills.installer import SkillInstaller
from agentcore.workspace.utils import is_superuser

from . import _get_driver
from .acl import is_allowed
from .persona_utils import parse_persona_cmd

logger = logging.getLogger(__name__)


DEFAULT_SKILLS_DIR = Path("data/skills")


def _live_registry():
    """取启动时注入的 live registry。

    「from agentcore.skills import registry」会在 import 时捕获单例，而插件启动
    阶段会 swap 成真实实例（`_skill_mod.registry = skill_registry`），捕获到的
    是空注册表。这里从 driver 上读真实 engine.skills，避免陈旧引用。
    """
    engine = getattr(_get_driver(), "_agent_engine", None)
    if engine is not None and getattr(engine, "skills", None) is not None:
        return engine.skills
    # 兜底：取模块级单例（用 importlib 绕过包 __init__ 对子模块名的遮蔽）
    import importlib

    return importlib.import_module("agentcore.skills.registry").registry


reset = on_command("reset", aliases={"重置"}, priority=5, block=True)


@reset.handle()
async def handle_reset(event: MessageEvent):
    if not is_allowed(event):
        await reset.finish("无权限")
    # L25：清会话历史属变更类操作，与 /kb 变更类命令同一标准（仅 superuser）
    if not is_superuser(str(event.get_user_id())):
        await reset.finish("只有管理员能重置会话。")
    user_id = str(event.get_user_id())
    group_id = str(event.group_id) if hasattr(event, "group_id") else None
    session_id = None
    try:
        driver = _get_driver()
        memory = getattr(driver, "_agent_memory", None)
        if memory is not None:
            session_id = await memory.resolve_session(user_id, group_id)
            if hasattr(memory, "messages") and hasattr(memory, "sessions"):
                # 只清消息、保留 session 映射：与 PG 实现一致，且让按会话作用域保存的
                # 长期记忆（facts.session_id）在 /reset 后仍可召回
                memory.messages.pop(session_id, None)
            elif hasattr(memory, "pool"):
                async with memory.pool.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM messages WHERE session_id=$1",
                        int(session_id),
                    )
    except Exception:
        logger.exception("reset session failed")
    await reset.finish(f"会话已重置（session={session_id}）")


help_cmd = on_command("aihelp", aliases={"agenthelp", "帮助"}, priority=5, block=True)


@help_cmd.handle()
async def handle_help(event: MessageEvent):
    await help_cmd.finish(
        "指令：\n/reset 重置会话\n/status 查看状态\n/skills 查看可用 skill\n"
        "/skill catalog 查看可安装 skill 目录\n/skill install <name> 从目录安装 skill\n"
        "/skill uninstall <name> 卸载 skill\n"
        "/kb search <关键词> 检索公共知识库（/kb help 看全部）\n"
        "群内发 ai + 内容 或 @我 即可对话\n私聊直接发消息即可。"
    )


status = on_command("status", aliases={"状态"}, priority=5, block=True)


def _build_status_lines() -> list[str]:
    """P2-7：/status 反映真实运行状态，而非硬编码文案。"""
    driver = _get_driver()
    memory = getattr(driver, "_agent_memory", None)
    engine = getattr(driver, "_agent_engine", None)
    pm = getattr(driver, "_agent_persona_manager", None)

    backend = type(memory).__name__ if memory is not None else "未初始化"
    model = os.getenv("LLM_MODEL", "step-1-flash")
    if engine is not None and getattr(engine, "skills", None) is not None:
        skill_count = len(engine.skills.skills)
    else:
        skill_count = 0

    lines = [
        "agent-demo 运行状态：",
        f"记忆后端：{backend}",
        f"模型：{model}",
        f"已装 skill：{skill_count} 个",
    ]
    try:
        from agentcore.budget import get_budget

        b = get_budget()
        day = b.today()
        line = f"今日 LLM 用量：{day['total']:,} tokens（对话 {day['chat_requests']} 次）"
        if b.daily_tokens > 0:
            line += f" / 预算 {b.daily_tokens:,}（{'硬闸' if b.enforce else '软'}）"
        cost = b.estimate_cost()
        if cost is not None:
            line += f" ≈ {cost:.2f} 元"
        lines.append(line)
        # L13/L5：embedding 用量此前只落盘、无展示出口，成本估算也只看 chat
        if day["embedding_requests"]:
            lines.append(
                f"今日 embedding 用量：{day['embedding_tokens']:,} tokens"
                f"（{day['embedding_requests']} 次，不计入对话预算）"
            )
    except Exception:
        logger.warning("status: 预算信息读取失败", exc_info=True)
    if pm is not None:
        try:
            default = pm.default()
            lines.append(f"默认人格：{default.name if default else '（无）'}")
        except Exception:
            lines.append("默认人格：（读取失败）")
    return lines


@status.handle()
async def handle_status(event: MessageEvent):
    if not is_allowed(event):
        await status.finish("无权限")
    await status.finish("\n".join(_build_status_lines()))


skills_cmd = on_command("skills", aliases={"技能列表", "可用技能"}, priority=5, block=True)


@skills_cmd.handle()
async def handle_skills(event: MessageEvent):
    if not is_allowed(event):
        await skills_cmd.finish("无权限")

    user_id = str(event.get_user_id())
    group_id = str(event.group_id) if hasattr(event, "group_id") else None

    lines = ["可用 skill："]
    reg = _live_registry()
    for name in sorted(reg.skills):
        allowed = reg.is_allowed(name, user_id, group_id)
        lines.append(f"- {name}: {'✅' if allowed else '❌'}")

    await skills_cmd.finish("\n".join(lines))


catalog_cmd = on_command("skillcatalog", aliases={"skill catalog", "技能目录"}, priority=5, block=True)


@catalog_cmd.handle()
async def handle_catalog(event: MessageEvent):
    if not is_allowed(event):
        await catalog_cmd.finish("无权限")
    if not CATALOG:
        await catalog_cmd.finish("技能目录为空。")
    lines = ["可安装 skill："]
    for name, manifest in CATALOG.items():
        lines.append(f"- {name}: {manifest.description} ({manifest.type})")
    await catalog_cmd.finish("\n".join(lines))


install_cmd = on_command("skillinstall", aliases={"skill install", "安装技能"}, priority=5, block=True)


@install_cmd.handle()
async def handle_install(event: MessageEvent):
    if not is_allowed(event):
        await install_cmd.finish("无权限")
    # L25：安装/卸载是全局变更，仅 superuser（对齐 /kb 变更类命令）
    if not is_superuser(str(event.get_user_id())):
        await install_cmd.finish("只有管理员能安装技能。")

    args = str(event.get_message()).strip()
    parts = args.split()
    if not parts:
        await install_cmd.finish("用法：/skill install <name>")
        return

    name = parts[-1].strip().lstrip("@")
    manifest = CATALOG.get(name)
    if not manifest:
        await install_cmd.finish(f"未找到 skill: {name}\n用 /skill catalog 查看可安装列表。")
        return

    installer = _get_installer(event)
    if installer.get(name):
        await install_cmd.finish(f"skill 已安装：{name}")
        return

    installer.install(manifest)
    _live_registry().install(manifest)
    await install_cmd.finish(f"已安装 skill：{name}\n类型：{manifest.type}\n描述：{manifest.description}")


uninstall_cmd = on_command("skilluninstall", aliases={"skill uninstall", "卸载技能"}, priority=5, block=True)


@uninstall_cmd.handle()
async def handle_uninstall(event: MessageEvent):
    if not is_allowed(event):
        await uninstall_cmd.finish("无权限")
    # L25：安装/卸载是全局变更，仅 superuser（对齐 /kb 变更类命令）
    if not is_superuser(str(event.get_user_id())):
        await uninstall_cmd.finish("只有管理员能卸载技能。")

    args = str(event.get_message()).strip()
    parts = args.split()
    if not parts:
        await uninstall_cmd.finish("用法：/skill uninstall <name>")
        return

    name = parts[-1].strip().lstrip("@")
    installer = _get_installer(event)
    if not installer.uninstall(name):
        await uninstall_cmd.finish(f"skill 未安装：{name}")
        return
    _live_registry().uninstall(name)
    await uninstall_cmd.finish(f"已卸载 skill：{name}")


info_cmd = on_command("skillinfo", aliases={"skill info", "技能信息"}, priority=5, block=True)


@info_cmd.handle()
async def handle_info(event: MessageEvent):
    if not is_allowed(event):
        await info_cmd.finish("无权限")

    args = str(event.get_message()).strip()
    parts = args.split()
    if not parts:
        await info_cmd.finish("用法：/skill info <name>")
        return

    name = parts[-1].strip().lstrip("@")
    installer = _get_installer(event)
    manifest = installer.get(name)
    if not manifest:
        await info_cmd.finish(f"skill 未安装：{name}")
        return

    lines = [
        f"名称：{manifest.name}",
        f"类型：{manifest.type}",
        f"权限：{manifest.permission}",
        f"描述：{manifest.description}",
    ]
    if manifest.parameters:
        lines.append("参数：")
        for p in manifest.parameters:
            lines.append(f"- {p.get('name')}: {p.get('description', '')}")
    await info_cmd.finish("\n".join(lines))


def _get_installer(event: MessageEvent) -> SkillInstaller:
    skills_dir = Path(os.getenv("AGENT_SKILLS_DIR", DEFAULT_SKILLS_DIR))
    return SkillInstaller(skills_dir=skills_dir)


# ============================================================
#  公共知识库（M5）：/kb list|stats|search|add|file|forget|digest|samples
# ============================================================
kb_cmd = on_command("kb", aliases={"知识库"}, priority=5, block=True)

_KB_USAGE = (
    "知识库指令：\n"
    "/kb list [n]            查看最近的来源\n"
    "/kb stats               规模统计\n"
    "/kb search <关键词>      语义检索（任何有权限用户可用）\n"
    "/kb add <标题>|<正文>    投喂一段资料（管理员）\n"
    "/kb file <工作区路径>     摄取工作区里的文本文件（管理员）\n"
    "/kb samples             后台导入 data/kb_samples 新文档（管理员）\n"
    "/kb forget <来源id>      删除一个来源（管理员）\n"
    "/kb digest              立即执行一次「记忆蒸馏」（管理员）"
)


def _get_kb():
    from agentcore.rag.service import KnowledgeBase

    kb = getattr(_get_driver(), "_agent_kb", None)
    if kb is None or not isinstance(kb, KnowledgeBase):
        return None
    return kb


_KB_ACTIONS = {"help", "stats", "search", "list", "add", "file", "forget", "digest", "samples"}


def parse_kb_cmd(raw: str) -> tuple[str, str]:
    """解析 /kb 子命令，返回 (action, argument)。"""
    text = (raw or "").strip()
    text = re.sub(r"^[/!！]?(kb|知识库)\s*", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return "help", ""
    parts = text.split(maxsplit=1)
    action = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    aliases = {"ls": "list", "stat": "stats", "find": "search", "rm": "forget", "del": "forget"}
    action = aliases.get(action, action)
    if action not in _KB_ACTIONS:
        # 不是已知子命令：默认整条内容作为搜索关键词
        return "search", text
    return action, arg


# 样例语料目录：用户把新文档丢进 data/kb_samples 后用 /kb samples 入库
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
KB_SAMPLES_DIR = _PROJECT_ROOT / "data" / "kb_samples"

# 后台导入：同一时间只允许一个任务；进度放在模块级字典供 /kb samples 查询
_SAMPLES_LOCK = asyncio.Lock()
_SAMPLES_STATE: dict = {}


async def _plan_samples(kb, samples_dir: Path) -> dict:
    """预检：按**内容指纹**判重、按 stat 判超限、按 location 判僵尸。

    评审 REVIEW-bbd8913..f6dffcc.md 的 M5：此前只按文件名判重，语料改过也不会
    重新入库。现在同名文件会比较 sha256——内容未变才跳过，内容变了单独列出
    （由脚本 ``--replace`` 处理，命令侧只提示不擅自删数据）。
    """
    from agentcore.rag.ingest import MAX_INGEST_BYTES, content_digest

    if not samples_dir.is_dir():
        return {"error": f"样例目录不存在：{samples_dir}"}
    files = sorted(samples_dir.glob("*.md"))
    if not files:
        return {"error": f"目录里没有 .md 文件：{samples_dir}"}
    sources = await kb.list_sources(limit=1000)
    by_name = {s.get("name"): s for s in sources if s.get("name")}
    new_files: list[Path] = []
    duplicated: list[str] = []
    changed: list[str] = []
    oversized: list[str] = []
    for p in files:
        old = by_name.get(p.name)
        if old is not None:
            text = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
            old_digest = ((old.get("meta") or {}) or {}).get("sha256")
            if old_digest and old_digest == content_digest(text):
                duplicated.append(p.name)
            else:
                # 内容已变，或历史存量没有指纹；均需 --replace 才会更新
                changed.append(p.name)
        elif p.stat().st_size > MAX_INGEST_BYTES:
            oversized.append(p.name)
        else:
            new_files.append(p)
    return {"new": new_files, "dup": duplicated, "changed": changed, "oversized": oversized}


def _samples_progress() -> str:
    st = _SAMPLES_STATE
    if not st.get("running"):
        return "当前没有后台导入任务。"
    finished = st["done"] + st["failed"]
    line = f"后台导入进行中：{finished}/{st['total']}（新增 {st['done']} / 失败 {st['failed']}）"
    if st.get("current"):
        line += f"，当前：{st['current']}"
    return line


def _samples_summary(state: dict) -> str:
    lines = [f"样例导入完成：新增 {state['done']} / 失败 {state['failed']}"]
    if state["failed_names"]:
        lines.append("失败：" + "；".join(state["failed_names"]))
    if state.get("dropped"):
        lines.append(
            f"⚠ 因单来源块数上限丢弃 {state['dropped']} 块（尾部内容未入库；"
            "可用 AGENT_KB_MAX_CHUNKS_PER_SOURCE 提高上限后重灌）"
        )
    if state["dup"]:
        lines.append("同名内容未变（跳过）：" + "、".join(state["dup"]))
    if state.get("changed"):
        lines.append(
            "同名但内容已变（未自动替换）："
            + "、".join(state["changed"])
            + "；如需更新请执行 scripts/ingest_kb_samples.py --replace"
        )
    if state["oversized"]:
        lines.append("超过 2MB 上限：" + "、".join(state["oversized"]))
    return "\n".join(lines)


async def _run_samples_job(kb, new_files: list[Path], notify) -> None:
    """后台任务：逐文件导入，每个文件一个事务（失败仅跳过该文件，重跑自动续传）。"""
    state = _SAMPLES_STATE
    try:
        for p in new_files:
            state["current"] = p.name
            try:
                result = await kb.add_file(str(p), kind="sample")
                state["done"] += 1
                state["dropped"] = state.get("dropped", 0) + int(result.get("dropped") or 0)
                logger.info(
                    "kb samples: %s -> %s 块（切出 %s，丢弃 %s）",
                    p.name, result["chunks"], result.get("chunks_total"), result.get("dropped"),
                )
            except Exception as e:
                state["failed"] += 1
                state["failed_names"].append(f"{p.name}（{_truncate(str(e), 80)}）")
                logger.warning("kb samples: ingest %s failed: %s", p.name, e)
            finally:
                state["current"] = ""
    finally:
        state["running"] = False
        state["current"] = ""
        _SAMPLES_LOCK.release()
    if notify:
        try:
            await notify(_samples_summary(state))
        except Exception:
            logger.warning("kb samples: notify failed", exc_info=True)


async def _start_samples_job(kb, samples_dir: Path, notify) -> str:
    """预检并后台启动样例导入；已在运行则返回进度。返回值是给用户的即时回复。"""
    if _SAMPLES_LOCK.locked():
        return "已有后台导入任务在进行中：\n" + _samples_progress()
    plan = await _plan_samples(kb, samples_dir)
    if "error" in plan:
        return plan["error"]
    if not plan["new"]:
        lines = ["没有需要导入的新文档。"]
        if plan["dup"]:
            lines.append("同名内容未变（跳过）：" + "、".join(plan["dup"]))
        if plan["changed"]:
            lines.append(
                "同名但内容已变（未自动替换）："
                + "、".join(plan["changed"])
                + "；如需更新请执行 scripts/ingest_kb_samples.py --replace"
            )
        if plan["oversized"]:
            lines.append("超过 2MB 上限：" + "、".join(plan["oversized"]))
        return "\n".join(lines)
    # 预检期间可能已被抢占：二次检查与 acquire 之间无 await，事件循环内原子
    if _SAMPLES_LOCK.locked():
        return "已有后台导入任务在进行中：\n" + _samples_progress()
    await _SAMPLES_LOCK.acquire()

    state = _SAMPLES_STATE
    state.update({
        "running": True,
        "total": len(plan["new"]),
        "done": 0,
        "failed": 0,
        "dropped": 0,
        "current": "",
        "failed_names": [],
        "dup": plan["dup"],
        "changed": plan["changed"],
        "oversized": plan["oversized"],
    })
    state["task"] = asyncio.create_task(_run_samples_job(kb, plan["new"], notify))

    size_mb = sum(p.stat().st_size for p in plan["new"]) / 1048576
    lines = [
        f"已在后台开始导入 {len(plan['new'])} 个新文档（约 {size_mb:.1f}MB），"
        "完成后会私聊通知你；进度可再发 /kb samples 查看。"
    ]
    if plan["dup"]:
        lines.append("同名内容未变（跳过）：" + "、".join(plan["dup"]))
    if plan["changed"]:
        lines.append(
            "同名但内容已变（未自动替换）："
            + "、".join(plan["changed"])
            + "；如需更新请执行 scripts/ingest_kb_samples.py --replace"
        )
    if plan["oversized"]:
        lines.append("超过 2MB 上限（跳过）：" + "、".join(plan["oversized"]))
    return "\n".join(lines)


@kb_cmd.handle()
async def handle_kb(event: MessageEvent):
    if not is_allowed(event):
        await kb_cmd.finish("无权限")

    kb = _get_kb()
    if kb is None:
        await kb_cmd.finish("知识库未初始化（检查 config.yaml 的 rag 段）。")

    action, arg = parse_kb_cmd(str(event.get_message()))
    user_id = str(event.get_user_id())
    is_admin = is_superuser(user_id)

    # L7：enabled=0 是「整体关闭」，写操作给出明确提示而不是等到摄取时抛通用错误
    if not getattr(kb, "enabled", True) and action in ("add", "file", "samples"):
        await kb_cmd.finish("知识库已关闭（AGENT_KB_ENABLED=0），写入类操作不可用。")

    try:
        if action in ("help", ""):
            await kb_cmd.finish(_KB_USAGE)

        if action == "stats":
            stats = await kb.stats()
            cfg = kb.describe()
            await kb_cmd.finish(
                f"公共知识库：{stats['sources']} 个来源 / {stats['chunks']} 个知识块\n"
                f"状态：{'启用' if cfg['enabled'] else '停用'}；"
                f"检索 top_k={cfg['top_k']}、阈值={cfg['threshold']}\n"
                f"每日蒸馏：{cfg['digest_cron']}（embedding {cfg['embedding']}）"
            )

        if action == "search":
            if not arg:
                await kb_cmd.finish("用法：/kb search <关键词>")
            hits = await kb.retrieve(arg)
            if not hits:
                await kb_cmd.finish("没有检索到相关公共知识。")
            lines = [f"检索「{arg}」命中 {len(hits)} 条："]
            for i, h in enumerate(hits, 1):
                chunk = (h.get("chunk") or "").strip().replace("\n", " ")
                lines.append(f"{i}. [{h.get('score', 0):.3f}] {_truncate(chunk, 120)}")
            await kb_cmd.finish("\n".join(lines))

        if action == "list":
            limit = int(arg) if arg.isdigit() else 10
            sources = await kb.list_sources(limit=limit)
            if not sources:
                await kb_cmd.finish("知识库还是空的。可以用 /kb add 投喂，或等每日蒸馏。")
            lines = [f"最近 {len(sources)} 个来源："]
            for s in sources:
                when = time.strftime("%m-%d %H:%M", time.localtime(s.get("created_at") or 0))
                lines.append(f"- #{s['id']} [{s['kind']}] {s['name']}（{s['chunks']} 块，{when}）")
            await kb_cmd.finish("\n".join(lines))

        if not is_admin:
            await kb_cmd.finish("只有管理员能投喂/删除知识或触发蒸馏。")

        if action == "add":
            if "|" not in arg:
                await kb_cmd.finish("用法：/kb add <标题>|<正文>")
            title, body = (x.strip() for x in arg.split("|", 1))
            if not body:
                await kb_cmd.finish("正文为空。")
            result = await kb.add_text(body, name=title or "管理员投喂")
            await kb_cmd.finish(
                f"已入库：{result['chunks']} 个知识块（来源 #{result['source_id']}）。\n"
                "提示：入库内容会对所有会话可见，请勿包含个人身份信息。"
            )

        if action == "file":
            if not arg:
                await kb_cmd.finish("用法：/kb file <工作区内的相对路径>")
            from agentcore.workspace.fs import WorkspaceFS
            from agentcore.workspace.utils import workspace_root

            fs = WorkspaceFS(workspace_root())
            path = fs.resolve(arg)  # 越界会抛 ValueError
            result = await kb.add_file(str(path))
            await kb_cmd.finish(f"已摄取文件：{result['chunks']} 个知识块（来源 #{result['source_id']}）。")

        if action == "forget":
            if not arg.isdigit():
                await kb_cmd.finish("用法：/kb forget <来源id>（用 /kb list 查看）")
            deleted = await kb.delete_source(arg)
            await kb_cmd.finish(f"已删除来源 #{arg}，同时移除 {deleted} 个知识块。")

        if action == "digest":
            result = await kb.digest()
            from agentcore.rag.distill import summarize

            await kb_cmd.finish(summarize(result))

        if action == "samples":

            async def _notify(text: str) -> None:
                from nonebot import get_bot

                try:
                    bot = get_bot(event.self_id)
                    await bot.send_private_msg(user_id=int(user_id), message=text)
                except Exception:
                    logger.warning("kb samples: notify failed", exc_info=True)

            await kb_cmd.finish(await _start_samples_job(kb, KB_SAMPLES_DIR, _notify))

        await kb_cmd.finish(_KB_USAGE)
    except ValueError as e:
        await kb_cmd.finish(f"拒绝：{e}")
    except FinishedException:
        raise
    except Exception:
        logger.exception("kb cmd failed")
        await kb_cmd.finish("知识库操作出错，请稍后再试。")


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


# ============================================================
#  人格系统
# ============================================================
persona_cmd = on_command(
    "persona",
    aliases={"personas", "人格", "人设"},
    priority=5,
    block=True,
)


def _persona_objs():
    driver = _get_driver()
    memory = getattr(driver, "_agent_memory", None)
    manager = getattr(driver, "_agent_persona_manager", None)
    return memory, manager


def _persona_list_lines(manager) -> list[str]:
    lines = ["可用人格："]
    for p in manager.list():
        mark = "⭐" if p.default else " "
        lines.append(f"{mark} {p.name}：{p.description or '（无描述）'}")
    return lines


@persona_cmd.handle()
async def handle_persona(event: MessageEvent):
    if not is_allowed(event):
        await persona_cmd.finish("无权限")
    memory, manager = _persona_objs()
    if memory is None or manager is None:
        await persona_cmd.finish("人格系统未初始化。")

    user_id = str(event.get_user_id())
    action, name = parse_persona_cmd(str(event.get_message()))

    try:
        if action == "list":
            default = manager.default()
            current = await memory.get_user_persona(user_id)
            cur_name = current or (default.name if default else "（无）")
            lines = [f"当前人格：{cur_name}"]
            lines += _persona_list_lines(manager)
            lines.append("用法：/persona list 查看；/persona use <名字> 或直接 /persona <名字> 切换；/persona reset 恢复默认")
            await persona_cmd.finish("\n".join(lines))

        if action == "reset":
            await memory.set_user_persona(user_id, None)
            await persona_cmd.finish("已恢复默认人格。")

        # action == "use"
        persona = manager.get(name) if name else None
        if persona:
            await memory.set_user_persona(user_id, persona.name)
            await persona_cmd.finish(
                f"已切换人格：{persona.name}\n{persona.description or ''}"
            )

        lines = [f"未找到人格：{name or '（空）'}"] + _persona_list_lines(manager)
        await persona_cmd.finish("\n".join(lines))
    except FinishedException:
        raise  # NoneBot 正常终止信号，不视为错误
    except Exception:
        logger.exception("persona cmd failed")
        await persona_cmd.finish("人格命令执行出错，请稍后再试。")


# ============================================================
#  工作区删除二次确认：用户回复「确认删除 XXXX」
# ============================================================
_CONFIRM_PATTERN = re.compile(r"^确认删除\s*([0-9A-Z]{6,})$")


def _confirm_delete_rule(event: MessageEvent) -> bool:
    # 只对有权限的用户生效：未授权聊天不响应也不拦截（原实现会回「无权限」并吞消息）
    if not is_allowed(event):
        return False
    return bool(_CONFIRM_PATTERN.match(str(event.get_message()).strip()))


_confirm_matcher = on_message(rule=_confirm_delete_rule, priority=8, block=True)


@_confirm_matcher.handle()
async def handle_confirm_delete(event: MessageEvent):
    if not is_allowed(event):
        await _confirm_matcher.finish("无权限")
    from agentcore.workspace.confirm import get_gate
    from agentcore.workspace.fs import WorkspaceFS

    user_id = str(event.get_user_id())
    m = _CONFIRM_PATTERN.match(str(event.get_message()).strip())
    code = m.group(1) if m else ""
    path = await get_gate().confirm(user_id, code)
    if not path:
        await _confirm_matcher.finish("确认码无效或已过期（删除未执行）。")
    try:
        from agentcore.workspace.utils import workspace_root

        fs = WorkspaceFS(workspace_root())
        result = await fs.delete_abs(path)
    except Exception:
        logger.exception("confirm delete failed")
        result = "删除失败，请稍后重试。"
    await _confirm_matcher.finish(result)
