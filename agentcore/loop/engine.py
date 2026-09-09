import json
import logging
import re
from typing import Optional

from agentcore.llm.client import LLMClient
from agentcore.skills.registry import SkillRegistry
from agentcore.memory.store import BaseMemoryStore

logger = logging.getLogger(__name__)

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class AgentEngine:
    """自研 tool-loop 引擎：组装 prompt → 调 LLM → skill 调用 → 循环 → 最终回复。"""

    def __init__(
        self,
        llm: LLMClient,
        skills: SkillRegistry,
        memory: BaseMemoryStore,
        config: Optional[dict] = None,
    ):
        self.llm = llm
        self.skills = skills
        self.memory = memory
        self.config = config or {}
        self.max_iterations = self.config.get("max_iterations", 8)

    def _safe_text(self, value: str) -> str:
        return _CONTROL_CHAR_RE.sub("", value)

    def _build_system_prompt(self, context: dict) -> str:
        parts = ["你是一个有帮助的 AI 助手，基于 skill 与记忆回答用户问题。"]
        if context.get("group_id"):
            parts.append("当前在群聊中，回复尽量简洁、有条理，避免刷屏。")
        else:
            parts.append("当前在私聊中，可以适当详细。")
        if context.get("user_id"):
            parts.append(f"当前用户 ID：{self._safe_text(context['user_id'])}")
        parts.append("需要时调用可用 skill；如果 skill 返回错误，尝试换一种方式或直接告知用户。")
        return "\n".join(parts)

    async def run(self, context: dict, user_message: str) -> str:
        user_id = context.get("user_id", "unknown")
        group_id = context.get("group_id")
        session_id = await self.memory.resolve_session(user_id, group_id)

        history = await self.memory.get_history(session_id)

        system_prompt = self._build_system_prompt(context)
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        await self.memory.append_message(session_id, "user", self._safe_text(user_message))

        for step in range(self.max_iterations):
            try:
                response = await self.llm.chat(
                    messages,
                    tools=self.skills.get_schemas(user_id, group_id),
                )
            except Exception:
                logger.exception("LLM call failed at step %s", step)
                return "LLM 调用失败，请稍后再试。"

            choices = response.get("choices")
            if not choices:
                return "LLM 返回空响应，请重试或换个方式提问。"
            choice = choices[0].get("message") or {}

            if choice.get("tool_calls"):
                await self.memory.append_message(
                    session_id, "assistant", "", tool_calls=choice["tool_calls"]
                )
                messages.append({"role": "assistant", "tool_calls": choice["tool_calls"]})
                for tc in choice["tool_calls"]:
                    func_name = tc["function"]["name"]
                    try:
                        func_args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        func_args = {}
                    func_args.pop("user_id", None)
                    func_args.pop("group_id", None)
                    logger.info("skill call: %s %s", func_name, func_args)
                    result = await self.skills.execute(
                        func_name,
                        user_id=user_id,
                        group_id=group_id,
                        **func_args,
                    )
                    tool_call_id = tc.get("id") or None
                    safe_result = self._safe_text(str(result))
                    try:
                        await self.memory.append_message(
                            session_id,
                            "tool",
                            safe_result,
                            tool_call_id=tool_call_id,
                        )
                    except Exception:
                        logger.exception("memory append failed for tool result")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id or "",
                            "content": safe_result,
                        }
                    )
                continue

            content = choice.get("content") or ""
            safe_content = self._safe_text(content)
            try:
                await self.memory.append_message(session_id, "assistant", safe_content)
            except Exception:
                logger.exception("memory append failed for assistant message")
            safe_content = safe_content.strip()
            if safe_content:
                return safe_content
            return "（LLM 返回空内容，请换个方式提问）"

        return "（已达最大思考步数，请换个方式提问或发送 /reset 重置会话）"
