"""长期记忆：从用户消息中抽取事实（facts）。

抽取后的 fact 是一句陈述（如「用户住在北京」「用户喜欢 Python」），
由 EmbeddingClient 向量化后存入 memory store，供后续对话按语义召回。
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = (
    "你是记忆抽取器。从下面的用户发言中，抽取值得长期记住的事实、偏好或背景信息"
    "（例如：姓名、所在城市、职业、语言/技术栈偏好、重要约定、禁忌等）。\n"
    "规则：\n"
    "1. 只输出 JSON 字符串数组，每个元素是一句简短陈述，主谓宾完整，例如 "
    '["用户住在北京", "用户正在使用 Python 开发"]。\n'
    "2. 临时性、一次性内容不要抽取（如普通提问、闲聊寒暄、本次任务的指令）。\n"
    "3. **绝对不要**抽取与图片/文件相关的临时动作与状态，包括但不限于："
    "「用户询问了图片内容」「用户上传/发送/分享了一张图片」「图片已保存到某路径」"
    "「用户提供了文件路径」——这些是一次性交互，不是长期事实。\n"
    "4. 不要记录文件路径、URL、文件名本身（项目仓库地址这类稳定标识除外）。\n"
    "5. 没有值得记的事实就输出 []。\n"
    "6. 不要输出任何其他文字。\n\n"
    "用户发言：\n{message}"
)

# 代码级兜底：即使模型没听提示词，也不让「图片/文件临时交互」进入长期记忆
_TRANSIENT_PATTERNS = (
    re.compile(r"图片|图像|插图|截图|表情包|照片已|图已", re.IGNORECASE),
    re.compile(r"media/|\.jpe?g|\.png|\.gif|\.webp|\.bmp", re.IGNORECASE),
    re.compile(r"工作区|文件路径|保存到|已保存至|上传了?文件", re.IGNORECASE),
)


def is_transient_fact(text: str) -> bool:
    """判断一条候选事实是否属于「临时交互」（图片/文件相关），应当丢弃。"""
    s = (text or "").strip()
    if not s:
        return True
    return any(p.search(s) for p in _TRANSIENT_PATTERNS)


def _parse_json_list(text: str) -> list[str]:
    """容忍 LLM 输出的 ```json ... ``` 包裹与前后杂讯。"""
    if not text:
        return []
    cleaned = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(1).strip()
    # 截取第一个 [ 到最后一个 ]
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(cleaned[start : end + 1])
    except Exception:
        logger.warning("facts parse failed, raw=%r", text[:500])
        return []
    if not isinstance(data, list):
        return []
    facts = []
    for item in data:
        if isinstance(item, str) and item.strip():
            facts.append(item.strip())
    return facts


async def extract_facts_from_message(
    llm: object,
    message: str,
    max_facts: int = 6,
) -> list[str]:
    """调用 LLM 抽取用户发言中的长期事实。任何失败都静默返回 []。"""
    try:
        prompt = EXTRACT_PROMPT.format(message=(message or "")[:1000])
        response = await llm.chat(
            [{"role": "system", "content": prompt}, {"role": "user", "content": "抽取事实"}],
            tools=None,
        )
        choice = (response.get("choices") or [{}])[0].get("message") or {}
        parsed = _parse_json_list(choice.get("content") or "")
        # 代码级兜底过滤：图片/文件临时交互不进长期记忆
        cleaned = [f for f in parsed if not is_transient_fact(f)]
        if len(cleaned) != len(parsed):
            logger.info(
                "extract facts: dropped %d transient fact(s)", len(parsed) - len(cleaned)
            )
        return cleaned[:max_facts]
    except Exception:
        logger.exception("extract facts failed")
        return []


def filter_new_facts(candidates: Sequence[str], existing: Sequence[str]) -> list[str]:
    """去掉与已有事实完全重复（或包含）的候选。"""
    existing_set = {e.strip().lower() for e in existing if e and e.strip()}
    new = []
    for c in candidates:
        key = c.strip().lower()
        if not key:
            continue
        if key in existing_set:
            continue
        if any(key in e or e in key for e in existing_set):
            continue
        new.append(c.strip())
    return new
