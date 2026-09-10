"""出站投递：阈值分层 / 合并转发降级 / 节流。

时钟与 sleep 全部注入，测试不真的等待。
"""
import pytest

from plugins.qq_agent_adapter import outbound
from plugins.qq_agent_adapter.outbound import (
    MODE_CHUNKED,
    MODE_FORWARD,
    MODE_SINGLE,
    OutboundThrottle,
    deliver_reply,
    forward_max,
    single_max,
    split_message,
)


class FakeTime:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class FakeBot:
    self_id = "10001"

    def __init__(self, forward_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.forward_error = forward_error

    async def send_group_msg(self, **kwargs):
        self.calls.append(("send_group_msg", kwargs))

    async def send_private_msg(self, **kwargs):
        self.calls.append(("send_private_msg", kwargs))

    async def call_api(self, api: str, **kwargs):
        self.calls.append((api, kwargs))
        if self.forward_error is not None:
            raise self.forward_error
        return {"status": "ok"}

    def apis(self) -> list[str]:
        return [name for name, _ in self.calls]


def no_wait_throttle() -> OutboundThrottle:
    """路由用：不引入任何等待。"""
    return OutboundThrottle(min_interval=0.0, global_min_interval=0.0, per_window=0)


def long_text(chars: int) -> str:
    """构造长度确定、且天然切成多块的文本（每段 20 字 + 句号）。"""
    unit = "第一段内容需要足够长。"
    out = ""
    while len(out) < chars:
        out += unit
    return out


# ---------------------------------------------------------------------------
# 阈值与切分
# ---------------------------------------------------------------------------
class TestThresholds:
    def test_defaults(self, monkeypatch):
        for key in (
            "AGENT_REPLY_SINGLE_MAX",
            "AGENT_REPLY_FORWARD_MAX",
            "AGENT_REPLY_FORWARD_MAX_NODES",
            "AGENT_REPLY_FORWARD",
        ):
            monkeypatch.delenv(key, raising=False)
        assert single_max() == 1500
        assert forward_max() == 4500

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "800")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "2000")
        assert single_max() == 800
        assert forward_max() == 2000

    def test_invalid_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "abc")
        assert single_max() == 1500
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "10")  # 低于下限
        assert single_max() == 1500

    def test_forward_max_never_below_single_max(self, monkeypatch):
        # 配成反向（转发阈值 < 单条阈值）时不能自相矛盾
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "3000")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "1000")
        assert forward_max() >= single_max()

    def test_split_matches_legacy_behaviour(self):
        assert split_message("") == []
        assert split_message("hello") == ["hello"]
        assert len(split_message("第一句。第二句。第三句。", 10)) > 1


# ---------------------------------------------------------------------------
# 分层投递
# ---------------------------------------------------------------------------
class TestDeliverReply:
    @pytest.mark.asyncio
    async def test_short_reply_is_single_message(self):
        bot = FakeBot()
        mode = await deliver_reply(
            bot, kind="group", ident=1, text="短回复", throttle=no_wait_throttle()
        )
        assert mode == MODE_SINGLE
        assert bot.apis() == ["send_group_msg"]

    @pytest.mark.asyncio
    async def test_medium_reply_uses_forward(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "5000")
        bot = FakeBot()
        mode = await deliver_reply(
            bot,
            kind="group",
            ident=77,
            text=long_text(400),
            self_id="10001",
            nickname="助手",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_FORWARD
        assert bot.apis() == ["send_group_forward_msg"]
        _, kwargs = bot.calls[0]
        assert kwargs["group_id"] == 77
        nodes = kwargs["messages"]
        assert len(nodes) > 1
        # node 段必须标 bot 自己的身份（伪造他人是风控点）
        assert all(node.type == "node" for node in nodes)
        assert all(node.data["user_id"] == "10001" for node in nodes)
        assert all(node.data["nickname"] == "助手" for node in nodes)

    @pytest.mark.asyncio
    async def test_private_uses_private_forward_api(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        bot = FakeBot()
        mode = await deliver_reply(
            bot,
            kind="private",
            ident=555,
            text=long_text(400),
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_FORWARD
        assert bot.apis() == ["send_private_forward_msg"]
        assert bot.calls[0][1]["user_id"] == 555

    @pytest.mark.asyncio
    async def test_forward_failure_falls_back_to_chunked(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        bot = FakeBot(forward_error=RuntimeError("action not supported"))
        text = long_text(400)
        mode = await deliver_reply(
            bot,
            kind="group",
            ident=1,
            text=text,
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_CHUNKED
        # 两个转发 API 都试过，然后逐条补发，内容不丢
        assert bot.apis()[:2] == ["send_group_forward_msg", "send_forward_msg"]
        sent = [kw["message"] for name, kw in bot.calls if name == "send_group_msg"]
        assert len(sent) > 1
        assert "".join(sent).replace("\n", "") == text.replace("\n", "")

    @pytest.mark.asyncio
    async def test_forward_disabled_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD", "0")
        bot = FakeBot()
        mode = await deliver_reply(
            bot,
            kind="group",
            ident=1,
            text=long_text(400),
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_CHUNKED
        assert "send_group_forward_msg" not in bot.apis()

    @pytest.mark.asyncio
    async def test_missing_self_id_falls_back(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        bot = FakeBot()
        mode = await deliver_reply(
            bot, kind="group", ident=1, text=long_text(400), self_id="", throttle=no_wait_throttle()
        )
        assert mode == MODE_CHUNKED
        assert bot.apis() == ["send_group_msg"] * len(bot.calls)

    @pytest.mark.asyncio
    async def test_too_many_nodes_falls_back(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX_NODES", "2")
        bot = FakeBot()
        mode = await deliver_reply(
            bot,
            kind="group",
            ident=1,
            text=long_text(1000),
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_CHUNKED
        assert "send_group_forward_msg" not in bot.apis()

    @pytest.mark.asyncio
    async def test_very_long_private_also_sends_file(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "200")

        sent: list[tuple] = []

        async def fake_send(user_id, content, filename="report.md"):
            sent.append((user_id, len(content), filename))
            return "FILE_OK: 已发送"

        monkeypatch.setattr("agentcore.skills.file_sender.send_markdown_file", fake_send)
        bot = FakeBot()
        text = long_text(500)
        mode = await deliver_reply(
            bot,
            kind="private",
            ident=9,
            text=text,
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_FORWARD + "+file"
        assert sent and sent[0][0] == "9"

    @pytest.mark.asyncio
    async def test_very_long_group_has_no_file(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "200")
        bot = FakeBot()
        mode = await deliver_reply(
            bot,
            kind="group",
            ident=1,
            text=long_text(500),
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_FORWARD

    @pytest.mark.asyncio
    async def test_file_failure_does_not_break_delivery(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "200")

        async def boom(*args, **kwargs):
            raise RuntimeError("upload failed")

        monkeypatch.setattr("agentcore.skills.file_sender.send_markdown_file", boom)
        bot = FakeBot()
        mode = await deliver_reply(
            bot,
            kind="private",
            ident=9,
            text=long_text(500),
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_FORWARD  # 文本已送达，只是没有附件
        assert bot.apis() == ["send_private_forward_msg"]

    @pytest.mark.asyncio
    async def test_default_throttle_used_when_none(self, monkeypatch):
        # 不传 throttle 时必须走共享实例，否则节流形同虚设
        monkeypatch.setattr(
            outbound, "_default_throttle", OutboundThrottle(min_interval=0, global_min_interval=0)
        )
        bot = FakeBot()
        await deliver_reply(bot, kind="group", ident=1, text="hi")
        assert bot.apis() == ["send_group_msg"]


# ---------------------------------------------------------------------------
# 节流
# ---------------------------------------------------------------------------
class TestOutboundThrottle:
    @pytest.mark.asyncio
    async def test_first_send_waits_nothing(self):
        ft = FakeTime()
        th = OutboundThrottle(clock=ft.clock, sleep=ft.sleep)
        assert await th.acquire("group:1") == 0.0
        assert ft.slept == []

    @pytest.mark.asyncio
    async def test_same_target_spaced_by_min_interval(self):
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=1.0, global_min_interval=0.0, per_window=0, clock=ft.clock, sleep=ft.sleep
        )
        await th.acquire("group:1")
        waited = await th.acquire("group:1")
        assert waited == pytest.approx(1.0)
        assert ft.slept == [pytest.approx(1.0)]

    @pytest.mark.asyncio
    async def test_different_targets_share_global_interval(self):
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=0.0, global_min_interval=0.5, per_window=0, clock=ft.clock, sleep=ft.sleep
        )
        await th.acquire("group:1")
        waited = await th.acquire("group:2")
        assert waited == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_per_window_cap_delays_next(self):
        ft = FakeTime()
        # max_wait 放宽，单独验证「窗口条数上限」本身会要求等待
        th = OutboundThrottle(
            min_interval=0.0,
            global_min_interval=0.0,
            per_window=2,
            window=60.0,
            max_wait=120.0,
            clock=ft.clock,
            sleep=ft.sleep,
        )
        await th.acquire("group:1")
        await th.acquire("group:1")
        waited = await th.acquire("group:1")
        assert waited == pytest.approx(60.0)

    @pytest.mark.asyncio
    async def test_per_window_cap_is_soft_under_default_max_wait(self):
        """默认 max_wait=10 < 窗口 60：超限时只等软上限就放行，不能卡死用户。"""
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=0.0,
            global_min_interval=0.0,
            per_window=1,
            window=60.0,
            max_wait=10.0,
            clock=ft.clock,
            sleep=ft.sleep,
        )
        await th.acquire("group:1")
        assert await th.acquire("group:1") == 0.0

    @pytest.mark.asyncio
    async def test_window_slides(self):
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=0.0,
            global_min_interval=0.0,
            per_window=1,
            window=10.0,
            clock=ft.clock,
            sleep=ft.sleep,
        )
        await th.acquire("group:1")
        ft.t += 10.0  # 窗口自然滚过
        assert await th.acquire("group:1") == 0.0

    @pytest.mark.asyncio
    async def test_max_wait_is_soft_cap(self):
        """时钟不前进（极端/时钟异常）时也必须有限返回，不能死等。"""
        th = OutboundThrottle(
            min_interval=5.0,
            global_min_interval=0.0,
            per_window=0,
            max_wait=2.0,
            clock=lambda: 1000.0,
            sleep=_never_advancing_sleep,
        )
        await th.acquire("group:1")
        waited = await th.acquire("group:1")
        assert waited <= 5.0

    @pytest.mark.asyncio
    async def test_wait_for_does_not_record(self):
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=1.0, global_min_interval=0.0, per_window=0, clock=ft.clock, sleep=ft.sleep
        )
        assert th.wait_for("group:1") == 0.0
        assert th.wait_for("group:1") == 0.0  # 未取额度，不产生间隔
        await th.acquire("group:1")
        assert th.wait_for("group:1") == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_concurrent_acquires_are_serialized(self):
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=1.0, global_min_interval=0.0, per_window=0, clock=ft.clock, sleep=ft.sleep
        )
        import asyncio

        results = await asyncio.gather(*(th.acquire("group:1") for _ in range(3)))
        # 返回值是「本次自身等待」，累计等待应为 2s（三次发送落在 t=1000/1001/1002）
        assert sum(results) == pytest.approx(2.0)
        assert ft.t == pytest.approx(1002.0)

    def test_reset_clears_state(self):
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=1.0, global_min_interval=0.0, per_window=0, clock=ft.clock, sleep=ft.sleep
        )
        th._bucket("group:1").record(ft.t)
        th.reset()
        assert th.wait_for("group:1") == 0.0


async def _never_advancing_sleep(seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# Sink 共用节流
# ---------------------------------------------------------------------------
class TestSinkThrottle:
    @pytest.mark.asyncio
    async def test_sink_acquires_before_send(self, monkeypatch):
        from plugins.qq_agent_adapter.sink import Sink

        acquired: list[str] = []

        class RecordingThrottle:
            async def acquire(self, target):
                acquired.append(target)
                return 0.0

        class Bot:
            async def send_group_msg(self, **kwargs):
                return None

        class Driver:
            bots = {"10001": Bot()}

        sink = Sink(throttle=RecordingThrottle())
        monkeypatch.setattr(sink, "_bots", lambda: [Bot()])
        assert await sink.send("group:777", "提醒") is True
        assert acquired == ["group:777"]

    @pytest.mark.asyncio
    async def test_sink_falls_back_to_default_throttle(self):
        from plugins.qq_agent_adapter.sink import Sink

        sink = Sink()
        assert sink._throttle_obj() is outbound.default_throttle()
