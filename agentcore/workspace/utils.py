"""用户 id 解析与「仅管理员」判定。

管理员集合来自 env SUPERUSERS：
- bot.py 会把逗号分隔转成 JSON 数组写入环境；这里两者都兼容。
- 未配置时为空集合（fail-closed）。
"""
from __future__ import annotations

import json
import os
import re

_USER_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def safe_user_dirname(user_id: str) -> str:
    """把 user_id 转成安全的目录名；非法则抛 ValueError。"""
    if not user_id or not _USER_ID_RE.match(user_id):
        raise ValueError(f"invalid user_id: {user_id!r}")
    return user_id


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
    return bool(user_id) and user_id in load_superusers()
