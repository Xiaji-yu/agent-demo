
import pytest

from plugins.qq_agent_adapter.matcher import _qq_plain, _truncate, trigger_rule
from plugins.qq_agent_adapter.outbound import split_message as _split_qq_message


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
            [{"user_id": "123", "group_id": None, "self_id": "10001", "chat_target": "private:123"}]
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

        await matcher._answer([{"user_id": "123", "group_id": None, "self_id": "", "chat_target": "private:123"}])
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
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        event = GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 0,
                "post_type": "message",
                "sub_type": "group",
                "user_id": 123,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": [{"type": "text", "data": {"text": "小助手 帮我查一下"}}],
                "original_message": [{"type": "text", "data": {"text": "小助手 帮我查一下"}}],
                "raw_message": "小助手 帮我查一下",
                "font": 0,
                "sender": {"user_id": 123, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
                "anonymous": None,
            }
        )
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        assert trigger_rule(event) is True

    def test_group_matches_at_me(self, monkeypatch):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        event = GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 10001,
                "post_type": "message",
                "sub_type": "group",
                "user_id": 123,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": [{"type": "at", "data": {"qq": "10001"}}, {"type": "text", "data": {"text": " 你好"}}],
                "original_message": [{"type": "at", "data": {"qq": "10001"}}, {"type": "text", "data": {"text": " 你好"}}],
                "raw_message": "",
                "font": 0,
                "sender": {"user_id": 123, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
                "anonymous": None,
            }
        )
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        monkeypatch.setattr(event, "is_tome", lambda: True)
        assert trigger_rule(event) is True

    def test_group_no_match_is_rejected(self, monkeypatch):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        event = GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 0,
                "post_type": "message",
                "sub_type": "group",
                "user_id": 123,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": [{"type": "text", "data": {"text": "随便聊聊"}}],
                "original_message": [{"type": "text", "data": {"text": "随便聊聊"}}],
                "raw_message": "随便聊聊",
                "font": 0,
                "sender": {"user_id": 123, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
                "anonymous": None,
            }
        )
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        assert trigger_rule(event) is False

    def test_group_falls_back_to_prefix_when_no_wake_words(self, monkeypatch):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        event = GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 0,
                "post_type": "message",
                "sub_type": "group",
                "user_id": 123,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": [{"type": "text", "data": {"text": "ai 你好"}}],
                "original_message": [{"type": "text", "data": {"text": "ai 你好"}}],
                "raw_message": "ai 你好",
                "font": 0,
                "sender": {"user_id": 123, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
                "anonymous": None,
            }
        )
        monkeypatch.delenv("AGENT_WAKE_WORDS", raising=False)
        monkeypatch.setenv("AGENT_PREFIX", r"^[!！/]?ai\s*")
        assert trigger_rule(event) is True

    def test_group_prefix_still_works_when_wake_words_configured(self, monkeypatch):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        event = GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 0,
                "post_type": "message",
                "sub_type": "group",
                "user_id": 123,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": [{"type": "text", "data": {"text": "ai 你好"}}],
                "original_message": [{"type": "text", "data": {"text": "ai 你好"}}],
                "raw_message": "ai 你好",
                "font": 0,
                "sender": {"user_id": 123, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
                "anonymous": None,
            }
        )
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        monkeypatch.setenv("AGENT_PREFIX", r"^[！!]?ai\s*")
        assert trigger_rule(event) is True

    def test_group_no_match_when_wake_words_and_prefix_both_miss(self, monkeypatch):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        event = GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 0,
                "post_type": "message",
                "sub_type": "group",
                "user_id": 123,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": [{"type": "text", "data": {"text": "随便聊聊"}}],
                "original_message": [{"type": "text", "data": {"text": "随便聊聊"}}],
                "raw_message": "随便聊聊",
                "font": 0,
                "sender": {"user_id": 123, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
                "anonymous": None,
            }
        )
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,助手")
        monkeypatch.setenv("AGENT_PREFIX", r"^[!！/]?ai\s*")
        assert trigger_rule(event) is False
