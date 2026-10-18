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
    async def test_stats_no_stream_and_format(self, monkeypatch):
        """docker stats 默认是流式刷新——argv 必须写死 --no-stream 取快照，
        否则 20s 级超时下只会拿到半行表头。变异：删掉 --no-stream 必须失败。"""
        _action, readonly_calls = _patch_runs(monkeypatch)
        await A.docker_stats()
        argv = readonly_calls[0]
        assert argv[0:3] == ["docker", "stats", "--no-stream"]
        assert "--format" in argv
        assert "--all" in argv  # 无参时含已停止容器（便于对账）

    @pytest.mark.asyncio
    async def test_stats_named_container_skips_all(self, monkeypatch):
        _action, readonly_calls = _patch_runs(monkeypatch)
        await A.docker_stats("snowluma")
        argv = readonly_calls[0]
        assert argv[-1] == "snowluma"
        assert "--no-stream" in argv
        assert "--all" not in argv, "具名容器与 --all 不同时用（目标集冲突）"

    def test_stats_bad_container_name(self):
        for bad in ("a;b", "../evil", "web extra", "a$(id)"):
            out = await_sync(A.docker_stats(bad))
            assert "非法字符" in out, bad

    @pytest.mark.asyncio
    async def test_stats_available_at_low_level(self, monkeypatch):
        """只读面：low 档也要能查资源用量（与 docker_ps/logs 同档）。"""
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        _action, readonly_calls = _patch_runs(monkeypatch)
        await A.docker_stats()
        assert readonly_calls[0][0:2] == ["docker", "stats"]

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

    def test_scan_pids_survives_missing_proc(self, monkeypatch):
        """平台移植（端口性评审 P2）：无 /proc 的平台（macOS/Windows）不得把
        FileNotFoundError 抛进 handler——扫描返回空、调用方走「无匹配」分支。
        与 scripts/backup_db.py 的机器人探测同口径（那里早有 try/except）。"""

        def _boom(_path):
            raise FileNotFoundError("/proc")

        monkeypatch.setattr(A.os, "listdir", _boom)
        assert A._scan_pids("anything") == []

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

    @pytest.mark.asyncio
    async def test_registry_execute_with_name_kwarg(self, monkeypatch, tmp_path):
        """经 registry.execute 走 engine 同款调用路径（REVIEW-6745556..d730827）。

        技能参数叫 ``name`` 时曾与 ``SkillRegistry.execute(name, ...)`` 的形参
        冲突（TypeError: got multiple values），异常发生在函数绑定阶段、
        handler 内的 try 兜不住 → run_build_script 在真实链路上永远执行失败，
        而直调 handler 的用例全部绿。修复：name 改 positional-only（/）。
        本用例锁「**kwargs 里的 name 正常落进 handler」。
        """
        from agentcore.skills.permissions import PermissionChecker
        from agentcore.skills.registry import SkillRegistry

        monkeypatch.setenv("SUPERUSERS", '["10000"]')
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "medium")
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "build.sh").write_text("#!/bin/sh\nexit 0\n")
        (tmp_path / "scripts" / "build.sh").chmod(0o755)

        async def fake_exec(*args, **kwargs):
            return _FakeProc(b"ok\n")

        monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)
        reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"10000"}))
        A.register_action_skills(reg)
        out = await reg.execute(
            "run_build_script", user_id="10000", name="build", args=""
        )
        assert "ok" in out and "执行失败" not in out, out

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


class TestNpmUpdate:
    """npm 全局包更新（登记表制，2026-10-18 管理员决策）。

    安全契约（每条都有对应用例，改坏实现必须失败）：
    - 包名**精确命中** AGENT_NPM_GLOBAL_PACKAGES 才执行（LLM 不能拼包名/flag；
      注入尝试如 "pkg --preinstall=x" / "pkg; rm -rf /" 天然不在表内）
    - 空登记表 = 功能关闭（handler 拒 + 技能不注册，schema 不可见）
    - low 档不注册；handler 内 is_superuser 二次校验
    - dry_run 布尔归一：LLM 传字符串 "false"/"0" 不得变成预演
    - 真实 HOME（尊重 ~/.npmrc prefix）但**不泄漏 LLM key**（最小 env）
    - 输出过围栏（npm 输出含 GitHub/registry 内容）

    变异复核：`pkg not in allowed` 改恒真放行 → 未登记者例必红；
    env 改 env.update(os.environ) → key 泄漏例必红；去掉字符串归一 →
    dry_run="false" 例必红。
    """

    @pytest.mark.asyncio
    async def test_unregistered_package_rejected(self, monkeypatch):
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "deepseek-ai/dsh")
        spawned: list = []

        async def fake_exec(*args, **kwargs):
            spawned.append(args)
            return _FakeProc(b"ok\n")

        monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)
        for bad in (
            "evil-pkg",
            "deepseek-ai/other",
            "deepseek-ai/dsh --preinstall=x",
            "dsh; rm -rf /",
            "deepseek-ai/dsh extra",
            "DEEPSEEK-AI/DSH",  # 精确匹配：大小写敏感
            "",
        ):
            out = await A.npm_update(bad)
            assert "不在登记表" in out, bad
        # 拒绝路径一个都不许 spawn（变异复核：恒真放行时本断言即红）
        assert not spawned, "拒绝路径不得 spawn npm"

    def test_empty_registry_fails_closed(self, monkeypatch):
        monkeypatch.delenv("AGENT_NPM_GLOBAL_PACKAGES", raising=False)
        out = await_sync(A.npm_update("deepseek-ai/dsh"))
        assert "功能关闭" in out

    def test_registry_parsing(self, monkeypatch):
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", " a , b ,,c ")
        assert A._npm_global_packages() == {"a", "b", "c"}
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "   ")
        assert A._npm_global_packages() == set()

    @pytest.mark.asyncio
    async def test_registered_runs_exact_cmd_minimal_env_fenced(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "deepseek-ai/dsh")
        monkeypatch.setenv("LLM_API_KEY", "sk-must-not-leak")
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)

        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env") or {}
            captured["cwd"] = kwargs.get("cwd")
            return _FakeProc(b"updated 1 package in 3s\n")

        monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)
        out = await A.npm_update("deepseek-ai/dsh", uid="10001")
        assert captured["args"] == ("npm", "update", "-g", "deepseek-ai/dsh")
        # 最小环境：真实 HOME 在（尊重 ~/.npmrc prefix）、LLM key 不在。
        # L3（REVIEW-6745556..d730827）：白名单制锁**精确键集**——实现未来
        # 多透传 NODE_OPTIONS/npm_config_* 等环境面时本断言即红
        assert set(captured["env"]) == {"PATH", "HOME", "LANG", "LC_ALL"}
        assert captured["cwd"] == str(tmp_path)
        # 输出过围栏（npm 输出含 GitHub/registry 内容，视同外部数据）
        assert "npm 全局更新输出开始" in out
        assert "updated 1 package" in out
        assert "npm 全局更新输出结束" in out

    @pytest.mark.asyncio
    async def test_output_truncated_over_4000(self, monkeypatch, tmp_path):
        """L3：超长输出截断到 4000 字符（截断在围栏之前）。"""
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "deepseek-ai/dsh")
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)

        async def fake_exec(*args, **kwargs):
            return _FakeProc(b"x" * 5000)

        monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)
        out = await A.npm_update("deepseek-ai/dsh")
        assert "已截断至 4000 字符" in out
        assert "x" * 4000 in out
        assert "x" * 4001 not in out

    @pytest.mark.asyncio
    async def test_timeout_kills_process_group(self, monkeypatch, tmp_path):
        """L3：90s 超时杀整个进程组（start_new_session 的存在意义）。"""
        import asyncio as _asyncio

        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "deepseek-ai/dsh")
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)
        monkeypatch.setattr(A, "_NPM_UPDATE_TIMEOUT", 0.05)

        killed: list = []

        class _HangingProc:
            def __init__(self):
                self.pid = 424242

            async def communicate(self):
                await _asyncio.sleep(10)

            def kill(self):
                killed.append("proc.kill")

            async def wait(self):
                return 0

        async def fake_exec(*args, **kwargs):
            self_ = kwargs.get("start_new_session")
            assert self_ is True, "必须独立进程组（超时连 node 钩子一起杀）"
            return _HangingProc()

        monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)

        def fake_killpg(pgid, sig):
            killed.append(("killpg", pgid, sig))

        monkeypatch.setattr(A.os, "killpg", fake_killpg)
        out = await A.npm_update("deepseek-ai/dsh")
        assert "超时" in out and "进程组" in out
        assert killed and killed[0][0] == "killpg" and killed[0][1] == 424242

    @pytest.mark.asyncio
    async def test_dry_run_flag_appended(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "deepseek-ai/dsh")
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)
        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            return _FakeProc(b"would install\n")

        monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)
        await A.npm_update("deepseek-ai/dsh", dry_run=True)
        assert captured["args"][-1] == "--dry-run"

    @pytest.mark.asyncio
    async def test_dry_run_string_false_is_false(self, monkeypatch, tmp_path):
        """LLM 可能把布尔传成字符串：'false'/'0' 不得变成预演。"""
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "deepseek-ai/dsh")
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)
        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            return _FakeProc(b"ok\n")

        monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)
        await A.npm_update("deepseek-ai/dsh", dry_run="false")
        assert "--dry-run" not in captured["args"]

    @pytest.mark.asyncio
    async def test_dry_run_garbage_string_refused(self, monkeypatch, tmp_path):
        """L4：无法识别的布尔串拒绝执行（旧实现落「真执行」分支——本技能
        写动作不可回退，歧义值必须 fail-safe 到不执行、让模型自纠）。"""
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "deepseek-ai/dsh")
        monkeypatch.setattr(A, "workspace_root", lambda: tmp_path)
        spawned: list = []

        async def fake_exec(*args, **kwargs):
            spawned.append(args)
            return _FakeProc(b"ok\n")

        monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)
        for bad in ("ture", "yes please", "2", "-enabled"):
            out = await A.npm_update("deepseek-ai/dsh", dry_run=bad)
            assert "无法识别" in out and "未执行任何操作" in out, bad
        assert not spawned, "歧义 dry_run 值不得 spawn npm"

    def test_missing_npm_binary(self, monkeypatch):
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "deepseek-ai/dsh")
        monkeypatch.setattr(A, "which", lambda name: False)
        out = await_sync(A.npm_update("deepseek-ai/dsh"))
        assert "没有 npm" in out

    @staticmethod
    def _reg_with(monkeypatch, level="medium"):
        from agentcore.skills.action_skills import register_action_skills
        from agentcore.skills.registry import SkillRegistry

        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", level)
        reg = SkillRegistry()
        register_action_skills(reg)
        return reg

    def test_not_registered_when_registry_empty(self, monkeypatch):
        """空登记表 = schema 不可见（未配置即整体关闭，play_music 同模式）。"""
        monkeypatch.delenv("AGENT_NPM_GLOBAL_PACKAGES", raising=False)
        assert "npm_update" not in self._reg_with(monkeypatch).skills

    def test_registered_when_registry_nonempty(self, monkeypatch):
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "deepseek-ai/dsh")
        assert "npm_update" in self._reg_with(monkeypatch).skills

    def test_low_level_not_registered(self, monkeypatch):
        """low 档不注册（medium+ 门在登记表门之前，双层）。"""
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "deepseek-ai/dsh")
        assert "npm_update" not in self._reg_with(monkeypatch, level="low").skills

    @pytest.mark.asyncio
    async def test_non_admin_denied(self, monkeypatch):
        """handler 内 is_superuser 二次校验（registry 层被击穿时的纵深）。"""
        monkeypatch.setenv("SUPERUSERS", "10001")
        monkeypatch.setenv("AGENT_NPM_GLOBAL_PACKAGES", "deepseek-ai/dsh")
        reg = self._reg_with(monkeypatch)
        out = await reg.skills["npm_update"].handler(
            package="deepseek-ai/dsh", user_id="99999"
        )
        assert "无权限" in out


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
            ("docker_stats", {"container": "web"}),
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

    def test_stats_registered_at_low_level(self, monkeypatch):
        """docker_stats 是只读面：low 档 schema 也要可见（动作类四件套不可见）。"""
        from agentcore.skills.action_skills import register_action_skills
        from agentcore.skills.registry import SkillRegistry

        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        reg = SkillRegistry()
        register_action_skills(reg)
        assert "docker_stats" in reg.skills
        assert "docker_ctrl" not in reg.skills


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


class TestStartupLogListGuard:
    """L9（REVIEW-a0a6bb4..6745556）：builtin 启动日志的技能清单是硬编码串，
    1332f9d 加 docker_stats 时漏改、c16fc54 才补——守卫「清单 == 注册集」，
    防同型漂移复发（改坏任一侧必失败）。清单 = 模板串 %s 填入三元分支字面量
    （medium/low），AST 层解析、相邻字符串拼接由 parser 天然折叠。"""

    @staticmethod
    def _log_lists() -> tuple[set[str], set[str]]:
        import ast
        from pathlib import Path

        from agentcore.skills import builtin as B

        tree = ast.parse(Path(B.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "info"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and "Action skills registered" in node.args[0].value
            ):
                continue
            template = node.args[0].value
            branch = node.args[1]
            assert isinstance(branch, ast.IfExp), "清单三元结构被改？守卫需同步"
            prefix = "Action skills registered: "
            # medium 分支是清单扩展（", x, y" 填入 %s）；low 分支是说明文字
            # （「（low 档：…）」直接拼在末项后），清单本身即 %s 之前的模板段
            medium = template.replace("%s", branch.body.value).removeprefix(prefix)
            low = template.split("%s")[0].removeprefix(prefix)
            split = lambda s: {  # noqa: E731
                x.strip() for x in s.split(",") if x.strip()
            }
            return split(medium), split(low)
        raise AssertionError("builtin.py 启动日志清单串不存在（被改没了？）")

    def test_log_list_matches_low_tier_registration(self, monkeypatch):
        from agentcore.skills.registry import SkillRegistry

        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        reg = SkillRegistry()
        A.register_action_skills(reg)
        registered = set(reg.skills)
        assert registered == {"docker_ps", "docker_logs", "docker_stats"}
        _, listed = self._log_lists()
        assert listed == registered, (
            "builtin.py 启动日志清单与 register_action_skills 低档注册集漂移："
            f"日志={sorted(listed)} 注册={sorted(registered)}"
        )

    def test_log_list_matches_medium_tier_registration(self, monkeypatch):
        """L9（REVIEW-6745556..d730827）：npm_update（d730827）注册进 medium 档
        但启动日志清单漏列——low 档守卫抓不到 medium 分支漂移，补 medium 守卫。
        登记表为空时 npm_update 不注册，清单串以「npm_update(登记表非空时)」注记
        表达该条件。"""
        from agentcore.skills.registry import SkillRegistry

        monkeypatch.delenv("AGENT_NPM_GLOBAL_PACKAGES", raising=False)
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "medium")
        reg = SkillRegistry()
        A.register_action_skills(reg)
        registered = set(reg.skills)
        assert registered == {
            "docker_ps",
            "docker_logs",
            "docker_stats",
            "service_ctrl",
            "docker_ctrl",
            "proc_kill",
            "run_build_script",
        }
        listed, _ = self._log_lists()
        # npm_update 按登记表条件注册（本用例空表 → 不注册），清单以
        # 「npm_update(登记表非空时)」注记表达该条件：注记项剥壳后不算漂移，
        # 但注记本身必须存在（防清单完全漏列——L9 同型漂移复发即在此抓）
        assert "npm_update(登记表非空时)" in listed, (
            "medium 清单缺 npm_update 条件注记（L9 同型漂移复发？）"
        )
        listed_bare = {x.split("(")[0] for x in listed} - {"npm_update"}
        assert listed_bare == registered, (
            "builtin.py 启动日志清单与 register_action_skills medium 档注册集漂移："
            f"日志={sorted(listed)} 注册={sorted(registered)}"
        )
