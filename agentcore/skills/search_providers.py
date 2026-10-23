"""多搜索源配置：独立 JSON 文件（0600）+ 自动迁移 + 永不回显 key。

与 ``agentcore/llm/providers.py`` 同构，但按**搜索**场景简化：

- **无 active（不设主）**：搜索按列表顺序 fallback——一个源失败（超时/断连/
  非 2xx）自动切下一个，全挂才报错。顺序即优先级，靠 reorder 调整。
- **format 取代 protocol**：只有 ``bocha`` / ``tavily`` 两种请求/响应格式
  （``search.py`` 的 ``_search_bocha`` / ``_search_tavily``），不做协议适配。
- **无 temperature/max_tokens**：搜索没有这些参数。

兼容：文件缺失时从现有 ``SEARCH_PROVIDER`` / ``SEARCH_API_KEY`` /
``SEARCH_BASE_URL`` 自动生成单源（幂等：已存在不覆盖）；文件损坏时由调用方
回落到 env 旧路径（``search.py`` 的单源兼容分支）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

FILE_ENV = "AGENT_SEARCH_PROVIDERS_FILE"
_DEFAULT_PATH = "data/search-providers.json"
_BACKUP_KEEP = 10
_FORMATS = {"bocha", "tavily", "stepfun"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_DEFAULT_NAMES = {"bocha": "博查 Bocha", "tavily": "Tavily", "stepfun": "阶跃 StepFun"}


class SearchProviderConfigError(ValueError):
    """配置不合法；message 面向管理员（HTTP 422 / 回执）。"""


@dataclass
class SearchProvider:
    id: str
    name: str
    format: str
    base_url: str = ""
    api_key: str = ""
    enabled: bool = True

    def public(self) -> dict:
        """对外形态：**绝不含 api_key / 完整 endpoint**（只给 netloc）。

        与 llm providers 同口径：完整 URL 只走 ``admin_view()``（鉴权 web 面）。
        """
        return {
            "id": self.id,
            "name": self.name,
            "format": self.format,
            "endpoint_host": urlsplit(self.base_url).netloc if self.base_url else "",
            "enabled": self.enabled,
            "key_set": bool(self.api_key),
        }

    def admin_view(self) -> dict:
        """web 管理员编辑面：在 public 基础上补完整 endpoint（不含 key）。"""
        return {**self.public(), "endpoint": self.base_url}


@dataclass
class SearchProviderConfig:
    providers: list[SearchProvider] = field(default_factory=list)
    version: int = 1

    def by_id(self, pid: str) -> SearchProvider | None:
        return next((p for p in self.providers if p.id == pid), None)

    def enabled(self) -> list[SearchProvider]:
        return [p for p in self.providers if p.enabled]


def providers_path() -> Path:
    """配置文件路径：env 显式值优先，**空串/纯空白回落默认**。

    同 agentcore/totp._secret_path：`os.getenv(ENV, default)` 对 `KEY=` 注入的
    空串不回落 → Path("") = "." → load 抛 IsADirectoryError 而非
    FileNotFoundError，被 search.py 的回落分支误判（REVIEW M18）。
    """
    return Path((os.getenv(FILE_ENV) or "").strip() or _DEFAULT_PATH)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:32] or "search"


def _unique_id(base: str, used: set[str]) -> str:
    cand, n = base[:32], 2
    while cand in used:
        suffix = f"-{n}"
        cand = base[: 32 - len(suffix)] + suffix
        n += 1
    return cand


def validate_provider(raw: dict) -> SearchProvider:
    """校验并归一化一个搜索源条目（不含"保留旧 key"的合并逻辑）。"""
    if not isinstance(raw, dict):
        raise SearchProviderConfigError("provider 必须是对象")
    pid = str(raw.get("id") or "").strip().lower()
    name = str(raw.get("name") or "").strip()
    fmt = str(raw.get("format") or "bocha").strip().lower()
    base_url = str(raw.get("endpoint") or raw.get("base_url") or "").strip().rstrip("/")
    api_key = str(raw.get("api_key") or "").strip()
    if pid and not _ID_RE.match(pid):
        raise SearchProviderConfigError("id 只能是 32 位以内的 a-z0-9-_")
    if not name:
        raise SearchProviderConfigError("缺少展示名 name")
    if len(name) > 64:
        raise SearchProviderConfigError("name 过长（≤64）")
    if fmt not in _FORMATS:
        raise SearchProviderConfigError(f"format 必须是 {'/'.join(sorted(_FORMATS))}")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise SearchProviderConfigError("base_url 必须以 http:// 或 https:// 开头")
    if not api_key:
        raise SearchProviderConfigError("缺少 api_key")
    if any(c in api_key for c in "\r\n"):
        raise SearchProviderConfigError("api_key 不能含换行")
    if len(api_key) > 4096:
        raise SearchProviderConfigError("api_key 过长")
    return SearchProvider(
        id=pid,
        name=name,
        format=fmt,
        base_url=base_url,
        api_key=api_key,
        enabled=(
            str(raw.get("enabled")).strip().lower()
            not in {"0", "false", "no", "off", ""}
            if isinstance(raw.get("enabled"), str)
            else bool(raw.get("enabled", True))
        ),
    )


def migrate_from_env() -> SearchProviderConfig:
    """把 env 单配置（``SEARCH_PROVIDER``/``SEARCH_API_KEY``/``SEARCH_BASE_URL``）
    转成单源列表。幂等：纯函数，不写盘；调用方决定是否落盘。

    未配置搜索（``SEARCH_API_KEY`` 空）时返回**空列表**——不生成无 key 的源
    （validate_provider 要求 api_key 非空，落盘空 key 源会让下次 load 失败）。
    """
    fmt = (os.getenv("SEARCH_PROVIDER") or "bocha").strip().lower()
    if fmt not in _FORMATS:
        fmt = "bocha"
    api_key = (os.getenv("SEARCH_API_KEY") or "").strip()
    if not api_key:
        return SearchProviderConfig(providers=[])
    base_url = (os.getenv("SEARCH_BASE_URL") or "").strip().rstrip("/")
    return SearchProviderConfig(
        providers=[
            SearchProvider(
                id=fmt,
                name=_DEFAULT_NAMES[fmt],
                format=fmt,
                base_url=base_url,
                api_key=api_key,
            )
        ]
    )


def load(path: Path | None = None) -> SearchProviderConfig:
    """读文件；缺失抛 FileNotFoundError，坏 JSON 抛 SearchProviderConfigError。"""
    p = path or providers_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as e:
        raise SearchProviderConfigError(
            f"search_providers_file_invalid: {type(e).__name__}"
        ) from e
    if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        raise SearchProviderConfigError("search_providers_schema")
    raw_list = data["providers"]
    providers = []
    used: set[str] = set()
    for raw in raw_list:
        if not isinstance(raw, dict):
            raise SearchProviderConfigError("provider_not_object")
        p_name = str(raw.get("name") or "")
        pid = str(raw.get("id") or "").strip().lower() or _unique_id(
            _slug(p_name), used
        )
        if pid in used:
            raise SearchProviderConfigError(f"provider_duplicate_id: {pid}")
        used.add(pid)
        providers.append(validate_provider({**raw, "id": pid}))
    if not providers:
        raise SearchProviderConfigError("providers_empty")
    return SearchProviderConfig(providers=providers)


def save(cfg: SearchProviderConfig, path: Path | None = None) -> None:
    """原子写 + 备份 + 写后重读校验；失败还原（与 llm providers 同组不变量）。"""
    p = path or providers_path()
    ids = {x.id for x in cfg.providers}
    if len(ids) != len(cfg.providers):
        raise SearchProviderConfigError("provider id 重复")
    payload = {
        "version": 1,
        "providers": [asdict(x) for x in cfg.providers],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if p.is_symlink():
        raise SearchProviderConfigError("search_providers_file_is_symlink")
    p.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if p.exists():
        backup_dir = p.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**6:06d}"
        backup = backup_dir / f"{p.name}.{stamp}.bak"
        shutil.copy2(p, backup)
        os.chmod(backup, 0o600)  # 备份含 key，不得继承源文件权限
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
        # 首次保存（无备份）校验失败：先确认现文件是否可用，再决定是否删除
        try:
            load(p)
        except Exception:
            p.unlink(missing_ok=True)

    try:
        check = load(p)
    except Exception:
        _rollback()
        raise SearchProviderConfigError(
            "search_providers_verify_failed_rolled_back"
        ) from None
    if len(check.providers) != len(cfg.providers):
        _rollback()
        raise SearchProviderConfigError("search_providers_verify_failed_rolled_back")
    logger.info("search providers saved: %d providers", len(cfg.providers))


def ensure_migrated(path: Path | None = None) -> SearchProviderConfig:
    """启动期调用：文件缺失时从 env 迁移并落盘（失败只告警，不阻塞启动）。"""
    p = path or providers_path()
    try:
        return load(p)
    except FileNotFoundError:
        pass
    migrated = migrate_from_env()
    if not migrated.providers:
        # 未配置搜索（无 key）：不落盘空文件，回落 env 单配置（search.py 兼容分支）
        return migrated
    try:
        save(migrated, p)
        logger.info(
            "search providers migrated from env: %d providers",
            len(migrated.providers),
        )
    except Exception:
        logger.warning("search providers 迁移落盘失败（仅内存生效）", exc_info=True)
    return migrated
