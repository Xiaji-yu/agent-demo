import asyncio

import pytest

from agentcore.workspace import utils as wu
from plugins.qq_agent_adapter.media import (
    MediaItem,
    download_image,
    extract_media,
    is_allowed_image_url,
    media_from_segments,
    resolve_forward_content,
    resolve_quoted_media,
    text_from_segments,
    _filename_for,
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
    def __init__(self, body: bytes, content_type: str = "image/jpeg", status: int = 200):
        self.body = body
        self.headers = {"content-type": content_type}
        self.status_code = status

    async def aiter_bytes(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i : i + n]


class _FakeClient:
    """最小 httpx 兼容：stream() 返回 async context manager，产出预设响应。"""

    def __init__(self, resp):
        self._resp = resp

    def stream(self, method, url, **kw):
        self.last_url = url
        self.last_kw = kw
        return _StreamCtx(self._resp)


class _StreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class TestMediaUtils:
    def test_url_scheme_guard(self):
        assert is_allowed_image_url("https://gchat.qpic.cn/x.jpg?t=1")
        assert not is_allowed_image_url("http://x/1.jpg")
        assert not is_allowed_image_url("file:///etc/passwd")
        assert not is_allowed_image_url("")

    def test_filename_ext(self):
        assert _filename_for("https://x/a.PNG", "") == _filename_for("https://x/a.PNG", "").replace(".jpg", "")
        assert _filename_for("https://x/a.gif") .endswith(".gif")
        assert _filename_for("https://x/noext") .endswith(".jpg")
        # content-type 优先于 url 后缀
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


class TestDownload:
    @pytest.mark.asyncio
    async def test_download_ok(self, tmp_path):
        body = b"\xff\xd8fakejpg"
        client = _FakeClient(_FakeResp(body, "image/jpeg"))
        # 注入：download_image 用 client.stream 获取内容
        saved = await download_image("https://example.com/i.jpg", tmp_path, client=client)
        assert saved is not None and saved.is_file()
        assert saved.read_bytes() == body

    @pytest.mark.asyncio
    async def test_download_rejects_non_https(self, tmp_path):
        assert await download_image("http://x/i.jpg", tmp_path) is None

    @pytest.mark.asyncio
    async def test_download_oversize(self, tmp_path):
        from plugins.qq_agent_adapter.media import MAX_BYTES

        client = _FakeClient(_FakeResp(b"a" * (MAX_BYTES + 1), "image/jpeg"))
        assert await download_image("https://example.com/big.jpg", tmp_path, client=client) is None

    @pytest.mark.asyncio
    async def test_download_non_image_type(self, tmp_path):
        client = _FakeClient(_FakeResp(b"<html></html>", "text/html"))
        assert await download_image("https://example.com/i.jpg", tmp_path, client=client) is None


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


class FakeBot:
    def __init__(self, quoted=None, forward=None):
        self._quoted = quoted
        self._forward = forward

    async def get_msg(self, message_id):
        return self._quoted

    async def get_forward_msg(self, message_id):
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
    async def test_quoted_failure_graceful(self):
        class BoomBot(FakeBot):
            async def get_msg(self, message_id):
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
                            {"type": "image", "data": {"url": "https://x/1.png"}},
                        ]
                    },
                    {"message": [{"type": "text", "data": {"text": "第二句"}}]},
                ]
            }
        )
        out = await resolve_forward_content(bot, "f1")
        assert out["count"] == 2
        assert out["texts"] == ["第一句", "第二句"]
        assert out["images"][0].url == "https://x/1.png"

    @pytest.mark.asyncio
    async def test_forward_list_form(self):
        bot = FakeBot(forward=[{"message": [{"type": "text", "data": {"text": "单条"}}]}])
        out = await resolve_forward_content(bot, "f1")
        assert out["texts"] == ["单条"]
