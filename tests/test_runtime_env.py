"""运行环境识别（agentcore/runtime_env.py）：容器判定与宿主机专属命令文案。

存在的理由：容器部署下 systemctl/journalctl/dmesg 与 reboot 的 C 步外部命令
会失效，若不识别，表现是「命令不在系统中」这类误导错误——模型会换个参数重试，
白烧一轮 token。这里锁死判定本身与「容器内必须给出明确文案」的语义。

判定只读 /proc/self/cgroup 与 /.dockerenv，测试用 monkeypatch 替换
``cgroup_text``/``_DOCKERENV_PATH`` 两个注入点，不依赖真实运行环境。
"""

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
