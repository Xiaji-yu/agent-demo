"""run_shell（high 级专属）的主题测试。

来源：2026-10 权限三级收束——high = medium 全部 + 任意命令执行（bash -c）。
契约（改坏实现必须失败）：
- 三层闸：非管理员拒、级别不足拒、（registry 层 schema 由注册期过滤保证）
- 最小环境：LLM_API_KEY 等不得泄漏进子进程
- 超时杀整个进程组；输出截断；审计日志带 uid 与命令全文
"""

import logging

import pytest

import agentcore.skills.shell_skill as S
from agentcore.skills.permissions import PermissionChecker
from agentcore.skills.registry import SkillRegistry
from agentcore.skills.shell_skill import register_shell_skill, run_shell


@pytest.fixture
def high_env(monkeypatch):
    monkeypatch.setenv("SUPERUSERS", "10001")
    monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "high")


def _reg(monkeypatch, superusers="10001", level="high"):
    monkeypatch.setenv("SUPERUSERS", superusers)
    monkeypatch.setenv("AGENT_PERMISSION_LEVEL", level)
    reg = SkillRegistry(permission_checker=PermissionChecker(superusers={superusers}))
    register_shell_skill(reg)
    return reg


class TestGates:
    @pytest.mark.asyncio
    async def test_non_superuser_registry_layer_unknown(self, monkeypatch, high_env):
        reg = _reg(monkeypatch)
        out = await reg.execute("run_shell", user_id="99999", command="echo hi")
        # registry 层先拦：schema 不可见的技能对探测者与不存在同文案
        assert "unknown skill" in out

    @pytest.mark.asyncio
    async def test_non_superuser_handler_gate(self, monkeypatch, high_env):
        reg = _reg(monkeypatch)

        class _AllowAll:
            def is_allowed(self, *a, **k):
                return True  # 模拟 registry 层被击穿：handler 必须兜住

        reg.permission_checker = _AllowAll()
        out = await reg.execute("run_shell", user_id="99999", command="echo hi")
        assert "无权限" in out

    @pytest.mark.asyncio
    async def test_low_level_denied(self, monkeypatch, high_env):
        reg = _reg(monkeypatch, level="low")
        out = await reg.execute("run_shell", user_id="10001", command="echo hi")
        assert "权限级别" in out and "high" in out

    @pytest.mark.asyncio
    async def test_medium_level_denied(self, monkeypatch, high_env):
        reg = _reg(monkeypatch, level="medium")
        out = await reg.execute("run_shell", user_id="10001", command="echo hi")
        assert "权限级别" in out and "high" in out

    def test_not_in_readonly_parallel_whitelist(self, monkeypatch, high_env):
        # builtin 的 mark_read_only 审计名单绝不收 run_shell（fail-closed 并行）
        from agentcore.skills.builtin import register_builtin_skills

        monkeypatch.setenv("SUPERUSERS", "10001")
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "high")
        monkeypatch.setenv("SEARCH_API_KEY", "")
        reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"10001"}))
        register_builtin_skills(reg)
        assert "run_shell" in reg.skills
        assert reg.is_read_only("run_shell") is False


class TestExecution:
    @pytest.mark.asyncio
    async def test_runs_command(self, high_env):
        out = await run_shell("echo hello-runshell", uid="10001")
        assert "hello-runshell" in out

    @pytest.mark.asyncio
    async def test_minimal_env_no_key_leak(self, monkeypatch, high_env):
        monkeypatch.setenv("LLM_API_KEY", "sk-SUPER-SECRET")
        monkeypatch.setenv("SECRET_TOKEN", "tok-SUPER-SECRET")
        out = await run_shell('echo "[$LLM_API_KEY][$SECRET_TOKEN]"', uid="10001")
        assert "sk-SUPER-SECRET" not in out
        assert "tok-SUPER-SECRET" not in out
        assert "[]" in out, "最小环境下这些变量不应存在"

    @pytest.mark.asyncio
    async def test_pipes_and_redirects_work(self, high_env, tmp_path, monkeypatch):
        monkeypatch.setattr(S, "workspace_root", lambda: tmp_path)
        out = await run_shell("echo abc | grep b > out.txt && cat out.txt", uid="t")
        assert "abc" in out
        assert (tmp_path / "out.txt").read_text(encoding="utf-8").strip() == "abc"

    @pytest.mark.asyncio
    async def test_timeout_kills_process_group(self, monkeypatch, high_env):
        monkeypatch.setattr(S, "_SHELL_TIMEOUT", 1)
        out = await run_shell("sleep 30", uid="t")
        assert "超时" in out and "进程组" in out

    @pytest.mark.asyncio
    async def test_output_truncated(self, high_env):
        out = await run_shell("yes A | head -c 20000", uid="t")
        assert len(out) <= S._MAX_OUTPUT_CHARS + 100
        assert "截断" in out

    @pytest.mark.asyncio
    async def test_empty_command(self, high_env):
        assert "命令为空" in await run_shell("  ", uid="t")

    @pytest.mark.asyncio
    async def test_audit_log_contains_uid_and_command(self, high_env, caplog):
        with caplog.at_level(logging.INFO, logger=S.__name__):
            await run_shell("echo audit-me", uid="u-42")
        audit = [r for r in caplog.records if r.getMessage().startswith("run_shell:")]
        assert audit, "必须留审计日志"
        assert "u-42" in audit[0].getMessage()
        assert "echo audit-me" in audit[0].getMessage()


class TestAuditLineEscaping:
    """L8（REVIEW-de09478..workdir）：raw 命令含换行不得伪造多行审计记录。

    runner 侧靠参数禁换行设防；run_shell 是 bash -c 语义（输入可含换行），
    故审计行必须用 repr——否则一行日志可被伪造成看似独立的多行事件。
    """

    @pytest.mark.asyncio
    async def test_audit_line_single_physical_line(self, monkeypatch, tmp_path):
        import logging

        from agentcore.skills.shell_skill import run_shell

        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        seen: list[str] = []
        logger = logging.getLogger("agentcore.skills.shell_skill")
        monkeypatch.setattr(
            logger,
            "info",
            lambda msg, *args: seen.append(msg % args if args else str(msg)),
        )

        # 审计行在 spawn 之前落：spawn 直接失败即可拿到审计、无需真跑 bash
        async def boom(*a, **kw):
            raise OSError("no bash in test")

        monkeypatch.setattr(
            "agentcore.skills.shell_skill.asyncio.create_subprocess_exec", boom
        )

        fake_cmd = "echo ok\nfake INFO run_shell: uid=admin cmd=harmless"
        out = await run_shell(fake_cmd, uid="1")

        assert out.startswith("(执行失败")
        audit = [m for m in seen if m.startswith("run_shell:")]
        assert audit, "必须留下审计行"
        assert all("\n" not in m for m in audit), "审计行不得被换行伪造"
        assert "\\n" in audit[0], "换行应以转义形态保留在审计里（repr）"
