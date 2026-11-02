"""/persona 命令的纯解析逻辑（无 NoneBot 依赖，便于单元测试）。"""

from __future__ import annotations

import re

_PERSONA_CMD_RE = re.compile(r"^(personas?|人格|人设)\b[\s:：]*", re.IGNORECASE)

_VIEW = {"list", "ls", "列表", "查看"}
_RESET = {"reset", "clear", "默认", "清除", "恢复默认"}
_USE = {"use", "set", "switch", "切换", "使用"}
# new/edit 的正文必须保留原始换行（M6，REVIEW-63f9444..858afcc）：人格正文
# 即注入 system prompt 的内容，按空白 token 化会把多行 markdown 压成单行、
# 两段确认钉住的就是损坏文本。在清洗后的原始文本上整体匹配，正文原样保留。
_NEW_EDIT_RE = re.compile(
    r"^(new|创建|edit|编辑|改)\s+(\S+)(?:[ \t]+(.+))?$", re.DOTALL
)


def _strip_cmd(raw: str) -> str:
    """去掉开头的 / ! 与命令词（persona/personas/人格/人设），返回剩余原文。"""
    s = (raw or "").strip()
    s = re.sub(r"^[/!！]?\s*", "", s).strip()
    return _PERSONA_CMD_RE.sub("", s).strip()


def persona_tokens(raw: str) -> list[str]:
    """去掉开头的 / ! 与命令词（persona/personas/人格/人设），返回剩余词。"""
    s = _strip_cmd(raw)
    return s.split() if s else []


def parse_persona_cmd(raw: str) -> tuple[str, str | None]:
    """解析人格命令，返回 (action, rest)：
    - ("list", None)         查看
    - ("reset", None)        恢复默认
    - ("use", name)          切换人格（name 可能为空表示未提供；裸名字同 use）
    - ("new", "名字 正文")    新建人格（superuser 私聊；两段确认；正文保留原始换行）
    - ("edit", "名字 正文")   覆盖编辑（superuser 私聊；两段确认；正文保留原始换行）
    - ("confirm", token)     落盘上一段的预览
    """
    s = _strip_cmd(raw)
    tokens = s.split() if s else []
    if not tokens:
        return "list", None
    head = tokens[0].lower()
    rest = " ".join(tokens[1:]) if len(tokens) > 1 else None
    if head in _VIEW:
        return "list", None
    if head in _RESET:
        return "reset", None
    if head in _USE:
        return "use", (tokens[1] if len(tokens) >= 2 else "")
    m = _NEW_EDIT_RE.match(s)
    if m:
        action = "new" if m.group(1) in {"new", "创建"} else "edit"
        # rest 形如「名字 正文」，正文含原始换行；admin 侧 split(None, 1) 取回
        body = m.group(3)
        return action, (f"{m.group(2)} {body}" if body else m.group(2))
    if head in {"new", "创建"}:
        # 裸 new/edit（没给名字）等 regex 兜不住的退化形态，交由上层给用法提示
        return "new", rest
    if head in {"edit", "编辑", "改"}:
        return "edit", rest
    if head == "confirm":
        return "confirm", rest
    # 单独给一个人格名（如 /persona yun）也视为切换
    return "use", tokens[0]
