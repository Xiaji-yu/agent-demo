"""agentcore/llm/client：配置解析兜底 + 主备切换通知。

评审 M6（REVIEW-46c85d1..6ec3f7c）：数值 env 空值曾令 _LLMCfg()（模块级单例，
import 即构造）抛 ValueError → bot 根本起不来。空值/脏值必须回落默认。

主备切换通知（LLM_FALLBACK_NOTIFY）：逐请求无状态的切换 + 边沿触发 + 按类型
冷却。AGENTS.md §5 的坑（哨兵用 0.0 吞掉首次告警）由
TestFallbackNotify.test_first_notify_survives_fresh_boot 钉住。
"""

import asyncio

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


# ==========================================================================
#  候选模型池（LLM_MODELS）与运行态切主（/model）
#
#  池化要守的不变量：
#  ① 解析**绝不抛**——_LLMCfg 是 import 期构造的模块级单例，一个坏条目不能让
#     bot 起不来（与数值 env 的兜底同纪律，评审 M6）
#  ② 活跃位（LLM_MODEL）不接受 key：它的值会进审计/回执/web 回包
#  ③ 降级链 = 活跃位 → 刚换下的旧主 → 池内其余 → LLM_FALLBACK_* 独立线
#  ④ model_status 与 chat 必须同源，否则 skill 会报告一个没在用的型号
# ==========================================================================


def _pool(monkeypatch, *, models=None, model=None, fallback=False):
    """按 env 造一个真实 _LLMCfg 并 patch 进模块，返回 (client, lc)。

    测的是解析+装配+客户端的整条路径，不是替身对象——替身测不出
    "坏条目让 bot 起不来"这类 import 期问题。
    """
    import agentcore.llm.client as lc

    if models is None:
        monkeypatch.delenv("LLM_MODELS", raising=False)
    else:
        monkeypatch.setenv("LLM_MODELS", models)
    if model is None:
        monkeypatch.delenv("LLM_MODEL", raising=False)
    else:
        monkeypatch.setenv("LLM_MODEL", model)
    for k in (
        "LLM_FALLBACK_BASE_URL",
        "LLM_FALLBACK_API_KEY",
        "LLM_FALLBACK_MODEL",
    ):
        monkeypatch.delenv(k, raising=False)
    if fallback:
        monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "http://fallback.invalid")
        monkeypatch.setenv("LLM_FALLBACK_API_KEY", "k-fallback")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "fallback-model")
    cfg = lc._LLMCfg()
    monkeypatch.setattr(lc, "_CFG", cfg)
    return lc.LLMClient(clock=lambda: 1000.0), lc


class _FakeHTTP:
    """最小 httpx 替身：按模型名决定成败，记录请求体。

    必须让请求走**真的** ``_post``——usage 上报就在它里面，patch 掉 _post 等于
    把要断言的那行代码一起绕开了。
    """

    def __init__(self, payload=None, fail_models=()):
        self.payload = payload or {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 7},
        }
        self.fail_models = set(fail_models)
        self.calls = []

    async def post(self, url, headers=None, json=None):
        self.calls.append((url, json))
        if json.get("model") in self.fail_models:
            raise httpx.ReadTimeout(f"{json['model']} down")

        class _Resp:
            status_code = 200

            @staticmethod
            def raise_for_status():
                pass

            @staticmethod
            def json():
                return self.payload

        return _Resp()


class TestModelSpecParse:
    def _parse(self, text, **kw):
        import agentcore.llm.client as lc

        return lc.parse_model_spec(text, **kw)

    def test_plain_model(self):
        import agentcore.llm.client as lc

        assert self._parse("step-3") == lc.ModelEntry("step-3")

    def test_model_with_base_url_and_key(self):
        e = self._parse("llama@http://127.0.0.1:11434/v1|ollama")
        assert e.model == "llama"
        assert e.base_url == "http://127.0.0.1:11434/v1"
        assert e.api_key == "ollama"

    def test_trailing_slash_stripped(self):
        assert self._parse("m@https://h/v1/").base_url == "https://h/v1"

    @pytest.mark.parametrize("bad", ["", "   ", "@https://h", "m@", "m@ftp://h"])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            self._parse(bad)

    @pytest.mark.parametrize("bad", ["m\nLLM_API_KEY=x", "m#c", 'm"', "m'", "m,n"])
    def test_rejects_line_injection_and_separator(self, bad):
        """**.env 是行格式**：换行/#/引号会改 dotenv 解析边界；逗号是条目分隔符。"""
        with pytest.raises(ValueError):
            self._parse(bad)

    def test_active_slot_rejects_key(self):
        """活跃位不接受 key——它的值会进审计/回执/web 回包。"""
        with pytest.raises(ValueError, match="不携带 key"):
            self._parse("m@https://h|sk-x", allow_key=False)

    def test_canonical_spec_never_contains_key(self):
        import agentcore.llm.client as lc

        e = self._parse("llama@http://127.0.0.1:11434/v1|ollama")
        assert lc.canonical_spec(e) == "llama@http://127.0.0.1:11434/v1"
        assert "ollama" not in lc.canonical_spec(e)

    def test_repr_has_no_key(self):
        e = self._parse("llama@http://h|sk-super-secret")
        assert "sk-super-secret" not in repr(e)

    def test_display_hides_main_endpoint_host(self):
        import agentcore.llm.client as lc

        e = lc.ModelEntry("m", "http://internal.host:1234")
        assert lc.entry_display(e, "http://internal.host:1234") == "m"
        assert lc.entry_display(e, "http://other") == "m@internal.host:1234"


class TestPoolConfig:
    def test_no_pool_is_single_model(self, monkeypatch):
        client, _ = _pool(monkeypatch, model="m1")
        assert client.pool_entries() == ()
        assert client.active_entry().model == "m1"
        assert client.model_status()["active"] == "m1"

    def test_empty_model_falls_back_to_pool_head(self, monkeypatch):
        client, _ = _pool(monkeypatch, models="a,b")
        assert client.active_entry().model == "a", "LLM_MODEL 缺省 = 池首"

    def test_active_matched_into_pool_inherits_key(self, monkeypatch):
        """对回池条目才能继承它登记的 key——否则多供应商池第二把门票失效。"""
        client, _ = _pool(
            monkeypatch, models="a@http://ha|ka,b@http://hb|kb", model="b@http://hb"
        )
        assert client.active_entry().model == "b"
        assert client.active_entry().api_key == "kb"

    def test_bad_pool_entry_dropped_not_raised(self, monkeypatch):
        """import 期解析：坏条目只告警丢弃，绝不让 bot 起不来。"""
        client, _ = _pool(monkeypatch, models="good,@bad,m@ftp://x,also-good")
        assert [e.model for e in client.pool_entries()] == ["good", "also-good"]

    def test_bad_active_falls_back_to_pool_head(self, monkeypatch):
        client, _ = _pool(monkeypatch, models="a,b", model="m|sk-x")
        assert client.active_entry().model == "a"

    def test_embedding_model_snapshotted_at_import(self, monkeypatch):
        """运行态切主不得改变 embedding 用的模型名。

        守的是一个**很可能的笨修改**：有人为了让"切主模型全家一起切"，把
        ``embeddings()`` 改成读 ``self.active_entry().model``——那会让语义检索
        静默退化（非 embedding 模型 → 向量全废）。旧实现读 ``_CFG.model`` 恰好
        也是 import 期的，所以这条是结构不变量，不是已发缺陷的回归。
        """
        monkeypatch.delenv("LLM_EMBEDDING_MODEL", raising=False)
        client, lc = _pool(monkeypatch, models="a,b", model="a")
        assert lc._CFG.embedding_model == "a"
        client.set_active(lc.ModelEntry("b"))
        fake = _FakeHTTP({"data": [{"embedding": [0.1]}]})
        client._client = fake
        asyncio.run(client.embeddings(["x"]))
        assert fake.calls, "embeddings 必须真的发请求"
        assert fake.calls[0][1]["model"] == "a", "运行态切主不得带走 embedding"

    def test_embedding_model_explicit_env_wins(self, monkeypatch):
        monkeypatch.setenv("LLM_EMBEDDING_MODEL", "emb-model")
        client, lc = _pool(monkeypatch, models="a,b", model="a")
        assert lc._CFG.embedding_model == "emb-model"
        client._client = _FakeHTTP({"data": [{"embedding": [0.1]}]})
        asyncio.run(client.embeddings(["x"]))
        assert client._client.calls[0][1]["model"] == "emb-model"


class TestCandidateChain:
    """降级链：活跃位 → 旧主 → 池内其余 → 独立备用线。"""

    def _names(self, client):
        return [e.model for e in client.chain_entries()]

    def test_no_pool_is_active_plus_legacy(self, monkeypatch):
        client, _ = _pool(monkeypatch, model="m1", fallback=True)
        assert self._names(client) == ["m1", "fallback-model"]

    def test_pool_order_after_active(self, monkeypatch):
        client, _ = _pool(monkeypatch, models="a,b,c", model="b")
        assert self._names(client) == ["b", "a", "c"]

    def test_previous_active_goes_first(self, monkeypatch):
        """旧主排最前：刚被换下的那个是"最可能还想用"的。"""
        client, _ = _pool(monkeypatch, models="a,b,c", model="a")
        assert self._names(client) == ["a", "b", "c"]
        client.set_active(lc_entry("c"))
        assert self._names(client) == ["c", "a", "b"]

    def test_previous_active_outside_pool_still_first(self, monkeypatch):
        """旧主不在池里也要留在降级链最前——它刚服务过，比池内更该先回退。"""
        client, _ = _pool(monkeypatch, models="b,c", model="a")
        assert self._names(client) == ["a", "b", "c"]
        client.set_active(lc_entry("c"))
        assert self._names(client) == ["c", "a", "b"], (
            "换到池内模型后，被换下的非池模型不能从降级链上消失"
        )

    def test_legacy_line_last(self, monkeypatch):
        client, _ = _pool(monkeypatch, models="a,b", model="a", fallback=True)
        assert self._names(client) == ["a", "b", "fallback-model"]

    def test_candidate_carries_entry_endpoint_and_key(self, monkeypatch):
        """多供应商池的第二把门票：候选配置必须带上条目自己的端点与 key。

        只断言模型名是不够的——_apply_entry 忘了套 base_url/api_key 时，请求会
        打到主端点、用主 key，表现为"模型名对却 401/404"。
        """
        monkeypatch.setenv("LLM_BASE_URL", "http://main.invalid")
        monkeypatch.setenv("LLM_API_KEY", "k-main")
        client, _ = _pool(
            monkeypatch,
            models="a@http://ha.invalid|ka,b@http://hb.invalid|kb",
            model="a",
            fallback=True,
        )
        cands = client._candidates()
        assert (cands[0].base_url, cands[0].api_key) == ("http://ha.invalid", "ka")
        assert (cands[1].base_url, cands[1].api_key) == ("http://hb.invalid", "kb")
        # 独立备用线自带端点与 key，不能被主 key 顶掉
        assert cands[2].model == "fallback-model"
        assert cands[2].api_key == "k-fallback"

    def test_main_endpoint_entry_inherits_main_key(self, monkeypatch):
        """池条目没写端点/key 时回落主端点主 key（否则单供应商池全废）。"""
        monkeypatch.setenv("LLM_BASE_URL", "http://main.invalid")
        monkeypatch.setenv("LLM_API_KEY", "k-main")
        client, _ = _pool(monkeypatch, models="a,b", model="a")
        cands = client._candidates()
        assert (cands[0].base_url, cands[0].api_key) == (
            "http://main.invalid",
            "k-main",
        )

    def test_duplicate_entries_deduped(self, monkeypatch):
        client, _ = _pool(monkeypatch, models="a,a,b", model="a")
        assert self._names(client) == ["a", "b"]

    def test_switching_to_same_entry_keeps_no_self_previous(self, monkeypatch):
        client, _ = _pool(monkeypatch, models="a,b", model="a")
        client.set_active(lc_entry("a"))
        assert self._names(client) == ["a", "b"]


def lc_entry(model, base_url="", api_key=""):
    import agentcore.llm.client as lc

    return lc.ModelEntry(model, base_url, api_key)


class TestRuntimeSwitch:
    @pytest.mark.asyncio
    async def test_chat_uses_overridden_model(self, monkeypatch):
        client, lc = _pool(monkeypatch, models="a,b", model="a")
        seen = _patch_post(lc, monkeypatch)
        client.set_active(lc_entry("b"))
        await client.chat(_MSG)
        assert [c.model for c in seen] == ["b"]

    @pytest.mark.asyncio
    async def test_usage_records_the_model_actually_used(self, monkeypatch):
        """预算按模型分账：降级到池内第二个时，usage 必须记它的名字。"""
        recorded = []
        client, lc = _pool(monkeypatch, models="a,b", model="a", fallback=False)
        # client.py 是 from-import：必须 patch 它自己命名空间里的名字，
        # patch agentcore.budget 对已导入的模块无效
        monkeypatch.setattr(
            lc, "record_chat_usage", lambda u, model=None: recorded.append(model)
        )
        client._client = _FakeHTTP(fail_models={"a"})
        await client.chat(_MSG)
        assert recorded == ["b"], recorded

    @pytest.mark.asyncio
    async def test_set_active_bare_name_resolves_into_pool(self, monkeypatch):
        """web 写面只写裸模型名：set_active 必须自己继承池条目的端点+key。"""
        monkeypatch.setenv("LLM_BASE_URL", "http://main.invalid")
        monkeypatch.setenv("LLM_API_KEY", "k-main")
        client, _ = _pool(
            monkeypatch,
            models="a@http://ha.invalid|ka,b@http://hb.invalid|kb",
            model="a",
        )
        client.set_active(lc_entry("b"))
        assert client.active_entry() == lc_entry("b", "http://hb.invalid", "kb"), (
            "裸名必须对回池条目，否则会拿主 key 打另一家供应商"
        )

    def test_set_active_explicit_endpoint_not_overridden(self, monkeypatch):
        client, _ = _pool(
            monkeypatch, models="b@http://hb.invalid|kb", model="b@http://hb.invalid"
        )
        client.set_active(lc_entry("b", "http://other.invalid"))
        assert client.active_entry().base_url == "http://other.invalid"
        assert client.active_entry().api_key == "", "显式条目沿用主 key"

    @pytest.mark.asyncio
    async def test_switch_clears_degraded_state(self, monkeypatch):
        """降级状态属于旧主：不清掉的话 model_status 会谎报线路。"""
        client, lc = _pool(monkeypatch, models="a,b", model="a")

        async def a_is_down(self, cfg, payload):
            if cfg.model == "a":
                raise httpx.ReadTimeout("a down")
            raise httpx.ConnectError("b down too")

        monkeypatch.setattr(lc.LLMClient, "_post", a_is_down)
        with pytest.raises(httpx.ConnectError):
            await client.chat(_MSG)
        client._using_fallback = True
        client.set_active(lc_entry("b"))
        assert client._using_fallback is False
        assert client.model_status()["line"] == "primary"

    @pytest.mark.asyncio
    async def test_reset_returns_to_env_default(self, monkeypatch):
        client, _ = _pool(monkeypatch, models="a,b", model="a")
        client.set_active(lc_entry("b"))
        assert client.active_entry().model == "b"
        target = client.reset_active()
        assert target.model == "a"
        assert client.active_entry().model == "a"
        assert client.model_status()["source"] == "env"

    def test_status_reports_runtime_source_and_pool(self, monkeypatch):
        client, _ = _pool(monkeypatch, models="a,b", model="a")
        st = client.model_status()
        assert st["source"] == "env" and st["pool"] == ["a", "b"]
        assert st["default"] == "a" and st["chain"] == ["a", "b"]
        client.set_active(lc_entry("b"))
        st2 = client.model_status()
        assert st2["source"] == "runtime"
        assert st2["active"] == "b" and st2["default"] == "a"
        assert st2["chain"] == ["b", "a"], "旧主要进降级链"

    def test_status_has_no_endpoint_or_key(self, monkeypatch):
        """返回值会经模型转述给群里的任何人：池/链也只能是模型名。"""
        monkeypatch.setenv("LLM_BASE_URL", "http://internal.host:1234")
        monkeypatch.setenv("LLM_API_KEY", "sk-super-secret")
        client, _ = _pool(
            monkeypatch,
            models="a@http://internal.host:1234|sk-super-secret,b",
            model="a",
        )
        blob = str(client.model_status())
        assert "sk-super-secret" not in blob
        assert "internal.host" not in blob


class TestProbe:
    @pytest.mark.asyncio
    async def test_probe_failure_raises_and_records_nothing(self, monkeypatch):
        """探测失败必须抛给调用方（由它决定不改状态），且不能冒充成功。"""
        client, lc = _pool(monkeypatch, models="a,b", model="a")
        calls = []

        async def boom(self, cfg, payload, client=None):
            calls.append(cfg.model)
            req = httpx.Request("POST", "http://x/chat/completions")
            raise httpx.HTTPStatusError(
                "404", request=req, response=httpx.Response(404)
            )

        monkeypatch.setattr(lc.LLMClient, "_post", boom)
        with pytest.raises(httpx.HTTPStatusError):
            await client.probe(lc_entry("typo-model"))
        assert calls == ["typo-model"]

    @pytest.mark.asyncio
    async def test_probe_uses_short_timeout_client(self, monkeypatch):
        """探测自带短超时客户端：不该把命令挂到共享池的 60s。"""
        client, lc = _pool(monkeypatch, models="a", model="a")
        seen = []

        async def fake_post(self, cfg, payload, client=None):
            seen.append(client)
            return {"choices": [{"message": {"content": "pong"}}]}

        monkeypatch.setattr(lc.LLMClient, "_post", fake_post)
        await client.probe(lc_entry("a"))
        assert seen[0] is not None and seen[0] is not client._client
        assert seen[0].timeout.connect == 10.0

    @pytest.mark.asyncio
    async def test_probe_payload_is_minimal(self, monkeypatch):
        client, lc = _pool(monkeypatch, models="a", model="a")
        payloads = []

        async def fake_post(self, cfg, payload, client=None):
            payloads.append(payload)
            return {"choices": [{"message": {"content": "pong"}}]}

        monkeypatch.setattr(lc.LLMClient, "_post", fake_post)
        await client.probe(lc_entry("a"))
        assert payloads[0]["model"] == "a"
        assert payloads[0]["max_tokens"] == 16


class TestProviderRouting:
    """多 provider 路由：协议分派、降级、禁用跳过、usage 记账。"""

    def _client_with(self, monkeypatch, providers, active):
        from agentcore.llm import providers as ps

        path = ps.agent_json_path()
        ps.save(ps.ProviderConfig(active=active, providers=providers), path)
        import agentcore.llm.client as lc

        return lc.LLMClient(clock=lambda: 1000.0)

    def _providers(self):
        from agentcore.llm.providers import Provider

        return [
            Provider("o", "OpenAI", "openai", "http://o", "m-openai", "k-o"),
            Provider("a", "Anthropic", "anthropic", "http://a", "m-anth", "k-a"),
            Provider(
                "x", "Disabled", "openai", "http://x", "m-x", "k-x", enabled=False
            ),
        ]

    @pytest.mark.asyncio
    async def test_active_provider_uses_its_protocol(self, monkeypatch):
        client = self._client_with(monkeypatch, self._providers(), "o")
        seen = []

        async def fake(self, url, headers, payload, client=None):
            seen.append((url, headers, payload))
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 3},
            }

        monkeypatch.setattr(client.__class__, "_post_raw", fake)
        data = await client.chat(_MSG)
        assert data["choices"][0]["message"]["content"] == "ok"
        assert seen[0][0] == "http://o/chat/completions"
        assert seen[0][1]["Authorization"] == "Bearer k-o"

    @pytest.mark.asyncio
    async def test_falls_back_to_anthropic_and_normalizes(self, monkeypatch):
        client = self._client_with(monkeypatch, self._providers(), "o")
        seen = []

        async def fake(self, url, headers, payload, client=None):
            seen.append((url, headers, payload))
            if url.endswith("/chat/completions"):
                raise httpx.ReadTimeout("openai down")
            return {
                "content": [{"type": "text", "text": "from claude"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }

        monkeypatch.setattr(client.__class__, "_post_raw", fake)
        data = await client.chat(_MSG)
        assert data["choices"][0]["message"]["content"] == "from claude"
        assert data["usage"]["total_tokens"] == 7
        assert seen[-1][0] == "http://a/v1/messages"
        assert seen[-1][1]["x-api-key"] == "k-a"

    @pytest.mark.asyncio
    async def test_disabled_provider_skipped(self, monkeypatch):
        client = self._client_with(monkeypatch, self._providers(), "x")
        # active 指向禁用项时，链从第一个 enabled 开始
        assert [p.id for p in client.provider_chain()] == ["o", "a"]

    @pytest.mark.asyncio
    async def test_usage_records_served_provider_model(self, monkeypatch):
        import agentcore.llm.client as lc

        client = self._client_with(monkeypatch, self._providers(), "o")
        recorded = []
        monkeypatch.setattr(
            lc, "record_chat_usage", lambda u, model=None: recorded.append(model)
        )

        async def fake(self, url, headers, payload, client=None):
            if url.endswith("/chat/completions"):
                raise httpx.ReadTimeout("down")
            return {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

        monkeypatch.setattr(client.__class__, "_post_raw", fake)
        await client.chat(_MSG)
        assert recorded == ["m-anth"]

    @pytest.mark.asyncio
    async def test_all_providers_disabled_raises(self, monkeypatch):
        """L15：全部停用时不能静默回落 env 旧路径。"""
        from agentcore.llm.providers import Provider

        client = self._client_with(
            monkeypatch,
            [
                Provider("a", "A", "openai", "http://a", "m-a", "k-a", enabled=False),
            ],
            "a",
        )
        with pytest.raises(RuntimeError, match="均已停用"):
            await client.chat(_MSG)

    def test_status_lists_providers_without_keys(self, monkeypatch):
        client = self._client_with(monkeypatch, self._providers(), "o")
        st = client.model_status()
        assert st["source"] == "providers" and st["active_id"] == "o"
        # M5：model_status 会进 current_model skill（群聊可问）——不带 provider 明细
        assert "providers" not in st
        providers = client.provider_status()["providers"]
        assert [p["id"] for p in providers] == ["o", "a", "x"]
        blob = str(providers)
        assert "k-o" not in blob and "k-a" not in blob
        # 管理员视图有完整 endpoint；public（skill/overview 来源）只有 host
        assert "endpoint" in providers[0]
        pub = client.provider_config().providers[0].public()
        assert "endpoint" not in pub and pub["endpoint_host"] == "o"

    @pytest.mark.asyncio
    async def test_probe_records_usage(self, monkeypatch):
        """L11：探测是花钱请求，usage 必须记账。"""
        client = self._client_with(monkeypatch, self._providers(), "o")
        recorded = []
        import agentcore.llm.client as lc

        monkeypatch.setattr(
            lc, "record_chat_usage", lambda u, model=None: recorded.append(model)
        )

        async def fake(self, url, headers, payload, client=None):
            return {
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"total_tokens": 2},
            }

        monkeypatch.setattr(client.__class__, "_post_raw", fake)
        await client.probe_provider(client.provider_config().providers[0], timeout=0)
        assert recorded == ["m-openai"]

    def test_set_active_provider_persists_previous(self, monkeypatch):
        client = self._client_with(monkeypatch, self._providers(), "o")
        client.set_active_provider("a")
        assert [p.id for p in client.provider_chain()] == ["a", "o"]
        assert client.provider_config().active == "a"
