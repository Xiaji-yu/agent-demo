"""容器化部署物的静态守卫（tests/test_docker_deploy.py）。

来源：M1（REVIEW-4d5c1b8..63f9444）——`scripts/docker-entrypoint.sh` 首行是注释、
没有 shebang，而 Dockerfile 用 exec-form `ENTRYPOINT`。结果 `docker run`（不带
`--init`）直接 `exec format error` 退出 255；compose 能跑**只因为** `init: true`
让 tini 用 glibc `execvp` 派生子进程、ENOEXEC 时静默回退 `/bin/sh`。构建期冒烟在
builder 阶段跑 `python -c "import bot"`，从不执行 ENTRYPOINT，所以抓不到。

为什么用静态断言而不是跑 docker：这些是「镜像/编排的形态不变量」，改一行就该红，
秒级可跑、CI 无需 docker daemon。真机验收（起容器、写文件、跑备份、渲图）仍按
AGENTS.md §5 的教训逐项实测，静态守卫不替代它。

同时锁住的既有不变量：不整卷挂 /app/data（遮字体）、db 端口只绑回环、PG 客户端取
postgres:16-bookworm、镜像不设 USER app、stdout 日志有上限。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
ENTRYPOINT = (REPO / "scripts" / "docker-entrypoint.sh").read_text(encoding="utf-8")


def _entry_script_paths() -> list[Path]:
    """Dockerfile 的 exec-form ENTRYPOINT 指向的脚本（相对仓库根）。"""
    m = re.search(r'^ENTRYPOINT\s*\["([^"]+)"', DOCKERFILE, re.M)
    assert m, "Dockerfile 应使用 exec-form ENTRYPOINT"
    return [REPO / "scripts" / m.group(1)]


class TestEntrypointShebang:
    """M1：exec-form ENTRYPOINT 的脚本必须有 shebang。

    runc 原生 `execve` 对无 shebang 的可执行文件返回 ENOEXEC；只有 tini 的
    `execvp` 会回退 `/bin/sh`。不依赖 `init: true` 这一偶然机制。
    """

    def test_entry_script_has_shebang(self):
        for path in _entry_script_paths():
            assert path.exists(), path
            first = path.read_text(encoding="utf-8").splitlines()[0]
            assert first.startswith("#!"), (
                f"{path.name} 首行必须是 shebang，实为 {first!r}"
            )

    def test_every_script_under_scripts_dir_has_shebang(self):
        """所有 scripts/*.sh 都该有——免得下一个入口脚本重犯。"""
        missing = [
            p.name
            for p in (REPO / "scripts").glob("*.sh")
            if not p.read_text(encoding="utf-8").startswith("#!")
        ]
        assert not missing, f"这些 shell 脚本缺 shebang：{missing}"

    def test_entry_script_is_posix_sh_compatible(self):
        """脚本由 dash 解释（shebang 是 /bin/sh）：不得出现 bashism。"""
        assert ENTRYPOINT.startswith("#!/bin/sh"), ENTRYPOINT.splitlines()[0]
        for bashism in ("[[", "source ", "<<<", "function ", "&>"):
            assert bashism not in ENTRYPOINT, f"bashism {bashism!r} 在 dash 下不可用"


class TestComposeInvariants:
    """compose 编排不变量（AGENTS.md §4「容器化」行逐条对应）。"""

    def test_data_dir_not_mounted_whole(self):
        """绝不整卷挂 /app/data——那会遮掉镜像内的 data/fonts，文字卡片静默失败。"""
        for svc in COMPOSE["services"].values():
            for vol in svc.get("volumes", []):
                assert not vol.endswith(":/app/data"), vol

    def test_db_port_bound_to_loopback(self):
        ports = COMPOSE["services"]["db"].get("ports", [])
        assert ports, "db 应保留「从宿主查数据」的开口子"
        for spec in ports:
            assert str(spec).startswith("127.0.0.1:"), spec

    def test_both_services_have_log_caps(self):
        """M5：容器 stdout 默认不轮转，而 bot 日志含聊天正文前 200 字。"""
        for name, svc in COMPOSE["services"].items():
            logging = svc.get("logging") or {}
            opts = logging.get("options") or {}
            assert "max-size" in opts, f"{name} 缺 logging max-size"
            assert "max-file" in opts, f"{name} 缺 logging max-file"

    def test_bot_has_init_for_signal_forwarding(self):
        assert COMPOSE["services"]["bot"].get("init") is True

    def test_no_plaintext_credentials(self):
        """凭据一律走 env 插值，不写进受版本控制的 compose。"""
        text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        assert "Yll@" not in text
        for secret in ("POSTGRES_PASSWORD", "POSTGRES_USER", "DATABASE_URL"):
            # 行首锚定：否则会命中 DATABASE_URL 值里的 ${POSTGRES_PASSWORD:-…}
            for m in re.finditer(rf"^\s*{secret}\s*:\s*(\S+)", text, re.M):
                value = m.group(1)
                # 允许 ${VAR:-default} / postgresql://${…} 形态（默认值是文档化的开发默认值）
                assert value.startswith("${") or value.startswith("postgresql://${"), (
                    f"{secret} 疑似硬编码：{value}"
                )


class TestDockerfileInvariants:
    def test_no_user_app_directive(self):
        """镜像刻意不设 USER app：属权要在 root 下修，再 gosu 降权。"""
        assert not re.search(r"^USER\s+app\b", DOCKERFILE, re.M)

    def test_pg_client_from_bookworm_tag(self):
        assert "postgres:16-bookworm" in DOCKERFILE

    def test_build_smoke_imports_bot(self):
        assert re.search(r'python -c "import bot"', DOCKERFILE)

    def test_dockerignore_keeps_fonts(self):
        text = (REPO / ".dockerignore").read_text(encoding="utf-8")
        # data/* 排除后必须 negate 放行 fonts，否则镜像里没有中文字体
        assert re.search(r"^!data/fonts", text, re.M), text


class TestEntrypointWritabilitySelfCheck:
    """M6/L11：挂载点可写性必须自检，且 root 分支要降权后测。

    日志/归档/账本对不可写目录全是容错降级（只 WARNING/ERROR 不崩），不喊一声
    的话故障表现是「日志只剩控制台、账本数字不动」——极难排查。root 的 `[ -w ]`
    恒真，所以顶层 chown 之后还要以 app 身份再测一次（顶层 chown 覆盖不到早年
    以 root 落下的异属主文件）。
    """

    def test_non_root_branch_checks_writability(self):
        assert "-w" in ENTRYPOINT, "应有 [ -w ] 自检"
        assert "ERROR" in ENTRYPOINT

    def test_root_branch_rechecks_after_dropping_privileges(self):
        """root 分支不能只 chown 就 exec——那测不出降权后的真实可写性。"""
        assert "gosu app" in ENTRYPOINT, "root 分支应降权到 app"
        assert "check_writable" in ENTRYPOINT
        # 降权后的自检必须在 exec 目标命令之前
        assert ENTRYPOINT.index("check_writable") < ENTRYPOINT.rindex('exec "$@"')

    def test_check_is_not_fatal(self):
        """不可写是降级不是致命：打 ERROR 但不得 exit 非零（否则容器起不来）。"""
        assert "exit 1" not in ENTRYPOINT

    def test_mount_list_covers_all_runtime_dirs(self):
        """自检目录集必须与 compose 的挂载子目录一致，漏一个就少喊一类故障。"""
        for d in (
            "logs",
            "archive",
            "backups",
            "budget",
            "workspace",
            "cache",
            "sessions",
            "privacy",
            "skills",
            "kb_samples",
        ):
            assert d in ENTRYPOINT, d
