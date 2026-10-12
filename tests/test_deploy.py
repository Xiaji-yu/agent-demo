"""scripts/deploy.py 一键部署脚本的单测（2026-10-11 新增）。

覆盖纯函数与带副作用的命令（env 不覆盖 / check 的 soft 分级 / unit 渲染安全面）。
按仓库纪律：每个断言都做过「改坏实现 → 必失败」的变异复核（见文件末注释）。
"""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_deploy():
    spec = importlib.util.spec_from_file_location(
        "deploy_script", ROOT / "scripts" / "deploy.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


deploy = _load_deploy()


class TestEnvParser:
    """.env 解析：引号/注释/空行/行内 # 不误伤。"""

    def test_parses_key_values(self, tmp_path: Path):
        p = tmp_path / ".env"
        p.write_text(
            "# 注释\n\nLLM_BASE_URL=https://api.example.com/v1\nLLM_MODEL=step-3\n",
            encoding="utf-8",
        )
        env = deploy.read_env_file(p)
        assert env == {
            "LLM_BASE_URL": "https://api.example.com/v1",
            "LLM_MODEL": "step-3",
        }

    def test_strips_matching_quotes_only(self, tmp_path: Path):
        p = tmp_path / ".env"
        p.write_text('A="x"\nB=\'y\'\nC="unmatched\nD=plain\n', encoding="utf-8")
        env = deploy.read_env_file(p)
        assert env["A"] == "x" and env["B"] == "y"
        assert env["C"] == '"unmatched' and env["D"] == "plain"

    def test_keeps_hash_inside_value(self, tmp_path: Path):
        """行内 # 不是注释（URL fragment/密码可能含 #）——只剥离开头即 # 的整行。"""
        p = tmp_path / ".env"
        p.write_text("K=v#not-comment\n#FULL=comment\n", encoding="utf-8")
        env = deploy.read_env_file(p)
        assert env == {"K": "v#not-comment"}

    def test_strips_export_prefix(self, tmp_path: Path):
        """M9（REVIEW-1e0f5ae..a0a6bb4）：shell 风格 ``export KEY=…`` 与 dotenv
        同口径——旧实现把「export SUPERUSERS」当键名，通配 ``*`` 检测漏报。"""
        p = tmp_path / ".env"
        p.write_text(
            "export SUPERUSERS=*\nexport  LLM_MODEL=step-3\nKEY=plain\n",
            encoding="utf-8",
        )
        env = deploy.read_env_file(p)
        assert env == {"SUPERUSERS": "*", "LLM_MODEL": "step-3", "KEY": "plain"}

    def test_unquoted_inline_comment_stripped_like_dotenv(self, tmp_path: Path):
        """M9：未引值的行内注释按 dotenv 口径剥掉（check 判定值 = 运行时值）。"""
        p = tmp_path / ".env"
        p.write_text(
            'FOO=bar # comment\nQ="a # b"\nR="bar" # c\nS=keep #1\n',
            encoding="utf-8",
        )
        env = deploy.read_env_file(p)
        assert env["FOO"] == "bar"
        assert env["Q"] == "a # b", "引号内的 # 是值的一部分，不得剥"
        assert env["R"] == "bar", "注释剥掉后露出的引号包裹也要剥"
        assert env["S"] == "keep", "「 #」即注释起点（dotenv 口径）；字面 # 请加引号"


class TestDsnParsing:
    def test_basic(self):
        assert deploy.dsn_host_port("postgresql://u:p@127.0.0.1:5432/db") == (
            "127.0.0.1",
            5432,
        )

    def test_encoded_at_in_password(self):
        """密码含 @ 必须 URL 编码——与 asyncpg 按第一个 @ 分区的口径一致。"""
        host, port = deploy.dsn_host_port("postgresql://u:p%40ss@db.host:6543/x")
        assert (host, port) == ("db.host", 6543)

    def test_defaults_and_garbage(self):
        assert deploy.dsn_host_port("postgresql://u:p@/db") == ("127.0.0.1", 5432)
        assert deploy.dsn_host_port("not a dsn") == ("127.0.0.1", 5432)
        # 端口非数字时 urlsplit.port 抛 ValueError——必须被吞成默认值而不是上抛
        assert deploy.dsn_host_port("postgresql://u:p@host:bad/db") == (
            "127.0.0.1",
            5432,
        )


class TestVersionGate:
    def test_floor(self):
        assert deploy.python_ok((3, 11, 0)) is True
        assert deploy.python_ok((3, 12, 3)) is True

    def test_below_floor(self):
        assert deploy.python_ok((3, 10, 9)) is False
        assert deploy.python_ok((2, 7, 18)) is False


class TestUnitRendering:
    """unit 安全面：绝对路径、零密钥、写面收敛到 data/。"""

    def test_absolute_paths_and_no_secrets(self):
        unit = deploy.render_unit(
            Path("/srv/agent-demo"), Path("/srv/agent-demo/.venv/bin/python")
        )
        assert "WorkingDirectory=/srv/agent-demo\n" in unit
        assert "EnvironmentFile=/srv/agent-demo/.env\n" in unit
        assert "ExecStart=/srv/agent-demo/.venv/bin/python bot.py\n" in unit
        # M10（REVIEW-1e0f5ae..a0a6bb4）：``-`` 前缀容许 data/ 缺席（fresh clone
        # 首启不被 mount namespace 搭建失败卡死）
        assert "ReadWritePaths=-/srv/agent-demo/data\n" in unit
        assert "Restart=always" in unit  # /reboot 执行链末端依赖它
        for secret in ("API_KEY", "BASE_URL", "TOKEN", "PASSWORD"):
            assert secret not in unit, (
                f"unit 不得出现 {secret}——密钥只走 EnvironmentFile"
            )

    def test_run_as_and_user_mode(self):
        unit = deploy.render_unit(
            Path("/srv/a"), Path("/srv/a/.venv/bin/python"), run_as="qqbot"
        )
        assert "User=qqbot\nGroup=qqbot" in unit
        assert "WantedBy=multi-user.target" in unit
        user_unit = deploy.render_unit(
            Path("/srv/a"), Path("/srv/a/.venv/bin/python"), user_mode=True
        )
        assert "WantedBy=default.target" in user_unit
        assert (
            "User=" not in user_unit
        )  # 用户级不写 User=（systemd --user 自己就是该用户）


class TestEnvCommand:
    """env 子命令：不覆盖已存在的 .env（可能含已填好的密钥）。"""

    def _patch_paths(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(deploy, "ENV_EXAMPLE", tmp_path / ".env.example")
        monkeypatch.setattr(deploy, "ENV_FILE", tmp_path / ".env")
        (tmp_path / ".env.example").write_text(
            "LLM_MODEL=x\nSUPERUSERS=\n", encoding="utf-8"
        )

    def test_creates_with_0600(self, monkeypatch, tmp_path: Path):
        self._patch_paths(monkeypatch, tmp_path)
        assert deploy.cmd_env() == 0
        env_file = tmp_path / ".env"
        assert env_file.is_file()
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    def test_refuses_overwrite(self, monkeypatch, tmp_path: Path):
        self._patch_paths(monkeypatch, tmp_path)
        assert deploy.cmd_env() == 0
        (tmp_path / ".env").write_text("LLM_MODEL=mine\n", encoding="utf-8")
        assert deploy.cmd_env() == 1  # 拒绝覆盖
        assert deploy.read_env_file(tmp_path / ".env") == {"LLM_MODEL": "mine"}

    def test_force_backs_up_and_rewrites(self, monkeypatch, tmp_path: Path):
        self._patch_paths(monkeypatch, tmp_path)
        assert deploy.cmd_env() == 0
        (tmp_path / ".env").write_text("LLM_MODEL=mine\n", encoding="utf-8")
        # L22（REVIEW-1e0f5ae..a0a6bb4）：旧 .env 曾被放宽时（0644），备份也必须
        # 收回 0600——rename 继承旧权限，声称「0600」不能只靠运气
        (tmp_path / ".env").chmod(0o644)
        assert deploy.cmd_env(force=True) == 0
        assert deploy.read_env_file(tmp_path / ".env") == {
            "LLM_MODEL": "x",
            "SUPERUSERS": "",
        }
        backups = list(tmp_path.glob(".env.bak-*"))
        assert len(backups) == 1
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


class TestCheckSoftMode:
    """check 的 soft 分级：all 流程里配置类问题只警告，放行门由 strict check 把守。"""

    def _patch_env(self, monkeypatch, tmp_path: Path, content: str):
        monkeypatch.setattr(deploy, "ENV_FILE", tmp_path / ".env")
        (tmp_path / ".env").write_text(content, encoding="utf-8")
        # 不隐式继承本地 .env（CI #91 同款教训：测试环境必须用例内显式给）
        for key in (
            "DATABASE_URL",
            "LLM_API_KEY",
            "LLM_BASE_URL",
            "LLM_MODEL",
            "SUPERUSERS",
        ):
            monkeypatch.delenv(key, raising=False)

    def test_strict_fails_on_placeholder_keys(
        self, monkeypatch, tmp_path: Path, capsys
    ):
        self._patch_env(
            monkeypatch, tmp_path, "LLM_BASE_URL=...\nLLM_API_KEY=...\nLLM_MODEL=...\n"
        )
        assert deploy.cmd_check() == 1
        out = capsys.readouterr().out
        assert "[FAIL] LLM_API_KEY 未填" in out

    def test_soft_passes_on_placeholder_keys(self, monkeypatch, tmp_path: Path, capsys):
        self._patch_env(
            monkeypatch, tmp_path, "LLM_BASE_URL=...\nLLM_API_KEY=...\nLLM_MODEL=...\n"
        )
        assert deploy.cmd_check(soft=True) == 0
        out = capsys.readouterr().out
        assert "[FAIL]" not in out and "[warn] LLM_API_KEY 未填" in out

    def test_strict_fails_on_wildcard_superusers(
        self, monkeypatch, tmp_path: Path, capsys
    ):
        self._patch_env(
            monkeypatch,
            tmp_path,
            "LLM_BASE_URL=u\nLLM_API_KEY=k\nLLM_MODEL=m\nSUPERUSERS=*\n",
        )
        assert deploy.cmd_check() == 1
        assert "通配" in capsys.readouterr().out

    def test_strict_passes_when_filled(self, monkeypatch, tmp_path: Path):
        self._patch_env(
            monkeypatch,
            tmp_path,
            "LLM_BASE_URL=https://api.example.com/v1\nLLM_API_KEY=sk-real\n"
            "LLM_MODEL=step-3\nSUPERUSERS=12345\n",
        )
        assert deploy.cmd_check() == 0

    def test_missing_env_file_fails_both_modes(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(deploy, "ENV_FILE", tmp_path / ".env.nope")
        for key in (
            "DATABASE_URL",
            "LLM_API_KEY",
            "LLM_BASE_URL",
            "LLM_MODEL",
            "SUPERUSERS",
        ):
            monkeypatch.delenv(key, raising=False)
        assert deploy.cmd_check(soft=True) == 1
        assert deploy.cmd_check() == 1


# 变异复核记录（2026-10-11，按 AGENTS.md §2.7 逐条「改坏实现 → 必失败」）：
# - render_unit 模板注入一行 Environment=LLM_API_KEY=x → test_absolute_paths_and_no_secrets 失败
# - cmd_env 去掉 is_file 拒绝分支 → test_refuses_overwrite 失败（文件被改写）
# - cmd_check 忽略 soft 参数（两种模式同样计数）→ test_soft_passes_on_placeholder_keys 失败
# - read_env_file 改成剥行内 # → test_keeps_hash_inside_value 失败
# - dsn_host_port 去掉 urlsplit 的 try → test_defaults_and_garbage 失败（ValueError 上抛）
