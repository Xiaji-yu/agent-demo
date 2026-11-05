"""用户 id 解析与「仅管理员」判定。

管理员集合来自 env SUPERUSERS：
- bot.py 会把逗号分隔转成 JSON 数组写入环境；这里两者都兼容。
- 未配置时为空集合（fail-closed）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

# 权限身份（AC-cron-trusted）：cron trusted 轮次以 job owner 作权限身份，
# 但数据身份仍是 ``cron:<job_id>`` 伪用户。registry 的可见性/execute 判定
# 用 perm_user_id 已经覆盖，但 handler 内的 ``is_superuser(user_id)`` 二次
# 校验仍拿到伪用户 → fs_* 等技能「看得见、调不动」（2026-11-05 生产
# 「每日备忘录推送」实测：fs_read 返回「无权限」）。
# 这里用 ContextVar 在 handler 调用期间声明权限身份，只影响 is_superuser()；
# 会话键、预算、growth、handler 收到的 user_id 一律不变。
_perm_user_id: ContextVar[str | None] = ContextVar("perm_user_id", default=None)


@contextmanager
def permission_identity(user_id: str | None) -> Iterator[None]:
    token = _perm_user_id.set(user_id or None)
    try:
        yield
    finally:
        _perm_user_id.reset(token)


def current_perm_user_id() -> str | None:
    return _perm_user_id.get()


def workspace_root() -> Path:
    """共享工作区根目录（单一事实来源；WORKSPACE_DIR 未配置时用 data/workspace）。"""
    return Path(os.getenv("WORKSPACE_DIR", "data/workspace")).resolve()


def load_superusers() -> set[str]:
    raw = (os.getenv("SUPERUSERS") or "").strip()
    if not raw:
        return set()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            return {str(x) for x in data} if isinstance(data, list) else set()
        except Exception:
            return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_superuser(user_id: str) -> bool:
    if user_id and user_id in load_superusers():
        return True
    perm = _perm_user_id.get()
    return bool(perm) and perm in load_superusers()
