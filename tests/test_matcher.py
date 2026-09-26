import asyncio

import pytest

from plugins.qq_agent_adapter.matcher import _qq_plain, _truncate, trigger_rule
from plugins.qq_agent_adapter.outbound import split_message as _split_qq_message


def _group_event(
    text: str,
    *,
    self_id: int = 0,
    user_id: int = 123,
    group_id: int = 456,
    message_id: int = 1,
    to_me: bool = False,
    segments: list | None = None,
):
    """构造群消息事件（模板去重：原先 15 行字典在本文件内联了 7 份）。

    ``segments`` 给定时覆盖 message/original_message（reply/at 等多段消息用），
    此时 ``text`` 仍作为 raw_message。
    """
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    if segments is None:
        segments = [{"type": "text", "data": {"text": text}}]
    return GroupMessageEvent.parse_obj(
        {
            "time": 0,
            "self_id": self_id,
            "post_type": "message",
            "sub_type": "group",
            "user_id": user_id,
            "message_type": "group",
            "message_id": message_id,
            "group_id": group_id,
            "message": segments,
            "original_message": segments,
            "raw_message": text,
            "font": 0,
            "sender": {"user_id": user_id, "nickname": "", "card": ""},
            "to_me": to_me,
            "reply": None,
            "anonymous": None,
        }
    )


class TestQQPlain:
    def test_bold_stripped(self):
        assert _qq_plain("**年柱**：辛巳") == "年柱：辛巳"

    def test_heading_stripped(self):
        assert _qq_plain("### 标题\n正文") == "标题\n正文"

    def test_quote_stripped(self):
        assert _qq_plain("> 引用") == "引用"

    def test_list_dash_normalized(self):
        assert _qq_plain("- a\n* b") == "- a\n- b"

    def test_inline_code_stripped(self):
        assert _qq_plain("运行 `python bot.py`") == "运行 python bot.py"

    def test_link_kept(self):
        out = _qq_plain("[百度](https://www.baidu.com)")
        assert "百度" in out and "https://www.baidu.com" in out and "[" not in out

    def test_keeps_newlines(self):
        out = _qq_plain("第一行\n第二行\n第三行")
        assert out == "第一行\n第二行\n第三行"

    def test_blank_lines_collapsed(self):
        """模型默认按 markdown 习惯用空行分段（实测一条回答 7 个空行），
        QQ 聊天框里空行只是"透气"——压成单个换行，结构不丢。"""
        assert _qq_plain("段一\n\n段二") == "段一\n段二"
        # 3+ 连续换行、行尾空白形成的"假空行"同样收敛
        assert _qq_plain("段一\n\n \n\n段二") == "段一\n段二"
        assert _qq_plain("a\n\n\n\n\nb") == "a\nb"

    def test_code_block_internal_blank_lines_preserved(self):
        """代码块在函数开头就被替换成单行占位符——块内空行绝不被收敛。"""
        src = (
            "前文\n\n```python\ndef f():\n    pass\n\n\ndef g():\n    pass\n```\n\n后文"
        )
        out = _qq_plain(src)
        assert "def f():\n    pass\n\n\ndef g():" in out, "代码块内容必须原样保留"
        assert "前文\ndef f()" in out.replace("```python\n", ""), "块外空行才收敛"
        assert "\n\n后文" not in out

    def test_real_world_long_answer_has_no_blank_lines(self):
        """线上形态回归：一条带标题/编号列表/要点的长回答，处理后不含空行。"""
        raw = (
            "夏祭，这是 Steam 内嵌的 Chromium 浏览器进程 \n\n"
            "简单说：Steam 客户端里的商店页面全是网页。\n\n"
            "为什么吃内存？\n\n"
            "1. Chromium 本身就很能吃\n"
            "2. Steam 会同时开好几个\n\n"
            "云崽建议：\n\n"
            "- 不用的时候直接退掉\n"
            "- 用 steam:// 协议直接启动\n\n"
            "简单说：客户端本质上是个套了壳的浏览器。"
        )
        out = _qq_plain(raw)
        assert "\n\n" not in out
        assert "为什么吃内存？" in out and "云崽建议：" in out
        assert out.count("\n") == raw.count("\n") - raw.count("\n\n"), (
            "只收空行，不动内容行"
        )

    def test_empty(self):
        assert _qq_plain("") == ""
        assert _qq_plain(None) is None


class TestMatcherUtils:
    def test_truncate_short(self):
        assert _truncate("hello") == "hello"

    def test_truncate_exact(self):
        assert _truncate("hello", 5) == "hello"

    def test_truncate_long(self):
        text = "a" * 300
        result = _truncate(text, 200)
        assert len(result) == 203
        assert result.endswith("...")

    def test_split_short(self):
        assert _split_qq_message("hello") == ["hello"]

    def test_split_long_with_breaks(self):
        text = "第一句。第二句。第三句。"
        result = _split_qq_message(text, max_len=10)
        assert len(result) > 1

    def test_split_empty(self):
        assert _split_qq_message("") == []

    def test_split_skips_whitespace_only_chunks(self):
        text = "w" + " " * 500 + "w"
        chunks = _split_qq_message(text, max_len=100)
        assert chunks
        assert all(c.strip() for c in chunks)

    def test_qq_plain_fenced_code_untouched(self):
        code = "```python\n# 注释\ns = '**hi**'\n```"
        out = _qq_plain(code)
        assert "# 注释" in out
        assert "**hi**" in out  # 代码块内不做 md 改写

    def test_qq_plain_double_underscore_bold(self):
        assert _qq_plain("a __b__ c") == "a b c"

    def test_split_keeps_newlines_in_chunk(self):
        text = "第一行\n第二行\n第三行"
        result = _split_qq_message(text, max_len=50)
        assert result == [text]
        assert "\n" in result[0]

    def test_split_long_md_cuts_at_punct_not_mid_word(self):
        # 无空格无换行的超长 md 连续文本，应在中文标点后断行
        seg = "**年柱**：辛巳（白蜡金），**月柱**：丙申（山下火）。" * 8
        chunks = _split_qq_message(seg, max_len=40)
        assert len(chunks) > 1
        joined = "".join(chunks)
        # 切割不丢字符
        assert joined.replace(" ", "") == seg.replace(" ", "")
        # 断点尽量在标点/换行处，而不是任意截断
        for c in chunks[:-1]:
            assert c[-1] in "。，；、！？：\n" or c.endswith("）")


class TestAnswerWiring:
    """_answer 必须把长回复交给 outbound.deliver_reply（分层 + 节流都在那里）。"""

    @pytest.mark.asyncio
    async def test_group_payload_passes_kind_and_ident(self, monkeypatch):
        from plugins.qq_agent_adapter import matcher

        captured: list[dict] = []

        async def fake_deliver(bot, **kwargs):
            captured.append(kwargs)
            return "single"

        async def fake_format(payload, text, images):
            return "好的"

        monkeypatch.setattr(matcher, "deliver_reply", fake_deliver)
        monkeypatch.setattr(matcher, "_run_and_format", fake_format)
        monkeypatch.setattr(matcher, "merge_parts", lambda parts: ("hi", []))
        monkeypatch.setattr(matcher, "get_bot", lambda sid=None: object())

        await matcher._answer(
            [
                {
                    "user_id": "123",
                    "group_id": "456",
                    "self_id": "10001",
                    "chat_target": "group:456",
                    "user_text": "hi",
                }
            ]
        )
        assert captured and captured[0]["kind"] == "group"
        assert captured[0]["ident"] == 456
        assert captured[0]["self_id"] == "10001"
        assert captured[0]["text"] == "好的"
        # 回复与主动推送必须共用同一进程级限流器，否则节流形同虚设
        from plugins.qq_agent_adapter.outbound import default_throttle

        assert captured[0]["throttle"] is default_throttle()

    @pytest.mark.asyncio
    async def test_private_payload_passes_user_id(self, monkeypatch):
        from plugins.qq_agent_adapter import matcher

        captured: list[dict] = []

        async def fake_deliver(bot, **kwargs):
            captured.append(kwargs)
            return "single"

        async def fake_format(payload, text, images):
            return "好的"

        monkeypatch.setattr(matcher, "deliver_reply", fake_deliver)
        monkeypatch.setattr(matcher, "_run_and_format", fake_format)
        monkeypatch.setattr(matcher, "merge_parts", lambda parts: ("hi", []))
        monkeypatch.setattr(matcher, "get_bot", lambda sid=None: object())

        await matcher._answer(
            [
                {
                    "user_id": "123",
                    "group_id": None,
                    "self_id": "10001",
                    "chat_target": "private:123",
                }
            ]
        )
        assert captured and captured[0]["kind"] == "private"
        assert captured[0]["ident"] == 123

    @pytest.mark.asyncio
    async def test_no_bot_raises_and_falls_back_to_error_reply(self, monkeypatch):
        from plugins.qq_agent_adapter import matcher

        sent: list[str] = []

        async def fake_format(payload, text, images):
            return "好的"

        async def fake_send_reply(payload, chunk):
            sent.append(chunk)

        monkeypatch.setattr(matcher, "_run_and_format", fake_format)
        monkeypatch.setattr(matcher, "merge_parts", lambda parts: ("hi", []))
        monkeypatch.setattr(matcher, "get_bot", lambda sid=None: None)
        monkeypatch.setattr(matcher, "_send_reply", fake_send_reply)

        await matcher._answer(
            [
                {
                    "user_id": "123",
                    "group_id": None,
                    "self_id": "",
                    "chat_target": "private:123",
                }
            ]
        )
        # 必须能看出是「显式识别到没有 bot」，而不是下游随便抛的异常
        assert sent and sent[0].startswith("出错啦")


class TestTriggerRule:
    def test_private_always_allowed(self, monkeypatch):
        from nonebot.adapters.onebot.v11 import PrivateMessageEvent

        event = PrivateMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 0,
                "post_type": "message",
                "sub_type": "friend",
                "user_id": 123,
                "message_type": "private",
                "message_id": 1,
                "message": [{"type": "text", "data": {"text": "hi"}}],
                "original_message": [{"type": "text", "data": {"text": "hi"}}],
                "raw_message": "hi",
                "font": 0,
                "sender": {"user_id": 123, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        assert trigger_rule(event) is True

    def test_group_matches_wake_word(self, monkeypatch):
        event = _group_event("小助手 帮我查一下")
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        assert trigger_rule(event) is True

    def test_group_matches_at_me(self, monkeypatch):
        event = _group_event(
            "",
            self_id=10001,
            segments=[
                {"type": "at", "data": {"qq": "10001"}},
                {"type": "text", "data": {"text": " 你好"}},
            ],
        )
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        # 不再打桩 is_tome：本用例要真正验证「自行扫描 at 段」这条路（M10）
        assert trigger_rule(event) is True

    def test_group_no_match_is_rejected(self, monkeypatch):
        event = _group_event("随便聊聊")
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        assert trigger_rule(event) is False

    def test_group_legacy_prefix_no_longer_triggers(self, monkeypatch):
        """旧前缀（ai/!ai//ai）已按需求移除：没有唤醒词时不得触发。"""

        event = _group_event("ai 你好")
        monkeypatch.delenv("AGENT_WAKE_WORDS", raising=False)
        monkeypatch.setenv("AGENT_PREFIX", r"^[!！/]?ai\s*")  # 残留配置也必须无效
        assert trigger_rule(event) is False

    def test_group_prefix_invalid_even_with_wake_words(self, monkeypatch):
        """配了唤醒词也一样：前缀文本（"ai 你好"）不命中唤醒词就不触发。"""

        event = _group_event("ai 你好")
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        monkeypatch.setenv("AGENT_PREFIX", r"^[！!]?ai\s*")
        assert trigger_rule(event) is False

    def test_group_at_bot_between_other_at_triggers(self, monkeypatch):
        """线上复现：「reply + @别人 + @bot」时适配器 _check_at_me 只认首/尾 @，
        to_me=False——trigger_rule 必须自行扫描 at 段补上（不触发即漏答）。"""

        event = _group_event(
            "[CQ:reply,id=610959594][CQ:at,qq=3958874605][CQ:at,qq=3629537600] 这张图上写了什么",
            self_id=3629537600,
            user_id=2224513919,
            group_id=1108838060,
            message_id=518483608,
            segments=[
                {"type": "reply", "data": {"id": "610959594"}},
                {"type": "at", "data": {"qq": "3958874605"}},
                {"type": "at", "data": {"qq": "3629537600"}},
                {"type": "text", "data": {"text": " 这张图上写了什么"}},
            ],
        )
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        assert trigger_rule(event) is True

    def test_group_at_others_only_not_triggered(self, monkeypatch):
        """对照组：只 @ 别人（无 @bot、无唤醒词）不应触发——群友互聊不叫醒 bot。"""

        event = _group_event(
            "[CQ:reply,id=808768036][CQ:at,qq=2224513919]防注入啊",
            self_id=3629537600,
            user_id=3865067623,
            group_id=1108838060,
            message_id=746727950,
            segments=[
                {"type": "reply", "data": {"id": "808768036"}},
                {"type": "at", "data": {"qq": "2224513919"}},
                {"type": "text", "data": {"text": "防注入啊"}},
            ],
        )
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        assert trigger_rule(event) is False

    def test_private_self_message_rejected(self, monkeypatch):
        """NapCat「上报自身消息」开启后，bot 私发的每条消息都会作为 message
        事件回传（user_id == self_id）——必须过滤，否则 bot 会对自己的消息跑
        完整对话（线上复现：私发文件 → 回传 → 空文本 + 复用历史图 → 模型解析旧图，
        且 bot 的回复再次回传，存在自循环）。"""
        from nonebot.adapters.onebot.v11 import PrivateMessageEvent

        event = PrivateMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 10001,
                "post_type": "message",
                "sub_type": "friend",
                "user_id": 10001,
                "message_type": "private",
                "message_id": 2,
                "message": [{"type": "text", "data": {"text": "文件已发送"}}],
                "original_message": [{"type": "text", "data": {"text": "文件已发送"}}],
                "raw_message": "文件已发送",
                "font": 0,
                "sender": {"user_id": 10001, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        assert trigger_rule(event) is False

    def test_group_self_message_rejected_even_with_wake_word(self, monkeypatch):
        """群聊 self 消息即使文本含唤醒词也不触发——否则 bot 的每条群回复都会
        自触发（回复里提到唤醒词的情况并不罕见）。"""
        event = _group_event(
            "小助手 文件已发送", self_id=3629537600, user_id=3629537600
        )
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        assert trigger_rule(event) is False

    def test_group_recorder_rule_rejects_self(self):
        """group_recorder 也要过滤 self：bot 的发言不是「其他群成员」的语境，
        混进群上下文会让模型把 bot 说过的话当成群友发言。"""
        from plugins.qq_agent_adapter.matcher import _record_group_rule

        self_event = _group_event(
            "bot 自己的回复", self_id=3629537600, user_id=3629537600
        )
        assert _record_group_rule(self_event) is False
        other = _group_event("群友的消息", self_id=3629537600, user_id=2224513919)
        assert _record_group_rule(other) is True


class TestWakeWordEdgeCases:
    """M9/M10/M11 的回归（REVIEW-bbd8913..f6dffcc.md）。"""

    @staticmethod
    def _group(msg, *, self_id=3629537600, to_me=False):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        return GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": self_id,
                "post_type": "message",
                "sub_type": "group",
                "user_id": 1,
                "message_type": "group",
                "message_id": 1,
                "group_id": 2,
                "message": msg,
                "original_message": msg,
                "raw_message": "",
                "font": 0,
                "sender": {"user_id": 1, "nickname": "", "card": ""},
                "to_me": to_me,
                "reply": None,
                "anonymous": None,
            }
        )

    def test_to_me_without_at_segment_triggers(self, monkeypatch):
        """M10：适配器会删掉首/尾 at 段并置 to_me=True，此时只能靠 is_tome() 兜底。"""
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        ev = self._group([{"type": "text", "data": {"text": "你好"}}], to_me=True)
        assert trigger_rule(ev) is True

    def test_no_at_no_to_me_still_rejected(self, monkeypatch):
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        ev = self._group([{"type": "text", "data": {"text": "随便聊聊"}}])
        assert trigger_rule(ev) is False

    def test_wake_word_after_reply_segment_triggers(self, monkeypatch):
        """M11：引用别人消息后打唤醒词，此前因 [CQ:reply…] 前缀而漏触发。"""
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        ev = self._group(
            [
                {"type": "reply", "data": {"id": "1"}},
                {"type": "text", "data": {"text": "小助手 帮我查天气"}},
            ]
        )
        assert trigger_rule(ev) is True

    def test_wake_word_after_at_other_segment_triggers(self, monkeypatch):
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        ev = self._group(
            [
                {"type": "at", "data": {"qq": "3958874605"}},
                {"type": "text", "data": {"text": "小助手 帮我查天气"}},
            ]
        )
        assert trigger_rule(ev) is True

    def test_prefix_after_reply_segment_no_longer_triggers(self, monkeypatch):
        """旧前缀已移除：reply 段之后的 "ai 帮我查天气" 不再触发（只有唤醒词/@ 可以）。"""
        monkeypatch.delenv("AGENT_WAKE_WORDS", raising=False)
        ev = self._group(
            [
                {"type": "reply", "data": {"id": "1"}},
                {"type": "text", "data": {"text": "ai 帮我查天气"}},
            ]
        )
        assert trigger_rule(ev) is False

    def test_strip_wake_word_handles_leading_space(self, monkeypatch):
        """M9：@ 段被移除后文本带前导空格，剥离必须仍然生效。"""
        from plugins.qq_agent_adapter.wakewords import strip_wake_word

        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        assert strip_wake_word("小助手 帮我查天气") == "帮我查天气"
        assert strip_wake_word(" 小助手 帮我查天气") == "帮我查天气"
        assert strip_wake_word("  助手 你好") == "你好"
        assert strip_wake_word("无关内容") == "无关内容"


# ==========================================================================
# REVIEW-a604023..679c9b3 第三批：全局 LLM 并发闸门
# ==========================================================================


# 来源: test_review_concurrency_fixes TestGlobalTurnSemaphore（含 _noop_deliver）
class TestGlobalTurnSemaphore:
    def test_semaphore_limits_concurrency(self, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_CONCURRENT_TURNS", "2")
        import plugins.qq_agent_adapter.matcher as m

        # L2：裸赋值不恢复会让进程内单例固定成 Semaphore(2)，污染后续用例
        monkeypatch.setattr(m, "_turn_semaphore", None)
        sem = m._get_turn_semaphore()
        assert sem._value == 2

    @pytest.mark.asyncio
    async def test_burst_does_not_exceed_limit(self, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_CONCURRENT_TURNS", "2")
        import plugins.qq_agent_adapter.matcher as m

        monkeypatch.setattr(m, "_turn_semaphore", None)
        running = 0
        peak = 0

        async def fake_run_and_format(payload, text, images):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return "ok"

        monkeypatch.setattr(m, "_run_and_format", fake_run_and_format)
        monkeypatch.setattr(m, "deliver_reply", _noop_deliver, raising=False)
        payload = {
            "user_id": "1",
            "group_id": None,
            "text": "hi",
            "images": [],
            "message_id": "1",
            "chat_target": "private:1",
        }
        await asyncio.gather(*(m._answer([dict(payload)]) for _ in range(6)))
        assert peak <= 2, f"并发闸门失效：峰值 {peak}"


async def _noop_deliver(*args, **kwargs):
    return {"mode": "single", "sent": 1}


class TestTurnTimeout:
    """单回合引擎超时（评审 C2 回归）：挂死的引擎调用不得永久占用并发闸门。"""

    @staticmethod
    def _engine_with(run):
        from types import SimpleNamespace

        return SimpleNamespace(run=run)

    @pytest.mark.asyncio
    async def test_hung_engine_times_out_to_fallback(self, monkeypatch):
        import plugins.qq_agent_adapter.matcher as m

        monkeypatch.setenv("AGENT_TURN_TIMEOUT", "0.05")

        async def hang(context, text, extra_images=None):
            await asyncio.sleep(30)
            return "never"

        monkeypatch.setattr(m, "engine", self._engine_with(hang))
        out = await m._run_and_format({"user_id": "u1", "user_text": "hi"}, "hi", [])
        # 超时走既有失败兜底（[echo]），而不是无限等待
        assert "hi" in out, out

    @pytest.mark.asyncio
    async def test_zero_disables_timeout(self, monkeypatch):
        import plugins.qq_agent_adapter.matcher as m

        monkeypatch.setenv("AGENT_TURN_TIMEOUT", "0")

        async def slow(context, text, extra_images=None):
            await asyncio.sleep(0.15)
            return "慢但完成了"

        monkeypatch.setattr(m, "engine", self._engine_with(slow))
        out = await m._run_and_format({"user_id": "u1", "user_text": "x"}, "x", [])
        assert out == "慢但完成了"

    def test_dirty_timeout_falls_back_to_default(self, monkeypatch):
        import plugins.qq_agent_adapter.matcher as m

        monkeypatch.setenv("AGENT_TURN_TIMEOUT", "abc")
        assert m._turn_timeout_seconds() == 180.0
        monkeypatch.setenv("AGENT_TURN_TIMEOUT", "-3")
        assert m._turn_timeout_seconds() == 180.0
        monkeypatch.setenv("AGENT_TURN_TIMEOUT", "30")
        assert m._turn_timeout_seconds() == 30.0
