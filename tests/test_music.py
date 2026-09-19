"""点歌：歌源层测试（搜索解析 / 时长过滤 / 受控下载 / silk 编码）。

来源：AC-music-playback.md 的 A 组。
"""

import json

import pytest

from agentcore.music import download as dl
from agentcore.music import silk
from agentcore.music.client import (
    Song,
    filter_by_duration,
    parse_search,
    song_url,
)


# ---------- A1 搜索解析 ----------
class TestParseSearch:
    def _payload(self, songs):
        return {"code": 200, "result": {"songs": songs, "songCount": len(songs)}}

    def _song(self, sid="1", name="海阔天空", dur=240000):
        return {
            "id": sid,
            "name": name,
            "duration": dur,
            "artists": [{"name": "Beyond"}],
            "album": {"name": "华纳廿三周年纪念精选"},
        }

    def test_parses_fields(self):
        songs = parse_search(self._payload([self._song()]))
        assert len(songs) == 1
        s = songs[0]
        assert (s.id, s.name, s.artists, s.album) == (
            "1",
            "海阔天空",
            "Beyond",
            "华纳廿三周年纪念精选",
        )
        assert s.duration_ms == 240000
        assert s.duration_seconds == 240
        assert s.label == "海阔天空 - Beyond"

    def test_multiple_artists_joined(self):
        payload = self._payload(
            [
                {
                    "id": "9",
                    "name": "X",
                    "duration": 1,
                    "artists": [{"name": "A"}, {"name": "B"}],
                }
            ]
        )
        assert parse_search(payload)[0].artists == "A、B"

    def test_missing_result_returns_empty(self):
        """上游形状变化是常态，坏响应只该导致「没搜到」，而不是崩掉整个点歌。"""
        assert parse_search({"code": 200}) == []
        assert parse_search({}) == []
        assert parse_search(None) == []
        assert parse_search({"result": None}) == []
        assert parse_search({"result": "not-a-dict"}) == []

    def test_respects_limit(self):
        payload = self._payload([self._song(str(i)) for i in range(10)])
        assert len(parse_search(payload, limit=3)) == 3

    def test_drops_entries_without_id(self):
        payload = self._payload([self._song("1"), {"name": "无 id", "duration": 1}])
        assert [s.id for s in parse_search(payload)] == ["1"]

    def test_skips_malformed_entry_not_whole_response(self):
        payload = self._payload(
            [self._song("1"), {"id": "2", "name": "坏", "duration": "不是数字"}]
        )
        assert [s.id for s in parse_search(payload)] == ["1"]


# ---------- A2 时长过滤 ----------
class TestFilterByDuration:
    def _songs(self, *seconds):
        return [Song(str(i), f"s{i}", "", "", s * 1000) for i, s in enumerate(seconds)]

    def test_boundary_keeps_exact_limit(self):
        """上限是「最长可发时长」，正好 300s 必须保留 —— 用 > 不是 >=。"""
        kept = filter_by_duration(self._songs(240, 300, 301), 300)
        assert [s.duration_seconds for s in kept] == [240, 300]

    def test_all_too_long_returns_empty(self):
        assert filter_by_duration(self._songs(400, 500), 300) == []

    def test_zero_limit_disables_filter(self):
        songs = self._songs(240, 9999)
        assert len(filter_by_duration(songs, 0)) == 2

    def test_does_not_mutate_input(self):
        songs = self._songs(240, 9999)
        filter_by_duration(songs, 300)
        assert len(songs) == 2


# ---------- A3 取音频地址 ----------
class TestSongUrl:
    @pytest.mark.asyncio
    async def test_returns_url_and_size(self, monkeypatch):
        captured = {}

        async def fake_post(path, payload):
            captured.update(path=path, payload=payload)
            return {
                "code": 200,
                "data": [{"id": 1, "url": "https://x/a.mp3", "size": 481115}],
            }

        monkeypatch.setattr("agentcore.music.client._post", fake_post)
        got = await song_url("1")
        assert got == ("https://x/a.mp3", 481115)
        assert captured["path"] == "/song/url/v1"
        assert captured["payload"]["level"] == "standard"

    @pytest.mark.parametrize(
        "data",
        [
            None,
            {},
            {"data": []},
            {"data": [{"url": ""}]},
            {"data": ["not-a-dict"]},
            {"data": {}},
        ],
    )
    @pytest.mark.asyncio
    async def test_no_url_returns_none_not_empty_string(self, monkeypatch, data):
        """拿不到就明确返回 None，绝不返回空串让上层当成合法地址去下载。"""
        monkeypatch.setattr("agentcore.music.client._post", lambda *a, **k: _ret(data))
        assert await song_url("1") is None


async def _ret(value):
    return value


# ---------- A4 受控下载 ----------
class TestUrlValidation:
    """_validate 是同步部分：scheme + 域名白名单，不需要网络。"""

    def test_rejects_plain_http(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_AUDIO_HOSTS", "music.126.net")
        with pytest.raises(dl.UnsafeURLError, match="https"):
            dl._validate("http://m702.music.126.net/a.mp3")

    def test_rejects_host_outside_whitelist(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_AUDIO_HOSTS", "music.126.net")
        with pytest.raises(dl.UnsafeURLError, match="白名单"):
            dl._validate("https://evil.example.com/a.mp3")

    def test_rejects_suffix_lookalike(self, monkeypatch):
        """notmusic.126.net 只是后缀巧合，不能算命中白名单。"""
        monkeypatch.setenv("AGENT_MUSIC_AUDIO_HOSTS", "music.126.net")
        with pytest.raises(dl.UnsafeURLError, match="白名单"):
            dl._validate("https://notmusic.126.net/a.mp3")

    def test_accepts_whitelisted_host_and_subdomain(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_AUDIO_HOSTS", "music.126.net")
        dl._validate("https://music.126.net/a.mp3")
        dl._validate("https://m702.music.126.net/a.mp3")

    def test_rejects_url_without_host(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_AUDIO_HOSTS", "music.126.net")
        with pytest.raises(dl.UnsafeURLError, match="主机名"):
            dl._validate("https:///a.mp3")

    def test_empty_hosts_env_falls_back_to_default(self, monkeypatch):
        """显式置空不等于放开：回落默认白名单，避免一个空 env 关掉整条防护。"""
        monkeypatch.setenv("AGENT_MUSIC_AUDIO_HOSTS", "")
        assert dl.audio_hosts() == ["music.126.net"]


class TestForbiddenIp:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "10.0.0.1",
            "192.168.1.1",
            "172.16.0.1",
            "169.254.169.254",  # 云元数据
            "100.64.0.1",  # CGNAT，is_private 覆盖不到
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fe80::1",
            "不是IP",
            "",
        ],
    )
    def test_forbidden(self, ip):
        assert dl._is_forbidden_ip(ip) is True

    @pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.8.8", "223.5.5.5"])
    def test_public_allowed(self, ip):
        assert dl._is_forbidden_ip(ip) is False


class _FakeStream:
    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = headers
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self, n):
        if self._body:
            yield self._body


class _FakeClient:
    """按 URL 返回预定响应，用于把 HTTP 层与 DNS/真实网络解耦。

    M12（REVIEW-6ec3f7c..a36ea1d）：**kwargs 原样留存供用例断言
    ``follow_redirects=False``。旧实现把 kwargs 吞掉，于是"改坏这个参数"
    不会让任何用例失败 —— SSRF 逐跳复检的开关完全没有护栏。
    """

    def __init__(self, handler, **kwargs):
        self._handler = handler
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url):
        return self._handler(url)


@pytest.fixture
def no_dns(monkeypatch):
    """把 IP 校验打桩成通过，让用例只验 HTTP 层（scheme/host 已由 _validate 覆盖）。"""
    monkeypatch.setattr(dl, "_host_is_safe", _async_true)


async def _async_true(host):
    return True


class TestFetchAudio:
    URL = "https://m702.music.126.net/a.mp3"

    def _client(self, monkeypatch, handler):
        created: list[_FakeClient] = []

        def factory(**kw):
            c = _FakeClient(handler, **kw)
            created.append(c)
            return c

        monkeypatch.setattr(dl.httpx, "AsyncClient", factory)
        self._created = created
        return created

    @pytest.mark.asyncio
    async def test_downloads_audio(self, monkeypatch, no_dns, tmp_path):
        body = b"ID3fake-mp3-bytes"

        def handler(url):
            return _FakeStream(200, {"content-type": "audio/mpeg"}, body)

        self._client(monkeypatch, handler)
        dest = tmp_path / "a.mp3"
        await dl.fetch_audio(self.URL, dest)
        assert dest.read_bytes() == body

    @pytest.mark.asyncio
    async def test_rejects_non_audio_content_type(self, monkeypatch, no_dns, tmp_path):
        def handler(url):
            return _FakeStream(200, {"content-type": "text/html"}, b"<html>")

        self._client(monkeypatch, handler)
        with pytest.raises(dl.UnsafeURLError, match="content-type"):
            await dl.fetch_audio(self.URL, tmp_path / "a.mp3")

    @pytest.mark.asyncio
    async def test_rejects_missing_content_type(self, monkeypatch, no_dns, tmp_path):
        def handler(url):
            return _FakeStream(200, {}, b"x")

        self._client(monkeypatch, handler)
        with pytest.raises(dl.UnsafeURLError, match="content-type"):
            await dl.fetch_audio(self.URL, tmp_path / "a.mp3")

    @pytest.mark.asyncio
    async def test_rejects_oversized_content_length(
        self, monkeypatch, no_dns, tmp_path
    ):
        monkeypatch.setenv("AGENT_MUSIC_MAX_DOWNLOAD_MB", "1")

        def handler(url):
            return _FakeStream(
                200, {"content-type": "audio/mpeg", "content-length": "2097152"}, b""
            )

        self._client(monkeypatch, handler)
        with pytest.raises(dl.UnsafeURLError, match="上限"):
            await dl.fetch_audio(self.URL, tmp_path / "a.mp3")

    @pytest.mark.asyncio
    async def test_rejects_oversized_stream(self, monkeypatch, no_dns, tmp_path):
        """content-length 缺失或撒谎时，靠流式计数兜住，不能靠它自报。"""
        monkeypatch.setenv("AGENT_MUSIC_MAX_DOWNLOAD_MB", "1")

        async def chunks(n):
            for _ in range(40):
                yield b"x" * (64 * 1024)

        class Stream(_FakeStream):
            async def aiter_bytes(self, n):
                async for c in chunks(n):
                    yield c

        def handler(url):
            return Stream(200, {"content-type": "audio/mpeg"}, b"")

        self._client(monkeypatch, handler)
        with pytest.raises(dl.UnsafeURLError, match="上限"):
            await dl.fetch_audio(self.URL, tmp_path / "a.mp3")

    @pytest.mark.asyncio
    async def test_rejects_non_200(self, monkeypatch, no_dns, tmp_path):
        def handler(url):
            return _FakeStream(403, {"content-type": "audio/mpeg"}, b"")

        self._client(monkeypatch, handler)
        with pytest.raises(RuntimeError, match="403"):
            await dl.fetch_audio(self.URL, tmp_path / "a.mp3")

    @pytest.mark.asyncio
    async def test_rejects_redirect_escaping_whitelist(
        self, monkeypatch, no_dns, tmp_path
    ):
        """重定向必须逐跳复检：第一跳在白名单、第二跳跳去别处要拒。"""

        def handler(url):
            if url == self.URL:
                return _FakeStream(
                    302,
                    {
                        "location": "https://evil.example.com/a.mp3",
                        "content-type": "audio/mpeg",
                    },
                    b"",
                )
            raise AssertionError(f"不应请求白名单外地址：{url}")

        self._client(monkeypatch, handler)
        with pytest.raises(dl.UnsafeURLError, match="白名单"):
            await dl.fetch_audio(self.URL, tmp_path / "a.mp3")

    @pytest.mark.asyncio
    async def test_follows_redirect_within_whitelist(
        self, monkeypatch, no_dns, tmp_path
    ):
        second = "https://m9.music.126.net/b.mp3"
        body = b"audio"

        def handler(url):
            if url == self.URL:
                return _FakeStream(
                    302, {"location": second, "content-type": "audio/mpeg"}, b""
                )
            return _FakeStream(200, {"content-type": "audio/mpeg"}, body)

        self._client(monkeypatch, handler)
        dest = tmp_path / "a.mp3"
        await dl.fetch_audio(self.URL, dest)
        assert dest.read_bytes() == body

    @pytest.mark.asyncio
    async def test_rejects_empty_body(self, monkeypatch, no_dns, tmp_path):
        def handler(url):
            return _FakeStream(200, {"content-type": "audio/mpeg"}, b"")

        self._client(monkeypatch, handler)
        with pytest.raises(RuntimeError, match="为空"):
            await dl.fetch_audio(self.URL, tmp_path / "a.mp3")

    @pytest.mark.asyncio
    async def test_rejects_too_many_redirects(self, monkeypatch, no_dns, tmp_path):
        def handler(url):
            return _FakeStream(
                302, {"location": url + "?x=1", "content-type": "audio/mpeg"}, b""
            )

        self._client(monkeypatch, handler)
        with pytest.raises(dl.UnsafeURLError, match="重定向"):
            await dl.fetch_audio(self.URL, tmp_path / "a.mp3")

    @pytest.mark.asyncio
    async def test_blocks_when_dns_resolves_to_private_ip(self, monkeypatch, tmp_path):
        """域名白名单之外的第二道闸：解析到内网/回环一律拒绝（防 DNS rebinding）。"""
        monkeypatch.setattr(dl, "_host_is_safe", _async_false)
        with pytest.raises(dl.UnsafeURLError, match="IP 校验"):
            await dl.fetch_audio(self.URL, tmp_path / "a.mp3")


async def _async_false(host):
    return False


# ---------- A5 silk 编码 ----------
@pytest.mark.skipif(not silk.silk_available()[0], reason="pysilk 或 ffmpeg 不可用")
class TestSilkEncode:
    def _wav(self, tmp_path, seconds=3):
        import subprocess

        src = tmp_path / "a.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={seconds}",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-acodec",
                "pcm_s16le",
                str(src),
            ],
            check=True,
            capture_output=True,
        )
        return src

    @pytest.mark.asyncio
    async def test_header_is_qq_silk(self, tmp_path):
        """产物必须以 \x02#!SILK_V3（10 字节）开头，否则实现侧不会认、会再转一次。"""
        out = await silk.encode_to_silk(self._wav(tmp_path))
        assert out.startswith(silk.SILK_HEADER)
        assert out[:10] == b"\x02#!SILK_V3"
        assert len(silk.SILK_HEADER) == 10
        assert len(out) > 100

    @pytest.mark.asyncio
    async def test_roundtrip_decode_recovers_pcm(self, tmp_path):
        """解码回来应是裸 PCM（24 kHz 单声道 16-bit ≈ 48000 字节/秒）。

        pysilk.decode 输出的是原始 PCM 而不是 WAV 容器，要包成 wav 得自己写头
        （参考 astrbot 的 tencent_record_helper）。这里只断言裸 PCM 的量级。
        """
        import io

        import pysilk

        seconds = 3
        out = await silk.encode_to_silk(self._wav(tmp_path, seconds))
        dec = io.BytesIO()
        # 顶层 0x02 是 QQ 容器前缀，pysilk 解码要剥掉
        pysilk.decode(io.BytesIO(out[1:]), dec, 24000)
        expected = seconds * 24000 * 2
        assert expected * 0.8 < dec.tell() < expected * 1.2, (
            f"解码出 {dec.tell()} 字节，期望约 {expected}"
        )

    @pytest.mark.asyncio
    async def test_size_scales_with_duration_not_content(self, tmp_path):
        """silk 速率基本只由时长决定，与音频内容无关（实测 2.1~2.3 KB/s）。"""
        short = await silk.encode_to_silk(self._wav(tmp_path, 2))
        long_ = await silk.encode_to_silk(self._wav(tmp_path, 8))
        ratio = len(long_) / len(short)
        assert 2.5 < ratio < 5.5, f"8s/2s 体积比 {ratio:.2f} 偏离时长比例过多"

    @pytest.mark.asyncio
    async def test_encodes_at_wav_actual_rate_not_constant(self, tmp_path, monkeypatch):
        """编码速率必须跟随 wav 文件头的真实采样率。

        若实现改用模块常量编码（与数据实际速率脱钩），产物会变速变调。
        这里捕获传给编码器的速率，与 ffmpeg 写出的 wav 头比对。
        """
        import agentcore.music.silk as silk_mod

        captured = {}
        real = silk_mod._encode_pcm_to_silk

        def spy(pcm, rate):
            captured["rate"] = rate
            return real(pcm, rate)

        monkeypatch.setattr(silk_mod, "_encode_pcm_to_silk", spy)
        await silk.encode_to_silk(self._wav(tmp_path, 2))
        assert captured["rate"] == 24000, (
            f"编码速率 {captured['rate']} 应等于 wav 实际采样率 24000"
        )

    @pytest.mark.asyncio
    async def test_rejects_empty_pcm_source(self, tmp_path):
        bad = tmp_path / "empty.mp3"
        bad.write_bytes(b"")
        with pytest.raises(RuntimeError):
            await silk.encode_to_silk(bad)


def test_parse_search_is_pure_json_helper():
    """parse_search 不该依赖网络：纯 JSON → 对象。"""
    data = json.loads(
        '{"code":200,"result":{"songs":[{"id":"7","name":"n","duration":1000}]}}'
    )
    assert [s.id for s in parse_search(data)] == ["7"]


# ---------- C1-C5 OneBot HTTP 发送 ----------
class _FakeResp:
    def __init__(self, status=200, payload=None, error=None):
        self._status = status
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error
        if self._status >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"HTTP {self._status}", request=None, response=None
            )

    def json(self):
        return self._payload


class _FakeHttpPostClient:
    """记录请求并按脚本返回响应/异常，把 HTTP 层与真实网络解耦。"""

    def __init__(self, script, **kwargs):
        self._script = script
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        result = self._script
        if isinstance(result, BaseException):
            raise result
        return _FakeResp(payload=result)


@pytest.fixture
def ob_env(monkeypatch):
    monkeypatch.setenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000/")
    monkeypatch.setenv("NAPCAT_HTTP_TOKEN", "secret-token")


class TestSendGroupVoice:
    def _client(self, monkeypatch, script):
        import agentcore.music.sender as sender

        client = _FakeHttpPostClient(script)
        monkeypatch.setattr(sender.httpx, "AsyncClient", lambda **kw: client)
        return client

    @pytest.mark.asyncio
    async def test_sends_record_segment_with_base64_uri(self, monkeypatch, ob_env):
        """C1/C2：载荷必须是 record 段且 file 用 ``base64://`` 前缀。

        ``data:`` URI 形式会被实现侧当本地路径 stat，报 ENAMETOOLONG
        （SnowLuma issue #236）——这个前缀是实测结论，锁死。
        """
        import agentcore.music.sender as sender

        client = self._client(monkeypatch, {"status": "ok", "message_id": 4242})
        status = await sender.send_group_voice(456, b"\x02#!SILK_V3payload")
        assert status == sender.SEND_OK
        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["url"] == "http://127.0.0.1:3000/send_group_msg"
        assert call["json"]["group_id"] == 456
        (seg,) = call["json"]["message"]
        assert seg["type"] == "record"
        file_value = seg["data"]["file"]
        assert file_value.startswith("base64://"), (
            f"必须用 base64:// 前缀：{file_value[:20]}"
        )
        assert "data:" not in file_value[:10]
        # 载荷确实是原字节的 base64
        import base64

        assert (
            base64.b64decode(file_value[len("base64://") :]) == b"\x02#!SILK_V3payload"
        )

    @pytest.mark.asyncio
    async def test_sends_bearer_token_header(self, monkeypatch, ob_env):
        import agentcore.music.sender as sender

        client = self._client(monkeypatch, {"status": "ok", "message_id": 1})
        await sender.send_group_voice(456, b"x")
        assert client.calls[0]["headers"]["Authorization"] == "Bearer secret-token"

    @pytest.mark.asyncio
    async def test_no_token_omits_auth_header(self, monkeypatch):
        import agentcore.music.sender as sender

        monkeypatch.setenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
        monkeypatch.delenv("NAPCAT_HTTP_TOKEN", raising=False)
        client = self._client(monkeypatch, {"status": "ok", "message_id": 1})
        await sender.send_group_voice(456, b"x")
        assert "Authorization" not in client.calls[0]["headers"]

    @pytest.mark.asyncio
    async def test_uncertain_error_returns_uncertain(self, monkeypatch, ob_env):
        """C4：超时属于结果不确定——请求可能已送达，绝不当作失败去重发。"""
        import httpx

        import agentcore.music.sender as sender

        self._client(monkeypatch, httpx.TimeoutException("timed out"))
        status = await sender.send_group_voice(456, b"x")
        assert status == sender.SEND_UNCERTAIN

    @pytest.mark.asyncio
    async def test_uncertain_reuses_repo_wide_criterion(self, monkeypatch, ob_env):
        """C4 的判据必须复用 file_sender.is_uncertain_send_error，不能另起炉灶。"""

        import agentcore.music.sender as sender

        self._client(monkeypatch, TimeoutError())
        assert await sender.send_group_voice(456, b"x") == sender.SEND_UNCERTAIN

    @pytest.mark.asyncio
    async def test_http_status_error_returns_failed(self, monkeypatch, ob_env):
        import httpx

        import agentcore.music.sender as sender

        err = httpx.HTTPStatusError("500", request=None, response=None)
        self._client(monkeypatch, err)
        assert await sender.send_group_voice(456, b"x") == sender.SEND_FAILED

    @pytest.mark.asyncio
    async def test_business_failure_returns_failed(self, monkeypatch, ob_env):
        """envelope 既非 ok 也无 message_id → 业务失败，不能当成功。"""
        import agentcore.music.sender as sender

        self._client(monkeypatch, {"status": "async", "wording": "提交中"})
        assert await sender.send_group_voice(456, b"x") == sender.SEND_FAILED

    @pytest.mark.asyncio
    async def test_message_id_alone_counts_as_ok(self, monkeypatch, ob_env):
        """有的实现只回 message_id 不回 status=ok，也算送达。"""
        import agentcore.music.sender as sender

        self._client(monkeypatch, {"message_id": 777})
        assert await sender.send_group_voice(456, b"x") == sender.SEND_OK

    @pytest.mark.asyncio
    async def test_non_json_response_returns_failed(self, monkeypatch, ob_env):
        import agentcore.music.sender as sender

        self._client(monkeypatch, "not-a-dict")
        assert await sender.send_group_voice(456, b"x") == sender.SEND_FAILED

    @pytest.mark.asyncio
    async def test_unconfigured_url_returns_failed(self, monkeypatch):
        """没配 HTTP 地址时明确失败，而不是把请求打到无意义的地方。"""
        import agentcore.music.sender as sender

        monkeypatch.delenv("NAPCAT_HTTP_URL", raising=False)
        self._client(monkeypatch, {"status": "ok", "message_id": 1})
        assert await sender.send_group_voice(456, b"x") == sender.SEND_FAILED

    @pytest.mark.asyncio
    async def test_group_id_coerced_to_int(self, monkeypatch, ob_env):
        """OneBot 的 group_id 必须是数字，字符串 "456" 有些实现会拒。"""
        import agentcore.music.sender as sender

        client = self._client(monkeypatch, {"status": "ok", "message_id": 1})
        await sender.send_group_voice("456", b"x")
        assert client.calls[0]["json"]["group_id"] == 456
        assert isinstance(client.calls[0]["json"]["group_id"], int)


# ---------- E2 依赖探测 ----------
class TestSilkAvailable:
    def test_reports_missing_ffmpeg(self, monkeypatch):
        import agentcore.music.silk as silk_mod

        monkeypatch.setattr(silk_mod.shutil, "which", lambda name: None)
        ok, reason = silk_mod.silk_available()
        assert ok is False
        assert "ffmpeg" in reason

    def test_reports_missing_pysilk(self, monkeypatch):
        """pysilk 缺失要报 pysilk——**与宿主有没有 ffmpeg 无关**。

        M1（REVIEW-6ec3f7c..a36ea1d）：旧用例只桩掉 `__import__`，而
        `silk_available()` 先探测 ffmpeg 并提前返回，于是 CI（无 ffmpeg）下
        reason 是 "ffmpeg 不在 PATH" → 断言失败。这里把 ffmpeg 一起桩成"存在"，
        让用例只测 pysilk 这一条分支。
        """
        import builtins

        import agentcore.music.silk as silk_mod

        monkeypatch.setattr(silk_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        real_import = builtins.__import__

        def no_pysilk(name, *a, **kw):
            if name == "pysilk":
                raise ImportError("No module named 'pysilk'")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", no_pysilk)
        ok, reason = silk_mod.silk_available()
        assert ok is False
        assert "pysilk" in reason

    def test_route_missing_deps_includes_silk_reason(self, monkeypatch):
        """ffmpeg/pysilk 缺失时，_missing_deps 要把它列为不注册的原因之一（E2）。"""
        import importlib

        mr = importlib.import_module("plugins.qq_agent_adapter.music_route")
        monkeypatch.setenv("AGENT_MUSIC_API_URL", "http://127.0.0.1:16300")
        monkeypatch.setenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
        # music_route 是 from-import 绑定，patch 必须打在使用方模块上
        monkeypatch.setattr(mr, "silk_available", lambda: (False, "ffmpeg 不在 PATH"))
        missing = mr._missing_deps()
        assert any("ffmpeg" in m for m in missing)


# ==========================================================================
# M12（REVIEW-6ec3f7c..a36ea1d）：`follow_redirects=False` 是 SSRF「逐跳复检」的开关
#
# 它一旦被改成 True，httpx 会在内部静默跟随重定向，_validate 的逐跳复检被彻底
# 绕过（可跳向内网）——而旧用例从不看这个参数，改坏后 69 passed 全绿。
# ==========================================================================


class TestRedirectFollowingIsDisabled:
    URL = "https://m702.music.126.net/a.mp3"

    @pytest.mark.asyncio
    async def test_client_is_created_with_follow_redirects_false(
        self, monkeypatch, no_dns, tmp_path
    ):
        created = []

        def handler(url):
            return _FakeStream(200, {"content-type": "audio/mpeg"}, b"audio")

        def factory(**kw):
            c = _FakeClient(handler, **kw)
            created.append(c)
            return c

        monkeypatch.setattr(dl.httpx, "AsyncClient", factory)
        await dl.fetch_audio(self.URL, tmp_path / "a.mp3")
        assert created, "未创建 client"
        # 必须是**显式** False（键存在），而不是没传、靠 httpx 默认值
        assert "follow_redirects" in created[0].kwargs, "参数必须显式传入"
        assert created[0].kwargs["follow_redirects"] is False, (
            f"必须 follow_redirects=False（否则逐跳复检被绕过）：{created[0].kwargs}"
        )


# ==========================================================================
# L15（REVIEW-6ec3f7c..a36ea1d）：AC A5 的"60s 编码 < 2s"性能声称没有护栏
# ==========================================================================


@pytest.mark.skipif(not silk.silk_available()[0], reason="pysilk 或 ffmpeg 不可用")
class TestSilkPerformance:
    def _wav(self, tmp_path, seconds):
        import subprocess

        src = tmp_path / f"p{seconds}.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={seconds}",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-acodec",
                "pcm_s16le",
                str(src),
            ],
            check=True,
            capture_output=True,
        )
        return src

    @pytest.mark.asyncio
    async def test_sixty_seconds_encodes_under_two_seconds(self, tmp_path):
        """AC A5 的性能断言（实测 ≈1.2s，上限 2s 留了余量）。"""
        import time

        src = self._wav(tmp_path, 60)
        t0 = time.perf_counter()
        out = await silk.encode_to_silk(src)
        elapsed = time.perf_counter() - t0
        assert len(out) > 1000
        assert elapsed < 2.0, f"60s 编码耗时 {elapsed:.2f}s 超过 2s 预算"


class TestDurationBoundarySubSecond:
    """L11：毫秒语义——亚秒边界必须能区分（整秒值下两种实现等价）。"""

    def _songs(self, *ms):
        from agentcore.music.client import Song

        return [Song(str(i), f"s{i}", "", "", v) for i, v in enumerate(ms)]

    def test_300_9s_is_rejected_for_300_limit(self):
        """300.9s 必须被拒；旧实现先 //1000 取整成 300 会误放行。"""
        from agentcore.music.client import filter_by_duration

        assert filter_by_duration(self._songs(300_000), 300) != []
        assert filter_by_duration(self._songs(300_900), 300) == []
        assert filter_by_duration(self._songs(300_001), 300) == []

    def test_299_999ms_is_kept(self):
        from agentcore.music.client import filter_by_duration

        assert len(filter_by_duration(self._songs(299_999), 300)) == 1


class TestFfmpegHasTimeout:
    """L9：ffmpeg 子进程必须有墙钟上限（否则坏文件永久占住工作线程）。"""

    @pytest.mark.asyncio
    async def test_subprocess_run_receives_timeout(self, monkeypatch, tmp_path):
        import agentcore.music.silk as silk_mod

        seen = {}

        class _R:
            returncode = 0
            stderr = ""

        def fake_run(cmd, **kw):
            seen.update(kw)
            seen["cmd"] = cmd
            return _R()

        # 让前置的 wav 解析直接抛错（我们只关心 run 的调用参数）
        # 依赖探测必须打桩：否则 CI（无 ffmpeg）下 encode_to_silk 在入口就
        # 抛"不可用"，根本走不到 subprocess.run——本用例会假失败（同 M1 的教训）
        monkeypatch.setattr(silk_mod, "silk_available", lambda: (True, ""))
        monkeypatch.setattr(silk_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            silk_mod,
            "_pcm_from_wav",
            lambda p: (_ for _ in ()).throw(RuntimeError("stop")),
        )
        src = tmp_path / "a.mp3"
        src.write_bytes(b"x")
        with pytest.raises(RuntimeError, match="stop"):
            await silk_mod.encode_to_silk(src)
        assert "timeout" in seen, f"subprocess.run 未收到 timeout：{sorted(seen)}"
        assert seen["timeout"] == silk_mod._FFMPEG_TIMEOUT
        assert "-nostdin" in seen["cmd"], "仍须用 argv 列表而非 shell 拼接"
