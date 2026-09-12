"""agent-demo 启动入口（独立运行）"""

import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()

# 让 agentcore / 插件的 INFO 日志（LLM step、skill call、[msg]/[reply] 等）可见
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

# M7 日志归档：控制台之外按天落盘轮转（午夜切分、保留 N 天，过期自动清理）。
# 日志含聊天内容明文，data/logs/ 已 gitignore 绝不入库。
# M12：初始化必须容错——脏值或不可写目录只降级为"仅控制台"，不能让 bot 起不来。
from agentcore.logging_setup import setup_file_logging  # noqa: E402

setup_file_logging()

# NoneBot + pydantic v2 兼容：SUPERUSERS 期望 set[str]，
# 但纯数字 env var 会被推断为 int，这里提前转成 JSON 数组。
_raw_superusers = os.getenv("SUPERUSERS", "")
if _raw_superusers and not _raw_superusers.strip().startswith("["):
    _list = [x.strip() for x in _raw_superusers.split(",") if x.strip()]
    os.environ["SUPERUSERS"] = json.dumps(_list)

from nonebot import get_driver, init  # noqa: E402
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter  # noqa: E402

init()

driver = get_driver()
driver.register_adapter(OneBotV11Adapter)


@driver.on_shutdown
async def _close_agent():
    # H5：flush 必须早于 memory.aclose()——NoneBot 停机钩子按注册顺序逆序执行，
    # 顺序不能依赖注册先后，故统一交给 lifecycle.shutdown_agent 固定
    # （插件侧钩子做同样收尾，两者幂等）。
    from plugins.qq_agent_adapter.lifecycle import shutdown_agent

    deb = None
    try:
        import plugins.qq_agent_adapter.matcher as _matcher

        deb = _matcher.get_debouncer()
    except Exception:
        logging.getLogger(__name__).exception("get debouncer for shutdown failed")

    async def _close_shared_llm() -> None:
        # P1-6：回收共享 LLM httpx 连接池，避免反复启停/热重载累积未关闭连接
        from agentcore.skills.registry import close_shared_llm_client

        await close_shared_llm_client()

    async def _close_search() -> None:
        # L21：回收 search 技能的常驻 httpx 连接池（与 P1-6 同型的停机收尾）
        from agentcore.skills.search import aclose_search_client

        await aclose_search_client()

    await shutdown_agent(
        debouncer=deb,
        memory=getattr(driver, "_agent_memory", None),
        extra_closers=(_close_shared_llm, _close_search),
    )


from nonebot import load_plugins  # noqa: E402

load_plugins("plugins")

if __name__ == "__main__":
    import nonebot

    nonebot.run()
