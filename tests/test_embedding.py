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

        async def boom(self, client, batch, start, total, **kwargs):
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


class TestEmbeddingProgress:
    """大批量嵌入的进度可观测性。

    动机（实测）：`ingest_text` 按**切块原子提交**——一份 930 块要全部嵌入完才写库，
    本地 CPU 上要 40 多分钟。中间没有任何输出的话，用户无法区分「在慢慢跑」与
    「卡死」（真实踩过：一小时没看到任何日志）。
    """

    def _client(self, **kw):
        return EmbeddingClient(base_url="https://api.test", api_key="k", **kw)

    @staticmethod
    def _handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": [1.0]} for i, t in enumerate(texts)]
            },
        )

    @pytest.mark.asyncio
    async def test_on_progress_receives_running_totals(self, monkeypatch):
        _patch_transport(monkeypatch, self._handler)
        seen: list[tuple[int, int]] = []
        client = self._client(batch=10)
        client.on_progress = lambda done, total: seen.append((done, total))

        await client._remote_embed([f"t{i}" for i in range(25)])

        assert seen == [(10, 25), (20, 25), (25, 25)]

    @pytest.mark.asyncio
    async def test_progress_logged_at_interval(self, monkeypatch, caplog):
        import logging

        _patch_transport(monkeypatch, self._handler)
        client = self._client(batch=10, progress_every=10)
        with caplog.at_level(logging.INFO, logger="agentcore.embedding.client"):
            await client._remote_embed([f"t{i}" for i in range(25)])

        assert "embedding 进度 10/25" in caplog.text
        assert "embedding 进度 20/25" in caplog.text
        assert "embedding 完成 25 块" in caplog.text

    @pytest.mark.asyncio
    async def test_progress_zero_disables_logging(self, monkeypatch, caplog):
        import logging

        _patch_transport(monkeypatch, self._handler)
        client = self._client(batch=10, progress_every=0)
        with caplog.at_level(logging.INFO, logger="agentcore.embedding.client"):
            await client._remote_embed([f"t{i}" for i in range(25)])

        assert "embedding 进度" not in caplog.text
        assert "embedding 完成" not in caplog.text

    @pytest.mark.asyncio
    async def test_progress_callback_failure_does_not_break_embedding(
        self, monkeypatch
    ):
        """宿主回调抛错不能影响嵌入本身（否则一个状态写入 bug 会拖垮导入）。"""
        _patch_transport(monkeypatch, self._handler)
        client = self._client(batch=10)

        def boom(done, total):
            raise RuntimeError("state write failed")

        client.on_progress = boom
        vecs = await client._remote_embed([f"t{i}" for i in range(25)])

        assert len(vecs) == 25
        assert client.on_progress is None, "失败后应摘掉回调，避免刷日志"


class TestOnErrorNotify:
    """远程失败时通知宿主（管理员提醒）。

    新语义（评审复盘 P1）：通知宿主后**响亮失败**（raise），不再降级 hash；
    失败发生在 _post_embeddings 的退避重试耗尽之后。
    """

    @pytest.fixture
    def no_delay(self, monkeypatch):
        """重试零延迟（避免测试真的 sleep）。"""
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "0")

    @pytest.mark.asyncio
    async def test_on_error_fires_on_remote_failure(self, monkeypatch, no_delay):
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
        # 新语义：通知宿主后 raise（不再返回 hash）
        with pytest.raises(RuntimeError):
            await client.embed_many(["a"])
        assert len(fired) == 1

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_repeat(self, monkeypatch, no_delay):
        from agentcore.embedding.client import EmbeddingClient

        client = EmbeddingClient(base_url="http://127.0.0.1:9", api_key="x", model="m")
        client._error_notify_cooldown = 600.0  # 冷却期内只报一次
        fired = []

        async def cb(exc):
            fired.append(exc)

        client.on_error = cb
        with pytest.raises(RuntimeError):
            await client.embed_many(["a"])
        with pytest.raises(RuntimeError):
            await client.embed_many(["b"])
        assert len(fired) == 1

    @pytest.mark.asyncio
    async def test_first_notify_not_suppressed_on_fresh_boot(
        self, monkeypatch, no_delay
    ):
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
        with pytest.raises(RuntimeError):
            await client.embed_many(["a"])
        assert len(fired) == 1, "开机秒数小于冷却时长时，首次告警仍必须发出"

        with pytest.raises(RuntimeError):
            await client.embed_many(["b"])
        assert len(fired) == 1, "冷却期内的第二次必须被抑制"

    @pytest.mark.asyncio
    async def test_no_callback_no_crash(self, monkeypatch, no_delay):
        from agentcore.embedding.client import EmbeddingClient

        client = EmbeddingClient(base_url="http://127.0.0.1:9", api_key="x", model="m")
        # 无回调也不崩（通知路径跳过，仍 raise）
        with pytest.raises(RuntimeError):
            await client.embed_many(["a"])


class _FakeResp:
    def __init__(
        self,
        status: int,
        text: str = "",
        json_data: dict | None = None,
        headers: dict | None = None,
    ):
        self.status_code = status
        self._text = text
        self._json = json_data or {}
        # L12：Retry-After 读取需要响应头；替身必须与 httpx.Response 的形状一致
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self._text

    def json(self) -> dict:
        return self._json


class TestRemoteRetryAndFail:
    """远程失败的运行期语义（评审复盘 P1 落地，对应 FIX-embedding-deploy-20260918）：

    - 429 限流 / 5xx / 超时 / 断连 → **退避重试**（重发而非降级）
    - 4xx（404 模型名错等配置错误）→ **响亮失败**（raise）
    - **任何情况都不再降级 hash**——hash 曾把硅基流动 TPM 限流期间导入的
      KB 块污染成垃圾向量（实测 42119 块中 3854 块）
    """

    @pytest.fixture
    def no_delay(self, monkeypatch):
        """重试零延迟（避免测试真的 sleep）。"""
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "0")

    @pytest.mark.asyncio
    async def test_429_retries_then_succeeds(self, monkeypatch, no_delay):
        """429 限流：退避后重发成功——污染防线（重发而非写 hash）。"""
        state = {"n": 0}

        async def flaky(self, client, batch):
            state["n"] += 1
            if state["n"] == 1:
                return _FakeResp(429, text='{"message":"TPM limit reached"}')
            return _FakeResp(
                200,
                json_data={
                    "data": [{"index": 0, "embedding": [1.0, 2.0]}],
                    "usage": {"total_tokens": 3},
                },
            )

        monkeypatch.setattr(EmbeddingClient, "_post_once", flaky)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=2)
        vecs = await client.embed_many(["x"])
        assert vecs == [[1.0, 2.0]]
        assert state["n"] == 2  # 1 次 429 + 1 次成功

    @pytest.mark.asyncio
    async def test_404_raises_without_retry(self, monkeypatch):
        """404 配置错误：立即 raise 且**不重试**（重试无意义）。"""

        async def not_found(self, client, batch):
            return _FakeResp(404, text='{"error":{"message":"model not found"}}')

        monkeypatch.setattr(EmbeddingClient, "_post_once", not_found)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=2)
        with pytest.raises(RuntimeError, match="404"):
            await client.embed_many(["x"])

    @pytest.mark.asyncio
    async def test_429_retry_exhausted_raises(self, monkeypatch, no_delay):
        """持续限流：重试次数耗尽后 raise（而非降级 hash 写库）。"""
        n = {"c": 0}

        async def always_429(self, client, batch):
            n["c"] += 1
            return _FakeResp(429, text="{}")

        monkeypatch.setattr(EmbeddingClient, "_post_once", always_429)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=2)
        with pytest.raises(RuntimeError, match="重试"):
            await client.embed_many(["x"])
        # L14（REVIEW-6ec3f7c..a36ea1d）：原先写 client.retry_count + 1 是**自引用
        # 断言**——把默认值从 5 改成 3 也照样通过。这里改成字面量 + 独立断言默认值。
        assert client.retry_count == 5, "默认重试次数（文档声称 5）"
        assert n["c"] == 6, "1 次原始 + 5 次重试"

    @pytest.mark.asyncio
    async def test_timeout_retry_exhausted_raises_literal(self, monkeypatch, no_delay):
        """同上的字面量版（原断言同样是自引用）。"""
        n = {"c": 0}

        async def boom(self, client, batch):
            n["c"] += 1
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(EmbeddingClient, "_post_once", boom)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=2)
        with pytest.raises(RuntimeError, match="重试"):
            await client.embed_many(["x"])
        assert n["c"] == 6

    @pytest.mark.asyncio
    async def test_timeout_retry_exhausted_raises(self, monkeypatch, no_delay):
        """超时重试耗尽：raise（超时也重发而非降级）。"""
        n = {"c": 0}

        async def boom(self, client, batch):
            n["c"] += 1
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(EmbeddingClient, "_post_once", boom)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=2)
        with pytest.raises(RuntimeError, match="重试"):
            await client.embed_many(["x"])
        assert n["c"] == 6  # 字面量（L14：不再自引用 retry_count）

    @pytest.mark.asyncio
    async def test_local_mode_returns_hash(self):
        """未配置远程（无 base_url/key）：合法的本地模式，直接 hash。"""
        client = EmbeddingClient(base_url="", api_key="", dim=8)
        vecs = await client.embed_many(["你好世界"])
        assert len(vecs) == 1 and len(vecs[0]) == 8

    @pytest.mark.asyncio
    async def test_probe_dim_failure_does_not_raise(self, monkeypatch, no_delay):
        """probe 失败不抛异常（不阻塞启动），回落配置维度。"""

        async def boom(self, client, batch):
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(EmbeddingClient, "_post_once", boom)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=64)
        assert await client.probe_dim() == 64


# ==========================================================================
# REVIEW-6ec3f7c..a36ea1d 修复回归
#   M2 退避预算按路径分离（探测单次 / 交互有墙钟上限）
#   M15 断连家族（RemoteProtocolError/ProxyError）必须重试
#   L12 Retry-After 优先；413/422 给"调小 batch"指引
#   L13 重试 env 脏值/越界要告警并 clamp
# ==========================================================================


class TestRetryBudgetSplit:
    """M2：probe 与交互路径不得陪跑批量级退避。"""

    @pytest.mark.asyncio
    async def test_probe_dim_makes_single_attempt(self, monkeypatch):
        """探测必须**只试一次**：旧实现会把 on_startup 挂 ~15 分钟。"""
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "0")
        n = {"c": 0}

        async def boom(self, client, batch):
            n["c"] += 1
            raise httpx.ConnectError("down")

        monkeypatch.setattr(EmbeddingClient, "_post_once", boom)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=64)
        assert await client.probe_dim() == 64
        assert n["c"] == 1, "probe 应单次尝试（retry_count=0）"

    @pytest.mark.asyncio
    async def test_interactive_uses_its_own_retry_count(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "0")
        monkeypatch.setenv("EMBEDDING_INTERACTIVE_RETRY_COUNT", "2")
        n = {"c": 0}

        async def boom(self, client, batch):
            n["c"] += 1
            raise httpx.ConnectError("down")

        monkeypatch.setattr(EmbeddingClient, "_post_once", boom)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=2)
        with pytest.raises(RuntimeError):
            await client.embed_many(["x"], interactive=True)
        assert n["c"] == 3, "1 次原始 + interactive_retry_count(2) 次"
        assert client.retry_count == 5, "批量预算不受交互预算影响"

    @pytest.mark.asyncio
    async def test_interactive_deadline_fails_fast_without_sleeping(self, monkeypatch):
        """墙钟上限：退避会超出预算时直接失败，而不是先睡满再失败。"""
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "600")  # 一次退避就远超预算
        monkeypatch.setenv("EMBEDDING_INTERACTIVE_BUDGET", "5")
        n = {"c": 0}
        slept: list[float] = []

        async def boom(self, client, batch):
            n["c"] += 1
            raise httpx.ConnectError("down")

        async def fake_sleep(secs):
            slept.append(secs)

        monkeypatch.setattr(EmbeddingClient, "_post_once", boom)
        monkeypatch.setattr("agentcore.embedding.client.asyncio.sleep", fake_sleep)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=2)
        with pytest.raises(RuntimeError):
            await client.embed_many(["x"], interactive=True)
        assert slept == [], "超出预算时不得再 sleep"
        assert n["c"] == 1

    @pytest.mark.asyncio
    async def test_bulk_path_keeps_long_budget(self, monkeypatch):
        """批量摄取仍按 retry_count 重试（正确性优先，是本次修复**保留**的行为）。"""
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "0")
        n = {"c": 0}

        async def boom(self, client, batch):
            n["c"] += 1
            raise httpx.ConnectError("down")

        monkeypatch.setattr(EmbeddingClient, "_post_once", boom)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=2)
        with pytest.raises(RuntimeError):
            await client.embed_many(["x"])  # 非 interactive
        assert n["c"] == 6

    @pytest.mark.asyncio
    async def test_backoff_sequence_is_linear_growth(self, monkeypatch):
        """退避间隔序列（L14：此前从无断言，改 sleep(0) 也全绿）。"""
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "10")
        monkeypatch.setenv("EMBEDDING_RETRY_COUNT", "4")
        slept: list[float] = []

        async def boom(self, client, batch):
            raise httpx.ConnectError("down")

        async def fake_sleep(secs):
            slept.append(secs)

        monkeypatch.setattr(EmbeddingClient, "_post_once", boom)
        monkeypatch.setattr("agentcore.embedding.client.asyncio.sleep", fake_sleep)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=2)
        with pytest.raises(RuntimeError):
            await client.embed_many(["x"])
        assert slept == [10.0, 20.0, 30.0, 40.0]


class TestDisconnectFamilyRetries:
    """M15：dc46b8f 声称"超时/断连→重试"，而原 except 漏掉 ProtocolError 家族。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.RemoteProtocolError("server disconnected"),
            httpx.ProxyError("proxy boom"),
            httpx.ReadError("read boom"),
            httpx.WriteError("write boom"),
            httpx.CloseError("closed"),
        ],
    )
    async def test_disconnect_family_is_retried(self, monkeypatch, exc):
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "0")
        n = {"c": 0}

        async def boom(self, client, batch):
            n["c"] += 1
            raise exc

        monkeypatch.setattr(EmbeddingClient, "_post_once", boom)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=2)
        with pytest.raises(RuntimeError):
            await client.embed_many(["x"])
        assert n["c"] == 6, f"{type(exc).__name__} 应被重试到耗尽"

    @pytest.mark.asyncio
    async def test_unsupported_protocol_gives_config_hint(self, monkeypatch):
        """base_url 写错（无 scheme）→ 立即失败并指出配置项，不重试。"""
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "0")
        n = {"c": 0}

        async def boom(self, client, batch):
            n["c"] += 1
            raise httpx.UnsupportedProtocol("Request URL is missing a protocol")

        monkeypatch.setattr(EmbeddingClient, "_post_once", boom)
        client = EmbeddingClient(base_url="example.com", api_key="k", dim=2)
        with pytest.raises(RuntimeError, match="EMBEDDING_BASE_URL"):
            await client.embed_many(["x"])
        assert n["c"] == 1, "配置错误不重试"


class TestRetryAfterAndBatchErrors:
    @pytest.mark.asyncio
    async def test_429_honours_retry_after_when_shorter(self, monkeypatch):
        """L12：429 带 Retry-After 时按它等待（且不超过原退避）。"""
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "100")
        slept: list[float] = []
        state = {"n": 0}

        async def flaky(self, client, batch):
            state["n"] += 1
            if state["n"] == 1:
                return _FakeResp(429, text="{}", headers={"retry-after": "3"})
            return _FakeResp(
                200, json_data={"data": [{"index": 0, "embedding": [1.0]}]}
            )

        async def fake_sleep(secs):
            slept.append(secs)

        monkeypatch.setattr(EmbeddingClient, "_post_once", flaky)
        monkeypatch.setattr("agentcore.embedding.client.asyncio.sleep", fake_sleep)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=1)
        await client.embed_many(["x"])
        assert slept == [3.0], "应听 Retry-After 而不是固定 100s"

    @pytest.mark.asyncio
    async def test_retry_after_never_exceeds_backoff(self, monkeypatch):
        """Retry-After 再大也被本次退避上限夹住（防被上游拖死）。"""
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "5")
        slept: list[float] = []
        state = {"n": 0}

        async def flaky(self, client, batch):
            state["n"] += 1
            if state["n"] == 1:
                return _FakeResp(429, text="{}", headers={"retry-after": "9999"})
            return _FakeResp(
                200, json_data={"data": [{"index": 0, "embedding": [1.0]}]}
            )

        async def fake_sleep(secs):
            slept.append(secs)

        monkeypatch.setattr(EmbeddingClient, "_post_once", flaky)
        monkeypatch.setattr("agentcore.embedding.client.asyncio.sleep", fake_sleep)
        client = EmbeddingClient(base_url="https://api.test", api_key="k", dim=1)
        await client.embed_many(["x"])
        assert slept == [5.0]

    @pytest.mark.asyncio
    async def test_413_points_at_batch_size(self, monkeypatch):
        """L12：413/422 是"请求过大"，指引必须是调小 EMBEDDING_BATCH。"""

        async def too_big(self, client, batch):
            return _FakeResp(413, text="payload too large")

        monkeypatch.setattr(EmbeddingClient, "_post_once", too_big)
        client = EmbeddingClient(
            base_url="https://api.test", api_key="k", dim=2, batch=64
        )
        with pytest.raises(RuntimeError, match="EMBEDDING_BATCH"):
            await client.embed_many(["x"])


class TestRetryEnvClamping:
    """L13：脏值/越界要告警并 clamp，不再静默接受。"""

    def test_dirty_value_warns_and_falls_back(self, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("EMBEDDING_RETRY_COUNT", "banana")
        with caplog.at_level(logging.WARNING):
            client = EmbeddingClient(base_url="https://x", api_key="k", dim=2)
        assert client.retry_count == 5
        assert "banana" in caplog.text

    def test_oversized_value_is_clamped(self, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("EMBEDDING_RETRY_COUNT", "100")
        with caplog.at_level(logging.WARNING):
            client = EmbeddingClient(base_url="https://x", api_key="k", dim=2)
        assert client.retry_count == 10, "上限 10：否则 100 次退避可放大到数小时"
        assert "100" in caplog.text

    def test_zero_delay_is_allowed(self, monkeypatch):
        """0 是合法值（不等待），_env_positive_float 会误拒，故单独用 nonneg 解析。"""
        monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "0")
        client = EmbeddingClient(base_url="https://x", api_key="k", dim=2)
        assert client.retry_delay == 0.0

    def test_interactive_defaults(self):
        client = EmbeddingClient(base_url="https://x", api_key="k", dim=2)
        assert client.interactive_retry_count == 1
        assert client.interactive_budget == 30.0
