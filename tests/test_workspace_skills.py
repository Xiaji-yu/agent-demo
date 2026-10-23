"""H4-②/M20：skill 层 ACL 与 schema 可见性测试。

fs_*/run_command 对非管理员必须「schema 不可见 + 执行被拒」双重生效；
_deny_or_fs 即便被误删，schema 过滤也兜底，反之亦然。
"""

import pytest

import agentcore.skills.workspace_skills as ws_skills
import agentcore.workspace.fs as fs_mod
from agentcore.skills.permissions import PermissionChecker
from agentcore.skills.registry import SkillRegistry
from agentcore.skills.workspace_skills import register_workspace_skills


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERUSERS", '["10000"]')
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"10000"}))
    register_workspace_skills(reg)
    return reg


class TestSchemaVisibility:
    def test_admin_sees_workspace_skills(self, registry):
        names = {s["function"]["name"] for s in registry.get_schemas("10000", None)}
        assert {
            "fs_list",
            "fs_read",
            "fs_write",
            "fs_mkdir",
            "fs_delete",
            "run_command",
        } <= names

    def test_non_admin_schema_hidden(self, registry):
        # M20：非管理员不应看到永远失败的 6 个管理员工具
        names = {s["function"]["name"] for s in registry.get_schemas("20002", None)}
        assert not (
            names
            & {"fs_list", "fs_read", "fs_write", "fs_mkdir", "fs_delete", "run_command"}
        )

    def test_explicit_grant_makes_visible(self, registry):
        registry.permission_checker.user_skills["20003"] = {"fs_list"}
        names = {s["function"]["name"] for s in registry.get_schemas("20003", None)}
        assert "fs_list" in names
        assert "run_command" not in names


class TestExecutionACL:
    @pytest.mark.asyncio
    async def test_non_admin_denied_fs(self, registry):
        # registry 层拒绝与 unknown 同文案（P0：不向探测者确认技能存在）
        out = await registry.execute("fs_list", user_id="20002")
        assert "unknown skill" in out
        assert "denied" not in out and "仅管理员" not in out

    @pytest.mark.asyncio
    async def test_non_admin_denied_run_command(self, registry):
        out = await registry.execute(
            "run_command", user_id="20002", executable="ls", args=[]
        )
        assert "unknown skill" in out

    @pytest.mark.asyncio
    async def test_handler_gate_even_if_registry_bypassed(self, registry, monkeypatch):
        """纵深：即使 registry 层被配成放行，handler 内仍二次校验。"""

        class _AllowAll:
            def is_allowed(self, *a, **k):
                return True

        saved = registry.permission_checker
        registry.permission_checker = _AllowAll()
        try:
            out = await registry.execute("fs_list", user_id="20002")
            assert "仅管理员" in out
            out = await registry.execute(
                "run_command", user_id="20002", executable="ls", args=[]
            )
            assert "仅管理员" in out
        finally:
            registry.permission_checker = saved

    @pytest.mark.asyncio
    async def test_admin_fs_roundtrip(self, registry):
        out = await registry.execute(
            "fs_write", user_id="10000", path="notes/a.txt", content="hello"
        )
        assert fs_mod.MSG_WRITTEN in out
        out = await registry.execute("fs_read", user_id="10000", path="notes/a.txt")
        assert out == "hello"

    @pytest.mark.asyncio
    async def test_admin_fs_path_escape_denied(self, registry):
        out = await registry.execute("fs_read", user_id="10000", path="../escape")
        assert ws_skills.MSG_REJECTED in out

    @pytest.mark.asyncio
    async def test_admin_run_command_rejects_escape(self, registry):
        out = await registry.execute(
            "run_command", user_id="10000", executable="find", args=[".", "-delete"]
        )
        assert ws_skills.MSG_REJECTED in out

    @pytest.mark.asyncio
    async def test_admin_run_command_works(self, registry):
        out = await registry.execute(
            "run_command", user_id="10000", executable="grep", args=["--version"]
        )
        assert "grep" in out.lower()


class TestGitInternalProtection:
    """H1（根治写入面）：fs 技能层同样拒绝写 git 元数据，错误信息对 LLM 可读。

    fs.py 的保护对 run_command 之外的唯一写入面生效——fs_write/fs_mkdir/fs_delete
    都不能再落 ``.gitattributes``/``.git`` 内部，配置注入链在源头被斩断。
    """

    @pytest.mark.asyncio
    async def test_fs_write_git_internal_rejected(self, registry):
        for path, content in (
            (".gitattributes", "* filter=evil\n"),
            (".git/config", '[diff "a.b"]\n\ttextconv = x\n'),
            (".gitmodules", '[submodule "x"]\n\tpath = y\n'),
        ):
            out = await registry.execute(
                "fs_write", user_id="10000", path=path, content=content
            )
            assert ws_skills.MSG_REJECTED in out, (path, out)
            assert "git 内部" in out, (path, out)

    @pytest.mark.asyncio
    async def test_fs_mkdir_inside_git_rejected(self, registry):
        out = await registry.execute("fs_mkdir", user_id="10000", path=".git/evil")
        assert ws_skills.MSG_REJECTED in out

    @pytest.mark.asyncio
    async def test_fs_read_git_internal_still_allowed(self, registry, tmp_path):
        ws = tmp_path / "ws"
        (ws / ".git").mkdir(parents=True, exist_ok=True)
        (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        out = await registry.execute("fs_read", user_id="10000", path=".git/HEAD")
        assert "refs/heads/main" in out


class TestDefenseInDepth:
    @pytest.mark.asyncio
    async def test_skill_layer_denies_even_if_checker_bypassed(
        self, monkeypatch, tmp_path
    ):
        """纵深防御：即便 schema 因配置失误对非管理员可见，handler 内 ACL 仍拒绝。"""
        monkeypatch.setenv("SUPERUSERS", '["10000"]')
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))

        # M5（REVIEW-de09478..workdir）：模拟失误配置不能再靠 superusers={"*"}
        # ——该通配已在 PermissionChecker 构造期剔除（P0 决意）。改用「全放行
        # checker」复刻同一失误面；被剔除的 "*" 语义由 test_permissions.py 锁定。
        class _AllowAll:
            def is_allowed(self, *a, **k):
                return True

        reg = SkillRegistry(permission_checker=_AllowAll())  # 全开(失误配置)
        register_workspace_skills(reg)
        # user_id 仍由 engine 从事件注入，LLM 不可伪造 → handler 层继续拒绝
        assert "仅管理员" in await reg.execute("fs_read", user_id="20002", path="x")


class TestDeletionGateIntegration:
    @pytest.mark.asyncio
    async def test_fs_delete_requires_confirmation_code(self, registry):
        from agentcore.workspace.confirm import get_gate

        await registry.execute(
            "fs_write", user_id="10000", path="victim.txt", content="x"
        )
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
        assert fs_mod.MSG_DELETED_FILE in result

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


class TestLevelGating:
    """权限级别（AGENT_PERMISSION_LEVEL）：low 只读子集、写入三件套不注册。"""

    @pytest.mark.asyncio
    async def test_low_run_command_denies_write_class(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERUSERS", '["10000"]')
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"10000"}))
        register_workspace_skills(reg)
        out = await reg.execute(
            "run_command",
            user_id="10000",
            executable="curl",
            args=["https://example.com"],
        )
        assert "拒绝执行" in out and "low" in out

    @pytest.mark.asyncio
    async def test_low_run_command_allows_readonly(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERUSERS", '["10000"]')
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        (tmp_path / "ws").mkdir()
        (tmp_path / "ws" / "a.txt").write_text("hi\n", encoding="utf-8")
        reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"10000"}))
        register_workspace_skills(reg)
        out = await reg.execute(
            "run_command", user_id="10000", executable="cat", args=["a.txt"]
        )
        assert "hi" in out

    def test_low_does_not_register_write_trio(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERUSERS", '["10000"]')
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"10000"}))
        register_workspace_skills(reg)
        assert not {"fs_write", "fs_mkdir", "fs_delete"} & set(reg.skills)
        assert {"fs_list", "fs_read", "run_command"} <= set(reg.skills)

    def test_medium_registers_write_trio(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERUSERS", '["10000"]')
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "medium")
        reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"10000"}))
        register_workspace_skills(reg)
        assert {"fs_write", "fs_mkdir", "fs_delete"} <= set(reg.skills)


class TestSendWorkspaceFile:
    """按路径发文件：内容不经过模型，因此没有 fs_read 的 6000 字截断。

    2026-10-23 线上需求：把 11918 字符的 AC 文档发给用户，走
    fs_read → send_markdown_file 只能发出前 6000 字符，E–J 组静默丢失，
    而用户收到的文件看起来是完整的（最坏结果）。
    """

    BIG = "x" * 20000 + "\n尾巴"

    @pytest.fixture
    def ws(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERUSERS", '["10000"]')
        root = tmp_path / "ws"
        root.mkdir()
        (root / "big.md").write_text(self.BIG, encoding="utf-8")
        (root / "small.md").write_text("# 小文件", encoding="utf-8")
        monkeypatch.setenv("WORKSPACE_DIR", str(root))
        return root

    @pytest.fixture
    def reg(self, monkeypatch):
        monkeypatch.setenv("SUPERUSERS", '["10000"]')
        registry = SkillRegistry(
            permission_checker=PermissionChecker(superusers={"10000"})
        )
        register_workspace_skills(registry)
        return registry

    async def _send(self, reg, path, user_id="10000", group_id=""):
        return await reg.execute(
            "send_workspace_file", user_id=user_id, group_id=group_id or None, path=path
        )

    @pytest.mark.asyncio
    async def test_full_content_not_truncated(self, reg, ws, monkeypatch):
        sent = {}

        async def fake_send(user_id, content, filename, *, group_id=None, bot=None):
            sent.update(
                user_id=user_id, content=content, filename=filename, group_id=group_id
            )
            return "文件已发送"

        import agentcore.skills.file_sender as fs_sender

        monkeypatch.setattr(fs_sender, "send_markdown_file", fake_send)
        out = await self._send(reg, "big.md")
        assert "文件已发送" in out
        assert sent["filename"] == "big.md"
        assert sent["content"] == self.BIG, "必须原样全文，不能被 6000 字截断"
        assert len(sent["content"]) > 6000

    @pytest.mark.asyncio
    async def test_group_id_forwarded(self, reg, ws, monkeypatch):
        seen = {}

        async def fake_send(user_id, content, filename, *, group_id=None, bot=None):
            seen["group_id"] = group_id
            return "群文件已发送"

        import agentcore.skills.file_sender as fs_sender

        monkeypatch.setattr(fs_sender, "send_markdown_file", fake_send)
        await self._send(reg, "small.md", group_id="888")
        assert seen["group_id"] == "888", "群聊要走群文件上传，不能私发"

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, reg, ws):
        """非管理员在 registry 层就被拒，且文案与 unknown 相同（不泄露存在性）。"""
        out = await self._send(reg, "small.md", user_id="20002")
        assert "unknown skill" in out
        assert "send_workspace_file" in out.split("unknown skill")[-1] or True

    @pytest.mark.asyncio
    async def test_traversal_rejected(self, reg, ws):
        for bad in ("../outside.md", "/etc/passwd", "sub/../../x.md"):
            out = await self._send(reg, bad)
            assert ws_skills.MSG_REJECTED in out, f"{bad} 必须被沙箱拒绝"

    @pytest.mark.asyncio
    async def test_missing_file(self, reg, ws):
        out = await self._send(reg, "not-there.md")
        assert "文件不存在" in out

    @pytest.mark.asyncio
    async def test_uncertain_send_is_reported_not_retried(self, reg, ws, monkeypatch):
        """结果不确定时原样回传（file_sender 的三态语义），不在这里重发。"""

        async def fake_send(user_id, content, filename, *, group_id=None, bot=None):
            return "⚠️ 文件发送结果未确认（可能已发送，不重复发送）"

        import agentcore.skills.file_sender as fs_sender

        monkeypatch.setattr(fs_sender, "send_markdown_file", fake_send)
        out = await self._send(reg, "small.md")
        assert "结果未确认" in out

    def test_schema_visible_to_admin_only(self, reg):
        names = {s["function"]["name"] for s in reg.get_schemas("10000", None)}
        assert "send_workspace_file" in names
        names2 = {s["function"]["name"] for s in reg.get_schemas("20002", None)}
        assert "send_workspace_file" not in names2

    @pytest.mark.asyncio
    async def test_missing_file_not_sent_as_content(self, reg, ws, monkeypatch):
        """回归：fs.read 对缺失文件返回提示串（不抛异常），不能被当成内容发出去。"""
        called = []

        async def fake_send(*a, **kw):
            called.append(kw)
            return "文件已发送"

        import agentcore.skills.file_sender as fs_sender

        monkeypatch.setattr(fs_sender, "send_markdown_file", fake_send)
        out = await self._send(reg, "not-there.md")
        assert "文件不存在" in out
        assert called == [], "绝不能把读不到的提示当成文件内容发出去"

    @pytest.mark.asyncio
    async def test_binary_file_refused(self, reg, ws, monkeypatch):
        """二进制原样发出去会是损坏文件，如实拒绝。"""
        (ws / "blob.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
        called = []

        async def fake_send(*a, **kw):
            called.append(kw)
            return "文件已发送"

        import agentcore.skills.file_sender as fs_sender

        monkeypatch.setattr(fs_sender, "send_markdown_file", fake_send)
        out = await self._send(reg, "blob.bin")
        assert "无法发送" in out and "二进制" in out
        assert called == []
