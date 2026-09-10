"""出站投递：阈值分层 / 合并转发降级 / 节流。

时钟与 sleep 全部注入，测试不真的等待。

约定：
- 凡是「不该发文件」的用例，都要把文件发送 stub 成**成功**再断言未被调用——
  否则真实实现在测试环境必然失败，用例会因为环境巧合而通过（假阳性）。
"""
import asyncio

import pytest

from plugins.qq_agent_adapter import outbound
from plugins.qq_agent_adapter.outbound import (
    MODE_CHUNKED,
    MODE_FORWARD,
    MODE_SINGLE,
    MODE_UNCONFIRMED,
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

    def __init__(self, fail_apis: dict[str, Exception] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.delivered: list[tuple[str, object]] = []
        self.fail_apis = fail_apis or {}
        self.fail_send_at: int | None = None
        self._send_count = 0

    async def send_group_msg(self, **kwargs):
        self._send_count += 1
        if self.fail_send_at is not None and self._send_count == self.fail_send_at:
            raise RuntimeError(f"send failed at chunk {self._send_count}")
        self.calls.append(("send_group_msg", kwargs))
        self.delivered.append(("text", kwargs["message"]))

    async def send_private_msg(self, **kwargs):
        self._send_count += 1
        if self.fail_send_at is not None and self._send_count == self.fail_send_at:
            raise RuntimeError(f"send failed at chunk {self._send_count}")
        self.calls.append(("send_private_msg", kwargs))
        self.delivered.append(("text", kwargs["message"]))

    async def call_api(self, api: str, **kwargs):
        if api in self.fail_apis:
            raise self.fail_apis[api]
        self.calls.append((api, kwargs))
        self.delivered.append((api, kwargs.get("messages")))
        return {"status": "ok"}

    def apis(self) -> list[str]:
        return [name for name, _ in self.calls]


def no_wait_throttle() -> OutboundThrottle:
    """路由用：不引入任何等待。"""
    return OutboundThrottle(min_interval=0.0, global_min_interval=0.0, per_window=0)


def long_text(chars: int) -> str:
    """构造长度确定、每段内容**可区分**的多块文本。

    分段必须可区分：否则「倒序发送」「重复发送同一块」这类变异检测不到。
    """
    parts: list[str] = []
    total = 0
    i = 0
    while total < chars:
        unit = f"第{i:03d}段内容需要足够长才能切分。"
        parts.append(unit)
        total += len(unit)
        i += 1
    return "".join(parts)


def stub_file_send(monkeypatch, calls: list | None = None, *, ok: bool = True, raises=None):
    """把文件发送替换成可控实现，返回记录用的 list。"""
    sent: list = calls if calls is not None else []

    async def fake(user_id, content, filename="report.md", *, bot=None):
        sent.append({"user_id": user_id, "len": len(content), "filename": filename, "bot": bot})
        if raises is not None:
            raise raises
        return "FILE_OK: 已发送" if ok else "[文件发送失败，返回文本内容]..."

    monkeypatch.setattr("agentcore.skills.file_sender.send_markdown_file", fake)
    return sent


def nodes_of(bot: FakeBot) -> list:
    for name, kwargs in bot.calls:
        if name.endswith("forward_msg"):
            return kwargs["messages"]
    return []


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
        # 配成反向（转发阈值 < 单条阈值）时不能自相矛盾，恰好抬到单条阈值
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "3000")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "1000")
        assert forward_max() == 3000


class TestSplitMessage:
    def test_basics(self):
        assert split_message("") == []
        assert split_message("hello") == ["hello"]
        assert len(split_message("第一句。第二句。第三句。", 10)) > 1

    def test_skips_whitespace_only_chunks(self):
        chunks = split_message("w" + " " * 500 + "w", 100)
        assert chunks
        assert all(c.strip() for c in chunks)

    def test_keeps_newlines_in_chunk(self):
        text = "第一行\n第二行\n第三行"
        assert split_message(text, 50) == [text]

    def test_cuts_at_punct_not_mid_word(self):
        seg = "**年柱**：辛巳（白蜡金），**月柱**：丙申（山下火）。" * 8
        chunks = split_message(seg, max_len=40)
        assert len(chunks) > 1
        assert "".join(chunks).replace(" ", "") == seg.replace(" ", "")
        for c in chunks[:-1]:
            assert c[-1] in "。，；、！？：\n" or c.endswith("）")

    def test_preserves_code_indent_and_blank_lines(self):
        """评审发现：逐行 strip 会把长回复里的代码块压平（缩进与空行全丢）。"""
        code = "下面是一段修复建议。\n\n```python\ndef f(x):\n    if x:\n        return 1\n    return 0\n```\n\n"
        text = code * 60
        chunks = split_message(text, 1500)
        assert len(chunks) > 1
        joined = "".join(chunks)
        assert "\n    if x:" in joined or joined.count("    if x:") > 0
        assert "        return 1" in joined
        assert "\n\n" in joined  # 空行保留

    def test_no_non_whitespace_loss(self):
        text = ("甲。" * 700) + "\n" + ("乙" * 900)
        chunks = split_message(text, 400)
        # 分块首尾空白会被裁掉，非空白字符必须一个不少、顺序不变
        assert "".join(chunks).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


# ---------------------------------------------------------------------------
# 分层投递
# ---------------------------------------------------------------------------
class TestDeliverReplyRouting:
    @pytest.mark.asyncio
    async def test_short_group_reply_is_single_message(self):
        bot = FakeBot()
        mode = await deliver_reply(
            bot, kind="group", ident=1, text="短回复", throttle=no_wait_throttle()
        )
        assert mode == MODE_SINGLE
        assert bot.apis() == ["send_group_msg"]
        assert bot.calls[0][1]["group_id"] == 1

    @pytest.mark.asyncio
    async def test_short_private_reply_uses_private_api(self):
        """私聊单条分支此前零覆盖：把它误改成群发也不会被发现。"""
        bot = FakeBot()
        mode = await deliver_reply(
            bot, kind="private", ident=9, text="短回复", throttle=no_wait_throttle()
        )
        assert mode == MODE_SINGLE
        assert bot.apis() == ["send_private_msg"]
        assert bot.calls[0][1]["user_id"] == 9

    @pytest.mark.asyncio
    async def test_medium_reply_uses_forward(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "5000")
        bot = FakeBot()
        text = long_text(400)
        mode = await deliver_reply(
            bot,
            kind="group",
            ident=77,
            text=text,
            self_id="10001",
            nickname="助手",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_FORWARD
        assert bot.apis() == ["send_group_forward_msg"]
        assert bot.calls[0][1]["group_id"] == 77

        nodes = nodes_of(bot)
        assert len(nodes) > 1
        assert all(node.type == "node" for node in nodes)
        assert all(node.data["user_id"] == "10001" for node in nodes)
        assert all(node.data["nickname"] == "助手" for node in nodes)
        # 内容必须完整：否则「把整条回复发成空消息」这种 bug 没人能发现
        assert "".join(n.data["content"] for n in nodes).replace("\n", "") == text.replace("\n", "")

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
    async def test_compat_fallback_api_is_used_and_correctly_shaped(self, monkeypatch):
        """第一个 API 不被支持时，第二跳要真的发出去，且请求体正确。"""
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        bot = FakeBot(fail_apis={"send_group_forward_msg": RuntimeError("unsupported action")})
        mode = await deliver_reply(
            bot,
            kind="group",
            ident=42,
            text=long_text(400),
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_FORWARD
        assert bot.apis() == ["send_forward_msg"]
        kwargs = bot.calls[0][1]
        assert kwargs["message_type"] == "group"
        assert kwargs["group_id"] == 42
        assert len(kwargs["messages"]) > 1

    @pytest.mark.asyncio
    async def test_compat_fallback_private_shape(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        bot = FakeBot(fail_apis={"send_private_forward_msg": RuntimeError("unsupported action")})
        mode = await deliver_reply(
            bot,
            kind="private",
            ident=7,
            text=long_text(400),
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_FORWARD
        kwargs = bot.calls[0][1]
        assert kwargs["message_type"] == "private"
        assert kwargs["user_id"] == 7


class TestDeliverReplyFallback:
    @pytest.mark.asyncio
    async def test_forward_failure_falls_back_to_chunked(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        bot = FakeBot(
            fail_apis={
                "send_group_forward_msg": RuntimeError("nope"),
                "send_forward_msg": RuntimeError("nope"),
            }
        )
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
        sent = [kw["message"] for name, kw in bot.calls if name == "send_group_msg"]
        assert len(sent) == len(split_message(text, 100))  # 精确条数，不用自证式断言
        assert "".join(sent).replace("\n", "") == text.replace("\n", "")

    @pytest.mark.asyncio
    async def test_uncertain_timeout_does_not_resend(self, monkeypatch):
        """超时可能「已送达但响应丢了」：此时重发就是重复刷屏。"""
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        bot = FakeBot(
            fail_apis={
                "send_group_forward_msg": TimeoutError("ws timeout after send"),
                "send_forward_msg": TimeoutError("ws timeout after send"),
            }
        )
        mode = await deliver_reply(
            bot,
            kind="group",
            ident=1,
            text=long_text(400),
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_UNCONFIRMED
        # 只尝试过一次转发，且没有任何逐条补发
        assert bot.apis() == []
        assert not [n for n, _ in bot.calls if n == "send_group_msg"]

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
        text = long_text(400)
        mode = await deliver_reply(
            bot,
            kind="group",
            ident=1,
            text=text,
            self_id="",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_CHUNKED
        assert bot.apis() == ["send_group_msg"] * len(split_message(text, 100))

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
    async def test_single_chunk_failure_keeps_sending_rest(self, monkeypatch):
        """逐条降级时单块失败不能中断，否则用户只看到一条报错。"""
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD", "0")
        text = long_text(400)
        expected = len(split_message(text, 100))
        bot = FakeBot()
        bot.fail_send_at = 2
        mode = await deliver_reply(
            bot,
            kind="group",
            ident=1,
            text=text,
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_CHUNKED
        assert len(bot.delivered) == expected - 1  # 只少了失败的那一块


class TestDeliverReplyFile:
    @pytest.mark.asyncio
    async def test_very_long_private_also_sends_file(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "200")
        text = long_text(500)
        sent = stub_file_send(monkeypatch)
        bot = FakeBot()
        mode = await deliver_reply(
            bot,
            kind="private",
            ident=9,
            text=text,
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_FORWARD + "+file"
        assert len(sent) == 1
        assert sent[0]["user_id"] == "9"
        assert sent[0]["len"] == len(text)  # 附件必须是完整原文

    @pytest.mark.asyncio
    async def test_very_long_group_never_sends_file(self, monkeypatch):
        """私聊护栏：文件把内容发到群里是隐私/风控事故。文件发送要 stub 成功，
        否则真实实现必然失败，用例会因环境巧合而通过（评审发现的假阳性）。"""
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "200")
        sent = stub_file_send(monkeypatch)  # 关键：stub 成「会成功」
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
        assert sent == []  # 一次都不能调用

    @pytest.mark.asyncio
    async def test_chunked_fallback_does_not_also_send_file(self, monkeypatch):
        """降级成逐条后再发文件 = 同一内容发两遍（评审发现）。"""
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "200")
        sent = stub_file_send(monkeypatch)
        bot = FakeBot(
            fail_apis={
                "send_private_forward_msg": RuntimeError("unsupported"),
                "send_forward_msg": RuntimeError("unsupported"),
            }
        )
        mode = await deliver_reply(
            bot,
            kind="private",
            ident=9,
            text=long_text(500),
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_CHUNKED
        assert sent == []

    @pytest.mark.asyncio
    async def test_forward_disabled_does_not_send_file(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "200")
        monkeypatch.setenv("AGENT_REPLY_FORWARD", "0")
        sent = stub_file_send(monkeypatch)
        bot = FakeBot()
        mode = await deliver_reply(
            bot,
            kind="private",
            ident=9,
            text=long_text(500),
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert mode == MODE_CHUNKED
        assert sent == []

    @pytest.mark.asyncio
    async def test_file_failure_does_not_break_delivery(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "200")
        stub_file_send(monkeypatch, raises=RuntimeError("upload failed"))
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
    async def test_file_send_receives_the_triggering_bot(self, monkeypatch):
        """多账号：附件必须由触发本次回复的 bot 发出，不能取 driver.bots 第一个。"""
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "200")
        sent = stub_file_send(monkeypatch)
        bot = FakeBot()
        await deliver_reply(
            bot,
            kind="private",
            ident=9,
            text=long_text(500),
            self_id="10001",
            throttle=no_wait_throttle(),
        )
        assert sent and sent[0]["bot"] is bot


class TestDeliverReplyThrottleUse:
    @pytest.mark.asyncio
    async def test_default_throttle_instance_is_shared(self, monkeypatch):
        """回复与主动推送必须共用同一实例，否则节流形同虚设。"""
        shared = OutboundThrottle(min_interval=0, global_min_interval=0)
        monkeypatch.setattr(outbound, "_default_throttle", shared)
        acquired: list[str] = []
        original = shared.acquire

        async def spy(target):
            acquired.append(target)
            return await original(target)

        monkeypatch.setattr(shared, "acquire", spy)
        bot = FakeBot()
        await deliver_reply(bot, kind="group", ident=1, text="hi")
        assert acquired == ["group:1"]

    @pytest.mark.asyncio
    async def test_file_send_also_passes_throttle(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        monkeypatch.setenv("AGENT_REPLY_FORWARD_MAX", "200")
        stub_file_send(monkeypatch)
        th = OutboundThrottle(min_interval=0, global_min_interval=0)
        acquired: list[str] = []
        original = th.acquire

        async def spy(target):
            acquired.append(target)
            return await original(target)

        monkeypatch.setattr(th, "acquire", spy)
        bot = FakeBot()
        await deliver_reply(
            bot, kind="private", ident=9, text=long_text(500), self_id="10001", throttle=th
        )
        # 转发一次 + 文件一次，都要经过节流器
        assert acquired == ["private:9", "private:9"]

    @pytest.mark.asyncio
    async def test_forward_acquires_quota_once_even_if_first_api_fails(self, monkeypatch):
        """备用 API 只是同一份消息的另一种发法，不能重复记账（评审 M2）。"""
        monkeypatch.setenv("AGENT_REPLY_SINGLE_MAX", "100")
        th = OutboundThrottle(min_interval=0, global_min_interval=0)
        acquired: list[str] = []
        original = th.acquire

        async def spy(target):
            acquired.append(target)
            return await original(target)

        monkeypatch.setattr(th, "acquire", spy)
        bot = FakeBot(fail_apis={"send_group_forward_msg": RuntimeError("unsupported")})
        mode = await deliver_reply(
            bot, kind="group", ident=42, text=long_text(400), self_id="10001", throttle=th
        )
        assert mode == MODE_FORWARD
        assert acquired == ["group:42"]


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
        assert await th.acquire("group:1") == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_different_targets_share_global_interval(self):
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=0.0, global_min_interval=0.5, per_window=0, clock=ft.clock, sleep=ft.sleep
        )
        await th.acquire("group:1")
        assert await th.acquire("group:2") == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_per_window_cap_delays_next(self):
        ft = FakeTime()
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
        assert await th.acquire("group:1") == pytest.approx(60.0)

    @pytest.mark.asyncio
    async def test_per_window_cap_is_soft_under_default_max_wait(self):
        """窗口上限是软的：最多等 max_wait 就放行（但最小间隔仍然生效，见下一个用例）。"""
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
        assert await th.acquire("group:1") == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_min_interval_survives_window_overflow(self):
        """评审 H1：越过窗口上限后，最小间隔**不能**被一起丢掉（旧实现会瞬时放行 21 条）。"""
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=1.0,
            global_min_interval=0.0,
            per_window=2,
            window=60.0,
            max_wait=10.0,
            clock=ft.clock,
            sleep=ft.sleep,
        )
        stamps = []
        for _ in range(6):
            await th.acquire("group:1")
            stamps.append(ft.t)
        assert stamps[1] - stamps[0] == pytest.approx(1.0)
        gaps = [round(b - a, 6) for a, b in zip(stamps, stamps[1:], strict=False)]
        assert all(g > 0 for g in gaps), gaps  # 任何相邻两条都不得落在同一时刻
        assert max(gaps) <= 10.0  # 但也不因为窗口上限而无限期等待

    @pytest.mark.asyncio
    async def test_window_slides(self, caplog):
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
        assert "超过软上限" not in caplog.text

    @pytest.mark.asyncio
    async def test_frozen_clock_terminates_and_records(self, caplog):
        """时钟不前进（注入时钟/异常时钟）时必须有限返回，不能死循环；放行后仍要记账。"""
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
        assert waited > 0
        assert "轮内仍未满足间隔" in caplog.text
        assert th._global.last == 1000.0  # 放行也要记账

    @pytest.mark.asyncio
    async def test_queue_time_is_not_counted_but_serialized(self):
        """返回值只含自身等待；同 target 并发仍然被串行化（首条 0，其余依次等待）。"""
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=1.0, global_min_interval=0.0, per_window=0, clock=ft.clock, sleep=ft.sleep
        )
        results = await asyncio.gather(*(th.acquire("group:1") for _ in range(3)))
        assert sum(results) == pytest.approx(2.0)
        assert ft.t == pytest.approx(1002.0)

    def test_wait_for_has_no_side_effects(self):
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=1.0, global_min_interval=0.0, per_window=0, clock=ft.clock, sleep=ft.sleep
        )
        assert th.wait_for("group:never-seen") == 0.0
        assert th._targets == {}  # 不为没见过的 target 建桶
        assert th.wait_for("group:never-seen") == 0.0

    @pytest.mark.asyncio
    async def test_target_table_is_bounded(self):
        """长期运行的机器人会见到大量会话，target 表必须有界（评审 M3）。"""
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=0.0,
            global_min_interval=0.0,
            per_window=0,
            max_targets=3,
            clock=ft.clock,
            sleep=ft.sleep,
        )
        for i in range(20):
            await th.acquire(f"group:{i}")
        assert len(th._targets) <= 3
        assert len(th._locks) <= 3

    @pytest.mark.asyncio
    async def test_clock_going_backwards_is_clamped(self):
        """时钟回拨不能让记账时间倒退、也不能让窗口记录无界增长。"""
        state = {"t": 1000.0}

        def clock():
            return state["t"]

        async def sleep(seconds):
            state["t"] += seconds

        th = OutboundThrottle(
            min_interval=1.0,
            global_min_interval=0.0,
            per_window=2,
            window=60.0,
            max_wait=0.0,
            clock=clock,
            sleep=sleep,
        )
        for _ in range(100):
            state["t"] -= 10.0  # 每次调用前回拨 10s
            await th.acquire("group:1")
        hist = list(th._targets["group:1"].history)
        assert hist == sorted(hist)  # 记账时间不倒退
        assert len(hist) <= 61  # 由 window/min_interval 约束，不随调用次数线性增长
        assert th.wait_for("group:1") <= 60.0

    @pytest.mark.asyncio
    async def test_wait_for_does_not_record(self):
        ft = FakeTime()
        th = OutboundThrottle(
            min_interval=1.0, global_min_interval=0.0, per_window=0, clock=ft.clock, sleep=ft.sleep
        )
        assert th.wait_for("group:1") == 0.0
        assert th.wait_for("group:1") == 0.0
        await th.acquire("group:1")
        assert th.wait_for("group:1") == pytest.approx(1.0)

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
    async def test_acquire_happens_before_send(self, monkeypatch):
        from plugins.qq_agent_adapter.sink import Sink

        order: list[str] = []

        class RecordingThrottle:
            async def acquire(self, target):
                order.append(f"acquire:{target}")
                return 0.0

        class Bot:
            async def send_group_msg(self, **kwargs):
                order.append("send")

        sink = Sink(throttle=RecordingThrottle())
        monkeypatch.setattr(sink, "_bots", lambda: [Bot()])
        assert await sink.send("group:777", "提醒") is True
        assert order == ["acquire:group:777", "send"]  # 顺序：先取额度再发送

    @pytest.mark.asyncio
    async def test_retries_next_bot_on_failure(self, monkeypatch):
        from plugins.qq_agent_adapter.sink import Sink

        acquired: list[str] = []

        class Bot:
            def __init__(self, ok):
                self.ok = ok

            async def send_group_msg(self, **kwargs):
                if not self.ok:
                    raise RuntimeError("boom")

        class Throttle:
            async def acquire(self, target):
                acquired.append(target)
                return 0.0

        sink = Sink(throttle=Throttle())
        monkeypatch.setattr(sink, "_bots", lambda: [Bot(False), Bot(True)])
        assert await sink.send("group:1", "提醒") is True
        # 换 bot 重试是同一逻辑投递的内部细节：额度只取一次（评审 M1）
        assert acquired == ["group:1"]

    @pytest.mark.asyncio
    async def test_all_bots_failed_returns_false(self, monkeypatch):
        from plugins.qq_agent_adapter.sink import Sink

        class Bot:
            async def send_group_msg(self, **kwargs):
                raise RuntimeError("boom")

        class Throttle:
            async def acquire(self, target):
                return 0.0

        sink = Sink(throttle=Throttle())
        monkeypatch.setattr(sink, "_bots", lambda: [Bot()])
        assert await sink.send("group:1", "提醒") is False

    @pytest.mark.asyncio
    async def test_bad_targets_are_rejected(self, monkeypatch):
        from plugins.qq_agent_adapter.sink import Sink

        sink = Sink()
        assert await sink.send("group:abc", "x") is False
        assert await sink.send("channel:1", "x") is False
        assert await sink.send("", "x") is False

    @pytest.mark.asyncio
    async def test_no_bot_connected(self, monkeypatch):
        from plugins.qq_agent_adapter.sink import Sink

        sink = Sink()
        monkeypatch.setattr(sink, "_bots", lambda: [])
        assert await sink.send("group:1", "x") is False

    @pytest.mark.asyncio
    async def test_sink_falls_back_to_default_throttle(self):
        from plugins.qq_agent_adapter.sink import Sink

        sink = Sink()
        assert sink._throttle_obj() is outbound.default_throttle()
