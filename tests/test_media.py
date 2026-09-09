import asyncio

import pytest

from agentcore.workspace import utils as wu
from plugins.qq_agent_adapter.media import (
    MediaItem,
    download_image,
    extract_media,
    is_allowed_image_url,
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
