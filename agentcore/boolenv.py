"""布尔 env 的统一口径（L7，REVIEW-ee39c8e..e55cab5）。

历史形态：各处自己写 ``raw not in {"0", "false", "no", "off"}``。大小写与带空白
的值（``"False\\n"`` / ``"OFF"``）早被 ``.strip().lower()`` 收掉了，但
``disabled`` / ``n`` 这类**认不出来的值会静默按开启**——运维以为自己关了某个
开关、实际开着，而日志里一行记录都没有。这正是 AGENTS.md §5 判定为「诊断成本
极高」的静默配置失效模式。

这里给单一实现，语义与旧写法**完全一致**（认出的值照旧判、空值照旧取默认），
只多一件事：认不出的值告警一次后回退默认，把静默变成响。去重按 (name, value)，
同一个错值不刷屏；新出现的错值仍会被报。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_FALSE = {"0", "false", "no", "off"}
_TRUE = {"1", "true", "yes", "on"}

# 已告警过的 (env 名, 值)；测试可清空以复位
_warned: set[tuple[str, str]] = set()


def parse_bool(raw: object) -> bool | None:
    """三态解析：认出 ``True``/``False``，认不出返回 ``None``（调用方决定默认）。

    大小写与首尾空白不敏感（``" False\\n"`` → ``False``）。
    """
    text = str(raw if raw is not None else "").strip().lower()
    if not text:
        return None
    if text in _FALSE:
        return False
    if text in _TRUE:
        return True
    return None


def env_flag(name: str, raw: object, default: bool) -> bool:
    """读一个布尔 env：认出值照旧判，空值取 ``default``，认不出告警一次后取 ``default``。"""
    value = parse_bool(raw)
    if value is not None:
        return value
    text = str(raw if raw is not None else "").strip().lower()
    if not text:
        return default
    key = (name, text)
    if key not in _warned:
        _warned.add(key)
        logger.warning(
            "%s=%r 不是布尔值（认得出的只有 %s / %s），已按 %s 处理；"
            "请改正配置，否则实际行为与预期不符",
            name,
            text,
            sorted(_TRUE),
            sorted(_FALSE),
            "开" if default else "关",
        )
    return default


def env_bool(name: str, default: bool = False) -> bool:
    """直接从 ``os.environ`` 读布尔 env（``env_flag`` 的便捷形态）。"""
    return env_flag(name, os.getenv(name), default)
