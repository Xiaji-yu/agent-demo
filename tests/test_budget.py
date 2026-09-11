"""M7 成本预算：按日累计、持久化、软/硬闸门、LLM/embedding 用量上报、蒸馏闸门。"""
import json
from datetime import date

import httpx
import pytest

import agentcore.budget as budget_mod
from agentcore.budget import CostBudget
from agentcore.embedding.client import EmbeddingClient
from agentcore.llm.client import LLMClient
from agentcore.memory.store import InMemoryMemoryStore
from agentcore.rag.service import KnowledgeBase


class TestCostBudgetLogic:
    def test_record_accumulates_and_persists(self, tmp_path):
        b1 = CostBudget(root=tmp_path)
        b1.record("chat", prompt_tokens=30, completion_tokens=20)
        b1.record("chat", prompt_tokens=10, completion_tokens=5)
        b1.record("embedding", prompt_tokens=99)
        day = b1.today()
        assert day["prompt"] == 40 and day["completion"] == 25
        assert day["embedding_tokens"] == 99 and day["chat_requests"] == 2
        # 新实例读同一目录：重启不丢
        b2 = CostBudget(root=tmp_path)
        assert b2.today()["total"] == 65

    def test_soft_vs_hard_gate(self, tmp_path):
        soft = CostBudget(root=tmp_path / "s", daily_tokens=100, enforce=False)
        soft.record("chat", prompt_tokens=60, completion_tokens=50)
        assert soft.chat_blocked() == (False, "")

        hard = CostBudget(root=tmp_path / "h", daily_tokens=100, enforce=True)
        hard.record("chat", prompt_tokens=60, completion_tokens=39)  # 99 < 100
        assert hard.chat_blocked() == (False, "")
        hard.record("chat", prompt_tokens=2)  # 101 >= 100
        blocked, reason = hard.chat_blocked()
        assert blocked is True and "预算" in reason

    def test_no_budget_never_blocks_without_writing_disk(self, tmp_path):
        b = CostBudget(root=tmp_path, daily_tokens=0, enforce=True)
        assert b.chat_blocked() == (False, "")
        assert not list(tmp_path.glob("usage-*.json")), "闸门早退不得产生任何落盘"

    def test_record_month_boundary_uses_single_now(self, tmp_path, monkeypatch):
        """M4 回归（REVIEW-fad144b..bbd8913）：_day 与 _save 必须同源取时，
        跨月午夜不能把 9 月的账写进 10 月文件。"""
        pending = [date(2026, 9, 30), date(2026, 10, 1)]

        class _FakeDate:
            @staticmethod
            def today():
                return pending.pop(0) if pending else date(2026, 10, 1)

        monkeypatch.setattr(budget_mod, "date", _FakeDate)
        b = CostBudget(root=tmp_path)
        b.record("chat", prompt_tokens=5)
        assert (tmp_path / "usage-2026-09.json").exists()
        assert not (tmp_path / "usage-2026-10.json").exists()
        data = json.loads((tmp_path / "usage-2026-09.json").read_text(encoding="utf-8"))
        assert data["days"]["2026-09-30"]["prompt"] == 5

    def test_estimate_cost(self, tmp_path):
        b = CostBudget(root=tmp_path, price_prompt_per_m=1.0, price_completion_per_m=3.0)
        b.record("chat", prompt_tokens=1_000_000, completion_tokens=1)
        assert abs(b.estimate_cost() - (1.0 + 3.0 / 1e6)) < 1e-9
        assert CostBudget(root=tmp_path).estimate_cost() is None


class TestUsageHooks:
    @pytest.mark.asyncio
    async def test_llm_chat_records_usage(self, tmp_path, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                },
            )

        client = LLMClient()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(budget_mod, "_default", CostBudget(root=tmp_path))
        await client.chat([{"role": "user", "content": "hi"}])
        day = budget_mod._default.today()
        assert day["prompt"] == 11 and day["completion"] == 7 and day["chat_requests"] == 1

    @pytest.mark.asyncio
    async def test_embedding_records_usage(self, tmp_path, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": [1.0, 2.0]}],
                    "usage": {"total_tokens": 42},
                },
            )

        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            return real(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr("agentcore.embedding.client.httpx.AsyncClient", factory)
        monkeypatch.setattr(budget_mod, "_default", CostBudget(root=tmp_path))
        await EmbeddingClient(base_url="https://api.test", api_key="k")._remote_embed(["x"])
        day = budget_mod._default.today()
        assert day["embedding_tokens"] == 42
        assert day["total"] == 0, "embedding 用量不计入对话预算"


class TestDigestBudgetGate:
    @pytest.mark.asyncio
    async def test_digest_skips_when_over_budget(self, tmp_path, monkeypatch):
        class _Emb:
            async def embed_many(self, texts):
                return [[0.0] for _ in texts]

        class _LLM:
            calls = 0

            async def chat(self, messages, tools=None, max_tokens=None):
                _LLM.calls += 1
                return {"choices": [{"message": {"content": "[]"}}]}

        monkeypatch.setattr(
            budget_mod, "_default", CostBudget(root=tmp_path, daily_tokens=10, enforce=True)
        )
        budget_mod._default.record("chat", prompt_tokens=10)
        kb = KnowledgeBase(InMemoryMemoryStore(), _Emb(), {"min_chars": 10}, llm=_LLM())
        result = await kb.digest()
        assert result == {"status": "skipped", "reason": "daily budget exceeded"}
        assert _LLM.calls == 0
