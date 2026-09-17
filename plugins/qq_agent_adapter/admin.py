from __future__ import annotations

import asyncio
import difflib
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


def _not_self_message(event: MessageEvent) -> bool:
    """排除 bot 自身消息（NapCat 上报自身消息时回传的事件，user_id == self_id）。

    评审 L-2：与 matcher._is_self_message 同一判据；matcher 无法反向 import，
    此处独立实现（判据两行，不值得引入模块耦合）。
    """
    self_id = str(getattr(event, "self_id", "") or "")
    return not (self_id and str(event.get_user_id()) == self_id)


reset = on_command(
    "reset", aliases={"重置"}, priority=5, block=True, rule=_not_self_message
)


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


help_cmd = on_command(
    "aihelp",
    aliases={"agenthelp", "帮助"},
    priority=5,
    block=True,
    rule=_not_self_message,
)


_HELP_TEXT = (
    "指令：\n/reset 重置会话\n/status 查看状态\n/skills 查看可用 skill\n"
    "/skill catalog 查看可安装 skill 目录\n/skill install <name> 从目录安装 skill\n"
    "/skill uninstall <name> 卸载 skill\n"
    "/kb search <关键词> 检索公共知识库（/kb help 看全部）\n"
    "群内发 ai + 内容 或 @我 即可对话\n私聊直接发消息即可。"
)


@help_cmd.handle()
async def handle_help(event: MessageEvent):
    # 图片菜单：Pillow 渲染失败 / 未开启时自动退回文本
    try:
        from .help_render import render_help_image

        png = render_help_image()
    except Exception:
        logger.exception("help menu render failed")
        png = None
    if png:
        from nonebot.adapters.onebot.v11 import MessageSegment

        await help_cmd.finish(MessageSegment.image(png))
    await help_cmd.finish(_HELP_TEXT)


status = on_command(
    "status", aliases={"状态"}, priority=5, block=True, rule=_not_self_message
)


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
        line = (
            f"今日 LLM 用量：{day['total']:,} tokens（对话 {day['chat_requests']} 次）"
        )
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


skills_cmd = on_command(
    "skills",
    aliases={"技能列表", "可用技能"},
    priority=5,
    block=True,
    rule=_not_self_message,
)


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


catalog_cmd = on_command(
    "skillcatalog",
    aliases={"skill catalog", "技能目录"},
    priority=5,
    block=True,
    rule=_not_self_message,
)


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


install_cmd = on_command(
    "skillinstall",
    aliases={"skill install", "安装技能"},
    priority=5,
    block=True,
    rule=_not_self_message,
)


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
        await install_cmd.finish(
            f"未找到 skill: {name}\n用 /skill catalog 查看可安装列表。"
        )
        return

    installer = _get_installer(event)
    if installer.get(name):
        await install_cmd.finish(f"skill 已安装：{name}")
        return

    installer.install(manifest)
    _live_registry().install(manifest)
    await install_cmd.finish(
        f"已安装 skill：{name}\n类型：{manifest.type}\n描述：{manifest.description}"
    )


uninstall_cmd = on_command(
    "skilluninstall",
    aliases={"skill uninstall", "卸载技能"},
    priority=5,
    block=True,
    rule=_not_self_message,
)


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


info_cmd = on_command(
    "skillinfo",
    aliases={"skill info", "技能信息"},
    priority=5,
    block=True,
    rule=_not_self_message,
)


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
kb_cmd = on_command(
    "kb", aliases={"知识库"}, priority=5, block=True, rule=_not_self_message
)

_KB_USAGE = (
    "知识库指令：\n"
    "/kb list [n]            查看最近的来源\n"
    "/kb stats               规模统计\n"
    "/kb search <关键词>      语义检索（任何有权限用户可用）\n"
    "/kb add <标题>|<正文>    投喂一段资料（管理员）\n"
    "/kb file <工作区路径>     摄取工作区里的文本文件（管理员）\n"
    "/kb samples             后台导入 data/kb_samples 新文档（管理员）\n"
    "/kb samples confirm      体积超阈值时确认导入（管理员）\n"
    "/kb forget <来源id>      删除一个来源（管理员）\n"
    "/kb digest              立即执行一次「记忆蒸馏」（管理员）\n"
    "提示：大文件（>2MB 或切块数超上限）会自动切块到同目录同名子目录，"
    "逐块入库且源文件保留，无需手动切块。\n"
    "提示：/kb samples 会先估算体积，超过阈值时要求再发一次 "
    "/kb samples confirm 才启动。\n"
    "提示：/kb 后面跟的不是上面这些子命令时，会**按搜索关键词**处理——"
    "想导入文档请确认拼写为 /kb samples。"
)


def _get_kb():
    from agentcore.rag.service import KnowledgeBase

    kb = getattr(_get_driver(), "_agent_kb", None)
    if kb is None or not isinstance(kb, KnowledgeBase):
        return None
    return kb


_KB_ACTIONS = {
    "help",
    "stats",
    "search",
    "list",
    "add",
    "file",
    "forget",
    "digest",
    "samples",
}


def parse_kb_cmd(raw: str) -> tuple[str, str]:
    """解析 /kb 子命令，返回 (action, argument)。

    分隔符容忍空白与斜杠混用（``/kb samples``、``/kb/samples``、``/kb /samples``
    等价）；**不认识的子命令会退化成搜索关键词**（见下方注释与
    `_kb_search_miss_message`——拼错时靠那里的提示兜底，而不是静默搜不到）。
    """
    text = (raw or "").strip()
    text = re.sub(r"^[/!！]?(kb|知识库)[\s/]*", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return "help", ""
    parts = text.split(maxsplit=1)
    action = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    aliases = {
        "ls": "list",
        "stat": "stats",
        "find": "search",
        "rm": "forget",
        "del": "forget",
    }
    action = aliases.get(action, action)
    if action not in _KB_ACTIONS:
        # 不是已知子命令：默认整条内容作为搜索关键词（保留「/kb 白名单 校验」
        # 这种免 search 的用法）。拼错子命令的可见性由 _kb_search_miss_message 兜。
        return "search", text
    return action, arg


def _suggest_kb_action(token: str) -> str | None:
    """把疑似拼错的子命令映射到最接近的已知子命令；没有接近的返回 None。

    用途：「/kb samoles」这类输入会走「未知子命令 → 搜索」的兜底路径并返回
    「没有检索到相关公共知识」，用户会以为语料没进去。这里给出「你是不是想用
    /kb samples」的提示，把静默失败变成可自纠的错误。
    """
    if not token or token.lower() in _KB_ACTIONS:
        return None
    matches = difflib.get_close_matches(
        token.lower(), sorted(_KB_ACTIONS), n=1, cutoff=0.6
    )
    return matches[0] if matches else None


def _kb_search_miss_message(query: str) -> str:
    """搜索零命中时的回复；首词疑似拼错的子命令时补一句提示。"""
    lines = ["没有检索到相关公共知识。"]
    tokens = (query or "").split()
    hint = _suggest_kb_action(tokens[0]) if tokens else None
    if hint:
        lines.append(
            f"提示：{tokens[0]!r} 不是知识库子命令，你是不是想用 /kb {hint}？"
            "（/kb help 查看全部命令；/kb 后面跟未知词会按搜索关键词处理）"
        )
    return "\n".join(lines)


# 样例语料目录：用户把新文档丢进 data/kb_samples 后用 /kb samples 入库
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
KB_SAMPLES_DIR = _PROJECT_ROOT / "data" / "kb_samples"

# 后台导入：同一时间只允许一个任务；进度放在模块级字典供 /kb samples 查询
_SAMPLES_LOCK = asyncio.Lock()
_SAMPLES_STATE: dict = {}

# 体积预检阈值（常量与解析都在 agentcore，脚本侧共用同一套默认值）
_SAMPLES_CONFIRM_WORDS = {"confirm", "yes", "y", "确认", "--yes"}


def _samples_confirm_gate(plan: dict) -> str | None:
    """导入前的体积预检：超阈值返回「需确认」文案，否则返回 None。

    只做算术与文案，不落盘也不起任务——用户回 ``/kb samples confirm`` 才真正开始。
    """
    from agentcore.rag.ingest import (
        DEFAULT_SAMPLES_CONFIRM_CHUNKS,
        DEFAULT_SAMPLES_CONFIRM_MB,
        SAMPLES_CONFIRM_CHUNKS_ENV,
        SAMPLES_CONFIRM_MB_ENV,
        confirm_threshold,
    )

    total_bytes = int(plan.get("total_bytes") or 0)
    total_chunks = int(plan.get("total_chunks") or 0)
    limit_mb = confirm_threshold(SAMPLES_CONFIRM_MB_ENV, DEFAULT_SAMPLES_CONFIRM_MB)
    limit_chunks = confirm_threshold(
        SAMPLES_CONFIRM_CHUNKS_ENV, DEFAULT_SAMPLES_CONFIRM_CHUNKS
    )
    over_mb = limit_mb > 0 and total_bytes > limit_mb * 1024 * 1024
    over_chunks = limit_chunks > 0 and total_chunks > limit_chunks
    if not (over_mb or over_chunks):
        return None
    exceeded = [
        label
        for label, over in (
            (f"{limit_mb}MB", over_mb),
            (f"{limit_chunks} 块", over_chunks),
        )
        if over
    ]
    return "\n".join(
        [
            f"这批新语料约 {total_bytes / 1048576:.1f}MB / {total_chunks} 个知识块，"
            f"超过预检阈值（{'、'.join(exceeded)}），**暂未启动**。",
            "本地 CPU embedding 每块约数秒，几万块量级要跑几十小时；"
            "换 OpenAI 兼容的云端 embedding 通常几分钟完成。",
            "确认导入请发送：/kb samples confirm",
            f"（阈值可用 {SAMPLES_CONFIRM_MB_ENV} / {SAMPLES_CONFIRM_CHUNKS_ENV} "
            "调整，设 0 关闭对应维度）",
        ]
    )


async def _plan_samples(kb, samples_dir: Path, *, materialize: bool = False) -> dict:
    """预检：按**内容指纹**判重、按 stat 判超限、按 location 判僵尸。

    评审 REVIEW-bbd8913..f6dffcc.md 的 M5：此前只按文件名判重，语料改过也不会
    重新入库。现在同名文件会比较 sha256——内容未变才跳过，内容变了单独列出
    （由脚本 ``--replace`` 处理，命令侧只提示不擅自删数据）。
    M7（REVIEW-c472e56..733f57e）：同名多条（历史遗留）时对**全部**同名来源做
    指纹比对——只看最新一条会被被遮蔽的旧来源骗成「未变跳过」。
    另：切块 → 整体（源文件变小或上限调大）的反向迁移也算「需 --replace」，
    否则整体来源会与残留的 `文件名/00N.md` 旧块来源同时存在（检索重复）。

    大文件（>2MB 或切块数超上限）在这里**自动切块**到同目录 ``<stem>/``，
    每个块文件成为一个独立导入单元（来源名 ``文件名/块文件名``），源文件保留。
    L8（REVIEW-c472e56..733f57e）：``materialize=False``（默认，预检用）时只
    计算份级指纹、**不落盘**——预检不再在不启动导入的分支里留下孤儿块文件；
    真正启动导入前用 ``materialize=True`` 重出一次计划。
    """
    from agentcore.rag.ingest import scan_samples_units

    scan = await asyncio.to_thread(
        scan_samples_units,
        samples_dir,
        # 用 kb 的生效上限（config.yaml / env 已解析），否则切块粒度会退回模块
        # 默认 200，与 `rag.max_chunks_per_source: 1000` 不一致——同一份语料会
        # 多出约 5 倍的切块文件（实测：原神.md 21.9MB 切 103 份而非 21 份）
        max_chunks=getattr(kb, "max_chunks_per_source", None),
        materialize=materialize,
    )
    if scan["error"]:
        return {"error": scan["error"]}

    sources = await kb.list_sources(limit=100000)
    # list_sources 最新在前；同名多条全部保留，判重按「任一同名来源指纹一致」
    by_name: dict = {}
    for s in sources:
        if s.get("name"):
            by_name.setdefault(s["name"], []).append(s)

    new_units: list[dict] = []
    duplicated: list[str] = []
    changed: list[str] = []
    for item in scan["sources"]:
        units = item["units"]
        missing = [u for u in units if u["name"] not in by_name]
        changed_units = [
            u
            for u in units
            if u["name"] in by_name
            and not any(
                ((s.get("meta") or {}) or {}).get("sha256") == u["sha256"]
                for s in by_name[u["name"]]
            )
        ]
        # 曾经作为整体导入过、现在改走切块：旧整体来源要 --replace 才会被替换
        stale_parent = bool(item["split"]) and item["name"] in by_name
        # 反向迁移：曾经切块、现在**不再**切块（源文件变小或上限调大）时，库里
        # 仍留着 `文件名/00N.md` 的旧块来源。此时源文件会被当成全新整体重新导入，
        # 旧块却无人处理 → 检索出现重复/过期片段。与 stale_parent 同样归入
        # 「需 --replace」，绝不擅自删数据。
        stale_parts = (not item["split"]) and any(
            name.startswith(f"{item['name']}/") for name in by_name
        )
        needs_replace = stale_parent or stale_parts
        if not missing and not changed_units and not needs_replace:
            duplicated.append(item["name"])
        elif not changed_units and not needs_replace and len(missing) == len(units):
            new_units.extend(units)
        else:
            changed.append(item["name"])
    return {
        "new": new_units,
        "dup": duplicated,
        "changed": changed,
        "oversized": scan["oversized"],
        "splits": scan["splits"],
        # 体积预检用：本次**将要新入库**的字节数与知识块数（不含 dup/changed）
        "total_bytes": sum(int(u.get("bytes") or 0) for u in new_units),
        "total_chunks": sum(int(u.get("chunks") or 0) for u in new_units),
    }


def _samples_progress() -> str:
    st = _SAMPLES_STATE
    if not st.get("running"):
        return "当前没有后台导入任务。"
    finished = st["done"] + st["failed"]
    line = f"后台导入进行中：{finished}/{st['total']}（新增 {st['done']} / 失败 {st['failed']}）"
    if st.get("current"):
        line += f"，当前：{st['current']}"
    if st.get("progress"):
        line += f"，{st['progress']}"
    return line


# 低于此块数的嵌入调用（聊天每轮的事实抽取等）不参与样本导入进度，避免把
# 「当前：xxx.md/001.md 嵌入 648/930」覆盖成「嵌入 1/1」
_EMBED_PROGRESS_MIN_TOTAL = 50


def note_embedding_progress(done: int, total: int) -> None:
    """嵌入进度回调：由 embedding 客户端在批量嵌入时**同步**调用。

    只在「有样本导入任务在跑」且总量够大时记录。动机：`ingest_text` 是**按切块
    原子提交**的（该份全部块嵌入完才写库），一个 930 块的切块要 40 多分钟才有一条
    日志/一次落库——用户完全无法区分「在慢慢跑」与「卡死」。
    """
    if total < _EMBED_PROGRESS_MIN_TOTAL:
        return
    state = _SAMPLES_STATE
    if not state.get("running"):
        return
    state["progress"] = f"嵌入 {done}/{total} 块（{done * 100 // total}%）"


def _oversized_note(names: list[str], *, skipped: bool = False) -> str:
    """超过**自动切块硬上限**的提示（小文件上限已被自动切块取代）。"""
    from agentcore.rag.ingest import MAX_SPLIT_SOURCE_BYTES

    limit_mb = MAX_SPLIT_SOURCE_BYTES // (1024 * 1024)
    suffix = "（跳过）" if skipped else ""
    return f"超过自动切块上限 {limit_mb}MB{suffix}：" + "、".join(names)


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
        lines.append(_oversized_note(state["oversized"]))
    return "\n".join(lines)


async def _run_samples_job(kb, units: list[dict], notify) -> None:
    """后台任务：逐**导入单元**导入，每个单元一个事务（失败仅跳过，重跑自动续传）。

    单元可能是源文件本身，也可能是大文件切块后的块文件（``unit["name"]`` 是写库
    用的来源名，切块时为 ``文件名/块文件名``）。
    """
    state = _SAMPLES_STATE
    try:
        for unit in units:
            state["current"] = unit["name"]
            state["progress"] = ""
            try:
                result = await kb.add_file(
                    str(unit["path"]), name=unit["name"], kind="sample"
                )
                state["done"] += 1
                state["dropped"] = state.get("dropped", 0) + int(
                    result.get("dropped") or 0
                )
                logger.info(
                    "kb samples: %s -> %s 块（切出 %s，丢弃 %s）",
                    unit["name"],
                    result["chunks"],
                    result.get("chunks_total"),
                    result.get("dropped"),
                )
            except Exception as e:
                state["failed"] += 1
                state["failed_names"].append(
                    f"{unit['name']}（{_truncate(_exc_brief(e), 120)}）"
                )
                # 用类型名+repr：httpx.ReadTimeout 之类的 str() 是空串，
                # 只打 `%s` 会得到「failed: 」这种没有原因的日志（已踩过）
                logger.warning(
                    "kb samples: ingest %s failed: %s", unit["name"], _exc_brief(e)
                )
            finally:
                state["current"] = ""
                state["progress"] = ""
    finally:
        state["running"] = False
        state["current"] = ""
        _SAMPLES_LOCK.release()
    if notify:
        try:
            await notify(_samples_summary(state))
        except Exception:
            logger.warning("kb samples: notify failed", exc_info=True)


async def _start_samples_job(
    kb, samples_dir: Path, notify, *, confirm: bool = False
) -> str:
    """预检并后台启动样例导入；已在运行则返回进度。返回值是给用户的即时回复。

    ``confirm``：用户已回 ``/kb samples confirm``。体积超过预检阈值时，未确认
    只返回预估与确认指引，**不起后台任务、不落盘切块**。
    """
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
            lines.append(_oversized_note(plan["oversized"], skipped=True))
        return "\n".join(lines)
    # 体积预检：先算清楚再问，避免起了后台任务几小时后才发现白跑
    if not confirm:
        gate = _samples_confirm_gate(plan)
        if gate is not None:
            return gate
    # 预检期间可能已被抢占：二次检查与 acquire 之间无 await，事件循环内原子
    if _SAMPLES_LOCK.locked():
        return "已有后台导入任务在进行中：\n" + _samples_progress()
    await _SAMPLES_LOCK.acquire()

    # L8：真正启动导入才落盘切块——预检（materialize=False）不留孤儿块文件
    plan = await _plan_samples(kb, samples_dir, materialize=True)
    if "error" in plan or not plan["new"]:
        _SAMPLES_LOCK.release()
        if "error" in plan:
            return plan["error"]
        return "没有需要导入的新文档。"

    state = _SAMPLES_STATE
    state.update(
        {
            "running": True,
            "total": len(plan["new"]),
            "done": 0,
            "failed": 0,
            "dropped": 0,
            "current": "",
            "progress": "",
            "failed_names": [],
            "dup": plan["dup"],
            "changed": plan["changed"],
            "oversized": plan["oversized"],
            "splits": plan.get("splits") or [],
        }
    )
    state["task"] = asyncio.create_task(_run_samples_job(kb, plan["new"], notify))

    size_mb = plan["total_bytes"] / 1048576
    lines = [
        f"已在后台开始导入 {len(plan['new'])} 个新文档"
        f"（约 {size_mb:.1f}MB / {plan['total_chunks']} 个知识块），"
        "完成后会私聊通知你；进度可再发 /kb samples 查看。"
    ]
    if plan.get("splits"):
        parts = sum(int(s["parts"]) for s in plan["splits"])
        lines.append(
            f"其中 {len(plan['splits'])} 个大文件已自动切块为 {parts} 份"
            "（切块文件在同目录同名子目录，源文件保留）"
        )
    if plan["dup"]:
        lines.append("同名内容未变（跳过）：" + "、".join(plan["dup"]))
    if plan["changed"]:
        lines.append(
            "同名但内容已变（未自动替换）："
            + "、".join(plan["changed"])
            + "；如需更新请执行 scripts/ingest_kb_samples.py --replace"
        )
    if plan["oversized"]:
        lines.append(_oversized_note(plan["oversized"], skipped=True))
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

    # L7/M3：enabled=0 是「整体关闭」，写操作（含删除）给出明确提示而不是等到底层抛异常
    if not getattr(kb, "enabled", True) and action in (
        "add",
        "file",
        "samples",
        "forget",
    ):
        await kb_cmd.finish(
            "知识库已关闭（AGENT_KB_ENABLED=0），写入/删除类操作不可用。"
        )

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
                await kb_cmd.finish(_kb_search_miss_message(arg))
            lines = [f"检索「{arg}」命中 {len(hits)} 条："]
            for i, h in enumerate(hits, 1):
                chunk = (h.get("chunk") or "").strip().replace("\n", " ")
                lines.append(f"{i}. [{h.get('score', 0):.3f}] {_truncate(chunk, 120)}")
            await kb_cmd.finish("\n".join(lines))

        if action == "list":
            limit = int(arg) if arg.isdigit() else 10
            sources = await kb.list_sources(limit=limit)
            if not sources:
                await kb_cmd.finish(
                    "知识库还是空的。可以用 /kb add 投喂，或等每日蒸馏。"
                )
            lines = [f"最近 {len(sources)} 个来源："]
            for s in sources:
                when = time.strftime(
                    "%m-%d %H:%M", time.localtime(s.get("created_at") or 0)
                )
                lines.append(
                    f"- #{s['id']} [{s['kind']}] {s['name']}（{s['chunks']} 块，{when}）"
                )
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
            result = await kb.add_file_smart(str(path))
            if result.get("split"):
                skipped = int(result.get("skipped") or 0)
                imported = int(result.get("imported") or 0)
                summary = f"文件较大，自动切块为 {result['parts']} 份："
                if skipped:
                    summary += f"本次新入库 {imported} 份、跳过未变 {skipped} 份"
                else:
                    summary += f"已逐份入库 {imported or result['parts']} 份"
                await kb_cmd.finish(
                    f"{summary}，共 {result['chunks']} 个知识块。\n"
                    f"切块文件目录：{result['dir']}（源文件保留；"
                    "重跑按内容指纹判重，不会重复入库）"
                )
            await kb_cmd.finish(
                f"已摄取文件：{result['chunks']} 个知识块（来源 #{result['source_id']}）。"
            )

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
                    # self_id 必须 str：OneBot 事件的 self_id 是 int，而 NoneBot
                    # 的 bots 字典以 str 为 key（int 直接索引恒 KeyError——
                    # /kb samples 的完成通知曾因此全部静默失败）
                    bot = get_bot(str(event.self_id))
                    await bot.send_private_msg(user_id=int(user_id), message=text)
                except Exception:
                    logger.warning("kb samples: notify failed", exc_info=True)

            # `/kb samples confirm`：越过体积预检；其余参数一律当作未确认
            confirmed = arg.strip().lower() in _SAMPLES_CONFIRM_WORDS
            await kb_cmd.finish(
                await _start_samples_job(kb, KB_SAMPLES_DIR, _notify, confirm=confirmed)
            )

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


def _exc_brief(exc: BaseException) -> str:
    """异常的一句话摘要（**带类型名**）。

    部分异常（`httpx.ReadTimeout`、裸 `TimeoutError` 等）的 ``str()`` 是**空串**：
    只打 ``str(e)`` 会得到「ingest xxx failed: 」这种没有原因的日志。类型名 +
    repr 才排得动。
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else f"{type(exc).__name__}: {exc!r}"


# ============================================================
#  人格系统
# ============================================================
persona_cmd = on_command(
    "persona",
    aliases={"personas", "人格", "人设"},
    priority=5,
    block=True,
    rule=_not_self_message,
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
            lines.append(
                "用法：/persona list 查看；/persona use <名字> 或直接 /persona <名字> 切换；/persona reset 恢复默认"
            )
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
    # 排除 bot 自身消息（评审 L-2）：确认门按 user_id 键控，自身消息虽然当前
    # 找不到待确认项，但明确过滤比依赖下游键控更稳（纵深防御）
    if not _not_self_message(event):
        return False
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
