
import pytest

from plugins.qq_agent_adapter.media import (
    MediaItem,
    _coerce_segments,
    _filename_for,
    download_image,
    extract_media,
    is_allowed_image_url,
    media_from_segments,
    resolve_forward_content,
    resolve_quoted_media,
    sniff_image_type,
    text_from_segments,
)


class _Seg:
    def __init__(self, type_, data):
        self.type = type_
        self.data = data


class _Ev:
    def __init__(self, segs):
        self._segs = segs

    def get_message(self):
        return self._segs


class _FakeResp:
    def __init__(self, body: bytes, content_type: str = "image/jpeg", status: int = 200, headers=None):
        self.body = body
        self.status_code = status
        if headers is not None:
            self.headers = headers
        else:
            self.headers = {"content-type": content_type}

    async def aiter_bytes(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i : i + n]


class _FakeClient:
    """最小 httpx 兼容：stream() 返回 async context manager，按次弹出预设响应。"""

    def __init__(self, *resps):
        self._resps = list(resps)
        self.calls = []

    def stream(self, method, url, **kw):
        self.calls.append((method, url))
        return _StreamCtx(self._resps.pop(0) if self._resps else _FakeResp(b"", "image/jpeg", 404))


class _StreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


async def _async_return(value):
    """把同步桩函数包装成可 await 的协程（替换 asyncio.to_thread 用）。"""
    return value


class TestMediaUtils:
    def test_url_scheme_guard(self):
        assert is_allowed_image_url("https://gchat.qpic.cn/x.jpg?t=1")
        assert not is_allowed_image_url("http://x/1.jpg")
        assert not is_allowed_image_url("file:///etc/passwd")
        assert not is_allowed_image_url("")

    def test_url_host_allowlist_default(self):
        # 默认白名单：QQ 图床系域名允许，其它 https 域名拒绝（防 SSRF 面扩大）
        assert is_allowed_image_url("https://multimedia.nt.qq.com.cn/a.jpg")
        assert not is_allowed_image_url("https://example.com/i.jpg")
        assert not is_allowed_image_url("https://169.254.169.254/x")

    def test_url_host_allowlist_empty_is_fail_closed(self, monkeypatch):
        """M3：显式置空**不再**等于「允许任意域名」。

        旧语义下 `AGENT_IMAGE_HOSTS=` 一个空环境变量即可整体关闭 SSRF 防护，
        现在回落默认白名单（fail-closed）。
        """
        monkeypatch.setenv("AGENT_IMAGE_HOSTS", "")
        assert not is_allowed_image_url("https://example.com/i.jpg")
        assert is_allowed_image_url("https://gchat.qpic.cn/x.jpg")

    def test_url_host_allowlist_blank_is_fail_closed(self, monkeypatch):
        monkeypatch.setenv("AGENT_IMAGE_HOSTS", "   ,  ,")
        assert not is_allowed_image_url("https://example.com/i.jpg")

    def test_allow_any_host_requires_explicit_optin(self, monkeypatch):
        """确需放开须显式开关，且仍需通过 IP 层校验。"""
        monkeypatch.setenv("AGENT_IMAGE_HOSTS", "")
        monkeypatch.delenv("AGENT_IMAGE_ALLOW_ANY_HOST", raising=False)
        assert not is_allowed_image_url("https://example.com/i.jpg")
        monkeypatch.setenv("AGENT_IMAGE_ALLOW_ANY_HOST", "1")
        assert is_allowed_image_url("https://example.com/i.jpg")

    def test_forbidden_ip_detection(self):
        """M3：内网/回环/链路本地/保留段一律判为不安全。"""
        from plugins.qq_agent_adapter.media import _is_forbidden_ip

        for ip in (
            "127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.1",
            "169.254.169.254",  # 云元数据
            "0.0.0.0", "::1", "fd00::1", "fe80::1",
        ):
            assert _is_forbidden_ip(ip), ip
        for ip in ("1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"):
            assert not _is_forbidden_ip(ip), ip
        assert _is_forbidden_ip("not-an-ip")  # 解析不出按不安全处理

    @pytest.mark.asyncio
    async def test_host_ips_are_safe_blocks_localhost(self, monkeypatch):
        """M3：域名解析到内网地址时拒绝（防 DNS rebinding / 元数据访问）。"""
        import plugins.qq_agent_adapter.media as M

        monkeypatch.setattr(
            M.asyncio, "to_thread",
            lambda fn, *a, **k: _async_return(
                [(2, 1, 6, "", ("169.254.169.254", 0))]
            ),
        )
        ok, why = await M.host_ips_are_safe("evil.example.com")
        assert not ok and "内网" in why

    @pytest.mark.asyncio
    async def test_host_ips_are_safe_allows_public(self, monkeypatch):
        import plugins.qq_agent_adapter.media as M

        monkeypatch.setattr(
            M.asyncio, "to_thread",
            lambda fn, *a, **k: _async_return([(2, 1, 6, "", ("93.184.216.34", 0))]),
        )
        ok, why = await M.host_ips_are_safe("example.com")
        assert ok, why

    def test_sniff_image_type(self):
        assert sniff_image_type(b"\xff\xd8\xff\xe0xxx") == "image/jpeg"
        assert sniff_image_type(b"\x89PNG\r\n\x1a\n" + b"0" * 8) == "image/png"
        assert sniff_image_type(b"GIF89a......") == "image/gif"
        assert sniff_image_type(b"RIFF1234WEBPVP8 ") == "image/webp"
        assert sniff_image_type(b"<html>") == ""

    def test_filename_ext(self):
        # content-type 优先于 url 后缀；url 后缀大小写不敏感
        assert _filename_for("https://x/a.PNG", "").endswith(".png")
        assert _filename_for("https://x/a.gif", "").endswith(".gif")
        assert _filename_for("https://x/noext", "").endswith(".jpg")
        assert _filename_for("https://x/a.jpg", "image/png").endswith(".png")

    def test_extract_media(self):
        ev = _Ev(
            [
                _Seg("text", {"text": "看看这张"}),
                _Seg("image", {"url": "https://gchat.qpic.cn/a.jpg", "file": "A.jpg"}),
                _Seg("image", {"file": "no-url.jpg"}),
                _Seg("face", {"id": "1"}),
            ]
        )
        items = extract_media(ev)
        assert len(items) == 2
        assert items[0].url == "https://gchat.qpic.cn/a.jpg"
        assert not items[1].url and items[1].file

    def test_media_item_available_respects_allowlist(self):
        assert MediaItem("image", url="https://gchat.qpic.cn/a.jpg").available()
        assert MediaItem("image", file="base64://AAAA").available()
        assert not MediaItem("image", url="http://gchat.qpic.cn/a.jpg").available()
        assert not MediaItem("image", url="https://example.com/a.jpg").available()
        assert not MediaItem("image", file="store/abc.dat").available()


class TestDownload:
    @pytest.mark.asyncio
    async def test_download_ok(self, tmp_path):
        body = b"\xff\xd8fakejpg"
        client = _FakeClient(_FakeResp(body, "image/jpeg"))
        # 注入：download_image 用 client.stream 获取内容
        saved = await download_image("https://gchat.qpic.cn/i.jpg", tmp_path, client=client)
        assert saved is not None and saved.is_file()
        assert saved.read_bytes() == body

    @pytest.mark.asyncio
    async def test_download_atomic_no_part_left(self, tmp_path):
        client = _FakeClient(_FakeResp(b"\xff\xd8\xffx", "image/jpeg"))
        saved = await download_image("https://gchat.qpic.cn/i.jpg", tmp_path, client=client)
        assert saved.is_file()
        assert not list(tmp_path.glob("*.part"))

    @pytest.mark.asyncio
    async def test_download_rejects_non_https(self, tmp_path):
        assert await download_image("http://gchat.qpic.cn/i.jpg", tmp_path) is None

    @pytest.mark.asyncio
    async def test_download_rejects_non_allowlisted_host(self, tmp_path):
        assert await download_image("https://example.com/i.jpg", tmp_path) is None

    @pytest.mark.asyncio
    async def test_download_oversize(self, tmp_path):
        from plugins.qq_agent_adapter.media import MAX_BYTES

        client = _FakeClient(_FakeResp(b"a" * (MAX_BYTES + 1), "image/jpeg"))
        assert await download_image("https://gchat.qpic.cn/big.jpg", tmp_path, client=client) is None

    @pytest.mark.asyncio
    async def test_download_non_image_type(self, tmp_path):
        client = _FakeClient(_FakeResp(b"<html></html>", "text/html"))
        assert await download_image("https://gchat.qpic.cn/i.jpg", tmp_path, client=client) is None

    @pytest.mark.asyncio
    async def test_missing_content_type_sniffed(self, tmp_path):
        # L17：缺失 content-type 时按魔数嗅探，嗅探不出才拒绝
        client = _FakeClient(_FakeResp(b"\x89PNG\r\n\x1a\n" + b"0" * 8, headers={}))
        saved = await download_image("https://gchat.qpic.cn/i", tmp_path, client=client)
        assert saved is not None and saved.name.endswith(".png")

    @pytest.mark.asyncio
    async def test_octet_stream_sniffed_or_rejected(self, tmp_path):
        ok = _FakeClient(_FakeResp(b"\xff\xd8\xffxx", "application/octet-stream"))
        assert await download_image("https://gchat.qpic.cn/i.jpg", tmp_path, client=ok) is not None
        bad = _FakeClient(_FakeResp(b"just text", "application/octet-stream"))
        assert await download_image("https://gchat.qpic.cn/i.jpg", tmp_path, client=bad) is None

    @pytest.mark.asyncio
    async def test_redirect_followed_when_allowlisted(self, tmp_path):
        body = b"\xff\xd8\xffok"
        client = _FakeClient(
            _FakeResp(b"", "text/html", status=302, headers={"content-type": "text/html", "location": "https://gchat.qpic.cn/real.jpg"}),
            _FakeResp(body, "image/jpeg"),
        )
        saved = await download_image("https://gchat.qpic.cn/i.jpg", tmp_path, client=client)
        assert saved is not None and saved.read_bytes() == body
        assert client.calls[1][1] == "https://gchat.qpic.cn/real.jpg"

    @pytest.mark.asyncio
    async def test_redirect_to_non_allowlisted_rejected(self, tmp_path):
        # M15：重定向逐跳重校验，https → 任意域名跳转不放行
        client = _FakeClient(
            _FakeResp(b"", "text/html", status=302, headers={"content-type": "text/html", "location": "https://evil.example.net/pw.jpg"}),
        )
        assert await download_image("https://gchat.qpic.cn/i.jpg", tmp_path, client=client) is None

    @pytest.mark.asyncio
    async def test_redirect_to_http_rejected(self, tmp_path):
        client = _FakeClient(
            _FakeResp(b"", "text/html", status=302, headers={"content-type": "text/html", "location": "http://gchat.qpic.cn/x.jpg"}),
        )
        assert await download_image("https://gchat.qpic.cn/i.jpg", tmp_path, client=client) is None


class TestMediaQuota:
    @pytest.mark.asyncio
    async def test_prune_evicts_oldest(self, tmp_path):
        import os

        from plugins.qq_agent_adapter.media import prune_media_dir

        d = tmp_path / "media"
        d.mkdir()
        old = d / "old.jpg"
        mid = d / "mid.jpg"
        new = d / "new.jpg"
        old.write_bytes(b"a" * 100)
        os.utime(old, (1, 1))
        mid.write_bytes(b"b" * 100)
        os.utime(mid, (2, 2))
        new.write_bytes(b"c" * 100)

        prune_media_dir(d, quota_bytes=250, incoming=50)
        assert not old.exists()      # 最旧被清
        assert mid.exists() and new.exists()

    @pytest.mark.asyncio
    async def test_quota_enforced_on_save(self, tmp_path):
        from plugins.qq_agent_adapter.media import save_image_atomic

        d = tmp_path / "media"
        save_image_atomic(d, "a.jpg", b"x" * 100, quota_bytes=150)
        save_image_atomic(d, "b.jpg", b"y" * 100, quota_bytes=150)
        names = sorted(p.name for p in d.iterdir())
        assert names == ["b.jpg"]  # a.jpg 被按最旧淘汰


class TestSegmentsHelpers:
    def test_media_and_text_from_segments(self):
        segs = [
            {"type": "text", "data": {"text": "看 "}},
            {"type": "image", "data": {"url": "https://gchat.qpic.cn/a.jpg"}},
            {"type": "image", "data": {"file": "x.jpg"}},
            {"type": "reply", "data": {"id": "99"}},
        ]
        imgs = media_from_segments(segs)
        assert len(imgs) == 2 and imgs[0].url == "https://gchat.qpic.cn/a.jpg"
        assert text_from_segments(segs) == "看"

    def test_seg_info_dict_and_obj(self):
        from plugins.qq_agent_adapter.media import _seg_info

        assert _seg_info({"type": "x", "data": {"k": 1}}) == ("x", {"k": 1})
        assert _seg_info(_Seg("text", {"text": "hi"})) == ("text", {"text": "hi"})

    def test_coerce_segments_cq_string(self):
        # M11：CQ 码字符串不再被 list(str) 拆成单字符
        segs = _coerce_segments("[CQ:image,file=a.jpg,url=https://gchat.qpic.cn/x.jpg]你好")
        assert len(segs) == 2
        imgs = media_from_segments(segs)
        assert imgs[0].url == "https://gchat.qpic.cn/x.jpg"
        assert text_from_segments(segs) == "你好"

    def test_coerce_segments_passthrough(self):
        assert _coerce_segments(None) == []
        assert _coerce_segments("") == []
        lst = [{"type": "text", "data": {"text": "a"}}]
        assert _coerce_segments(lst) is lst


class FakeBot:
    def __init__(self, quoted=None, forward=None):
        self._quoted = quoted
        self._forward = forward
        self.get_msg_kwargs = None
        self.get_forward_kwargs = None

    async def get_msg(self, **kwargs):
        self.get_msg_kwargs = kwargs
        return self._quoted

    async def get_forward_msg(self, **kwargs):
        self.get_forward_kwargs = kwargs
        return self._forward


class IdOnlyBot(FakeBot):
    """只接受 OneBot v11 标准参数名 id 的协议端（如严格实现）。"""

    async def get_forward_msg(self, **kwargs):
        self.get_forward_kwargs = kwargs
        if "id" not in kwargs:
            raise TypeError("unexpected keyword argument 'message_id'")
        return self._forward


class TestQuoteForward:
    @pytest.mark.asyncio
    async def test_quoted_image(self):
        bot = FakeBot(
            quoted={
                "message": [
                    {"type": "text", "data": {"text": "之前那张"}},
                    {"type": "image", "data": {"url": "https://gchat.qpic.cn/q.png"}},
                ]
            }
        )
        out = await resolve_quoted_media(bot, "1")
        assert out["text"] == "之前那张"
        assert out["images"][0].url == "https://gchat.qpic.cn/q.png"

    @pytest.mark.asyncio
    async def test_quoted_cq_string_body(self):
        bot = FakeBot(quoted={"message": "[CQ:image,file=a.jpg,url=https://gchat.qpic.cn/q.png]描述"})
        out = await resolve_quoted_media(bot, "1")
        assert out["text"] == "描述"
        assert out["images"][0].url == "https://gchat.qpic.cn/q.png"

    @pytest.mark.asyncio
    async def test_quoted_nested_data(self):
        bot = FakeBot(quoted={"data": {"message": [{"type": "text", "data": {"text": "嵌套"}}]}})
        out = await resolve_quoted_media(bot, "1")
        assert out["text"] == "嵌套"

    @pytest.mark.asyncio
    async def test_quoted_failure_graceful(self):
        class BoomBot(FakeBot):
            async def get_msg(self, **kwargs):
                raise RuntimeError("api down")

        out = await resolve_quoted_media(BoomBot(), "1")
        assert out == {"text": "", "images": []}

    @pytest.mark.asyncio
    async def test_forward_images_and_texts(self):
        bot = FakeBot(
            forward={
                "messages": [
                    {
                        "message": [
                            {"type": "text", "data": {"text": "第一句"}},
                            {"type": "image", "data": {"url": "https://gchat.qpic.cn/1.png"}},
                        ]
                    },
                    {"message": [{"type": "text", "data": {"text": "第二句"}}]},
                ]
            }
        )
        out = await resolve_forward_content(bot, "f1")
        assert out["count"] == 2
        assert out["shown"] == 2
        assert out["texts"] == ["第一句", "第二句"]
        assert out["images"][0].url == "https://gchat.qpic.cn/1.png"

    @pytest.mark.asyncio
    async def test_forward_list_form(self):
        bot = FakeBot(forward=[{"message": [{"type": "text", "data": {"text": "单条"}}]}])
        out = await resolve_forward_content(bot, "f1")
        assert out["texts"] == ["单条"]

    @pytest.mark.asyncio
    async def test_forward_count_is_total_not_truncated(self):
        # M6：count 必须是转发内消息总数，shown 才是摘录条数
        bot = FakeBot(
            forward={"messages": [{"message": [{"type": "text", "data": {"text": f"第{i}句"}}]} for i in range(20)]}
        )
        out = await resolve_forward_content(bot, "f1")
        assert out["count"] == 20
        assert out["shown"] == 15
        assert len(out["texts"]) == 15

    @pytest.mark.asyncio
    async def test_forward_uses_message_id_kwarg(self):
        # M4：默认实现接受 message_id
        bot = FakeBot(forward={"messages": []})
        await resolve_forward_content(bot, "42")
        assert "message_id" in bot.get_forward_kwargs

    @pytest.mark.asyncio
    async def test_forward_falls_back_to_id_kwarg(self):
        # M4：严格标准实现只接受 id，应自动回退而不是静默失败
        bot = IdOnlyBot(forward={"messages": [{"message": [{"type": "text", "data": {"text": "标准"}}]}]})
        out = await resolve_forward_content(bot, "42")
        assert "id" in bot.get_forward_kwargs
        assert out["texts"] == ["标准"]

    @pytest.mark.asyncio
    async def test_forward_cq_string_items(self):
        # M11：转发条目的 body 为 CQ 码字符串时也能解析
        bot = FakeBot(
            forward={"messages": [{"message": "[CQ:image,file=a.jpg,url=https://gchat.qpic.cn/x.jpg]卡片文字"}]}
        )
        out = await resolve_forward_content(bot, "f1")
        assert out["texts"] == ["卡片文字"]
        assert out["images"][0].url == "https://gchat.qpic.cn/x.jpg"


class TestM4LogRedaction:
    """M4：quoted 解析的 DEBUG 日志不得包含被引用消息的原始结构 / 图片 URL。"""

    @pytest.mark.asyncio
    async def test_debug_log_does_not_leak_url(self, caplog):
        import logging as _logging

        url = "https://gchat.qpic.cn/leak-this-secret-path.jpg"
        bot = FakeBot(
            quoted={"message": [{"type": "image", "data": {"url": url}},
                                {"type": "text", "data": {"text": "机密文字"}}]}
        )
        with caplog.at_level(_logging.DEBUG, logger="plugins.qq_agent_adapter.media"):
            out = await resolve_quoted_media(bot, "7")

        assert out["images"][0].url == url  # 功能不受影响
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert url not in blob, "日志泄漏了图片 URL"
        assert "机密文字" not in blob, "日志泄漏了被引用消息正文"
        assert "image" in blob  # 仍保留段类型统计便于排障


class TestL2DataUrlAsync:
    """L2：data URI 的 base64 编码不应阻塞事件循环。"""

    @pytest.mark.asyncio
    async def test_async_variant_matches_sync(self):
        from plugins.qq_agent_adapter.media import (
            data_url_from_bytes,
            data_url_from_bytes_async,
        )

        raw = b"\xff\xd8\xff" + b"a" * 64
        assert await data_url_from_bytes_async(raw, "image/jpeg") == data_url_from_bytes(raw, "image/jpeg")

    @pytest.mark.asyncio
    async def test_async_variant_runs_off_loop(self, monkeypatch):
        import plugins.qq_agent_adapter.media as M

        seen = {}

        async def fake_to_thread(fn, *a, **k):
            seen["threaded"] = True
            return fn(*a, **k)

        monkeypatch.setattr(M.asyncio, "to_thread", fake_to_thread)
        await M.data_url_from_bytes_async(b"\xff\xd8\xffxx", "image/jpeg")
        assert seen.get("threaded") is True
