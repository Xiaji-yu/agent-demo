import base64

import pytest

from agentcore.skills.file_sender import (
    _safe_filename,
    _safe_user_id,
)


class TestFileSender:
    def test_safe_filename_basic(self):
        assert _safe_filename("report.md") == "report.md"

    def test_safe_filename_strips_path(self):
        assert _safe_filename("../../etc/passwd") == "passwd"

    def test_safe_filename_empty(self):
        assert _safe_filename("") == "report.md"

    def test_safe_user_id_valid(self):
        assert _safe_user_id("123456") == 123456

    def test_safe_user_id_invalid(self):
        with pytest.raises(ValueError):
            _safe_user_id("abc")

    def test_safe_user_id_empty(self):
        with pytest.raises(ValueError):
            _safe_user_id("")

    @pytest.mark.asyncio
    async def test_send_no_bot(self, monkeypatch):
        import agentcore.skills.file_sender as fs

        monkeypatch.setattr(fs, "get_driver", lambda: type("D", (), {"bots": {}})())
        result = await fs.send_markdown_file("123", "hello")
        assert "no bot" in result.lower()

    @pytest.mark.asyncio
    async def test_send_base64_content(self, monkeypatch):
        from nonebot.adapters.onebot.v11 import MessageSegment

        import agentcore.skills.file_sender as fs

        class FakeBot:
            def __init__(self):
                self.last_msg = None

            async def send_private_msg(self, user_id=0, message=None):
                self.last_msg = message

        class FakeDriver:
            def __init__(self):
                self.bots = {"bot": FakeBot()}

        fake_driver = FakeDriver()
        monkeypatch.setattr(fs, "get_driver", lambda: fake_driver)
        monkeypatch.setattr(fs, "MessageSegment", MessageSegment)
        result = await fs.send_markdown_file("123", "# Hello\nWorld", "test.md")
        assert "已发送" in result
        assert "test.md" in result
        seg = fake_driver.bots["bot"].last_msg
        assert isinstance(seg, MessageSegment)
        assert seg.type == "file"
        encoded = seg.data["file"].replace("base64://", "")
        decoded = base64.b64decode(encoded).decode("utf-8")
        assert decoded == "# Hello\nWorld"

    @pytest.mark.asyncio
    async def test_uncertain_timeout_is_not_reported_as_plain_failure(
        self, monkeypatch
    ):
        """M6：超时/断连时不能返回「文件发送失败，返回文本内容」这种普通失败串。

        否则上层（outbound.deliver_reply）会据此把**全文**再发一遍，同一内容到用户手里两遍。
        """
        import agentcore.skills.file_sender as fs

        class BoomBot:
            async def send_private_msg(self, user_id=0, message=None):
                raise TimeoutError("websocket timed out")

        class FakeDriver:
            def __init__(self):
                self.bots = {"bot": BoomBot()}

        from nonebot.adapters.onebot.v11 import MessageSegment

        monkeypatch.setattr(fs, "NAPCAT_HTTP_URL", "")
        monkeypatch.setattr(fs, "get_driver", lambda: FakeDriver())
        monkeypatch.setattr(fs, "MessageSegment", MessageSegment)

        result = await fs.send_markdown_file("123", "很长的正文" * 50)
        assert result.startswith(fs.FILE_SEND_UNCERTAIN_PREFIX), result
        assert "返回文本内容" not in result

    def test_uncertain_predicate_covers_timeout_and_disconnect(self):
        from agentcore.skills.file_sender import is_uncertain_send_error

        class WebSocketClosed(Exception):
            pass

        assert is_uncertain_send_error(TimeoutError("x"))
        assert is_uncertain_send_error(RuntimeError("ws timeout after send"))
        assert is_uncertain_send_error(WebSocketClosed("closed"))
        # 确定失败必须能区分出来，否则会白白放弃一次降级
        assert not is_uncertain_send_error(RuntimeError("unsupported action"))
        assert not is_uncertain_send_error(RuntimeError("file too large"))
