"""agent-demo 启动入口（独立运行）"""
import json
import logging
import os
from dotenv import load_dotenv

load_dotenv()

# 让 agentcore / 插件的 INFO 日志（LLM step、skill call、[msg]/[reply] 等）可见
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

# NoneBot + pydantic v2 兼容：SUPERUSERS 期望 set[str]，
# 但纯数字 env var 会被推断为 int，这里提前转成 JSON 数组。
_raw_superusers = os.getenv("SUPERUSERS", "")
if _raw_superusers and not _raw_superusers.strip().startswith("["):
    _list = [x.strip() for x in _raw_superusers.split(",") if x.strip()]
    os.environ["SUPERUSERS"] = json.dumps(_list)

from nonebot import init, get_driver
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

init()

driver = get_driver()
driver.register_adapter(OneBotV11Adapter)


@driver.on_shutdown
async def _close_agent():
    # 停机前把防抖窗口中未到期的消息立即执行，避免静默丢失
    try:
        import plugins.qq_agent_adapter.matcher as _matcher

        deb = _matcher.get_debouncer()
        if deb is not None:
            await deb.flush_all()
    except Exception:
        logging.getLogger(__name__).exception("debounce flush on shutdown failed")
    memory = getattr(driver, "_agent_memory", None)
    if memory is not None:
        await memory.aclose()


from nonebot import load_plugins

load_plugins("plugins")

if __name__ == "__main__":
    import nonebot

    nonebot.run()
