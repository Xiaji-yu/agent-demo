"""戳一戳 → 本机概览图：开关/冷却/ACL 与各降级分支。

不碰真机、不碰看板：``_reply`` 打桩成记录器，客户端与渲染器都注入假实现，
这样每个分支（未授权 / 冷却 / 未配令牌 / 未就绪 / 渲染失败 / 取数失败）都能断言。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from nonebot.adapters.onebot.v11 import PokeNotifyEvent

from agentcore.dashboard import DashboardError
from plugins.qq_agent_adapter import poke

SELF_ID = 10001
USER = 222
GROUP = 456


def make_poke(
    *,
    user_id: int = USER,
    target_id: int = SELF_ID,
    group_id: int | None = None,
    self_id: int = SELF_ID,
) -> PokeNotifyEvent:
    payload = {
        "time": int(time.time()),
        "self_id": self_id,
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "user_id": user_id,
        "target_id": target_id,
    }
    if group_id is not None:
        payload["group_id"] = group_id
    return PokeNotifyEvent.model_validate(payload)


class FakeClient:
    """假看板客户端：snapshot 返回注入值或抛注入异常。"""

    def __init__(
        self, snapshot=None, error: Exception | None = None, token: str = "dshk_x"
    ):
        self.token = token
        self._snapshot = snapshot
        self._error = error
        self.closed = False

    async def snapshot(self):
        if self._error is not None:
            raise self._error
        return self._snapshot

    async def aclose(self):
        self.closed = True


def _ready_snapshot(ready: bool = True) -> dict:
    return {
        "overview": {"ready": ready, "host": "h", "cpu": {"available": False}},
        "series": {},
        "ts": 1.0,
    }


@pytest.fixture
def sent(monkeypatch):
    """把 _reply 打桩成记录器，返回收集列表。"""
    box: list = []

    async def _record(event, message):
        box.append(message)

    monkeypatch.setattr(poke, "_reply", _record)
    monkeypatch.setattr(poke, "_last_reply", {})
    monkeypatch.setattr(poke, "_clock", time.monotonic)
    # 默认放行 ACL，需要测 ACL 的用例自己覆盖
    monkeypatch.setattr(poke, "is_strict_allowed", lambda event: True)
    return box


class TestEnvToggles:
    def test_enabled_default_true(self, monkeypatch):
        monkeypatch.delenv("AGENT_POKE_ENABLED", raising=False)
        assert poke.enabled() is True

    def test_explicit_empty_means_disabled(self, monkeypatch):
        """与 help_render 同款：显式设了值（含空串）就按值生效，不能被 `or "1"` 翻回开启。"""
        monkeypatch.setenv("AGENT_POKE_ENABLED", "")
        assert poke.enabled() is False

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv("AGENT_POKE_ENABLED", "0")
        assert poke.enabled() is False

    def test_cooldown_default_and_parsing(self, monkeypatch):
        monkeypatch.delenv("AGENT_POKE_COOLDOWN", raising=False)
        assert poke.cooldown_seconds() == 30.0
        monkeypatch.setenv("AGENT_POKE_COOLDOWN", "0")
        assert poke.cooldown_seconds() == 0.0
        monkeypatch.setenv("AGENT_POKE_COOLDOWN", "5.5")
        assert poke.cooldown_seconds() == 5.5
        monkeypatch.setenv("AGENT_POKE_COOLDOWN", "-3")
        assert poke.cooldown_seconds() == 0.0
        monkeypatch.setenv("AGENT_POKE_COOLDOWN", "abc")
        assert poke.cooldown_seconds() == 30.0


class TestRule:
    def test_accepts_poke(self):
        assert poke._poke_rule(make_poke()) is True

    def test_rejects_non_poke_event(self):
        from nonebot.adapters.onebot.v11 import GroupRecallNoticeEvent

        recall = GroupRecallNoticeEvent.model_validate(
            {
                "time": 1,
                "self_id": SELF_ID,
                "post_type": "notice",
                "notice_type": "group_recall",
                "group_id": GROUP,
                "user_id": USER,
                "operator_id": USER,
                "message_id": 1,
            }
        )
        assert poke._poke_rule(recall) is False

    def test_rejects_self_poke(self):
        """bot 自己戳别人回传的 notice 不得再触发（否则自激）。"""
        assert poke._is_self_poke(make_poke(user_id=SELF_ID, target_id=USER)) is True
        assert poke._poke_rule(make_poke(user_id=SELF_ID, target_id=USER)) is False

    def test_ignores_poke_aimed_at_others(self, sent, monkeypatch):
        monkeypatch.setenv("AGENT_DASHBOARD_TOKEN", "dshk_x")
        asyncio.run(poke.handle_poke(make_poke(target_id=USER)))
        assert sent == []


class TestAcl:
    def test_unauthorized_is_silent(self, sent, monkeypatch):
        """未授权必须**静默**：不回复（回"你没权限"等于把机器信息的存在性也说出去）。"""
        monkeypatch.setattr(poke, "is_strict_allowed", lambda event: False)
        asyncio.run(poke.handle_poke(make_poke()))
        assert sent == []

    def test_disabled_returns_silently(self, sent, monkeypatch):
        monkeypatch.setenv("AGENT_POKE_ENABLED", "0")
        asyncio.run(poke.handle_poke(make_poke()))
        assert sent == []


class TestCooldown:
    def test_second_poke_within_cooldown_is_dropped(self, sent, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_DASHBOARD_TOKEN", "dshk_x")
        monkeypatch.setenv("AGENT_POKE_COOLDOWN", "60")
        monkeypatch.setattr(
            poke, "load_from_env", lambda: FakeClient(_ready_snapshot())
        )
        monkeypatch.setattr(poke, "render_overview_png", lambda snap: b"\x89PNG-x")
        asyncio.run(poke.handle_poke(make_poke()))
        asyncio.run(poke.handle_poke(make_poke()))
        assert len(sent) == 1

    def test_cooldown_zero_allows_repeat(self, sent, monkeypatch):
        monkeypatch.setenv("AGENT_DASHBOARD_TOKEN", "dshk_x")
        monkeypatch.setenv("AGENT_POKE_COOLDOWN", "0")
        monkeypatch.setattr(
            poke, "load_from_env", lambda: FakeClient(_ready_snapshot())
        )
        monkeypatch.setattr(poke, "render_overview_png", lambda snap: b"\x89PNG-x")
        asyncio.run(poke.handle_poke(make_poke()))
        asyncio.run(poke.handle_poke(make_poke()))
        assert len(sent) == 2

    def test_cooldown_is_per_chat(self, sent, monkeypatch):
        """私聊冷却不能连带把自己在白名单群的响应也挡掉。"""
        monkeypatch.setenv("AGENT_DASHBOARD_TOKEN", "dshk_x")
        monkeypatch.setenv("AGENT_POKE_COOLDOWN", "60")
        monkeypatch.setattr(
            poke, "load_from_env", lambda: FakeClient(_ready_snapshot())
        )
        monkeypatch.setattr(poke, "render_overview_png", lambda snap: b"\x89PNG-x")
        asyncio.run(poke.handle_poke(make_poke()))
        asyncio.run(poke.handle_poke(make_poke(group_id=GROUP)))
        assert len(sent) == 2

    def test_different_users_share_group_cooldown(self, sent, monkeypatch):
        monkeypatch.setenv("AGENT_DASHBOARD_TOKEN", "dshk_x")
        monkeypatch.setenv("AGENT_POKE_COOLDOWN", "60")
        monkeypatch.setattr(
            poke, "load_from_env", lambda: FakeClient(_ready_snapshot())
        )
        monkeypatch.setattr(poke, "render_overview_png", lambda snap: b"\x89PNG-x")
        asyncio.run(poke.handle_poke(make_poke(group_id=GROUP, user_id=1)))
        asyncio.run(poke.handle_poke(make_poke(group_id=GROUP, user_id=2)))
        assert len(sent) == 1


class TestBranches:
    def _stub(self, monkeypatch, client, png=b"\x89PNG-x"):
        monkeypatch.setenv("AGENT_DASHBOARD_TOKEN", "dshk_x")
        monkeypatch.setenv("AGENT_POKE_COOLDOWN", "0")
        monkeypatch.setattr(poke, "load_from_env", lambda: client)
        monkeypatch.setattr(poke, "render_overview_png", lambda snap: png)

    def test_unconfigured_token_hints(self, sent, monkeypatch):
        client = FakeClient(_ready_snapshot(), token="")
        self._stub(monkeypatch, client)
        asyncio.run(poke.handle_poke(make_poke()))
        assert len(sent) == 1 and "AGENT_DASHBOARD_TOKEN" in sent[0]
        assert client.closed is True, "客户端必须被关闭（连接池不能泄漏）"

    def test_not_ready_hints(self, sent, monkeypatch):
        self._stub(monkeypatch, FakeClient(_ready_snapshot(ready=False)))
        asyncio.run(poke.handle_poke(make_poke()))
        assert len(sent) == 1 and "还没就绪" in sent[0]

    def test_sends_image_segment(self, sent, monkeypatch):
        self._stub(monkeypatch, FakeClient(_ready_snapshot()), png=b"\x89PNG-real")
        asyncio.run(poke.handle_poke(make_poke()))
        assert len(sent) == 1
        seg = sent[0]
        assert getattr(seg, "type", None) == "image"
        assert "base64://" in str(seg)

    def test_render_none_falls_back_to_text(self, sent, monkeypatch):
        self._stub(monkeypatch, FakeClient(_ready_snapshot()), png=None)
        asyncio.run(poke.handle_poke(make_poke()))
        assert len(sent) == 1
        assert isinstance(sent[0], str) and "文本" in sent[0]

    def test_dashboard_error_reports_reason(self, sent, monkeypatch):
        self._stub(monkeypatch, FakeClient(error=DashboardError("连不上看板")))
        asyncio.run(poke.handle_poke(make_poke()))
        assert len(sent) == 1 and "连不上看板" in sent[0]

    def test_unexpected_error_gets_friendly_text(self, sent, monkeypatch):
        self._stub(monkeypatch, FakeClient(error=RuntimeError("kaboom")))
        asyncio.run(poke.handle_poke(make_poke()))
        assert len(sent) == 1 and "失败" in sent[0]


class TestFailureIsBounded:
    """处理链路**任何一环失败都不得把异常抛给 NoneBot**（否则日志里是与业务无关的报错栈）。

    这些用例都是"把实现改回裸 await _reply / 裸 aclose，就必须失败"。
    """

    def _stub(self, monkeypatch, client, png=b"\x89PNG-x"):
        monkeypatch.setenv("AGENT_DASHBOARD_TOKEN", "dshk_x")
        monkeypatch.setenv("AGENT_POKE_COOLDOWN", "0")
        monkeypatch.setattr(poke, "load_from_env", lambda: client)
        monkeypatch.setattr(poke, "render_overview_png", lambda snap: png)

    def test_send_failure_does_not_propagate(self, monkeypatch):
        self._stub(monkeypatch, FakeClient(_ready_snapshot()))

        async def boom(event, message):
            raise RuntimeError("qq 断连")

        monkeypatch.setattr(poke, "_reply", boom)
        monkeypatch.setattr(poke, "is_strict_allowed", lambda event: True)
        monkeypatch.setattr(poke, "_last_reply", {})
        asyncio.run(poke.handle_poke(make_poke()))  # 不抛即通过

    def test_aclose_failure_does_not_propagate(self, monkeypatch, sent):
        class BadCloseClient(FakeClient):
            async def aclose(self):
                self.closed = True
                raise RuntimeError("连接池关闭失败")

        self._stub(monkeypatch, BadCloseClient(_ready_snapshot()))
        asyncio.run(poke.handle_poke(make_poke()))
        assert len(sent) == 1, "aclose 失败不能影响已经发出去的回复"

    def test_aclose_failure_does_not_mask_dashboard_error(self, monkeypatch, sent):
        class BadCloseClient(FakeClient):
            async def aclose(self):
                raise RuntimeError("关闭也炸")

        self._stub(monkeypatch, BadCloseClient(error=DashboardError("连不上看板")))
        asyncio.run(poke.handle_poke(make_poke()))
        assert len(sent) == 1 and "连不上看板" in sent[0], (
            "原失败原因不能被 aclose 顶掉"
        )

    def test_fallback_text_survives_dirty_numbers(self):
        """看板字段可能是 None/字符串/别的类型；降级路径不能因为 float() 再炸一次。

        每个指标都给脏值：任何一个字段少了一层容错，这里都会抛出去。
        """
        text = poke._fallback_text(
            {
                "overview": {
                    "host": "box",
                    "cpu": {"available": True, "percent": "x"},
                    "memory": {"available": True, "used_gb": None, "total_gb": "x"},
                    "temp": {"available": True, "celsius": object()},
                    "disk": {"available": True, "free_gb": "not-a-number"},
                }
            }
        )
        assert "box" in text

    def test_safe_number(self):
        assert poke._safe_number("12.5") == 12.5
        assert poke._safe_number(None) == 0.0
        assert poke._safe_number("x") == 0.0
        assert poke._safe_number(True) == 0.0  # bool 不是数字
        assert poke._safe_number(None, -1.0) == -1.0


class TestFallbackText:
    def test_contains_key_metrics(self):
        text = poke._fallback_text(
            {
                "overview": {
                    "host": "box",
                    "cpu": {"available": True, "percent": 12.4},
                    "memory": {"available": True, "used_gb": 4.0, "total_gb": 16.0},
                    "temp": {"available": True, "celsius": 70.0},
                    "disk": {"available": True, "free_gb": 93.1},
                }
            }
        )
        assert "box" in text and "12" in text and "70" in text and "93" in text

    def test_survives_empty_overview(self):
        assert isinstance(poke._fallback_text({}), str)
