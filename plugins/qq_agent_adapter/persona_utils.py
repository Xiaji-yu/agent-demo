"""/persona 命令的纯解析逻辑（无 NoneBot 依赖，便于单元测试）。"""
from __future__ import annotations

import re
from typing import Optional

_PERSONA_CMD_RE = re.compile(r"^(personas?|人格|人设)\b[\s:：]*", re.IGNORECASE)

_VIEW = {"list", "ls", "列表", "查看"}
_RESET = {"reset", "clear", "默认", "清除", "恢复默认"}
_USE = {"use", "set", "switch", "切换", "使用"}


def persona_tokens(raw: str) -> list[str]:
    """去掉开头的 / ! 与命令词（persona/personas/人格/人设），返回剩余词。"""
    s = (raw or "").strip()
    s = re.sub(r"^[/!！]?\s*", "", s).strip()
    s = _PERSONA_CMD_RE.sub("", s).strip()
    return s.split() if s else []


def parse_persona_cmd(raw: str) -> tuple[str, Optional[str]]:
    """解析人格命令，返回 (action, name)：
    - ("list", None)  查看
    - ("reset", None) 恢复默认
    - ("use", name)   切换人格（name 可能为空表示未提供）
    """
    tokens = persona_tokens(raw)
    if not tokens or tokens[0].lower() in _VIEW:
        return "list", None
    if tokens[0].lower() in _RESET:
        return "reset", None
    if tokens[0].lower() in _USE:
        name = tokens[1] if len(tokens) >= 2 else ""
        return "use", name.strip()
    # 单独给一个人格名（如 /persona yun）也视为切换
    return "use", tokens[0]
