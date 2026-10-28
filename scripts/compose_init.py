"""compose 项目目录一键引导：生成 ``.env`` / ``config.yaml`` / ``data/`` 三件套。

存在的理由：NAS 上部署要把 compose、``config.yaml``、``.env.example`` 三个文件
凑齐、改名、再逐项填密钥，还要先 ``mkdir`` 14 个运行期目录——漏一步不报错，而是
静默变坑（``config.yaml`` 被 Docker bind mount 建成**空目录**、``data/state``
没挂载导致 recreate 时 provider/key 与 2FA secret 全丢）。本脚本把「凑文件」这步
收成一条命令，部署者只剩填密钥这一件非做不可的事。

用法（镜像的 ENTRYPOINT 已是 docker-entrypoint.sh，传 init 子命令即可）::

    docker run --rm -v "$PWD:/out" ghcr.io/<owner>/agent-demo:latest init /out

边界（刻意收窄，别放宽）：

* **绝不覆盖已存在的 ``.env`` / ``config.yaml`` 文件**——那里面是部署者填好的密钥，
  覆盖等于毁配置；已存在就原样保留并在报告里说 kept。
* Docker 的 bind mount 指向不存在的文件时会创建一个**同名空目录**，本脚本把这种
  目录态当成「不存在」换回文件（``rmdir`` 只移空目录，非空即报错退出）。
* ``POSTGRES_PASSWORD`` 随机生成且只用字母数字：compose 的 ``${VAR}`` 插值会把
  单个 ``$`` 后当未定义变量静默替换为空，而 compose 拼的 DATABASE_URL 不做 URL
  编码，含 ``@`` 会直接被解析成 userinfo 分隔符。
* 纯标准库：``venv`` 建成前就要能引导自身（照 scripts/deploy.py 的先例）。
"""

from __future__ import annotations

import argparse
import re
import secrets
import shutil
import string
import sys
from pathlib import Path

# 脚本在仓库里是 <root>/scripts/compose_init.py，在镜像里是 /app/scripts/compose_init.py
APP_ROOT = Path(__file__).resolve().parent.parent
ENV_TEMPLATE = APP_ROOT / ".env.example"
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

# .env.example 里「容器部署」注释块的 7 个单文件状态重定向键 → 容器内的目标值。
# 不重定向时这些文件落在 data/ 根，而 compose 只挂了 data/state/——recreate
# （down/up、升级镜像）即静默丢失（web 配过的 provider/key、2FA secret、nudge
# 目标与热点记录）。
#
# **这 7 个键在 .env.example 里每一个都至少出现两次**（容器部署块的注释行 + 功能
# 文档里的活跃行，AGENT_PUSH_STATE_FILE 甚至三处）。python-dotenv 是「后者覆盖
# 前者」，所以只把注释行取消注释会留下靠后的空值/旧值行并把重定向**静默抵消**——
# 必须连活跃行一起去重，每个键只留一行。
STATE_REDIRECTS = {
    "AGENT_LLM_PROVIDERS_FILE": "data/state/llm-providers.json",
    "AGENT_SEARCH_PROVIDERS_FILE": "data/state/search-providers.json",
    "AGENT_2FA_SECRET_FILE": "data/state/2fa-secret.json",
    "AGENT_NUDGE_CONFIG": "data/state/nudge.json",
    "AGENT_NUDGE_HOTSPOT_FILE": "data/state/nudge-hotspot.json",
    "AGENT_REBOOT_NOTICE_FILE": "data/state/reboot-notice.json",
    "AGENT_PUSH_STATE_FILE": "data/state/push-state.json",
}
STATE_REDIRECT_KEYS = tuple(STATE_REDIRECTS)

# 部署者必须自己填的键（换机器就不一样，无法烤进公开镜像）。值为空或模板占位
# 「...」时算未填。
REQUIRED_KEYS = (
    "SUPERUSERS",
    "ALLOWED_GROUPS",
    "LLM_API_KEY",
    "ONEBOT_ONEV11_ACCESS_TOKEN",
    "NAPCAT_HTTP_URL",
)
_UNFILLED = {"", "..."}

_PASSWORD_LENGTH = 24
_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def generate_password(length: int = _PASSWORD_LENGTH) -> str:
    """随机数据库密码：只用字母数字，避开 compose 插值与 URL 裸拼的坑。"""
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def _key_of(line: str) -> str | None:
    """行定义的 env 键；注释行剥掉 ``#`` 也算（容器部署块本来就是注释态）。

    只认 ``全大写下划线`` 形态，免得把 `# 密码请用纯字母数字` 这类说明文字当成键。
    """
    body = line.strip()
    if body.startswith("#"):
        body = body[1:].strip()
    if "=" not in body:
        return None
    key = body.split("=", 1)[0].strip()
    return key if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) else None


def render_env(template_text: str, password: str) -> str:
    """把 ``.env.example`` 模板改写成容器部署可直接用的 ``.env``。

    只做三件事，其余行原样保留：换数据库密码、把 7 个状态文件重定向到
    ``data/state/``、给备份镜像目录一个容器内默认值。

    重定向那 7 个键是**去重**而不是「取消注释」：模板里每个键都至少出现两次
    （容器部署块的注释行 + 功能文档里的活跃行），python-dotenv 后者覆盖前者，
    只取消注释会被靠后的空值行静默抵消。这里保留首次出现的那行并改写成目标值，
    丢掉其余全部出现，保证每个键在产物里只有一行。

    幂等——对已改写过的文本再跑一遍结果不变。
    """
    out_lines: list[str] = []
    seen: set[str] = set()
    for line in template_text.splitlines():
        key = _key_of(line)
        if key in STATE_REDIRECTS:
            if key in seen:
                continue  # 重复定义：dotenv 后者赢，必须去掉
            seen.add(key)
            out_lines.append(f"{key}={STATE_REDIRECTS[key]}")
            continue
        active = not line.strip().startswith("#")
        if active and key == "POSTGRES_PASSWORD":
            out_lines.append(f"POSTGRES_PASSWORD={password}")
            continue
        if active and key == "AGENT_BACKUP_MIRROR_DIR":
            out_lines.append("AGENT_BACKUP_MIRROR_DIR=data/mirror")
            continue
        out_lines.append(line)
    # 模板漂移兜底：哪个键在模板里根本没出现，补在末尾（后者覆盖，位置无关）
    for key, value in STATE_REDIRECTS.items():
        if key not in seen:
            out_lines.append(f"{key}={value}")
    return "\n".join(out_lines) + "\n"


def missing_keys(env_text: str) -> list[str]:
    """``.env`` 里还是空值/占位的必填键，按 REQUIRED_KEYS 顺序返回。"""
    values: dict[str, str] = {}
    for line in env_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return [k for k in REQUIRED_KEYS if values.get(k, "").strip() in _UNFILLED]


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


def init_project(
    out_dir: Path,
    *,
    template: Path | None = None,
    source_config: Path | None = None,
) -> dict[str, str]:
    """在 ``out_dir`` 生成 compose 项目三件套，返回每项的 created/kept 报告。"""
    template = template or ENV_TEMPLATE
    source_config = source_config or CONFIG_SOURCE
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, str] = {}

    # --- config.yaml：缺失或误建目录才写，已存在一律不动 ---
    config_target = out_dir / "config.yaml"
    if config_target.is_file():
        report["config"] = "kept"
    else:
        _clear_directory_trap(config_target)
        if not source_config.is_file():
            raise SystemExit(f"镜像里没有 {source_config}，无法生成 config.yaml")
        shutil.copyfile(source_config, config_target)
        report["config"] = "created"

    # --- data/ 运行期子目录：幂等 mkdir ---
    for name in DATA_DIRS:
        (out_dir / "data" / name).mkdir(parents=True, exist_ok=True)
    report["data"] = "created"

    # --- .env：已有文件绝不覆盖 ---
    env_target = out_dir / ".env"
    if env_target.is_file():
        report["env"] = "kept"
    else:
        _clear_directory_trap(env_target)
        if not template.is_file():
            raise SystemExit(f"镜像里没有 {template}，无法生成 .env")
        env_target.write_text(
            render_env(template.read_text(encoding="utf-8"), generate_password()),
            encoding="utf-8",
        )
        report["env"] = "created"

    return report


def _print_next_steps(out_dir: Path, report: dict[str, str]) -> None:
    print(f"项目目录已就绪：{out_dir}")
    for what, how in (("config.yaml", report["config"]), (".env", report["env"])):
        verb = "已生成" if how == "created" else "已存在，保留原样"
        print(f"  {what}: {verb}")
    print(f"  data/: {len(DATA_DIRS)} 个运行期子目录")

    env_text = (out_dir / ".env").read_text(encoding="utf-8")
    todo = missing_keys(env_text)
    if todo:
        print("\n还要填这几项（.env，换机器就不一样，无法烤进公开镜像）：")
        for key in todo:
            print(f"  {key}")
        print("\n填完再启动：")
    else:
        print("\n必填项都已填写，可以启动：")
    print("  docker compose up -d db bot")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compose_init",
        description="生成 compose 项目目录（.env/config.yaml/data/）",
    )
    parser.add_argument(
        "out_dir",
        nargs="?",
        default=".",
        help='项目目录（docker run -v "$PWD:<这里>" 挂进来的那个路径）',
    )
    args = parser.parse_args(argv)
    report = init_project(Path(args.out_dir))
    _print_next_steps(Path(args.out_dir), report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
