"""多 LLM Provider 配置：独立 JSON 文件（0600）+ 自动迁移 + 永不回显 key。

为什么从 ``LLM_MODELS`` 单行 csv 换成 JSON 文件：多 provider 需要
id / 展示名 / 协议 / 端点 / model / key / 启用位 / 顺序——单行 csv 已经装不下，
而且 key 会混在 `.env` 的可读文本里。独立文件还能做到：

- **0600**：只有进程用户可读；web 只回 ``key_set``，永不回显；
- **原子写 + 备份 + 写后重读校验**：与 ``config_write`` 同一组不变量；
- **不污染 config.yaml 的注释**：那是运维文档，不能因为加 provider 被 safe_dump 抹掉。

兼容：文件缺失时从现有 ``LLM_MODEL`` / ``LLM_MODELS`` / ``LLM_FALLBACK_*``
自动生成一份（幂等：已存在不覆盖）；文件损坏时由调用方回落到 env 旧路径。
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

FILE_ENV = "AGENT_LLM_PROVIDERS_FILE"
_DEFAULT_PATH = "data/llm-providers.json"
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


def providers_path() -> Path:
    """配置文件路径：env 显式值优先，**空串/纯空白回落默认**。

    同 agentcore/totp._secret_path：`os.getenv(ENV, default)` 对 `KEY=` 注入的
    空串不回落 → Path("") = "."（REVIEW-1b16fa7..9c2d3c1 M18 存量同型，
    随本轮一并修）。
    """
    return Path((os.getenv(FILE_ENV) or "").strip() or _DEFAULT_PATH)


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


def load(path: Path | None = None) -> ProviderConfig:
    """读文件；缺失抛 FileNotFoundError，坏 JSON 抛 ProviderConfigError。"""
    p = path or providers_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as e:
        raise ProviderConfigError(f"providers_file_invalid: {type(e).__name__}") from e
    if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        raise ProviderConfigError("providers_file_schema")
    raw_list = data["providers"]
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
    active = str(data.get("active") or "").strip()
    by_id = {p.id: p for p in providers}
    if active not in by_id:
        active = next((p.id for p in providers if p.enabled), providers[0].id)
    return ProviderConfig(active=active, providers=providers)


def save(cfg: ProviderConfig, path: Path | None = None) -> None:
    """原子写 + 备份 + 写后重读校验；失败还原。"""
    p = path or providers_path()
    ids = {x.id for x in cfg.providers}
    if cfg.active not in ids:
        raise ProviderConfigError("active 不在 provider 列表里")
    if len(ids) != len(cfg.providers):
        raise ProviderConfigError("provider id 重复")
    payload = {
        "version": 1,
        "active": cfg.active,
        "providers": [asdict(x) for x in cfg.providers],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if p.is_symlink():
        raise ProviderConfigError("providers_file_is_symlink")
    p.parent.mkdir(parents=True, exist_ok=True)
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
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
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


# env 旧键 → provider 字段（漂移检测用；只把**字段名**进日志，值一律不落）
_DRIFT_LABELS = {
    "model": "LLM_MODEL",
    "base_url": "LLM_BASE_URL",
    "api_key": "LLM_API_KEY",
}


def _placeholder(value: str) -> bool:
    """模板占位值（与 scripts/deploy.py check 同口径）。"""
    v = (value or "").strip()
    return not v or v in {"...", "xxx"} or v.lower().startswith("your")


def env_drift(file_cfg: ProviderConfig, cfg=None) -> list[str]:
    """env 旧键与 provider 文件**不一致**的字段名（不含值，可安全进日志）。

    为什么需要：迁移是一次性的，文件一旦生成，``chat()`` 就走 provider 链，
    `.env` 里的 ``LLM_BASE_URL/API_KEY/MODEL`` 从此完全被忽略——但它们在
    `.env.example` 里仍是最显眼的一组配置，用户改完重启只会觉得「没生效」。

    只在 env 侧**确实填了**（非空、非占位）时才比：key 只在 web 模型页填、
    `.env` 留着 `...` 是正常部署，不该每次启动都告警。
    """
    if cfg is None:
        from agentcore.llm.client import _CFG as cfg
    try:
        migrated = migrate_from_env(cfg)
    except Exception:
        return []
    want = migrated.providers[0]
    have = file_cfg.by_id(file_cfg.active)
    if have is None:
        return []
    return [
        field_name
        for field_name, env_key in _DRIFT_LABELS.items()
        if not _placeholder(os.environ.get(env_key, ""))
        and getattr(want, field_name, "") != getattr(have, field_name, "")
    ]


def ensure_migrated(cfg=None, path: Path | None = None) -> ProviderConfig:
    """启动期调用：文件缺失时从 env 迁移并落盘（失败只告警，不阻塞启动）。

    **不在 ``LLMClient.__init__`` 里自动迁移**：那会让所有单测在构造客户端时
    产生写盘副作用、并把 patch 过的假 ``_CFG`` 变成 provider 配置。迁移是
    明确的一次启动动作，只有插件初始化调用它。
    """
    p = path or providers_path()
    try:
        existing = load(p)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        # 迁移是**一次性**的：文件存在即权威，env 的 LLM_* 从此被忽略。
        # 用户改完 .env 重启发现没生效时，唯一的线索就是这条日志（2026-10-28
        # 部署反馈：以为 .env 改了就该生效，实际被文件覆盖，只能靠猜）。
        drifted = env_drift(existing, cfg)
        if drifted:
            logger.warning(
                "env 里的 %s 与 %s 不一致——以文件为准（env 值只在文件缺失时参与"
                "首次迁移）。改模型/端点/key 请用 web「模型」标签或 /model；"
                "确实想用 .env 覆盖：备份后删掉该文件再重启",
                "/".join(_DRIFT_LABELS[f] for f in drifted),
                p,
            )
        return existing
    if cfg is None:
        from agentcore.llm.client import _CFG as cfg
    migrated = migrate_from_env(cfg)
    try:
        save(migrated, p)
        logger.info(
            "llm providers migrated from env: %d providers", len(migrated.providers)
        )
    except Exception:
        logger.warning("llm providers 迁移落盘失败（仅内存生效）", exc_info=True)
    return migrated


def restore_mode(path: Path) -> int:
    """仅供测试/诊断：返回文件权限位。"""
    return stat.S_IMODE(path.stat().st_mode)
