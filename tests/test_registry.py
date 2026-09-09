import pytest

from agentcore.skills.registry import SkillRegistry, registry


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

        reg.register("add", "加法", {"type": "object"}, permission="public")(
            handler
        )
        result = await reg.execute("add", user_id="222", group_id=None, x=2)
        assert result == "3"

    @pytest.mark.asyncio
    async def test_execute_error_sanitized(self, reg):
        async def handler():
            raise ValueError("internal secret detail")

        reg.register("boom", "爆炸", {"type": "object"}, permission="public")(
            handler
        )
        result = await reg.execute("boom", user_id="222", group_id=None)
        assert "internal secret detail" not in result
        assert "执行失败" in result

    def test_is_allowed(self, reg):
        reg.register(
            "public_skill", "公开", {"type": "object"}, permission="public"
        )(lambda: "ok")
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
