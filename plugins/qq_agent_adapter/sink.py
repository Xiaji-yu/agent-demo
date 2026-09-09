from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Message


class Sink:
    """主动推送消息（定时任务、工具通知等）"""

    def __init__(self):
        self.bot = get_bot()

    async def send_group(self, group_id, message: str):
        await self.bot.send_group_msg(group_id=int(group_id), message=Message(message))

    async def send_private(self, user_id, message: str):
        await self.bot.send_private_msg(user_id=int(user_id), message=Message(message))
