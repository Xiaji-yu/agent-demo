"""agent-demo 启动入口（独立运行）"""
import os
from dotenv import load_dotenv

load_dotenv()

from nonebot import init, get_driver
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

init()

driver = get_driver()
driver.register_adapter(OneBotV11Adapter)

from nonebot import load_plugins

load_plugins("plugins")

if __name__ == "__main__":
    import nonebot

    nonebot.run()
