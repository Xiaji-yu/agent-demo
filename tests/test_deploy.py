"""scripts/deploy.py 一键部署脚本的单测（2026-10-11 新增）。

覆盖纯函数与带副作用的命令（env 不覆盖 / check 的 soft 分级 / unit 渲染安全面）。
按仓库纪律：每个断言都做过「改坏实现 → 必失败」的变异复核（见文件末注释）。
"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import yaml

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

    def test_creates_strict_no_post_chmod(self, monkeypatch, tmp_path: Path):
        """C7（REVIEW 2026-11-02）：.env 含全部凭据，必须创建即 0600。

        与 test_creates_with_0600 的分工：那条断言落盘权限位（POSIX-only），
        这条断言**行为**——fchmod 被以 0o600 调用、且主写入上没有事后 chmod
        （备份路径的 chmod 是有意的，见 L22）。Windows 上也能跑。
        """
        self._patch_paths(monkeypatch, tmp_path)
        seen: list[int] = []
        chmod_paths: list[str] = []
        real_fchmod = os.fchmod
        real_chmod = os.chmod

        def _spy_fchmod(fd, mode):
            seen.append(mode)
            return real_fchmod(fd, mode)

        def _spy_chmod(path, mode):
            chmod_paths.append(str(path))
            return real_chmod(path, mode)

        monkeypatch.setattr(os, "fchmod", _spy_fchmod)
        monkeypatch.setattr(os, "chmod", _spy_chmod)
        assert deploy.cmd_env() == 0
        env_file = tmp_path / ".env"
        assert env_file.is_file()
        assert seen == [0o600]
        # 主写入不得依赖事后 chmod（备份路径的 chmod 在 force 分支里，这里没触发）
        assert chmod_paths == []
        assert deploy.read_env_file(env_file) == {
            "LLM_MODEL": "x",
            "SUPERUSERS": "",
        }

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


class TestDockerBootstrap:
    """容器部署自举（2026-10-28）：`.env` + data 目录 + compose 预检。

    用户反馈原话是「还要自己手动拷贝 .env config.yaml」「还得自己新建 data 文件夹，
    要不然就会报错」——根因是 `docker-compose.yml` 的 `env_file: .env` 对**解析期**
    就要求文件存在（实测 fresh clone 上 `docker compose config` 直接
    `env file …/.env not found`，连 db 都起不来），而模板是隐藏文件、目录也没有。
    `cmd_docker` 把这三步压成一条幂等命令。
    """

    TEMPLATE = "LLM_BASE_URL=...\nLLM_API_KEY=...\nLLM_MODEL=...\nSUPERUSERS=\n"

    def _patch(
        self, monkeypatch, tmp_path, *, env_text=None, rc=0, stderr="", have_docker=True
    ):
        """把脚本的路径常量全挪到 tmp（绝不碰仓库里的真 .env / 真 data/）。"""
        calls = []

        def fake_run(argv, *, cwd=None, timeout=None):
            calls.append(list(argv))
            return SimpleNamespace(returncode=rc, stdout="", stderr=stderr)

        monkeypatch.setattr(deploy, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(deploy, "ENV_EXAMPLE", tmp_path / ".env.example")
        monkeypatch.setattr(deploy, "ENV_FILE", tmp_path / ".env")
        monkeypatch.setattr(deploy, "COMPOSE_FILE", tmp_path / "docker-compose.yml")
        (tmp_path / ".env.example").write_text(self.TEMPLATE, encoding="utf-8")
        (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        if env_text is not None:
            (tmp_path / ".env").write_text(env_text, encoding="utf-8")
        monkeypatch.setattr(
            deploy, "_have", lambda exe: have_docker and exe == "docker"
        )
        monkeypatch.setattr(deploy, "_run", fake_run)
        return calls

    def test_data_dirs_match_compose_mounts(self):
        """脚本建目录的清单必须与 compose 的实际挂载点一一对应。

        两处各写一份清单是必然漂移的：漏一个目录 = 容器里某个功能静默降级，
        多一个 = 宿主多出一堆空目录。这条断言把两侧钉在一起。
        """
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        mounts = {
            vol.rsplit(":", 1)[1].split("/app/data/", 1)[1]
            for vol in compose["services"]["bot"]["volumes"]
            if ":/app/data/" in vol
        }
        assert mounts, "compose 里应有一批 /app/data/ 子目录挂载"
        assert mounts == set(deploy.DOCKER_DATA_DIRS)

    def test_generates_env_and_creates_dirs(self, monkeypatch, tmp_path, capsys):
        calls = self._patch(monkeypatch, tmp_path)
        assert deploy.cmd_docker() == 0
        env_file = tmp_path / ".env"
        assert env_file.is_file()
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        for name in deploy.DOCKER_DATA_DIRS:
            assert (tmp_path / "data" / name).is_dir(), name
        out = capsys.readouterr().out
        assert ".env" in out and "运行期目录就绪" in out
        # 不带 --up 时绝不起服务：部署面的硬边界是不自动启动
        assert calls == [["docker", "compose", "config", "-q"]]

    def test_keeps_existing_env(self, monkeypatch, tmp_path):
        """已有 .env 是用户的密钥文件，绝不能覆盖。"""
        self._patch(monkeypatch, tmp_path, env_text="LLM_MODEL=mine\n")
        assert deploy.cmd_docker() == 0
        assert (tmp_path / ".env").read_text(encoding="utf-8") == "LLM_MODEL=mine\n"

    def test_respects_agent_data_dir(self, monkeypatch, tmp_path):
        """AGENT_DATA_DIR 是 compose 插值变量，脚本必须同口径解析。"""
        custom = tmp_path / "mnt" / "agentdata"
        self._patch(monkeypatch, tmp_path, env_text=f"AGENT_DATA_DIR={custom}\n")
        assert deploy.cmd_docker() == 0
        assert (custom / "logs").is_dir()
        assert not (tmp_path / "data").exists()

    def test_relative_agent_data_dir_is_relative_to_repo_root(
        self, monkeypatch, tmp_path
    ):
        """compose 的相对路径相对 compose 文件（仓库根）解析，不是 CWD。"""
        self._patch(monkeypatch, tmp_path, env_text="AGENT_DATA_DIR=vol/data\n")
        assert deploy.cmd_docker() == 0
        assert (tmp_path / "vol" / "data" / "logs").is_dir()

    def test_up_runs_compose_up(self, monkeypatch, tmp_path):
        calls = self._patch(monkeypatch, tmp_path)
        assert deploy.cmd_docker(up=True) == 0
        assert ["docker", "compose", "up", "-d", "db", "bot"] in calls

    def test_placeholder_keys_warn_without_blocking(
        self, monkeypatch, tmp_path, capsys
    ):
        """占位密钥只警告：允许先起库、后填 key（严格放行门是 check 的职责）。"""
        self._patch(monkeypatch, tmp_path)
        assert deploy.cmd_docker() == 0
        out = capsys.readouterr().out
        assert "[warn] LLM_BASE_URL 仍是占位" in out

    def test_compose_preflight_failure_is_reported(self, monkeypatch, tmp_path, capsys):
        self._patch(monkeypatch, tmp_path, rc=1, stderr="env file not found")
        assert deploy.cmd_docker() == 1
        assert "预检失败" in capsys.readouterr().out

    def test_missing_docker_fails(self, monkeypatch, tmp_path, capsys):
        self._patch(monkeypatch, tmp_path, have_docker=False)
        assert deploy.cmd_docker() == 1
        assert "没有 docker" in capsys.readouterr().out


class TestDocsComposeSingleSource:
    """docs/ 下不得出现第二份 compose，NAS 文档不得引用未合入的配置路线。

    2026-11-03 PR #17 把 UGREEN 分支的 `docs/deploy-nas/docker-compose.yml` 与
    `docs/deploy-nas.md` 一起搬进了 main：那份 compose 依赖 UGREEN 独有的
    `config/agent.json` 主配置与 `scripts/compose_init.py` 自举（main 从未合入），
    照文档做必然起不来。两条根因各钉一处：

    - 「第二份 compose」与根 compose 的挂载点/语义必然漂移（根 compose 的清单已由
      `test_data_dirs_match_compose_mounts` 锁死，docs/ 这份没人锁）；
    - 文档引用不存在的配置路线时，没有任何测试会红——错误直接落到部署者头上。
    """

    #: main 上不存在的配置路线/文件/命令（出现即说明又从别的分支搬了东西）
    FORBIDDEN = ("agent.json", "init /out", "compose_init", "config-quickstart")

    def test_no_compose_file_under_docs(self):
        drifted = sorted(
            p.relative_to(ROOT).as_posix()
            for p in (ROOT / "docs").rglob("*compose*.y*ml")
        )
        assert drifted == [], f"docs/ 下不应有第二份 compose（必然漂移）：{drifted}"

    def test_nas_doc_has_no_unmerged_route(self):
        doc = ROOT / "docs" / "deploy-nas.md"
        if not doc.is_file():
            return  # 文档可被整体移除，不强制存在
        text = doc.read_text(encoding="utf-8")
        hits = [s for s in self.FORBIDDEN if s in text]
        assert hits == [], f"NAS 文档引用了 main 上不存在的配置路线：{hits}"

    def test_nas_doc_env_keys_all_documented(self):
        """文档里出现的每个配置键都必须能在 `.env.example` 找到。

        防的是另一类漂移：文档教人设一个代码根本不读的键（`AGENT_PUSH_STATE_FILE`
          就是这么留下的），或漏掉一个必填键。与 test_repo_guards 的
        「代码读的 env 必须在 .env.example」是反方向，互补。
        """
        doc = ROOT / "docs" / "deploy-nas.md"
        if not doc.is_file():
            return
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        text = doc.read_text(encoding="utf-8")
        # 只查「配置键」：反引号包住的键（`KEY` 或 `KEY=value`）与赋值形态的 KEY=。
        # 正文里的大写词（GHCR/SIGKILL/DX4600…）、构建参数（APT_MIRROR）与 compose
        # 服务级变量（POSTGRES_*）不是 .env 键，靠下面的前缀/白名单过滤掉。
        mentioned = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})(?:=[^`]*)?`", text))
        mentioned |= set(re.findall(r"\b([A-Z][A-Z0-9_]{3,})=", text))
        plain = {"SUPERUSERS", "ALLOWED_GROUPS", "DATABASE_URL", "TZ", "PORT", "HOST"}
        candidates = {
            k
            for k in mentioned
            if k.startswith(
                ("AGENT_", "LLM_", "ONEBOT_", "NAPCAT_", "EMBEDDING_", "SEARCH_")
            )
            or k in plain
        }
        missing = sorted(k for k in candidates if f"{k}=" not in env_example)
        assert missing == [], f"NAS 文档提到但 .env.example 没有的键：{missing}"


class TestInstallUsesUvLock:
    """M6（REVIEW-ee39c8e..e55cab5）：部署安装必须走与 CI 同源的 uv.lock 路径。

    ``pip install -e`` 用另一套解析器、完全绕开锁文件；README 却承诺
    「uv.lock 是唯一锁文件」——两条安装路径会让 CI 绿 ≠ 部署可复现。
    """

    def _patch(self, monkeypatch, tmp_path, *, have_uv=True, rc=0, stderr=""):
        calls = []
        py = tmp_path / "python"
        py.write_text("", encoding="utf-8")

        def fake_run(argv, *, cwd=None, timeout=None):
            calls.append({"argv": argv, "cwd": cwd, "timeout": timeout})
            return SimpleNamespace(returncode=rc, stdout="", stderr=stderr)

        monkeypatch.setattr(deploy, "_have", lambda exe: have_uv and exe == "uv")
        monkeypatch.setattr(deploy, "_run", fake_run)
        monkeypatch.setattr(deploy, "venv_python", lambda: py)
        return calls

    def test_dev_install_uses_locked_uv_sync(self, monkeypatch, tmp_path):
        calls = self._patch(monkeypatch, tmp_path)
        assert deploy.cmd_install(dev=True) == 0
        assert calls[0]["argv"] == ["uv", "sync", "--locked", "--extra", "dev"]
        assert calls[0]["cwd"] == deploy.REPO_ROOT
        assert all("pip" not in " ".join(c["argv"]) for c in calls)

    def test_force_requests_reinstall(self, monkeypatch, tmp_path):
        calls = self._patch(monkeypatch, tmp_path)
        assert deploy.cmd_install(force=True) == 0
        assert calls[0]["argv"] == ["uv", "sync", "--locked", "--reinstall"]

    def test_missing_uv_fails_without_pip_fallback(self, monkeypatch, tmp_path, capsys):
        calls = self._patch(monkeypatch, tmp_path, have_uv=False)
        assert deploy.cmd_install() == 1
        out = capsys.readouterr().out
        assert "缺少 uv" in out and "astral.sh/uv" in out
        assert calls == [], "无 uv 时不得回退到 pip（绕过锁文件）"

    def test_lock_mismatch_points_at_uv_lock(self, monkeypatch, tmp_path, capsys):
        self._patch(
            monkeypatch, tmp_path, rc=1, stderr="error: lockfile is out of date"
        )
        assert deploy.cmd_install() == 1
        assert "uv lock" in capsys.readouterr().out


# 变异复核记录（2026-10-11，按 AGENTS.md §2.7 逐条「改坏实现 → 必失败」）：
# - render_unit 模板注入一行 Environment=LLM_API_KEY=x → test_absolute_paths_and_no_secrets 失败
# - cmd_env 去掉 is_file 拒绝分支 → test_refuses_overwrite 失败（文件被改写）
# - cmd_check 忽略 soft 参数（两种模式同样计数）→ test_soft_passes_on_placeholder_keys 失败
# - read_env_file 改成剥行内 # → test_keeps_hash_inside_value 失败
# - dsn_host_port 去掉 urlsplit 的 try → test_defaults_and_garbage 失败（ValueError 上抛）
# - M6（2026-10-25）：cmd_install 改回 pip install -e → test_dev_install_uses_locked_uv_sync
#   失败；去掉 _have("uv") 守卫 → test_missing_uv_fails_without_pip_fallback 失败


class TestWebWriteMounts:
    """容器内 web 受控写面与状态持久化的 compose 接线（P1-2/P1-3，UGREEN 028ed19）。

    `.env`/`config.yaml` 单文件挂载与 `AGENT_ENV_FILE` 环境变量任何一处被删，
    web 写面在容器里都会 500 或写到容器层（recreate 丢失）——静默失效，必须锁。
    """

    def test_compose_mounts_env_and_config_yaml(self):
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        bot = compose["services"]["bot"]
        vols = bot["volumes"]
        assert "./.env:/app/.env" in vols, (
            ".env 必须单文件挂载（env_file 只注入不挂文件）"
        )
        assert "./config.yaml:/app/config.yaml" in vols, (
            "config.yaml 必须单文件挂载（web 写 config.yaml 键的落点；"
            "deploy.py docker 会预建该文件，否则 docker 把单文件挂载当目录创建）"
        )
        assert bot["environment"]["AGENT_ENV_FILE"] == "/app/.env"

    def test_entrypoint_mounts_list_matches_compose(self):
        """entrypoint 的 MOUNTS/FILES 清单与 compose 挂载一一对应（双清单漂移锁）。"""
        import re

        entry = (ROOT / "scripts" / "docker-entrypoint.sh").read_text(encoding="utf-8")
        mounts = re.search(r'MOUNTS="([^"]+)"', entry).group(1).split()
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        data_mounts = {
            vol.rsplit(":", 1)[1].split("/app/data/", 1)[1]
            for vol in compose["services"]["bot"]["volumes"]
            if ":/app/data/" in vol
        }
        assert set(mounts) == data_mounts, (
            "entrypoint 自检清单与 compose 挂载目录不一致（漏 = 不可写不报，多 = 空检查）"
        )
        assert "/app/.env" in entry and "/app/config.yaml" in entry, (
            "entrypoint 必须修两个单文件挂载的属权（web 写面落点）"
        )
