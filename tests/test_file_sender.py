import base64

import pytest

from agentcore.skills.file_sender import _safe_filename, _safe_user_id, send_markdown_file


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
        import agentcore.skills.file_sender as fs
        from nonebot.adapters.onebot.v11 import MessageSegment

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
