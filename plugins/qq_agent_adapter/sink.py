"""主动推送：定时提醒等后台任务往指定会话发消息。

与 matcher 里的回复不同，这里没有「当前事件」，因此需要自己找 bot：
遍历所有已连接 bot 逐个尝试（多账号时可都试一遍）。返回值明确表示是否送达，
调用方据此决定重试还是标记完成——旧实现在失败时静默 pass，提醒会无声丢失。

出站节流与 matcher 回复**共用同一个进程级限流器**（``outbound.default_throttle``）：
否则「回复 + 多条提醒同时到点」会各自计数，合计仍是瞬时高频。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class Sink:
    """主动推送消息（定时提醒、工具通知等）。"""

    def __init__(self, throttle=None) -> None:
        self._throttle = throttle

    def _throttle_obj(self):
        if self._throttle is None:
            from .outbound import default_throttle

            self._throttle = default_throttle()
        return self._throttle

    def _bots(self) -> list:
        try:
            from nonebot import get_driver

            driver = get_driver()
        except Exception:
            return []
        return list(getattr(driver, "bots", {}).values())

    async def send(self, target: str, message: str) -> bool:
        """target 形如 `private:123456` 或 `group:789012`。返回是否送达。"""
        kind, _, ident = (target or "").partition(":")
        try:
            ident = int(ident)
        except (TypeError, ValueError):
            logger.warning("sink: bad target %r", target)
            return False
        if kind not in ("private", "group"):
            logger.warning("sink: unknown target kind %r", kind)
            return False

        bots = self._bots()
        if not bots:
            logger.warning("sink: no bot connected, cannot deliver to %s", target)
            return False
        throttle = self._throttle_obj()
        # 额度按 **target** 计一次，不能放在 bot 循环里：换 bot 重试只是同一逻辑投递的
        # 内部细节，重复取额度会让一条消息吃掉 2 份配额并白等一个 min_interval。
        await throttle.acquire(target)
        last_error = None
        for bot in bots:
            try:
                if kind == "group":
                    await bot.send_group_msg(group_id=ident, message=message)
                else:
                    await bot.send_private_msg(user_id=ident, message=message)
                logger.info("sink: delivered to %s (%d chars)", target, len(message))
                return True
            except Exception as e:  # 换下一个 bot 再试
                last_error = e
                continue
        logger.warning("sink: delivery to %s failed: %s", target, last_error)
        return False

    # 兼容旧调用方式
    async def send_group(self, group_id, message: str) -> bool:
        return await self.send(f"group:{group_id}", message)

    async def send_private(self, user_id, message: str) -> bool:
        return await self.send(f"private:{user_id}", message)
