"""运行版本指纹：当前 git 提交与包版本。

两个用途，都不能省：

- ``/update`` 与 ``/update status`` 要能回答「现在跑的是哪个提交」；
- 更新脚本靠 ``data/state/boot.json`` 里的 ``sha`` 判定「新进程真的起来了」，
  没起来就回滚。这条链路比看 PID 可靠——``/reboot`` 走 ``os.execve``，PID 不变，
  「进程启动时间」根本区分不了更没更新。

**git 不可用必须降级为空串**：release 包、容器镜像、没装 git 的机器都要能启动，
版本指纹缺失不是启动失败的理由。所以这里全部走 ``subprocess`` 且吞异常。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_cache: dict[str, str] = {}


def _git(*args: str, timeout: float = 2.0) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def git_sha() -> str:
    """当前提交的短 sha（12 位）；不可用时返回空串。"""
    if "sha" not in _cache:
        _cache["sha"] = _git("rev-parse", "--short=12", "HEAD")
    return _cache["sha"]


def git_branch() -> str:
    """当前分支名；detached HEAD 或非 git 目录返回空串。"""
    if "branch" not in _cache:
        _cache["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD")
    return _cache["branch"]


def version() -> str:
    """包版本（pyproject 的 ``version``）；取不到返回空串。"""
    if "version" not in _cache:
        try:
            from importlib.metadata import version as _dist_version

            _cache["version"] = _dist_version("agent-demo")
        except Exception:
            _cache["version"] = ""
    return _cache["version"]
