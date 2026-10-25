"""长期记忆自助命令（/memory）：查看 / 删除 / 清空的权限与确认语义。

存储层契约在 tests/test_store_contract.py；本文件锁平台接线：CLI 解析、
「只能操作自己」、**当前会话作用域**（REVIEW-ee39c8e..e55cab5 M1：群里
不得回显私聊/其他群的事实）、清空两段确认与过期、加载器接线（L5）。
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
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


def _group_event(
    text: str, user_id: int = 10000, group_id: int = 456
) -> GroupMessageEvent:
    segs = [{"type": "text", "data": {"text": text}}]
    return GroupMessageEvent.parse_obj(
        {
            "time": 0,
            "self_id": 1,
            "post_type": "message",
            "sub_type": "group",
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
            "anonymous": None,
        }
    )


async def _save(
    store: InMemoryMemoryStore,
    user_id: str,
    content: str,
    group_id: str | None = None,
    embedding: list[float] | None = None,
) -> int:
    """按会话键写一条事实；返回事实 id。"""
    session_id = await store.resolve_session(user_id, group_id)
    await store.save_fact(
        user_id,
        content,
        embedding or [1.0, 0.0],
        source="chat",
        session_id=session_id,
    )
    items = await store.list_facts_detailed(user_id, session_id=session_id)
    return next(int(it["id"]) for it in items if it["content"] == content)


async def _run_event(monkeypatch, store, event) -> str:
    matcher = _FakeMatcher()
    monkeypatch.setattr(memory_route, "memory_cmd", matcher)
    monkeypatch.setattr(memory_route, "is_allowed", lambda e: True)
    monkeypatch.setattr(memory_route, "_get_driver", lambda: _FakeDriver(store))
    with pytest.raises(FinishedException):
        await memory_route.handle_memory(event)
    return str(matcher.finished)


async def _run(monkeypatch, store, text: str, user_id: int = 10000) -> str:
    return await _run_event(monkeypatch, store, _event(text, user_id))


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
    await _save(store, "10000", "喜欢猫")
    await _save(store, "20000", "别人的秘密")

    msg = await _run(monkeypatch, store, "/memory")

    assert "喜欢猫" in msg
    assert "别人的秘密" not in msg, "不得列出他人的事实"
    assert "#1" in msg, "必须给出可删除的 id"
    assert "/memory forget" in msg


@pytest.mark.asyncio
async def test_list_is_scoped_to_current_session(monkeypatch, store):
    """M1：群里不得回显私聊/其他群的事实正文。"""
    await _save(store, "10000", "私聊的秘密")
    await _save(store, "10000", "群A的秘密", group_id="111")
    await _save(store, "10000", "群B的日常", group_id="222")

    private_msg = await _run(monkeypatch, store, "/memory")
    assert "私聊的秘密" in private_msg
    assert "群A的秘密" not in private_msg
    assert "群B的日常" not in private_msg

    group_a = await _run_event(
        monkeypatch, store, _group_event("/memory", group_id=111)
    )
    assert "群A的秘密" in group_a
    assert "私聊的秘密" not in group_a, "群聊不得回显私聊事实"
    assert "群B的日常" not in group_a, "群聊不得回显其他群的事实"

    group_b = await _run_event(
        monkeypatch, store, _group_event("/memory", group_id=222)
    )
    assert "群B的日常" in group_b
    assert "群A的秘密" not in group_b


@pytest.mark.asyncio
async def test_forget_cannot_cross_session(monkeypatch, store):
    """M1：群里的 /memory forget 不得删掉私聊事实。"""
    private_id = await _save(store, "10000", "私聊的秘密")

    msg = await _run_event(
        monkeypatch,
        store,
        _group_event(f"/memory forget {private_id}", group_id=111),
    )

    assert "没有找到" in msg
    assert "私聊的秘密" in await store.list_facts("10000"), "跨会话删除必须失败"

    # 本会话内仍可正常删除
    group_id = await _save(store, "10000", "群A的秘密", group_id="111")
    done = await _run_event(
        monkeypatch, store, _group_event(f"/memory forget {group_id}", group_id=111)
    )
    assert "已删除" in done
    assert "群A的秘密" not in await store.list_facts("10000")


@pytest.mark.asyncio
async def test_empty_list_explains(monkeypatch, store):
    msg = await _run(monkeypatch, store, "/memory")
    assert "还没有长期记忆" in msg


@pytest.mark.asyncio
async def test_forget_own_fact(monkeypatch, store):
    fact_id = await _save(store, "10000", "喜欢猫")

    msg = await _run(monkeypatch, store, f"/memory forget {fact_id}")

    assert "已删除" in msg
    assert await store.list_facts("10000") == []


@pytest.mark.asyncio
async def test_forget_cannot_touch_other_user(monkeypatch, store):
    other_id = await _save(store, "20000", "别人的秘密")

    msg = await _run(monkeypatch, store, f"/memory forget {other_id}")

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
async def test_confirmation_survives_store_failure(monkeypatch, store):
    """L11：清空失败不得吃掉确认码（TTL 内可重试）。"""
    await _save(store, "10000", "事实")
    first = await _run(monkeypatch, store, "/memory clear")
    code = re.search(r"/memory clear ([0-9A-F]{6})", first).group(1)

    calls = {"n": 0}
    original = store.clear_facts

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "clear_facts", flaky)
    failed = await _run(monkeypatch, store, f"/memory clear {code}")
    assert "失败" in failed
    assert await store.list_facts("10000") == ["事实"]

    done = await _run(monkeypatch, store, f"/memory clear {code}")
    assert "已清空 1 条" in done
    assert await store.list_facts("10000") == []


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


# ==========================================================================
# L5（REVIEW-ee39c8e..e55cab5）：/memory 是模块级 on_command 注册，必须由
# _load_plugin_modules() import；漏接线时命令静默不存在（与 2026-09-30
# /reboot 漏接线同类）。子进程从零 init，唯一导入路径就是加载器——本进程
# 已 import 过模块，同进程断言对「加载器漏 import」不敏感。
# ==========================================================================

_LOADER_SCRIPT = """
import os
os.environ["SUPERUSERS"] = '["10000"]'
from nonebot import init, get_driver
from nonebot.adapters.onebot.v11 import Adapter
init(); get_driver().register_adapter(Adapter)
import plugins.qq_agent_adapter as pkg
pkg._load_plugin_modules()
from nonebot.rule import TrieRule
print("MEMORY_ROUTE:", pkg.memory_route is not None)
for prefix in ("/memory", "/记忆"):
    print("PREFIX:", prefix, prefix in TrieRule.prefix)
"""


def test_memory_module_loaded_by_plugin_loader():
    proc = subprocess.run(
        [sys.executable, "-c", _LOADER_SCRIPT],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "MEMORY_ROUTE: True" in proc.stdout, proc.stdout
    for prefix in ("/memory", "/记忆"):
        assert f"PREFIX: {prefix} True" in proc.stdout, (prefix, proc.stdout)
