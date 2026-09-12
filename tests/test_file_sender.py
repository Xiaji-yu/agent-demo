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

    def test_safe_user_id_rejects_whitespace_and_signs(self):
        """这些值 int() 可能接受（前导空白/正负号），必须被 isdigit 守卫拒绝——
        否则该守卫删掉也不会让测试失败（原用例只喂 'abc'，int 自己就会抛）。"""
        for bad in (" 12", "+12", "-12", "12.0", "1_2", "１２"):
            with pytest.raises(ValueError):
                _safe_user_id(bad)

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

    @pytest.mark.asyncio
    async def test_napcat_upload_private_file_posts_base64(self, monkeypatch):
        """`_napcat_upload_private_file` 此前**从未被执行**：NapCat 分支是主路径，
        它一坏（编码/字段名/鉴权头）所有文件都只能降级成正文文本。
        """
        import agentcore.skills.file_sender as fs

        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "ok", "message_id": 42}

        class FakeClient:
            def __init__(self, **kw):
                captured["timeout"] = kw.get("timeout")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
                return FakeResponse()

        monkeypatch.setattr(fs.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(fs, "NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
        monkeypatch.setattr(fs, "NAPCAT_HTTP_TOKEN", "secret-token")

        result = await fs._napcat_upload_private_file(
            "10001", "# 报告\n正文", "报告.md"
        )

        assert result.startswith(fs.FILE_SEND_OK_PREFIX), result
        assert "报告.md" in result
        assert captured["url"] == "http://127.0.0.1:3000/upload_private_file"
        assert captured["headers"] == {
            "Content-Type": "application/json",
            "Authorization": "Bearer secret-token",
        }
        assert captured["json"]["user_id"] == 10001, (
            "必须是 int（字符串会被 NapCat 忽略）"
        )
        assert captured["json"]["name"] == "报告.md"
        assert (
            captured["json"]["file"]
            == "base64://" + base64.b64encode("# 报告\n正文".encode()).decode()
        )

    @pytest.mark.asyncio
    async def test_napcat_upload_without_token_omits_auth_header(self, monkeypatch):
        import agentcore.skills.file_sender as fs

        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "ok"}

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, headers=None):
                captured["headers"] = headers
                return FakeResponse()

        monkeypatch.setattr(fs.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(fs, "NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
        monkeypatch.setattr(fs, "NAPCAT_HTTP_TOKEN", "")

        await fs._napcat_upload_private_file("1", "x", "a.md")
        assert captured["headers"] == {"Content-Type": "application/json"}

    @pytest.mark.asyncio
    async def test_napcat_upload_requires_url(self, monkeypatch):
        import agentcore.skills.file_sender as fs

        monkeypatch.setattr(fs, "NAPCAT_HTTP_URL", "")
        with pytest.raises(RuntimeError, match="NAPCAT_HTTP_URL not configured"):
            await fs._napcat_upload_private_file("1", "x", "a.md")

    @pytest.mark.asyncio
    async def test_napcat_upload_reports_abnormal_response(self, monkeypatch):
        """NapCat 返回了 HTTP 200 但业务失败：必须把原文带回去，不能谎报成功。"""
        import agentcore.skills.file_sender as fs

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "failed", "msg": "群文件上传受限"}

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, headers=None):
                return FakeResponse()

        monkeypatch.setattr(fs.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(fs, "NAPCAT_HTTP_URL", "http://127.0.0.1:3000")

        result = await fs._napcat_upload_private_file("1", "x", "a.md")
        assert result.startswith("NapCat 返回异常"), result
        assert "群文件上传受限" in result

    @pytest.mark.asyncio
    async def test_send_markdown_file_prefers_napcat_over_onebot(self, monkeypatch):
        """公开入口也要真的走 NapCat 分支：配了 URL 就不该再碰 OneBot。"""
        import agentcore.skills.file_sender as fs

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "ok"}

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, headers=None):
                return FakeResponse()

        class LoudBot:
            async def send_private_msg(self, user_id=0, message=None):
                raise AssertionError("配了 NapCat 就不该回落到 OneBot")

        monkeypatch.setattr(fs.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(fs, "NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
        monkeypatch.setattr(
            fs, "get_driver", lambda: type("D", (), {"bots": {"b": LoudBot()}})()
        )

        result = await fs.send_markdown_file("10001", "# hi", "a.md")
        assert result.startswith(fs.FILE_SEND_OK_PREFIX), result

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
