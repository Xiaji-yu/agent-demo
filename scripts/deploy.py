#!/usr/bin/env python
"""agent-demo 一键部署：配置 → 预检 → 依赖 → 数据库 → 冒烟（systemd unit 生成）。

用法::

    python scripts/deploy.py check                # 预检（只读，不动任何东西）
    python scripts/deploy.py env                  # .env.example → .env（已存在则拒绝）
    python scripts/deploy.py install [--dev]      # .venv + pip install -e .
    python scripts/deploy.py db [--down]          # docker compose 起本地 PG（等 healthy）
    python scripts/deploy.py systemd [--install|--uninstall] [--user [--run-as NAME]]
    python scripts/deploy.py smoke                # import + 配置装配 + 守卫测试冒烟
    python scripts/deploy.py all [--skip-db] [--skip-install] [--dev] [--force]

**两条硬边界（部署面安全，评审固化）**：

1. **绝不自动启动 bot**：启动前必须人工编辑 ``.env``（LLM key 等）。``all`` 到
   smoke 为止；``systemd`` 也只生成/安装 unit，**不** ``enable/start``——带着占位
   配置静默跑起来比明确失败更糟（fail-loud）。
2. **不写仓库外文件**：唯一例外是显式 ``systemd --install``（system 级要求 root），
   其余产物全部落在仓库内。unit 内**零密钥**（``EnvironmentFile`` 指向 .env），
   全部子进程用 argv 列表起，不经过 shell。

本脚本**纯标准库**：它必须在 venv 建成之前就能跑（引导自身），因此不 import
项目任何模块（smoke 子命令除外——那时 venv 已就绪）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / ".venv"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_FILE = REPO_ROOT / ".env"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
UNIT_NAME = "agent-demo.service"
MIN_PYTHON = (3, 11)


# ---------------------------------------------------------------- 输出助手


def _ok(msg: str) -> None:
    print(f"[ok] {msg}")


def _warn(msg: str) -> None:
    print(f"[warn] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def _hint(msg: str) -> None:
    print(f"      {msg}")


# ---------------------------------------------------------------- 纯函数（可单测）


def read_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：``KEY=VALUE``，容忍引号/空行/``#`` 注释与行内注释。

    刻意不引第三方库：本脚本要在 venv 建成前运行。行内注释只剥离开头即 ``#``
    的整行——值里的 ``#``（如 base_url 带 fragment）原样保留，避免误伤。

    与运行时权威（python-dotenv）的口径对齐（M9，REVIEW-1e0f5ae..a0a6bb4）：
    - 剥 ``export `` 前缀（dotenv 支持 shell 风格行；不剥会把「export KEY」
      当成键名，SUPERUSERS 通配 ``*`` 检测漏报）；
    - **值内禁行内注释**：dotenv 会把 ``KEY=value # c`` 的值解析为 ``value``，
      本解析器无法无损复刻该行为，故含行内注释的行按 dotenv 口径剥掉
      `` #`` 之后的部分。若确需值内 ``#``（如 DSN 密码），请加引号。
    """
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # 未加引号的行内注释：与 dotenv 同口径剥掉（dotenv 按未引值处理）
            if " #" in value:
                value = value.split(" #", 1)[0].strip()
            # 注释剥掉后可能露出完整引号包裹（``KEY="bar" # c`` → ``"bar"``）
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
        if key:
            env[key] = value
    return env


def dsn_host_port(dsn: str) -> tuple[str, int]:
    """从 postgres DSN 取 (host, port)；解析失败回退 (127.0.0.1, 5432)。

    密码含 ``@`` 时必须 URL 编码（``%40``）——asyncpg 与 urlsplit 同口径，
    见 .env.example 的 DATABASE_URL 注释。
    """
    try:
        parts = urllib.parse.urlsplit(dsn)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 5432
        return host, port
    except ValueError:
        return "127.0.0.1", 5432


def python_ok(version: tuple[int, ...]) -> bool:
    """对齐 pyproject 的 requires-python（>=3.11）。"""
    return tuple(version[:2]) >= MIN_PYTHON


def render_unit(
    repo: Path, venv_py: Path, run_as: str | None = None, user_mode: bool = False
) -> str:
    """渲染 systemd unit。**不含任何密钥**（EnvironmentFile 指向 .env）。

    ProtectSystem=strict 让整个文件系统只读，bot 的写面只有 ``data/``（归档/
    备份/日志/工作区/账本/ssh known_hosts 全在其下）。把 AGENT_*_DIR 指到绝对
    路径的部署，必须把那些目录也加进 ReadWritePaths——unit 注释里写明了。
    """
    user_line = f"User={run_as}\nGroup={run_as}" if run_as and not user_mode else ""
    wanted = "default.target" if user_mode else "multi-user.target"
    return f"""[Unit]
Description=agent-demo (QQ Agent: NoneBot2 reverse-WS + agentcore)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={repo}
EnvironmentFile={repo}/.env
ExecStart={venv_py} bot.py
Restart=always
RestartSec=3
# —— 以下为建议硬化值，按部署环境增删 ——
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
# ProtectSystem=strict 令文件系统整体只读；bot 的写面只有 data/。
# 若 AGENT_ARCHIVE_DIR / AGENT_BACKUP_DIR / AGENT_BUDGET_DIR / AGENT_LOG_DIR /
# WORKSPACE_DIR / AGENT_MUSIC_CACHE_DIR / AGENT_SSH_KNOWN_HOSTS 指到绝对路径，
# 必须把那些目录追加进下面这行（空格分隔多项；目录不存在时加 ``-`` 前缀，
# 否则 mount namespace 搭建失败、服务首启即失败——systemd.exec(5)）。
ReadWritePaths=-{repo}/data
{user_line}

[Install]
WantedBy={wanted}
"""


def venv_python() -> Path:
    """venv 里的 python 路径（Windows 形态顺手兼容，虽然 README 标注不支持）。"""
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


# ---------------------------------------------------------------- 子进程


def _run(
    argv: list[str], *, cwd: Path | None = None, timeout: int | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


def _have(exe: str) -> bool:
    return shutil.which(exe) is not None


# ---------------------------------------------------------------- check


def cmd_check(soft: bool = False) -> int:
    """只读预检：环境、配置就绪度、外部依赖可达性。不动任何东西。

    ``soft=True``（``all`` 用）：配置类问题（LLM 密钥占位/无超管/PG 不可达）降级
    为 warn——一键部署的职责是「把机器准备好」，密钥由人事后填、放行门由单独的
    ``check``（strict）把守。环境类问题（Python 版本）两种模式都 FAIL。
    """
    problems = 0

    if python_ok(sys.version_info):
        _ok(
            f"Python {sys.version.split()[0]}（要求 >={MIN_PYTHON[0]}.{MIN_PYTHON[1]}）"
        )
    else:
        _fail(
            f"Python {sys.version.split()[0]} 低于要求的 >={MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
        )
        _hint("pyproject.toml requires-python；请升级解释器或用更新的 python3 重跑")
        problems += 1

    if sys.platform.startswith("linux"):
        _ok("操作系统 Linux（一等公民平台）")
    elif sys.platform == "darwin":
        _warn(
            "macOS：核心对话可用，运维技能（/proc/systemctl）降级——见 README 平台矩阵"
        )
    else:
        _warn(f"平台 {sys.platform} 不在支持矩阵内（README「平台支持与移植边界」）")

    if _have("docker"):
        _ok("docker 可用（db 子命令可起本地 PG）")
    else:
        _warn("没有 docker：跳过本地 PG 选项（可用外部 DATABASE_URL，或 M0 内存模式）")

    env = read_env_file(ENV_FILE) if ENV_FILE.is_file() else {}
    if ENV_FILE.is_file():
        _ok(f".env 已存在（{len(env)} 个键）")
    else:
        _fail("没有 .env——跑 `python scripts/deploy.py env` 从模板生成")
        problems += 1

    def _config_problem(msg: str, hint: str = "") -> None:
        nonlocal problems
        if soft:
            _warn(msg)
            if hint:
                _hint(hint)
        else:
            _fail(msg)
            if hint:
                _hint(hint)
            problems += 1

    def _is_placeholder(value: str) -> bool:
        v = value.strip()
        return not v or v in {"...", "xxx"} or v.lower().startswith("your")

    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        value = env.get(key) or os.environ.get(key) or ""
        if _is_placeholder(value):
            _config_problem(
                f"{key} 未填（当前 {value!r}）——LLM 三件套不齐，bot 无法推理",
                ".env.example 有各供应商的填写示例；密钥不要提交进 git",
            )

    superusers = (env.get("SUPERUSERS") or os.environ.get("SUPERUSERS") or "").strip()
    if not superusers:
        _config_problem(
            "SUPERUSERS 为空：没有超级用户（fail-closed，管理类技能全不可用）",
            "填**你自己**的 QQ 号（逗号分隔）；模板默认空正是为了防止照抄出陌生管理员",
        )
    elif "*" in superusers:
        _config_problem(
            "SUPERUSERS 含 '*' 通配——该语义已删除（构造期剔除+告警），请填具体 QQ 号"
        )

    dsn = (env.get("DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        _warn("未配 DATABASE_URL：M0 纯内存模式（重启丢历史）；M1+ 建议配 PostgreSQL")
    else:
        host, port = dsn_host_port(dsn)
        try:
            with socket.create_connection((host, port), timeout=3):
                _ok(f"PostgreSQL 可达 {host}:{port}")
        except OSError as exc:
            _config_problem(
                f"PostgreSQL 不可达 {host}:{port}（{exc.__class__.__name__}）",
                "本地起库：python scripts/deploy.py db；或修正 DATABASE_URL",
            )

    if not (env.get("AGENT_SCHEDULER_TZ") or os.environ.get("AGENT_SCHEDULER_TZ")):
        _warn("未设 AGENT_SCHEDULER_TZ：提醒/cron/日键按默认 Asia/Shanghai")
        _hint("非中国部署设为 IANA 名（如 America/New_York）或 local")

    _warn(
        "提醒：data/* 相对路径按启动 CWD 解析——用 systemd/绝对目录部署，勿从临时目录启动"
    )

    print()
    if problems:
        _fail(f"预检未过（{problems} 项必须处理）")
        return 1
    _ok("预检通过（warn 项为建议，不阻断）")
    return 0


# ---------------------------------------------------------------- env


def cmd_env(force: bool = False) -> int:
    """.env.example → .env。已存在则拒绝覆盖——.env 可能含已填好的密钥。"""
    if not ENV_EXAMPLE.is_file():
        _fail(f"缺少模板 {ENV_EXAMPLE}")
        return 1
    if ENV_FILE.is_file() and not force:
        _fail(".env 已存在，拒绝覆盖（里面可能有你填好的密钥）")
        _hint("确要重建：先手动备份 mv .env .env.bak，再重跑；或删掉 .env")
        return 1
    if ENV_FILE.is_file() and force:
        backup = ENV_FILE.with_name(
            f"{ENV_FILE.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        ENV_FILE.rename(backup)
        # L22（REVIEW-1e0f5ae..a0a6bb4）：rename 继承旧文件权限——声称 0600
        # 就要真 0600（旧文件若曾被放宽，备份里躺着全部密钥）
        try:
            os.chmod(backup, 0o600)
        except OSError:
            _warn(f"备份 {backup.name} 收紧 0600 失败（Windows 无 POSIX 权限语义？）")
        _ok(f"旧 .env 已备份为 {backup.name}（0600，含密钥——勿外传）")
    ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)
    _ok(f"已生成 {ENV_FILE.name}（0600）")
    _hint(
        "下一步：编辑 .env 填 LLM_BASE_URL/API_KEY/MODEL、SUPERUSERS（你自己的号）、DATABASE_URL"
    )
    return 0


# ---------------------------------------------------------------- install


def cmd_install(dev: bool = False, force: bool = False) -> int:
    """建 .venv 并安装项目（editable）。幂等：已装则跳过（--force 重装）。"""
    if not VENV_DIR.is_dir():
        _ok("创建 .venv …")
        proc = _run([sys.executable, "-m", "venv", str(VENV_DIR)])
        if proc.returncode != 0:
            _fail(f"venv 创建失败：{proc.stderr.strip()[:300]}")
            return 1
    py = venv_python()
    if not py.is_file():
        _fail(f"venv 里找不到 python：{py}")
        return 1
    extra = ".[dev]" if dev else "."
    probe = _run([str(py), "-m", "pip", "show", "agent-demo"])
    if probe.returncode == 0 and not force:
        _ok(f"agent-demo 已安装（{extra} 跳过；--force 重装）")
        return 0
    _ok(f"pip install -e {extra} …（首次需要一两分钟）")
    proc = _run([str(py), "-m", "pip", "install", "-e", extra], timeout=900)
    if proc.returncode != 0:
        _fail(f"安装失败：{proc.stderr.strip()[-500:]}")
        return 1
    _ok("依赖安装完成")
    return 0


# ---------------------------------------------------------------- db


def cmd_db(down: bool = False) -> int:
    """docker compose 起本地 PG+pgvector；轮询 healthy。--down 只 stop（保留卷）。"""
    if not _have("docker"):
        _fail("没有 docker——无法起本地库；改用外部 DATABASE_URL 或 M0 内存模式")
        return 1
    if not COMPOSE_FILE.is_file():
        _fail(f"缺少 {COMPOSE_FILE}")
        return 1
    if down:
        proc = _run(["docker", "compose", "stop", "db"], cwd=REPO_ROOT, timeout=120)
        _ok(
            "本地 PG 已 stop（数据卷保留）"
            if proc.returncode == 0
            else f"stop 失败：{proc.stderr[:200]}"
        )
        return proc.returncode
    proc = _run(["docker", "compose", "up", "-d", "db"], cwd=REPO_ROOT, timeout=300)
    if proc.returncode != 0:
        _fail(f"compose up 失败：{proc.stderr.strip()[-400:]}")
        return 1
    _ok("compose up -d db 已执行，等待 healthy …")
    user = (
        read_env_file(ENV_FILE).get("POSTGRES_USER") if ENV_FILE.is_file() else None
    ) or "qqagent"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        probe = _run(
            ["docker", "compose", "exec", "-T", "db", "pg_isready", "-U", user],
            cwd=REPO_ROOT,
            timeout=20,
        )
        if probe.returncode == 0:
            _ok(f"PostgreSQL healthy（用户 {user}）")
            _hint(
                "DATABASE_URL=postgresql://<user>:<pass>@127.0.0.1:5432/<db>（凭据见 .env）"
            )
            return 0
        time.sleep(2)
    _fail("90s 内未 healthy——`docker compose logs db` 看原因")
    return 1


# ---------------------------------------------------------------- systemd


def cmd_systemd(
    install: bool, uninstall: bool, user_mode: bool, run_as: str | None
) -> int:
    """生成/安装/卸载 unit。**不 enable/start**——启动必须人工确认 .env 填好之后。"""
    if install and uninstall:
        _fail("--install 与 --uninstall 互斥")
        return 1
    py = venv_python()
    if install and not py.is_file():
        _fail(
            "venv 不存在——先跑 `python scripts/deploy.py install`（unit 的 ExecStart 指向它）"
        )
        return 1
    if not ENV_FILE.is_file():
        _fail(".env 不存在（unit 的 EnvironmentFile 指向它）——先跑 env 子命令")
        return 1
    unit = render_unit(REPO_ROOT, py, run_as=run_as, user_mode=user_mode)

    if user_mode:
        dest = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    else:
        dest = Path("/etc/systemd/system") / UNIT_NAME

    if uninstall:
        if not dest.is_file():
            _fail(f"未安装：{dest} 不存在")
            return 1
        dest.unlink()
        reload_cmd = (
            ["systemctl", "--user", "daemon-reload"]
            if user_mode
            else ["systemctl", "daemon-reload"]
        )
        proc = _run(reload_cmd)
        _ok(
            f"已删除 {dest}"
            + (
                "并 daemon-reload"
                if proc.returncode == 0
                else "（daemon-reload 失败，请手动执行）"
            )
        )
        _hint("如服务正在运行：先 systemctl disable --now agent-demo 再卸载更干净")
        return 0

    if not install:
        # 只生成到 stdout：先给人看，再决定要不要落盘
        print(unit)
        _hint(f"预览模式。落盘：加 --install（写到 {dest}）")
        return 0

    if not user_mode and hasattr(os, "geteuid") and os.geteuid() != 0:
        _fail("system 级安装需要 root")
        _hint(
            "sudo python scripts/deploy.py systemd --install；或用 --user 装到用户级 systemd"
        )
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    # M10（REVIEW-1e0f5ae..a0a6bb4）：fresh clone 没有 data/（运行时才创建）。
    # unit 的 ReadWritePaths 已带 ``-`` 前缀兜底，这里仍显式建好——bot 首启
    # 就要往里写，目录缺席不该等到运行期才暴露。
    (REPO_ROOT / "data").mkdir(exist_ok=True)
    dest.write_text(unit, encoding="utf-8")
    os.chmod(dest, 0o644)
    _ok(f"unit 已写入 {dest}")
    reload_cmd = (
        ["systemctl", "--user", "daemon-reload"]
        if user_mode
        else ["systemctl", "daemon-reload"]
    )
    proc = _run(reload_cmd)
    if proc.returncode != 0:
        _warn(f"daemon-reload 失败：{proc.stderr.strip()[:200]}")
    start_cmd = (
        "systemctl --user enable --now agent-demo"
        if user_mode
        else "sudo systemctl enable --now agent-demo"
    )
    _hint(f"确认 .env 已填好后手动启动：{start_cmd}")
    _hint(" /reboot 与 web 重启都依赖 unit 的 Restart=always——这正是执行链的末端")
    return 0


# ---------------------------------------------------------------- smoke


def cmd_smoke() -> int:
    """import 冒烟 + 配置装配 + 守卫测试。不起 bot、不连协议端。"""
    py = venv_python()
    if not py.is_file():
        _fail("venv 不存在——先跑 install")
        return 1
    failed = 0

    proc = _run([str(py), "-c", "import bot"], cwd=REPO_ROOT, timeout=120)
    if proc.returncode == 0:
        _ok("import bot 成功（nonebot/plugins 全图加载无缺件）")
    else:
        _fail(f"import bot 失败：{proc.stderr.strip()[-500:]}")
        failed += 1

    proc = _run(
        [
            str(py),
            "-c",
            "import yaml; yaml.safe_load(open('config.yaml', encoding='utf-8'))",
        ],
        cwd=REPO_ROOT,
        timeout=30,
    )
    if proc.returncode == 0:
        _ok("config.yaml 可解析")
    else:
        _fail(f"config.yaml 解析失败：{proc.stderr.strip()[-300:]}")
        failed += 1

    probe = _run([str(py), "-c", "import pytest"], timeout=30)
    if probe.returncode == 0:
        proc = _run(
            [str(py), "-m", "pytest", "tests/test_repo_guards.py", "-q"],
            cwd=REPO_ROOT,
            timeout=120,
        )
        if proc.returncode == 0:
            _ok("仓库不变量守卫测试通过")
        else:
            _fail("守卫测试未过——铁律被破坏，先修再部署")
            _hint(proc.stdout.strip()[-300:])
            failed += 1
    else:
        _warn("venv 里没有 pytest，跳过守卫测试（要跑：install --dev）")

    print()
    if failed:
        _fail(f"冒烟未过（{failed} 项）")
        return 1
    _ok("冒烟通过。下一步：填好 .env 后 `python bot.py` 前台试跑，或 systemd --install")
    return 0


# ---------------------------------------------------------------- all


def cmd_all(skip_db: bool, skip_install: bool, dev: bool, force: bool) -> int:
    """一键：env → check(soft) → install → db → smoke。不启动 bot（硬边界 1）。"""
    print("== 1/5 生成 .env ==")
    if ENV_FILE.is_file():
        _ok(".env 已存在，跳过（不覆盖）")
    elif cmd_env() != 0:
        return 1
    print("\n== 2/5 预检（soft：配置类问题只警告，放行门用单独的 check） ==")
    if cmd_check(soft=True) != 0:
        _fail("环境类预检未过（如 Python 版本），中止")
        return 1
    print("\n== 3/5 安装依赖 ==")
    if skip_install:
        _ok("按参数跳过")
    elif cmd_install(dev=dev, force=force) != 0:
        return 1
    print("\n== 4/5 本地数据库 ==")
    if skip_db:
        _ok("按参数跳过")
    elif not _have("docker"):
        _warn("没有 docker，跳过（用外部 DATABASE_URL 或 M0 内存模式）")
    else:
        env = read_env_file(ENV_FILE) if ENV_FILE.is_file() else {}
        dsn = env.get("DATABASE_URL", "")
        host, _port = dsn_host_port(dsn) if dsn else ("127.0.0.1", 5432)
        if dsn and host not in ("127.0.0.1", "localhost", "::1"):
            _warn(f"DATABASE_URL 指向外部库（{host}），不起本地 PG")
        elif cmd_db() != 0:
            _warn("本地 PG 未就绪——可先用 M0 内存模式跑通，或修好后重跑 db 子命令")
    print("\n== 5/5 冒烟 ==")
    if skip_install and not venv_python().is_file():
        _warn("跳过了安装且没有 venv，冒烟无法进行")
        return 0
    if cmd_smoke() != 0:
        return 1
    print(
        "\n部署准备完成。**刻意不自动启动**——请先编辑 .env：\n"
        "  1. LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（必填，模板里的 ... 是占位符）\n"
        "  2. SUPERUSERS = 你自己的 QQ 号（否则没有管理员）\n"
        "  3. ONEBOT_ONEV11_ACCESS_TOKEN 与协议端一致\n"
        "然后：python bot.py（前台试跑）或 python scripts/deploy.py systemd --install（常驻）"
    )
    return 0


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="agent-demo 一键部署（预检/配置/依赖/数据库/冒烟/systemd unit）",
        epilog="硬边界：不自动启动 bot；不写仓库外文件（systemd --install 除外，需显式 flag）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="只读预检")

    p_env = sub.add_parser("env", help=".env.example → .env")
    p_env.add_argument(
        "--force", action="store_true", help="覆盖已存在的 .env（先备份）"
    )

    p_install = sub.add_parser("install", help="建 venv 并安装项目")
    p_install.add_argument(
        "--dev", action="store_true", help="连带装 pytest/ruff（开发用）"
    )
    p_install.add_argument("--force", action="store_true", help="已装也重装")

    p_db = sub.add_parser("db", help="docker compose 起本地 PG")
    p_db.add_argument("--down", action="store_true", help="停止（保留数据卷）")

    p_systemd = sub.add_parser("systemd", help="生成/安装/卸载 systemd unit")
    p_systemd.add_argument(
        "--install", action="store_true", help="落盘安装（不 enable/start）"
    )
    p_systemd.add_argument("--uninstall", action="store_true", help="删除 unit 文件")
    p_systemd.add_argument(
        "--user", action="store_true", help="用户级 systemd（~/.config/systemd/user）"
    )
    p_systemd.add_argument(
        "--run-as", metavar="NAME", help="以指定用户运行（system 级生效）"
    )

    sub.add_parser("smoke", help="import + 配置 + 守卫测试冒烟")

    p_all = sub.add_parser("all", help="一键：check→env→install→db→smoke")
    p_all.add_argument("--skip-db", action="store_true")
    p_all.add_argument("--skip-install", action="store_true")
    p_all.add_argument("--dev", action="store_true")
    p_all.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "env":
        return cmd_env(force=args.force)
    if args.cmd == "install":
        return cmd_install(dev=args.dev, force=args.force)
    if args.cmd == "db":
        return cmd_db(down=args.down)
    if args.cmd == "systemd":
        return cmd_systemd(
            install=args.install,
            uninstall=args.uninstall,
            user_mode=args.user,
            run_as=args.run_as,
        )
    if args.cmd == "smoke":
        return cmd_smoke()
    if args.cmd == "all":
        return cmd_all(
            skip_db=args.skip_db,
            skip_install=args.skip_install,
            dev=args.dev,
            force=args.force,
        )
    return 2  # pragma: no cover - argparse required=True 已拦兜底


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
