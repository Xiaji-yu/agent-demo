"""agentcore/llm/client：配置解析兜底 + 主备切换通知。

评审 M6（REVIEW-46c85d1..6ec3f7c）：数值 env 空值曾令 _LLMCfg()（模块级单例，
import 即构造）抛 ValueError → bot 根本起不来。空值/脏值必须回落默认。

主备切换通知（LLM_FALLBACK_NOTIFY）：逐请求无状态的切换 + 边沿触发 + 按类型
冷却。AGENTS.md §5 的坑（哨兵用 0.0 吞掉首次告警）由
TestFallbackNotify.test_first_notify_survives_fresh_boot 钉住。
"""

import httpx
import pytest


class TestLLMConfigFallback:
    def test_empty_temperature_falls_back_to_default(self, monkeypatch):
        import agentcore.llm.client as lc

        monkeypatch.setenv("LLM_TEMPERATURE", "")
        cfg = lc._LLMCfg()
        assert cfg.temperature == 0.7

    def test_empty_max_tokens_falls_back_to_default(self, monkeypatch):
        import agentcore.llm.client as lc

        monkeypatch.setenv("LLM_MAX_TOKENS", "")
        cfg = lc._LLMCfg()
        assert cfg.max_tokens == 1024

    def test_dirty_values_fall_back(self, monkeypatch):
        import agentcore.llm.client as lc

        monkeypatch.setenv("LLM_TEMPERATURE", "hot")
        monkeypatch.setenv("LLM_MAX_TOKENS", "lots")
        cfg = lc._LLMCfg()
        assert cfg.temperature == 0.7
        assert cfg.max_tokens == 1024

    def test_valid_values_still_used(self, monkeypatch):
        """守卫兜底没收紧过头：合法值照常生效。"""
        import agentcore.llm.client as lc

        monkeypatch.setenv("LLM_TEMPERATURE", "0.3")
        monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
        cfg = lc._LLMCfg()
        assert cfg.temperature == 0.3
        assert cfg.max_tokens == 2048


# ==========================================================================
# 主备模型切换通知（LLM_FALLBACK_NOTIFY）
#
# 切换是**逐请求无状态**的（每次先打主模型、失败才临时切备用），所以"当前在
# 用哪个"要客户端自己记，才能在边沿通知宿主；否则主模型挂一小时就是 N 条私聊。
# ==========================================================================


class _Cfg:
    """可编排主备成败的最小配置替身。"""

    def __init__(self, *, fallback: bool = False):
        self.base_url = "http://primary.invalid"
        self.api_key = "k-primary"
        self.model = "primary-model"
        self.temperature = 0.7
        self.max_tokens = 1024
        if fallback:
            self.fallback_base_url = "http://fallback.invalid"
            self.fallback_api_key = "k-fallback"
            self.fallback_model = "fallback-model"
        else:
            self.fallback_base_url = ""
            self.fallback_api_key = ""
            self.fallback_model = ""


class _OkPost:
    """_post 替身：恒成功（模拟主模型恢复）。"""

    async def __call__(self, cfg, payload):
        return {"choices": [{"message": {"content": "ok"}}]}


def _patch_post(lc, monkeypatch, *, primary_ok=True, fallback_ok=True):
    """按 cfg.model 分派成败；返回调用记录供断言。"""
    seen: list = []

    async def fake_post(self, cfg, payload):
        seen.append(cfg)
        if cfg.model == "primary-model" and not primary_ok:
            raise httpx.ReadTimeout("primary down")
        if cfg.model == "fallback-model" and not fallback_ok:
            raise httpx.ConnectError("fallback down")
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(lc.LLMClient, "_post", fake_post)
    return seen


def _client(monkeypatch, *, fallback: bool, events: list, now=None):
    """造一个 LLMClient：通知收进 events，时钟可编排。"""
    import agentcore.llm.client as lc

    monkeypatch.setattr(lc, "_CFG", _Cfg(fallback=fallback))
    clock = (lambda: now[0]) if now is not None else (lambda: 1000.0)
    client = lc.LLMClient(clock=clock)

    async def on_fallback(ev):
        events.append(ev)

    client.on_fallback = on_fallback
    return client, lc


_MSG = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _zero_llm_backoff(monkeypatch):
    """主模型重试退避归零：本文件测的是分支逻辑，不是真实等待。"""
    import agentcore.llm.client as lc

    monkeypatch.setattr(lc, "_RETRY_BACKOFF_SECONDS", 0)


class TestFallbackNotify:
    """主备切换/恢复的边沿通知 + 按类型冷却。"""

    @pytest.mark.asyncio
    async def test_first_switch_notifies_once(self, monkeypatch):
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events)
        _patch_post(lc, monkeypatch, primary_ok=False)

        data = await client.chat(_MSG)
        assert data["choices"], "切到备用后必须照常返回结果"
        assert len(events) == 1, f"首次切换必须通知一次：{events}"
        ev = events[0]
        assert ev["kind"] == "switched"
        assert ev["primary_model"] == "primary-model"
        assert ev["fallback_model"] == "fallback-model"
        assert ev["error"] == "ReadTimeout", "要带错误类型（超时的 str(e) 是空串）"

    @pytest.mark.asyncio
    async def test_sustained_outage_does_not_spam_per_request(self, monkeypatch):
        """持续故障：冷却内不逐请求刷屏（5 个请求只 1 条）。"""
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events)
        _patch_post(lc, monkeypatch, primary_ok=False)
        for _ in range(5):
            await client.chat(_MSG)
        assert len(events) == 1, f"冷却内只应一条通知，实际 {len(events)}"

    @pytest.mark.asyncio
    async def test_sustained_outage_reminds_after_cooldown(self, monkeypatch):
        """持续故障跨过冷却后要**再提醒一次**——主人得知道"还在降级"，
        而不是只在故障第一分钟知道过一次（与推送文案承诺一致）。"""
        now = [1000.0]
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events, now=now)
        _patch_post(lc, monkeypatch, primary_ok=False)
        await client.chat(_MSG)
        now[0] += 1801  # 跨过默认冷却
        await client.chat(_MSG)
        assert [e["kind"] for e in events] == ["switched", "switched"], (
            f"跨冷却应再提醒一次：{events}"
        )

    @pytest.mark.asyncio
    async def test_recovery_notifies_once(self, monkeypatch):
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events)
        _patch_post(lc, monkeypatch, primary_ok=False)
        await client.chat(_MSG)
        assert [e["kind"] for e in events] == ["switched"]

        monkeypatch.setattr(lc.LLMClient, "_post", _OkPost())
        await client.chat(_MSG)
        await client.chat(_MSG)
        assert [e["kind"] for e in events] == ["switched", "recovered"], (
            f"恢复应只报一次：{events}"
        )

    @pytest.mark.asyncio
    async def test_recovery_not_blocked_by_switch_cooldown(self, monkeypatch):
        """恢复通知不能被「切换」的冷却压掉——两类事件各自记冷却。"""
        now = [1000.0]
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events, now=now)
        _patch_post(lc, monkeypatch, primary_ok=False)
        await client.chat(_MSG)
        now[0] += 5  # 远小于默认 1800s 冷却
        monkeypatch.setattr(lc.LLMClient, "_post", _OkPost())
        await client.chat(_MSG)
        assert [e["kind"] for e in events] == ["switched", "recovered"]

    @pytest.mark.asyncio
    async def test_flapping_is_bounded_per_kind(self, monkeypatch):
        """主备抖动时同一类型最多每冷却期一条（防刷屏）。"""
        now = [1000.0]
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events, now=now)
        for _ in range(3):
            _patch_post(lc, monkeypatch, primary_ok=False)
            await client.chat(_MSG)
            now[0] += 1
            monkeypatch.setattr(lc.LLMClient, "_post", _OkPost())
            await client.chat(_MSG)
            now[0] += 1
        kinds = [e["kind"] for e in events]
        assert kinds.count("switched") == 1, f"抖动时切换通知应被限流：{kinds}"
        assert kinds.count("recovered") == 1, f"抖动时恢复通知应被限流：{kinds}"

    @pytest.mark.asyncio
    async def test_switch_renotifies_after_cooldown(self, monkeypatch):
        """跨过冷却后再次降级（新 episode）要能再报。"""
        now = [1000.0]
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events, now=now)
        _patch_post(lc, monkeypatch, primary_ok=False)
        await client.chat(_MSG)
        now[0] += 3600  # 跨过默认 1800s 冷却
        monkeypatch.setattr(lc.LLMClient, "_post", _OkPost())
        await client.chat(_MSG)  # 恢复
        _patch_post(lc, monkeypatch, primary_ok=False)
        await client.chat(_MSG)  # 再次降级
        assert [e["kind"] for e in events] == ["switched", "recovered", "switched"]

    @pytest.mark.asyncio
    async def test_no_fallback_configured_raises_without_notify(self, monkeypatch):
        """没配备份：原样抛，且**不能**伪报成切换。"""
        events: list = []
        client, lc = _client(monkeypatch, fallback=False, events=events)
        _patch_post(lc, monkeypatch, primary_ok=False)
        with pytest.raises(httpx.ReadTimeout):
            await client.chat(_MSG)
        assert events == []

    @pytest.mark.asyncio
    async def test_fallback_also_failing_reraises_without_claiming_switch(
        self, monkeypatch
    ):
        """备用也挂：照旧抛，且不标记"已切换"（否则恢复通知会是假信号）。"""
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events)
        _patch_post(lc, monkeypatch, primary_ok=False, fallback_ok=False)
        with pytest.raises(httpx.ConnectError):
            await client.chat(_MSG)
        assert events == []
        assert client._using_fallback is False

        monkeypatch.setattr(lc.LLMClient, "_post", _OkPost())
        await client.chat(_MSG)
        assert events == [], "从未宣称切换，就不该有恢复通知"

    @pytest.mark.asyncio
    async def test_callback_failure_does_not_break_chat(self, monkeypatch):
        """推送失败（QQ 不可达等）绝不能把对话也带崩。"""
        import agentcore.llm.client as lc

        monkeypatch.setattr(lc, "_CFG", _Cfg(fallback=True))
        client = lc.LLMClient(clock=lambda: 1000.0)

        async def boom(ev):
            raise RuntimeError("qq down")

        client.on_fallback = boom
        _patch_post(lc, monkeypatch, primary_ok=False)
        data = await client.chat(_MSG)
        assert data["choices"], "通知炸了也要把备用结果返回给用户"

    @pytest.mark.asyncio
    async def test_notify_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("LLM_FALLBACK_NOTIFY", "0")
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events)
        _patch_post(lc, monkeypatch, primary_ok=False)
        await client.chat(_MSG)
        assert events == [], "=0 时不得通知"

    @pytest.mark.asyncio
    async def test_no_callback_injected_is_fine(self, monkeypatch):
        """宿主没注入回调（脚本/测试直接用客户端）时不炸。"""
        import agentcore.llm.client as lc

        monkeypatch.setattr(lc, "_CFG", _Cfg(fallback=True))
        client = lc.LLMClient(clock=lambda: 1000.0)
        _patch_post(lc, monkeypatch, primary_ok=False)
        data = await client.chat(_MSG)
        assert data["choices"]

    @pytest.mark.asyncio
    async def test_first_notify_survives_fresh_boot(self, monkeypatch):
        """AGENTS.md §5 的坑：哨兵不能用 0.0。

        ``time.monotonic()`` 是**开机秒数**——刚重启的机器上它很小。若用 0.0
        当"从未通知过"，``now - 0.0 < cooldown`` 恒成立，首次切换会被吞掉
        （embedding 侧同款 bug 在 CI 新开机 runner 上实测复现过）。
        """
        now = [0.5]  # 模拟刚启动 0.5 秒
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events, now=now)
        _patch_post(lc, monkeypatch, primary_ok=False)
        await client.chat(_MSG)
        assert len(events) == 1, "开机瞬间的首次切换也必须通知"

    @pytest.mark.asyncio
    async def test_event_contains_no_secrets(self, monkeypatch):
        """事件要跨层进私聊文本：不得夹带 api_key / base_url。"""
        import agentcore.llm.client as lc

        monkeypatch.setattr(lc, "_CFG", _Cfg(fallback=True))
        client = lc.LLMClient(clock=lambda: 1000.0)
        seen: list = []

        async def capture(ev):
            seen.append(ev)

        client.on_fallback = capture
        await client._notify_fallback("switched", error="ReadTimeout")
        assert seen, "回调应被触发"
        blob = str(seen[0])
        assert "k-primary" not in blob and "k-fallback" not in blob
        assert "primary.invalid" not in blob and "fallback.invalid" not in blob


class TestModelStatus:
    """model_status()：current_model skill 的数据源。

    注意：它读的是模块级 ``_CFG``——与 ``chat()`` 挑主模型用**同一份**配置
    （有意耦合：两者必须一致，否则 skill 会报告一个实际没在用的型号）。
    所以用例像 TestFallbackNotify 一样 patch ``lc._CFG``，而不是 setenv
    （env 只在 import 时进 _CFG，setenv 对已导入的模块无效）。
    """

    def _client(self, monkeypatch, cfg):
        import agentcore.llm.client as lc

        monkeypatch.setattr(lc, "_CFG", cfg)
        return lc.LLMClient(clock=lambda: 1000.0)

    def test_primary_line_by_default(self, monkeypatch):
        client = self._client(monkeypatch, _Cfg(fallback=True))
        st = client.model_status()
        assert st["active"] == "primary-model"
        assert st["line"] == "primary"
        assert st["fallback"] == "fallback-model"

    def test_fallback_line_when_degraded(self, monkeypatch):
        client = self._client(monkeypatch, _Cfg(fallback=True))
        client._using_fallback = True  # 模拟已切换到备用
        st = client.model_status()
        assert st["active"] == "fallback-model"
        assert st["line"] == "fallback"
        assert st["primary"] == "primary-model"

    def test_no_fallback_configured_stays_primary(self, monkeypatch):
        client = self._client(monkeypatch, _Cfg(fallback=False))
        client._using_fallback = True  # 没配备份时不可能真切换，但不该误报
        st = client.model_status()
        assert st["line"] == "primary", "没有备用模型时报 fallback 是假信息"
        assert st["fallback"] == ""

    def test_status_has_no_secrets(self, monkeypatch):
        """返回值会经模型转述给群里的任何人：不得含 key / base_url。"""
        cfg = _Cfg(fallback=True)
        cfg.api_key = "sk-super-secret"
        cfg.base_url = "http://internal.host:1234"
        client = self._client(monkeypatch, cfg)
        blob = str(client.model_status())
        assert "sk-super-secret" not in blob
        assert "internal.host" not in blob
        assert "k-primary" not in blob and "k-fallback" not in blob


class TestPrimaryRetry:
    """主模型瞬时故障重试（评审 C1 回归）。

    旧实现一次抖动（超时/429/5xx）立即降级到备用或直接失败。现在瞬时错误
    先在主模型上重试 1 次；确定性错误（401/400）不重试直接走降级。
    退避已由模块级 fixture 归零。
    """

    @pytest.mark.asyncio
    async def test_transient_failure_retried_then_recovered(self, monkeypatch):
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events)
        calls = {"n": 0}

        async def flaky_post(self, cfg, payload):
            calls["n"] += 1
            if cfg.model == "primary-model" and calls["n"] == 1:
                raise httpx.ReadTimeout("blip")
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(lc.LLMClient, "_post", flaky_post)
        data = await client.chat(_MSG)
        assert data["choices"], "重试成功必须照常返回"
        assert calls["n"] == 2, "瞬时故障应在主模型上重试一次"
        assert events == [], "主模型自愈不算降级，不应通知"
        assert client.model_status()["line"] == "primary"

    @pytest.mark.asyncio
    async def test_non_retryable_goes_straight_to_fallback(self, monkeypatch):
        """401 这类确定性错误重试只会白等：立即降级。"""
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events)
        calls = {"n": 0}

        async def auth_fail(self, cfg, payload):
            calls["n"] += 1
            if cfg.model == "primary-model":
                req = httpx.Request("POST", "http://primary.invalid/chat/completions")
                raise httpx.HTTPStatusError(
                    "401 unauthorized", request=req, response=httpx.Response(401)
                )
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(lc.LLMClient, "_post", auth_fail)
        data = await client.chat(_MSG)
        assert data["choices"]
        assert calls["n"] == 2, "一次主模型 + 一次备用，不得在 401 上重试"
        assert [e["kind"] for e in events] == ["switched"]

    @pytest.mark.asyncio
    async def test_retry_exhausted_falls_back(self, monkeypatch):
        events: list = []
        client, lc = _client(monkeypatch, fallback=True, events=events)
        seen = _patch_post(lc, monkeypatch, primary_ok=False)
        data = await client.chat(_MSG)
        assert data["choices"]
        models = [c.model for c in seen]
        assert models == ["primary-model", "primary-model", "fallback-model"], models
        assert [e["kind"] for e in events] == ["switched"]

    @pytest.mark.asyncio
    async def test_no_fallback_still_raises_after_retry(self, monkeypatch):
        """没配备份：重试耗尽后原样抛（不伪装成功）。"""
        client, lc = _client(monkeypatch, fallback=False, events=[])
        calls = {"n": 0}

        async def always_down(self, cfg, payload):
            calls["n"] += 1
            raise httpx.ConnectError("down")

        monkeypatch.setattr(lc.LLMClient, "_post", always_down)
        with pytest.raises(httpx.ConnectError):
            await client.chat(_MSG)
        assert calls["n"] == 2
