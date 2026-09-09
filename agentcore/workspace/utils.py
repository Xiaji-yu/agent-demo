"""用户 id 解析与「仅管理员」判定。

管理员集合来自 env SUPERUSERS：
- bot.py 会把逗号分隔转成 JSON 数组写入环境；这里两者都兼容。
- 未配置时为空集合（fail-closed）。
"""
from __future__ import annotations

import json
import os


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
