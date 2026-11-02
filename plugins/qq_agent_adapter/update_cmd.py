"""``/update``：拉取 ``main`` → 同步依赖 → 重启 → 失败自动回滚。

命令面（与 ``/reboot`` 同口径：superuser 直执行，无二次确认）：

- ``/update``         立即更新
- ``/update status``  只读：当前提交、启动时间、上次更新结果

为什么重启交给脚本而不是本进程：脚本人 ``systemd-run --user`` 起的独立 cgroup，
服务重启杀不到它；进程内 ``execv`` 没有回滚观察者——新代码一旦起不来就是
crash loop，而脚本会在 90 秒内等到 boot marker 或把代码回滚。

回执分两段：更新前失败（拒绝/已是最新/合并失败）由**当前进程**的 watcher 发；
真正重启后的结果（成功/回滚）由**新进程**在 ``on_bot_connect`` 时发——旧进程
那时已经不在了，不能指望它。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from nonebot import get_bot, get_driver, on_command
from nonebot.adapters.onebot.v11 import MessageEvent

from agentcore import tz
from agentcore.diagnostics import record as _diag_record
from agentcore.workspace.utils import is_superuser

from .acl import deny, is_allowed
from .admin import _not_self_message

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "data" / "state"
BOOT_STATE = STATE_DIR / "boot.json"
UPDATE_STATE = STATE_DIR / "update.json"

_TERMINAL = {"up_to_date", "refused", "failed", "rolled_back", "ok"}
_RUNNING = {"restarting", "rolling_back"}
_TERMINAL_WAIT = 180.0
_NOTIFY_POLL = 1.0

update_cmd = on_command("update", priority=5, block=True, rule=_not_self_message)


# ---------------------------------------------------------------- 纯函数


def parse_update_arg(text: str) -> str:
    """``/update`` → ``run``；``/update status`` → ``status``；其它 → ``unknown``。

    按 token 解析而不是截字符串：``get_plaintext()`` 之后 @ 段已经不在，但
    ``/update`` 也可能以裸 ``update`` 被匹配（不同协议端实现），首个 token 一律
    归一化后再看剩下什么。
    """
    tokens = str(text or "").strip().split()
    if tokens and tokens[0].lstrip("/").lower() in {"update", "更新"}:
        tokens = tokens[1:]
    if not tokens:
        return "run"
    arg = tokens[0].lower()
    return "status" if arg in {"status", "状态"} else "unknown"


def reply_target(event: MessageEvent) -> str:
    group_id = getattr(event, "group_id", None)
    if group_id:
        return f"group:{group_id}"
    return f"private:{event.get_user_id()}"


def format_notice(data: dict) -> str:
    """把 update.json 的终态翻译成一句回执。"""
    status = str(data.get("status") or "")
    src = str(data.get("from") or "?")
    dst = str(data.get("to") or "?")
    reason = str(data.get("reason") or "").strip()
    if status == "ok":
        return f"✅ 更新完成：{src} → {dst}（已重启并校验）"
    if status == "up_to_date":
        return f"已是最新，未重启（{dst}）"
    if status == "rolled_back":
        return f"⚠️ 更新失败，已自动回滚到 {src}。原因：{reason or '启动校验未通过'}"
    if status == "failed":
        return f"❌ 更新失败：{reason or '未知原因'}"
    if status == "refused":
        return f"未更新：{reason or '前置检查未通过'}"
    return f"更新状态未知：{status or '空'}"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(data: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = UPDATE_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, UPDATE_STATE)
    except Exception:
        logger.warning("update state write failed", exc_info=True)


def status_text() -> str:
    boot = _read(BOOT_STATE)
    last = _read(UPDATE_STATE)
    sha = str(boot.get("sha") or "未知")
    branch = str(boot.get("branch") or "?")
    version = str(boot.get("version") or "未知")
    started = boot.get("started_at")
    lines = [f"当前运行：{sha}（{branch}）· 版本 {version}"]
    if isinstance(started, (int, float)):
        lines.append(f"启动于：{tz.fmt_ts(float(started))}")
    status = str(last.get("status") or "")
    if status:
        when = last.get("finished_at") or last.get("started_at")
        stamp = (
            f"（{tz.fmt_ts(float(when))}）" if isinstance(when, (int, float)) else ""
        )
        lines.append(
            f"上次更新：{status} {last.get('from', '?')} → {last.get('to', '?')}{stamp}"
        )
        if last.get("reason"):
            lines.append(f"原因：{last['reason']}")
    else:
        lines.append("上次更新：无记录")
    return "\n".join(lines)


def _running_reason(now: float | None = None) -> str:
    data = _read(UPDATE_STATE)
    status = str(data.get("status") or "")
    if status not in _RUNNING:
        return ""
    started = data.get("started_at")
    moment = time.time() if now is None else float(now)
    if isinstance(started, (int, float)) and moment - float(started) < 300:
        return "上一次更新还在进行中，稍后再试"
    return ""


# ---------------------------------------------------------------- 执行


def _spawn_update(notify_to: str) -> tuple[bool, str]:
    """用 systemd-run 拉起更新脚本（独立 cgroup，服务重启杀不到它）。"""
    unit = f"agent-demo-update-{int(time.time())}"
    argv = [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        "--collect",
        "--property=Type=oneshot",
        f"--working-directory={ROOT}",
        sys.executable,
        str(ROOT / "scripts" / "self_update.py"),
        "--notify-to",
        notify_to,
    ]
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=15, check=False, env=env
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-300:]
        return False, tail or f"systemd-run 退出码 {proc.returncode}"
    return True, ""


async def _send(target: str, text: str) -> bool:
    try:
        bot = get_bot()
        kind, _, ident = target.partition(":")
        if kind == "group" and ident.isdigit():
            await bot.call_api("send_group_msg", group_id=int(ident), message=text)
            return True
        if kind == "private" and ident.isdigit():
            await bot.call_api("send_private_msg", user_id=int(ident), message=text)
            return True
    except Exception:
        logger.warning("update notice send failed", exc_info=True)
    return False


def _start_watcher(target: str) -> None:
    """挂后台回执任务；抽成函数便于测试替换（不把 asyncio 细节漏进用例）。"""
    asyncio.create_task(_watch_result(target))


async def _watch_result(target: str) -> None:
    """旧进程侧的回执：只覆盖「没走到重启」的终态。"""
    deadline = time.time() + _TERMINAL_WAIT
    while time.time() < deadline:
        await asyncio.sleep(2.0)
        data = _read(UPDATE_STATE)
        status = str(data.get("status") or "")
        if status in {"restarting", "rolling_back"}:
            return  # 交给新进程的 on_bot_connect
        if status in _TERMINAL and not data.get("notified"):
            if await _send(target, format_notice(data)):
                data["notified"] = True
                _write(data)
            return


async def _notify_after_boot(bot) -> None:
    """新进程侧的回执：等脚本把 restarting 改成终态再发。"""
    deadline = time.time() + 60.0
    data = _read(UPDATE_STATE)
    while time.time() < deadline and str(data.get("status") or "") in _RUNNING:
        await asyncio.sleep(_NOTIFY_POLL)
        data = _read(UPDATE_STATE)
    if str(data.get("status") or "") in _RUNNING:
        # 脚本还在等 boot marker（最坏 90s）。别把「restarting」当结果发出去；
        # 下一次连接会再来看，回滚成功也还有一次连接。
        return
    if not data or data.get("notified"):
        return
    target = str(data.get("notify_to") or "")
    if not target:
        return
    try:
        kind, _, ident = target.partition(":")
        if kind == "group" and ident.isdigit():
            await bot.call_api(
                "send_group_msg", group_id=int(ident), message=format_notice(data)
            )
        elif kind == "private" and ident.isdigit():
            await bot.call_api(
                "send_private_msg", user_id=int(ident), message=format_notice(data)
            )
        else:
            return
    except Exception:
        logger.warning("update notice send failed", exc_info=True)
        return
    data["notified"] = True
    _write(data)


get_driver().on_bot_connect(_notify_after_boot)


@update_cmd.handle()
async def handle_update(event: MessageEvent) -> None:
    if not is_allowed(event):
        await deny(update_cmd, event, "无权限")
    who = str(event.get_user_id())
    if not is_superuser(who):
        await update_cmd.finish("只有管理员能执行更新。")

    action = parse_update_arg(event.get_plaintext())
    if action == "status":
        await update_cmd.finish(status_text())
    if action == "unknown":
        await update_cmd.finish(
            "用法：/update（立即更新）或 /update status（只看状态）"
        )

    if (os.getenv("AGENT_UPDATE_OK") or "").strip() != "1":
        await update_cmd.finish(
            "当前进程不是生产方式启动（缺 AGENT_UPDATE_OK），拒绝更新。"
        )
    running = _running_reason()
    if running:
        await update_cmd.finish(f"未开始：{running}")

    target = reply_target(event)
    ok, reason = _spawn_update(target)
    if not ok:
        logger.warning("update spawn failed: %s", reason)
        await update_cmd.finish(f"启动更新脚本失败：{reason}")

    _diag_record("update_requested", by=f"qq:{who}", target=target)
    logger.warning("update: 管理员 %s 触发更新（回执到 %s）", who, target)
    await update_cmd.send(
        "🔄 开始更新：拉取 main → 同步依赖 → 重启。完成后会在这里回执；"
        "失败会自动回滚到当前提交。"
    )
    _start_watcher(target)
