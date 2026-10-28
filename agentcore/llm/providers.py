"""多 LLM Provider 配置：存在 ``config/agent.json`` 里（2026-10-28 需求③）。

为什么从独立 JSON 文件并回主配置：用户明确要求「删掉 llm-providers.json，
直接复用 agent.json 的配置」——一个配置源比两个好，且冷备必含清单里本来就只有
agent.json，并回去之后备份链路少一个要同步的点。

**存储形态**：agent.json 里两个键（普通 JSON 值，不是 env 字符串）：

- ``LLM_PROVIDERS``：provider 数组，元素字段与 :class:`Provider` 一致
  （``id``/``name``/``protocol``/``base_url``/``model``/``api_key``/``enabled``/
  ``temperature``/``max_tokens``）
- ``LLM_ACTIVE``：活跃位 id

**优先级**：``LLM_PROVIDERS`` 数组存在 → 用它；否则回落 env 单 provider
（``LLM_BASE_URL``/``LLM_API_KEY``/``LLM_MODEL``/``LLM_TEMPERATURE``），
兼容只有旧键的 agent.json。

**为什么不做进 ``config_write`` 的 ENV_SPECS**：那个体系的 ``KeySpec.kind`` 只有
``bool|int|float|csv|str``，没有数组类型；而模型标签本来就有自己独立的
``/api/llm/providers`` 端点（两段确认 + 审计 + 改前备份），直接调这里的
:func:`save`，不经过 ENV_SPECS——所以数组由本模块自行补丁 agent.json。

**迁移**：首次发现旧的 ``data/state/llm-providers.json``（或 env 覆盖路径）
且 agent.json 无 ``LLM_PROVIDERS`` 时，把内容并进 agent.json 并告警；旧文件不自动删。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# agent.json 里的两个键（普通 JSON 值）
PROVIDERS_KEY = "LLM_PROVIDERS"
ACTIVE_KEY = "LLM_ACTIVE"
# 旧的独立文件：只读用于一次性迁移，不再作为权威源
LEGACY_FILE_ENV = "AGENT_LLM_PROVIDERS_FILE"
_LEGACY_DEFAULT = "data/state/llm-providers.json"
_BACKUP_KEEP = 10
_PROTOCOLS = {"openai", "anthropic"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ProviderConfigError(ValueError):
    """配置不合法；message 面向管理员（HTTP 422 / QQ 回执）。"""


@dataclass
class Provider:
    id: str
    name: str
    protocol: str
    base_url: str
    model: str
    api_key: str = ""
    enabled: bool = True
    temperature: float | None = None
    max_tokens: int | None = None

    def public(self) -> dict:
        """对外形态：**绝不含 api_key / 完整 endpoint**（只给 netloc）。

        这会经 ``model_status()`` 流入 current_model skill（群聊里任何人都能问），
        完整 URL 只能走 ``admin_view()``，且仅用于鉴权的 web 设置面。
        """
        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol,
            "endpoint_host": urlsplit(self.base_url).netloc if self.base_url else "",
            "model": self.model,
            "enabled": self.enabled,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "key_set": bool(self.api_key),
        }

    def admin_view(self) -> dict:
        """web 管理员编辑面：在 public 基础上补完整 endpoint（不含 key）。"""
        return {**self.public(), "endpoint": self.base_url}


@dataclass
class ProviderConfig:
    active: str
    providers: list[Provider] = field(default_factory=list)
    version: int = 1

    def by_id(self, pid: str) -> Provider | None:
        return next((p for p in self.providers if p.id == pid), None)

    def enabled(self) -> list[Provider]:
        return [p for p in self.providers if p.enabled]


def agent_json_path() -> Path:
    """主配置 agent.json 的路径（provider 数组的落点）。

    与 ``agentcore.config_json`` 同一口径：``AGENT_CONFIG_JSON`` 可覆盖，
    空串/纯空白回落默认 ``config/agent.json``（容器内即 /app/config/agent.json）。
    """
    raw = (os.getenv("AGENT_CONFIG_JSON") or "").strip()
    return Path(raw or "config/agent.json")


def legacy_path() -> Path | None:
    """旧的独立 providers 文件路径；仅用于一次性迁移读取。

    env 未显式设置时返回 None（不再有默认落点）——它已经不是权威源了。
    """
    raw = (os.getenv(LEGACY_FILE_ENV) or "").strip()
    return Path(raw) if raw else None


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:32] or "provider"


def _unique_id(base: str, used: set[str]) -> str:
    cand, n = base[:32], 2
    while cand in used:
        suffix = f"-{n}"
        cand = base[: 32 - len(suffix)] + suffix
        n += 1
    return cand


def validate_provider(raw: dict) -> Provider:
    """校验并归一化一个 provider 条目（不含"保留旧 key"的合并逻辑）。"""
    if not isinstance(raw, dict):
        raise ProviderConfigError("provider 必须是对象")
    pid = str(raw.get("id") or "").strip().lower()
    name = str(raw.get("name") or "").strip()
    protocol = str(raw.get("protocol") or "openai").strip().lower()
    base_url = str(raw.get("endpoint") or raw.get("base_url") or "").strip().rstrip("/")
    model = str(raw.get("model") or "").strip()
    api_key = str(raw.get("api_key") or "").strip()
    if pid and not _ID_RE.match(pid):
        raise ProviderConfigError("id 只能是 32 位以内的 a-z0-9-_")
    if not name:
        raise ProviderConfigError("缺少展示名 name")
    if len(name) > 64:
        raise ProviderConfigError("name 过长（≤64）")
    if protocol not in _PROTOCOLS:
        raise ProviderConfigError(f"protocol 必须是 {'/'.join(sorted(_PROTOCOLS))}")
    if not base_url.startswith(("http://", "https://")):
        raise ProviderConfigError("base_url 必须以 http:// 或 https:// 开头")
    if not model or any(c in model for c in " \t\r\n"):
        raise ProviderConfigError("model 不能为空或含空白")
    if any(c in api_key for c in "\r\n"):
        raise ProviderConfigError("api_key 不能含换行")
    if len(api_key) > 4096:
        raise ProviderConfigError("api_key 过长")
    temp = raw.get("temperature")
    if temp in ("", None):
        temp = None
    else:
        try:
            temp = float(temp)
        except (TypeError, ValueError):
            raise ProviderConfigError("temperature 必须是数字") from None
        if not 0 <= temp <= 2:
            raise ProviderConfigError("temperature 必须在 0~2")
    max_tokens = raw.get("max_tokens")
    if max_tokens in ("", None):
        max_tokens = None
    else:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            raise ProviderConfigError("max_tokens 必须是整数") from None
        if not 1 <= max_tokens <= 10**6:
            raise ProviderConfigError("max_tokens 必须在 1~1000000")
    return Provider(
        id=pid,
        name=name,
        protocol=protocol,
        base_url=base_url,
        model=model,
        api_key=api_key,
        enabled=(
            str(raw.get("enabled")).strip().lower()
            not in {"0", "false", "no", "off", ""}
            if isinstance(raw.get("enabled"), str)
            else bool(raw.get("enabled", True))
        ),
        temperature=temp,
        max_tokens=max_tokens,
    )


def migrate_from_env(cfg) -> ProviderConfig:
    """把 env 旧配置（``_LLMCfg``）转成 provider 列表。

    幂等：纯函数，不写盘；调用方决定是否落盘。去重按 (model, base_url)，
    id 由 model 派生并自动避让。
    """
    providers: list[Provider] = []
    used: set[str] = set()
    seen: set[tuple[str, str]] = set()

    def add(model: str, base_url: str, api_key: str, *, protocol="openai") -> None:
        key = (model, base_url)
        if not model or key in seen:
            return
        seen.add(key)
        pid = _unique_id(_slug(model), used)
        used.add(pid)
        providers.append(
            Provider(
                id=pid,
                name=model,
                protocol=protocol,
                base_url=base_url,
                model=model,
                api_key=api_key,
            )
        )

    active = getattr(cfg, "active", None)
    main_model = getattr(active, "model", None) or getattr(cfg, "model", "")
    main_base = getattr(active, "base_url", "") or getattr(cfg, "base_url", "")
    main_key = getattr(active, "api_key", "") or getattr(cfg, "api_key", "")
    add(main_model, main_base, main_key)
    for entry in getattr(cfg, "models", ()) or ():
        add(
            entry.model,
            entry.base_url or getattr(cfg, "base_url", ""),
            entry.api_key or getattr(cfg, "api_key", ""),
        )
    if getattr(cfg, "fallback_model", ""):
        add(cfg.fallback_model, cfg.fallback_base_url, cfg.fallback_api_key)
    if not providers:
        # 极端兜底：连主模型都为空（测试/坏 env）也别产出空配置
        add(getattr(cfg, "model", "") or "unknown", getattr(cfg, "base_url", ""), "")
    return ProviderConfig(active=providers[0].id, providers=providers)


def _parse_providers(data: dict) -> ProviderConfig:
    """从 agent.json 的字典里解析 ``LLM_PROVIDERS`` / ``LLM_ACTIVE``。

    坏形态抛 :class:`ProviderConfigError`（调用方转 422 / 回落 env）。
    """
    raw_list = data.get(PROVIDERS_KEY)
    if not isinstance(raw_list, list):
        raise ProviderConfigError("providers_schema")
    providers = []
    used: set[str] = set()
    for raw in raw_list:
        if not isinstance(raw, dict):
            raise ProviderConfigError("provider_not_object")
        p_model = str(raw.get("model") or "")
        pid = str(raw.get("id") or "").strip().lower() or _unique_id(
            _slug(p_model), used
        )
        if pid in used:
            raise ProviderConfigError(f"provider_duplicate_id: {pid}")
        used.add(pid)
        providers.append(validate_provider({**raw, "id": pid}))
    if not providers:
        raise ProviderConfigError("providers_empty")
    active = str(data.get(ACTIVE_KEY) or "").strip()
    by_id = {p.id: p for p in providers}
    if active not in by_id:
        active = next((p.id for p in providers if p.enabled), providers[0].id)
    return ProviderConfig(active=active, providers=providers)


def load(path: Path | None = None) -> ProviderConfig:
    """从 agent.json 读 provider 配置。

    - 文件缺失 → :class:`FileNotFoundError`（调用方回落 env 单 provider）
    - agent.json 里**没有** ``LLM_PROVIDERS`` 键 → 同样抛 ``FileNotFoundError``，
      语义是「没有多 provider 配置」，让调用方走 env 回落（与旧「文件缺失」一致）
    - 有键但形态坏 → :class:`ProviderConfigError`
    """
    p = path or agent_json_path()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as e:
        raise ProviderConfigError(f"providers_file_invalid: {type(e).__name__}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProviderConfigError(f"providers_file_invalid: {type(e).__name__}") from e
    if not isinstance(data, dict):
        raise ProviderConfigError("agent_json_not_object")
    if PROVIDERS_KEY not in data:
        # 没有数组 = 没有多 provider 配置（可能是全新部署，只有 LLM_BASE_URL 等旧键）
        raise FileNotFoundError(f"{PROVIDERS_KEY} not in {p}")
    return _parse_providers(data)


def save(cfg: ProviderConfig, path: Path | None = None) -> None:
    """原子写 + 备份 + 写后重读校验；失败还原。"""
    p = path or agent_json_path()
    ids = {x.id for x in cfg.providers}
    if cfg.active not in ids:
        raise ProviderConfigError("active 不在 provider 列表里")
    if len(ids) != len(cfg.providers):
        raise ProviderConfigError("provider id 重复")
    if p.is_symlink():
        raise ProviderConfigError("providers_file_is_symlink")
    p.parent.mkdir(parents=True, exist_ok=True)
    # 读现文件 → 只替换两个键 → 整体重序列化（保住其余键与键序）
    try:
        current = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            raise ProviderConfigError("agent_json_not_object")
    except FileNotFoundError:
        current = {}
    except json.JSONDecodeError as e:
        raise ProviderConfigError(f"agent_json_invalid: {type(e).__name__}") from e
    current[PROVIDERS_KEY] = [asdict(x) for x in cfg.providers]
    current[ACTIVE_KEY] = cfg.active
    text = json.dumps(current, ensure_ascii=False, indent=2) + "\n"
    backup = None
    if p.exists():
        backup_dir = p.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**6:06d}"
        backup = backup_dir / f"{p.name}.{stamp}.bak"
        shutil.copy2(p, backup)
        os.chmod(backup, 0o600)  # 备份含 key，不得继承源文件权限（L14）
        for old in sorted(backup_dir.glob(f"{p.name}.*.bak"))[:-_BACKUP_KEEP]:
            old.unlink(missing_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    # O_NOFOLLOW 防 tmp 名被预置 symlink 截断目标；Windows 无此常量，取 0 兜底
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    def _rollback() -> None:
        if backup is not None:
            shutil.copy2(backup, p)
            return
        # 首次保存（无备份）校验失败：另一进程可能刚好写入了有效文件，
        # 先确认现文件是否可用，再决定是否删除（M4：双进程首启互删凭据）。
        try:
            load(p)
        except Exception:
            p.unlink(missing_ok=True)

    try:
        check = load(p)
    except Exception:
        _rollback()
        raise ProviderConfigError("providers_verify_failed_rolled_back") from None
    if check.active != cfg.active or len(check.providers) != len(cfg.providers):
        _rollback()
        raise ProviderConfigError("providers_verify_failed_rolled_back")
    logger.info(
        "llm providers saved: %d providers, active=%s", len(cfg.providers), cfg.active
    )


def _parse_legacy(data: dict) -> ProviderConfig:
    """把旧独立文件形态（顶层 ``providers``/``active``）转成 ProviderConfig。"""
    return _parse_providers(
        {PROVIDERS_KEY: data.get("providers"), ACTIVE_KEY: data.get("active")}
    )


def ensure_migrated(cfg=None, path: Path | None = None) -> ProviderConfig:
    """启动期调用：agent.json 无 ``LLM_PROVIDERS`` 时把配置补起来。

    三个来源按优先级：

    1. **agent.json 已有数组** → 直接用（权威源）
    2. **旧的独立文件还在**（``AGENT_LLM_PROVIDERS_FILE`` 显式指过）→ 并入 agent.json
       并告警「可手动删除旧文件」。一次性迁移，避免用户手抄 key
    3. **都没有** → 从 env 派生（``LLM_BASE_URL``/``LLM_API_KEY``/``LLM_MODEL``…），
       **不落盘**：那是内存态兼容，agent.json 保持干净，只有用户显式保存或
       ``/model`` 切换才写数组

    **不在 ``LLMClient.__init__`` 里自动迁移**：那会让所有单测在构造客户端时产生
    写盘副作用、并把 patch 过的假 ``_CFG`` 变成 provider 配置。迁移是明确的一次
    启动动作，只有插件初始化调用它。
    """
    p = path or agent_json_path()
    try:
        return load(p)
    except FileNotFoundError:
        pass
    legacy = legacy_path()
    if legacy is not None and legacy.is_file() and legacy != p:
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("providers"), list):
                migrated = _parse_legacy(data)
                save(migrated, p)
                logger.warning(
                    "已把旧 provider 文件 %s 并入 %s，可手动删除旧文件", legacy, p
                )
                return migrated
        except Exception:
            logger.warning("旧 provider 文件迁移失败，改用 env 配置", exc_info=True)
    if cfg is None:
        from agentcore.llm.client import _CFG as cfg
    return migrate_from_env(cfg)


def restore_mode(path: Path) -> int:
    """仅供测试/诊断：返回文件权限位。"""
    return stat.S_IMODE(path.stat().st_mode)
