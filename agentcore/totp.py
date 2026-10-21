"""TOTP 双因素认证：5 分钟滚动码 + secret 管理（0600 文件）。

用于 web 写面的 2FA——用户私聊 bot 获取滚动码，网页填入才能确认修改。
secret 存独立 0600 文件（gitignore），首次自动生成；基于 RFC 4226 HOTP，
时间步长 300s（5 分钟窗口），验证容忍 ±1 步（时钟漂移/边界）。

设计：TOTP 码是「门禁」（证明持有者刚从 bot 拿到码），不绑定具体操作；
操作绑定由 web 的 challenge（HMAC(nonce|key|值)）负责，两者叠加。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SECRET_FILE_ENV = "AGENT_2FA_SECRET_FILE"
_DEFAULT_PATH = "data/2fa-secret.json"
_STEP = 300  # 5 分钟窗口
_DIGITS = 6


def _secret_path() -> Path:
    return Path(os.getenv(SECRET_FILE_ENV, _DEFAULT_PATH))


def generate_secret() -> str:
    """生成 base32 secret（20 字节 → 32 字符，兼容 authenticator App）。"""
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def load_or_create_secret() -> str:
    """读 secret；不存在/损坏则生成并落盘 0600（幂等，首次自动生成）。

    落盘失败（只读目录等）仍返回内存 secret——本次进程可用，重启后重新生成
    （旧码随之失效，属可接受降级：2FA 门禁宁可重置也不静默用固定值）。
    """
    p = _secret_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        secret = str(data.get("secret") or "").strip()
        if secret:
            return secret
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError, ValueError):
        logger.warning("2fa secret 文件损坏，重新生成", exc_info=True)
    secret = generate_secret()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"secret": secret}, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except OSError:
        logger.warning("2fa secret 落盘失败（仅内存生效）", exc_info=True)
    return secret


def _hotp(secret: str, counter: int) -> str:
    """HMAC-based OTP（RFC 4226）：HMAC-SHA1(secret, counter) 动态截取。"""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (
        (digest[offset] & 0x7F) << 24
        | (digest[offset + 1] & 0xFF) << 16
        | (digest[offset + 2] & 0xFF) << 8
        | (digest[offset + 3] & 0xFF)
    )
    return str(code % (10**_DIGITS)).zfill(_DIGITS)


def current_code(secret: str, timestamp: float | None = None) -> str:
    """当前 5 分钟窗口的滚动码。"""
    t = int(timestamp if timestamp is not None else time.time())
    return _hotp(secret, t // _STEP)


def verify(secret: str, code: str, timestamp: float | None = None) -> bool:
    """验证滚动码：当前窗口 ±1 步（容忍时钟漂移与窗口边界）。"""
    code = str(code or "").strip()
    if not (len(code) == _DIGITS and code.isascii() and code.isdigit()):
        return False
    t = int(timestamp if timestamp is not None else time.time())
    counter = t // _STEP
    for c in (counter - 1, counter, counter + 1):
        if hmac.compare_digest(_hotp(secret, c), code):
            return True
    return False
