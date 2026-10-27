"""运行环境识别（agentcore/runtime_env.py）：容器判定与宿主机专属命令文案。

存在的理由：容器部署下 systemctl/journalctl/dmesg 与 reboot 的 C 步外部命令
会失效，若不识别，表现是「命令不在系统中」这类误导错误——模型会换个参数重试，
白烧一轮 token。这里锁死判定本身与「容器内必须给出明确文案」的语义。

判定只读 /proc/self/cgroup 与 /.dockerenv，测试用 monkeypatch 替换
``cgroup_text``/``_DOCKERENV_PATH`` 两个注入点，不依赖真实运行环境。
"""

import pytest

import agentcore.runtime_env as E


def test_cgroup_v1_docker(monkeypatch):
    monkeypatch.setattr(E, "cgroup_text", lambda: "4:cpu:/docker/abc123def456\n")
    assert E.container_id() == "abc123def456"
    assert E.in_container()


def test_cgroup_v2_docker_scope(monkeypatch):
    monkeypatch.setattr(E, "cgroup_text", lambda: "0::/docker/0123456789abcdef.scope\n")
    assert E.container_id() == "0123456789abcdef"
    assert E.in_container()


def test_non_container_cgroup(monkeypatch):
    monkeypatch.setattr(
        E, "cgroup_text", lambda: "0::/user.slice/user-1000.slice/session-3.scope\n"
    )
    monkeypatch.setattr(E, "_DOCKERENV_PATH", "/nonexistent/.dockerenv")
    assert E.container_id() == ""
    assert not E.in_container()


def test_dockerenv_fallback(monkeypatch, tmp_path):
    """cgroup 认不出来（部分 containerd 形态）时靠 /.dockerenv 兜底。"""
    monkeypatch.setattr(E, "cgroup_text", lambda: "")
    marker = tmp_path / "dockerenv"
    marker.write_text("", encoding="utf-8")
    monkeypatch.setattr(E, "_DOCKERENV_PATH", str(marker))
    assert E.in_container()


# M3（REVIEW-4d5c1b8..63f9444）：2026-10-26 提取 container_id() 时把「取 /docker/
# 之后第一段」改成「取末段路径」，嵌套形态从对变错；且 Docker 在 cgroup v2 上默认
# cgroupns=private，容器内 cgroup 只剩 0::/，两个自保护通道一起失效——而原有测试
# 恰好只覆盖 v1 平铺/v2 scope/混合三种，Docker 的默认形态不在测试集里。
_NESTED_AND_DEFAULT_FORMS = [
    # (cgroup 文本, 期望 container_id)
    ("4:cpu:/docker/abc123def456", "abc123def456"),
    ("4:cpu:/docker/abc123def456/init", "abc123def456"),
    (
        "0::/docker/abc123def456/system.slice/agent-demo.service",
        "abc123def456",
    ),
    ("0::/system.slice/docker-abc123def456.scope", "abc123def456"),
    ("0::/system.slice/docker-abc123def456.scope/init", "abc123def456"),
    ("0::/docker/abc123def456.scope", "abc123def456"),
    # 非容器形态必须解析不出
    ("0::/user.slice/user-1000.slice/session-3.scope", ""),
    ("0::/", ""),
    ("", ""),
]


@pytest.mark.parametrize("cgroup,want", _NESTED_AND_DEFAULT_FORMS)
def test_container_id_covers_nested_and_default_forms(monkeypatch, cgroup, want):
    """嵌套 cgroup 与 Docker 默认（cgroupns=private）形态都要解析正确。"""
    monkeypatch.setattr(E, "cgroup_text", lambda c=cgroup: c)
    monkeypatch.setattr(E, "_DOCKERENV_PATH", "/nonexistent/.dockerenv")
    assert E.container_id() == want


def test_cgroupns_private_falls_back_to_hostname(monkeypatch):
    """cgroupns=private 下 cgroup 只剩 0::/，改用 Docker 默认主机名（短 id）。

    不兜这一下的话 docker_ctrl 的「拒操作自身容器」在 Docker 的默认配置下
    整体失效——而 superuser+medium 下停掉自身容器会跳过优雅停机、丢在途消息。
    """
    monkeypatch.setattr(E, "cgroup_text", lambda: "0::/\n")
    monkeypatch.setattr(E, "_DOCKERENV_PATH", "/dev/null")  # /.dockerenv 存在
    monkeypatch.setattr(E.socket, "gethostname", lambda: "abc123def456")
    assert E.container_id() == "abc123def456"
    assert E.in_container()


def test_hostname_fallback_requires_dockerenv(monkeypatch):
    """宿主机上一个恰为 12 位十六进制的主机名不得被当成自身容器。"""
    monkeypatch.setattr(E, "cgroup_text", lambda: "0::/\n")
    monkeypatch.setattr(E, "_DOCKERENV_PATH", "/nonexistent/.dockerenv")
    monkeypatch.setattr(E.socket, "gethostname", lambda: "abc123def456")
    assert E.container_id() == ""
    assert not E.in_container()


def test_hostname_fallback_rejects_non_id_hostname(monkeypatch):
    """显式设过 hostname 的容器不误判成自身容器 id。"""
    monkeypatch.setattr(E, "cgroup_text", lambda: "0::/\n")
    monkeypatch.setattr(E, "_DOCKERENV_PATH", "/dev/null")
    monkeypatch.setattr(E.socket, "gethostname", lambda: "my-bot-container")
    assert E.container_id() == ""


def test_no_proc_falls_back_to_dockerenv(monkeypatch, tmp_path):
    """/proc 不可读（极窄镜像）时不能抛异常，只按 dockerenv 判。"""
    monkeypatch.setattr(E, "cgroup_text", lambda: "")
    monkeypatch.setattr(E, "_DOCKERENV_PATH", str(tmp_path / "missing"))
    assert not E.in_container()


def test_host_only_command_set():
    """只有 systemctl/journalctl/dmesg 三个被划为宿主机专属。

    多划一个就会把容器内本来可用的命令（ps/ss/hostname/top…）误挡，少划一个
    就会漏放行——所以精确断言集合本身。
    """
    assert E.is_host_only_command("systemctl")
    assert E.is_host_only_command("journalctl")
    assert E.is_host_only_command("dmesg")
    for exe in ("ps", "ss", "hostname", "top", "df", "git", "curl", "find"):
        assert not E.is_host_only_command(exe), exe


def test_hints_are_self_explanatory():
    """文案必须自解释：说清「容器里没有 + 该看哪里」，而不是「命令不存在」。

    后者会让模型以为环境坏了并重试——这正是加本模块要消除的行为。
    """
    for exe in ("systemctl", "journalctl", "dmesg"):
        hint = E.host_only_hint(exe)
        assert "容器" in hint, exe
        assert "不存在" not in hint, exe
    # 各命令的替代指引不同（systemctl→restart 策略、journalctl→log_tail、
    # dmesg→宿主机），不能是同一句通用文案
    assert len({E.host_only_hint(e) for e in ("systemctl", "journalctl", "dmesg")}) == 3
    assert "restart" in E.host_only_hint("systemctl")
    assert "log_tail" in E.host_only_hint("journalctl")
    assert "宿主机" in E.host_only_hint("dmesg")


def test_hint_unknown_command_has_generic_text():
    assert "容器" in E.host_only_hint("something-else")
