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

**重启完毕回执**：``/reboot`` 会在 perform_reboot 前把「回执目标 + 起始时间」
落一份原子文件（``data/reboot-notice.json``）；新进程启动后由 bot 连接钩子
（``driver.on_bot_connect``）一次性消费，往**同一条会话**发「重启完毕 + 耗时」。
web 重启没有会话上下文，不登记回执（UI 自身有状态）。

安全口径：**无 shell**；外部命令走 argv 白名单（systemctl/docker/
supervisorctl/service）+ 逐项字符校验——沿用沙箱铁律的白名单姿势。
QQ 命令限 superuser（``is_allowed`` 内已含黑名单静默）；web 与写面同门禁
（CIDR + ``AGENT_WEB_WRITE``）+ 两段确认码。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CMD_ENV = "AGENT_REBOOT_CMD"
DELAY_ENV = "AGENT_REBOOT_DELAY"
ENTRY_ENV = "AGENT_REBOOT_ENTRY"
NOTICE_FILE_ENV = "AGENT_REBOOT_NOTICE_FILE"
DEFAULT_DELAY = 2.0
DEFAULT_NOTICE_FILE = "data/reboot-notice.json"
# C 步观察窗：spawn 后给外部命令这么长时间暴露「秒退」（配置错误时 systemctl/
# docker/supervisorctl 立刻非零退出）——观察到非零退出即回退 execv，不再退
# 出等一个起不来的 supervisor（REVIEW-3ce6e0a..de09478 M3）
_C_STEP_OBSERVE = 0.5

# 外部命令可执行体白名单（裸名；走 OS PATH 解析，解析失败 spawn 抛错 → 回退
# re-exec）。刻意不含 shell/sh/-c 形态——QQ 消息触发的重启绝不经 shell 解析。
_ALLOWED_EXECS = ("systemctl", "docker", "supervisorctl", "service")


def reboot_delay() -> float:
    """回复发出到开始停机的宽限（秒）。脏值/负值/非有限值回退默认。"""
    raw = (os.getenv(DELAY_ENV) or "").strip()
    if not raw:
        return DEFAULT_DELAY
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，回退 %s", DELAY_ENV, raw, DEFAULT_DELAY)
        return DEFAULT_DELAY
    if not math.isfinite(value):
        # inf/nan 会让 asyncio.sleep 永不返回（永不重启且无告警）——lifecycle
        # flush timeout 的 L6 同型护栏，此处不能漏
        logger.warning("%s=%r 非有限数值，回退 %s", DELAY_ENV, raw, DEFAULT_DELAY)
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


def _spawn_detached(argv: list[str]) -> subprocess.Popen[bytes] | None:
    """脱离会话执行外部重启命令（新会话组， stdio 全部断开，无 shell）。

    返回 Popen 供 C 步观察窗轮询（配置错误的子命令会秒退非零，见
    perform_reboot）；测试替身返回 None 则跳过观察直接退出。
    """
    return subprocess.Popen(
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
    observe: float | None = None,
) -> str:
    """同步执行重启。**调用前必须已完成停机编排且回复已发出**。

    ``spawn``/``execv``/``_exit`` 可注入（测试用）。正常路径不返回（进程被
    替换或退出）；返回只可能发生在测试替身下。C 步 spawn 成功后观察
    ``observe``（默认 :data:`_C_STEP_OBSERVE`）秒：外部命令已非零退出即视为
    该步失败，回退 re-exec（REVIEW-3ce6e0a..de09478 M3——只回退「spawn 失败」
    会让配错的命令把 bot 彻底带下线）。
    """
    do_exit = _exit or os._exit
    observe = _C_STEP_OBSERVE if observe is None else max(0.0, observe)
    argv, _reason = parse_reboot_cmd(os.getenv(CMD_ENV, ""))
    if argv:
        proc: Any = None
        try:
            proc = (spawn or _spawn_detached)(argv)
        except Exception:
            logger.exception("reboot: 外部命令 spawn 失败，回退 re-exec")
        else:
            if proc is None:
                do_exit(0)
                return "external"
            deadline = time.monotonic() + observe
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            rc = proc.poll()
            if rc is not None and rc != 0:
                logger.error(
                    "reboot: 外部命令 %s 秒内退出（code=%s，配置错误？），回退 re-exec",
                    observe,
                    rc,
                )
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
    await shutdown_agent(
        debouncer=debouncer,
        memory=memory,
        # 停机顺序不变量的第一环：scheduler 必须在 flush 之前停——reboot 经
        # execv/_exit 退出，on_shutdown 钩子（优雅停机唯一停 scheduler 的地方）
        # 永远不会执行，这里是 reboot 路径唯一的收尾（REVIEW M2）
        scheduler=getattr(get_driver(), "_agent_scheduler", None),
        extra_closers=extra,
    )


# 后台任务强引用：asyncio 只持弱引用，不存起来会被 GC 吞掉重启序列
_REBOOT_TASKS: set[asyncio.Task] = set()


# ---------- 重启完毕回执（跨进程） ----------
def _notice_path() -> Path:
    return Path(os.getenv(NOTICE_FILE_ENV) or DEFAULT_NOTICE_FILE)


def write_reboot_notice(target: str, started_at: float, by: str = "-") -> None:
    """登记「重启完毕」回执：目标会话 + 起始时间。原子写（tmp + replace）。

    重启语义下半截文件比没有文件更糟——新进程读到坏 JSON 只能丢弃，回执静默
    丢失；写入失败只记日志，不影响重启本身（回执是增益，不是前提）。
    """
    payload = {"target": target, "started_at": started_at, "by": by}
    path = _notice_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.exception("reboot: 回执登记失败（新进程不会收到重启完毕提示）")


def consume_reboot_notice() -> dict | None:
    """读并删除回执登记（一次性消费）；无/坏文件都按「无回执」处理。"""
    path = _notice_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        # 读失败不删：保留到下次启动再消费（比丢回执好）
        logger.warning("reboot: 回执文件读取失败（保留待下次消费）：%s", path)
        return None
    # 读到了就消费掉：坏内容不该在每次启动/重连时反复告警
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("reboot: 回执文件删除失败（可能重复回执一次）：%s", path)
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("reboot: 回执文件不是合法 JSON，已丢弃")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("target"), str):
        logger.warning("reboot: 回执文件缺 target，已丢弃")
        return None
    try:
        data["started_at"] = float(data.get("started_at"))
    except (TypeError, ValueError):
        logger.warning("reboot: 回执文件 started_at 非法，已丢弃")
        return None
    return data


_DONE_TEXT = "☁️ 重启完毕，耗时 {elapsed:.1f}s，我回来了。"


async def _send_reboot_done(bot) -> None:
    """bot 连接后发送一次性「重启完毕」回执（同一条会话）。

    只在 ``/reboot`` 命令路径有登记时触发；web 重启无会话上下文，不登记。
    任何失败只记日志：回执送不出去不影响刚起来的 bot 正常工作。
    """
    notice = consume_reboot_notice()
    if notice is None:
        return
    target = notice["target"]
    kind, _, ident = target.partition(":")
    if kind not in ("private", "group") or not ident.isascii() or not ident.isdigit():
        logger.warning("reboot: 回执目标形态不合法：%r", target)
        return
    elapsed = max(0.0, time.time() - notice["started_at"])
    text = _DONE_TEXT.format(elapsed=elapsed)
    try:
        if kind == "group":
            await bot.send_group_msg(group_id=int(ident), message=text)
        else:
            await bot.send_private_msg(user_id=int(ident), message=text)
        logger.info("reboot: 重启完毕回执已送达 %s（耗时 %.1fs）", target, elapsed)
    except Exception:
        logger.exception("reboot: 重启完毕回执发送失败：%s", target)


async def reboot_after_reply(
    delay: float | None = None, target: str | None = None
) -> None:
    """回复发出后调用：等宽限 → 停机编排 → 执行重启。绝不抛异常。"""
    started_at = time.time()
    try:
        await asyncio.sleep(reboot_delay() if delay is None else max(0.0, delay))
        await _shutdown_everything()
    except Exception:
        logger.exception("reboot: 停机编排失败（仍然继续重启）")
    if target:
        # 紧贴 perform_reboot 之前落盘：起始时间包含宽限+停机+exec+启动全程
        write_reboot_notice(target, started_at)
    perform_reboot()


def schedule_reboot(target: str | None = None) -> None:
    """把重启序列挂成后台任务（持强引用），立即返回不阻塞调用方。

    ``target``：重启完毕回执的会话（``private:<uid>`` / ``group:<gid>``）；
    web 重启无会话上下文，传 None = 不回执。
    """
    try:
        task = asyncio.get_running_loop().create_task(reboot_after_reply(target=target))
    except RuntimeError:  # 无运行中事件循环（测试环境）：退化为同步执行
        if target:
            write_reboot_notice(target, time.time())
        perform_reboot()
        return
    _REBOOT_TASKS.add(task)
    task.add_done_callback(_REBOOT_TASKS.discard)


# ---------- /reboot 命令 ----------
# 模块级注册（与 admin.py 同构）：superuser 直执行，无二次确认（用户 2026-09-29
# 定稿）。重启前先回复，停机序列挂后台任务——handler 返回不阻塞。
from nonebot import get_driver, on_command  # noqa: E402
from nonebot.adapters.onebot.v11 import MessageEvent  # noqa: E402

from agentcore.diagnostics import record as _diag_record  # noqa: E402
from agentcore.workspace.utils import is_superuser  # noqa: E402

from .acl import deny, is_allowed  # noqa: E402
from .admin import _not_self_message  # noqa: E402

reboot_cmd = on_command(
    "reboot", aliases={"重启"}, priority=5, block=True, rule=_not_self_message
)

# 重启完毕回执：新进程启动、bot 连上协议端后一次性发送（登记见 write_reboot_notice）。
# 注册点是模块导入时——reboot 模块由 _load_plugin_modules() 在 on_startup 阶段加载，
# 钩子在每次 bot 连接时求值，时序上晚于连接、不会漏。
get_driver().on_bot_connect(_send_reboot_done)

_STRATEGY_TEXT = {
    "external": "外部命令（supervisor 接管）",
    "execv": "进程内替换（同 PID）",
    "exit": "退出等 supervisor 拉起",
}


def _reply_target(event: MessageEvent) -> str:
    """回执目标：命令所在的会话（群→group:，私聊→private:）。"""
    group_id = getattr(event, "group_id", None)
    if group_id:
        return f"group:{group_id}"
    return f"private:{event.get_user_id()}"


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
    # 回复已 await 投递，再挂后台停机+重启序列；target 供新进程发「重启完毕」回执
    schedule_reboot(_reply_target(event))
