"""长期记忆自助命令（/memory）：查看 / 删除 / 清空的权限与确认语义。

存储层契约在 tests/test_store_contract.py；本文件只锁平台接线：CLI 解析、
「只能操作自己」、清空两段确认与过期。
"""

from __future__ import annotations

import importlib
import re
import time

import pytest
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from nonebot.exception import FinishedException

from agentcore.memory.store import InMemoryMemoryStore

memory_route = None


@pytest.fixture(scope="module", autouse=True)
def _load_memory_route():
    global memory_route
    import nonebot

    nonebot.init(_env_file=None, superusers={"10000"})
    memory_route = importlib.import_module("plugins.qq_agent_adapter.memory_route")


class _FakeMatcher:
    def __init__(self):
        self.finished = None

    async def finish(self, msg=None, **kwargs):
        self.finished = msg
        raise FinishedException


class _FakeDriver:
    def __init__(self, store):
        self._agent_memory = store


def _event(text: str, user_id: int = 10000) -> PrivateMessageEvent:
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
            "sender": {"user_id": user_id, "nickname": ""},
            "to_me": False,
            "reply": None,
        }
    )


async def _run(monkeypatch, store, text: str, user_id: int = 10000) -> str:
    matcher = _FakeMatcher()
    monkeypatch.setattr(memory_route, "memory_cmd", matcher)
    monkeypatch.setattr(memory_route, "is_allowed", lambda e: True)
    monkeypatch.setattr(memory_route, "_get_driver", lambda: _FakeDriver(store))
    with pytest.raises(FinishedException):
        await memory_route.handle_memory(_event(text, user_id))
    return str(matcher.finished)


@pytest.fixture
def store():
    return InMemoryMemoryStore()


@pytest.fixture(autouse=True)
def _clean_pending():
    memory_route._pending_clear.clear()
    yield
    memory_route._pending_clear.clear()


@pytest.mark.asyncio
async def test_list_shows_only_own_facts(monkeypatch, store):
    await store.save_fact("10000", "喜欢猫", [1.0, 0.0], source="chat")
    await store.save_fact("20000", "别人的秘密", [0.0, 1.0])

    msg = await _run(monkeypatch, store, "/memory")

    assert "喜欢猫" in msg
    assert "别人的秘密" not in msg, "不得列出他人的事实"
    assert "#1" in msg, "必须给出可删除的 id"
    assert "/memory forget" in msg


@pytest.mark.asyncio
async def test_empty_list_explains(monkeypatch, store):
    msg = await _run(monkeypatch, store, "/memory")
    assert "还没有长期记忆" in msg


@pytest.mark.asyncio
async def test_forget_own_fact(monkeypatch, store):
    await store.save_fact("10000", "喜欢猫", [1.0, 0.0])
    detail = await store.list_facts_detailed("10000")

    msg = await _run(monkeypatch, store, f"/memory forget {detail[0]['id']}")

    assert "已删除" in msg
    assert await store.list_facts("10000") == []


@pytest.mark.asyncio
async def test_forget_cannot_touch_other_user(monkeypatch, store):
    await store.save_fact("20000", "别人的秘密", [1.0, 0.0])
    other = await store.list_facts_detailed("20000")

    msg = await _run(monkeypatch, store, f"/memory forget {other[0]['id']}")

    assert "没有找到" in msg
    assert await store.list_facts("20000") == ["别人的秘密"], "跨用户删除必须失败"


@pytest.mark.asyncio
async def test_forget_rejects_bad_id(monkeypatch, store):
    msg = await _run(monkeypatch, store, "/memory forget abc")
    assert "用法" in msg


@pytest.mark.asyncio
async def test_clear_requires_matching_confirmation(monkeypatch, store):
    await store.save_fact("10000", "事实一", [1.0, 0.0])
    await store.save_fact("10000", "事实二", [0.0, 1.0])
    await store.save_fact("20000", "别人的秘密", [1.0, 0.0])

    first = await _run(monkeypatch, store, "/memory clear")
    assert "确认" in first and "/memory clear" in first
    assert len(await store.list_facts("10000")) == 2, "第一段不得删除"

    wrong = await _run(monkeypatch, store, "/memory clear WRONG1")
    assert "无效" in wrong
    assert len(await store.list_facts("10000")) == 2

    code = re.search(r"/memory clear ([0-9A-F]{6})", first).group(1)
    done = await _run(monkeypatch, store, f"/memory clear {code}")
    assert "已清空 2 条" in done
    assert await store.list_facts("10000") == []
    assert await store.list_facts("20000") == ["别人的秘密"], "清空不得越用户"

    reused = await _run(monkeypatch, store, f"/memory clear {code}")
    assert "无效" in reused, "确认码一次性"


@pytest.mark.asyncio
async def test_expired_confirmation_is_rejected(monkeypatch, store):
    await store.save_fact("10000", "事实", [1.0, 0.0])
    memory_route._pending_clear["10000"] = ("ABC123", time.monotonic() - 1)

    msg = await _run(monkeypatch, store, "/memory clear ABC123")

    assert "过期" in msg
    assert await store.list_facts("10000") == ["事实"]


@pytest.mark.asyncio
async def test_acl_denied(monkeypatch, store):
    called = []

    async def fake_deny(matcher, event, message):
        called.append(message)
        raise FinishedException

    monkeypatch.setattr(memory_route, "is_allowed", lambda e: False)
    monkeypatch.setattr(memory_route, "deny", fake_deny)
    monkeypatch.setattr(memory_route, "_get_driver", lambda: _FakeDriver(store))
    with pytest.raises(FinishedException):
        await memory_route.handle_memory(_event("/memory"))
    assert called == ["无权限"]


def test_alias_and_unknown_command():
    assert memory_route.parse_memory_cmd("/记忆") == ("list", "")
    assert memory_route.parse_memory_cmd("/memory rm 3") == ("forget", "3")
    assert memory_route.parse_memory_cmd("/memory 随便") == ("随便", "")
