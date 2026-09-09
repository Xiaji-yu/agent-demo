import json
import json
import logging
from typing import Optional

from agentcore.llm.client import LLMClient
from agentcore.tools.registry import ToolRegistry
from agentcore.memory.store import BaseMemoryStore

logger = logging.getLogger(__name__)


class AgentEngine:
    """自研 tool-loop 引擎：组装 prompt → 调 LLM → 工具调用 → 循环 → 最终回复。"""

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        memory: BaseMemoryStore,
        config: Optional[dict] = None,
    ):
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.config = config or {}
        self.max_iterations = self.config.get("max_iterations", 8)

    def _build_system_prompt(self, context: dict) -> str:
        parts = ["你是一个有帮助的 AI 助手，基于工具与记忆回答用户问题。"]
        if context.get("group_id"):
            parts.append("当前在群聊中，回复尽量简洁、有条理，避免刷屏。")
        else:
            parts.append("当前在私聊中，可以适当详细。")
        parts.append("需要时调用可用工具；如果工具返回错误，尝试换一种方式或直接告知用户。")
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

        await self.memory.append_message(session_id, "user", user_message)

        for step in range(self.max_iterations):
            try:
                response = await self.llm.chat(messages, tools=self.tools.get_schemas())
            except Exception:
                logger.exception("LLM call failed at step %s", step)
                return "LLM 调用失败，请稍后再试。"

            choice = response.get("choices", [{}])[0].get("message", {})

            if choice.get("tool_calls"):
                messages.append({"role": "assistant", "tool_calls": choice["tool_calls"]})
                for tc in choice["tool_calls"]:
                    func_name = tc["function"]["name"]
                    try:
                        func_args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        func_args = {}
                    logger.info("tool call: %s %s", func_name, func_args)
                    result = await self.tools.execute(func_name, **func_args)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": str(result),
                        }
                    )
                continue

            content = choice.get("content") or ""
            await self.memory.append_message(session_id, "assistant", content)
            return content.strip()

        return "（已达最大思考步数，请换个方式提问或发送 /reset 重置会话）"
