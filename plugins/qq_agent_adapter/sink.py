from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import ActionFailed


class Sink:
    """主动推送消息（定时任务、工具通知等）"""

    def __init__(self):
        self._bot = None

    def _get_bot(self):
        try:
            return get_bot()
        except Exception:
            return None

    async def send_group(self, group_id, message: str):
        bot = self._get_bot()
        if not bot:
            return
        try:
            await bot.send_group_msg(group_id=int(group_id), message=Message(message))
        except ActionFailed:
            pass

    async def send_private(self, user_id, message: str):
        bot = self._get_bot()
        if not bot:
            return
        try:
            await bot.send_private_msg(user_id=int(user_id), message=Message(message))
        except ActionFailed:
            pass
