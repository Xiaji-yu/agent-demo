"""进程出生环境快照：reboot 的 re-exec 只带这份 environ（bot.py 启动时调用）。

为什么需要：``bot.py`` 的 ``load_dotenv()`` 是 ``override=False``——它把 .env
的值**注入** os.environ，但不覆盖已存在的键。reboot 的 B 步 re-exec 原来直接
``os.execv`` 自替换，用**当前** environ 启动新进程：上一轮注入的旧 .env 值被
原样带过去，新进程的 load_dotenv 再也刷不掉 →「改 .env + /reboot」静默不生效
（2026-10-12 线上事故：ALLOWED_GROUPS 新加白名单的群，/reboot 后戳一戳无响应——
``is_strict_allowed`` 读的还是进程里的旧白名单；同群聊天正常只是因为 superuser
走 ``is_allowed`` 无条件放行，把问题盖住了）。

修法：``prime()`` 把「抓出生快照 → load_dotenv」的顺序**写死在一起**（顺序反了
快照就混进 .env 派生值，而这种错误在调用方单元测试里看不出来）；reboot 的
re-exec 改用 ``os.execve`` 只带出生快照——.env 派生值全部丢弃，由新进程从文件
重新读。优先级模型不变：真实环境变量（systemd EnvironmentFile / shell 导出）
本来就在快照里，load_dotenv 不会覆盖它们；web 受控回写只改 .env 文件本身
（``config_write``），重新读即拿到新值。

未捕获快照时（测试、或未走 bot.py 启动路径的调用方）:func:`pristine_environ`
返回 None，reboot 退化为原来的 ``os.execv``（继承当前 environ）——行为与修复前
一致。
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# None = 尚未捕获。模块 import 不产生副作用，捕获时机由 prime() 决定。
_snapshot: dict[str, str] | None = None


def capture() -> dict[str, str]:
    """抓取当前 environ 作为出生快照（**必须在 load_dotenv 之前**，见 :func:`prime`）。

    幂等：已抓过就沿用第一次的结果——启动路径上重复调用不会把已注入的 .env
    值混进快照。返回快照副本（调用方改动不污染全局）。
    """
    global _snapshot
    if _snapshot is None:
        _snapshot = dict(os.environ)
    return dict(_snapshot)


def prime(dotenv_path: str | os.PathLike | None = None) -> None:
    """抓出生环境快照，然后加载 .env。**bot.py 启动时调用一次。**

    两件事的顺序是本模块的核心不变量，刻意收在一个函数里而不是让调用方
    「先 capture 再 load_dotenv」——顺序写反时快照会被 .env 派生值污染，
    execv 重启仍拿旧值，且那种错误在各自为政的调用方测试里发现不了。
    ``load_dotenv`` 保持默认 ``override=False``：shell/systemd 显式导出的
    变量优先于 .env（override=True 曾把导出值覆盖掉，仓库历史明确拒绝）。
    """
    capture()
    load_dotenv(dotenv_path)


def pristine_environ() -> dict[str, str] | None:
    """出生环境快照；未捕获时返回 None（调用方按「无快照」退化）。"""
    return dict(_snapshot) if _snapshot is not None else None
