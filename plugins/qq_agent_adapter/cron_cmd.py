"""cron 定时任务的 QQ 管理命令（/cron）与 /openclaw 导入。

**通道纪律（AC-cron-superuser-only 修订）**：仅 superuser 可用，私聊与群聊
均可（群聊放开是刻意取舍：命令只有 superuser 调得动，H2 的「任务定义被
引用/转发扩散」防的是信息扩散而非权限提升）。与 SSH / web 写面并列的
人通道——LLM 零通道不变（AC D2）。

业务逻辑在 agentcore/scheduler/cron_admin.py（纯函数，无 nonebot 依赖），
本模块是 matcher 薄壳。命令面见 cron_admin 的 parse_cron_cmd docstring。
"""

from __future__ import annotations

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent

from agentcore.scheduler.cron_admin import (
    _record,
    import_openclaw,
    parse_cron_cmd,
    run_cron_action,
)

from .acl import deny, is_superuser_id

cron_cmd = on_command("cron", aliases={"定时任务"}, priority=5, block=True)


@cron_cmd.handle()
async def _cron_handle(event: MessageEvent):
    uid = str(event.get_user_id())
    if not is_superuser_id(uid):
        await deny(cron_cmd, event, "cron 命令仅 superuser 可用")
        return
    action, rest = parse_cron_cmd(str(event.get_message()))
    await cron_cmd.finish(run_cron_action(action, rest, owner=uid))


openclaw_cmd = on_command("openclaw", priority=5, block=True)


@openclaw_cmd.handle()
async def _openclaw_handle(event: MessageEvent):
    uid = str(event.get_user_id())
    if not is_superuser_id(uid):
        await deny(openclaw_cmd, event, "openclaw 命令仅 superuser 可用")
        return
    raw = str(event.get_message()).strip()
    if not raw.startswith("import"):
        await openclaw_cmd.finish(
            "用法：/openclaw import <OpenClaw 工作区目录>（bot 所在机器的本地路径）"
        )
    root = raw[len("import") :].strip()
    pm = None
    try:
        from nonebot import get_driver

        pm = getattr(get_driver(), "_agent_persona_manager", None)
    except Exception:
        pm = None
    text = import_openclaw(root, persona_manager=pm)
    _record("openclaw_imported", by=f"qq:{uid}", root=root[:120])
    await openclaw_cmd.finish(text)
