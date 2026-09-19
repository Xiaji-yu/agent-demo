import base64

import httpx
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
        assert result.startswith("协议端 返回异常"), result
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


# ==========================================================================
# REVIEW-a604023..679c9b3 H4：超时/断连=结果不确定
# ==========================================================================


# 来源: test_review_h_fixes TestUncertainSendError
class TestUncertainSendError:
    def test_httpx_timeouts_are_uncertain(self):
        from agentcore.skills.file_sender import is_uncertain_send_error

        for exc in (
            httpx.ReadTimeout(""),
            httpx.ConnectTimeout(""),
            httpx.WriteTimeout(""),
            httpx.PoolTimeout(""),
        ):
            assert is_uncertain_send_error(exc), type(exc).__name__

    def test_plain_timeout_still_uncertain(self):
        from agentcore.skills.file_sender import is_uncertain_send_error

        assert is_uncertain_send_error(TimeoutError("x"))
        assert is_uncertain_send_error(TimeoutError())

    def test_non_timeout_not_uncertain(self):
        from agentcore.skills.file_sender import is_uncertain_send_error

        assert not is_uncertain_send_error(ValueError("bad arg"))


# ==========================================================================
# H1（REVIEW-6ec3f7c..a36ea1d）：断连家族必须归「结果不确定」
#
# 旧实现用 type(err).__name__ == "NetworkError" 精确匹配类名，只命中那个从不被
# 直接抛出的基类本身；httpx 的 ReadError/WriteError/RemoteProtocolError 等**子类**
# 全部落空 → 被判「确定失败」→ 出站降级重发 → 用户收到两遍。
# ==========================================================================


class TestUncertainCoversTransportFamily:
    _UNCERTAIN = (
        "ReadError",
        "WriteError",
        "CloseError",
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "ProxyError",
    )
    # 「请求根本没发出去」的两类：判确定失败才准确（允许安全降级重发）
    _NOT_UNCERTAIN = ("ConnectError", "UnsupportedProtocol")

    def test_transport_family_is_uncertain(self):
        from agentcore.skills.file_sender import is_uncertain_send_error

        for name in self._UNCERTAIN:
            exc = getattr(httpx, name)("")
            assert is_uncertain_send_error(exc), f"{name} 应判为结果不确定"

    def test_never_sent_errors_are_certain_failure(self):
        from agentcore.skills.file_sender import is_uncertain_send_error

        for name in self._NOT_UNCERTAIN:
            exc = getattr(httpx, name)("")
            assert not is_uncertain_send_error(exc), (
                f"{name} 表示请求从未发出，应判确定失败（可安全重发）"
            )

    def test_name_based_matching_would_miss_subclasses(self):
        """守住回归的根因：这些异常的类型名都不等于 "NetworkError"。

        若实现退回 ``type(err).__name__ in {...}`` 的精确匹配，本用例即失败。
        """
        for name in self._UNCERTAIN:
            assert type(getattr(httpx, name)("")).__name__ != "NetworkError"

    def test_transport_base_class_itself_is_uncertain(self):
        """基类直接实例化（httpx 内部某些包装路径）也应归不确定。"""
        from agentcore.skills.file_sender import is_uncertain_send_error

        assert is_uncertain_send_error(httpx.TransportError("boom"))


# ==========================================================================
# 群文件路径：此前 send_markdown_file 只有私聊实现（群里要文件 → 静默私发）
# ==========================================================================


class TestGroupFile:
    """群聊发文件必须走 upload_group_file；失败时不降级私发。"""

    @pytest.mark.asyncio
    async def test_group_file_sent_via_upload_group_file(self, monkeypatch):
        import agentcore.skills.file_sender as fs

        class FakeBot:
            def __init__(self):
                self.uploaded = None

            async def upload_group_file(self, group_id=0, file="", name=""):
                self.uploaded = (group_id, file, name)

            async def send_private_msg(self, user_id=0, message=None):
                raise AssertionError("群文件路径不得回退私聊发送")

        class FakeDriver:
            def __init__(self):
                self.bots = {"b": FakeBot()}

        driver = FakeDriver()
        monkeypatch.setattr(fs, "get_driver", lambda: driver)
        monkeypatch.setattr(fs, "NAPCAT_HTTP_URL", "")

        result = await fs.send_markdown_file(
            "123", "# 群报告", "report.md", group_id="456"
        )

        assert result.startswith(fs.FILE_SEND_OK_PREFIX), result
        gid, file, name = driver.bots["b"].uploaded
        assert gid == 456
        assert name == "report.md"
        assert base64.b64decode(file.removeprefix("base64://")).decode("utf-8") == (
            "# 群报告"
        )

    @pytest.mark.asyncio
    async def test_group_file_plain_failure_does_not_fall_back_to_private(
        self, monkeypatch
    ):
        """确定失败（如无上传权限）时不得静默私发：用户明示要文件，私发会把
        文件送到错误的地方（线上复现：群里要文件 → 私聊收到）。"""

        import agentcore.skills.file_sender as fs

        class NoPermBot:
            async def upload_group_file(self, group_id=0, file="", name=""):
                raise RuntimeError("权限不足，无法上传群文件")

            async def send_private_msg(self, user_id=0, message=None):
                raise AssertionError("确定失败时不得回退私聊发送")

        class FakeDriver:
            def __init__(self):
                self.bots = {"b": NoPermBot()}

        monkeypatch.setattr(fs, "get_driver", lambda: FakeDriver())
        monkeypatch.setattr(fs, "NAPCAT_HTTP_URL", "")

        result = await fs.send_markdown_file("123", "正文", group_id="456")
        assert "群文件发送失败" in result
        assert "权限不足" in result
        assert not result.startswith(fs.FILE_SEND_OK_PREFIX)

    @pytest.mark.asyncio
    async def test_group_file_uncertain_not_retried(self, monkeypatch):
        """超时 → UNCERTAIN：文件可能已上传，绝不能重发（M6 纪律）。"""

        import agentcore.skills.file_sender as fs

        class SlowBot:
            async def upload_group_file(self, group_id=0, file="", name=""):
                raise TimeoutError("websocket timed out")

            async def send_private_msg(self, user_id=0, message=None):
                raise AssertionError("不确定结果时不得回退私聊发送")

        class FakeDriver:
            def __init__(self):
                self.bots = {"b": SlowBot()}

        monkeypatch.setattr(fs, "get_driver", lambda: FakeDriver())
        monkeypatch.setattr(fs, "NAPCAT_HTTP_URL", "")

        result = await fs.send_markdown_file("123", "正文", group_id="456")
        assert result.startswith(fs.FILE_SEND_UNCERTAIN_PREFIX), result

    @pytest.mark.asyncio
    async def test_group_file_invalid_group_id_rejected(self, monkeypatch):
        """非法群号（非 ASCII 数字，含全角）不得上传——会发到错误的群。"""

        import agentcore.skills.file_sender as fs

        class FakeBot:
            async def upload_group_file(self, group_id=0, file="", name=""):
                raise AssertionError("非法群号不应到达上传调用")

        class FakeDriver:
            def __init__(self):
                self.bots = {"b": FakeBot()}

        monkeypatch.setattr(fs, "get_driver", lambda: FakeDriver())
        monkeypatch.setattr(fs, "NAPCAT_HTTP_URL", "")

        result = await fs.send_markdown_file("123", "正文", group_id="４５６")
        assert result.startswith("Error:"), result

    def test_safe_group_id_rejects_fullwidth_and_non_digits(self):
        import agentcore.skills.file_sender as fs

        for bad in ("abc", "", " 12", "+12", "-12", "12.0", "1_2", "４５６"):
            with pytest.raises(ValueError):
                fs._safe_group_id(bad)
        assert fs._safe_group_id("456") == 456

    @pytest.mark.asyncio
    async def test_registry_injects_group_id_into_skill(self, monkeypatch):
        """engine 走 registry.execute(func, user_id=, group_id=) 时，group_id 必须
        按签名注入 send_markdown_file_skill——这是群文件路径生效的关键链路。"""

        import agentcore.skills.file_sender as fs
        from agentcore.skills.registry import SkillRegistry

        captured = {}

        async def fake_send(user_id, content, filename="report.md", *, group_id=None):
            captured["group_id"] = group_id
            return fs.FILE_SEND_OK_PREFIX + " ok"

        monkeypatch.setattr(fs, "send_markdown_file", fake_send)
        reg = SkillRegistry()
        fs.register_file_skills(reg)

        await reg.execute(
            "send_markdown_file",
            user_id="123",
            group_id="456",
            content="正文",
            filename="a.md",
        )
        assert captured["group_id"] == "456"

        captured.clear()
        await reg.execute(
            "send_markdown_file",
            user_id="123",
            group_id=None,
            content="正文",
        )
        assert captured["group_id"] is None


# ---------------------------------------------------------------- H5 停机顺序
