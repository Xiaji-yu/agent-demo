"""文字卡片图片发送：路由、门禁与三态发送纪律。

重点抓「改坏实现必然失败」：
- 私聊/群聊必须发到正确目标，消息段必须是 image
- 超过硬上限不得进入渲染（不是渲染完再拒绝）
- 渲染失败/超时/图片过大必须文本降级，内容不丢
- 超时/断连返回 IMAGE_UNCERTAIN 且**只发一次**（不能重发成两张）
"""

from __future__ import annotations

import base64
import time

import pytest

from agentcore.render.text_card import TextCardResult
from agentcore.skills.registry import SkillRegistry
from plugins.qq_agent_adapter import text_image as ti
from plugins.qq_agent_adapter.outbound import OutboundThrottle

pytest.importorskip("PIL")


def _throttle() -> OutboundThrottle:
    """测试用零间隔节流：生产默认 1s/会话，别让单测真的等。"""
    return OutboundThrottle(min_interval=0.0, global_min_interval=0.0)


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, object]] = []

    async def send_private_msg(self, user_id=0, message=None):
        self.calls.append(("private", user_id, message))

    async def send_group_msg(self, group_id=0, message=None):
        self.calls.append(("group", group_id, message))


@pytest.mark.asyncio
async def test_private_success_sends_png():
    from nonebot.adapters.onebot.v11 import MessageSegment

    bot = FakeBot()
    result = await ti.send_text_image(
        "123", "歌词\n第二行", title="歌名", bot=bot, throttle=_throttle()
    )

    assert result.startswith(ti.IMAGE_SEND_OK_PREFIX), result
    assert len(bot.calls) == 1
    kind, ident, msg = bot.calls[0]
    assert (kind, ident) == ("private", 123)
    assert isinstance(msg, MessageSegment)
    assert msg.type == "image"
    encoded = msg.data["file"].replace("base64://", "")
    assert base64.b64decode(encoded).startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_group_success_routes_to_group():
    bot = FakeBot()
    result = await ti.send_text_image(
        "123", "正文", group_id="456", bot=bot, throttle=_throttle()
    )
    assert result.startswith(ti.IMAGE_SEND_OK_PREFIX)
    assert bot.calls[0][0] == "group"
    assert bot.calls[0][1] == 456


@pytest.mark.asyncio
async def test_hard_limit_rejects_before_render(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("超过硬上限不得进入渲染")

    monkeypatch.setattr(ti, "render_text_card_png", boom)
    body = "字" * (ti.MAX_TEXT_CHARS_HARD + 1)
    result = await ti.send_text_image("123", body, bot=FakeBot())

    assert result.startswith("Error:")
    assert str(ti.MAX_TEXT_CHARS_HARD) in result


@pytest.mark.asyncio
async def test_render_failure_falls_back_to_text(monkeypatch):
    monkeypatch.setattr(
        ti,
        "render_text_card_png",
        lambda *a, **k: TextCardResult(None, reason="no_font"),
    )
    result = await ti.send_text_image("123", "要渲染的正文", bot=FakeBot())
    assert "没有可用的中文字体" in result
    assert "要渲染的正文" in result


@pytest.mark.asyncio
async def test_render_timeout_falls_back_to_text(monkeypatch):
    def slow(*args, **kwargs):
        time.sleep(0.3)
        return TextCardResult(b"x")

    monkeypatch.setattr(ti, "render_text_card_png", slow)
    monkeypatch.setattr(ti, "RENDER_TIMEOUT_S", 0.02)

    result = await ti.send_text_image("123", "正文", bot=FakeBot())
    assert "渲染超时" in result
    assert "正文" in result


@pytest.mark.asyncio
async def test_oversize_png_falls_back_to_text(monkeypatch):
    monkeypatch.setattr(
        ti,
        "render_text_card_png",
        lambda *a, **k: TextCardResult(b"x" * (ti.MAX_PNG_BYTES + 1)),
    )
    result = await ti.send_text_image("123", "正文", bot=FakeBot())
    assert "图片过大" in result
    assert "正文" in result


@pytest.mark.asyncio
async def test_uncertain_send_is_not_retried():
    class BoomBot:
        def __init__(self) -> None:
            self.calls = 0

        async def send_private_msg(self, user_id=0, message=None):
            self.calls += 1
            raise TimeoutError("websocket timed out")

    bot = BoomBot()
    result = await ti.send_text_image("123", "正文", bot=bot, throttle=_throttle())

    assert result.startswith(ti.IMAGE_SEND_UNCERTAIN_PREFIX), result
    assert bot.calls == 1, "结果未知时绝不能重发"
    assert "返回文本内容" not in result


@pytest.mark.asyncio
async def test_no_bot_falls_back_to_text(monkeypatch):
    monkeypatch.setattr(ti, "_first_bot", lambda: None)
    result = await ti.send_text_image("123", "正文")
    assert "没有已连接的 bot" in result
    assert "正文" in result


def test_skill_registered_as_public():
    registry = SkillRegistry()
    ti.register_text_image_skill(registry)
    skill = registry.skills["render_text_image"]
    assert skill.permission == "public"
    assert skill.params_schema["required"] == ["content"]


@pytest.mark.asyncio
async def test_registry_execute_injects_user_id(monkeypatch):
    """端到端接线：registry 把 user_id 注入 handler，图片真的发到该会话。"""
    from plugins.qq_agent_adapter import outbound

    bot = FakeBot()
    monkeypatch.setattr(ti, "_first_bot", lambda: bot)
    monkeypatch.setattr(outbound, "default_throttle", _throttle)

    registry = SkillRegistry()
    ti.register_text_image_skill(registry)
    result = await registry.execute(
        "render_text_image", user_id="123", content="端到端正文"
    )

    assert result.startswith(ti.IMAGE_SEND_OK_PREFIX), result
    assert bot.calls[0][0] == "private"
    assert bot.calls[0][1] == 123


# ---- REVIEW-1b16fa7..9c2d3c1 M16/L14/L15 回归 ----


@pytest.mark.asyncio
async def test_m16_long_title_is_truncated_not_o_n_squared(monkeypatch):
    """M16：title 超限必须截断——旧实现 title 直通渲染，_fit 逐字符收缩 O(n²)，
    12000 字 title 实测 27s 单核（6000 字已 6.9s 逼近 10s 超时），且线程无法
    被 wait_for 取消、僵尸线程继续满核跑完。public 技能 + 群聊一句话即可触发。
    """
    seen = {}

    def fake_render(text, title):
        seen["title"] = title
        return TextCardResult(b"\x89PNG-fake")

    monkeypatch.setattr(ti, "render_text_card_png", fake_render)
    result = await ti.send_text_image(
        "123", "正文", title="题" * 5000, bot=FakeBot(), throttle=_throttle()
    )
    assert len(seen["title"]) == ti.MAX_TITLE_CHARS
    assert "标题超" in result and "已截断" in result


@pytest.mark.asyncio
async def test_l14_render_has_concurrency_gate():
    """L14：渲染走专用信号量（不是裸 to_thread）——多会话同时渲染时排队受控，
    且 wait_for 的 10s 计时含排队时间，池满载不会无限堆积。"""
    import asyncio

    assert isinstance(ti._RENDER_SEMAPHORE, asyncio.Semaphore)
    assert ti._RENDER_SEMAPHORE._value <= 2


@pytest.mark.asyncio
async def test_l15_definite_failure_falls_back_to_text(monkeypatch):
    """L15：发送「确定失败」（非 uncertain）必须文本降级 + 错误串——
    旧实现该分支无用例（uncertain/no_bot/超时/过大都有，唯独它没有）。"""

    class BoomBot(FakeBot):
        async def send_private_msg(self, user_id=0, message=None):
            raise ValueError("send blew up")

    result = await ti.send_text_image(
        "123", "要发送的正文", bot=BoomBot(), throttle=_throttle()
    )
    assert "图片发送失败" in result
    assert "要发送的正文" in result  # 内容不丢


@pytest.mark.asyncio
async def test_m17_soft_limit_annotation_path(monkeypatch):
    """M17 的发送侧可见性：软上限截断时 IMAGE_OK 回执带「已截断」。"""
    monkeypatch.setattr(
        ti,
        "render_text_card_png",
        lambda *a, **k: TextCardResult(b"\x89PNG-fake", truncated=True, chars=7000),
    )
    result = await ti.send_text_image(
        "123", "正文", bot=FakeBot(), throttle=_throttle()
    )
    assert result.startswith(ti.IMAGE_SEND_OK_PREFIX)
    assert "已截断" in result
