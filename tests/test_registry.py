import pytest

from agentcore.skills.registry import SkillRegistry


class TestSkillRegistry:
    @pytest.fixture
    def reg(self):
        return SkillRegistry()

    @pytest.fixture
    def reg_with_checker(self):
        from agentcore.skills.permissions import PermissionChecker

        checker = PermissionChecker(superusers={"111"}, default_permission="private")
        return SkillRegistry(permission_checker=checker)

    def test_register_and_get_schema(self, reg):
        reg.register("calc", "计算器", {"type": "object"}, permission="public")(
            lambda expr="": expr
        )
        schemas = reg.get_schemas()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "calc"

    def test_permission_filter(self, reg_with_checker):
        reg_with_checker.register(
            "secret", "秘密技能", {"type": "object"}, permission="private"
        )(lambda: "secret")
        schemas = reg_with_checker.get_schemas("222", None)
        assert len(schemas) == 0

    def test_superuser_sees_all(self, reg_with_checker):
        reg_with_checker.register(
            "secret", "秘密技能", {"type": "object"}, permission="private"
        )(lambda: "secret")
        schemas = reg_with_checker.get_schemas("111", None)
        assert len(schemas) == 1

    @pytest.mark.asyncio
    async def test_execute_unknown(self, reg):
        result = await reg.execute("unknown")
        assert "unknown" in result

    @pytest.mark.asyncio
    async def test_execute_permission_denied(self, reg_with_checker):
        reg_with_checker.register(
            "secret", "秘密技能", {"type": "object"}, permission="private"
        )(lambda: "secret")
        result = await reg_with_checker.execute("secret", user_id="222", group_id=None)
        assert "permission denied" in result

    @pytest.mark.asyncio
    async def test_execute_success(self, reg):
        async def handler(x=1):
            return str(x + 1)

        reg.register("add", "加法", {"type": "object"}, permission="public")(handler)
        result = await reg.execute("add", user_id="222", group_id=None, x=2)
        assert result == "3"

    @pytest.mark.asyncio
    async def test_execute_error_sanitized(self, reg):
        async def handler():
            raise ValueError("internal secret detail")

        reg.register("boom", "爆炸", {"type": "object"}, permission="public")(handler)
        result = await reg.execute("boom", user_id="222", group_id=None)
        assert "internal secret detail" not in result
        assert "执行失败" in result

    def test_is_allowed(self, reg):
        reg.register("public_skill", "公开", {"type": "object"}, permission="public")(
            lambda: "ok"
        )
        assert reg.is_allowed("public_skill", "222", None) is True

    def test_install_and_uninstall(self, reg):
        from agentcore.skills.manifest import SkillManifest

        manifest = SkillManifest(
            name="tmp",
            description="tmp",
            type="prompt",
            prompt="prompt",
            parameters=[],
            permission="public",
        )
        reg.install(manifest, handler=lambda **kw: "ok")
        assert "tmp" in reg.skills
        assert reg.uninstall("tmp") is True
        assert "tmp" not in reg.skills
        assert reg.uninstall("tmp") is False


class TestSharedLLMClient:
    """P1-4/P1-6：prompt skill 复用共享连接池，不再每次 new 一个不释放。"""

    def test_singleton_reused(self):
        import importlib

        R = importlib.import_module("agentcore.skills.registry")

        R._shared_llm_client = None
        a = R.get_shared_llm_client()
        b = R.get_shared_llm_client()
        assert a is b

    @pytest.mark.asyncio
    async def test_close_resets(self, monkeypatch):
        import importlib

        R = importlib.import_module("agentcore.skills.registry")

        closed = []

        class FakeLLM:
            async def aclose(self):
                closed.append(True)

        R._shared_llm_client = FakeLLM()
        await R.close_shared_llm_client()
        assert closed == [True]
        assert R._shared_llm_client is None

    @pytest.mark.asyncio
    async def test_prompt_skill_uses_shared_client(self, monkeypatch):
        import importlib

        R = importlib.import_module("agentcore.skills.registry")
        from agentcore.skills.manifest import SkillManifest

        calls = []

        class FakeLLM:
            async def chat(self, messages, tools=None):
                calls.append(messages)
                return {"choices": [{"message": {"content": "译文"}}]}

        fake = FakeLLM()
        monkeypatch.setattr(R, "get_shared_llm_client", lambda: fake)
        manifest = SkillManifest(
            name="t",
            description="d",
            type="prompt",
            prompt="p",
            parameters=[],
            permission="public",
        )
        out = await R._run_prompt_skill(manifest, {"text": "hi"})
        assert out == "译文"
        assert len(calls) == 1


class TestInstallToolGuard:
    """tool 型 manifest 无 handler 时拒装（本轮审查 P1 回归）。

    旧实现把 tool 型静默包装成 prompt skill：/skill install search_web 会用
    空 prompt 桩覆盖带真 handler 的内置搜索，且落盘后每次重启反复覆盖。
    """

    @pytest.fixture
    def reg(self):
        return SkillRegistry()

    @staticmethod
    def _tool_manifest(name="search_web"):
        from agentcore.skills.manifest import SkillManifest

        return SkillManifest(
            name=name,
            description="联网搜索（内置工具）",
            type="tool",
            prompt="",
            parameters=[{"name": "query", "type": "string", "required": True}],
            permission="public",
        )

    def test_tool_without_handler_rejected(self, reg):
        with pytest.raises(ValueError, match="handler"):
            reg.install(self._tool_manifest())

    def test_tool_with_handler_still_installs(self, reg):
        async def handler(query=""):
            return "hit"

        reg.install(self._tool_manifest(), handler=handler)
        assert reg.skills["search_web"].handler is handler

    def test_prompt_manifest_still_wraps_prompt_handler(self, reg):
        from agentcore.skills.manifest import SkillManifest

        m = SkillManifest(
            name="translator",
            description="翻译",
            type="prompt",
            prompt="你是翻译。",
            parameters=[{"name": "text", "type": "string"}],
            permission="public",
        )
        reg.install(m)
        handler = reg.skills["translator"].handler
        assert handler.__name__ == "prompt_skill_translator"


class TestCatalogHygiene:
    """目录只收 prompt 型技能；内置工具不得出现在可安装目录（P1 同源）。"""

    def test_no_tool_type_entries(self):
        from agentcore.skills.catalog import CATALOG

        for name, manifest in CATALOG.items():
            assert manifest.type == "prompt", name

    def test_builtin_tools_not_in_catalog(self):
        from agentcore.skills.catalog import CATALOG

        # 内置工具由代码注册；出现在目录里会被空桩覆盖（历史缺陷）
        for builtin in ("search_web", "search_multi", "fetch_url", "calc", "now"):
            assert builtin not in CATALOG, builtin


class TestReadOnlyFlag:
    """只读标记（C3 并行工具的前提）：默认关闭、集中补标、fail-closed。"""

    @pytest.fixture
    def reg(self):
        return SkillRegistry()

    def test_default_not_read_only(self, reg):
        reg.register("t", "t", {"type": "object"})(lambda: "x")
        assert reg.is_read_only("t") is False

    def test_register_flag_and_mark_read_only(self, reg):
        reg.register("a", "a", {"type": "object"}, read_only=True)(lambda: "x")
        reg.register("b", "b", {"type": "object"})(lambda: "x")
        assert reg.is_read_only("a") is True
        assert reg.is_read_only("b") is False
        reg.mark_read_only("b")
        assert reg.is_read_only("b") is True

    def test_unknown_skill_fail_closed(self, reg):
        assert reg.is_read_only("nope") is False
        reg.mark_read_only("nope")  # 条件注册的技能：静默跳过不报错
