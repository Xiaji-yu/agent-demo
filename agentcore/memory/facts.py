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
    "3. 没有值得记的事实就输出 []。\n"
    "4. 不要输出任何其他文字。\n\n"
    "用户发言：\n{message}"
)


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
        return _parse_json_list(choice.get("content") or "")[:max_facts]
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
