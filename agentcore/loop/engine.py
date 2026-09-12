import json
import logging
import re

from agentcore.budget import get_budget
from agentcore.llm.client import LLMClient
from agentcore.memory.store import BaseMemoryStore
from agentcore.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

# 仅剔除真正的控制字符；保留 \t(09) \n(0a) \r(0d) 等空白，避免把换行抹成一行
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
# 用于 user_id/group_id 等标识符：连空白一起去掉，防止注入多行
_STRICT_ID_RE = re.compile(r"[\x00-\x1f\x7f\s]+")
# data URI 严格校验：裸 data: 前缀、非 base64 内容一律不透传给 provider
_DATA_URI_RE = re.compile(
    r"^data:image/(?:png|jpeg|jpg|webp|gif);base64,[A-Za-z0-9+/=]+$"
)
# 技能层权限拒绝（registry.execute 返回 "Error: permission denied for skill X"）
_PERMISSION_DENIED_RE = re.compile(
    r"permission denied|无权限|权限不足|not authorized", re.IGNORECASE
)
# M2：同一会话内无权限的工具最多容忍 LLM 重试几次，超过即硬停，不再空转
_MAX_DENIED_RETRIES = 2


def _valid_image_ref(image) -> bool:
    if not isinstance(image, str):
        return False
    if image.startswith("data:"):
        return bool(_DATA_URI_RE.match(image))
    return image.startswith("https://")


def _valid_tool_calls(value) -> bool:
    """L5：tool_calls 必须是「对象数组」；坏 JSONB（如被写坏成 object）视为缺失。"""
    return isinstance(value, list) and all(isinstance(tc, dict) for tc in value)


def _sanitize_history(history: list[dict]) -> list[dict]:
    """把裁剪出来的历史整理成合法的消息序列。

    `get_history` 取的是「最近 N 条」，边界可能正好切在一次工具调用中间，于是开头
    出现没有前置 ``assistant.tool_calls`` 的孤儿 ``tool`` 消息——OpenAI 兼容接口
    （DeepSeek 等）会直接 400：
    「Messages with role 'tool' must be a response to a preceding message with 'tool_calls'」。

    规则（按位置配对，而不是只看 id 是否出现过）：
    - 孤儿 ``tool`` 消息（前面没有带该 tool_call 的 assistant）→ 丢弃
    - ``assistant.tool_calls`` 只保留紧随其后确实有响应的那些；若一个都没有，
      退化成普通文本消息（无内容则整条丢弃）
    - 没有 ``tool_call_id`` 的 tool 消息 → 丢弃（旧数据可能是 NULL）
    - 形状非法的 ``tool_calls``（坏 JSONB，如被写坏成 object）→ 按缺失处理（L5）
    """
    out: list[dict] = []
    i = 0
    n = len(history)
    while i < n:
        msg = history[i]
        role = msg.get("role")

        # L5 防御：坏形 tool_calls（旧数据被写坏成 JSON object 等）按缺失处理并剥掉
        # 该字段，否则下方 ``tc.get(...)`` 会抛 AttributeError（异常有上层兜底不会崩
        # 进程，但整轮对话就废了）；剥掉后无内容则与「无响应 tool_calls」同样整条丢弃
        if (
            role == "assistant"
            and msg.get("tool_calls") is not None
            and not _valid_tool_calls(msg["tool_calls"])
        ):
            if not (msg.get("content") or "").strip():
                i += 1
                continue
            msg = {k: v for k, v in msg.items() if k != "tool_calls"}

        if role == "assistant" and msg.get("tool_calls"):
            # 收集紧随其后的连续 tool 响应
            j = i + 1
            responses: dict[str, dict] = {}
            while j < n and history[j].get("role") == "tool":
                tcid = str(history[j].get("tool_call_id") or "")
                if tcid:
                    responses[tcid] = history[j]
                j += 1
            kept = [
                tc for tc in msg["tool_calls"] if str(tc.get("id") or "") in responses
            ]
            if kept:
                out.append({**msg, "tool_calls": kept})
                for tc in kept:
                    out.append(responses[str(tc.get("id"))])
            elif (msg.get("content") or "").strip():
                out.append({k: v for k, v in msg.items() if k != "tool_calls"})
            i = j
            continue

        if role == "tool":
            i += 1  # 孤儿 tool：没有前置 assistant.tool_calls，直接丢
            continue

        out.append(msg)
        i += 1
    return out


def _is_permission_denied(result) -> bool:
    """判定技能执行结果是否为「权限不足」。"""
    return bool(_PERMISSION_DENIED_RE.search(str(result or "")))


class AgentEngine:
    """自研 tool-loop 引擎：组装 prompt → 调 LLM → skill 调用 → 循环 → 最终回复。"""

    def __init__(
        self,
        llm: LLMClient,
        skills: SkillRegistry,
        memory: BaseMemoryStore,
        config: dict | None = None,
        embedding: object | None = None,
        persona_manager: object | None = None,
        kb: object | None = None,
    ):
        self.llm = llm
        self.skills = skills
        self.memory = memory
        self.config = config or {}
        self.max_iterations = self.config.get("max_iterations", 8)
        self.embedding = embedding
        self.persona_manager = persona_manager
        # M4 长期记忆参数（均可通过 config 覆盖）
        self.facts_top_k = int(self.config.get("memory_facts_top_k", 5))
        self.facts_threshold = float(self.config.get("memory_facts_threshold", 0.15))
        self.extract_facts = bool(self.config.get("extract_facts", True))
        # M5 公共知识库（可选）：检索结果按不可信数据围栏注入 prompt
        self.kb = kb

    def _safe_text(self, value: str) -> str:
        return _CONTROL_CHAR_RE.sub("", value)

    def _safe_id(self, value: str) -> str:
        return _STRICT_ID_RE.sub("", value)

    def _build_system_prompt(
        self,
        context: dict,
        long_term_facts: list[dict] | None = None,
        persona_text: str = "",
        knowledge_block: str = "",
    ) -> str:
        parts = []
        if persona_text:
            parts.append(f"当前人格设定（请遵循此角色与语气）：\n{persona_text}")
        parts.extend(
            [
                "你是一个有帮助的 AI 助手，基于 skill 与记忆回答用户问题。",
                "严格工作流：",
                "1. 用户要求搜索/找热点/找最新信息时，优先调用 search_web（联网搜索返回摘要）。",
                "2. fetch_url 只用于抓取用户明确给出的具体网址；禁止自己猜测热榜/门户 URL 去抓取（多为 503/429 反爬，浪费时间）。",
                "3. 如果用户要求文件/文档/md，必须调用 send_markdown_file skill，content 参数放完整 markdown 内容，filename 参数放文件名如 report.md。",
                "4. 如果工具返回错误，最多重试 2 次（换参数或换工具），不要直接放弃；"
                "但「无权限 / permission denied / 仅管理员」类错误说明权限不足，不要重试，直接向用户说明。",
                "5. 只有以上都不需要时，才返回最终文本回复。",
            ]
        )
        if long_term_facts:
            fact_lines = "\n".join(f"- {f['content']}" for f in long_term_facts)
            parts.append(
                "用户长期记忆（仅当前会话/群内的记录，其他群聊与私聊的内容不可见；"
                "可能过时，以当前对话为准）：\n" + fact_lines
            )
        if context.get("group_id"):
            parts.append("当前在群聊中，回复尽量简洁、有条理，避免刷屏。")
        else:
            parts.append("当前在私聊中，可以适当详细。")
        if knowledge_block:
            parts.append(knowledge_block)
        if context.get("user_id"):
            parts.append(f"当前用户 ID：{self._safe_id(context['user_id'])}")
        return "\n".join(parts)

    async def _recall_facts(
        self, user_id: str, query: str, session_id: str | None = None
    ) -> list[dict]:
        if self.embedding is None:
            return []
        try:
            q_emb = await self.embedding.embed(query)
            facts = await self.memory.recall_facts(
                user_id,
                q_emb,
                top_k=self.facts_top_k,
                threshold=self.facts_threshold,
                session_id=session_id,
            )
            return facts or []
        except Exception:
            logger.exception("facts recall failed")
            return []

    async def _remember_facts(
        self, user_id: str, session_id: str, message: str
    ) -> None:
        if self.embedding is None or not self.extract_facts:
            return
        try:
            from agentcore.memory.facts import extract_facts_from_message

            candidates = await extract_facts_from_message(self.llm, message)
            if not candidates:
                return
            existing = await self.memory.list_facts(
                user_id, limit=200, session_id=session_id
            )
            from agentcore.memory.facts import filter_new_facts

            new_facts = filter_new_facts(candidates, existing)
            if not new_facts:
                return
            embeddings = await self.embedding.embed_many(new_facts)
            for content, emb in zip(new_facts, embeddings, strict=False):
                try:
                    await self.memory.save_fact(
                        user_id,
                        content,
                        emb,
                        source="user_message",
                        session_id=session_id,
                    )
                except Exception:
                    logger.exception("save fact failed: %s", content[:80])
            logger.info(
                "remembered %d new fact(s) for user %s", len(new_facts), user_id
            )
        except Exception:
            logger.exception("remember facts failed")

    async def _load_persona_text(self, user_id: str) -> str:
        """按用户读取当前人格的行为指南；未设置或名称失效时用默认人格。异常静默返回空。"""
        if self.persona_manager is None:
            return ""
        try:
            pname = await self.memory.get_user_persona(user_id) or None
            persona = self.persona_manager.get(pname) or self.persona_manager.default()
            return persona.body if persona and persona.body else ""
        except Exception:
            logger.exception("load persona failed")
            return ""

    async def _recall_knowledge(self, query: str) -> str:
        """检索公共知识库并渲染成不可信数据区块；未启用/无命中返回空串。"""
        if self.kb is None or not (query or "").strip():
            return ""
        try:
            hits = await self.kb.retrieve(query)
            return self.kb.format_block(hits) if hits else ""
        except Exception:
            logger.exception("knowledge recall failed")
            return ""

    async def run(
        self,
        context: dict,
        user_message: str,
        extra_images: list[str] | None = None,
    ) -> str:
        # M7 成本预算：硬闸开启且当日超预算时直接返回提示，不再发起 LLM 调用。
        # 闸门在**入口与 tool-loop 每一步**各判一次（评审 REVIEW-bbd8913..f6dffcc.md 的 M1）——
        # 只在入口判会让一轮对话最多再打 max_iterations 次 LLM，与"当日不再发起调用"不符
        blocked, reason = get_budget().chat_blocked()
        if blocked:
            return reason
        user_id = context.get("user_id", "unknown")
        group_id = context.get("group_id")
        session_id = await self.memory.resolve_session(user_id, group_id)

        history = _sanitize_history(await self.memory.get_history(session_id))

        # M4：先抽取并保存用户消息中的长期事实（静默、失败不影响对话）；
        # 空消息（纯图等）跳过抽取与召回，避免无效 LLM/embedding 开销
        has_text = bool((user_message or "").strip())
        if has_text:
            await self._remember_facts(user_id, session_id, user_message)
            # 长期记忆按会话（用户 + 群/私聊）作用域召回：不同群聊的记忆不互串
            long_term = await self._recall_facts(user_id, user_message, session_id)
        else:
            long_term = []
        persona_text = await self._load_persona_text(user_id)
        # M5：检索公共知识库（与个人无关的沉淀），按不可信数据围栏注入
        knowledge_block = await self._recall_knowledge(user_message)
        system_prompt = self._build_system_prompt(
            context, long_term, persona_text, knowledge_block
        )
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        image_msg_index = -1
        if extra_images:
            # 多模态：图片以内容块传给模型；仅接受严格 data URI 与 https URL
            parts = [{"type": "text", "text": user_message}]
            added_image = False
            for image in extra_images:
                if _valid_image_ref(image):
                    parts.append({"type": "image_url", "image_url": {"url": image}})
                    added_image = True
            if added_image:
                messages.append({"role": "user", "content": parts})
                image_msg_index = len(messages) - 1
            else:
                messages.append({"role": "user", "content": user_message})
        else:
            messages.append({"role": "user", "content": user_message})

        await self.memory.append_message(
            session_id, "user", self._safe_text(user_message)
        )

        empty_turns = 0
        max_empty_turns = 2
        denied_skills: set[str] = (
            set()
        )  # 本轮 tool-loop 已确认无权限的技能（每次 run 重建，非跨会话）
        denied_retries = 0
        for step in range(self.max_iterations):
            if step > 0:
                # M1 修复：中途越过预算必须立即停手，否则一次 tool-loop 还能再打多次 LLM
                blocked, reason = get_budget().chat_blocked()
                if blocked:
                    logger.warning(
                        "budget: hard gate reached mid-turn at step %s, aborting", step
                    )
                    return reason
            if (
                step > 0
                and image_msg_index >= 0
                and isinstance(messages[image_msg_index].get("content"), list)
            ):
                # tool-loop 后续步骤不再重发图片载荷（token/请求体按步数放大），
                # 首次调用后降级为纯文本
                messages[image_msg_index] = {"role": "user", "content": user_message}
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
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": choice["tool_calls"],
                    }
                )
                for tc in choice["tool_calls"]:
                    func_name = tc["function"]["name"]
                    try:
                        func_args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        func_args = {}
                    func_args.pop("user_id", None)
                    func_args.pop("group_id", None)
                    if func_name in denied_skills:
                        # M2：已确认无权限的技能不再真正执行——只回拒绝结果，
                        # 让「不重试」成为代码约束而非仅靠 prompt 引导
                        denied_retries += 1
                        logger.info("skill denied (skip re-exec): %s", func_name)
                        result = (
                            f"Error: permission denied for skill {func_name}"
                            "（本轮已确认无权限，请勿重试）"
                        )
                    else:
                        logger.info("skill call: %s %s", func_name, func_args)
                        result = await self.skills.execute(
                            func_name,
                            user_id=user_id,
                            group_id=group_id,
                            **func_args,
                        )
                        if _is_permission_denied(result):
                            denied_skills.add(func_name)
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
                if denied_retries > _MAX_DENIED_RETRIES:
                    logger.warning(
                        "aborting loop: %s retries on permission-denied skills %s",
                        denied_retries,
                        sorted(denied_skills),
                    )
                    return "（该操作需要管理员权限，当前账号权限不足，已停止重试。）"
                continue

            content = choice.get("content") or ""
            safe_content = self._safe_text(content).strip()
            if safe_content:
                try:
                    await self.memory.append_message(
                        session_id, "assistant", safe_content
                    )
                except Exception:
                    logger.exception("memory append failed for assistant message")
                return safe_content

            # LLM 返回了空内容且没有工具调用：给一两次机会重试，而不是直接放弃
            empty_turns += 1
            # 注意：finish_reason 在 choice 上，不在 message 里
            finish = choices[0].get("finish_reason")
            if empty_turns > max_empty_turns:
                logger.error(
                    "LLM kept returning empty output (finish_reason=%s, steps=%s); giving up",
                    finish,
                    step,
                )
                return "（LLM 返回空内容，请换个方式提问）"
            logger.warning(
                "LLM empty output at step %s (finish_reason=%s), retrying (%s/%s)%s",
                step,
                finish,
                empty_turns,
                max_empty_turns,
                "；输出被 max_tokens 截断，建议调大 LLM_MAX_TOKENS"
                if finish == "length"
                else "",
            )
            messages.append(
                {
                    "role": "user",
                    "content": "（提示：你上一步没有输出任何内容）请直接给用户一个完整、有用的中文回答，"
                    "或调用一个工具来完成用户请求；不要重复已经做过的工具调用。",
                }
            )
            continue

        return "（已达最大思考步数，请换个方式提问或发送 /reset 重置会话）"
