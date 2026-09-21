"""当前模型信息：让模型能准确回答"你在用哪个模型"。

为什么不放在人格/prompt 里写死型号：模型对自己的型号没有可靠认知（会按训练
知识自称），而主备切换又是**运行期动态**的——prompt 里写死的型号在一次降级后
就成了假信息。所以这个问题由 skill 在**被问到时**向 LLMClient 要准信。

线路状态（主/备）取自觉出时的客户端实例，与主备切换通知（on_fallback）同一
口径：只含模型名，**不含** api_key / base_url——返回值会经模型转述给用户，
而群聊里任何人都可能问这个问题。
"""

from __future__ import annotations

import logging

from agentcore.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


def register_llm_info_skills(registry: SkillRegistry, llm) -> None:
    """注册 ``current_model`` skill。

    ``llm`` 由宿主注入（与 ``register_reminder_skills(registry, memory, sink)``
    同一依赖注入模式）：agentcore 不认识进程级单例的获取时机，注入也让测试
    能传替身。
    """

    @registry.register(
        "current_model",
        "当用户询问你当前使用哪个 AI 模型、哪个接口/线路、是不是在用什么备用模型时"
        "（如「你是什么模型」「用的什么接口」「现在是谁在回答」），必须调用本 skill "
        "获取准确信息。不要根据自己的训练知识猜测或自称某个具体产品——你的真实型号"
        "只以本 skill 的返回为准。用户没问起时不要主动调用。",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
        permission="public",
    )
    async def current_model_skill() -> str:
        try:
            status = llm.model_status()
        except Exception:
            logger.warning("current_model: 读取模型状态失败", exc_info=True)
            return "Error: 读取当前模型状态失败，请稍后再试。"
        active = status.get("active") or "未知"
        if status.get("line") == "fallback":
            return (
                f"当前生效模型：{active}（备用线路；主模型 "
                f"{status.get('primary')} 当前不可用，已自动切换）。"
            )
        text = f"当前生效模型：{active}（主线路）。"
        if status.get("fallback"):
            text += f"配置的备用模型：{status['fallback']}（主线路不通时自动切换）。"
        else:
            text += "未配置备用模型。"
        return text
