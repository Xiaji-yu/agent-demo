"""运行环境识别：当前进程跑在容器里还是宿主机。

为什么需要：容器部署下有一批「宿主机专属」能力自然失效——systemd 的
``systemctl``/``journalctl``、内核环形缓冲 ``dmesg``、以及 reboot 执行链的 C 步
（``AGENT_REBOOT_CMD`` 的 systemctl/docker/supervisorctl 外部命令）。这些能力
失效时若不识别，表现是「命令不在系统中：systemctl」这类误导性错误：模型会以为
环境缺了东西、换个参数再试，白烧一轮 token 还答不到点上。给单一判定源，让各
调用点能明确说出「这是容器，本来就没有」。

判定优先级：

1. ``/proc/self/cgroup``——v1 形态 ``/docker/<id>``、v2 形态 ``0::/docker-<id>.scope``；
2. ``/.dockerenv``——docker 必建，但 compose/containerd/podman 变体不一定有，故只作兜底。

与 ``agentcore/skills/action_skills.py`` 的自保护识别（认出「自己的容器」以免
docker_ctrl 把自己停机）同源：那处的 ``_self_container_id`` 已改为委托
:func:`container_id`，不再各自解析 cgroup。
"""

from __future__ import annotations

import functools

_CGROUP_PATH = "/proc/self/cgroup"
_DOCKERENV_PATH = "/.dockerenv"

# 容器专属说明文案：进 prompt 的工具结果，必须自解释——说清「容器里没有这个、
# 该看哪里」，而不是「命令不存在」这种会让模型重试的表述。
_HINTS = {
    "systemctl": (
        "容器部署环境：没有 systemd，systemctl 不可用。"
        "本服务由容器的 restart 策略托管（重启用 /reboot 或 docker compose restart）；"
        "要看本服务状态用 proc_detail / port_check / log_tail。"
    ),
    "journalctl": (
        "容器部署环境：没有 journald，journalctl 不可用。"
        "日志已按天落盘在 data/logs/，用 log_tail 查看（AGENT_LOG_DIR 可改目录）。"
    ),
    "dmesg": (
        "容器部署环境：读不到内核环形缓冲（dmesg 需要特权）。"
        "要看宿主机内核信息应登录宿主机执行；容器内可用 proc_detail / disk_usage / lscpu。"
    ),
}


@functools.lru_cache(maxsize=1)
def cgroup_text() -> str:
    try:
        with open(_CGROUP_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def container_id() -> str:
    """本进程所在容器 id；非容器部署返回空串。

    覆盖三种实测形态：

    - cgroup v1：``4:cpu:/docker/<id>``（末段就是 id）
    - cgroup v2：``0::/system.slice/docker-<id>.scope``（末段带 ``docker-`` 前缀
      与 ``.scope`` 后缀，都要剥）
    - 混合变体：``0::/docker/<id>.scope``（部分 containerd 形态）

    **后缀必须剥**：原 ``action_skills._self_container_id`` 漏了混合变体的
    ``.scope``，v2 下返回 ``<id>.scope`` 而真实 id 是 ``<id>``，docker_ctrl 的
    「拒操作自身容器」自保护会静默失效（2026-10-26 提取本函数时修掉，回归见
    tests/test_runtime_env.py）。
    """
    for entry in cgroup_text().split():
        last = entry.rsplit("/", 1)[-1]
        candidate = last
        if candidate.endswith(".scope"):
            candidate = candidate[: -len(".scope")]
        if candidate.startswith("docker-"):
            candidate = candidate[len("docker-") :]
        if not candidate or candidate == "docker":
            continue
        if last.startswith("docker-") or "/docker/" in entry:
            return candidate
    return ""


def in_container() -> bool:
    """是否运行在容器内。"""
    if container_id():
        return True
    try:
        with open(_DOCKERENV_PATH, encoding="utf-8"):
            return True
    except OSError:
        return False


def host_only_hint(exe: str) -> str:
    """宿主机专属命令在容器内的说明文案（未知命令给通用文案）。"""
    return _HINTS.get(exe, "容器部署环境：该命令依赖宿主机设施，容器内不可用。")


def is_host_only_command(exe: str) -> bool:
    """是否是「只在宿主机有意义」的白名单命令（容器内一律拒）。"""
    return exe in _HINTS
