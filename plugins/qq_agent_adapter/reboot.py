"""进程重启：``/reboot`` 命令与 web 重启按钮共用的核心。

执行链（2026-09-29 用户定稿：A 为主 + C/B 兜底，每一步失败都有明确退路）：

1. **C 外部命令**：``AGENT_REBOOT_CMD`` 配了白名单 argv 时 spawn 脱离会话的
   子进程执行（如 ``["systemctl","restart","agent-demo"]``），本进程随即退出
   让位——restart 语义显式，docker/supervisorctl 场景覆盖；
2. **B re-exec 兜底**：外部命令未配或 spawn 失败时 ``os.execv`` 自替换——同
   PID、全新 import，``.env`` 新值天然生效；执行前校验解释器与入口文件存在，
   校验不过**不进** execv；
3. **A 退出兜底**：execv 也失败时 ``os._exit(0)``，由 supervisor（systemd
   ``Restart=always`` / compose ``restart: unless-stopped``）拉起。裸 ``nohup``
   部署退到这一步会死亡——README 强制要求声明部署形态。

停机编排：所有路径都先跑 ``lifecycle.shutdown_agent``（固定顺序
scheduler→debounce flush→store.aclose，幂等，flush 有
``AGENT_SHUTDOWN_FLUSH_TIMEOUT`` deadline），且执行前等 ``AGENT_REBOOT_DELAY``
秒让当轮回复真正发出。

安全口径：**无 shell**；外部命令走 argv 白名单（systemctl/docker/
supervisorctl/service）+ 逐项字符校验——沿用沙箱铁律的白名单姿势。
QQ 命令限 superuser（``is_allowed`` 内已含黑名单静默）；web 与写面同门禁
（CIDR + ``AGENT_WEB_WRITE``）+ 两段确认码。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)

CMD_ENV = "AGENT_REBOOT_CMD"
UNIT_ENV = "AGENT_REBOOT_UNIT"
DELAY_ENV = "AGENT_REBOOT_DELAY"
ENTRY_ENV = "AGENT_REBOOT_ENTRY"
DEFAULT_DELAY = 2.0
DEFAULT_UNIT = "agent-demo"

# 外部命令可执行体白名单（basename；实际存在性由 shutil.which 运行时校验）。
# 刻意不含 shell/sh/-c 形态——QQ 消息触发的重启绝不经 shell 解析。
_ALLOWED_EXECS = ("systemctl", "docker", "supervisorctl", "service")


def reboot_delay() -> float:
    """回复发出到开始停机的宽限（秒）。脏值/负值回退默认。"""
    raw = (os.getenv(DELAY_ENV) or "").strip()
    if not raw:
        return DEFAULT_DELAY
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，回退 %s", DELAY_ENV, raw, DEFAULT_DELAY)
        return DEFAULT_DELAY
    return max(0.0, value)


def parse_reboot_cmd(raw: str) -> tuple[list[str] | None, str]:
    """解析 ``AGENT_REBOOT_CMD``（JSON argv 数组）。返回 ``(argv, reason)``。

    任一不满足即整体拒绝（返回 None 与原因）：非 JSON / 非数组 / 空项 /
    可执行体不在白名单 / 参数含控制字符。fail-closed：宁可退回 execv/exit
    也不放行一个没校验过的命令。
    """
    text = (raw or "").strip()
    if not text:
        return None, "empty"
    try:
        data = json.loads(text)
    except Exception:
        return None, "not_json"
    if not isinstance(data, list) or len(data) < 2:
        return None, "not_argv_list"
    items: list[str] = []
    for item in data:
        if not isinstance(item, str) or not item.strip():
            return None, "empty_item"
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in item):
            return None, "control_chars"
        items.append(item.strip())
    # 可执行体必须是**裸名**（OS 走 PATH 解析），拒任何带路径的形式——
    # 否则 ["/tmp/systemctl", …] 这类指向自建脚本的 argv 会借白名单名放行。
    # 不做 basename 归一化：名字恰好等于白名单项是唯一入口。
    if items[0] not in _ALLOWED_EXECS:
        return None, f"exec_not_allowed:{items[0]}"
    return items, "ok"


def _execv_target_ok() -> bool:
    """re-exec 前置校验：解释器与入口文件都必须真实存在。"""
    entry = os.getenv(ENTRY_ENV, "bot.py")
    return (
        bool(sys.executable)
        and os.path.isfile(sys.executable)
        and os.path.isfile(entry)
    )


def reboot_plan() -> tuple[str, list[str] | None]:
    """当前配置下的重启计划：``("external", argv)`` / ``("execv", None)`` /
    ``("exit", None)``。供 UI/审计展示与测试断言。"""
    argv, _ = parse_reboot_cmd(os.getenv(CMD_ENV, ""))
    if argv:
        return "external", argv
    if _execv_target_ok():
        return "execv", None
    return "exit", None


def _spawn_detached(argv: list[str]) -> None:
    """脱离会话执行外部重启命令（新会话组， stdio 全部断开，无 shell）。"""
    subprocess.Popen(
        argv,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def perform_reboot(
    *,
    spawn: Any = None,
    execv: Any = None,
    _exit: Any = None,
) -> str:
    """同步执行重启。**调用前必须已完成停机编排且回复已发出**。

    ``spawn``/``execv``/``_exit`` 可注入（测试用）。正常路径不返回（进程被
    替换或退出）；返回只可能发生在测试替身下。
    """
    do_exit = _exit or os._exit
    argv, _reason = parse_reboot_cmd(os.getenv(CMD_ENV, ""))
    if argv:
        try:
            (spawn or _spawn_detached)(argv)
        except Exception:
            logger.exception("reboot: 外部命令 spawn 失败，回退 re-exec")
        else:
            do_exit(0)
            return "external"
    if _execv_target_ok():
        try:
            (execv or os.execv)(
                sys.executable,
                [sys.executable, os.getenv(ENTRY_ENV, "bot.py")],
            )
        except Exception:
            logger.exception("reboot: re-exec 失败，回退退出（等 supervisor）")
        else:
            return "execv"
    logger.error(
        "reboot: 退到退出兜底——需要 supervisor（systemd Restart=always 或 "
        "compose restart: unless-stopped）拉起；裸 nohup 部署不会恢复"
    )
    do_exit(0)
    return "exit"


async def _shutdown_everything() -> None:
    """跑与 bot.py on_shutdown 同一套收尾（幂等）。任何失败只记日志——
    重启优先级高于单个 closer 的洁癖。"""
    from plugins.qq_agent_adapter.lifecycle import shutdown_agent

    debouncer = None
    memory = None
    extra: list = []
    try:
        import plugins.qq_agent_adapter.matcher as _matcher

        debouncer = _matcher.get_debouncer()
    except Exception:
        logger.exception("reboot: get debouncer failed")
    try:
        from nonebot import get_driver

        memory = getattr(get_driver(), "_agent_memory", None)
    except Exception:
        logger.exception("reboot: get memory failed")

    async def _close_shared_llm() -> None:
        from agentcore.skills.registry import close_shared_llm_client

        await close_shared_llm_client()

    async def _close_search() -> None:
        from agentcore.skills.search import aclose_search_client

        await aclose_search_client()

    extra = [_close_shared_llm, _close_search]
    await shutdown_agent(debouncer=debouncer, memory=memory, extra_closers=extra)


# 后台任务强引用：asyncio 只持弱引用，不存起来会被 GC 吞掉重启序列
_REBOOT_TASKS: set[asyncio.Task] = set()


async def reboot_after_reply(delay: float | None = None) -> None:
    """回复发出后调用：等宽限 → 停机编排 → 执行重启。绝不抛异常。"""
    try:
        await asyncio.sleep(reboot_delay() if delay is None else max(0.0, delay))
        await _shutdown_everything()
    except Exception:
        logger.exception("reboot: 停机编排失败（仍然继续重启）")
    perform_reboot()


def schedule_reboot() -> None:
    """把重启序列挂成后台任务（持强引用），立即返回不阻塞调用方。"""
    try:
        task = asyncio.get_running_loop().create_task(reboot_after_reply())
    except RuntimeError:  # 无运行中事件循环（测试环境）：退化为同步执行
        perform_reboot()
        return
    _REBOOT_TASKS.add(task)
    task.add_done_callback(_REBOOT_TASKS.discard)


# ---------- /reboot 命令 ----------
# 模块级注册（与 admin.py 同构）：superuser 直执行，无二次确认（用户 2026-09-29
# 定稿）。重启前先回复，停机序列挂后台任务——handler 返回不阻塞。
from nonebot import on_command  # noqa: E402
from nonebot.adapters.onebot.v11 import MessageEvent  # noqa: E402

from agentcore.diagnostics import record as _diag_record  # noqa: E402
from agentcore.workspace.utils import is_superuser  # noqa: E402

from .acl import deny, is_allowed  # noqa: E402
from .admin import _not_self_message  # noqa: E402

reboot_cmd = on_command(
    "reboot", aliases={"重启"}, priority=5, block=True, rule=_not_self_message
)

_STRATEGY_TEXT = {
    "external": "外部命令（supervisor 接管）",
    "execv": "进程内替换（同 PID）",
    "exit": "退出等 supervisor 拉起",
}


@reboot_cmd.handle()
async def handle_reboot(event: MessageEvent) -> None:
    if not is_allowed(event):
        await deny(reboot_cmd, event, "无权限")
    # L25 同标准：变更类操作仅 superuser（黑名单已在 is_allowed 内静默）
    if not is_superuser(str(event.get_user_id())):
        await reboot_cmd.finish("只有管理员能重启 bot。")
    who = str(event.get_user_id())
    strategy, argv = reboot_plan()
    _diag_record(
        "reboot_requested", by=f"qq:{who}", strategy=strategy, cmd=(argv or [])[:1]
    )
    logger.warning("reboot: 管理员 %s 触发重启（strategy=%s）", who, strategy)
    await reboot_cmd.send(
        "♻️ 收到重启指令，正在保存状态并重启……\n"
        f"方式：{_STRATEGY_TEXT[strategy]}；期间无法应答，稍等片刻回来。"
    )
    # 回复已 await 投递，再挂后台停机+重启序列
    schedule_reboot()
