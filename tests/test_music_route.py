"""点歌：歌名校验 + skill 注册/ACL + 序号选歌兜底 测试。

来源：AC-music-playback.md 的 B/D/G 组，以及「确定性直路由 → LLM skill」修订
（触发改为模型判断，安全闸门全部保留在 handler 内）。
"""

import importlib
from pathlib import Path

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


# ---------- G2 歌名校验（防误触硬闸，不依赖模型自觉） ----------
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
    ② reload 在同一命名空间里重执行，**不会删除**上一次残留的
    ``selection_matcher`` 属性，所以显式 pop，避免用例之间互相污染。
    """
    monkeypatch.delenv("AGENT_MUSIC_API_URL", raising=False)
    monkeypatch.delenv("NAPCAT_HTTP_URL", raising=False)
    monkeypatch.setattr("nonebot.on_message", lambda **kw: _FakeMatcher())
    importlib.reload(mr)
    mr.__dict__.pop("selection_matcher", None)
    yield
    importlib.reload(mr)
    mr.__dict__.pop("selection_matcher", None)


class TestConditionalRegistration:
    def test_not_registered_when_env_missing(self, clean_module, monkeypatch):
        """没配 API / OneBot HTTP 就什么都不注册——消息落给普通聊天，核心零影响。"""
        calls = []
        monkeypatch.setattr(
            "nonebot.on_message", lambda **kw: calls.append(kw) or _FakeMatcher()
        )
        importlib.reload(mr)
        assert calls == []
        assert not hasattr(mr, "selection_matcher")

    def test_selection_matcher_registered_with_priority_and_block(
        self, clean_module, monkeypatch
    ):
        """序号选歌 matcher：priority 必须小于 chat_matcher 的 10 且 block=True。"""
        monkeypatch.setenv("AGENT_MUSIC_API_URL", "http://127.0.0.1:16300")
        monkeypatch.setenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
        captured_all = []

        def fake_on_message(**kw):
            captured_all.append(kw)
            return _FakeMatcher()

        # 打在源模块上：reload 会重执行 `from nonebot import on_message`
        monkeypatch.setattr("nonebot.on_message", fake_on_message)
        # M1：注册**参数**的断言不该依赖宿主是否装了 ffmpeg（CI 上没有）。
        # 桩必须打在**源模块**上：reload 会重执行 `from agentcore.music.silk import
        # silk_available`，打在 mr 上会被重新绑定覆盖掉。
        monkeypatch.setattr("agentcore.music.silk.silk_available", lambda: (True, ""))
        importlib.reload(mr)
        by_rule = {kw["rule"]: kw for kw in captured_all}
        assert mr._selection_rule in by_rule, f"未注册序号选歌 matcher：{captured_all}"
        captured = by_rule[mr._selection_rule]
        assert captured["priority"] < 10, "必须比 chat_matcher(priority=10) 更先执行"
        assert captured["block"] is True, "命中即阻止普通聊天，避免同一条消息回两遍"
        # 点歌触发已 skill 化：只应注册**序号选歌**一个 matcher。
        # 若再把「点歌/放歌 子命令」加回 matcher，就是两条入口语义漂移
        assert len(captured_all) == 1, f"只应有一个 matcher：{captured_all}"

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
#
# skill 化修订：_play/_play_song 不再接 reply 回调，改为**返回文本**——
# 调用方（模型转述 / selection_matcher.send）决定怎么发。
# ==========================================================================


def _group_ev(text="点歌", group_id=456, user_id=12345):
    """**真实** GroupMessageEvent。

    线上事故的教训：旧替身自己定义了 ``async def reply`` 方法，而真实 OneBot v11
    事件的 ``reply`` 是「引用消息」数据字段（普通消息为 None）—— 于是
    ``event.reply(...)`` 在生产必崩（TypeError），测试却全绿。本文件不再用鸭子
    类型替身，一律构造真实事件对象。
    """
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    segs = [{"type": "text", "data": {"text": text}}]
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
            "raw_message": text,
            "font": 0,
            "sender": {"user_id": user_id, "nickname": "", "card": ""},
            "to_me": False,
            "reply": None,
        }
    )


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
        text = await mr._play("海阔天空", user_id="12345", group_id="456")
        assert calls == {"search": 1, "download": 1, "encode": 1, "send": 1}
        assert "♪" in text

    @pytest.mark.asyncio
    async def test_cache_hit_skips_download_and_encode(self, monkeypatch, tmp_path):
        """M11：第二次点同一首要**不下载也不编码**（旧实现每次都下载）。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        calls = self._stub(monkeypatch)
        mr.default_cooldown().reset()
        await mr._play("海阔天空", user_id="12345", group_id="456")
        mr.default_cooldown().reset()  # 绕过冷却，模拟"过了一会再点"
        await mr._play("海阔天空", user_id="12345", group_id="456")
        assert calls["download"] == 1, "缓存命中仍重新下载（M11 未修）"
        assert calls["encode"] == 1
        assert calls["send"] == 2

    @pytest.mark.asyncio
    async def test_private_refused_before_any_heavy_work(self, monkeypatch, tmp_path):
        """M9：私聊必须在**下载/编码/冷却之前**拒绝。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        calls = self._stub(monkeypatch)
        mr.default_cooldown().reset()
        text = await mr._play("海阔天空", user_id="12345", group_id=None)
        assert calls == {"search": 0, "download": 0, "encode": 0, "send": 0}
        assert text == "私聊暂时只支持文字，语音放歌仅在群里可用。"
        assert mr.default_cooldown().remaining() == 0.0, "私聊不得消耗账号级冷却"

    @pytest.mark.asyncio
    async def test_invalid_name_before_cooldown_and_work(self, monkeypatch, tmp_path):
        calls = self._stub(monkeypatch)
        mr.default_cooldown().reset()
        text = await mr._play("你喜欢听什么歌", user_id="12345", group_id="456")
        assert calls["search"] == 0
        assert "没识别出歌名" in text
        assert mr.default_cooldown().remaining() == 0.0, "误触不得消耗冷却"

    @pytest.mark.asyncio
    async def test_disk_file_removed_after_encode(self, monkeypatch, tmp_path):
        """M11：silk 进内存后应删掉落盘 mp3（否则磁盘单调增长）。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        self._stub(monkeypatch)
        await mr._play("海阔天空", user_id="12345", group_id="456")
        assert list(tmp_path.glob("*.mp3")) == [], "编码后应清理 mp3"

    @pytest.mark.asyncio
    async def test_bad_song_id_rejected(self, monkeypatch, tmp_path):
        """M10：id 不合法直接拒绝，不拼路径。"""

        async def fake_search(name):
            return [mr.Song("/etc/cron.d/evil", name, "", "", 240000)]

        monkeypatch.setattr(mr, "search", fake_search)
        monkeypatch.setattr(mr, "_cache", mr.SilkCache())
        mr.default_cooldown().reset()
        text = await mr._play("海阔天空", user_id="12345", group_id="456")
        assert "标识异常" in text, text
        assert mr.default_cooldown().remaining() == 0.0, "失败应返还冷却额度"

    @pytest.mark.asyncio
    async def test_failed_play_resets_cooldown(self, monkeypatch, tmp_path):
        """M1：失败路径应返还冷却，否则用户被误导为「刚放完一首」。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))

        async def fake_search(name):
            return [mr.Song("12345", name, "a", "b", 240000)]

        async def fake_url(sid):
            return None

        monkeypatch.setattr(mr, "search", fake_search)
        monkeypatch.setattr(mr, "song_url", fake_url)
        monkeypatch.setattr(mr, "_cache", mr.SilkCache())
        mr.default_cooldown().reset()
        text = await mr._play("海阔天空", user_id="12345", group_id="456")
        assert "拿不到可播放的地址" in text
        assert mr.default_cooldown().remaining() == 0.0, "失败应返还冷却额度"


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
        c = mr.SilkCache(quota=1024)
        assert c.get("x") is None
        c.put("x", b"abc")
        assert c.get("x") == b"abc"

    def test_evicts_oldest_when_over_quota(self):
        c = mr.SilkCache(quota=10)
        c.put("a", b"x" * 6)
        c.put("b", b"y" * 6)
        assert c.get("a") is None, "超配额应淘汰最旧"
        assert c.get("b") == b"y" * 6

    def test_keeps_at_least_one_even_if_oversized(self):
        c = mr.SilkCache(quota=1)
        c.put("a", b"x" * 100)
        assert c.get("a") == b"x" * 100, "至少要留一条，不能把缓存清空"

    def test_empty_values_ignored(self):
        c = mr.SilkCache()
        c.put("", b"x")
        c.put("a", b"")
        assert c.get("a") is None


class TestCacheMbEnv:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_MUSIC_CACHE_MB", raising=False)
        assert mr._env_cache_mb("AGENT_MUSIC_CACHE_MB", mr.DEFAULT_CACHE_MB) == 200

    def test_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_MB", "50")
        assert mr._env_cache_mb("AGENT_MUSIC_CACHE_MB", mr.DEFAULT_CACHE_MB) == 50

    def test_dirty_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_MB", "abc")
        assert mr._env_cache_mb("AGENT_MUSIC_CACHE_MB", mr.DEFAULT_CACHE_MB) == 200

    def test_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_MB", "-5")
        assert mr._env_cache_mb("AGENT_MUSIC_CACHE_MB", mr.DEFAULT_CACHE_MB) == 200


class TestDiskPurge:
    def test_purge_removes_oldest_until_under_quota(self, tmp_path):
        import os

        files = []
        for i, size in enumerate((300, 200, 100)):
            p = tmp_path / f"{i}.mp3"
            p.write_bytes(b"x" * size)
            os.utime(p, (1000 + i, 1000 + i))  # 0 最旧
            files.append(p)
        mr._purge_disk_cache(tmp_path, 400)
        left = sorted(p.name for p in tmp_path.glob("*.mp3"))
        assert left == ["1.mp3", "2.mp3"], f"应按 mtime 最旧优先删：{left}"

    def test_purge_noop_when_under_quota(self, tmp_path):
        (tmp_path / "a.mp3").write_bytes(b"x" * 10)
        mr._purge_disk_cache(tmp_path, 1000)
        assert (tmp_path / "a.mp3").exists()


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
        saved = sys.modules.pop("plugins.qq_agent_adapter.music_route", None)
        try:
            pkg.music_route = None
            pkg._load_plugin_modules()
            assert "plugins.qq_agent_adapter.music_route" not in sys.modules
            assert pkg.music_route is None
        finally:
            # 恢复现场：本用例故意把模块移出 sys.modules，不能把这个副作用
            # 留给后续用例（曾导致 importlib.reload(mr) 抛 ImportError）
            if saved is not None:
                sys.modules["plugins.qq_agent_adapter.music_route"] = saved


# ==========================================================================
# 线上事故回归（2026-09-19 真机测试）
#
# `event.reply(...)` 在 OneBot v11 事件上**不可调用**：`reply` 是「引用消息」
# 数据字段（普通消息为 None）。旧代码 14 处回复全部
# `TypeError: 'NoneType' object is not callable` → 点歌一个消息都发不出去
# （连"音频地址不可用"这种错误提示也发不出，用户侧完全静默）。
# 旧的鸭子类型替身自己定义了 `async def reply`，把这个问题完全掩盖了。
#
# skill 化后 `_play` 连 event 都不接了（从结构上消灭这一类坑）；剩下的
# event 相关路径只有序号选歌，发送一律走 matcher.send。
# ==========================================================================


class TestEventReplyIsNotCallable:
    def test_real_event_reply_is_none_not_method(self):
        """钉住事实：真实事件的 reply 是数据字段，不是方法。"""
        ev = _group_ev()
        assert ev.reply is None
        assert not callable(ev.reply)

    def test_source_has_no_event_reply_call(self):
        """结构护栏：本模块任何路径不得出现 ``event.reply(...)``。"""
        src = Path("plugins/qq_agent_adapter/music_route.py").read_text(
            encoding="utf-8"
        )
        assert "event.reply" not in src, "又走回头路了：event.reply 在生产必崩"

    def test_selection_handler_sends_via_matcher(self):
        """接线检查：序号路径必须走 matcher.send。"""
        src = Path("plugins/qq_agent_adapter/music_route.py").read_text(
            encoding="utf-8"
        )
        assert "await selection_matcher.send(text)" in src

    @pytest.mark.asyncio
    async def test_selection_with_real_event_does_not_raise(
        self, registered, monkeypatch
    ):
        """用**真实事件**跑序号路径：若实现里又出现 event.reply(...)，这里会 TypeError。

        这条比源码 grep 强：它验的是行为，不是文本。
        """
        mod, _matchers, sends = registered
        monkeypatch.setattr(mod, "_selections", mod.PendingSelections(ttl=100))
        songs = [mod.Song("1", "a", "b", "", 1000) for _ in range(2)]
        ev = _group_ev("2")
        mod._selections.put(mod._event_selection_key(ev), songs)

        async def fake_play_song(song, group_id):
            return "♪ a"

        monkeypatch.setattr(mod, "_play_song", fake_play_song)
        await mod.handle_selection(ev)
        assert sends and "♪" in sends[-1]


# ==========================================================================
# 候选选择（多版本让用户挑）
#
# 实测动机：搜「稻香」前 5 条**全是翻唱**（Lucky小爱/Lie/卡罗尔…），网易云没有
# 周杰伦版权、原唱搜不到；旧实现盲取 songs[0]，用户无从选择且要等播完才知道
# 不是原唱。
# ==========================================================================


class TestParseSelection:
    def test_accepts_valid_index(self):
        assert mr.parse_selection("1", 3) == 1
        assert mr.parse_selection(" 3 ", 3) == 3

    def test_rejects_out_of_range(self):
        assert mr.parse_selection("0", 3) is None
        assert mr.parse_selection("4", 3) is None
        assert mr.parse_selection("9", 5) is None

    def test_rejects_non_numeric(self):
        for text in ("点歌 稻香", "一", "", "1个", "1.0"):
            assert mr.parse_selection(text, 5) is None, text

    def test_rejects_full_width_digit(self):
        """全角「１」不认（§5 的坑）：宁可落回普通聊天，也不做"看着选了其实没选"。"""
        assert mr.parse_selection("１", 5) is None


class TestPendingSelections:
    def test_put_peek_take(self):
        sel = mr.PendingSelections(ttl=100, clock=lambda: 0.0)
        sel.put(("g", "1", "u"), ["a", "b"])
        assert sel.peek(("g", "1", "u")) == ["a", "b"]
        assert sel.take(("g", "1", "u")) == ["a", "b"]
        assert sel.peek(("g", "1", "u")) is None, "取用即消费"

    def test_expires_after_ttl(self):
        now = {"t": 0.0}
        sel = mr.PendingSelections(ttl=60, clock=lambda: now["t"])
        sel.put(("g", "1", "u"), ["a"])
        now["t"] = 61.0
        assert sel.peek(("g", "1", "u")) is None

    def test_keys_are_isolated_per_user_and_chat(self):
        sel = mr.PendingSelections(ttl=100, clock=lambda: 0.0)
        sel.put(("g", "1", "u1"), ["a"])
        assert sel.peek(("g", "1", "u2")) is None
        assert sel.peek(("g", "2", "u1")) is None


class TestCandidateFlow:
    def _stub_search(self, monkeypatch, songs):
        async def fake_search(name):
            return songs

        monkeypatch.setattr(mr, "search", fake_search)
        monkeypatch.setattr(mr, "_selections", mr.PendingSelections(ttl=100))
        mr.default_cooldown().reset()

    def _songs(self, n=3):
        return [
            mr.Song(str(100 + i), f"稻香{i}", f"翻唱者{i}", f"专辑{i}", 200000)
            for i in range(n)
        ]

    @pytest.mark.asyncio
    async def test_multiple_candidates_lists_instead_of_playing(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        self._stub_search(monkeypatch, self._songs(3))
        sent = []

        async def fake_send(gid, silk, **kw):
            sent.append(silk)
            return "ok"

        monkeypatch.setattr(mr, "send_group_voice", fake_send)
        text = await mr._play("稻香", user_id="12345", group_id="456")
        assert sent == [], "多候选时不得直接播放"
        assert "找到 3 个版本" in text
        for i in (1, 2, 3):
            assert f"{i}. 稻香" in text
        assert "翻唱者0" in text and "专辑0" in text, (
            "必须显示歌手与专辑（翻唱一眼可辨）"
        )

    @pytest.mark.asyncio
    async def test_single_candidate_plays_directly(self, monkeypatch, tmp_path):
        """只有一个版本时直接播，不额外交互。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        self._stub_search(monkeypatch, self._songs(1))

        async def fake_fetch(url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"ID3")
            return dest

        async def fake_url(sid):
            return ("https://m.music.126.net/a.mp3", 1)

        async def fake_encode(src):
            return b"\x02#!SILK_V3x"

        sent = []

        async def fake_send(gid, silk, **kw):
            sent.append(silk)
            return "ok"

        monkeypatch.setattr(mr, "fetch_audio", fake_fetch)
        monkeypatch.setattr(mr, "song_url", fake_url)
        monkeypatch.setattr(mr, "encode_to_silk", fake_encode)
        monkeypatch.setattr(mr, "send_group_voice", fake_send)
        text = await mr._play("稻香", user_id="12345", group_id="456")
        assert sent, "单候选应直接播放"
        assert "找到" not in text

    @pytest.mark.asyncio
    async def test_listing_does_not_consume_cooldown(self, monkeypatch, tmp_path):
        """列候选不该消耗账号级冷却（用户还没决定放哪首）。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        self._stub_search(monkeypatch, self._songs(3))
        mr.default_cooldown().reset()
        await mr._play("稻香", user_id="12345", group_id="456")
        assert mr.default_cooldown().remaining() == 0.0, "仅列候选不得占用冷却"

    @pytest.mark.asyncio
    async def test_selection_plays_chosen_song(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        self._stub_search(monkeypatch, self._songs(3))
        await mr._play("稻香", user_id="12345", group_id="456")

        songs = mr._selections.peek(mr._selection_key("12345", "456"))
        assert songs is not None
        idx = mr.parse_selection("2", len(songs))
        assert idx == 2

        sent = []

        async def fake_fetch(url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"ID3")
            return dest

        async def fake_url(sid):
            return ("https://m.music.126.net/a.mp3", 1)

        async def fake_encode(src):
            return b"\x02#!SILK_V3x"

        async def fake_send(gid, silk, **kw):
            sent.append(silk)
            return "ok"

        monkeypatch.setattr(mr, "fetch_audio", fake_fetch)
        monkeypatch.setattr(mr, "song_url", fake_url)
        monkeypatch.setattr(mr, "encode_to_silk", fake_encode)
        monkeypatch.setattr(mr, "send_group_voice", fake_send)
        text = await mr._play_song(songs[idx - 1], "456")
        assert sent, "应播放列表里的第 2 首"
        assert "♪" in text

    @pytest.mark.asyncio
    async def test_skill_listed_candidates_reachable_by_matcher_key(
        self, monkeypatch, tmp_path
    ):
        """key 对齐回归：skill 列入的候选，序号 matcher 必须能取到。

        skill 入口用 (user_id, group_id) 造 key、matcher 入口用 event 造 key——
        两边公式一旦漂移，用户回序号永远选不中，且**完全静默**。
        这里钉住**确切 key 形状**（(类型, 群, 用户)）：只断言"取得到"的话，
        把公式整体换序这类对称变异两边一起变、照样通过，等于没测。
        """
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        self._stub_search(monkeypatch, self._songs(3))
        text = await mr._play("稻香", user_id="12345", group_id="456")
        assert "找到 3 个版本" in text
        # 形状断言：群在前、用户在后；换序/换类型前缀都必须失败
        assert mr._selection_key("12345", "456") == ("g", "456", "12345")
        assert mr._selection_key("12345", None) == ("p", "", "12345")
        ev = _group_ev("2", group_id=456, user_id=12345)
        assert mr._event_selection_key(ev) == mr._selection_key("12345", "456")
        songs = mr._selections.peek(mr._event_selection_key(ev))
        assert songs is not None, "skill 列的候选 matcher 侧取不到（key 公式漂移）"
        assert mr.parse_selection("2", len(songs)) == 2

    def test_selection_rule_requires_pending(self, monkeypatch):
        monkeypatch.setenv("AGENT_WAKE_WORDS", "云崽")
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
        monkeypatch.setattr(mr, "_selections", mr.PendingSelections(ttl=100))
        ev = _group_event("2")
        # 无待选项：即使文本是序号也不命中（消息应落回普通聊天）
        assert mr._selection_rule(ev) is False

    def test_selection_rule_matches_only_valid_index(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
        monkeypatch.setattr(mr, "_selections", mr.PendingSelections(ttl=100))
        ev = _group_event("2")
        mr._selections.put(mr._event_selection_key(ev), self._songs(3))
        assert mr._selection_rule(ev) is True
        # 超范围序号不命中
        assert mr._selection_rule(_group_event("4")) is False
        # 点歌文本不命中（落普通聊天 / 交给模型判断）
        assert mr._selection_rule(_group_event("点歌 稻香")) is False

    def test_selection_rule_respects_group_whitelist(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "999")  # 不含 456
        monkeypatch.setattr(mr, "_selections", mr.PendingSelections(ttl=100))
        ev = _group_event("2", group_id=456)
        mr._selections.put(mr._event_selection_key(ev), self._songs(3))
        assert mr._selection_rule(ev) is False


class TestSelectionWithoutWakeWord:
    """回复序号**不需要**唤醒词——bot 刚问过，再要求「云崽 2」是多余的。

    安全性由「该用户在当前会话确有待选项」这条更强的上下文条件保证。
    """

    def test_bare_number_matches_when_pending(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
        monkeypatch.setattr(mr, "_selections", mr.PendingSelections(ttl=100))
        ev = _group_event("2", group_id=456)
        mr._selections.put(mr._event_selection_key(ev), [object(), object(), object()])
        assert mr._selection_rule(ev) is True, "待选项存在时裸序号应命中"

    def test_bare_number_ignored_without_pending(self, monkeypatch):
        """无待选项时必须放行给普通聊天（不能被音乐路由吞掉）。"""
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
        monkeypatch.setattr(mr, "_selections", mr.PendingSelections(ttl=100))
        assert mr._selection_rule(_group_event("2", group_id=456)) is False

    def test_other_user_number_is_not_matched(self, monkeypatch):
        """A 的待选项不能被 B 的「2」消费。"""
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
        monkeypatch.setattr(mr, "_selections", mr.PendingSelections(ttl=100))
        a = _group_event("2", group_id=456, user_id=111)
        b = _group_event("2", group_id=456, user_id=222)
        mr._selections.put(mr._event_selection_key(a), [object(), object()])
        assert mr._selection_rule(b) is False

    def test_self_message_ignored(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
        monkeypatch.setattr(mr, "_selections", mr.PendingSelections(ttl=100))
        ev = _group_event("2", group_id=456, self_id=123, user_id=123)
        mr._selections.put(mr._event_selection_key(ev), [object(), object()])
        assert mr._selection_rule(ev) is False


@pytest.fixture
def registered(monkeypatch):
    """强制注册序号 matcher（依赖探测打桩），拿到 selection_matcher 句柄。

    `handle_selection` 这个 coroutine 在线上承担「回复序号 → 播放 → 发送」，
    此前无任何用例（变异"取用不消费"因此存活）。
    """
    monkeypatch.setenv("AGENT_MUSIC_API_URL", "http://127.0.0.1:16300")
    monkeypatch.setenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
    monkeypatch.setattr("agentcore.music.silk.silk_available", lambda: (True, ""))
    sends: list[str] = []
    matchers: list[tuple] = []

    class _M:
        def handle(self):
            def deco(fn):
                return fn

            return deco

        async def send(self, msg=None, **kw):
            sends.append(str(msg))

    def fake_on_message(**kw):
        m = _M()
        matchers.append((kw, m))
        return m

    monkeypatch.setattr("nonebot.on_message", fake_on_message)
    # 别的用例可能（故意）把它从 sys.modules 移除，reload 前先补回去
    import sys as _sys

    _sys.modules.setdefault(mr.__name__, mr)
    importlib.reload(mr)
    yield mr, matchers, sends
    _sys.modules.setdefault(mr.__name__, mr)
    importlib.reload(mr)


class TestSelectionHandler:
    @pytest.mark.asyncio
    async def test_handler_plays_chosen_and_consumes(self, registered, monkeypatch):
        mod, matchers, sends = registered
        assert hasattr(mod, "handle_selection"), "未注册候选选择 handler"
        monkeypatch.setattr(mod, "_selections", mod.PendingSelections(ttl=100))

        songs = [
            mod.Song(str(100 + i), f"稻香{i}", f"歌手{i}", "", 200000) for i in range(3)
        ]
        ev = _group_ev("2")
        mod._selections.put(mod._event_selection_key(ev), songs)

        played = []

        async def fake_play_song(song, group_id):
            played.append(song.id)
            return "♪ 稻香"

        monkeypatch.setattr(mod, "_play_song", fake_play_song)
        await mod.handle_selection(ev)

        assert played == ["101"], f"应播放第 2 首，实际 {played}"
        assert mod._selections.peek(mod._event_selection_key(ev)) is None, (
            "取用即消费——否则同一条语音会被重复播放"
        )
        assert sends and "♪" in sends[-1], "播放结果要经 matcher.send 发出"

    @pytest.mark.asyncio
    async def test_handler_noop_without_pending(self, registered, monkeypatch):
        mod, _matchers, _sends = registered
        monkeypatch.setattr(mod, "_selections", mod.PendingSelections(ttl=100))
        played = []

        async def fake_play_song(song, group_id):
            played.append(song.id)
            return "♪"

        monkeypatch.setattr(mod, "_play_song", fake_play_song)
        await mod.handle_selection(_group_ev("2"))
        assert played == []

    @pytest.mark.asyncio
    async def test_handler_reports_error_via_matcher_send(
        self, registered, monkeypatch
    ):
        """播放异常要走 matcher.send 报错（不能静默、也不能用 event.reply）。"""
        mod, _matchers, sends = registered
        monkeypatch.setattr(mod, "_selections", mod.PendingSelections(ttl=100))
        songs = [mod.Song("1", "a", "b", "", 1000) for _ in range(2)]
        ev = _group_ev("2")
        mod._selections.put(mod._event_selection_key(ev), songs)

        async def boom(song, group_id):
            raise RuntimeError("boom")

        monkeypatch.setattr(mod, "_play_song", boom)
        await mod.handle_selection(ev)
        assert sends and "出错" in sends[-1]

    @pytest.mark.asyncio
    async def test_cooldown_preserves_pending_selection(self, registered, monkeypatch):
        """M2：冷却被占时序号选择应先告知冷却中，**保留**待选项。"""
        mod, _matchers, sends = registered
        monkeypatch.setattr(mod, "_selections", mod.PendingSelections(ttl=100))
        songs = [mod.Song(str(i), f"a{i}", "b", "", 1000) for i in range(2)]
        ev = _group_ev("2")
        mod._selections.put(mod._event_selection_key(ev), songs)

        # 占住冷却
        cd = mod.default_cooldown()
        cd.try_acquire()

        played = []

        async def fake_play_song(song, group_id):
            played.append(song.id)
            return "♪ a"

        monkeypatch.setattr(mod, "_play_song", fake_play_song)
        await mod.handle_selection(ev)
        assert played == [], "冷却期内不应播放"
        assert sends and "冷却中" in sends[-1], sends
        # 待选项必须保留，用户不必重新点歌
        assert mod._selections.peek(mod._event_selection_key(ev)) == songs, (
            "冷却被拒时候选被消费，用户丢失选择"
        )


# ==========================================================================
# skill 化（LLM 判断是否放歌）：注册形状 / ACL / handler 行为
#
# 安全模型：模型只决定"要不要调"，"能不能发"全在 handler 内——
# 群白名单 + 私聊 superuser + 歌名校验 + 账号级冷却，四道闸都不依赖模型自觉。
# ==========================================================================


def _register_skill(monkeypatch) -> "object":
    """在依赖打桩的前提下把 play_music 注册进一个真实 SkillRegistry。"""
    from agentcore.skills.registry import SkillRegistry

    monkeypatch.setenv("AGENT_MUSIC_API_URL", "http://127.0.0.1:16300")
    monkeypatch.setenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
    # 打在源模块上：register_music_skill 内的 _missing_deps 走的是
    # `from agentcore.music.silk import silk_available` 的绑定
    monkeypatch.setattr("agentcore.music.silk.silk_available", lambda: (True, ""))
    reg = SkillRegistry()
    mr.register_music_skill(reg)
    return reg


class TestMusicSkillRegistration:
    def test_registered_with_expected_schema(self, monkeypatch):
        reg = _register_skill(monkeypatch)
        assert mr.SKILL_NAME in reg.skills
        sk = reg.skills[mr.SKILL_NAME]
        assert sk.params_schema["required"] == ["song_name"]
        assert "song_name" in sk.params_schema["properties"]
        assert sk.permission != "public", (
            "未授权部署不该把 schema 暴露给所有用户（hardened 下靠权限层隐身）"
        )

    def test_description_mentions_configured_aliases(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_COMMANDS", "来首歌,听歌")
        reg = _register_skill(monkeypatch)
        desc = reg.skills[mr.SKILL_NAME].description
        assert "来首歌" in desc and "听歌" in desc, (
            "别名要进描述，模型才知道哪些词是意图"
        )

    def test_description_forbids_generic_and_index_names(self, monkeypatch):
        reg = _register_skill(monkeypatch)
        desc = reg.skills[mr.SKILL_NAME].description
        assert "序号" in desc, "要告诉模型别把序号当歌名"
        assert "泛称" in desc or "随便" in desc

    def test_not_registered_when_deps_missing(self, monkeypatch):
        """依赖缺失 → 不注册（模型看不到这个工具，行为与未配置一致）。"""
        monkeypatch.setenv("AGENT_MUSIC_API_URL", "http://127.0.0.1:16300")
        monkeypatch.setenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
        monkeypatch.setattr(mr, "silk_available", lambda: (False, "pysilk 不可导入"))
        from agentcore.skills.registry import SkillRegistry

        reg = SkillRegistry()
        mr.register_music_skill(reg)
        assert reg.skills == {}


class TestMusicSkillAcl:
    """ACL 是硬闸：模型调了也不算数。"""

    @pytest.mark.asyncio
    async def test_group_outside_whitelist_rejected_without_search(self, monkeypatch):
        reg = _register_skill(monkeypatch)
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
        called = []

        async def fake_search(name):
            called.append(name)
            return []

        monkeypatch.setattr(mr, "search", fake_search)
        text = await reg.skills[mr.SKILL_NAME].handler(
            song_name="稻香", user_id="1", group_id="999"
        )
        assert "没有开放" in text
        assert called == [], "被拒时不得发起搜索/下载"

    @pytest.mark.asyncio
    async def test_whitelisted_group_passes_acl(self, monkeypatch):
        reg = _register_skill(monkeypatch)
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")

        async def fake_search(name):
            return []

        monkeypatch.setattr(mr, "search", fake_search)
        text = await reg.skills[mr.SKILL_NAME].handler(
            song_name="稻香", user_id="1", group_id="456"
        )
        assert "没搜到" in text, "过了 ACL 应走到搜索（这里只返回搜索空结果）"

    @pytest.mark.asyncio
    async def test_private_non_superuser_rejected(self, monkeypatch):
        reg = _register_skill(monkeypatch)
        monkeypatch.setattr(mr, "is_superuser_id", lambda uid: False)
        text = await reg.skills[mr.SKILL_NAME].handler(
            song_name="稻香", user_id="1", group_id=""
        )
        assert "没有开放" in text

    @pytest.mark.asyncio
    async def test_private_superuser_told_voice_is_group_only(self, monkeypatch):
        reg = _register_skill(monkeypatch)
        monkeypatch.setattr(mr, "is_superuser_id", lambda uid: True)
        text = await reg.skills[mr.SKILL_NAME].handler(
            song_name="稻香", user_id="1", group_id=""
        )
        assert "私聊" in text and "群" in text

    @pytest.mark.asyncio
    async def test_question_style_name_rejected_without_cooldown(self, monkeypatch):
        """模型把疑问句当歌名传进来：拒绝，且不烧账号级冷却。"""
        reg = _register_skill(monkeypatch)
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
        mr.default_cooldown().reset()
        text = await reg.skills[mr.SKILL_NAME].handler(
            song_name="推荐点歌", user_id="1", group_id="456"
        )
        assert "没识别出歌名" in text
        assert mr.default_cooldown().remaining() == 0.0, "误触不得烧掉账号级冷却"

    @pytest.mark.asyncio
    async def test_full_path_in_whitelisted_group(self, monkeypatch, tmp_path):
        """白名单群 + 单候选 → 走完整链路并返回发送结果文本。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        reg = _register_skill(monkeypatch)
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
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
            assert int(gid) == 456
            return "ok"

        monkeypatch.setattr(mr, "search", fake_search)
        monkeypatch.setattr(mr, "song_url", fake_url)
        monkeypatch.setattr(mr, "fetch_audio", fake_fetch)
        monkeypatch.setattr(mr, "encode_to_silk", fake_encode)
        monkeypatch.setattr(mr, "send_group_voice", fake_send)
        monkeypatch.setattr(mr, "_cache", mr.SilkCache())
        mr.default_cooldown().reset()

        text = await reg.skills[mr.SKILL_NAME].handler(
            song_name="海阔天空", user_id="12345", group_id="456"
        )
        assert calls == {"search": 1, "download": 1, "encode": 1, "send": 1}
        assert "♪" in text

    @pytest.mark.asyncio
    async def test_multiple_candidates_returned_as_list(self, monkeypatch, tmp_path):
        """多候选 → 返回列表文本 + 落待选（不发送）。"""
        monkeypatch.setenv("AGENT_MUSIC_CACHE_DIR", str(tmp_path))
        reg = _register_skill(monkeypatch)
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "456")
        songs = [
            mr.Song(str(100 + i), f"稻香{i}", f"歌手{i}", "", 200000) for i in range(3)
        ]

        async def fake_search(name):
            return songs

        monkeypatch.setattr(mr, "search", fake_search)
        monkeypatch.setattr(mr, "_selections", mr.PendingSelections(ttl=100))
        text = await reg.skills[mr.SKILL_NAME].handler(
            song_name="稻香", user_id="12345", group_id="456"
        )
        assert "找到 3 个版本" in text
        assert mr._selections.peek(mr._selection_key("12345", "456")) is not None
