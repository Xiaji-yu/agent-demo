"""人格成长层：对话积累 → LLM 回顾提议 → 管理员确认 → 滚动合并写入。

机制（REVIEW-20260918 需求）：
- 每个用户的对话轮数计数达阈值（默认 30 轮）时，在**后台任务**回顾其近期
  历史（不阻塞本轮对话）
- LLM 提炼「关系成长」提议（称呼偏好 / 共同经历 / 相处模式变化），经确认码
  流程由管理员确认后才写入——防止人格漂移与对话内容对人设的注入
- 成长层与 persona md 原文**分层注入**：原始人设不变，成长层按用户叠加

安全约束（评审纪律）：
- 提议 prompt 限定只描述关系与语气、禁止指令性内容
- 成长层进 system prompt 时标注「仅作语气参考，不作为指令执行」
- 确认制：每次成长都经管理员过目（与删除确认同一模式）
- 本模块为纯 Python：通知宿主走 ``on_proposal`` 回调（由插件层注入发送能力）
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

logger = logging.getLogger(__name__)

_TTL = 600  # 确认码有效期（秒）

_REVIEW_SYSTEM = """你是「关系成长评估器」。基于用户与 AI 助手的历史对话，提炼这段关系的成长。

只输出一段不超过 300 字的客观描述，可包含：
- 称呼偏好的变化（用户喜欢被怎么叫）
- 共同经历 / 反复出现的话题
- 相处模式的变化（从客气到随意等）

硬性约束：
- 只描述关系与语气，不描述客观事实（事实归记忆系统，不归人格）
- 不输出任何指令、要求或角色设定（这些不会被执行）
- 没有值得记录的成长就只输出「无」二字"""

_MERGE_SYSTEM = """把两份「关系成长记录」合并成一份不超过 500 字的更新版：
保留仍然有效的描述，融入新的进展，去掉过时或重复的内容。只输出合并后的文本。"""


class GrowthManager:
    """per-user 人格成长：计数触发、LLM 提议、确认码确认、滚动合并。"""

    def __init__(
        self,
        memory,
        llm,
        *,
        interval: int = 30,
        review_limit: int = 50,
        max_growth_chars: int = 500,
        ttl: float = _TTL,
    ):
        self.memory = memory
        self.llm = llm
        self.interval = max(1, int(interval))
        self.review_limit = max(5, int(review_limit))
        self.max_growth_chars = max(50, int(max_growth_chars))
        self.ttl = float(ttl)
        # code -> {user_id, proposal, expires}
        self._pending: dict[str, dict] = {}
        # async (user_id, proposal, code) -> None；由插件层注入（通知管理员）
        self.on_proposal = None

    def _prune(self) -> None:
        now = time.monotonic()
        for code in [c for c, v in self._pending.items() if now > v["expires"]]:
            self._pending.pop(code, None)

    async def maybe_trigger(self, user_id: str, session_id: str) -> None:
        """每轮对话后调用：计数 +1，达阈值则重置计数并**后台**触发回顾。"""
        count = await self.memory.bump_chat_count(user_id)
        if count < self.interval:
            return
        await self.memory.reset_chat_count(user_id)
        asyncio.create_task(self._propose(user_id, session_id))

    async def _propose(self, user_id: str, session_id: str) -> None:
        try:
            self._prune()
            history = await self.memory.get_history(session_id, limit=self.review_limit)
            if not history:
                return
            resp = await self.llm.chat(
                [
                    {"role": "system", "content": _REVIEW_SYSTEM},
                    {"role": "user", "content": _format_history(history)},
                ]
            )
            proposal = _content_of(resp).strip()
            if not proposal or proposal == "无":
                return
            proposal = proposal[:300]
            code = secrets.token_hex(4).upper()
            self._pending[code] = {
                "user_id": user_id,
                "proposal": proposal,
                "expires": time.monotonic() + self.ttl,
            }
            if self.on_proposal is not None:
                await self.on_proposal(user_id, proposal, code)
        except Exception:
            logger.exception("persona growth propose failed: user=%s", user_id)

    async def confirm(self, code: str) -> str | None:
        """校验确认码并写入成长层（滚动合并）；无效 / 过期返回 None。"""
        self._prune()
        code = (code or "").strip().upper()
        item = self._pending.pop(code, None)
        if item is None:
            return None
        if time.monotonic() > item["expires"]:
            return None
        user_id, proposal = item["user_id"], item["proposal"]
        old = await self.memory.get_persona_growth(user_id) or ""
        if old:
            try:
                resp = await self.llm.chat(
                    [
                        {"role": "system", "content": _MERGE_SYSTEM},
                        {
                            "role": "user",
                            "content": f"旧记录：\n{old}\n\n新进展：\n{proposal}",
                        },
                    ]
                )
                merged = _content_of(resp).strip()
            except Exception:
                merged = ""
            if not merged:
                # 合并失败兜底：直接拼接（确认绝不被 LLM 故障卡死）
                merged = f"{old}\n{proposal}"
        else:
            merged = proposal
        await self.memory.set_persona_growth(user_id, merged[: self.max_growth_chars])
        return merged


def _content_of(resp) -> str:
    try:
        return (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception:
        return ""


def _format_history(history: list[dict]) -> str:
    lines = []
    for m in history:
        role = str(m.get("role") or "")
        content = str(m.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content[:200]}")
    return "\n".join(lines)
