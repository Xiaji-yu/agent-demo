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
        embedding: Optional[object] = None,
    ):
        self.llm = llm
        self.skills = skills
        self.memory = memory
        self.config = config or {}
        self.max_iterations = self.config.get("max_iterations", 8)
        self.embedding = embedding
        # M4 长期记忆参数（均可通过 config 覆盖）
        self.facts_top_k = int(self.config.get("memory_facts_top_k", 5))
        self.facts_threshold = float(self.config.get("memory_facts_threshold", 0.15))
        self.extract_facts = bool(self.config.get("extract_facts", True))

    def _safe_text(self, value: str) -> str:
        return _CONTROL_CHAR_RE.sub("", value)

    def _build_system_prompt(self, context: dict, long_term_facts: Optional[list[dict]] = None) -> str:
        parts = [
            "你是一个有帮助的 AI 助手，基于 skill 与记忆回答用户问题。",
            "严格工作流：",
            "1. 如果用户要求搜索/抓取信息，必须调用 search_web 或 fetch_url skill。",
            "2. 如果用户要求文件/文档/md，必须调用 send_markdown_file skill，content 参数放完整 markdown 内容，filename 参数放文件名如 report.md。",
            "3. 如果工具返回错误，最多重试 2 次（换参数或换工具），不要直接放弃。",
            "4. 只有以上都不需要时，才返回最终文本回复。",
        ]
        if long_term_facts:
            fact_lines = "\n".join(f"- {f['content']}" for f in long_term_facts)
            parts.append(
                "用户长期记忆（可能过时，以当前对话为准）：\n" + fact_lines
            )
        if context.get("group_id"):
            parts.append("当前在群聊中，回复尽量简洁、有条理，避免刷屏。")
        else:
            parts.append("当前在私聊中，可以适当详细。")
        if context.get("user_id"):
            parts.append(f"当前用户 ID：{self._safe_text(context['user_id'])}")
        return "\n".join(parts)

    async def _recall_facts(self, user_id: str, query: str) -> list[dict]:
        if self.embedding is None:
            return []
        try:
            q_emb = await self.embedding.embed(query)
            facts = await self.memory.recall_facts(
                user_id, q_emb, top_k=self.facts_top_k, threshold=self.facts_threshold
            )
            return facts or []
        except Exception:
            logger.exception("facts recall failed")
            return []

    async def _remember_facts(self, user_id: str, session_id: str, message: str) -> None:
        if self.embedding is None or not self.extract_facts:
            return
        try:
            from agentcore.memory.facts import extract_facts_from_message

            candidates = await extract_facts_from_message(self.llm, message)
            if not candidates:
                return
            existing = await self.memory.list_facts(user_id, limit=200)
            from agentcore.memory.facts import filter_new_facts

            new_facts = filter_new_facts(candidates, existing)
            if not new_facts:
                return
            embeddings = await self.embedding.embed_many(new_facts)
            for content, emb in zip(new_facts, embeddings):
                try:
                    await self.memory.save_fact(
                        user_id, content, emb, source="user_message", session_id=session_id
                    )
                except Exception:
                    logger.exception("save fact failed: %s", content[:80])
            logger.info("remembered %d new fact(s) for user %s", len(new_facts), user_id)
        except Exception:
            logger.exception("remember facts failed")

    async def run(self, context: dict, user_message: str) -> str:
        user_id = context.get("user_id", "unknown")
        group_id = context.get("group_id")
        session_id = await self.memory.resolve_session(user_id, group_id)

        history = await self.memory.get_history(session_id)

        # M4：先抽取并保存用户消息中的长期事实（静默、失败不影响对话）
        await self._remember_facts(user_id, session_id, user_message)

        # M4：按语义召回相关长期记忆注入 system prompt
        long_term = await self._recall_facts(user_id, user_message)
        system_prompt = self._build_system_prompt(context, long_term)
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
                logger.warning("LLM returned empty choices at step %s", step)
                return "LLM 返回空响应，请重试或换个方式提问。"
            choice = choices[0].get("message") or {}
            logger.info(
                "LLM step %s: content=%r, tool_calls=%s",
                step,
                choice.get("content"),
                len(choice.get("tool_calls") or []) > 0,
            )

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
