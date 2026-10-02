"""ssh_run 专用技能：host/action/凭据三道白名单 + 密码绝不进 argv/日志。

对应会话「放宽 command 白名单」（2026-09-30 管理员决策）：
LLM 无法指定主机、无法自定义远端命令，凭据只从 .env 进环境变量。
"""

import asyncio
import os

import pytest

import agentcore.skills.ssh_skill as S
from agentcore.skills.ssh_skill import parse_hosts, register_ssh_skills, ssh_run


class _FakeStdout:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(0)
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProc:
    def __init__(self, chunks: list[bytes] | None = None, *, hang: bool = False):
        self.stdout = _FakeStdout(chunks if chunks is not None else [b"output\n"])
        self._hang = hang
        self.killed = False
        self.returncode = 0

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _HangStdout:
    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(3600)
        return b""


class _HangProc:
    def __init__(self):
        self.stdout = _HangStdout()

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        pass


class TestHostParsing:
    def test_valid_entry(self):
        hosts = parse_hosts("istore=root@192.168.1.2:22, nas=admin@10.0.0.5:2222")
        assert hosts == {
            "istore": {"user": "root", "host": "192.168.1.2", "port": 22},
            "nas": {"user": "admin", "host": "10.0.0.5", "port": 2222},
        }

    def test_empty_and_garbage_skipped(self):
        assert parse_hosts("") == {}
        assert parse_hosts("   ") == {}
        for bad in (
            "istore=root@192.168.1.2",  # 缺端口
            "ISTORE=root@192.168.1.2:22",  # alias 必须小写开头
            "istore=root@host:99999",  # 端口越界
            "istore=root@host:abc",  # 端口非数字
            "istore=ro ot@host:22",  # user 含空格
            "istore=root@@host:22",  # 缺 user
            "=root@host:22",  # 缺 alias
        ):
            assert parse_hosts(bad) == {}, bad


class TestSkillContract:
    def test_registered_superuser_with_enum(self):
        from agentcore.skills.registry import SkillRegistry

        reg = SkillRegistry()
        register_ssh_skills(reg)
        skill = reg.skills["ssh_run"]
        assert skill.permission == "superuser"
        assert skill.params_schema["required"] == ["alias", "action"]
        # action 走 enum：模型只能从菜单里选，拼不出别的字符串
        assert set(skill.params_schema["properties"]["action"]["enum"]) == set(
            S._ACTIONS
        )

    @pytest.mark.asyncio
    async def test_permission_denied_for_non_admin(self):
        from agentcore.skills.registry import SkillRegistry

        reg = SkillRegistry()
        register_ssh_skills(reg)
        # 未挂 PermissionChecker 时 superuser 技能对任何人都不可见（fail-closed）
        out = await reg.execute(
            "ssh_run", alias="istore", action="free", user_id="10001"
        )
        assert "permission denied" in out

    @pytest.mark.asyncio
    async def test_in_handler_superuser_guard(self):
        """即使权限层被配成放行，handler 内仍二次校验（纵深防御）。"""
        from agentcore.skills.registry import SkillRegistry

        class _AllowAll:
            def is_allowed(self, *a, **k):
                return True

        reg = SkillRegistry(_AllowAll())
        register_ssh_skills(reg)
        out = await reg.execute(
            "ssh_run", alias="istore", action="free", user_id="10001"
        )
        assert "无权限" in out


class TestSshRunGates:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("AGENT_SSH_HOSTS", "istore=root@192.168.1.2:22")
        monkeypatch.setenv("AGENT_SSH_KNOWN_HOSTS", "known_hosts_test")

    @pytest.mark.asyncio
    async def test_unknown_alias_rejected(self, monkeypatch):
        out = await ssh_run("evilbox", "free")
        assert "拒绝" in out and "AGENT_SSH_HOSTS" in out

    @pytest.mark.asyncio
    async def test_unknown_action_rejected(self):
        out = await ssh_run("istore", "free; rm -rf /")
        assert "拒绝" in out and "free" in out  # 提示里带可用菜单

    @pytest.mark.asyncio
    async def test_missing_credential_rejected(self, monkeypatch):
        monkeypatch.delenv("AGENT_SSH_KEY_ISTORE", raising=False)
        monkeypatch.delenv("AGENT_SSH_PASSWORD_ISTORE", raising=False)
        out = await ssh_run("istore", "free")
        assert "拒绝" in out and "AGENT_SSH_KEY_ISTORE" in out

    @pytest.mark.asyncio
    async def test_world_readable_key_treated_as_missing(self, monkeypatch, tmp_path):
        key = tmp_path / "id_ed25519"
        key.write_text("x")
        os.chmod(key, 0o644)
        monkeypatch.setenv("AGENT_SSH_KEY_ISTORE", str(key))
        monkeypatch.delenv("AGENT_SSH_PASSWORD_ISTORE", raising=False)
        out = await ssh_run("istore", "free")
        assert "拒绝" in out and "凭据" in out


class TestSshRunExecution:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("AGENT_SSH_HOSTS", "istore=root@192.168.1.2:22")
        monkeypatch.setenv("AGENT_SSH_KNOWN_HOSTS", "known_hosts_test")

    @staticmethod
    def _capture(monkeypatch, proc):
        seen = {}

        async def fake_exec(*argv, env=None, **kwargs):
            seen["argv"] = list(argv)
            seen["env"] = env
            return proc

        monkeypatch.setattr(S.asyncio, "create_subprocess_exec", fake_exec)
        return seen

    @pytest.mark.asyncio
    async def test_password_mode_never_in_argv(self, monkeypatch, caplog):
        monkeypatch.setenv("AGENT_SSH_PASSWORD_ISTORE", "s3cret-pw")
        seen = self._capture(monkeypatch, _FakeProc([b"Mem: 1024\n"]))
        with caplog.at_level("DEBUG"):
            out = await ssh_run("istore", "meminfo")
        assert out == "Mem: 1024\n"
        argv, env = seen["argv"], seen["env"]
        assert argv[0] == "sshpass" and argv[1] == "-e"
        # 密码只经环境变量进 sshpass；argv/任何位置都不出现
        assert "s3cret-pw" not in " ".join(argv)
        assert env["SSHPASS"] == "s3cret-pw"
        # docstring「不进审计日志」半边的锁定（REVIEW L12）：日志值在 env 里
        # 真实存在，非恒真断言——往日志里加 env 打印本用例必红
        assert "s3cret-pw" not in caplog.text
        # 远端命令是常量；`--` 在 destination 之前终结本地选项解析
        # （host 正则允许 - 开头，不隔离会被 ssh 当选项；REVIEW L13）
        assert argv[-3] == "--"
        assert argv[-2] == "root@192.168.1.2"
        assert argv[-1] == "cat /proc/meminfo"
        for opt in (
            "BatchMode=yes",
            "ConnectTimeout=5",
            "StrictHostKeyChecking=accept-new",
            "IdentitiesOnly=yes",
        ):
            assert opt in argv, opt
        assert os.devnull in argv  # -F /dev/null：不读 bot 自己的 ssh config
        # REVIEW M4：LogLevel=ERROR 会吞掉 INFO 级的 TOFU「Permanently added」
        # 首连警告（本机 sshd 实测），首连 MITM 检测信号必须保留
        assert not any(str(a).startswith("LogLevel=") for a in argv)

    @pytest.mark.asyncio
    async def test_known_hosts_inside_workspace_rejected(self, monkeypatch, tmp_path):
        """REVIEW L15：known_hosts 落在 fs 可写工作区内 = 注入可预埋 pinned
        host key（TOFU 信任根失守），必须 fail-closed。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("WORKSPACE_DIR", str(ws))
        monkeypatch.setenv("AGENT_SSH_KNOWN_HOSTS", str(ws / "ssh" / "known_hosts"))
        monkeypatch.setenv("AGENT_SSH_PASSWORD_ISTORE", "pw")
        out = await ssh_run("istore", "free")
        assert "拒绝" in out and "known_hosts" in out
        assert "AGENT_SSH_KNOWN_HOSTS" in out

    @pytest.mark.asyncio
    async def test_key_mode(self, monkeypatch, tmp_path):
        key = tmp_path / "id_ed25519"
        key.write_text("k")
        os.chmod(key, 0o600)
        monkeypatch.setenv("AGENT_SSH_KEY_ISTORE", str(key))
        seen = self._capture(monkeypatch, _FakeProc())
        await ssh_run("istore", "free")
        argv, env = seen["argv"], seen["env"]
        assert argv[0] == "ssh"
        assert argv[argv.index("-i") + 1] == str(key)
        assert "SSHPASS" not in env

    @pytest.mark.asyncio
    async def test_action_whitelist_is_constant_command(self, monkeypatch):
        """LLM 报的 action 只用于查表；exec 到远端的是常量串。"""
        seen = self._capture(monkeypatch, _FakeProc())
        monkeypatch.setenv("AGENT_SSH_PASSWORD_ISTORE", "pw")
        await ssh_run("istore", "dmesg")
        assert seen["argv"][-1] == "dmesg | tail -30"

    @pytest.mark.asyncio
    async def test_timeout_returns_note(self, monkeypatch):
        monkeypatch.setattr(S, "_SSH_TIMEOUT", 0.5)
        monkeypatch.setenv("AGENT_SSH_PASSWORD_ISTORE", "pw")
        self._capture(monkeypatch, _HangProc())
        out = await ssh_run("istore", "free")
        assert "超时" in out

    @pytest.mark.asyncio
    async def test_output_capped(self, monkeypatch):
        """双层截断：流式 8000B 封顶 + 返回 4000 字符封顶，不会全量进 prompt。"""
        monkeypatch.setenv("AGENT_SSH_PASSWORD_ISTORE", "pw")
        big = b"x" * (S._MAX_OUTPUT_BYTES + 4096)
        self._capture(monkeypatch, _FakeProc([big]))
        out = await ssh_run("istore", "ps")
        assert "截断" in out
        assert len(out) < S._MAX_OUTPUT_CHARS + 200
