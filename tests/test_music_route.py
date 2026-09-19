"""点歌：触发规则 + 歌名校验（防误触）测试。

来源：AC-music-playback.md 的 D 组与 G 组。
"""

import importlib

import pytest

# 用 import_module 而不是 `from package import music_route`：__init__.py 里有
# `music_route = None` 作为惰性加载哨兵（与 matcher/admin 同款），会把子模块名
# 遮蔽掉，from-import 拿到的是 None。
mr = importlib.import_module("plugins.qq_agent_adapter.music_route")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """默认：唤醒词就位，音乐 env 未配（未配置是本文件的常态基线）。"""
    monkeypatch.setenv("AGENT_WAKE_WORDS", "云崽")
    for key in (
        "AGENT_MUSIC_API_URL",
        "NAPCAT_HTTP_URL",
        "AGENT_MUSIC_ALLOWED_GROUPS",
        "AGENT_MUSIC_COMMANDS",
        "AGENT_MUSIC_MAX_SECONDS",
        "AGENT_MUSIC_COOLDOWN",
    ):
        monkeypatch.delenv(key, raising=False)


def _group_event(text, *, group_id=456, self_id=0, user_id=123, to_me=False):
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    segments = [{"type": "text", "data": {"text": text}}]
    return GroupMessageEvent.parse_obj(
        {
            "time": 0,
            "self_id": self_id,
            "post_type": "message",
            "sub_type": "group",
            "user_id": user_id,
            "message_type": "group",
            "message_id": 1,
            "group_id": group_id,
            "message": segments,
            "original_message": segments,
            "raw_message": text,
            "font": 0,
            "sender": {"user_id": user_id, "nickname": "", "card": ""},
            "to_me": to_me,
            "reply": None,
        }
    )


# ---------- D1 第二层：子命令 ----------
class TestParseCommand:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("云崽 点歌 海阔天空", ("点歌", "海阔天空")),
            ("云崽放歌  光年之外", ("放歌", "光年之外")),
            (
                "云崽点歌 海阔天空",
                ("点歌", "海阔天空"),
            ),  # 唤醒词后无空格：剥词必须照常生效
            ("云崽 点歌：海阔天空", ("点歌", "海阔天空")),
            ("云崽 点歌，周杰伦 晴天", ("点歌", "周杰伦 晴天")),
            ("云崽 点歌", ("点歌", "")),
            ("云崽 放歌", ("放歌", "")),
        ],
    )
    def test_matches(self, text, expected):
        assert mr.parse_command(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "云崽 你觉得周杰伦哪首歌好听",  # 剥词后不是子命令开头 → 落普通聊天
            "云崽 你好呀",
            "云崽 推荐点歌",
            "云崽 帮我点一首海阔天空",  # 子命令不在开头
            "云崽",
            "",
        ],
    )
    def test_does_not_match(self, text):
        assert mr.parse_command(text) is None

    def test_command_aliases_configurable(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_COMMANDS", "来首歌,播放")
        assert mr.music_commands() == ["来首歌", "播放"]
        assert mr.parse_command("云崽 播放 海阔天空") == ("播放", "海阔天空")
        assert mr.parse_command("云崽 点歌 海阔天空") is None

    def test_default_commands(self):
        assert mr.music_commands() == ["点歌", "放歌"]


# ---------- D1 两层条件合一 ----------
class TestRule:
    def _set_whitelist(self, monkeypatch, groups="456"):
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", groups)

    def test_wake_word_plus_command_matches(self, monkeypatch):
        self._set_whitelist(monkeypatch)
        assert mr._rule(_group_event("云崽 点歌 海阔天空")) is True

    def test_at_bot_plus_command_matches(self, monkeypatch):
        self._set_whitelist(monkeypatch)
        event = _group_event(
            "",
            self_id=10001,
            to_me=True,
        )
        # at 段在 _plain_text 里被忽略，只剩子命令文本
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        segments = [
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "text", "data": {"text": " 点歌 海阔天空"}},
        ]
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
                "message": segments,
                "original_message": segments,
                "raw_message": "",
                "font": 0,
                "sender": {"user_id": 123, "nickname": "", "card": ""},
                "to_me": True,
                "reply": None,
            }
        )
        assert mr._rule(event) is True

    def test_command_without_wake_word_rejected_in_group(self, monkeypatch):
        """路由不变量（AGENTS.md:103）：群里没有唤醒词/@bot 就不能触发。

        这正是「点歌不是被禁的 ai/!ai//ai 式触发前缀」的保证。
        """
        self._set_whitelist(monkeypatch)
        assert mr._rule(_group_event("点歌 海阔天空")) is False

    def test_wake_word_without_command_rejected(self, monkeypatch):
        self._set_whitelist(monkeypatch)
        assert mr._rule(_group_event("云崽 你觉得周杰伦哪首歌好听")) is False

    def test_group_not_in_whitelist_rejected(self, monkeypatch):
        self._set_whitelist(monkeypatch, groups="999")
        assert mr._rule(_group_event("云崽 点歌 海阔天空", group_id=456)) is False

    def test_empty_whitelist_rejects_all(self, monkeypatch):
        assert mr._rule(_group_event("云崽 点歌 海阔天空")) is False


# ---------- G2 歌名校验 ----------
class TestInvalidSongName:
    @pytest.mark.parametrize(
        "name",
        [
            "",
            "   ",
            "你喜欢听什么歌",
            "这首歌叫什么",
            "推荐点歌",
            "来一首",
            "？？？",
            "???",
            "周杰伦哪首歌最好听",
            "怎么点歌",
            "为什么放不了",
            "有哪些好听的",
            "多少首",
            "随便",
            "歌",
            "音乐",
            "放一首",
            "来点",
            "好不好听",
            "x" * 51,
            "海阔天空\n下一行",
            "海阔天空\x00",
        ],
    )
    def test_rejected(self, name):
        assert mr.invalid_song_name(name) != "", f"应当拒绝：{name!r}"

    @pytest.mark.parametrize(
        "name",
        [
            "海阔天空",
            "周杰伦 晴天",
            "光年之外",
            "邓紫棋",
            "Yesterday Once More",
            "x" * 50,  # 边界：正好 50 字应通过
        ],
    )
    def test_accepted(self, name):
        assert mr.invalid_song_name(name) == "", f"应当通过：{name!r}"

    def test_reason_is_specific(self):
        """拒绝理由要能说清是哪条，用户才知道怎么改。"""
        assert "歌名" in mr.invalid_song_name("")
        assert "问号" in mr.invalid_song_name("什么?")
        assert "笼统" in mr.invalid_song_name("随便")


# ---------- F2 条件注册 ----------
class _FakeMatcher:
    """假 matcher：只需支持 ``@matcher.handle()`` 这个装饰器用法。"""

    def handle(self):
        def deco(fn):
            return fn

        return deco


@pytest.fixture
def clean_module(monkeypatch):
    """把模块重置到「未配置 → 未注册」基线。

    两处坑：① ``importlib.reload`` 会重执行模块体的 ``from nonebot import on_message``，
    所以补丁必须打在 ``nonebot.on_message`` 上而不是模块属性上，否则会被冲掉；
    ② reload 在同一命名空间里重执行，**不会删除**上一次残留的 ``music_matcher``
    属性，所以显式 pop，避免用例之间互相污染。
    """
    monkeypatch.delenv("AGENT_MUSIC_API_URL", raising=False)
    monkeypatch.delenv("NAPCAT_HTTP_URL", raising=False)
    monkeypatch.setattr("nonebot.on_message", lambda **kw: _FakeMatcher())
    importlib.reload(mr)
    mr.__dict__.pop("music_matcher", None)
    yield
    importlib.reload(mr)
    mr.__dict__.pop("music_matcher", None)


class TestConditionalRegistration:
    def test_not_registered_when_env_missing(self, clean_module, monkeypatch):
        """没配 API / OneBot HTTP 就不注册 matcher——消息落给普通聊天，核心零影响。"""
        calls = []
        monkeypatch.setattr(
            "nonebot.on_message", lambda **kw: calls.append(kw) or _FakeMatcher()
        )
        importlib.reload(mr)
        assert calls == []
        assert not hasattr(mr, "music_matcher")

    def test_registered_with_higher_priority_and_block(self, clean_module, monkeypatch):
        """priority 必须小于 chat_matcher 的 10（更先执行）且 block=True。"""
        monkeypatch.setenv("AGENT_MUSIC_API_URL", "http://127.0.0.1:16300")
        monkeypatch.setenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
        captured = {}

        def fake_on_message(**kw):
            captured.update(kw)
            return _FakeMatcher()

        # 打在源模块上：reload 会重执行 `from nonebot import on_message`
        monkeypatch.setattr("nonebot.on_message", fake_on_message)
        # M1：注册**参数**的断言不该依赖宿主是否装了 ffmpeg（CI 上没有）。
        # 桩必须打在**源模块**上：reload 会重执行 `from agentcore.music.silk import
        # silk_available`，打在 mr 上会被重新绑定覆盖掉。
        monkeypatch.setattr("agentcore.music.silk.silk_available", lambda: (True, ""))
        importlib.reload(mr)
        assert captured, "应当注册了 matcher"
        assert captured["priority"] < 10, "必须比 chat_matcher(priority=10) 更先执行"
        assert captured["block"] is True, "命中即阻止普通聊天，避免同一条消息回两遍"
        assert captured["rule"] is mr._rule

    def test_missing_dependency_lists_reasons(self, clean_module, monkeypatch):
        """缺依赖要能说清缺什么，运维才知道该配哪个 env 或装哪个包。"""
        monkeypatch.delenv("AGENT_MUSIC_API_URL", raising=False)
        monkeypatch.delenv("NAPCAT_HTTP_URL", raising=False)
        missing = mr._missing_deps()
        assert any("AGENT_MUSIC_API_URL" in m for m in missing)
        assert any("NAPCAT_HTTP_URL" in m for m in missing)


# ---------- 配置 ----------
class TestConfig:
    def test_max_seconds_default(self):
        assert mr.max_seconds() == 300

    def test_max_seconds_configurable(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_MAX_SECONDS", "600")
        assert mr.max_seconds() == 600

    def test_max_seconds_bad_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_MAX_SECONDS", "五分钟")
        assert mr.max_seconds() == 300

    def test_max_seconds_negative_falls_back_to_default(self, monkeypatch, caplog):
        """L10：负值必须回退默认，而不是变成 0（0 = 不过滤 → 静默关掉上限）。

        旧实现 `max(0, int(raw))` 把 -1 变成 0，一个手滑的负号就 fail-open
        关掉了 5 分钟上限；这与 cooldown_seconds() 的负值回退语义也相反。
        """
        import logging

        monkeypatch.setenv("AGENT_MUSIC_MAX_SECONDS", "-1")
        with caplog.at_level(logging.WARNING):
            assert mr.max_seconds() == 300
        assert "-1" in caplog.text

    def test_max_seconds_zero_means_unlimited(self, monkeypatch):
        """0 仍是"不限制"（显式选择，与负值的手滑区分开）。"""
        monkeypatch.setenv("AGENT_MUSIC_MAX_SECONDS", "0")
        assert mr.max_seconds() == 0


# ==========================================================================
# REVIEW-6ec3f7c..a36ea1d 修复回归
#   M9 私聊 ACL + 「能否发送」必须先于下载/冷却
#   M10 song.id 路径校验    M11 缓存命中不下载 + 磁盘清理
#   L10 负值语义    M8 AGENT_MUSIC_CACHE_MB 真的被读取
#   L18 编排顺序 / SilkCache / 注册闸门（旧实现这些变异全部存活）
# ==========================================================================


def _group_ev(group_id=456, user_id=12345):
    """**真实** GroupMessageEvent。

    线上事故的教训：旧替身自己定义了 ``async def reply`` 方法，而真实 OneBot v11
    事件的 ``reply`` 是「引用消息」数据字段（普通消息为 None）—— 于是
    ``event.reply(...)`` 在生产必崩（TypeError），测试却全绿。本文件不再用鸭子
    类型替身，一律构造真实事件对象。
    """
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    segs = [{"type": "text", "data": {"text": "点歌"}}]
    return GroupMessageEvent.parse_obj(
        {
            "time": 0,
            "self_id": 1,
            "post_type": "message",
            "sub_type": "normal",
            "user_id": user_id,
            "message_type": "group",
            "message_id": 1,
            "group_id": group_id,
            "message": segs,
            "original_message": segs,
            "raw_message": "点歌",
            "font": 0,
            "sender": {"user_id": user_id, "nickname": "", "card": ""},
            "to_me": False,
            "reply": None,
        }
    )


class _Replies:
    """收集注入给 ``_play`` 的回复文本。"""

    def __init__(self):
        self.items: list[str] = []

    async def __call__(self, msg=None, **kw):
        self.items.append(str(msg))


class TestPlayOrchestration:
    """L18：`_play` 的调用**顺序**此前零覆盖（禁歌名闸门/挪冷却/禁缓存 都存活）。"""

    def _stub(self, monkeypatch, *, download_ok=True):
        calls = {"search": 0, "download": 0, "encode": 0, "send": 0}

        async def fake_search(name):
            calls["search"] += 1
            return [mr.Song("12345", name, "a", "b", 240000)]

        async def fake_url(sid):
            return ("https://m.music.126.net/a.mp3", 100)

        async def fake_fetch(url, dest):
            calls["download"] += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"mp3")
            return dest

        async def fake_encode(src):
            calls["encode"] += 1
            return b"\x02#!SILK_V3payload"

        async def fake_send(gid, silk, **kw):
            calls["send"] += 1
            return "ok"

        monkeypatch.setattr(mr, "search", fake_search)
        monkeypatch.setattr(mr, "song_url", fake_url)
        monkeypatch.setattr(mr, "fetch_audio", fake_fetch)
        monkeypatch.setattr(mr, "encode_to_silk", fake_encode)
        monkeypatch.setattr(mr, "send_group_voice", fake_send)
        monkeypatch.setattr(mr, "_cache", mr.SilkCache())
        mr.default_cooldown().reset()
        return calls

    @pytest.mark.asyncio
    async def test_full_path_order(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        calls = self._stub(monkeypatch)
        reply = _Replies()
        await mr._play(_group_ev(), "海阔天空", reply)
        assert calls == {"search": 1, "download": 1, "encode": 1, "send": 1}
        assert reply.items and "♪" in reply.items[-1]

    @pytest.mark.asyncio
    async def test_cache_hit_skips_download_and_encode(self, monkeypatch, tmp_path):
        """M11：第二次点同一首要**不下载也不编码**（旧实现每次都下载）。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        calls = self._stub(monkeypatch)
        mr.default_cooldown().reset()
        await mr._play(_group_ev(), "海阔天空", _Replies())
        mr.default_cooldown().reset()  # 绕过冷却，模拟"过了一会再点"
        await mr._play(_group_ev(), "海阔天空", _Replies())
        assert calls["download"] == 1, "缓存命中仍重新下载（M11 未修）"
        assert calls["encode"] == 1
        assert calls["send"] == 2

    @pytest.mark.asyncio
    async def test_private_refused_before_any_heavy_work(self, monkeypatch, tmp_path):
        """M9：私聊必须在**下载/编码/冷却之前**拒绝。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        calls = self._stub(monkeypatch)
        mr.default_cooldown().reset()
        reply = _Replies()
        await mr._play(_private_event("点歌 海阔天空"), "海阔天空", reply)
        assert calls == {"search": 0, "download": 0, "encode": 0, "send": 0}
        assert reply.items == ["私聊暂时只支持文字，语音放歌仅在群里可用。"]
        assert mr.default_cooldown().remaining() == 0.0, "私聊不得消耗账号级冷却"

    @pytest.mark.asyncio
    async def test_invalid_name_before_cooldown_and_work(self, monkeypatch, tmp_path):
        calls = self._stub(monkeypatch)
        mr.default_cooldown().reset()
        await mr._play(_group_ev(), "你喜欢听什么歌", _Replies())
        assert calls["search"] == 0
        assert mr.default_cooldown().remaining() == 0.0, "误触不得消耗冷却"

    @pytest.mark.asyncio
    async def test_disk_file_removed_after_encode(self, monkeypatch, tmp_path):
        """M11：silk 进内存后应删掉落盘 mp3（否则磁盘单调增长）。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        self._stub(monkeypatch)
        await mr._play(_group_ev(), "海阔天空", _Replies())
        assert list(tmp_path.glob("*.mp3")) == [], "编码后应清理 mp3"

    @pytest.mark.asyncio
    async def test_bad_song_id_rejected(self, monkeypatch, tmp_path):
        """M10：id 不合法直接拒绝，不拼路径。"""

        async def fake_search(name):
            return [mr.Song("/etc/cron.d/evil", name, "", "", 240000)]

        monkeypatch.setattr(mr, "search", fake_search)
        monkeypatch.setattr(mr, "_cache", mr.SilkCache())
        mr.default_cooldown().reset()
        reply = _Replies()
        await mr._play(_group_ev(), "海阔天空", reply)
        assert any("标识异常" in r for r in reply.items), reply.items


class TestCachePathValidation:
    def test_absolute_id_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        assert mr._cache_path(mr.Song("/etc/cron.d/evil", "", "", "", 1)) is None

    def test_traversal_id_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        assert mr._cache_path(mr.Song("../../../../tmp/pwn", "", "", "", 1)) is None

    def test_normal_id_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        p = mr._cache_path(mr.Song("12345", "", "", "", 1))
        assert p is not None and p.name == "12345.mp3"

    def test_overlong_id_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        assert mr._cache_path(mr.Song("9" * 100, "", "", "", 1)) is None


class TestSilkCache:
    def test_hit_and_miss(self):
        c = mr.SilkCache(quota=1000)
        assert c.get("a") is None
        c.put("a", b"x" * 10)
        assert c.get("a") == b"x" * 10

    def test_evicts_oldest_when_over_quota(self):
        c = mr.SilkCache(quota=25)
        c.put("a", b"x" * 10)
        c.put("b", b"x" * 10)
        c.put("c", b"x" * 10)  # 30 > 25 → 淘汰最旧的 a
        assert c.get("a") is None
        assert c.get("c") is not None

    def test_keeps_at_least_one_even_if_oversized(self):
        c = mr.SilkCache(quota=5)
        c.put("big", b"x" * 100)
        assert c.get("big") is not None, "单条超配额时至少保留最新一条"

    def test_empty_values_ignored(self):
        c = mr.SilkCache()
        c.put("", b"x")
        c.put("a", b"")
        assert c.get("a") is None


class TestCacheMbEnv:
    """M8：AGENT_MUSIC_CACHE_MB 在 AC 里声明过，但旧代码**从不读取**（契约漂移）。"""

    def test_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_MUSIC_CACHE_MB", raising=False)
        assert mr._env_cache_mb("AGENT_MUSIC_CACHE_MB", 200) == 200

    def test_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_MB", "50")
        assert mr._env_cache_mb("AGENT_MUSIC_CACHE_MB", 200) == 50

    def test_dirty_falls_back(self, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("AGENT_MUSIC_CACHE_MB", "abc")
        with caplog.at_level(logging.WARNING):
            assert mr._env_cache_mb("AGENT_MUSIC_CACHE_MB", 200) == 200
        assert "abc" in caplog.text

    def test_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_MB", "-1")
        assert mr._env_cache_mb("AGENT_MUSIC_CACHE_MB", 200) == 200


class TestDiskPurge:
    def test_purge_removes_oldest_until_under_quota(self, tmp_path):
        import os as _os
        import time as _time

        for i, name in enumerate(["old.mp3", "mid.mp3", "new.mp3"]):
            p = tmp_path / name
            p.write_bytes(b"x" * 1000)
            _os.utime(p, (_time.time() - 100 + i, _time.time() - 100 + i))
        # 3×1000=3000 > 2500 → 只需删掉最旧的一个即可回到配额内
        mr._purge_disk_cache(tmp_path, quota_bytes=2500)
        left = sorted(p.name for p in tmp_path.glob("*.mp3"))
        assert left == ["mid.mp3", "new.mp3"], f"应删最旧的，实际剩 {left}"

    def test_purge_noop_when_under_quota(self, tmp_path):
        (tmp_path / "a.mp3").write_bytes(b"x" * 10)
        mr._purge_disk_cache(tmp_path, quota_bytes=1000)
        assert len(list(tmp_path.glob("*.mp3"))) == 1


class TestPrivateAclGate:
    """M9：私聊必须有 ACL 门（旧实现只对群做白名单）。"""

    def test_private_non_superuser_rejected(self, monkeypatch):
        monkeypatch.setenv("AGENT_WAKE_WORDS", "云崽")
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
        monkeypatch.setattr(mr, "is_allowed", lambda ev: False)
        assert mr._rule(_private_event("云崽 点歌 海阔天空")) is False

    def test_private_allowed_user_accepted(self, monkeypatch):
        monkeypatch.setenv("AGENT_WAKE_WORDS", "云崽")
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
        monkeypatch.setattr(mr, "is_allowed", lambda ev: True)
        assert mr._rule(_private_event("云崽 点歌 海阔天空")) is True


def _private_event(text, user_id=12345):
    """真实 PrivateMessageEvent：trigger_rule 按 isinstance 分派，替身过不去。"""
    from nonebot.adapters.onebot.v11 import PrivateMessageEvent

    segs = [{"type": "text", "data": {"text": text}}]
    return PrivateMessageEvent.parse_obj(
        {
            "time": 0,
            "self_id": 1,
            "post_type": "message",
            "sub_type": "friend",
            "user_id": user_id,
            "message_type": "private",
            "message_id": 1,
            "message": segs,
            "original_message": segs,
            "raw_message": text,
            "font": 0,
            "sender": {"user_id": user_id, "nickname": "", "card": ""},
            "to_me": False,
            "reply": None,
        }
    )


class TestMusicEnvReadyGate:
    """F1/L18：`_music_env_ready` 决定 music_route 是否被导入（旧实现零覆盖）。"""

    def test_false_when_api_missing(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.delenv("AGENT_MUSIC_API_URL", raising=False)
        monkeypatch.setenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
        assert pkg._music_env_ready() is False

    def test_false_when_http_missing(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("AGENT_MUSIC_API_URL", "http://127.0.0.1:16300")
        monkeypatch.delenv("NAPCAT_HTTP_URL", raising=False)
        assert pkg._music_env_ready() is False

    def test_true_when_both_set(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("AGENT_MUSIC_API_URL", "http://127.0.0.1:16300")
        monkeypatch.setenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
        assert pkg._music_env_ready() is True

    def test_blank_values_count_as_missing(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("AGENT_MUSIC_API_URL", "   ")
        monkeypatch.setenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
        assert pkg._music_env_ready() is False

    def test_route_not_imported_when_env_missing(self, monkeypatch):
        """闸门为假时 music_route 不得进 sys.modules（AC F1 的"零影响"）。"""
        import sys

        import plugins.qq_agent_adapter as pkg

        monkeypatch.delenv("AGENT_MUSIC_API_URL", raising=False)
        monkeypatch.delenv("NAPCAT_HTTP_URL", raising=False)
        sys.modules.pop("plugins.qq_agent_adapter.music_route", None)
        pkg.music_route = None
        pkg._load_plugin_modules()
        assert "plugins.qq_agent_adapter.music_route" not in sys.modules
        assert pkg.music_route is None


# ==========================================================================
# 线上事故回归（2026-09-19 真机测试）
#
# `event.reply(...)` 在 OneBot v11 事件上**不可调用**：`reply` 是「引用消息」
# 数据字段（普通消息为 None）。旧代码 14 处回复全部
# `TypeError: 'NoneType' object is not callable` → 点歌一个消息都发不出去
# （连"音频地址不可用"这种错误提示也发不出，用户侧完全静默）。
# 旧的鸭子类型替身自己定义了 `async def reply`，把这个问题完全掩盖了。
# ==========================================================================


class TestEventReplyIsNotCallable:
    def test_real_event_reply_is_none_not_method(self):
        """钉住事实：真实事件的 reply 是数据字段，不是方法。"""
        ev = _group_ev()
        assert ev.reply is None
        assert not callable(ev.reply)

    @pytest.mark.asyncio
    async def test_play_never_touches_event_reply(self, monkeypatch, tmp_path):
        """用**真实事件**跑一遍：若实现里又出现 event.reply(...)，这里会 TypeError。

        这条比源码 grep 强：它验的是行为，不是文本。
        """
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))

        async def fake_search(name):
            return [mr.Song("12345", name, "a", "b", 240000)]

        async def fake_url(sid):
            return ("https://m.music.126.net/a.mp3", 1)

        async def fake_fetch(url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"ID3")
            return dest

        async def fake_encode(src):
            return b"\x02#!SILK_V3x"

        async def fake_send(gid, silk, **kw):
            return "ok"

        monkeypatch.setattr(mr, "search", fake_search)
        monkeypatch.setattr(mr, "song_url", fake_url)
        monkeypatch.setattr(mr, "fetch_audio", fake_fetch)
        monkeypatch.setattr(mr, "encode_to_silk", fake_encode)
        monkeypatch.setattr(mr, "send_group_voice", fake_send)
        monkeypatch.setattr(mr, "_cache", mr.SilkCache())
        mr.default_cooldown().reset()

        reply = _Replies()
        await mr._play(_group_ev(), "海阔天空", reply)  # 不应抛 TypeError
        assert reply.items, "必须通过注入的 reply 回复"

    @pytest.mark.asyncio
    async def test_error_paths_also_reply_via_injected_callable(
        self, monkeypatch, tmp_path
    ):
        """失败分支同样走注入的 reply（线上就是这里崩的）。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))

        async def fake_search(name):
            return []

        monkeypatch.setattr(mr, "search", fake_search)
        monkeypatch.setattr(mr, "_cache", mr.SilkCache())
        mr.default_cooldown().reset()
        reply = _Replies()
        await mr._play(_group_ev(), "不存在的歌", reply)
        assert reply.items and "没搜到" in reply.items[0]

    def test_handler_passes_matcher_send(self):
        """接线检查：handler 必须把 music_matcher.send 传进 _play。"""
        src = (
            __import__("pathlib")
            .Path("plugins/qq_agent_adapter/music_route.py")
            .read_text(encoding="utf-8")
        )
        assert "await _play(event, song_name, music_matcher.send)" in src
