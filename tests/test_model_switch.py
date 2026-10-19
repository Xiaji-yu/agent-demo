"""运行期切模型的编排层：``/model`` 与 web 写面共用的那道链。

顺序是这条链的全部难点，用例按顺序逐段钉：
  校验形态 → 池成员 → **探测** → **落盘** → 运行态切换
探测失败与落盘失败都必须"原地不动"——否则管理员会得到一个改了却没生效、
或改了却坏掉的机器人。
"""

from pathlib import Path

import httpx
import pytest

from plugins.qq_agent_adapter import config_write as cw
from plugins.qq_agent_adapter import model_switch


@pytest.fixture(autouse=True)
def _clean_diagnostics():
    """这些用例会写审计事件；不清会给 test_diagnostics 留下跨文件污染。"""
    from agentcore.diagnostics import clear

    clear()
    yield
    clear()


def lc_entry(model, base_url="", api_key=""):
    import agentcore.llm.client as lc

    return lc.ModelEntry(model, base_url, api_key)


def _client(monkeypatch, *, models="a,b", model="a"):
    """造一个带候选池的真实 LLMClient，并让编排层拿得到它。"""
    import agentcore.llm.client as lc

    if models is None:
        monkeypatch.delenv("LLM_MODELS", raising=False)
    else:
        monkeypatch.setenv("LLM_MODELS", models)
    monkeypatch.setenv("LLM_MODEL", model)
    for k in (
        "LLM_FALLBACK_BASE_URL",
        "LLM_FALLBACK_API_KEY",
        "LLM_FALLBACK_MODEL",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = lc._LLMCfg()
    monkeypatch.setattr(lc, "_CFG", cfg)
    client = lc.LLMClient(clock=lambda: 1000.0)
    # 不能按字符串路径 patch：agentcore.skills 包把单例导出为同名属性 registry，
    # monkeypatch 的 derive_importpath 会解析到那个实例上
    import importlib

    reg_mod = importlib.import_module("agentcore.skills.registry")
    monkeypatch.setattr(reg_mod, "get_shared_llm_client", lambda: client)
    monkeypatch.setattr(model_switch, "_client", lambda: client)
    return client


def _stub_probe(monkeypatch, detail=""):
    seen: list = []

    async def fake(entry):
        seen.append(entry)
        return detail

    monkeypatch.setattr(model_switch, "probe_failure_detail", fake)
    return seen


@pytest.fixture(autouse=True)
def _isolated_env_file(tmp_path, monkeypatch):
    """绝不碰真实 .env：编排层真的会写盘，测试必须被关进 tmp。

    2026-10-19 教训：第一个版本漏了这层，某条用例走了真实 write_env_key，
    把仓库 .env 的 LLM_MODEL 改成了测试值「b」（已从备份还原）。凡是会落盘的
    编排层测试，AGENT_ENV_FILE 一律显式指到 tmp。
    """
    envf = tmp_path / ".env"
    envf.write_text("LLM_MODEL=step-3.7-flash\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_ENV_FILE", str(envf))
    monkeypatch.setenv("AGENT_WEB_BACKUP_DIR", str(tmp_path / "bk"))
    return envf


def _stub_persist(monkeypatch, error=None):
    """桩掉落盘与运行态同步，返回调用记录。"""
    writes: list = []
    applied: list = []

    def fake_write(spec, file_text):
        writes.append(file_text)
        if error is not None:
            raise error
        return cw.WriteResult(
            spec.key, "old-value", file_text, Path("/tmp/x.bak"), "replace"
        )

    monkeypatch.setattr(cw, "write_env_key", fake_write)
    monkeypatch.setattr(
        cw,
        "apply_runtime",
        lambda spec, normalized, file_text: applied.append(file_text),
    )
    return writes, applied


class TestSwitchGuards:
    @pytest.mark.asyncio
    async def test_rejects_key_in_spec(self, monkeypatch):
        """活跃位不接受 key：值会进审计/回执/web 回包。"""
        _client(monkeypatch)
        probes = _stub_probe(monkeypatch)
        result = await model_switch.switch_active_model(
            "b@http://h|sk-leak", by_uid="9001"
        )
        assert result.ok is False
        assert "不携带 key" in result.message
        assert probes == [], "形态都不对，不该去探测"

    @pytest.mark.asyncio
    async def test_model_outside_pool_rejected(self, monkeypatch):
        """池是运营方维护的候选集：切一个从没进过降级链的模型要先改 .env。"""
        _client(monkeypatch, models="a,b")
        probes = _stub_probe(monkeypatch)
        result = await model_switch.switch_active_model("zzz", by_uid="9001")
        assert result.ok is False
        assert "不在候选池" in result.message and "a、b" in result.message
        assert probes == []

    @pytest.mark.asyncio
    async def test_empty_pool_allows_arbitrary_model(self, monkeypatch):
        """没配 LLM_MODELS 的单模型部署：不拿池限制人。"""
        client = _client(monkeypatch, models=None)
        assert client.pool_entries() == ()
        _stub_probe(monkeypatch)
        writes, applied = _stub_persist(monkeypatch)
        result = await model_switch.switch_active_model("any-model", by_uid="9001")
        assert result.ok is True, result.message
        assert writes == ["any-model"] and applied == ["any-model"]
        assert client.active_entry().model == "any-model"

    @pytest.mark.asyncio
    async def test_bare_name_inherits_pool_entry_endpoint(self, monkeypatch):
        """池里同名片条可能带端点+key：按名字切要继承那一份，不然第二把门票失效。"""
        client = _client(monkeypatch, models="a,b@http://hb|kb")
        _stub_probe(monkeypatch)
        writes, _applied = _stub_persist(monkeypatch)
        result = await model_switch.switch_active_model("b", by_uid="9001")
        assert result.ok is True, result.message
        assert client.active_entry().api_key == "kb"
        assert writes == ["b@http://hb"], (
            "落盘写规范文本：端点显式落上（池重排后仍能解析），key 仍留在池条目里"
        )
        assert "kb" not in writes[0]


class TestSwitchOrdering:
    @pytest.mark.asyncio
    async def test_probe_failure_changes_nothing(self, monkeypatch):
        """探测不过：不落盘、不切运行态——否则机器人直接哑掉。"""
        client = _client(monkeypatch)
        probes = _stub_probe(monkeypatch, detail="探测失败：HTTP 404 model not found")
        writes, applied = _stub_persist(monkeypatch)
        result = await model_switch.switch_active_model("b", by_uid="9001")
        assert result.ok is False and "404" in result.message
        assert writes == [] and applied == []
        assert client.active_entry().model == "a", "运行态不得动"
        assert len(probes) == 1

    @pytest.mark.asyncio
    async def test_persist_failure_keeps_runtime(self, monkeypatch):
        """落盘失败：运行态也不切——否则重启后模型会变，进程内却不一致。"""
        client = _client(monkeypatch)
        _stub_probe(monkeypatch)
        writes, applied = _stub_persist(
            monkeypatch, error=cw.EnvWriteError("verify_failed_rolled_back")
        )
        result = await model_switch.switch_active_model("b", by_uid="9001")
        assert result.ok is False
        assert "已自动还原" in result.message
        assert writes == ["b"] and applied == []
        assert client.active_entry().model == "a"

    @pytest.mark.asyncio
    async def test_happy_path_persists_then_applies(self, monkeypatch):
        client = _client(monkeypatch)
        _stub_probe(monkeypatch)
        writes, applied = _stub_persist(monkeypatch)
        result = await model_switch.switch_active_model("b", by_uid="9001")
        assert result.ok is True, result.message
        assert writes == ["b"] and applied == ["b"]
        assert client.active_entry().model == "b"
        assert "已写入 .env" in result.message and "探测通过" in result.message
        # 降级链要把刚换下的旧主排最前
        assert [e.model for e in client.chain_entries()] == ["b", "a"]

    @pytest.mark.asyncio
    async def test_real_apply_runtime_keeps_old_model_in_chain(
        self, monkeypatch, tmp_path
    ):
        """只桩落盘、让运行态同步真跑：链上必须仍留着刚换下的旧主。

        2026-10-19 实测缺陷：``_persist`` 里的 ``apply_runtime`` 已经同步过一次
        活跃位，``switch_active_model`` 返回前又 ``set_active`` 一次——旧实现按
        "与当前相同就清空 _previous" 处理，第二次调用把旧主记录抹了，降级链退化成
        池序。只桩 apply_runtime 的用例抓不到它（同步根本没跑）。
        """
        client = _client(monkeypatch, models="b@http://hb.invalid|kb,c", model="a")
        _stub_probe(monkeypatch)
        monkeypatch.setattr(
            cw,
            "write_env_key",
            lambda spec, file_text: cw.WriteResult(
                spec.key, "a", file_text, tmp_path / "x.bak", "replace"
            ),
        )
        result = await model_switch.switch_active_model("b", by_uid="9001")
        assert result.ok is True, result.message
        assert client.active_entry().model == "b"
        # web 路径只写裸名：端点/key 必须从池条目继承下来
        assert client.active_entry().base_url == "http://hb.invalid"
        assert client.active_entry().api_key == "kb"
        assert [e.model for e in client.chain_entries()] == ["b", "a", "c"], (
            "旧主必须留在降级链最前（刚服务过的模型最该先回退）"
        )

    @pytest.mark.asyncio
    async def test_audit_records_who(self, monkeypatch):
        from agentcore.diagnostics import clear as _clear
        from agentcore.diagnostics import recent as _recent

        _client(monkeypatch)
        _stub_probe(monkeypatch)
        _stub_persist(monkeypatch)
        _clear()
        try:
            await model_switch.switch_active_model("b", by_uid="9001")
            events = [e for e in _recent(5) if e["kind"] == "model_switch"]
            assert events and events[0]["by"] == "9001"
            assert events[0]["old"] == "a" and events[0]["new"] == "b"
        finally:
            _clear()


class TestReset:
    @pytest.mark.asyncio
    async def test_reset_returns_to_env_default(self, monkeypatch):
        client = _client(monkeypatch)
        _stub_probe(monkeypatch)
        writes, _applied = _stub_persist(monkeypatch)
        await model_switch.switch_active_model("b", by_uid="9001")
        assert client.active_entry().model == "b"
        result = await model_switch.reset_active_model(by_uid="9001")
        assert result.ok is True, result.message
        assert client.active_entry().model == "a", "reset 回 .env 装配值，不是池首"
        assert writes == ["b", "a"]

    @pytest.mark.asyncio
    async def test_reset_when_already_default_is_noop(self, monkeypatch):
        _client(monkeypatch)
        _stub_probe(monkeypatch)
        writes, _applied = _stub_persist(monkeypatch)
        result = await model_switch.reset_active_model(by_uid="9001")
        assert result.ok is True
        assert "未做改动" in result.message
        assert writes == [], "已经是默认位还去写 .env 是多余 IO"

    @pytest.mark.asyncio
    async def test_reset_probe_failure_restores_previous(self, monkeypatch):
        """默认位也探测不过：先把运行态改回去，不留半成品状态。"""
        client = _client(monkeypatch)
        _stub_probe(monkeypatch)
        writes, _applied = _stub_persist(monkeypatch)
        await model_switch.switch_active_model("b", by_uid="9001")
        assert writes == ["b"]
        _stub_probe(monkeypatch, detail="探测失败：HTTP 401")
        result = await model_switch.reset_active_model(by_uid="9001")
        assert result.ok is False
        assert client.active_entry().model == "b", "运行态必须被改回去"
        assert writes == ["b"], "默认位探测失败不得再落盘"


class TestProbeFailureDetail:
    """``probe_failure_detail`` 自己：把异常翻成管理员能动手的一句话。

    上面各类用例都桩掉了它，所以这里必须真身覆盖——否则"探测失败被当成成功"
    这种改动全绿。
    """

    def _patch_probe(self, monkeypatch, exc):
        import agentcore.llm.client as lc

        seen: list = []

        async def fake(self, entry, timeout=None):
            seen.append(entry)
            if exc is not None:
                raise exc
            return None

        monkeypatch.setattr(lc.LLMClient, "probe", fake)
        return seen

    @pytest.mark.asyncio
    async def test_http_status_surfaces_code_and_body(self, monkeypatch):
        """404/400 = 模型名或端点不对，401/403 = key 无权限：都要原样说清。"""
        _client(monkeypatch)
        self._patch_probe(
            monkeypatch,
            httpx.HTTPStatusError(
                "404",
                request=httpx.Request("POST", "http://h/chat/completions"),
                response=httpx.Response(404, text="model not found"),
            ),
        )
        detail = await model_switch.probe_failure_detail(lc_entry("typo"))
        assert "HTTP 404" in detail and "model not found" in detail

    @pytest.mark.asyncio
    async def test_transport_error_gets_actionable_hint(self, monkeypatch):
        _client(monkeypatch)
        self._patch_probe(monkeypatch, httpx.ConnectError("refused"))
        detail = await model_switch.probe_failure_detail(lc_entry("m"))
        assert "ConnectError" in detail and "base_url" in detail

    @pytest.mark.asyncio
    async def test_success_returns_empty(self, monkeypatch):
        _client(monkeypatch)
        self._patch_probe(monkeypatch, None)
        assert await model_switch.probe_failure_detail(lc_entry("m")) == ""


class TestDescribeStatus:
    def test_contains_pool_chain_and_usage(self, monkeypatch):
        _client(monkeypatch, models="a,b")
        text = model_switch.describe_status()
        assert "当前生效：a" in text
        assert "候选池" in text and "1. a" in text and "2. b" in text
        assert "降级链：a → b" in text
        assert "/model <名字> 切换" in text
        assert "sk-" not in text

    def test_marks_runtime_override(self, monkeypatch):
        client = _client(monkeypatch, models="a,b")
        client.set_active(client.pool_entries()[1])
        text = model_switch.describe_status()
        assert "当前生效：b" in text
        assert "默认位（.env 装配值）：a" in text
        assert "降级链：b → a" in text

    def test_no_pool_says_so(self, monkeypatch):
        _client(monkeypatch, models=None)
        assert "未配置 LLM_MODELS" in model_switch.describe_status()


class TestProviderSwitch:
    """/model 在 provider 模式下的切换 / 回执 / reset。"""

    def _client(self, monkeypatch):
        import agentcore.llm.client as lc
        from agentcore.llm import providers as ps

        ps.save(
            ps.ProviderConfig(
                active="a",
                providers=[
                    ps.Provider("a", "A", "openai", "http://a", "m-a", "k-a"),
                    ps.Provider("b", "B", "anthropic", "http://b", "m-b", "k-b"),
                ],
            )
        )
        client = lc.LLMClient(clock=lambda: 1000.0)
        monkeypatch.setattr(model_switch, "_client", lambda: client)
        return client

    def _probe(self, monkeypatch, detail=""):
        async def fake(provider):
            return detail

        monkeypatch.setattr(model_switch, "provider_probe_detail", fake)

    @pytest.mark.asyncio
    async def test_switch_reports_previous_provider_model(self, monkeypatch):
        self._client(monkeypatch)
        self._probe(monkeypatch)
        result = await model_switch.switch_active_model("b", by_uid="9001")
        assert result.ok is True, result.message
        assert "m-a → m-b" in result.message, (
            "回执必须记当前 provider 的 model，不能拿 legacy _CFG 的型号顶替"
        )
        assert "已写入 provider 配置" in result.message

    @pytest.mark.asyncio
    async def test_switch_by_model_name(self, monkeypatch):
        self._client(monkeypatch)
        self._probe(monkeypatch)
        result = await model_switch.switch_active_model("m-b", by_uid="9001")
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_switch_by_unique_prefix(self, monkeypatch):
        self._client(monkeypatch)
        self._probe(monkeypatch)
        result = await model_switch.switch_active_model("m-b", by_uid="9001")
        assert result.ok is True
        result = await model_switch.switch_active_model("a", by_uid="9001")
        assert result.ok is True, result.message

    @pytest.mark.asyncio
    async def test_switch_probe_failure_changes_nothing(self, monkeypatch):
        client = self._client(monkeypatch)
        self._probe(monkeypatch, detail="探测失败：HTTP 404")
        result = await model_switch.switch_active_model("b", by_uid="9001")
        assert result.ok is False and "404" in result.message
        assert client.provider_status()["active_id"] == "a"

    @pytest.mark.asyncio
    async def test_disabled_provider_rejected(self, monkeypatch):
        import agentcore.llm.client as lc
        from agentcore.llm import providers as ps

        ps.save(
            ps.ProviderConfig(
                active="a",
                providers=[
                    ps.Provider("a", "A", "openai", "http://a", "m-a", "k-a"),
                    ps.Provider(
                        "x", "X", "openai", "http://x", "m-x", "", enabled=False
                    ),
                ],
            )
        )
        client = lc.LLMClient(clock=lambda: 1000.0)
        monkeypatch.setattr(model_switch, "_client", lambda: client)
        self._probe(monkeypatch)
        result = await model_switch.switch_active_model("x", by_uid="9001")
        assert result.ok is False and "停用" in result.message

    @pytest.mark.asyncio
    async def test_reset_goes_to_first_enabled(self, monkeypatch):
        client = self._client(monkeypatch)
        self._probe(monkeypatch)
        await model_switch.switch_active_model("b", by_uid="9001")
        result = await model_switch.reset_active_model(by_uid="9001")
        assert result.ok is True
        assert client.provider_status()["active_id"] == "a"
        assert "已恢复默认 provider：a" in result.message

    def test_describe_status_lists_providers(self, monkeypatch):
        self._client(monkeypatch)
        text = model_switch.describe_status()
        assert "当前 provider：a" in text
        assert "b: m-b · anthropic" in text
        assert "k-a" not in text and "k-b" not in text
