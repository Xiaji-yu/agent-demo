#!/usr/bin/env python
"""生产自更新：拉 main → 同步依赖 → 重启 → 校验 → 失败自动回滚。

由 ``/update`` 通过 ``systemd-run --user`` 拉起，所以它**不在**
``agent-demo.service`` 的 cgroup 里——服务重启时 systemd 清理的是服务的 cgroup，
脚本不会被一起杀掉，回滚才有执行者（``start_new_session=True`` 不够：那只换会话，
换不了 cgroup）。

只做四类固定动作，全部走 argv、不经 shell：

1. ``git fetch / merge --ff-only / reset --hard``
2. ``uv sync --locked``（拒绝退回 pip，否则等于绕过 uv.lock）
3. ``os.kill(主进程, SIGTERM)``——SIGTERM 会让 uvicorn 走优雅停机，
   进而触发 ``lifecycle.shutdown_agent``（scheduler → debounce flush → aclose）
4. 读写 ``data/state/*.json``（boot marker / update 状态）

安全边界：

- 必须 ``AGENT_UPDATE_OK=1``（生产 unit 注入；dev 目录没有）——禁止在开发目录
  或手工前台启动的实例里误触发；
- 分支写死 ``main``，不接受任何外部传入的 ref；
- 工作树必须干净：这是 ``merge --ff-only`` 能成立的前提，也证明更新只动代码；
- 回滚只 reset 到脚本自己记录的 ``prev``，不接受外部指定目标。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BRANCH = "main"
REMOTE = "origin"
# 与 scripts/deploy.py 的 UNIT_NAME 一致；systemd 的 MainPID 从这里取
UNIT = "agent-demo"
BOOT_TIMEOUT = 90.0
POLL_INTERVAL = 1.0
MIN_FREE_BYTES = 2 * 1024**3
_ROLLBACK_TAIL = 800
# 运行态目录：bot 会往里写（`SkillInstaller` 装技能、渲染产物、日志…），
# 这些改动是正常现象，不该把更新永久挡住。gitignore 覆盖的路径本来就不出现在
# status 里；这里处理的是**被跟踪**的 data/ 文件（字体、示例技能、kb 样例）。
# 真正的边界是下一条：只要本次上游没改同名文件，`merge --ff-only` 就能安全跳过它们。
RUNTIME_PREFIXES = ("data/", "agentcore/personas/")

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
UPDATE_STATE = STATE_DIR / "update.json"
BOOT_STATE = STATE_DIR / "boot.json"
LOCK_FILE = STATE_DIR / "update.lock"
LOG_FILE = ROOT / "data" / "logs" / "update.log"


# ---------------------------------------------------------------- 基础设施


def log(msg: str) -> None:
    line = f"[{time.strftime('%F %T')}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except Exception as exc:  # 超时/找不到可执行文件都按失败返回
        return subprocess.CompletedProcess(
            argv, 127, "", f"{type(exc).__name__}: {exc}"
        )


def git(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(ROOT), *args], timeout=timeout)


def find_uv() -> str | None:
    """显式按候选找 uv：systemd 用户单元的 PATH 可能不含 ~/.local/bin。"""
    for cand in (
        shutil.which("uv"),
        str(Path.home() / ".local" / "bin" / "uv"),
        "/usr/local/bin/uv",
        "/usr/bin/uv",
    ):
        if cand and Path(cand).is_file():
            return cand
    return None


def write_state(payload: dict) -> None:
    """原子写更新状态；失败只记日志（回执缺失不该让更新本身失败）。"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), prefix=".update-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, UPDATE_STATE)
    except Exception:
        log(f"write_state failed: {payload.get('status')}")


def main_pid() -> int | None:
    proc = _run(["systemctl", "show", "-p", "MainPID", "--value", UNIT], timeout=10)
    raw = proc.stdout.strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def read_boot(path: Path | None = None) -> dict:
    try:
        return json.loads((path or BOOT_STATE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_state() -> dict:
    try:
        return json.loads(UPDATE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def porcelain_paths(text: str) -> list[str]:
    """从 ``git status --porcelain`` 的输出里取路径（兼容重命名与引号）。"""
    paths: list[str] = []
    for line in str(text or "").splitlines():
        if len(line) < 4:
            continue
        rest = line[3:].strip()
        if " -> " in rest:  # 重命名：取新路径
            rest = rest.split(" -> ", 1)[1]
        paths.append(rest.strip().strip('"'))
    return paths


def blocking_dirty(paths: list[str]) -> list[str]:
    """真正该拒绝的改动：只放过运行态目录，代码/配置/脚本一律不放。"""
    return [p for p in paths if not p.startswith(RUNTIME_PREFIXES)]


def conflicts_with_incoming(dirty: list[str], incoming: list[str]) -> list[str]:
    """运行态改动与本次上游改动的交集——有交集就不能 merge。"""
    return sorted(set(dirty) & set(incoming))


def dirty_paths() -> list[str]:
    proc = git("-c", "core.quotepath=false", "status", "--porcelain")
    return porcelain_paths(proc.stdout)


def acquire_lock():
    """独占更新锁；已有更新在跑时返回 None（不做排队）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# ---------------------------------------------------------------- 纯逻辑（可单测）


def preflight(env: dict[str, str] | None = None) -> tuple[bool, str]:
    """更新前检查；返回 (是否可继续, 原因)。"""
    env = os.environ if env is None else env
    if (env.get("AGENT_UPDATE_OK") or "").strip() != "1":
        return False, "AGENT_UPDATE_OK 未设置：当前不是生产方式启动的进程，拒绝更新"
    if not (ROOT / ".git").exists():
        return False, f"{ROOT} 不是 git 仓库（更新只支持源码部署）"
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != BRANCH:
        return False, f"当前分支是 {branch or '?'}，生产只允许 {BRANCH}"
    dirty = blocking_dirty(dirty_paths())
    if dirty:
        return (
            False,
            "工作树有改动（只允许 data/ 与人格目录），拒绝更新：\n"
            + "\n".join(dirty[:10]),
        )
    if not (ROOT / ".venv" / "bin" / "python").is_file():
        return False, ".venv 不存在（更新不会替你造 venv）"
    try:
        free = shutil.disk_usage(ROOT).free
    except OSError:
        free = 0
    if free < MIN_FREE_BYTES:
        return False, f"磁盘剩余不足 2G（{free // 1024**2} MB）"
    if find_uv() is None:
        return False, "找不到 uv：拒绝退回 pip（会绕过 uv.lock）"
    return True, "ok"


def boot_ready(
    target_sha: str,
    old_pid: int,
    *,
    timeout: float = BOOT_TIMEOUT,
    poll: float = POLL_INTERVAL,
    path: Path | None = None,
    clock=time.time,
    sleep=time.sleep,
) -> bool:
    """新进程是否真的起来了：pid 变了 **且** 跑的是目标提交。"""
    deadline = clock() + timeout
    while clock() < deadline:
        data = read_boot(path)
        pid = int(data.get("pid") or 0)
        sha = str(data.get("sha") or "")
        if pid and pid != old_pid and sha == target_sha:
            return True
        sleep(poll)
    return False


# ---------------------------------------------------------------- 动作


def _sync_and_smoke() -> tuple[bool, str]:
    uv = find_uv()
    if uv is None:
        return False, "找不到 uv"
    proc = _run([uv, "sync", "--locked", "--no-progress"], timeout=900)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-_ROLLBACK_TAIL:]
        return False, f"uv sync 失败：{tail}"
    py = ROOT / ".venv" / "bin" / "python"
    proc = _run([str(py), "-c", "import bot"], timeout=180)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-_ROLLBACK_TAIL:]
        return False, f"import bot 失败：{tail}"
    return True, ""


def _restart_and_wait(target_sha: str) -> tuple[bool, str]:
    pid = main_pid()
    if pid is None:
        return (
            False,
            "拿不到 systemd 主进程号（/update 需要在 systemctl 管理的服务里运行）",
        )
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # 已经退出：systemd 会按 Restart=always 拉起，直接等 marker
    except PermissionError:
        return False, f"无权限向 PID {pid} 发送 SIGTERM"
    if boot_ready(target_sha, pid):
        return True, ""
    return False, f"{int(BOOT_TIMEOUT)} 秒内未等到新进程的 boot marker"


def _rollback(prev: str) -> tuple[bool, str]:
    proc = git("reset", "--hard", prev, timeout=60)
    if proc.returncode != 0:
        return False, f"git reset 失败：{proc.stderr.strip()[-_ROLLBACK_TAIL:]}"
    ok, reason = _sync_and_smoke()
    if not ok:
        return False, f"回滚代码已就位，但重建依赖失败：{reason}"
    ok, reason = _restart_and_wait(prev[:12])
    if not ok:
        return False, f"回滚后仍未能启动：{reason}"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent-demo 生产自更新")
    parser.add_argument(
        "--notify-to", default="", help="结果回执会话：private:<qq> / group:<gid>"
    )
    args = parser.parse_args(argv)

    started = time.time()
    base = {"notify_to": args.notify_to, "started_at": started}

    lock = acquire_lock()
    if lock is None:
        log("已有更新在运行，拒绝")
        # 不要覆盖正在进行的终态：那会让新进程误以为这次更新被拒
        if str(read_state().get("status") or "") not in {"restarting", "rolling_back"}:
            write_state({**base, "status": "refused", "reason": "已有更新在运行"})
        return 3

    ok, reason = preflight()
    if not ok:
        log(f"拒绝更新：{reason}")
        write_state({**base, "status": "refused", "reason": reason})
        return 2

    prev = git("rev-parse", "HEAD").stdout.strip()
    if len(prev) != 40:
        log("拿不到当前提交")
        write_state({**base, "status": "failed", "reason": "拿不到当前提交"})
        return 1

    log(f"fetch {REMOTE}/{BRANCH}")
    proc = git("fetch", "--prune", REMOTE, timeout=180)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-_ROLLBACK_TAIL:]
        write_state({**base, "status": "failed", "reason": f"git fetch 失败：{tail}"})
        return 1

    target = git("rev-parse", f"{REMOTE}/{BRANCH}").stdout.strip()
    if len(target) != 40:
        write_state(
            {**base, "status": "failed", "reason": f"解析 {REMOTE}/{BRANCH} 失败"}
        )
        return 1
    if target == prev:
        log("已是最新")
        write_state(
            {
                **base,
                "status": "up_to_date",
                "from": prev[:12],
                "to": prev[:12],
                "finished_at": time.time(),
            }
        )
        return 0

    dirty = dirty_paths()
    if dirty:
        incoming = git(
            "-c", "core.quotepath=false", "diff", "--name-only", f"{prev}..{target}"
        ).stdout.splitlines()
        overlap = conflicts_with_incoming(
            dirty, [p.strip() for p in incoming if p.strip()]
        )
        if overlap:
            reason = "本次上游会覆盖运行态文件，先手工处理：" + ", ".join(overlap[:10])
            log(reason)
            write_state(
                {
                    **base,
                    "status": "failed",
                    "from": prev[:12],
                    "to": target[:12],
                    "reason": reason,
                }
            )
            return 1
        log(f"运行态有 {len(dirty)} 处改动，上游未触碰，继续")

    log(f"更新 {prev[:12]} -> {target[:12]}")
    proc = git("merge", "--ff-only", f"{REMOTE}/{BRANCH}", timeout=120)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-_ROLLBACK_TAIL:]
        write_state(
            {
                **base,
                "status": "failed",
                "from": prev[:12],
                "to": target[:12],
                "reason": f"合并失败（未重启）：{tail}",
            }
        )
        return 1

    ok, reason = _sync_and_smoke()
    if not ok:
        log(f"依赖/冒烟失败，回滚：{reason}")
        write_state(
            {
                **base,
                "status": "rolling_back",
                "from": prev[:12],
                "to": target[:12],
                "reason": reason,
            }
        )
        rb_ok, rb_reason = _rollback(prev)
        write_state(
            {
                **base,
                "status": "rolled_back" if rb_ok else "failed",
                "from": prev[:12],
                "to": target[:12],
                "reason": reason if rb_ok else f"{reason}；回滚失败：{rb_reason}",
                "finished_at": time.time(),
            }
        )
        return 0 if rb_ok else 1

    write_state(
        {
            **base,
            "status": "restarting",
            "from": prev[:12],
            "to": target[:12],
        }
    )
    ok, reason = _restart_and_wait(target[:12])
    if ok:
        log("更新成功")
        write_state(
            {
                **base,
                "status": "ok",
                "from": prev[:12],
                "to": target[:12],
                "finished_at": time.time(),
            }
        )
        return 0

    log(f"启动校验失败，回滚：{reason}")
    write_state(
        {
            **base,
            "status": "rolling_back",
            "from": prev[:12],
            "to": target[:12],
            "reason": reason,
        }
    )
    rb_ok, rb_reason = _rollback(prev)
    write_state(
        {
            **base,
            "status": "rolled_back" if rb_ok else "failed",
            "from": prev[:12],
            "to": target[:12],
            "reason": reason if rb_ok else f"{reason}；回滚失败：{rb_reason}",
            "finished_at": time.time(),
        }
    )
    return 0 if rb_ok else 1


if __name__ == "__main__":
    sys.exit(main())
