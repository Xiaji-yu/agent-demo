"""删除二次确认：LLM 请求删除 → 登记确认码 → 用户回复确认码后才执行。"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_TTL = 600  # 确认码有效期（秒）


class DeletionGate:
    def __init__(self, ttl: float = _TTL):
        self.ttl = ttl
        # user_id -> {code: {"path": str, "expires": float}}
        self._pending: Dict[str, Dict[str, dict]] = {}
        self._lock = asyncio.Lock()

    async def request(self, user_id: str, abs_path: str) -> str:
        code = secrets.token_hex(3).upper()
        async with self._lock:
            self._pending.setdefault(user_id, {})[code] = {
                "path": abs_path,
                "expires": time.monotonic() + self.ttl,
            }
        return code

    async def confirm(self, user_id: str, code: str) -> Optional[str]:
        """校验并返回待删除的绝对路径；无效/过期返回 None。"""
        code = (code or "").strip().upper()
        async with self._lock:
            bucket = self._pending.get(user_id, {})
            item = bucket.get(code)
            if not item:
                return None
            if time.monotonic() > item["expires"]:
                bucket.pop(code, None)
                return None
            bucket.pop(code, None)
            return item["path"]

    def pending_count(self, user_id: str) -> int:
        return len(self._pending.get(user_id, {}))

    async def prune(self) -> None:
        now = time.monotonic()
        async with self._lock:
            for uid, bucket in list(self._pending.items()):
                expired = [c for c, it in bucket.items() if now > it["expires"]]
                for c in expired:
                    bucket.pop(c, None)
                if not bucket:
                    self._pending.pop(uid, None)


_gate: Optional[DeletionGate] = None


def get_gate() -> DeletionGate:
    global _gate
    if _gate is None:
        _gate = DeletionGate()
    return _gate
