import asyncio

import pytest

from agentcore.workspace.confirm import DeletionGate
from agentcore.workspace.fs import WorkspaceFS
from agentcore.workspace.utils import is_superuser, safe_user_dirname


class TestUtils:
    def test_safe_dirname(self):
        assert safe_user_dirname("2224513919") == "2224513919"
        with pytest.raises(ValueError):
            safe_user_dirname("../etc")

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
    def fs(self, tmp_path):
        return WorkspaceFS(tmp_path, "u1")

    def test_path_escape_rejected(self, fs, tmp_path):
        for bad in ["../u2/x", "..", "../../etc/passwd"]:
            with pytest.raises(ValueError):
                fs.resolve(bad)

    def test_absolute_path_rejected(self, fs):
        with pytest.raises(ValueError):
            fs.resolve("/etc/passwd")

    @pytest.mark.asyncio
    async def test_write_read_roundtrip(self, fs):
        await fs.write("notes/a.txt", "hello")
        out = await fs.read("notes/a.txt")
        assert out == "hello"

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
    async def test_delete_abs_containment(self, fs, tmp_path):
        await fs.write("a.txt", "x")
        outside = (tmp_path / "outside.txt").resolve()
        outside.write_text("y")
        with pytest.raises(ValueError):
            await fs.delete_abs(outside)
        # 合法绝对路径可删
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
        ok, _ = self.permit("git", ["status"])
        assert ok
        ok, reason = self.permit("git", ["push"])
        assert not ok and "只读" in reason

    def test_shell_meta_rejected(self):
        for args in [["a;b"], ["a", "|", "b"], ["a&&b"], ["$(x)"], ["a`b`"]]:
            ok, _ = self.permit("grep", args)
            assert not ok, args

    def test_path_escape_rejected(self):
        for args in [["../../etc/passwd"], ["/etc/passwd"]]:
            ok, _ = self.permit("cat", args)
            assert not ok

    def test_python_no_dash_c(self):
        assert not self.permit("python3", ["-c", "print(1)"])[0]
        assert self.permit("python3", ["script.py", "--x"])[0]

    def test_node_no_dash_e(self):
        assert not self.permit("node", ["-e", "x"])[0]
        assert self.permit("node", ["script.js"])[0]

    def test_npm_only_run(self):
        assert not self.permit("npm", ["install", "x"])[0]
        assert self.permit("npm", ["run", "build"])[0]

    def test_curl_https_only(self):
        assert not self.permit("curl", ["http://example.com"])[0]
        assert self.permit("curl", ["https://example.com"])[0]
        assert not self.permit("curl", ["https://example.com", "-k"])[0]

    def test_unknown_rejected(self):
        assert not self.permit("rm", ["-rf", "x"])[0]
        assert not self.permit("sh", ["-c", "x"])[0]


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
    async def test_python_version(self, tmp_path):
        from agentcore.workspace.runner import CommandRunner

        runner = CommandRunner(tmp_path)
        out = await runner.run("u1", "python3", ["--version"])
        assert "Python" in out
