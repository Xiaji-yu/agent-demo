from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from nonebot import on_command
from nonebot.exception import FinishedException
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from .acl import is_allowed
from . import _get_driver
from agentcore.skills.manifest import SkillManifest
from agentcore.skills.installer import SkillInstaller
from agentcore.skills.catalog import CATALOG
from agentcore.skills import registry as skill_registry

logger = logging.getLogger(__name__)


DEFAULT_SKILLS_DIR = Path("data/skills")


reset = on_command("reset", aliases={"重置"}, priority=5, block=True)


@reset.handle()
async def handle_reset(event: MessageEvent):
    if not is_allowed(event):
        await reset.finish("无权限")
    user_id = str(event.get_user_id())
    group_id = str(event.group_id) if hasattr(event, "group_id") else None
    session_id = None
    try:
        driver = _get_driver()
        memory = getattr(driver, "_agent_memory", None)
        if memory is not None:
            session_id = await memory.resolve_session(user_id, group_id)
            if hasattr(memory, "messages") and hasattr(memory, "sessions"):
                memory.messages.pop(session_id, None)
                memory.sessions = {k: v for k, v in memory.sessions.items() if v != session_id}
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
        "群内发 ai + 内容 或 @我 即可对话\n私聊直接发消息即可。"
    )


status = on_command("status", aliases={"状态"}, priority=5, block=True)


@status.handle()
async def handle_status(event: MessageEvent):
    if not is_allowed(event):
        await status.finish("无权限")
    await status.finish(
        "agent-demo M0-M2 骨架已就绪。\n"
        "当前记忆：内存模式（配置 DATABASE_URL 切 PG）\n"
        "Skill 系统：已启用（默认 public）\n"
        "LLM：读取 config.yaml + .env"
    )


skills_cmd = on_command("skills", aliases={"技能列表", "可用技能"}, priority=5, block=True)


@skills_cmd.handle()
async def handle_skills(event: MessageEvent):
    if not is_allowed(event):
        await skills_cmd.finish("无权限")

    user_id = str(event.get_user_id())
    group_id = str(event.group_id) if hasattr(event, "group_id") else None

    lines = ["可用 skill："]
    for name in sorted(skill_registry.skills):
        allowed = skill_registry.is_allowed(name, user_id, group_id)
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
    skill_registry.install(manifest)
    await install_cmd.finish(f"已安装 skill：{name}\n类型：{manifest.type}\n描述：{manifest.description}")


uninstall_cmd = on_command("skilluninstall", aliases={"skill uninstall", "卸载技能"}, priority=5, block=True)


@uninstall_cmd.handle()
async def handle_uninstall(event: MessageEvent):
    if not is_allowed(event):
        await uninstall_cmd.finish("无权限")

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
    skill_registry.uninstall(name)
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


def _persona_tokens(raw: str) -> list[str]:
    """去掉开头的 / ! 与命令词（persona/personas/人格/人设），返回子命令词。"""
    s = (raw or "").strip()
    s = re.sub(r"^[/!！]?\s*", "", s).strip()
    s = re.sub(r"^(personas?|人格|人设)\b[\s:：]*", "", s, flags=re.IGNORECASE).strip()
    return s.split() if s else []


@persona_cmd.handle()
async def handle_persona(event: MessageEvent):
    if not is_allowed(event):
        await persona_cmd.finish("无权限")
    memory, manager = _persona_objs()
    if memory is None or manager is None:
        await persona_cmd.finish("人格系统未初始化。")

    user_id = str(event.get_user_id())
    tokens = _persona_tokens(str(event.get_message()))
    sub = tokens[0] if tokens else ""

    try:
        if sub in {"list", "ls", "列表", "查看"} or not sub:
            default = manager.default()
            current = await memory.get_user_persona(user_id)
            cur_name = current or (default.name if default else "（无）")
            lines = [f"当前人格：{cur_name}"]
            lines += _persona_list_lines(manager)
            lines.append("用法：/persona list 查看；/persona use <名字> 或直接 /persona <名字> 切换；/persona reset 恢复默认")
            await persona_cmd.finish("\n".join(lines))

        if sub in {"reset", "clear", "默认", "清除", "恢复默认"}:
            await memory.set_user_persona(user_id, None)
            await persona_cmd.finish("已恢复默认人格。")

        # use <name> / 使用 <name> / 直接给一个人格名
        if sub in {"use", "set", "switch", "切换", "使用"}:
            name = tokens[-1].strip() if len(tokens) >= 2 else ""
        else:
            name = sub

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

    await persona_cmd.finish("用法：/persona list 查看；/persona use <名字> 或 /persona <名字> 切换；/persona reset 恢复默认")
