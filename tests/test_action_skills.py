"""管理员动作类 skill 的测试（tests/test_action_skills.py）。

来源：2026-10-02 管理员决策（动作类能力）+ 2026-10 权限级别制重构
（AGENT_PERMISSION_LEVEL 替代目标白名单）。安全契约（每条都有对应用例，
改坏实现必须失败）：
- 级别门：low 档动作类整体拒绝（medium+ 才注册/可用）
- 自保护：service_ctrl 拒 bot 自身 unit（cgroup 识别）；docker_ctrl 拒
  PG_CONTAINER 与 bot 自身容器
- 动作/信号是枚举；服务名/容器名/脚本名过形状正则
- proc_kill 两段确认：无 token 不杀、错 token 不杀、过期不杀；
  拒绝 pid 1 与 bot 自身；匹配数超上限拒绝
- run_build_script 只跑工作区 scripts/ 下的脚本；参数禁 shell 元字符与
  路径逃逸；输出过围栏
"""

import re

import pytest

import agentcore.skills.action_skills as A


@pytest.fixture(autouse=True)
def _clean_pending():
    A._PENDING.clear()
    yield
    A._PENDING.clear()


class _FakeProc:
    def __init__(self, out: bytes = b"ok\n"):
        self._out = out

    async def communicate(self):
        return self._out, None

    def kill(self):
        pass

    async def wait(self):
        return 0


def _patch_runs(monkeypatch, actions=None, readonlys=None):
    """替换 run_action/run_readonly，记录调用。"""
    action_calls: list = []
    readonly_calls: list = []

    async def fake_action(cmd, *, timeout=6, max_lines=20):
        action_calls.append(cmd)
        return (actions or {}).get(cmd[0], "(noop)")

    async def fake_readonly(cmd, *, timeout=6, max_lines=20):
        readonly_calls.append(cmd)
        return (readonlys or {}).get(cmd[0], "(active)")

    monkeypatch.setattr(A, "run_action", fake_action)
    monkeypatch.setattr(A, "run_readonly", fake_readonly)
    return action_calls, readonly_calls


class TestServiceCtrl:
    def test_action_enum_enforced(self):
        out = await_sync(A.service_ctrl("mask", "nginx.service"))
        assert "action" in out and "restart" in out

    def test_unit_shape_rejected(self):
        for bad in ("../evil", "a; rm -rf /", "nginx.service extra", ""):
            out = await_sync(A.service_ctrl("restart", bad))
            assert "非法字符" in out, bad

    def test_low_level_disables(self, monkeypatch):
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        out = await_sync(A.service_ctrl("restart", "nginx.service"))
        assert "权限级别" in out and "medium" in out

    def test_self_unit_refused(self, monkeypatch):
        monkeypatch.setattr(A, "_self_unit", lambda: "agent-demo.service")
        for name in ("agent-demo.service", "agent-demo"):
            out = await_sync(A.service_ctrl("restart", name))
            assert "拒绝" in out and "/reboot" in out, name

    def test_unit_shape_for_self_check_order(self, monkeypatch):
        # 自保护在形状校验之后：怪名字先进形状闸（不会把非法名当 unit 放过）
        monkeypatch.setattr(A, "_self_unit", lambda: "agent-demo.service")
        out = await_sync(A.service_ctrl("restart", "a;id"))
        assert "非法字符" in out

    @pytest.mark.asyncio
    async def test_any_unit_runs_systemctl(self, monkeypatch):
        action_calls, readonly_calls = _patch_runs(monkeypatch)
        out = await A.service_ctrl("restart", "nginx.service")
        assert ["systemctl", "restart", "nginx.service"] in action_calls
        assert ["systemctl", "is-active", "nginx.service"] in readonly_calls
        assert "is-active" in out


class TestDocker:
    @pytest.mark.asyncio
    async def test_ps_passes_fixed_args(self, monkeypatch):
        _action, readonly_calls = _patch_runs(monkeypatch)
        await A.docker_ps(all=True)
        argv = readonly_calls[0]
        assert argv[0:2] == ["docker", "ps"]
        assert "--all" in argv
        assert "--format" in argv

    def test_logs_bad_container_name(self):
        out = await_sync(A.docker_logs("a;b"))
        assert "非法字符" in out

    @pytest.mark.asyncio
    async def test_logs_lines_clamped(self, monkeypatch):
        _action, readonly_calls = _patch_runs(monkeypatch)
        await A.docker_logs("web", 9999)
        assert "--tail" in readonly_calls[0]
        assert "200" in readonly_calls[0]

    def test_low_level_disables(self, monkeypatch):
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        out = await_sync(A.docker_ctrl("stop", "web"))
        assert "权限级别" in out and "medium" in out

    def test_pg_container_refused(self, monkeypatch):
        monkeypatch.setenv("PG_CONTAINER", "agent-demo-db-1")
        out = await_sync(A.docker_ctrl("stop", "agent-demo-db-1"))
        assert "拒绝" in out and "数据库" in out

    def test_self_container_refused_by_id_and_prefix(self, monkeypatch):
        cid = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
        monkeypatch.setattr(A, "_self_container_id", lambda: cid)
        for name in (cid, cid[:12]):
            out = await_sync(A.docker_ctrl("stop", name))
            assert "拒绝" in out and "/reboot" in out, name

    @pytest.mark.asyncio
    async def test_ctrl_other_container_runs(self, monkeypatch):
        action_calls, _ro = _patch_runs(monkeypatch)
        out = await A.docker_ctrl("restart", "db")
        assert ["docker", "restart", "db"] in action_calls
        assert "docker restart db" in out


class TestProcKill:
    def _patch_targets(self, monkeypatch, pids):
        monkeypatch.setattr(A, "_kill_targets", lambda pattern: list(pids))

    def test_low_level_disables(self, monkeypatch):
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        out = await_sync(A.proc_kill("vite", "TERM", "", "u1"))
        assert "权限级别" in out and "medium" in out

    def test_any_pattern_enters_confirm_flow(self, monkeypatch):
        # pattern 白名单已随级别制移除：任意 pattern 都进入两段确认（不直接杀）
        self._patch_targets(monkeypatch, [111])
        killed: list = []
        monkeypatch.setattr(A.os, "kill", lambda p, s: killed.append((p, s)))
        out = await_sync(A.proc_kill("sshd", "TERM", "", "u1"))
        assert "确认码" in out and killed == []

    def test_signal_enum_enforced(self):
        out = await_sync(A.proc_kill("vite", "HUP", "", "u1"))
        assert "signal" in out and "TERM" in out and "KILL" in out

    @pytest.mark.asyncio
    async def test_first_call_returns_token_without_killing(self, monkeypatch):
        self._patch_targets(monkeypatch, [111, 222])
        killed: list = []
        monkeypatch.setattr(A.os, "kill", lambda p, s: killed.append((p, s)))
        out = await A.proc_kill("vite", "TERM", "", "u1")
        assert "确认码" in out and "111" in out and "222" in out
        assert killed == [], "无确认码不得杀进程"
        assert len(A._PENDING) == 1

    @pytest.mark.asyncio
    async def test_wrong_token_refused(self, monkeypatch):
        self._patch_targets(monkeypatch, [111])
        killed: list = []
        monkeypatch.setattr(A.os, "kill", lambda p, s: killed.append((p, s)))
        await A.proc_kill("vite", "TERM", "", "u1")
        out = await A.proc_kill("vite", "TERM", "deadbeef", "u1")
        assert "不正确或已过期" in out
        assert killed == []
        assert A._PENDING == {}

    @pytest.mark.asyncio
    async def test_expired_token_refused(self, monkeypatch):
        self._patch_targets(monkeypatch, [111])
        monkeypatch.setattr(A.os, "kill", lambda p, s: None)
        ticks = iter([0.0, 0.0, 9999.0])
        monkeypatch.setattr(A, "_now", lambda: next(ticks, 9999.0))
        await A.proc_kill("vite", "TERM", "", "u1")
        out = await A.proc_kill("vite", "TERM", "x", "u1")
        assert "不正确或已过期" in out

    @pytest.mark.asyncio
    async def test_valid_token_kills(self, monkeypatch):
        self._patch_targets(monkeypatch, [111, 222])
        killed: list = []
        monkeypatch.setattr(A.os, "kill", lambda p, s: killed.append((p, s)))
        first = await A.proc_kill("vite", "TERM", "", "u1")
        token = re.search(r"确认码 ([0-9a-f]+)", first).group(1)
        out = await A.proc_kill("vite", "TERM", token, "u1")
        assert killed == [(111, 15), (222, 15)]
        assert "已发送 SIGTERM" in out
        assert A._PENDING == {}, "执行后必须清掉待确认项"

    @pytest.mark.asyncio
    async def test_kill_signal_nine(self, monkeypatch):
        self._patch_targets(monkeypatch, [111])
        killed: list = []
        monkeypatch.setattr(A.os, "kill", lambda p, s: killed.append((p, s)))
        first = await A.proc_kill("vite", "KILL", "", "u1")
        token = re.search(r"确认码 ([0-9a-f]+)", first).group(1)
        await A.proc_kill("vite", "KILL", token, "u1")
        assert killed == [(111, 9)]

    def test_kill_targets_excludes_pid1_and_self(self, monkeypatch):
        monkeypatch.setattr(A, "_scan_pids", lambda p: [1, 2, A._SELF_PID, 3, 1])
        assert A._kill_targets("x") == [2, 3]

    @pytest.mark.asyncio
    async def test_too_many_pids_refused(self, monkeypatch):
        self._patch_targets(monkeypatch, list(range(100, 111)))
        killed: list = []
        monkeypatch.setattr(A.os, "kill", lambda p, s: killed.append((p, s)))
        out = await A.proc_kill("vite", "TERM", "", "u1")
        assert "上限" in out
        assert killed == []
        assert A._PENDING == {}

    @pytest.mark.asyncio
    async def test_no_pids_message(self, monkeypatch):
        self._patch_targets(monkeypatch, [])
        out = await A.proc_kill("vite", "TERM", "", "u1")
        assert "没有匹配" in out

    @pytest.mark.asyncio
    async def test_permission_error_reported(self, monkeypatch):
        self._patch_targets(monkeypatch, [111])

        def deny(p, s):
            raise PermissionError(p)

        monkeypatch.setattr(A.os, "kill", deny)
        first = await A.proc_kill("vite", "TERM", "", "u1")
        token = re.search(r"确认码 ([0-9a-f]+)", first).group(1)
        out = await A.proc_kill("vite", "TERM", token, "u1")
        assert "无权限" in out


class TestRunBuildScript:
    def test_name_shape_rejected(self, monkeypatch):
        for bad in ("../etc/x", "a/b", "build;rm", ""):
            out = await_sync(A.run_build_script(bad))
            assert "非法字符" in out, bad

    def test_low_level_disables(self, monkeypatch):
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        out = await_sync(A.run_build_script("build"))
        assert "权限级别" in out and "medium" in out

    def test_script_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)
        out = await_sync(A.run_build_script("build"))
        assert "找不到脚本" in out

    @pytest.mark.asyncio
    async def test_args_shell_meta_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "build.sh").write_text("#!/bin/sh\nexit 0\n")
        for bad in ("x; rm -rf /", "$(id)", "a `id`", "a | b"):
            out = await A.run_build_script("build", bad)
            assert "shell 元字符" in out, bad

    @pytest.mark.asyncio
    async def test_args_dotdot_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "build.sh").write_text("#!/bin/sh\nexit 0\n")
        out = await A.run_build_script("build", "../../etc/passwd")
        assert ".." in out

    @pytest.mark.asyncio
    async def test_args_absolute_escape_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "build.sh").write_text("#!/bin/sh\nexit 0\n")
        out = await A.run_build_script("build", "/etc/passwd")
        assert "工作区" in out

    @pytest.mark.asyncio
    async def test_runs_whitelisted_script_with_fenced_output(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "build.sh").write_text("#!/bin/sh\nexit 0\n")
        (tmp_path / "scripts" / "build.sh").chmod(0o755)

        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            captured["cwd"] = kwargs.get("cwd")
            return _FakeProc(b"build done\nsecond line\n")

        monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)
        out = await A.run_build_script("build", "--flag x")
        argv = captured["args"]
        assert argv[0].endswith("build.sh")
        assert "--flag" in argv and "x" in argv
        assert captured["cwd"] == str(tmp_path)
        # 输出必须过围栏（构建日志视同外部数据）
        assert "构建脚本输出开始" in out
        assert "build done" in out
        assert "构建脚本输出结束" in out

    def test_env_int_dirty_value_fallback(self, monkeypatch):
        monkeypatch.setenv("AGENT_BUILD_TIMEOUT", "abc")
        assert A._env_int("AGENT_BUILD_TIMEOUT", 300, 1, 3600) == 300
        monkeypatch.setenv("AGENT_BUILD_TIMEOUT", "99999")
        assert A._env_int("AGENT_BUILD_TIMEOUT", 300, 1, 3600) == 300
        monkeypatch.setenv("AGENT_BUILD_TIMEOUT", "42")
        assert A._env_int("AGENT_BUILD_TIMEOUT", 300, 1, 3600) == 42

    def test_self_protection_helpers(self, monkeypatch):
        # cgroup 解析：systemd unit / 容器 id（v1 与 v2 形态）/ 非 systemd 部署
        monkeypatch.setattr(
            A, "_cgroup_text", lambda: "0::/system.slice/agent-demo.service\n"
        )
        assert A._self_unit() == "agent-demo.service"
        assert A._self_container_id() == ""
        cid = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
        monkeypatch.setattr(A, "_cgroup_text", lambda: f"0::/docker/{cid}\n")
        assert A._self_unit() == ""
        assert A._self_container_id() == cid
        monkeypatch.setattr(
            A, "_cgroup_text", lambda: "12:pids:/system.slice/docker-a1b2.scope\n"
        )
        assert A._self_container_id() == "a1b2"
        monkeypatch.setattr(A, "_cgroup_text", lambda: "")
        assert A._self_unit() == "" and A._self_container_id() == ""
        # 非 systemd/非容器部署：识别不到自己 → 自保护判定恒 False（README 注明）
        assert A._unit_is_self("anything") is False
        assert A._container_is_self("anything") is False


# ---------- 同步断言小工具 ----------
def await_sync(coro):
    """在同步用例里跑协程（asyncio.run 自带独立事件循环，互不干扰）。"""
    import asyncio

    return asyncio.run(coro)


class TestHandlerAdminGate:
    """P0 回归：动作类 handler 必须二次校验 is_superuser（纵深）。

    registry 层权限曾被 config.yaml 通配符击穿（人人过闸），而本模块全部是
    有副作用的动作——单一闸门不够，与 fs/ssh 对齐。
    """

    @staticmethod
    def _reg():
        from agentcore.skills.action_skills import register_action_skills
        from agentcore.skills.registry import SkillRegistry

        reg = SkillRegistry()
        register_action_skills(reg)
        return reg

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("skill", "kwargs"),
        [
            ("service_ctrl", {"action": "restart", "unit": "nginx.service"}),
            ("docker_ps", {}),
            ("docker_logs", {"container": "web"}),
            ("docker_ctrl", {"action": "restart", "container": "web"}),
            ("proc_kill", {"pattern": "vite"}),
        ],
    )
    async def test_non_admin_gets_handler_denial(self, monkeypatch, skill, kwargs):
        monkeypatch.setenv("SUPERUSERS", "10001")
        reg = self._reg()

        class _AllowAll:
            def is_allowed(self, *a, **k):
                return True  # 模拟 registry 层被击穿：handler 必须兜住

        reg.permission_checker = _AllowAll()
        out = await reg.execute(skill, user_id="99999", **kwargs)
        assert "无权限" in out, skill

    @pytest.mark.asyncio
    async def test_run_build_script_non_admin_denied(self, monkeypatch):
        # run_build_script 的参数名与 execute(name) 形参冲突，直接调 handler

        monkeypatch.setenv("SUPERUSERS", "10001")
        reg = self._reg()
        out = await reg.skills["run_build_script"].handler(
            name="build", user_id="99999"
        )
        assert "无权限" in out

    @pytest.mark.asyncio
    async def test_superuser_passes_handler_gate(self, monkeypatch):
        from agentcore.skills.action_skills import register_action_skills
        from agentcore.skills.permissions import PermissionChecker
        from agentcore.skills.registry import SkillRegistry

        monkeypatch.setenv("SUPERUSERS", "10001")
        reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"10001"}))
        register_action_skills(reg)
        # 拿只读的 docker_ps 验证闸门放行（本机无 docker 时返回提示而非拒绝）
        out = await reg.execute("docker_ps", user_id="10001")
        assert "无权限" not in out


class TestContainerSelfProtectionNames:
    """M6（REVIEW-de09478..workdir）：自身容器自保护补**名字**通道。

    cgroup 只给容器 id，而 docker ps 语境下引用容器**名字**是主形态——只认 id
    时 `docker_ctrl("stop", "<bot容器名>")` 穿过自保护（compose 部署实害；
    本机宿主部署无症状，属保护承诺与实现不符的条件性缺陷）。
    """

    @pytest.mark.asyncio
    async def test_name_channel_refuses_self(self, monkeypatch):
        cid = "abcdef0123456789"
        monkeypatch.setattr(A, "_self_container_id", lambda: cid)
        monkeypatch.setattr(
            A, "run_readonly", _fake_self_names(cid, "agent-demo-bot-1")
        )
        action_calls: list = []
        monkeypatch.setattr(A, "run_action", _record(action_calls))
        _fresh_names_cache()
        out = await A.docker_ctrl("stop", "agent-demo-bot-1")
        assert "拒绝" in out and "/reboot" in out, out
        assert action_calls == [], "自身容器不得真的执行 docker"

    @pytest.mark.asyncio
    async def test_name_channel_multi_name_alias(self, monkeypatch):
        cid = "abcdef0123456789"
        monkeypatch.setattr(A, "_self_container_id", lambda: cid)
        # compose 场景：Names 可为逗号分隔多名字
        monkeypatch.setattr(
            A, "run_readonly", _fake_self_names(cid, "compose-project-bot-1,bot")
        )
        _fresh_names_cache()
        for name in ("compose-project-bot-1", "bot"):
            out = await A.docker_ctrl("restart", name)
            assert "拒绝" in out, name

    @pytest.mark.asyncio
    async def test_other_container_name_still_allowed(self, monkeypatch):
        cid = "abcdef0123456789"
        monkeypatch.setattr(A, "_self_container_id", lambda: cid)
        monkeypatch.setattr(
            A, "run_readonly", _fake_self_names(cid, "agent-demo-bot-1")
        )
        action_calls: list = []
        monkeypatch.setattr(A, "run_action", _record(action_calls))
        _fresh_names_cache()
        await A.docker_ctrl("restart", "homeassistant")
        assert action_calls == [["docker", "restart", "homeassistant"]]

    @pytest.mark.asyncio
    async def test_lookup_failure_fails_open_to_id_channel(self, monkeypatch):
        """守卫：docker ps 不可用（无 CLI/daemon 挂了/不在容器）时 fail-open 回退
        仅 id 通道——与旧行为一致，不把自保护退化成全员拒绝。"""
        cid = "abcdef0123456789"
        monkeypatch.setattr(A, "_self_container_id", lambda: cid)

        async def boom(*a, **kw):
            raise OSError("docker daemon unreachable")

        monkeypatch.setattr(A, "run_readonly", boom)
        action_calls: list = []
        monkeypatch.setattr(A, "run_action", _record(action_calls))
        _fresh_names_cache()
        await A.docker_ctrl("restart", "other")
        assert action_calls == [["docker", "restart", "other"]]
        # id 形态仍被认（fail-open 只丢名字通道）
        out = await A.docker_ctrl("stop", cid)
        assert "拒绝" in out

    @pytest.mark.asyncio
    async def test_not_in_container_no_lookup(self, monkeypatch):
        """非容器部署（cgroup 无 docker 段）：完全不做 docker ps 查询。"""
        monkeypatch.setattr(A, "_self_container_id", lambda: "")

        def boom(*a, **kw):
            raise AssertionError("非容器部署不应查 docker ps")

        monkeypatch.setattr(A, "run_readonly", boom)
        action_calls: list = []
        monkeypatch.setattr(A, "run_action", _record(action_calls))
        _fresh_names_cache()
        await A.docker_ctrl("restart", "whatever")
        assert action_calls == [["docker", "restart", "whatever"]]


def _fake_self_names(cid: str, names: str):
    async def fake_readonly(argv, **kw):
        return f"{cid[:12]} {names}"

    return fake_readonly


def _record(sink: list):
    async def fake_action(argv, **kw):
        sink.append(argv)
        return "ok"

    return fake_action


def _fresh_names_cache():
    """把 M6 名字缓存打到过期，避免用例间串味。"""
    A._SELF_NAMES_CACHE = (0.0, set())
