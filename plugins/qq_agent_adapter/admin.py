from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent
from .acl import is_allowed
from agentcore.skills import registry as skill_registry


reset = on_command("reset", aliases={"重置"}, priority=5, block=True)


@reset.handle()
async def handle_reset(event: MessageEvent):
    if not is_allowed(event):
        await reset.finish("无权限")
    await reset.finish("会话已重置（持久化清理待实现）")


help_cmd = on_command("aihelp", aliases={"agenthelp", "帮助"}, priority=5, block=True)


@help_cmd.handle()
async def handle_help(event: MessageEvent):
    await help_cmd.finish(
        "指令：\n/reset 重置会话\n/status 查看状态（待实现）\n/skills 查看可用 skill\n"
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
