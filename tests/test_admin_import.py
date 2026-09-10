"""H4-③：admin.py / matcher.py 的 import 冒烟测试。

历史教训：be1fb07 的裸 lambda rule 在 nonebot Rule 构造期抛 ValueError，
使整个 admin 插件 import 崩溃并存活 3 个 commit——因为没有任何测试 import 它。
本文件保证两个插件模块在 nonebot 初始化后可安全加载；另覆盖适配层的
配置解析（L11）与变更类命令权限（L25）。
"""
import pytest
from nonebot.exception import FinishedException


@pytest.fixture(scope="module")
def nb_driver():
    import nonebot

    # 显式传配置并禁用 .env 读取：环境/文件里的裸数字 SUPERUSERS 会让
    # pydantic 校验失败（bot.py 运行时有归一化，测试里直接显式给值）
    nonebot.init(_env_file=None, superusers={"10000"})
    from nonebot import get_driver

    return get_driver()


@pytest.mark.usefixtures("nb_driver")
class TestPluginImport:
    def test_admin_imports(self):
        import importlib as _il

        admin = _il.import_module("plugins.qq_agent_adapter.admin")

        for attr in (
            "handle_reset", "handle_help", "handle_status", "handle_skills",
            "handle_catalog", "handle_install", "handle_uninstall", "handle_info",
            "handle_persona", "handle_confirm_delete",
        ):
            assert hasattr(admin, attr), attr

    def test_matcher_imports(self):
        import importlib as _il

        matcher = _il.import_module("plugins.qq_agent_adapter.matcher")

        assert matcher.chat_matcher is not None
        from plugins.qq_agent_adapter.pipeline import build_payload

        assert callable(build_payload)

    def test_admin_rule_is_annotated_function_not_lambda(self):
        # 回归锁定：nonebot Rule 构造期要求依赖注入可识别的参数注解
        import importlib as _il
        import inspect

        admin = _il.import_module("plugins.qq_agent_adapter.admin")

        assert inspect.isfunction(admin._confirm_delete_rule)
        sig = inspect.signature(admin._confirm_delete_rule)
        (param,) = sig.parameters.values()
        assert param.annotation is not inspect.Parameter.empty


@pytest.mark.usefixtures("nb_driver")
class TestConfirmDeleteRule:
    def _event(self, text, user_id="10000"):
        class _Ev:
            def get_message(self):
                return text

            def get_user_id(self):
                return user_id

        return _Ev()

    def test_matches_when_allowed(self, monkeypatch):
        import importlib as _il

        admin = _il.import_module("plugins.qq_agent_adapter.admin")

        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        assert admin._confirm_delete_rule(self._event("确认删除 ABC123"))
        assert not admin._confirm_delete_rule(self._event("确认删除短"))
        assert not admin._confirm_delete_rule(self._event("hello"))

    def test_non_allowed_user_not_matched_and_not_blocked(self, monkeypatch):
        # L10：未授权聊天不响应也不拦截（消息继续下传）
        import importlib as _il

        admin = _il.import_module("plugins.qq_agent_adapter.admin")

        monkeypatch.setattr(admin, "is_allowed", lambda ev: False)
        assert not admin._confirm_delete_rule(self._event("确认删除 ABC123"))


@pytest.mark.usefixtures("nb_driver")
class TestStatusLines:
    """P2-7：/status 反映真实运行状态，而非硬编码文案。"""

    def test_reflects_memory_backend_and_model(self, monkeypatch):
        import importlib as _il

        admin = _il.import_module("plugins.qq_agent_adapter.admin")

        class FakeMemory:
            pass

        class FakeSkills:
            def __init__(self):
                self.skills = {"calc": None, "search": None}

        class FakeEngine:
            def __init__(self):
                self.skills = FakeSkills()

        class FakeDriver:
            _agent_memory = FakeMemory()
            _agent_engine = FakeEngine()
            _agent_persona_manager = None

        monkeypatch.setattr(admin, "_get_driver", lambda: FakeDriver())
        monkeypatch.setenv("LLM_MODEL", "test-model-x")
        lines = admin._build_status_lines()
        joined = "\n".join(lines)
        assert "FakeMemory" in joined
        assert "test-model-x" in joined
        assert "2 个" in joined

    def test_uninitialized_is_honest(self, monkeypatch):
        import importlib as _il

        admin = _il.import_module("plugins.qq_agent_adapter.admin")

        class FakeDriver:
            _agent_memory = None
            _agent_engine = None
            _agent_persona_manager = None

        monkeypatch.setattr(admin, "_get_driver", lambda: FakeDriver())
        lines = admin._build_status_lines()
        assert "未初始化" in "\n".join(lines)


class TestReminderTickParsing:
    """L11：AGENT_REMINDER_TICK 解析容错（抽成纯函数便于隔离测试）。"""

    @staticmethod
    def _tick(monkeypatch, value):
        import plugins.qq_agent_adapter as adapter

        if value is None:
            monkeypatch.delenv("AGENT_REMINDER_TICK", raising=False)
        else:
            monkeypatch.setenv("AGENT_REMINDER_TICK", value)
        return adapter._reminder_tick_seconds()

    def test_unset_defaults_to_30(self, monkeypatch):
        assert self._tick(monkeypatch, None) == 30

    def test_invalid_falls_back_to_30(self, monkeypatch):
        assert self._tick(monkeypatch, "abc") == 30
        assert self._tick(monkeypatch, "10s") == 30

    def test_zero_clamped_to_5(self, monkeypatch):
        assert self._tick(monkeypatch, "0") == 5

    def test_negative_clamped_to_5(self, monkeypatch):
        assert self._tick(monkeypatch, "-3") == 5

    def test_valid_value_kept(self, monkeypatch):
        assert self._tick(monkeypatch, "10") == 10


@pytest.mark.usefixtures("nb_driver")
class TestChangeCommandRequiresSuperuser:
    """L25：/reset、/skill install、/skill uninstall 与 /kb 变更类命令同一标准。

    处理器直接以函数调用：matcher.finish 被替换为记录文案后抛 FinishedException
    的桩；_get_driver 被替换为断言炸弹，验证非 superuser 根本走不到业务逻辑。
    """

    @staticmethod
    def _event(user_id="20002", text="/reset"):
        class _Ev:
            def get_message(self):
                return text

            def get_user_id(self):
                return user_id

        return _Ev()

    @staticmethod
    def _stub_finish(monkeypatch, matcher, calls):
        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            raise FinishedException()

        monkeypatch.setattr(matcher, "finish", fake_finish)

    @staticmethod
    def _admin():
        import importlib as _il

        return _il.import_module("plugins.qq_agent_adapter.admin")

    @pytest.mark.asyncio
    async def test_reset_rejected_for_non_superuser(self, monkeypatch):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.reset, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: False)

        def _boom():
            raise AssertionError("非 superuser 不应触达会话重置逻辑")

        monkeypatch.setattr(admin, "_get_driver", _boom)
        with pytest.raises(FinishedException):
            await admin.handle_reset(self._event())
        assert calls == ["只有管理员能重置会话。"]

    @pytest.mark.asyncio
    async def test_reset_allowed_for_superuser(self, monkeypatch):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.reset, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        class _Driver:
            _agent_memory = None  # 走到「无可清记忆」分支并正常结束

        monkeypatch.setattr(admin, "_get_driver", lambda: _Driver())
        with pytest.raises(FinishedException):
            await admin.handle_reset(self._event())
        assert calls and "会话已重置" in calls[0]

    @pytest.mark.asyncio
    async def test_install_rejected_for_non_superuser(self, monkeypatch):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.install_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: False)
        monkeypatch.setattr(admin, "CATALOG", {"boom": object()})

        def _boom_installer(*a, **k):
            raise AssertionError("非 superuser 不应触达安装逻辑")

        monkeypatch.setattr(admin, "_get_installer", _boom_installer)
        with pytest.raises(FinishedException):
            await admin.handle_install(self._event(text="/skill install boom"))
        assert calls == ["只有管理员能安装技能。"]

    @pytest.mark.asyncio
    async def test_uninstall_rejected_for_non_superuser(self, monkeypatch):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.uninstall_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: False)

        def _boom_installer(*a, **k):
            raise AssertionError("非 superuser 不应触达卸载逻辑")

        monkeypatch.setattr(admin, "_get_installer", _boom_installer)
        with pytest.raises(FinishedException):
            await admin.handle_uninstall(self._event(text="/skill uninstall calc"))
        assert calls == ["只有管理员能卸载技能。"]
