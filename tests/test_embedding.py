import json
import math

import httpx
import pytest

from agentcore.embedding.client import EmbeddingClient, load_embedding_client_from_env


class TestLocalEmbedding:
    def test_same_text_same_vector(self):
        client = EmbeddingClient()
        a = client._local_embed("用户住在北京")
        b = client._local_embed("用户住在北京")
        assert a == b

    def test_dimension(self):
        client = EmbeddingClient()
        v = client._local_embed("测试")
        assert len(v) == 2048

    def test_normalized(self):
        client = EmbeddingClient()
        v = client._local_embed("hello world")
        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 1e-6

    def test_similar_text_closer_than_unrelated(self):
        client = EmbeddingClient()
        q = client._local_embed("今天天气怎么样")
        same = client._local_embed("今天天气怎么样")
        related = client._local_embed("天气预报说明天有雨")
        unrelated = client._local_embed("量子物理和弦理论")
        sim_same = sum(x * y for x, y in zip(q, same, strict=False))
        sim_rel = sum(x * y for x, y in zip(q, related, strict=False))
        sim_un = sum(x * y for x, y in zip(q, unrelated, strict=False))
        assert sim_same > sim_rel > sim_un

    @pytest.mark.asyncio
    async def test_embed_many_async(self):
        client = EmbeddingClient()
        vecs = await client.embed_many(["a", "b"])
        assert len(vecs) == 2
        assert all(len(v) == 2048 for v in vecs)

    @pytest.mark.asyncio
    async def test_probe_dim_local(self):
        client = EmbeddingClient(dim=1024)
        assert await client.probe_dim() == 1024


def _patch_transport(monkeypatch, handler):
    """把 AsyncClient 换成走 MockTransport 的工厂，拦截 _remote_embed 的真实 HTTP。"""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("agentcore.embedding.client.httpx.AsyncClient", factory)


class TestRemoteEmbedding:
    def _client(self, **kw):
        return EmbeddingClient(base_url="https://api.test", api_key="k", **kw)

    @pytest.mark.asyncio
    async def test_long_input_splits_into_batches_and_keeps_order(self, monkeypatch):
        sizes = []

        def handler(request: httpx.Request) -> httpx.Response:
            texts = json.loads(request.content)["input"]
            sizes.append(len(texts))
            # 用文本长度编码向量，验证合并后顺序与原文一一对应
            return httpx.Response(
                200,
                json={"data": [{"index": i, "embedding": [float(len(t))] * 3} for i, t in enumerate(texts)]},
            )

        _patch_transport(monkeypatch, handler)
        texts = [f"t{i}".ljust(i + 2) for i in range(25)]  # 25 条 → 10/10/5 三批
        vecs = await self._client(batch=10)._remote_embed(texts)
        assert sizes == [10, 10, 5]
        assert [v[0] for v in vecs] == [float(len(t)) for t in texts]

    @pytest.mark.asyncio
    async def test_error_surfaces_body_and_batch_range(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"error": {"message": "input array size exceed limit 10"}}
            )

        _patch_transport(monkeypatch, handler)
        with pytest.raises(RuntimeError) as ei:
            await self._client(batch=10)._remote_embed([f"t{i}" for i in range(12)])
        msg = str(ei.value)
        assert "400" in msg and "第 1-10 条 / 共 12 条" in msg
        assert "input array size exceed limit 10" in msg

    def test_loader_reads_batch_env(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.test")
        monkeypatch.setenv("EMBEDDING_API_KEY", "k")
        monkeypatch.setenv("EMBEDDING_BATCH", "7")
        assert load_embedding_client_from_env().batch == 7

    @pytest.mark.asyncio
    async def test_batch_floored_to_one(self, monkeypatch):
        sizes = []

        def handler(request: httpx.Request) -> httpx.Response:
            texts = json.loads(request.content)["input"]
            sizes.append(len(texts))
            return httpx.Response(
                200, json={"data": [{"index": i, "embedding": [1.0]} for i, t in enumerate(texts)]}
            )

        _patch_transport(monkeypatch, handler)
        await self._client(batch=0)._remote_embed(["a", "b"])
        assert sizes == [1, 1]
