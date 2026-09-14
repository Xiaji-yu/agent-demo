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
                json={
                    "data": [
                        {"index": i, "embedding": [float(len(t))] * 3}
                        for i, t in enumerate(texts)
                    ]
                },
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

    def test_loader_reads_timeout_env(self, monkeypatch):
        """本地 CPU 推理必须能调大超时（默认 30s 对 bge-m3 单批 10 条不够）。"""
        monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.test")
        monkeypatch.setenv("EMBEDDING_API_KEY", "k")
        monkeypatch.setenv("EMBEDDING_TIMEOUT", "180")
        assert load_embedding_client_from_env().timeout == 180.0

    def test_empty_timeout_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.test")
        monkeypatch.setenv("EMBEDDING_API_KEY", "k")
        monkeypatch.setenv("EMBEDDING_TIMEOUT", "")
        assert load_embedding_client_from_env().timeout == 30.0

    @pytest.mark.parametrize("dirty", ["abc", "0", "-5", "inf", "nan"])
    def test_dirty_timeout_env_falls_back_with_warning(
        self, monkeypatch, caplog, dirty
    ):
        """脏值/nan/inf 不得让启动崩，也不得静默变成 0（=立即超时）。"""
        import logging

        monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.test")
        monkeypatch.setenv("EMBEDDING_API_KEY", "k")
        monkeypatch.setenv("EMBEDDING_TIMEOUT", dirty)
        with caplog.at_level(logging.WARNING):
            client = load_embedding_client_from_env()
        assert client.timeout == 30.0
        assert any("EMBEDDING_TIMEOUT" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_timeout_is_forwarded_to_httpx(self, monkeypatch):
        captured = {}
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            captured.update(kwargs)
            return real(
                *args,
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(
                        200, json={"data": [{"index": 0, "embedding": [1.0]}]}
                    )
                ),
                **kwargs,
            )

        monkeypatch.setattr("agentcore.embedding.client.httpx.AsyncClient", factory)
        await self._client(timeout=123.0)._remote_embed(["a"])
        assert captured.get("timeout") == 123.0

    @pytest.mark.asyncio
    async def test_timeout_exception_is_logged_with_type(self, monkeypatch, caplog):
        """httpx.ReadTimeout 的 ``str()`` 是空串——日志必须带类型名，否则无从排障。

        真实故障：`/kb samples` 每个切块都失败，日志只有「failed: 」，看不到
        是超时还是服务没起。
        """
        import logging

        async def boom(self, client, batch, start, total):
            raise httpx.ReadTimeout("")

        monkeypatch.setattr(EmbeddingClient, "_post_embeddings", boom)
        client = self._client(timeout=45.0)
        with caplog.at_level(logging.WARNING):
            with pytest.raises(httpx.ReadTimeout):
                await client._remote_embed(["a"])

        assert "ReadTimeout" in caplog.text
        assert "timeout=45" in caplog.text

    @pytest.mark.asyncio
    async def test_batch_floored_to_one(self, monkeypatch):
        sizes = []

        def handler(request: httpx.Request) -> httpx.Response:
            texts = json.loads(request.content)["input"]
            sizes.append(len(texts))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [1.0]} for i, t in enumerate(texts)
                    ]
                },
            )

        _patch_transport(monkeypatch, handler)
        await self._client(batch=0)._remote_embed(["a", "b"])
        assert sizes == [1, 1]


class TestOnErrorNotify:
    @pytest.mark.asyncio
    async def test_on_error_fires_on_remote_failure(self, monkeypatch):
        from agentcore.embedding.client import EmbeddingClient

        client = EmbeddingClient(
            base_url="http://127.0.0.1:9",  # 不可达端口
            api_key="x",
            model="m",
        )
        client._error_notify_cooldown = 0.0
        fired = []

        async def cb(exc):
            fired.append(exc)

        client.on_error = cb
        with pytest.raises(httpx.HTTPError):
            await client.embed_many(["a"])
        assert len(fired) == 1

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_repeat(self, monkeypatch):
        from agentcore.embedding.client import EmbeddingClient

        client = EmbeddingClient(base_url="http://127.0.0.1:9", api_key="x", model="m")
        client._error_notify_cooldown = 600.0  # 冷却期内只报一次
        fired = []

        async def cb(exc):
            fired.append(exc)

        client.on_error = cb
        with pytest.raises(httpx.HTTPError):
            await client.embed_many(["a"])
        with pytest.raises(httpx.HTTPError):
            await client.embed_many(["b"])
        assert len(fired) == 1

    @pytest.mark.asyncio
    async def test_first_notify_not_suppressed_on_fresh_boot(self, monkeypatch):
        """回归：`time.monotonic()` 是开机秒数，刚重启的机器 uptime < cooldown 时，
        首次告警不能被冷却逻辑吞掉（旧实现用 0.0 作哨兵 → CI 新开机 runner 实测复现；
        本地长 uptime 机器测不出来）。"""
        import agentcore.embedding.client as mod

        monkeypatch.setattr(mod.time, "monotonic", lambda: 5.0)  # 开机仅 5 秒
        client = mod.EmbeddingClient(
            base_url="http://127.0.0.1:9", api_key="x", model="m"
        )
        fired = []

        async def cb(exc):
            fired.append(exc)

        client.on_error = cb
        with pytest.raises(httpx.HTTPError):
            await client.embed_many(["a"])
        assert len(fired) == 1, "开机秒数小于冷却时长时，首次告警仍必须发出"

        with pytest.raises(httpx.HTTPError):
            await client.embed_many(["b"])
        assert len(fired) == 1, "冷却期内的第二次必须被抑制"

    @pytest.mark.asyncio
    async def test_no_callback_no_crash(self):
        from agentcore.embedding.client import EmbeddingClient

        client = EmbeddingClient(base_url="http://127.0.0.1:9", api_key="x", model="m")
        with pytest.raises(httpx.HTTPError):
            await client.embed_many(["a"])
