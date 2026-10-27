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
import re
import socket

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


def _strip_docker_affixes(seg: str) -> str:
    """剥掉 v2 scope 形态的 ``docker-`` 前缀与 ``.scope`` 后缀。"""
    if seg.startswith("docker-"):
        seg = seg[len("docker-") :]
    if seg.endswith(".scope"):
        seg = seg[: -len(".scope")]
    return seg


def _cgroup_container_id() -> str:
    """只从 cgroup 解析容器 id；解析不出返回空串（不看主机名）。"""
    for entry in cgroup_text().split():
        # 形态一：`/docker/<id>`（v1 平铺，或 `/docker/<id>/init` 这类嵌套）。
        # 必须取 `/docker/` 之后的**第一段**：取末段的话嵌套形态会返回 `init` /
        # `agent-demo.service`，docker_ctrl 的「拒操作自身容器」自保护静默失效
        # （M3 回归，REVIEW-4d5c1b8..63f9444——2026-10-26 提取本函数时引入）。
        if "/docker/" in entry:
            seg = _strip_docker_affixes(entry.split("/docker/", 1)[1].split("/")[0])
            if seg and seg != "docker":
                return seg
        # 形态二：v2 `…/docker-<id>.scope`——末段带前缀与后缀；也可能是嵌套子路径
        # （`/system.slice/docker-<id>.scope/init`），故扫路径里任意一段
        for part in entry.split("/"):
            if part.startswith("docker-"):
                seg = _strip_docker_affixes(part)
                if seg:
                    return seg
    return ""


def _has_dockerenv() -> bool:
    try:
        with open(_DOCKERENV_PATH, encoding="utf-8"):
            return True
    except OSError:
        return False


# Docker 默认主机名 = 容器短 id（12 位十六进制）；compose 不显式设 hostname 即如此
_HOSTNAME_ID_RE = re.compile(r"[0-9a-f]{12}")


def _hostname_container_id() -> str:
    """cgroupns=private 下的容器 id 兜底：读 Docker 默认主机名。

    Docker 在 cgroup v2 上默认 `cgroupns=private`，容器内 `/proc/self/cgroup` 只剩
    `0::/`——cgroup 通道整体失效，`docker_ctrl` 的自身容器自保护（id 与名字两个
    通道）随之失效。主机名只在**已判定为容器**时采信，避免宿主机上一个恰为 12 位
    十六进制的主机名被误判成自身容器（M3，REVIEW-4d5c1b8..63f9444）。
    """
    try:
        name = socket.gethostname()
    except OSError:
        return ""
    return name if _HOSTNAME_ID_RE.fullmatch(name) else ""


def container_id() -> str:
    """本进程所在容器 id；非容器部署返回空串。

    覆盖的形态：

    - cgroup v1：``4:cpu:/docker/<id>``（以及 ``/docker/<id>/init`` 等嵌套）
    - cgroup v2：``0::/system.slice/docker-<id>.scope``
    - 混合变体：``0::/docker/<id>.scope``
    - cgroupns=private：``0::/``（cgroup 无信息，退回 Docker 默认主机名）

    **`.scope` 必须剥**：原 ``action_skills._self_container_id`` 漏了混合变体的
    ``.scope``，v2 下返回 ``<id>.scope`` 而真实 id 是 ``<id>``，docker_ctrl 的
    「拒操作自身容器」自保护会静默失效（2026-10-26 提取本函数时修掉）。
    **嵌套必须取第一段**：同轮引入的反向回归，见 ``_cgroup_container_id``。
    """
    cid = _cgroup_container_id()
    if cid:
        return cid
    if not _has_dockerenv():
        return ""
    return _hostname_container_id()


def in_container() -> bool:
    """是否运行在容器内。"""
    if _cgroup_container_id():
        return True
    return _has_dockerenv()


def host_only_hint(exe: str) -> str:
    """宿主机专属命令在容器内的说明文案（未知命令给通用文案）。"""
    return _HINTS.get(exe, "容器部署环境：该命令依赖宿主机设施，容器内不可用。")


def is_host_only_command(exe: str) -> bool:
    """是否是「只在宿主机有意义」的白名单命令（容器内一律拒）。"""
    return exe in _HINTS
