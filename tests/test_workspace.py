import asyncio

import pytest

import agentcore.workspace.runner as R
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
    async def test_write_created_0600(self, fs, tmp_path, monkeypatch):
        """M2（REVIEW-de09478..workdir）：工作区文件创建即 0600。

        旧实现 write_text 后 chmod——chmod 之前 .part 是 umask 产物
        （通常 0644），崩溃即留下全局可读的中间产物。chmod 换成
        一触即炸的探针：实现若退回事后修补必失败。
        """
        import os as _os

        def _tripwire(*a, **kw):
            raise AssertionError("不得依赖 chmod 事后修补权限位")

        monkeypatch.setattr(_os, "chmod", _tripwire)
        old_umask = _os.umask(0o022)
        try:
            await fs.write("notes/priv.txt", "s")
        finally:
            _os.umask(old_umask)
        target = tmp_path / "ws" / "notes" / "priv.txt"
        assert target.read_text(encoding="utf-8") == "s"
        assert _os.stat(target).st_mode & 0o777 == 0o600
        assert not list((tmp_path / "ws" / "notes").glob("*.part"))

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


class TestFsGitInternalProtection:
    """H1（根治写入面）：fs 写/建/删不得触及 git 元数据；读取保持允许。

    ``.gitattributes`` 属性 + repo 配置里的驱动定义即可让白名单内的 ``git diff``
    执行任意命令，因此这些路径对 LLM 完全只读，错误信息需 LLM 可理解。
    """

    @pytest.fixture
    def fs(self, tmp_path):
        w = WorkspaceFS(tmp_path / "ws")
        gitdir = w.root / ".git"
        gitdir.mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (gitdir / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8"
        )
        (w.root / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")
        (w.root / ".gitmodules").write_text("", encoding="utf-8")
        return w

    @pytest.mark.asyncio
    async def test_write_git_config_rejected(self, fs):
        # 推演链第②步：fs_write('.git/config', '[diff "a.b"] textconv = ...') 被斩断
        with pytest.raises(ValueError, match="git 内部"):
            await fs.write(".git/config", '[diff "a.b"]\n\ttextconv = evil\n')

    @pytest.mark.asyncio
    async def test_write_gitattributes_rejected(self, fs):
        # 推演链第①步：fs_write('.gitattributes', '* diff=a.b') 被斩断
        with pytest.raises(ValueError, match="git 内部"):
            await fs.write(".gitattributes", "* filter=evil\n")

    @pytest.mark.asyncio
    async def test_write_gitmodules_rejected(self, fs):
        with pytest.raises(ValueError, match="git 内部"):
            await fs.write(".gitmodules", '[submodule "x"]\n\tpath = y\n')

    @pytest.mark.asyncio
    async def test_write_inside_git_rejected(self, fs):
        with pytest.raises(ValueError, match="git 内部"):
            await fs.write(".git/hooks/pre-commit", "#!/bin/sh\nid\n")

    @pytest.mark.asyncio
    async def test_write_nested_repo_git_rejected(self, fs):
        # 嵌套仓库（submodule/vendor）的 .git 同样受保护：按路径组件判定
        with pytest.raises(ValueError, match="git 内部"):
            await fs.write("vendor/lib/.git/config", "[core]\n")

    @pytest.mark.asyncio
    async def test_mkdir_inside_git_rejected(self, fs):
        with pytest.raises(ValueError, match="git 内部"):
            await fs.mkdir(".git/evil")

    @pytest.mark.asyncio
    async def test_delete_gitattributes_rejected(self, fs):
        with pytest.raises(ValueError, match="git 内部"):
            await fs.delete(".gitattributes")

    @pytest.mark.asyncio
    async def test_delete_inside_git_rejected(self, fs):
        (fs.root / ".git" / "refs").mkdir(exist_ok=True)
        with pytest.raises(ValueError, match="git 内部"):
            await fs.delete(".git/refs")

    @pytest.mark.asyncio
    async def test_delete_abs_gitattributes_rejected(self, fs):
        with pytest.raises(ValueError, match="git 内部"):
            await fs.delete_abs(fs.root / ".gitattributes")

    @pytest.mark.asyncio
    async def test_read_git_still_allowed(self, fs):
        # 读操作不受限：.git/HEAD 等仍可查看（排查/审计需要）
        assert "refs/heads/main" in await fs.read(".git/HEAD")
        assert "repositoryformatversion" in await fs.read(".git/config")

    @pytest.mark.asyncio
    async def test_list_git_still_allowed(self, fs):
        out = await fs.list(".git")
        assert "HEAD" in out and "config" in out


class TestFsReservedNames:
    """工作区根下的 ``SOUL.md`` / ``personas`` / ``cron`` 是保留名（2026-10-29）。

    这三个名字是人格与定时任务配置的**历史落点**：fs_write 能改写它们时，
    一条提示注入（群聊上下文/引用/网页）就能改默认人格或给自己加定时任务。
    现在写/建目录/删除一律拒绝、读取保持允许（旧布局迁移与排障要读）。
    大小写不敏感：Windows/mac 上 ``soul.md`` 就是同一个文件。
    """

    @pytest.fixture
    def fs(self, tmp_path):
        w = WorkspaceFS(tmp_path / "ws")
        w.root.mkdir(parents=True)
        (w.root / "SOUL.md").write_text("原始人格", encoding="utf-8")
        (w.root / "cron").mkdir()
        (w.root / "cron" / "jobs.json").write_text('{"jobs": []}', encoding="utf-8")
        (w.root / "personas").mkdir()
        return w

    @pytest.mark.asyncio
    async def test_write_soul_rejected(self, fs):
        with pytest.raises(ValueError, match="保留名"):
            await fs.write("SOUL.md", "被注入的人格")
        assert (fs.root / "SOUL.md").read_text(encoding="utf-8") == "原始人格"

    @pytest.mark.asyncio
    async def test_write_cron_jobs_rejected(self, fs):
        with pytest.raises(ValueError, match="保留名"):
            await fs.write("cron/jobs.json", '{"jobs": [{"evil": 1}]}')
        assert (fs.root / "cron" / "jobs.json").read_text(encoding="utf-8") == (
            '{"jobs": []}'
        )

    @pytest.mark.asyncio
    async def test_write_case_insensitive(self, fs):
        with pytest.raises(ValueError, match="保留名"):
            await fs.write("soul.md", "x")
        with pytest.raises(ValueError, match="保留名"):
            await fs.write("Cron/jobs.json", "x")

    @pytest.mark.asyncio
    async def test_mkdir_personas_rejected(self, fs):
        with pytest.raises(ValueError, match="保留名"):
            await fs.mkdir("personas")

    @pytest.mark.asyncio
    async def test_delete_reserved_rejected(self, fs):
        with pytest.raises(ValueError, match="保留名"):
            await fs.delete("cron/jobs.json")
        with pytest.raises(ValueError, match="保留名"):
            await fs.delete_abs(fs.root / "SOUL.md")
        assert (fs.root / "cron" / "jobs.json").is_file()

    @pytest.mark.asyncio
    async def test_same_name_nested_is_allowed(self, fs):
        # 只有**根级**名字保留：用户在自己的子目录里放同名文件不受影响
        await fs.write("notes/SOUL.md", "我的笔记")
        await fs.write("notes/cron/jobs.json", "{}")
        assert await fs.read("notes/SOUL.md") == "我的笔记"

    @pytest.mark.asyncio
    async def test_read_reserved_still_allowed(self, fs):
        # 旧布局迁移/排障要能读，只是不能写
        assert await fs.read("SOUL.md") == "原始人格"
        assert "jobs" in await fs.read("cron/jobs.json")

    @pytest.mark.asyncio
    async def test_write_trailing_dot_rejected(self, tmp_path):
        """尾部点绕过（2026-11-02 架构评审复现）：Win32 建文件时剥掉名字尾部的
        点与空格，而 ``resolve()`` 对**不存在**的路径只做词法归一、不剥——
        于是 ``cron./jobs.json`` / ``SOUL.md.`` / ``personas./default/SOUL.md``
        在全新工作区上全部写成功，落盘后正是保留名本体。目标已存在时 realpath
        归一到 canonical、后续即被拒，缺口只在**首写**那一瞬间。
        本用例不依赖 Windows（Linux 不剥尾部点，多剥一次只是让近似名也拒），
        钉的是「判定必须先归一再比对」这条不变量。
        """
        w = WorkspaceFS(tmp_path / "fresh")
        w.root.mkdir(parents=True)
        for rel in ("cron./jobs.json", "SOUL.md.", "personas./default/SOUL.md"):
            with pytest.raises(ValueError, match="保留名"):
                await w.write(rel, "注入的人格 / 定时任务")
        # 目录创建同罪：mkdir("cron.") 在 Windows 上建出的是 cron
        with pytest.raises(ValueError, match="保留名"):
            await w.mkdir("cron.")
        assert sorted(p.name for p in w.root.iterdir()) == [], "不得留下任何残留"
        # 尾部空格同形态
        with pytest.raises(ValueError, match="保留名"):
            await w.write("cron /jobs.json", "x")

    @pytest.mark.asyncio
    async def test_trailing_dot_non_reserved_still_allowed(self, tmp_path):
        """归一不能过头：普通名字带尾点仍可写（fail-closed 只针对保留名近似形）。"""
        w = WorkspaceFS(tmp_path / "ws2")
        w.root.mkdir(parents=True)
        await w.write("notes./readme.txt", "普通笔记")
        assert (w.root / "notes.").is_dir() or (w.root / "notes").is_dir()


class TestFsReadSafety:
    """M19：fs_read 的分块截断与二进制防护。"""

    @pytest.fixture
    def fs(self, tmp_path):
        return WorkspaceFS(tmp_path / "ws")

    @pytest.mark.asyncio
    async def test_binary_file_not_dumped_into_context(self, fs):
        # 落盘图片被 fs_read 时：不产出乱码，只报告大小
        await fs.mkdir("media")
        (fs.root / "media" / "pic.jpg").write_bytes(
            b"\xff\xd8\xff" + bytes(range(256)) * 64
        )
        out = await fs.read("media/pic.jpg")
        assert out.startswith("(二进制文件")
        assert "\ufffd" not in out

    @pytest.mark.asyncio
    async def test_large_text_truncated(self, fs):
        await fs.write("big.txt", "字" * 100000)
        out = await fs.read("big.txt", max_chars=1000)
        assert "仅显示前 1000 字符" in out
        assert len(out) < 2000

    @pytest.mark.asyncio
    async def test_read_is_nonblocking_for_large_file(self, fs):
        # 语义验证：读接口是 async 的（内部 to_thread），可并发调用
        await fs.write("a.txt", "hello")
        out = await fs.read("a.txt")
        assert out == "hello"


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
        assert R.MSG_REFUSED in out
