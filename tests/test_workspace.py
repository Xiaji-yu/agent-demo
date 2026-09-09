import asyncio

import pytest

from agentcore.workspace.confirm import DeletionGate
from agentcore.workspace.fs import WorkspaceFS
from agentcore.workspace.utils import is_superuser


class TestUtils:
    def test_is_superuser_env(self, monkeypatch):
        monkeypatch.setenv("SUPERUSERS", '["2224513919","10002"]')
        assert is_superuser("2224513919")
        assert not is_superuser("123")
        monkeypatch.setenv("SUPERUSERS", "2224513919,10002")
        assert is_superuser("10002")
        monkeypatch.delenv("SUPERUSERS")
        assert not is_superuser("2224513919")  # fail closed


class TestWorkspaceFS:
    @pytest.fixture
    def ws_root(self, tmp_path):
        r = tmp_path / "ws"
        r.mkdir()
        return r

    @pytest.fixture
    def fs(self, ws_root):
        return WorkspaceFS(ws_root)

    def test_path_escape_rejected(self, fs, ws_root):
        for bad in ["../x", "..", "../../etc/passwd"]:
            with pytest.raises(ValueError):
                fs.resolve(bad)

    def test_absolute_path_rejected(self, fs):
        with pytest.raises(ValueError):
            fs.resolve("/etc/passwd")

    @pytest.mark.asyncio
    async def test_write_read_roundtrip(self, fs):
        await fs.write("notes/a.txt", "hello")
        assert await fs.read("notes/a.txt") == "hello"

    @pytest.mark.asyncio
    async def test_delete_file_and_empty_dir(self, fs):
        await fs.write("f.txt", "x")
        assert "已删除文件" in await fs.delete("f.txt")
        await fs.mkdir("empty")
        assert "已删除空目录" in await fs.delete("empty")

    @pytest.mark.asyncio
    async def test_delete_nonempty_dir_rejected(self, fs):
        await fs.write("d/x.txt", "x")
        assert "非空" in await fs.delete("d")

    @pytest.mark.asyncio
    async def test_delete_abs_containment(self, fs, ws_root):
        await fs.write("a.txt", "x")
        outside = (ws_root.parent / "outside.txt").resolve()
        outside.write_text("y")
        with pytest.raises(ValueError):
            await fs.delete_abs(outside)
        inside = fs.resolve("a.txt")
        assert "已删除文件" in await fs.delete_abs(inside)


class TestPermitted:
    @pytest.fixture(autouse=True)
    def _fake_which(self, monkeypatch):
        import agentcore.workspace.runner as R

        monkeypatch.setattr(R.shutil, "which", lambda name: name)

    def permit(self, exe, args):
        import agentcore.workspace.runner as R

        return R.permitted(exe, args)

    def test_readonly_git_only(self):
        assert self.permit("git", ["status"])[0]
        ok, reason = self.permit("git", ["push"])
        assert not ok and "只读" in reason

    def test_shell_meta_rejected(self):
        for args in [["a;b"], ["a", "|", "b"], ["a&&b"], ["$(x)"], ["a`b`"]]:
            assert not self.permit("grep", args)[0], args

    def test_path_escape_rejected(self):
        for args in [["../../etc/passwd"], ["/etc/passwd"]]:
            assert not self.permit("cat", args)[0]

    def test_high_risk_runtimes_disabled(self):
        # python3 / node / npm 已禁用（任意脚本 ≈ 任意代码）
        for exe, args in [
            ("python3", ["script.py"]),
            ("python3", ["-c", "print(1)"]),
            ("node", ["script.js"]),
            ("node", ["-e", "x"]),
            ("npm", ["run", "build"]),
        ]:
            assert not self.permit(exe, args)[0], (exe, args)

    def test_curl_https_only(self):
        assert not self.permit("curl", ["http://example.com"])[0]
        assert self.permit("curl", ["https://example.com"])[0]
        assert not self.permit("curl", ["https://example.com", "-k"])[0]

    def test_unknown_rejected(self):
        assert not self.permit("rm", ["-rf", "x"])[0]
        assert not self.permit("sh", ["-c", "x"])[0]
        assert not self.permit("python3", ["-c", "x"])[0]


class TestDeletionGate:
    @pytest.mark.asyncio
    async def test_request_confirm_roundtrip(self):
        gate = DeletionGate(ttl=60)
        code = await gate.request("u1", "/tmp/a")
        assert len(code) >= 6
        assert await gate.confirm("u1", code) == "/tmp/a"
        assert await gate.confirm("u1", code) is None  # 一次性

    @pytest.mark.asyncio
    async def test_wrong_user_cannot_confirm(self):
        gate = DeletionGate(ttl=60)
        code = await gate.request("u1", "/tmp/a")
        assert await gate.confirm("u2", code) is None

    @pytest.mark.asyncio
    async def test_expiry(self):
        gate = DeletionGate(ttl=0.01)
        code = await gate.request("u1", "/tmp/a")
        await asyncio.sleep(0.05)
        assert await gate.confirm("u1", code) is None


class TestRunnerReal:
    @pytest.mark.asyncio
    async def test_grep_version(self, tmp_path):
        from agentcore.workspace.runner import CommandRunner

        runner = CommandRunner(tmp_path)
        out = await runner.run("grep", ["--version"])
        assert "grep" in out.lower()

    @pytest.mark.asyncio
    async def test_python3_rejected(self, tmp_path):
        from agentcore.workspace.runner import CommandRunner

        runner = CommandRunner(tmp_path)
        out = await runner.run("python3", ["--version"])
        assert "拒绝" in out
