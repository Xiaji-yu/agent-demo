"""JSON 配置：把 ``config/agent.json`` 的键值写进 ``os.environ``。

为什么不是继续用 ``.env``：dotfile 在部分 NAS 上建不住——文件管理器反复把它建成
目录，而 Docker 的**单文件** bind mount 在源路径不存在时会自动补一个目录，再把
目录绑到镜像里的文件上，报一句指不到病因的 ``not a directory``（2026-10-28 在
绿联 DX4600 上连踩两次）。改成「整个 ``config/`` 目录挂进容器 + 里面放
``agent.json``」之后，dotfile 与单文件挂载两类坑同时消失：目录挂载在源不存在时
自动建目录，那正是它想要的行为。

契约（下游无感的关键）：

* **键名与 ``.env`` 完全一致**，所以 NoneBot 的 ``Config`` 与 agentcore 里上百个
  ``os.getenv`` 一行都不用改；
* 值统一转成字符串——env 只能是字符串，JSON 的数字/布尔/数组由 :func:`to_env_value`
  转（布尔转 ``true``/``false``，因为 :mod:`agentcore.boolenv` 只认这两个形态，
  Python 的 ``True`` 会被当「认不出」告警后取默认）；
* ``_`` 开头的键是**备注**，忽略——JSON 没有注释语法，用这个约定让它能自解释；
* **不覆盖已存在的 environ**：真实环境变量（systemd EnvironmentFile / shell 导出）
  优先级最高，与 ``load_dotenv(override=False)`` 同模型；
* 文件缺失 / JSON 坏 / 顶层不是对象，都**不抛异常**——启动不能因为配置读不了就崩，
  但必须响：返回的 :class:`LoadReport` 带 ``error`` 与 ``missing_required``，
  调用方负责把它喊出来（静默比响故障难查，2026-10-12 事故的教训）。

调用顺序由 :func:`agentcore.env_snapshot.prime` 的 ``before_dotenv`` 钩子保证：
先抓出生快照（JSON 派生值**不进**快照，否则 ``/reboot`` 的 re-exec 会把旧值带给
新进程、改 JSON 后重启不生效），再写 JSON，最后 ``load_dotenv`` 盖不上已写的键
——于是优先级是 真实环境变量 > JSON > ``.env``。

web 受控写面（:func:`apply_env_file`）在 ``load_dotenv`` **之后**应用，落在
:func:`web_env_path` 指向的那个文件（默认 ``config/agent.env``）：管理员在设置页
改过的键要赢过 JSON 基础配置，但仍盖不掉真实环境变量（用出生快照区分）。完整优先级
是 真实环境变量 > web 写面文件 > JSON > ``.env``。
"""

from __future__ import annotations

import json
import os
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

# 环境变量名本身也走 env 覆盖（测试与例外部署用）
PATH_ENV = "AGENT_CONFIG_JSON"
# 默认相对 CWD——容器里 WORKDIR 是 /app，即 /app/config/agent.json
DEFAULT_PATH = "config/agent.json"

# web 受控写面（plugins/qq_agent_adapter/config_write.py）的落点。**单一事实源**：
# bot.py 读它、config_write 写它，两边都必须问这个函数——此前两边各有一份默认值
# （config/agent.env 与 .env），容器里靠 compose 显式设 AGENT_ENV_FILE 才没暴露，
# 而落点不一致的故障形态是「设置页显示成功、重启后不生效」，排查成本极高。
WEB_ENV_FILE_ENV = "AGENT_ENV_FILE"
DEFAULT_WEB_ENV_FILE = "config/agent.env"

# 缺了就不能用的键。值为空或模板占位 "..." 都算没配。
REQUIRED_KEYS = (
    "LLM_API_KEY",
    "SUPERUSERS",
    "ALLOWED_GROUPS",
    "ONEBOT_ONEV11_ACCESS_TOKEN",
    "NAPCAT_HTTP_URL",
)
# 「还没填」的两种形态：空值 / 模板占位。`...` 是 .env.example 的占位约定，
# `换成` 是 scripts/compose_init.py 生成 JSON 时的占位前缀（"换成你的key" 这类自解释
# 占位）。两边口径必须一致，否则会出现「看着填了其实没有」——bot 起得来但不能对话。
_UNFILLED_EXACT = {"", "..."}
_UNFILLED_PREFIXES = ("换成",)


def _is_unfilled(value: str) -> bool:
    text = (value or "").strip()
    return text in _UNFILLED_EXACT or text.startswith(_UNFILLED_PREFIXES)


def to_env_value(value: object) -> str | None:
    """JSON 值 → env 字符串；``None`` 返回 None 表示「不设置这个键」。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    # bool 是 int 的子类，必须放在前面：json.dumps(True) == "true"（env_flag 认得），
    # 而 str(True) == "True"（不认）
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class LoadReport:
    """一次加载的结果。``loaded``/``skipped_existing`` 按文件里的键序。"""

    source: Path | None = None
    loaded: tuple[str, ...] = ()
    skipped_existing: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()
    error: str | None = None
    notes: tuple[str, ...] = field(default=())


def _resolve_path(explicit: Path | None, environ) -> Path:
    if explicit is not None:
        return Path(explicit)
    raw = (environ.get(PATH_ENV) or "").strip()
    return Path(raw) if raw else Path(DEFAULT_PATH)


def _read_json(path: Path) -> tuple[dict, str | None]:
    """读并校验。返回 (数据, 错误信息)；读不了时数据为 {}。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, f"读不到 {path}: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"{path} 不是合法 JSON: {exc}"
    if not isinstance(data, dict):
        return {}, f"{path} 顶层必须是对象（{{...}}），实际是 {type(data).__name__}"
    return data, None


def load(
    path: Path | None = None, *, environ: MutableMapping[str, str] | None = None
) -> LoadReport:
    """把 JSON 配置写进 ``environ``（默认 ``os.environ``）。永不抛异常。"""
    env = os.environ if environ is None else environ
    target = _resolve_path(path, env)

    data, error = _read_json(target)
    if error is not None:
        return LoadReport(
            source=target if target.exists() else None,
            error=error,
            missing_required=missing_required(env),
        )
    if not target.is_file():
        # 文件不存在：不是错误，按「全部未配」报告
        return LoadReport(source=None, missing_required=missing_required(env))

    loaded: list[str] = []
    skipped: list[str] = []
    notes: list[str] = []
    for key, value in data.items():
        if key.startswith("_"):
            notes.append(key)
            continue
        text = to_env_value(value)
        if text is None:
            continue
        if key in env:
            skipped.append(key)
            continue
        env[key] = text
        loaded.append(key)

    return LoadReport(
        source=target,
        loaded=tuple(loaded),
        skipped_existing=tuple(skipped),
        missing_required=missing_required(env),
        notes=tuple(notes),
    )


def missing_required(
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """environ 里还是空值/占位的必填键，按 REQUIRED_KEYS 顺序。"""
    env = os.environ if environ is None else environ
    return tuple(key for key in REQUIRED_KEYS if _is_unfilled(env.get(key, "")))


def apply_env_file(path: Path | None) -> tuple[str, ...]:
    """加载 web 受控写面落地的 env 文件，返回实际覆盖的键。

    ``plugins/qq_agent_adapter/config_write.py`` 的契约是「写 bot.py 会读的那个
    文件」。JSON 化之后那个文件是 ``config/agent.env``（与 agent.json 同在挂载的
    config/ 目录里，普通文件名），由本函数在 :func:`load` 之后应用。

    优先级因此是 **真实环境变量 > web 写面文件 > JSON > ``.env``**：web 的显式
    修改要赢过 JSON 基础配置，但**不能**盖掉真实环境变量（systemd
    EnvironmentFile / shell 导出）——后者用出生快照识别，见
    :mod:`agentcore.env_snapshot`。
    """
    if path is None:
        return ()
    target = Path(path)
    if not target.is_file():
        return ()
    from agentcore.env_snapshot import pristine_environ

    protected = set(pristine_environ() or ())
    applied: list[str] = []
    for key, value in dotenv_values(target).items():
        if value is None or key in protected:
            continue
        os.environ[key] = value
        applied.append(key)
    return tuple(applied)


def web_env_path() -> Path:
    """web 受控写面的落点：``AGENT_ENV_FILE`` 可覆盖，默认 ``config/agent.env``。

    与 :func:`apply_env_file` 的入参、``config_write.env_file_path()`` 同源。
    """
    raw = (os.environ.get(WEB_ENV_FILE_ENV) or "").strip()
    return Path(raw) if raw else Path(DEFAULT_WEB_ENV_FILE)
