"""agent-demo 启动入口（独立运行）"""
import json
import os
from dotenv import load_dotenv

load_dotenv()

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
    memory = getattr(driver, "_agent_memory", None)
    if memory is not None:
        await memory.aclose()


from nonebot import load_plugins

load_plugins("plugins")

if __name__ == "__main__":
    import nonebot

    nonebot.run()
