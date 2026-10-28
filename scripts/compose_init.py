"""compose 项目目录一键引导：生成 ``config/agent.json`` / ``config.yaml`` / ``data/``。

存在的理由：NAS 上部署要把 compose、``config.yaml``、``.env.example`` 三个文件
凑齐、改名、再逐项填密钥，还要先 ``mkdir`` 14 个运行期目录——漏一步不报错，而是
静默变坑。本脚本把「凑文件」这步收成一条命令，部署者只剩填密钥这一件非做不可的事。

用法（镜像的 ENTRYPOINT 已是 docker-entrypoint.sh，传 init 子命令即可）::

    docker run --rm -v "$PWD:/out" ghcr.io/<owner>/agent-demo:latest init /out

**配置走 JSON 而不是 ``.env``**（2026-10-28 重构）：dotfile 在部分 NAS 上建不住
（文件管理器反复把它建成目录，而 Docker 的**单文件** bind mount 在源路径不存在时
会自动补一个目录，再把目录绑到镜像里的文件上，报一句指不到病因的
``not a directory``）。改成整个 ``config/`` **目录**挂载、里面放 ``agent.json``
之后，dotfile 与单文件挂载两类坑同时消失——目录挂载在源不存在时自动建目录，
那正是它想要的行为。

边界（刻意收窄，别放宽）：

* **绝不覆盖已存在的 ``config/agent.json`` / ``config.yaml``**——那里面是部署者
  填好的密钥，覆盖等于毁配置；已存在就原样保留并在报告里说 kept。
* Docker 的 bind mount 指向不存在的文件时会创建一个**同名空目录**，本脚本把这种
  目录态当成「不存在」换回文件（``rmdir`` 只移空目录，非空即报错退出）。
* 纯标准库：``venv`` 建成前就要能引导自身（照 scripts/deploy.py 的先例）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

# 脚本在仓库里是 <root>/scripts/compose_init.py，在镜像里是 /app/scripts/compose_init.py
APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_SOURCE = APP_ROOT / "config.yaml"

# compose 挂载的运行期子目录（与 docs/deploy-nas/docker-compose.yml 的挂载表一致）
DATA_DIRS = (
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
    "state",
    "mirror",
    "ssh",
)

# data/ 根下的单文件状态。默认按 CWD 落在 data/ 根，而 compose 只挂了 data/state/
# ——recreate（down/up、升级镜像）即静默丢失（web 配过的 provider/key、2FA secret、
# nudge 目标与热点记录），所以生成时一并重定向进去。
STATE_REDIRECTS = {
    "AGENT_LLM_PROVIDERS_FILE": "data/state/llm-providers.json",
    "AGENT_SEARCH_PROVIDERS_FILE": "data/state/search-providers.json",
    "AGENT_2FA_SECRET_FILE": "data/state/2fa-secret.json",
    "AGENT_NUDGE_CONFIG": "data/state/nudge.json",
    "AGENT_NUDGE_HOTSPOT_FILE": "data/state/nudge-hotspot.json",
    "AGENT_REBOOT_NOTICE_FILE": "data/state/reboot-notice.json",
    "AGENT_PUSH_STATE_FILE": "data/state/push-state.json",
}

# 部署者必须自己填的键（换机器就不一样，无法烤进公开镜像）。值为空或占位
# 「换成你的...」时算未填。
REQUIRED_KEYS = (
    "LLM_API_KEY",
    "SUPERUSERS",
    "ALLOWED_GROUPS",
    "ONEBOT_ONEV11_ACCESS_TOKEN",
    "NAPCAT_HTTP_URL",
)

# 生成给新手的极简配置。只放「不填就不能用」和「填错就静默失效」的键，其余全部
# 有默认值——要加功能时按需往这里加，键名与 .env.example 完全一致。
# `_` 开头的键是备注，agentcore/config_json.py 会忽略。
#
# 占位符统一以「换成」开头：compose_init.missing_keys 与 config_json.missing_required
# 都按这个前缀判定「还没填」，两边口径必须一致，否则会出现「看着填了其实没有」。
NAPCAT_HTTP_URL_EXAMPLE = "http://192.168.1.100:3000"
PLACEHOLDER_PREFIX = "换成"
# agent.env 的首行说明。dotenv 行格式，`#` 起头是注释；这个文件由 web 设置页 /
# QQ /model 命令写入，键值会覆盖 agent.json 里的同名项（但不盖真实环境变量）。
WEB_ENV_HEADER = (
    "# web 受控写面落点：设置页与 QQ /model 改的键写在这里，\n"
    "# 重启后覆盖 agent.json 里的同名项。不要手删——删了设置页会报错。\n"
)
# 镜像内烤好的 config.yaml 缺失时的兜底（正常不会发生：Dockerfile 有 COPY）。
# 只放能让引擎启动的最小结构；真配置由部署者随后编辑 config/config.yaml。
FALLBACK_CONFIG_YAML = "agent:\n  max_iterations: 8\n"

MINIMAL_CONFIG: dict[str, object] = {
    "_说明": "agent-demo 配置。以 _ 开头的键只是备注，程序会忽略。"
    "改完执行 docker compose restart bot 生效。"
    "完整可配项（键名与此处一致）见仓库 .env.example。",
    "_说明_监听": "容器部署必须 0.0.0.0。填 127.0.0.1 的话协议端连不上，"
    "现象是 ECONNREFUSED。PORT 在容器里不生效——compose 的 environment 会用"
    "AGENT_BOT_PORT 显式注入，改端口改那个 compose 层变量（映射两侧同时变）；"
    "这里的 PORT 只在本机 python bot.py 时起作用。",
    "HOST": "0.0.0.0",
    "PORT": "8080",
    "_说明_权限": "SUPERUSERS=管理员 QQ 号；ALLOWED_GROUPS=允许响应的群，多个用逗号。"
    "留空 = 群里不响应 / 没有超管（fail-closed）。",
    "SUPERUSERS": "换成你的QQ号",
    "ALLOWED_GROUPS": "换成群号,另一个群号",
    "_说明_LLM": "OpenAI 兼容接口，不绑死供应商。key 只进这个文件，不进日志/截图/提交。",
    "LLM_BASE_URL": "https://api.stepfun.com/v1",
    "LLM_API_KEY": "换成你的key",
    "LLM_MODEL": "step-1-flash",
    "_说明_协议端": "ONEBOT_ONEV11_ACCESS_TOKEN 要与协议端「反向 WebSocket 客户端」里"
    "填的 Token 一致。NAPCAT_HTTP_URL 是协议端的 HTTP API 地址——NapCat 默认 3000，"
    "**不是** WebUI 的 6099，且必须带 http:// 前缀。",
    "ONEBOT_ONEV11_ACCESS_TOKEN": "换成一个长随机串",
    "NAPCAT_HTTP_URL": f"换成协议端的HTTP地址，如 {NAPCAT_HTTP_URL_EXAMPLE}",
    "_说明_状态文件": "下面 7 个把易失状态重定向进 data/state/（compose 挂了这个目录）。"
    "不配的话 recreate 时 web 配过的 provider/key、2FA secret、nudge 记录会全丢。",
    **STATE_REDIRECTS,
}


def render_json() -> str:
    """渲染极简配置。缩进 2 + ensure_ascii=False，中文备注可直接读。"""
    return json.dumps(MINIMAL_CONFIG, ensure_ascii=False, indent=2) + "\n"


def missing_keys(config: dict) -> list[str]:
    """配置里还是空值/占位的必填键，按 REQUIRED_KEYS 顺序返回。

    占位符统一以 :data:`PLACEHOLDER_PREFIX`（「换成」）开头，
    空字符串同样算没填。**这个口径必须与 ``config_json`` 一致**，否则会出现
    「看着填了其实没有」——bot 起得来但不能对话。
    """
    out: list[str] = []
    for key in REQUIRED_KEYS:
        value = str(config.get(key, "")).strip()
        if not value or value.startswith(PLACEHOLDER_PREFIX):
            out.append(key)
    return out


def _clear_directory_trap(path: Path) -> None:
    """Docker bind mount 误建的同名**空**目录：移掉，好写文件进去。

    非空就报错退出——那更可能是部署者自己的目录，不能替它决定删什么。
    """
    if not path.is_dir():
        return
    try:
        path.rmdir()
    except OSError as exc:
        raise SystemExit(
            f"{path} 是个非空目录，不是文件。请先确认里面的内容再手动处理，"
            f"本脚本不替你删数据。"
        ) from exc


def _ensure_file(target: Path, write: Callable[[Path], None]) -> str:
    """目标文件不存在才写；已存在一律 ``kept``。目录态先换回文件。

    「已存在就不动」是硬契约：那里面可能是部署者填好的密钥，覆盖等于毁配置。
    """
    if target.is_file():
        return "kept"
    _clear_directory_trap(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    write(target)
    return "created"


def init_project(
    out_dir: Path,
    *,
    source_config: Path | None = None,
) -> dict[str, str]:
    """在 ``out_dir`` 生成 compose 项目三件套，返回每项的 created/kept 报告。"""
    source_config = source_config or CONFIG_SOURCE
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, str] = {}

    # --- config.yaml：缺失或误建目录才写，已存在一律不动 ---
    report["config"] = _ensure_file(
        out_dir / "config.yaml",
        lambda p: _copy_config_yaml(p, source_config),
    )

    # --- data/ 运行期子目录：幂等 mkdir ---
    for name in DATA_DIRS:
        (out_dir / "data" / name).mkdir(parents=True, exist_ok=True)
    report["data"] = "created"

    # --- config/agent.json：已有文件绝不覆盖 ---
    report["json"] = _ensure_file(
        out_dir / "config" / "agent.json",
        lambda p: p.write_text(render_json(), encoding="utf-8"),
    )

    # --- config/agent.env：web 受控写面的落点，必须**先存在** ---
    # config_write.write_env_key 对不存在的文件是 fail-closed 的
    # EnvWriteError("env_file_missing")——没人先建它，设置页就整体 500，而原因
    # 只是「缺个空文件」（P1-2 在 .env 上踩过同款）。里面不放任何键：它只在
    # 管理员用过 web 写面 / QQ /model 之后才有内容。
    report["web_env"] = _ensure_file(
        out_dir / "config" / "agent.env",
        lambda p: p.write_text(WEB_ENV_HEADER, encoding="utf-8"),
    )

    return report


def _copy_config_yaml(target: Path, source: Path) -> None:
    if not source.is_file():
        raise SystemExit(f"镜像里没有 {source}，无法生成 config.yaml")
    shutil.copyfile(source, target)


def _write_config_yaml(target: Path, source: Path) -> None:
    """自举用的 config.yaml 落地：有模板就复制，没有就写最小兜底并喊一声。

    刻意不像 :func:`_copy_config_yaml` 那样直接失败——自举跑在容器启动路径上，
    为一份配置文件让整个容器起不来比「先用最小配置起来、日志里说明」更糟。宿主侧
    真正的配置由部署者随后编辑 ``config/config.yaml``。
    """
    if source.is_file():
        shutil.copyfile(source, target)
        return
    print(
        f"WARNING: 模板 {source} 不存在，已写入最小兜底配置；"
        "请编辑 config/config.yaml 补全",
        file=sys.stderr,
    )
    target.write_text(FALLBACK_CONFIG_YAML, encoding="utf-8")


def bootstrap(app_root: Path, *, source_config: Path | None = None) -> dict[str, str]:
    """容器首次启动自举：把配置补进挂载目录，**缺什么建什么，绝不覆盖已有文件**。

    与 :func:`init_project` 的差别只在**落地位置与触发方式**：``init`` 是部署者
    手动跑一次性命令、生成在项目目录；``bootstrap`` 由 ``docker-entrypoint.sh``
    在每次启动时以 root 跑，生成在容器内的 ``/app``——宿主侧就是 compose 挂进来
    的 ``./config`` 与 ``./data``。于是「空目录 + 一条 compose up」就能起来，
    不必先找模板改名、先 mkdir 14 个目录、先跑 init。

    ``config.yaml`` 落在 ``config/`` 里（而不是 ``/app/config.yaml``）：compose
    因此不需要任何**单文件** bind mount——那正是 NAS 上反复踩的坑（源路径不存在时
    Docker 自动补一个同名目录，再把目录绑到镜像内的文件上，报一句指不到病因的
    ``not a directory``）。引擎经 ``AGENT_CONFIG=/app/config/config.yaml`` 读它
    （``config_yaml.config_path`` 与 engine 同源）。

    返回 created/kept 报告；已存在的项一律 ``kept``，部署者填的密钥不会被重置。
    """
    root = Path(app_root)
    source = Path(source_config) if source_config is not None else root / "config.yaml"

    report: dict[str, str] = {}
    report["config_yaml"] = _ensure_file(
        root / "config" / "config.yaml",
        lambda p: _write_config_yaml(p, source),
    )
    report["json"] = _ensure_file(
        root / "config" / "agent.json",
        lambda p: p.write_text(render_json(), encoding="utf-8"),
    )
    report["web_env"] = _ensure_file(
        root / "config" / "agent.env",
        lambda p: p.write_text(WEB_ENV_HEADER, encoding="utf-8"),
    )

    missing_dirs = [name for name in DATA_DIRS if not (root / "data" / name).is_dir()]
    for name in DATA_DIRS:
        (root / "data" / name).mkdir(parents=True, exist_ok=True)
    report["data"] = "created" if missing_dirs else "kept"
    return report


def _print_next_steps(out_dir: Path, report: dict[str, str]) -> None:
    print(f"项目目录已就绪：{out_dir}")
    for what, how in (
        ("config.yaml", report["config"]),
        ("config/agent.json", report["json"]),
        ("config/agent.env", report["web_env"]),
    ):
        verb = "已生成" if how == "created" else "已存在，保留原样"
        print(f"  {what}: {verb}")
    print(f"  data/: {len(DATA_DIRS)} 个运行期子目录")

    json_path = out_dir / "config" / "agent.json"
    try:
        current = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = {}
    todo = missing_keys(current) if isinstance(current, dict) else []
    if todo:
        print("\n还要填这几项（config/agent.json，换机器就不一样，无法烤进公开镜像）：")
        for key in todo:
            print(f"  {key}")
        print("\n填完再启动：")
    else:
        print("\n必填项都已填写，可以启动：")
    print("  docker compose up -d db bot")


def _main_bootstrap(root: Path) -> int:
    """自举模式的 CLI 入口：补建缺的配置，并把「还要填什么」讲清楚。

    打印必须短——它跑在每次容器启动的路径上。没缺东西时只打一行，不刷屏。
    """
    report = bootstrap(root)
    created = [what for what, how in report.items() if how == "created"]
    if created:
        print(f"首次启动自举：已生成 {', '.join(created)}（已存在的一律未动）")
    else:
        print("首次启动自举：配置已存在，无需生成")

    try:
        current = json.loads(
            (root / "config" / "agent.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        current = {}
    todo = missing_keys(current) if isinstance(current, dict) else []
    if todo:
        print(
            "\n还要填这几项（config/agent.json；改完 docker compose restart bot 生效）："
        )
        for key in todo:
            print(f"  {key}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compose_init",
        description="生成 compose 项目目录（agent.json/config.yaml/data/）",
    )
    parser.add_argument(
        "out_dir",
        nargs="?",
        default=".",
        help='项目目录（docker run -v "$PWD:<这里>" 挂进来的那个路径）',
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help=(
            "容器自举模式：在 out_dir（容器里是 /app）下补建 "
            "config/config.yaml、config/agent.json、config/agent.env、data/ 子目录；"
            "缺什么建什么，已存在的一律不动。由 docker-entrypoint.sh 每次启动时调用。"
        ),
    )
    args = parser.parse_args(argv)
    root = Path(args.out_dir)
    if args.bootstrap:
        return _main_bootstrap(root)
    report = init_project(root)
    _print_next_steps(root, report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
