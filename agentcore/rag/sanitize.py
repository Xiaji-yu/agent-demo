"""公共知识库的脱敏与防注入后置过滤。

公共库是全局共享的：任何一条入库内容都可能被注入到别人的 prompt 里，因此
蒸馏时不仅要「不显示这是谁的知识」，还要防止知识库变成注入传播通道。

两层防护：
1. 蒸馏 prompt 要求模型脱敏（见 distill.py）——这是第一层，但模型不可全信；
2. 本模块做确定性后置过滤——命中即丢弃/脱敏，可测试、不依赖模型。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 个人身份标识：命中即替换为占位符（保留句子结构）
_PII_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[1-9]\d{4,11}"), "[数字已脱敏]"),                    # QQ 号/手机号/长数字 ID
    (re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"), "[邮箱已脱敏]"),
    (re.compile(r"(?:https?://|www\.)\S+"), "[链接已脱敏]"),
    (re.compile(r"(?:群|房间)\s*号?\s*\d+"), "[群号已脱敏]"),
]

# 指向特定个人的主语：出现则整条丢弃（改写成客观陈述是模型的责任）
_PERSONAL_SUBJECTS = (
    "用户", "该用户", "这位用户", "某人", "本人", "对方",
    "他的", "她的", "他本人", "她的", "该同学", "这位朋友",
)

# 指令性/角色扮演内容：知识库不得成为注入载体，命中即丢弃
_INJECTION_HINTS = (
    "忽略之前", "忽略上面", "忽略以上", "忽略所有", "不要理会之前",
    "ignore previous", "ignore all previous", "ignore the above", "disregard previous",
    "system prompt", "系统提示", "你现在是", "从现在开始你", "越狱", "开发者模式",
    "jailbreak", "roleplay as", "扮演",
)

# 明显无沉淀价值的内容
_NOISE_HINTS = ("哈哈哈哈", "草", "666", "收到", "在吗", "谢谢")


def scrub_pii(text: str) -> str:
    """把个人身份标识替换成占位符。"""
    out = text or ""
    for pat, repl in _PII_PATTERNS:
        out = pat.sub(repl, out)
    return out


def rejection_reason(text: str, min_len: int = 8) -> str | None:
    """返回不应入库的原因；可入库返回 None。

    min_len 只对正文要点有意义——标题很短是正常的（如「部署经验」），
    调用方对标题传 min_len=0。
    """
    s = (text or "").strip()
    if not s:
        return "empty"
    low = s.lower()
    for hint in _INJECTION_HINTS:
        if hint in low:
            return f"injection-like content ({hint})"
    for subj in _PERSONAL_SUBJECTS:
        if subj in s:
            return f"refers to a specific person ({subj})"
    if len(s) < min_len:
        return "too short"
    return None


def sanitize_point(point: str) -> tuple[str | None, str | None]:
    """清洗一个要点：返回 (清洗后的文本, 丢弃原因)。丢弃时文本为 None。"""
    reason = rejection_reason(point)
    if reason:
        return None, reason
    return scrub_pii(point).strip(), None


def sanitize_entry(entry: dict) -> tuple[dict | None, list[str]]:
    """清洗一条知识条目 {"title","points"}；返回 (条目或 None, 丢弃原因列表)。"""
    dropped: list[str] = []
    if not isinstance(entry, dict):
        return None, ["not a dict"]

    title = scrub_pii(str(entry.get("title") or "").strip())
    if title:
        title_reason = rejection_reason(title, min_len=0)  # 标题允许很短
        if title_reason:
            dropped.append(f"title: {title_reason}")
            title = ""

    points: list[str] = []
    raw_points = entry.get("points")
    if isinstance(raw_points, str):
        raw_points = [raw_points]
    for p in raw_points or []:
        cleaned, reason = sanitize_point(str(p))
        if cleaned:
            points.append(cleaned)
        else:
            dropped.append(reason or "unknown")
    if not points:
        return None, dropped or ["no usable points"]
    return {"title": title, "points": points}, dropped


def format_entry(entry: dict) -> str:
    """把条目渲染成入库的知识块文本。"""
    title = (entry.get("title") or "").strip()
    body = "\n".join(f"- {p}" for p in entry.get("points") or [])
    return f"{title}\n{body}".strip() if title else body
