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
import fcntl
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
# 旧进程 watcher 的时间预算：必须覆盖脚本在「重启之前」能合法花掉的最长链——
# fetch 180s + merge 120s + uv sync 900s + import bot 180s = 1380s（M3：原值
# 180s 与 git fetch 的 timeout 压线相等，fetch 挂满后脚本才写 failed，watcher
# 早已静默退出，「更新前失败」的回执面整个丢失）。另留 120s 给子进程开销。
_TERMINAL_WAIT = 1500.0
_NOTIFY_POLL = 1.0
# 超过这个窗口的旧结果不再重播：一次几小时前的失败不该在每次重启后又弹一遍
_STALE_WINDOW = 600.0

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


def is_stale_notice(
    state: dict, boot_started, *, window: float = _STALE_WINDOW
) -> bool:
    """这条更新结果是否属于「本次启动之前很久」的旧账（重启后不该重播）。

    成功/回滚发生在重启前几秒，仍然会通知；几小时前的拒绝不会。

    M2（2026-11 评审）：判据原本取 ``started_at``（更新开始时刻）。慢更新
    （fetch 180s + merge 120s + uv sync 900s + import 180s，回滚再叠一遍）
    合法超过 600s 窗口时，新进程把**本次**的成功/回滚判为 stale、只留一行
    ``logger.info``，``notified`` 永不置位——之后每次重启同样 stale，用户
    永远收不到回执。改取终态落盘时刻 ``finished_at``（缺省回退
    ``started_at``）：它天然贴近「这个结果是什么时候产生的」。
    """
    moment = state.get("finished_at")
    if not isinstance(moment, (int, float)):
        moment = state.get("started_at")
    if not isinstance(moment, (int, float)):
        return False
    if not isinstance(boot_started, (int, float)):
        return False
    return float(moment) < float(boot_started) - window


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


def _update_lock_held() -> bool:
    """``update.lock`` 的 flock 是否被占（= 更新脚本还活着）。

    脚本从 ``acquire_lock()`` 到进程退出全程持锁，所以「锁被占」比
    「state 是 restarting」更接近真相。探测用 ``LOCK_EX|LOCK_NB`` 试拿再立刻
    释放——本进程从不长期持锁，不妨碍真正的脚本。
    """
    lock_path = STATE_DIR / "update.lock"
    try:
        handle = lock_path.open("a+")
    except OSError:
        # 连锁文件都打不开：证明不了脚本活着。放行——重启是恢复手段，
        # 不能被「测不出」的状态挡住。
        return False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        handle.close()


def update_in_progress() -> str:
    """/update 的更新窗口是否正开着：开着则返回拒绝原因，否则空串。

    M4（2026-11 评审）：``/reboot`` 与 ``/update`` 原本无互锁。更新窗口内
    重启（``os.execve`` **PID 不变**）会让 ``self_update.boot_ready`` 的
    「pid 变了」条件永不满足，90s 后 ``reset --hard`` 把刚更新的代码回滚、
    状态写 ``rolled_back``——回执显示「启动校验未通过」，真实原因却是并发
    重启。flock 只挡并发脚本，覆盖不到 /reboot 与 web ``/api/reboot``。

    为什么不看 state 的 ``restarting``：脚本被杀（机器重启清掉 user 单元）
    时那个状态会**永远**留在文件里，把 /reboot 永久挡死。锁是活的判据——
    锁空即死账，放行并留一条 WARNING 供排障。
    """
    if not _update_lock_held():
        status = str(_read(UPDATE_STATE).get("status") or "")
        if status in _RUNNING:
            logger.warning(
                "update: state 停在 %r 但更新锁已释放（脚本已退出/被杀），"
                "不再拦截并发重启；这条更新不会有回执了",
                status,
            )
        return ""
    status = str(_read(UPDATE_STATE).get("status") or "")
    logger.info("update: 更新脚本正在运行（status=%r），拦截并发重启", status)
    return (
        "更新正在进行中（完成后会自动重启）。此时并发重启会触发自动回滚误判，"
        "请等这次更新结束"
    )


# ---------------------------------------------------------------- 执行


def _spawn_update(notify_to: str) -> tuple[bool, str]:
    """用 systemd-run 拉起更新脚本（独立 cgroup，服务重启杀不到它）。"""
    unit = f"agent-demo-update-{int(time.time())}"
    argv = [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        "--collect",
        # 必须异步：默认 systemd-run 会等 oneshot 跑完才返回，而真正更新时
        # 那一步包含重启——handler 会卡到进程被杀，连「开始更新」都发不出去。
        "--no-block",
        "--property=Type=oneshot",
        # 这枚标记必须显式带过去：systemd-run --user 的 unit 跑在用户管理器里，
        # 不继承 bot 那个 system unit 的 Environment=（2026-11-02 首次 /update
        # 实测踩中：脚本拒绝了这次调用）。这里传的是**已校验过**的值——handler
        # 先确认本进程有 AGENT_UPDATE_OK，才允许拉起。
        "--setenv=AGENT_UPDATE_OK=1",
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
    if is_stale_notice(data, _read(BOOT_STATE).get("started_at")):
        logger.info("update notice skipped (stale result)")
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
