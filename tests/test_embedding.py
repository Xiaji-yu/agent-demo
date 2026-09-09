import math

import pytest

from agentcore.embedding.client import EmbeddingClient


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
        sim_same = sum(x * y for x, y in zip(q, same))
        sim_rel = sum(x * y for x, y in zip(q, related))
        sim_un = sum(x * y for x, y in zip(q, unrelated))
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
