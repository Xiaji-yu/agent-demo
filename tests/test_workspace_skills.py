"""H4-②/M20：skill 层 ACL 与 schema 可见性测试。

fs_*/run_command 对非管理员必须「schema 不可见 + 执行被拒」双重生效；
_deny_or_fs 即便被误删，schema 过滤也兜底，反之亦然。
"""
import pytest

from agentcore.skills.permissions import PermissionChecker
from agentcore.skills.registry import SkillRegistry
from agentcore.skills.workspace_skills import register_workspace_skills


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERUSERS", '["10000"]')
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    reg = SkillRegistry(
        permission_checker=PermissionChecker(superusers={"10000"})
    )
    register_workspace_skills(reg)
    return reg


class TestSchemaVisibility:
    def test_admin_sees_workspace_skills(self, registry):
        names = {s["function"]["name"] for s in registry.get_schemas("10000", None)}
        assert {"fs_list", "fs_read", "fs_write", "fs_mkdir", "fs_delete", "run_command"} <= names

    def test_non_admin_schema_hidden(self, registry):
        # M20：非管理员不应看到永远失败的 6 个管理员工具
        names = {s["function"]["name"] for s in registry.get_schemas("20002", None)}
        assert not (names & {"fs_list", "fs_read", "fs_write", "fs_mkdir", "fs_delete", "run_command"})

    def test_explicit_grant_makes_visible(self, registry):
        registry.permission_checker.user_skills["20003"] = {"fs_list"}
        names = {s["function"]["name"] for s in registry.get_schemas("20003", None)}
        assert "fs_list" in names
        assert "run_command" not in names


class TestExecutionACL:
    @pytest.mark.asyncio
    async def test_non_admin_denied_fs(self, registry):
        # registry 层 checker 先拦截（schema 同源），纵深上 handler 层 ACL 兜底
        out = await registry.execute("fs_list", user_id="20002")
        assert "denied" in out or "仅管理员" in out

    @pytest.mark.asyncio
    async def test_non_admin_denied_run_command(self, registry):
        out = await registry.execute("run_command", user_id="20002", executable="ls", args=[])
        assert "denied" in out or "仅管理员" in out

    @pytest.mark.asyncio
    async def test_admin_fs_roundtrip(self, registry):
        out = await registry.execute("fs_write", user_id="10000", path="notes/a.txt", content="hello")
        assert "已写入" in out
        out = await registry.execute("fs_read", user_id="10000", path="notes/a.txt")
        assert out == "hello"

    @pytest.mark.asyncio
    async def test_admin_fs_path_escape_denied(self, registry):
        out = await registry.execute("fs_read", user_id="10000", path="../escape")
        assert "拒绝" in out

    @pytest.mark.asyncio
    async def test_admin_run_command_rejects_escape(self, registry):
        out = await registry.execute(
            "run_command", user_id="10000", executable="find", args=[".", "-delete"]
        )
        assert "拒绝" in out

    @pytest.mark.asyncio
    async def test_admin_run_command_works(self, registry):
        out = await registry.execute(
            "run_command", user_id="10000", executable="grep", args=["--version"]
        )
        assert "grep" in out.lower()


class TestDefenseInDepth:
    @pytest.mark.asyncio
    async def test_skill_layer_denies_even_if_checker_bypassed(self, monkeypatch, tmp_path):
        """纵深防御：即便 schema 因配置失误对非管理员可见，handler 内 ACL 仍拒绝。"""
        monkeypatch.setenv("SUPERUSERS", '["10000"]')
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
        reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"*"}))  # 全开(失误配置)
        register_workspace_skills(reg)
        # user_id 仍由 engine 从事件注入，LLM 不可伪造 → handler 层继续拒绝
        assert "仅管理员" in await reg.execute("fs_read", user_id="20002", path="x")


class TestDeletionGateIntegration:
    @pytest.mark.asyncio
    async def test_fs_delete_requires_confirmation_code(self, registry):
        from agentcore.workspace.confirm import get_gate

        await registry.execute("fs_write", user_id="10000", path="victim.txt", content="x")
        out = await registry.execute("fs_delete", user_id="10000", path="victim.txt")
        assert "确认删除" in out
        # 未确认前文件仍在
        assert "victim.txt" in await registry.execute("fs_list", user_id="10000")
        # 确认码校验 → 删除生效（真实确认走 admin matcher，这里直接验证 gate→fs 链）
        gate = get_gate()
        code = next(iter(gate._pending["10000"]))
        path = await gate.confirm("10000", code)
        assert path is not None and path.endswith("victim.txt")
        from agentcore.workspace.fs import WorkspaceFS
        from agentcore.workspace.utils import workspace_root

        result = await WorkspaceFS(workspace_root()).delete_abs(path)
        assert "已删除" in result

    @pytest.mark.asyncio
    async def test_wrong_user_cannot_confirm(self, registry):
        from agentcore.workspace.confirm import get_gate

        await registry.execute("fs_write", user_id="10000", path="v.txt", content="x")
        await registry.execute("fs_delete", user_id="10000", path="v.txt")
        gate = get_gate()
        code = next(iter(gate._pending["10000"]))
        assert await gate.confirm("99999", code) is None  # 他人确认码无效

    @pytest.mark.asyncio
    async def test_wrong_code_cleared_after_failures(self):
        from agentcore.workspace.confirm import DeletionGate

        gate = DeletionGate(ttl=60)
        await gate.request("u1", "/tmp/a")
        for _ in range(5):
            assert await gate.confirm("u1", "WRONG1") is None
        assert gate.pending_count("u1") == 0  # 连续输错 → 待确认项全部作废
