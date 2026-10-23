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
            "handle_reset",
            "handle_help",
            "handle_status",
            "handle_skills",
            "handle_catalog",
            "handle_install",
            "handle_uninstall",
            "handle_info",
            "handle_persona",
            "handle_confirm_delete",
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

        class FakeLLM:
            @staticmethod
            def model_status():
                return {"active": "test-model-x", "line": "primary"}

        import sys

        monkeypatch.setattr(
            sys.modules["agentcore.skills.registry"],
            "get_shared_llm_client",
            lambda: FakeLLM(),
        )
        lines = admin._build_status_lines()
        joined = "\n".join(lines)
        assert "FakeMemory" in joined
        assert "test-model-x" in joined
        assert "2 个" in joined

    def test_model_line_falls_back_to_env(self, monkeypatch):
        """客户端取不到时回落 env——不因为取不到就把模型行弄丢。"""
        import importlib as _il

        admin = _il.import_module("plugins.qq_agent_adapter.admin")

        class FakeDriver:
            _agent_memory = None
            _agent_engine = None
            _agent_persona_manager = None

        monkeypatch.setattr(admin, "_get_driver", lambda: FakeDriver())
        monkeypatch.setenv("LLM_MODEL", "env-model-y")
        import sys

        def boom():
            raise RuntimeError("no client")

        monkeypatch.setattr(
            sys.modules["agentcore.skills.registry"], "get_shared_llm_client", boom
        )
        assert "env-model-y" in "\n".join(admin._build_status_lines())

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


@pytest.mark.usefixtures("nb_driver")
class TestHelpAndSkillsDisclosure:
    """REVIEW-de09478..workdir M3/M7：帮助与技能列表的披露面。

    - M7：handle_help 曾是唯一没有 is_allowed 门的命令——未授权群/被拉黑
      用户也能拿到帮助（含 Pillow 渲染）与管理指令面描述。
    - M3：/skills 对非 superuser 全量列举隐藏技能名（含 ❌），把 registry 层
      「unknown 同文案、无探测口」的 P0 收口原样奉还；/skill info 无 superuser
      门，向普通用户回显 manifest 的 permission 串。
    """

    @staticmethod
    def _admin():
        import importlib as _il

        return _il.import_module("plugins.qq_agent_adapter.admin")

    @staticmethod
    def _event(user_id="20002", text="/aihelp"):
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

    @pytest.mark.asyncio
    async def test_help_rejected_when_not_allowed(self, monkeypatch):
        """M7：is_allowed 为假时必须走 deny 出口，不再渲染/回文案。"""
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.help_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: False)
        monkeypatch.delenv("SUPERUSERS", raising=False)
        monkeypatch.delenv("BLOCKED_USERS", raising=False)
        # is_blocked 内会读 NoneBot driver 的 superusers；测试里没有 driver，
        # acl 会回退 workspace.utils.load_superusers（env 已删 → 空集）

        def _boom_render(*a, **k):
            raise AssertionError("未授权用户不应触发帮助渲染")

        monkeypatch.setattr(
            "plugins.qq_agent_adapter.help_render.render_help_image", _boom_render
        )
        with pytest.raises(FinishedException):
            await admin.handle_help(self._event())
        assert calls == ["无权限"], calls

    @pytest.mark.asyncio
    async def test_skills_list_hides_unauthorized_names_for_non_superuser(
        self, monkeypatch
    ):
        """M3：非 superuser 只看得见自己可用的名字，隐藏技能名不得泄露。"""
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.skills_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: False)

        class _Reg:
            skills = {"calc": None, "log_tail": None, "run_shell": None}

            def is_allowed(self, name, uid, gid):
                return name == "calc"

        monkeypatch.setattr(admin, "_live_registry", lambda: _Reg())
        with pytest.raises(FinishedException):
            await admin.handle_skills(self._event(text="/skills"))
        listed = "\n".join(calls)
        assert "calc" in listed, "公开的技能仍应列出"
        assert "log_tail" not in listed and "run_shell" not in listed, (
            "隐藏技能名不得对普通用户枚举"
        )
        assert "✅" not in listed and "❌" not in listed, "非超管不展示可否认证"

    @pytest.mark.asyncio
    async def test_skills_superuser_still_sees_full_markers(self, monkeypatch):
        """守卫：superuser 的 ✅/❌ 全量视图不受 M3 影响。"""
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.skills_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        class _Reg:
            skills = {"calc": None, "log_tail": None}

            def is_allowed(self, name, uid, gid):
                return name == "calc"

        monkeypatch.setattr(admin, "_live_registry", lambda: _Reg())
        with pytest.raises(FinishedException):
            await admin.handle_skills(self._event(text="/skills"))
        listed = "\n".join(calls)
        assert "calc: ✅" in listed and "log_tail: ❌" in listed

    @pytest.mark.asyncio
    async def test_skill_info_rejected_for_non_superuser(self, monkeypatch):
        """M3：/skill info 回显权限串，只对 superuser 开放。"""
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.info_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: False)

        def _boom_installer(*a, **k):
            raise AssertionError("非 superuser 不应触达 manifest 读取")

        monkeypatch.setattr(admin, "_get_installer", _boom_installer)
        with pytest.raises(FinishedException):
            await admin.handle_info(self._event(text="/skill info log_tail"))
        assert calls == ["只有管理员能查看技能详情。"]


@pytest.mark.usefixtures("nb_driver")
class TestKbDisabledGuardsDeleteM3:
    """REVIEW-f6dffcc..08006e7.md 的 M3：AGENT_KB_ENABLED=0 时 /kb forget 也必须被挡住。

    此前守卫集合只有 add|file|samples，而 delete_source 又不受门控——关闭知识库后
    仍可删光全库。
    """

    @staticmethod
    def _event(text="/kb forget 1", user_id="10000"):
        class _Ev:
            def get_message(self):
                return text

            def get_user_id(self):
                return user_id

        return _Ev()

    @staticmethod
    def _admin():
        import importlib as _il

        return _il.import_module("plugins.qq_agent_adapter.admin")

    @pytest.mark.asyncio
    async def test_forget_rejected_when_kb_disabled(self, monkeypatch):
        admin = self._admin()
        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            raise FinishedException()

        monkeypatch.setattr(admin.kb_cmd, "finish", fake_finish)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        class _DisabledKB:
            enabled = False

            async def delete_source(self, sid):
                raise AssertionError("关闭态下不应触达删除逻辑")

        monkeypatch.setattr(admin, "_get_kb", lambda: _DisabledKB())
        with pytest.raises(FinishedException):
            await admin.handle_kb(self._event())
        assert calls and "已关闭" in calls[0]


@pytest.mark.usefixtures("nb_driver")
class TestKbSearchMissHint:
    """拼错的子命令走「未知子命令 → 搜索」兜底时，回复必须指出可能的正确命令。

    这是**接线级**回归：只测 `_kb_search_miss_message()` 本身不够——把
    `handle_kb` 里的调用换回硬编码字符串时，纯函数单测照样绿。
    """

    @staticmethod
    def _event(text, user_id="10000"):
        class _Ev:
            # L7：OneBot 的 self_id 是 int（NoneBot bots 字典的 key 是 str），
            # 替身必须保住这个类型，否则 /kb samples 通知的 str() 修复测不出来
            self_id = 10000

            def get_message(self):
                return text

            def get_user_id(self):
                return user_id

        return _Ev()

    @staticmethod
    def _admin():
        import importlib as _il

        return _il.import_module("plugins.qq_agent_adapter.admin")

    def _patch(self, monkeypatch, admin, calls):
        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            raise FinishedException()

        monkeypatch.setattr(admin.kb_cmd, "finish", fake_finish)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)

        class _KB:
            enabled = True

            async def retrieve(self, query):
                return []  # 零命中

        monkeypatch.setattr(admin, "_get_kb", lambda: _KB())

    @pytest.mark.asyncio
    async def test_typo_subcommand_gets_suggestion(self, monkeypatch):
        admin = self._admin()
        calls = []
        self._patch(monkeypatch, admin, calls)

        with pytest.raises(FinishedException):
            await admin.handle_kb(self._event("/kb samoles"))

        assert calls, "必须有回复"
        assert "没有检索到相关公共知识。" in calls[0]
        assert "/kb samples" in calls[0], "拼错子命令必须提示正确命令"

    @pytest.mark.asyncio
    async def test_real_query_miss_has_no_suggestion(self, monkeypatch):
        admin = self._admin()
        calls = []
        self._patch(monkeypatch, admin, calls)

        with pytest.raises(FinishedException):
            await admin.handle_kb(self._event("/kb 白名单 校验"))

        assert calls == ["没有检索到相关公共知识。"]

    @pytest.mark.asyncio
    async def test_known_subcommand_still_parses(self, monkeypatch):
        """放宽分隔符后 `/kb/samples` 必须走 samples 分支，而不是退化成搜索。"""
        admin = self._admin()
        calls = []
        seen = []
        self._patch(monkeypatch, admin, calls)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        async def fake_start(kb, samples_dir, notify, *, confirm=False):
            seen.append(confirm)
            return "已在后台开始导入 0 个新文档"

        monkeypatch.setattr(admin, "_start_samples_job", fake_start)

        with pytest.raises(FinishedException):
            await admin.handle_kb(self._event("/kb/samples"))

        assert calls == ["已在后台开始导入 0 个新文档"]
        assert seen == [False], "不带 confirm 时不得当作已确认"

    @pytest.mark.asyncio
    async def test_samples_confirm_passes_through(self, monkeypatch):
        """`/kb samples confirm` 必须把 confirm=True 传到启动函数（越过体积预检）。"""
        admin = self._admin()
        calls = []
        seen = []
        self._patch(monkeypatch, admin, calls)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        async def fake_start(kb, samples_dir, notify, *, confirm=False):
            seen.append(confirm)
            return "已启动"

        monkeypatch.setattr(admin, "_start_samples_job", fake_start)

        with pytest.raises(FinishedException):
            await admin.handle_kb(self._event("/kb samples confirm"))

        assert calls == ["已启动"]
        assert seen == [True]

    @pytest.mark.asyncio
    async def test_samples_notify_uses_str_self_id(self, monkeypatch):
        """L7（REVIEW-6ec3f7c..a36ea1d）：通知必须用 str(self_id) 取 bot。

        OneBot 事件的 self_id 是 **int**，而 NoneBot 的 bots 字典以 **str** 为 key
        —— e1aa9ec 修的就是"int 索引恒 KeyError、通知全部静默失败"。此前无任何
        用例触达该闭包，把 str() 去掉不会有测试失败。
        """
        import nonebot

        admin = self._admin()
        calls = []
        self._patch(monkeypatch, admin, calls)

        captured = {}

        async def fake_start(kb, samples_dir, notify, *, confirm=False):
            captured["notify"] = notify
            return "已启动"

        monkeypatch.setattr(admin, "_start_samples_job", fake_start)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        with pytest.raises(FinishedException):
            await admin.handle_kb(self._event("/kb samples"))

        # 直接检查闭包实际传给 get_bot 的键类型
        seen_keys = []

        class _FakeBot:
            async def send_private_msg(self, *, user_id, message):
                seen_keys.append(("sent", user_id))

        def fake_get_bot(key=None):
            seen_keys.append(("key", key))
            return _FakeBot()

        monkeypatch.setattr(nonebot, "get_bot", fake_get_bot)
        await captured["notify"]("done")

        keys = [k for kind, k in seen_keys if kind == "key"]
        assert keys and isinstance(keys[0], str), (
            f"get_bot 必须收到 str（int 索引恒 KeyError）：{keys!r}"
        )


@pytest.mark.usefixtures("nb_driver")
class TestExcBrief:
    """异常摘要必须带类型名：`httpx.ReadTimeout` 等的 ``str()`` 是空串。"""

    def _admin(self):
        import importlib as _il

        return _il.import_module("plugins.qq_agent_adapter.admin")

    def test_bare_timeout_keeps_type_name(self):
        admin = self._admin()
        brief = admin._exc_brief(TimeoutError())
        assert brief.startswith("TimeoutError"), brief
        assert brief != "", "空 str 的异常不能退化成空摘要"

    def test_message_is_kept(self):
        admin = self._admin()
        assert admin._exc_brief(RuntimeError("embedding 炸了")) == (
            "RuntimeError: embedding 炸了"
        )


# ==========================================================================
# L3（REVIEW-6ec3f7c..a36ea1d）：superuser QQ 号必须 ASCII 数字
#
# 旧实现用 uid.isdigit()，全角 "１２３" 也通过且 int() 得 123 → 含用户内容的
# 告警/成长提议会被发到**另一个** QQ 号（AGENTS.md §5 点名的坑）。
# ==========================================================================


class TestSuperuserIdParsing:
    def test_full_width_digits_are_skipped(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg
        from agentcore.workspace import utils as ws_utils

        monkeypatch.setattr(ws_utils, "load_superusers", lambda: {"１２３", "456"})
        assert pkg._superuser_ids() == [456]

    def test_plain_digits_sorted(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg
        from agentcore.workspace import utils as ws_utils

        monkeypatch.setattr(ws_utils, "load_superusers", lambda: {"9", "1", "5"})
        assert pkg._superuser_ids() == [1, 5, 9]

    def test_non_numeric_skipped(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg
        from agentcore.workspace import utils as ws_utils

        monkeypatch.setattr(ws_utils, "load_superusers", lambda: {"abc", "789"})
        assert pkg._superuser_ids() == [789]

    def test_full_width_would_reach_wrong_qq(self):
        """守住根因：全角串在旧判据下确实会被 int() 成另一个号。"""
        assert "１２３".isdigit() is True
        assert int("１２３") == 123
        assert not ("１２３".isascii() and "１２３".isdigit())

    def test_notify_sends_only_to_ascii_ids(self, monkeypatch):
        import asyncio

        import plugins.qq_agent_adapter as pkg
        from agentcore.workspace import utils as ws_utils

        monkeypatch.setattr(ws_utils, "load_superusers", lambda: {"１２３", "456"})
        sent = []

        class FakeBot:
            async def send_private_msg(self, *, user_id, message):
                sent.append(user_id)

        asyncio.run(pkg._notify_superusers(FakeBot(), "hi"))
        assert sent == [456]


class TestEmbeddingHintRouting:
    """M16：告警指引必须按异常类型分流，不能一律甩"Ollama 未启动"。"""

    def test_not_found_points_at_model_name(self):
        import plugins.qq_agent_adapter as pkg

        hint = pkg._embedding_hint_for(
            RuntimeError("embeddings API 404: model not found")
        )
        assert "EMBEDDING_MODEL" in hint

    def test_unsupported_protocol_points_at_base_url(self):
        import httpx

        import plugins.qq_agent_adapter as pkg

        hint = pkg._embedding_hint_for(httpx.UnsupportedProtocol("missing protocol"))
        assert "EMBEDDING_BASE_URL" in hint

    def test_auth_error_points_at_api_key(self):
        import plugins.qq_agent_adapter as pkg

        hint = pkg._embedding_hint_for(RuntimeError("embeddings API 401: unauthorized"))
        assert "EMBEDDING_API_KEY" in hint

    def test_timeout_falls_back_to_generic_hint(self):
        import httpx

        import plugins.qq_agent_adapter as pkg

        hint = pkg._embedding_hint_for(httpx.ReadTimeout(""))
        assert "EMBEDDING_TIMEOUT" in hint


class TestProbeTimeoutBudget:
    """M2：启动期探测必须有墙钟上限（旧实现可把 on_startup 挂 ~15 分钟）。"""

    def test_default_is_ten_seconds(self):
        import plugins.qq_agent_adapter as pkg

        assert pkg._probe_timeout_seconds() == 10.0

    def test_dirty_value_falls_back(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("EMBEDDING_PROBE_TIMEOUT", "abc")
        assert pkg._probe_timeout_seconds() == 10.0

    def test_non_positive_falls_back(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("EMBEDDING_PROBE_TIMEOUT", "0")
        assert pkg._probe_timeout_seconds() == 10.0

    def test_override_is_used(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("EMBEDDING_PROBE_TIMEOUT", "3")
        assert pkg._probe_timeout_seconds() == 3.0


# ==========================================================================
# M5（REVIEW-6ec3f7c..a36ea1d）：「确认成长」的管理员门
#
# commit 140ed63a 声称「管理员确认」，旧实现却只过 is_allowed —— 对群消息而言
# 那只判断群号，白名单群里任意成员都能兑换私聊发给 SUPERUSERS 的确认码。
# ==========================================================================


@pytest.mark.usefixtures("nb_driver")
class TestGrowthConfirmRequiresSuperuser:
    def _event(self, user_id: int):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        segs = [{"type": "text", "data": {"text": "确认成长 DEADBEEF"}}]
        return GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 1,
                "post_type": "message",
                "sub_type": "group",
                "user_id": user_id,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": segs,
                "original_message": segs,
                "raw_message": "确认成长 DEADBEEF",
                "font": 0,
                "sender": {"user_id": user_id, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )

    def test_group_member_without_superuser_is_rejected(self, nb_driver, monkeypatch):
        import importlib

        admin = importlib.import_module("plugins.qq_agent_adapter.admin")
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)  # ACL 放过群成员
        monkeypatch.setattr(admin, "is_superuser", lambda uid: uid == "9001")
        assert admin._growth_confirm_rule(self._event(12345)) is False

    def test_superuser_is_accepted(self, nb_driver, monkeypatch):
        import importlib

        admin = importlib.import_module("plugins.qq_agent_adapter.admin")
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: uid == "9001")
        assert admin._growth_confirm_rule(self._event(9001)) is True

    def test_self_message_still_filtered(self, nb_driver, monkeypatch):
        import importlib

        admin = importlib.import_module("plugins.qq_agent_adapter.admin")
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        assert admin._growth_confirm_rule(self._event(1)) is False  # self_id == 1


class TestGrowthIntervalSemantics:
    """L5：0 必须表示**关闭**（旧实现 max(1,...) 把 0 反转成"每轮都触发"）。"""

    def test_default_is_30(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.delenv("AGENT_PERSONA_GROWTH_INTERVAL", raising=False)
        assert pkg._growth_interval_env() == 30

    def test_zero_disables(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("AGENT_PERSONA_GROWTH_INTERVAL", "0")
        assert pkg._growth_interval_env() == 0, "0 是关闭语义，不得被改成 1"

    def test_negative_disables(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("AGENT_PERSONA_GROWTH_INTERVAL", "-5")
        assert pkg._growth_interval_env() <= 0

    def test_dirty_value_warns_and_defaults(self, monkeypatch, caplog):
        import logging

        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("AGENT_PERSONA_GROWTH_INTERVAL", "abc")
        with caplog.at_level(logging.WARNING):
            assert pkg._growth_interval_env() == 30
        assert "abc" in caplog.text

    def test_positive_value_used(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("AGENT_PERSONA_GROWTH_INTERVAL", "7")
        assert pkg._growth_interval_env() == 7


class TestGrowthEnabledSemantics:
    """AGENT_PERSONA_GROWTH_ENABLED：显式总开关（默认开，0/false 关）。"""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, True),
            ("1", True),
            ("true", True),
            ("0", False),
            ("false", False),
            ("off", False),
        ],
    )
    def test_parsing(self, monkeypatch, raw, expected):
        import plugins.qq_agent_adapter as pkg

        if raw is None:
            monkeypatch.delenv("AGENT_PERSONA_GROWTH_ENABLED", raising=False)
        else:
            monkeypatch.setenv("AGENT_PERSONA_GROWTH_ENABLED", raw)
        assert pkg._growth_enabled_env() is expected

    @pytest.mark.parametrize(
        "enabled,interval,expected",
        [
            ("1", "30", True),
            (None, "30", True),
            ("0", "30", False),  # 显式关闭优先于 interval
            ("1", "0", False),
            ("0", "0", False),
        ],
    )
    def test_growth_active(self, monkeypatch, enabled, interval, expected):
        import plugins.qq_agent_adapter as pkg

        if enabled is None:
            monkeypatch.delenv("AGENT_PERSONA_GROWTH_ENABLED", raising=False)
        else:
            monkeypatch.setenv("AGENT_PERSONA_GROWTH_ENABLED", enabled)
        monkeypatch.setenv("AGENT_PERSONA_GROWTH_INTERVAL", interval)
        assert pkg._growth_active() is expected


class TestGrowthMaxCharsSemantics:
    """成长层字符上限：与人格段争「怎么说话」的唯一调温旋钮（2026-10-15）。

    旧实现只有 interval 一个 env，max_growth_chars 写死 500——人格被成长层
    稀释时除了整体关闭没有任何调温手段。
    """

    def test_default_is_500(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.delenv("AGENT_PERSONA_GROWTH_MAX_CHARS", raising=False)
        assert pkg._growth_max_chars_env() == 500

    def test_smaller_value_used(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("AGENT_PERSONA_GROWTH_MAX_CHARS", "150")
        assert pkg._growth_max_chars_env() == 150

    def test_dirty_value_warns_and_defaults(self, monkeypatch, caplog):
        import logging

        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("AGENT_PERSONA_GROWTH_MAX_CHARS", "三百")
        with caplog.at_level(logging.WARNING):
            assert pkg._growth_max_chars_env() == 500
        assert "三百" in caplog.text

    def test_empty_string_falls_back(self, monkeypatch):
        import plugins.qq_agent_adapter as pkg

        monkeypatch.setenv("AGENT_PERSONA_GROWTH_MAX_CHARS", "   ")
        assert pkg._growth_max_chars_env() == 500


@pytest.mark.usefixtures("nb_driver")
class TestGrowthNotifyWiring:
    """growth 提议回调挂接的 None 守卫（本轮审查 P1 回归）。

    1d9b7b1 把 `growth.on_proposal = ...` 裸赋值搬进 _init_agent：interval=0
    （README 明文的关闭开关）时 growth=None → startup 期 AttributeError →
    NoneBot Lifespan 无容错 → 整个 bot 起不来。守卫集中在
    `_wire_growth_notify`，本用例钉死「None 必须安全跳过」。
    """

    def test_none_growth_is_safe_noop(self):
        import plugins.qq_agent_adapter as pkg

        # 关闭态（growth=None）：不得赋值、不得抛——守卫被移除时这里必然 AttributeError
        pkg._wire_growth_notify(None, lambda *a: None)

    def test_real_growth_gets_wired(self):
        import plugins.qq_agent_adapter as pkg

        class _G:
            pass

        g = _G()
        cb = lambda *a: None  # noqa: E731
        pkg._wire_growth_notify(g, cb)
        assert g.on_proposal is cb


@pytest.mark.usefixtures("nb_driver")
class TestIntOrHelper:
    """_int_or：env/config 数值脏值告警回退，启动路径零抛异常（本轮审查 P2）。"""

    def test_valid_values(self):
        import plugins.qq_agent_adapter as pkg

        assert pkg._int_or("7", 7, label="x") == 7
        assert pkg._int_or(14, 7, label="x") == 14

    def test_dirty_value_warns_and_falls_back(self, caplog):
        import logging

        import plugins.qq_agent_adapter as pkg

        with caplog.at_level(logging.WARNING):
            assert pkg._int_or("7天", 7, label="AGENT_X_KEEP") == 7
        assert "7天" in caplog.text
        assert pkg._int_or(None, 7, label="x") == 7


# ==========================================================================
# /growth [QQ] 查看、/growth clear [QQ] 清空（2026-10-15，BACKLOG §6.8）
#
# 改注入逻辑只能防新文本；库里已有的降格描述仍会原样生效，而 /reset 只清消息
# 不动 user_state.persona_growth——此前管理员只能手工连库写 SQL。
# ==========================================================================


@pytest.mark.usefixtures("nb_driver")
class TestGrowthCmdRule:
    """命令形态识别 + self/ACL/superuser 三层门。"""

    SUPERUSER = 9001

    def _event(self, user_id: int, text: str):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        segs = [{"type": "text", "data": {"text": text}}]
        return GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 1,
                "post_type": "message",
                "sub_type": "group",
                "user_id": user_id,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": segs,
                "original_message": segs,
                "raw_message": text,
                "font": 0,
                "sender": {"user_id": user_id, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )

    def _rule(self, monkeypatch):
        import importlib

        admin = importlib.import_module("plugins.qq_agent_adapter.admin")
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(
            admin, "is_superuser", lambda uid: uid == str(self.SUPERUSER)
        )
        return admin._growth_cmd_rule

    @pytest.mark.parametrize(
        "text",
        [
            "/growth",
            "growth",
            "/growth clear",
            "/growth 12345",
            "/growth clear 123456789",
            "growth clear 3865067623",
        ],
    )
    def test_supported_forms_match(self, monkeypatch, text):
        assert self._rule(monkeypatch)(self._event(self.SUPERUSER, text)) is True

    @pytest.mark.parametrize(
        "text",
        [
            "/growthclear",  # 无空白：nonebot 认命令，但参数形态非法
            "/growth 清空",  # 中文子命令不认
            "/growth 1234",  # QQ 号过短
            "/growth 1234567890123",  # QQ 号过长
            "/growth clear abc",
            "/growth １２３４５",  # 全角数字：能过 isdigit 但不是账号
            "确认成长 DEADBEEF",  # 属于另一个 matcher
            "今天天气怎么样",
        ],
    )
    def test_unsupported_forms_do_not_match(self, monkeypatch, text):
        assert self._rule(monkeypatch)(self._event(self.SUPERUSER, text)) is False

    def test_group_member_without_superuser_is_rejected(self, monkeypatch):
        rule = self._rule(monkeypatch)
        assert rule(self._event(12345, "/growth")) is False

    def test_self_message_filtered_by_shared_checker(self, monkeypatch):
        """self 过滤由共享的 _not_self_message 承担，并显式挂在 rule 上。

        仓库守卫 `test_usage.py::test_all_on_commands_have_self_filter` 按 checker
        **名字**识别 self 过滤——漏挂会红（L2 的根因正是漏加）。
        """
        import importlib

        admin = importlib.import_module("plugins.qq_agent_adapter.admin")
        assert admin._not_self_message(self._event(1, "/growth")) is False
        assert admin._not_self_message(self._event(self.SUPERUSER, "/growth")) is True
        names = set()
        for checker in admin.growth_cmd.rule.checkers:
            call = getattr(checker, "call", None)
            names.add(getattr(call, "__name__", "") or type(call).__name__)
        assert "_not_self_message" in names, f"rule 未挂 self 过滤：{names}"


@pytest.mark.usefixtures("nb_driver")
class TestGrowthCmdHandler:
    """handler 行为：清空/查看 × 有记录/无记录 × 显式 QQ/自己。"""

    SUPERUSER = "9001"

    class _Memory:
        def __init__(self, growth=None):
            self.growth = dict(growth or {})
            self.cleared = []

        async def get_persona_growth(self, user_id):
            return self.growth.get(user_id)

        async def clear_persona_growth(self, user_id):
            return self.growth.pop(user_id, None) is not None

    def _event(self, user_id: str, text: str):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        segs = [{"type": "text", "data": {"text": text}}]
        return GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 1,
                "post_type": "message",
                "sub_type": "group",
                "user_id": int(user_id),
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": segs,
                "original_message": segs,
                "raw_message": text,
                "font": 0,
                "sender": {"user_id": int(user_id), "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )

    async def _run(self, monkeypatch, text, memory):
        """跑一次 handler，返回它发出的全部回复文本。

        finish 被替换为收集 + 抛 FinishedException——真实 finish 的行为，
        这样「FinishedException 被 except Exception 吞掉」这类回归会直接暴露。
        """
        import importlib

        admin = importlib.import_module("plugins.qq_agent_adapter.admin")
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: uid == self.SUPERUSER)
        monkeypatch.setattr(admin, "_get_driver", lambda: _FakeDriver(memory))
        sent = []

        async def fake_finish(msg=None, **kwargs):
            sent.append(str(msg))
            raise _FinishedSentinel()

        monkeypatch.setattr(admin.growth_cmd, "finish", fake_finish)
        with pytest.raises(_FinishedSentinel):
            await admin.handle_growth_cmd(self._event(self.SUPERUSER, text))
        return sent

    @pytest.mark.asyncio
    async def test_clear_removes_and_reports(self, monkeypatch):
        mem = self._Memory({self.SUPERUSER: "用户喜欢被叫老板"})
        sent = await self._run(monkeypatch, "/growth clear", mem)
        assert mem.growth == {}, "必须真的清掉"
        assert len(sent) == 1, f"只应回一句，实得 {sent}"
        assert "已清空" in sent[0] and "用户喜欢被叫老板" in sent[0]
        assert "计数未重置" in sent[0]

    @pytest.mark.asyncio
    async def test_clear_without_record_is_noop(self, monkeypatch):
        mem = self._Memory()
        sent = await self._run(monkeypatch, "/growth clear", mem)
        assert "本来就没有" in sent[0]

    @pytest.mark.asyncio
    async def test_clear_targets_explicit_qq(self, monkeypatch):
        mem = self._Memory({"3865067623": "别人的成长层"})
        sent = await self._run(monkeypatch, "/growth clear 3865067623", mem)
        assert mem.growth == {}
        assert "3865067623" in sent[0]

    @pytest.mark.asyncio
    async def test_show_returns_full_text(self, monkeypatch):
        mem = self._Memory({self.SUPERUSER: "用户喜欢被叫老板"})
        sent = await self._run(monkeypatch, "/growth", mem)
        assert len(sent) == 1, f"只应回一句，实得 {sent}"
        assert "用户喜欢被叫老板" in sent[0]
        assert mem.growth, "查看不得改动数据"

    @pytest.mark.asyncio
    async def test_show_without_record(self, monkeypatch):
        sent = await self._run(monkeypatch, "/growth", self._Memory())
        assert "暂无人格成长记录" in sent[0]

    @pytest.mark.asyncio
    async def test_store_failure_reports_failure_once(self, monkeypatch):
        """存储炸了 → 只回一句「操作失败」，不得把成功伪装成失败（老坑回归）。"""

        class _Boom:
            async def get_persona_growth(self, user_id):
                raise RuntimeError("db down")

            async def clear_persona_growth(self, user_id):
                raise RuntimeError("db down")

        sent = await self._run(monkeypatch, "/growth clear", _Boom())
        assert sent == ["操作失败，请稍后重试。"]


class TestModelCmdRule:
    """``/model`` 命令形态识别 + self/ACL/superuser 三层门。"""

    SUPERUSER = 9001

    def _event(self, user_id: int, text: str):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        segs = [{"type": "text", "data": {"text": text}}]
        return GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 1,
                "post_type": "message",
                "sub_type": "group",
                "user_id": user_id,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": segs,
                "original_message": segs,
                "raw_message": text,
                "font": 0,
                "sender": {"user_id": user_id, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )

    def _rule(self, monkeypatch):
        import importlib

        admin = importlib.import_module("plugins.qq_agent_adapter.admin")
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(
            admin, "is_superuser", lambda uid: uid == str(self.SUPERUSER)
        )
        return admin._model_cmd_rule

    @pytest.mark.parametrize(
        "text",
        [
            "/model",
            "model",
            "/model reset",
            "/model step-3",
            "/model step-3@https://api.stepfun.com/v1",
            "/model llama@http://127.0.0.1:11434/v1",
            "/model reset step-3",  # 形态认得，由 handler 回用法错误
            "model  " + "x" * 200,  # 长度上限内
        ],
    )
    def test_supported_forms_match(self, monkeypatch, text):
        assert self._rule(monkeypatch)(self._event(self.SUPERUSER, text)) is True

    @pytest.mark.parametrize(
        "text",
        [
            "/modelclear",  # 无空白：nonebot 认命令，但参数形态非法
            "/model a b",  # 条目含空白：不让它落给聊天响应器
            "/model " + "x" * 201,  # 超长
            "确认成长 DEADBEEF",  # 属于另一个 matcher
            "今天天气怎么样",
        ],
    )
    def test_unsupported_forms_do_not_match(self, monkeypatch, text):
        assert self._rule(monkeypatch)(self._event(self.SUPERUSER, text)) is False

    def test_group_member_without_superuser_is_rejected(self, monkeypatch):
        rule = self._rule(monkeypatch)
        assert rule(self._event(12345, "/model")) is False

    def test_self_message_filtered_by_shared_checker(self, monkeypatch):
        """self 过滤由共享的 _not_self_message 承担，并显式挂在 rule 上。"""
        import importlib

        admin = importlib.import_module("plugins.qq_agent_adapter.admin")
        assert admin._not_self_message(self._event(1, "/model")) is False
        assert admin._not_self_message(self._event(self.SUPERUSER, "/model")) is True
        names = set()
        for checker in admin.model_cmd.rule.checkers:
            call = getattr(checker, "call", None)
            names.add(getattr(call, "__name__", "") or type(call).__name__)
        assert "_not_self_message" in names, f"rule 未挂 self 过滤：{names}"


@pytest.mark.usefixtures("nb_driver")
class TestModelCmdHandler:
    """handler 行为：查看 / 切换 / 回默认位 / 用法错误 / 权限门 / 异常只回一句。"""

    SUPERUSER = "9001"

    def _event(self, user_id: str, text: str):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        segs = [{"type": "text", "data": {"text": text}}]
        return GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 1,
                "post_type": "message",
                "sub_type": "group",
                "user_id": int(user_id),
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": segs,
                "original_message": segs,
                "raw_message": text,
                "font": 0,
                "sender": {"user_id": int(user_id), "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )

    async def _run(self, monkeypatch, text, *, superuser=True):
        """跑一次 handler，返回它发出的全部回复文本。

        finish 被替换为收集 + 抛 _FinishedSentinel——真实 finish 的行为，
        这样「FinishedException 被 except Exception 吞掉」这类回归会直接暴露。
        """
        import importlib

        from plugins.qq_agent_adapter import model_switch

        admin = importlib.import_module("plugins.qq_agent_adapter.admin")
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(
            admin, "is_superuser", lambda uid: superuser and uid == self.SUPERUSER
        )
        calls = {}

        async def fake_switch(spec, *, by_uid):
            calls["switch"] = (spec, by_uid)
            return model_switch.SwitchResult(True, f"已切换主模型：x → {spec}")

        async def fake_reset(*, by_uid):
            calls["reset"] = by_uid
            return model_switch.SwitchResult(True, "已恢复默认主模型：step-3.7-flash")

        monkeypatch.setattr(model_switch, "switch_active_model", fake_switch)
        monkeypatch.setattr(model_switch, "reset_active_model", fake_reset)
        monkeypatch.setattr(model_switch, "describe_status", lambda: "当前生效：step-3")
        sent = []

        async def fake_finish(msg=None, **kwargs):
            sent.append(str(msg))
            raise _FinishedSentinel()

        monkeypatch.setattr(admin.model_cmd, "finish", fake_finish)
        with pytest.raises(_FinishedSentinel):
            await admin.handle_model_cmd(self._event(self.SUPERUSER, text))
        return sent, calls

    @pytest.mark.asyncio
    async def test_show_returns_status(self, monkeypatch):
        sent, calls = await self._run(monkeypatch, "/model")
        assert sent == ["当前生效：step-3"]
        assert calls == {}, "查看不得触发切换"

    @pytest.mark.asyncio
    async def test_switch_passes_spec_and_uid(self, monkeypatch):
        """by_uid 必须传进去：审计要记 who（与 /growth clear 的 self 过滤同源）。"""
        sent, calls = await self._run(monkeypatch, "/model step-3")
        assert calls["switch"] == ("step-3", self.SUPERUSER)
        assert sent == ["已切换主模型：x → step-3"]

    @pytest.mark.asyncio
    async def test_reset_calls_reset(self, monkeypatch):
        sent, calls = await self._run(monkeypatch, "/model reset")
        assert calls.get("reset") == self.SUPERUSER
        assert "已恢复默认主模型" in sent[0]

    @pytest.mark.asyncio
    async def test_reset_with_spec_is_usage_error(self, monkeypatch):
        """reset 与条目同时出现 = 形态歧义，明说用法而不是猜一个执行。"""
        sent, calls = await self._run(monkeypatch, "/model reset step-3")
        assert calls == {}
        assert "用法" in sent[0]

    @pytest.mark.asyncio
    async def test_non_superuser_rejected(self, monkeypatch):
        sent, calls = await self._run(monkeypatch, "/model", superuser=False)
        assert sent == ["仅管理员可切换模型。"]
        assert calls == {}

    @pytest.mark.asyncio
    async def test_failure_reports_once(self, monkeypatch):
        """探测/落盘炸了 → 只回一句「未改动」，不得把成功伪装成失败。"""
        import importlib

        from plugins.qq_agent_adapter import model_switch

        admin = importlib.import_module("plugins.qq_agent_adapter.admin")
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: uid == self.SUPERUSER)

        async def boom(spec, *, by_uid):
            raise RuntimeError("probe down")

        monkeypatch.setattr(model_switch, "switch_active_model", boom)
        sent = []

        async def fake_finish(msg=None, **kwargs):
            sent.append(str(msg))
            raise _FinishedSentinel()

        monkeypatch.setattr(admin.model_cmd, "finish", fake_finish)
        with pytest.raises(_FinishedSentinel):
            await admin.handle_model_cmd(self._event(self.SUPERUSER, "/model step-3"))
        assert sent == ["操作失败，请稍后重试（模型未改动）。"]


class _FinishedSentinel(Exception):
    """测试用的流程控制异常替身（真实场景是 nonebot 的 FinishedException）。"""


class _FakeDriver:
    def __init__(self, memory):
        self._agent_memory = memory


@pytest.mark.usefixtures("nb_driver")
class TestSkillListCommand:
    """/skill list：查询 data/skills 下已安装技能（两种介质分组）。

    - 权限与 install/uninstall/info 同门（M3：manifest 权限串只回 superuser）。
    - 环境全部显式给（monkeypatch.setenv + tmp_path）：不隐式依赖本地 .env
      或仓库内 data/skills（CI #91 教训）。
    """

    @staticmethod
    def _admin():
        import importlib as _il

        return _il.import_module("plugins.qq_agent_adapter.admin")

    @staticmethod
    def _event(user_id="20002", text="/skill list"):
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
    def _skills_dir(tmp_path, monkeypatch, name="skills"):
        d = tmp_path / name
        d.mkdir()
        monkeypatch.setenv("AGENT_SKILLS_DIR", str(d))
        return d

    @pytest.mark.asyncio
    async def test_list_rejected_for_non_superuser(self, monkeypatch):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.list_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: False)

        def _boom_installer(*a, **k):
            raise AssertionError("非 superuser 不应触达已安装列表查询")

        monkeypatch.setattr(admin, "_get_installer", _boom_installer)
        with pytest.raises(FinishedException):
            await admin.handle_list(self._event())
        assert calls == ["只有管理员能查看已安装技能列表。"]

    @pytest.mark.asyncio
    async def test_list_denied_when_not_allowed(self, monkeypatch):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.list_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: False)

        def _boom_installer(*a, **k):
            raise AssertionError("未授权用户不应触达已安装列表查询")

        monkeypatch.setattr(admin, "_get_installer", _boom_installer)
        with pytest.raises(FinishedException):
            await admin.handle_list(self._event())
        assert calls == ["无权限"], calls

    @pytest.mark.asyncio
    async def test_empty_dir_gets_explicit_message(self, monkeypatch, tmp_path):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.list_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        d = self._skills_dir(tmp_path, monkeypatch)

        with pytest.raises(FinishedException):
            await admin.handle_list(self._event())
        assert calls == [f"{d} 下没有已安装的 skill。"], calls

    @pytest.mark.asyncio
    async def test_lists_both_media_grouped(self, monkeypatch, tmp_path):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.list_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        d = self._skills_dir(tmp_path, monkeypatch)
        (d / "translator.yaml").write_text(
            "name: translator\ndescription: 中英互译专家\ntype: prompt\n"
            "prompt: 你是一个专业翻译。\npermission: public\n",
            encoding="utf-8",
        )
        (d / "installed.yaml").write_text("name: installed\n", encoding="utf-8")
        (d / "ha_light.py").write_text("# 代码型技能\n", encoding="utf-8")
        (d / "zz_admin.py").write_text("# 代码型技能\n", encoding="utf-8")

        with pytest.raises(FinishedException):
            await admin.handle_list(self._event())
        text = calls[0]
        # yaml 组：installed.yaml 是记账文件，不得出现在列表里
        assert "translator: 中英互译专家" in text
        assert "installed:" not in text
        assert "prompt 型" in text and "即时生效" in text
        # py 组：文件名 + 重启生效提示（与 local.py「无热重载」一致）
        assert "ha_light.py" in text and "zz_admin.py" in text
        assert "代码型" in text and "重启后生效" in text

    @pytest.mark.asyncio
    async def test_respects_env_skills_dir(self, monkeypatch, tmp_path):
        """目录必须来自 AGENT_SKILLS_DIR：另一个目录的内容不得串味。"""
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.list_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        d = self._skills_dir(tmp_path, monkeypatch, name="mine")
        other = tmp_path / "other"
        other.mkdir()
        (d / "alpha.yaml").write_text(
            "name: alpha\ndescription: 甲的技能\ntype: prompt\nprompt: 甲\n",
            encoding="utf-8",
        )
        (other / "beta.yaml").write_text(
            "name: beta\ndescription: 乙的技能\ntype: prompt\nprompt: 乙\n",
            encoding="utf-8",
        )

        with pytest.raises(FinishedException):
            await admin.handle_list(self._event())
        assert "alpha: 甲的技能" in calls[0]
        assert "乙的技能" not in calls[0]


@pytest.mark.usefixtures("nb_driver")
class TestPersonaGroupCommand:
    """群人格命令（2026-10-20）：群聊仅 SUPERUSERS、只作用于本群；非超管静默 + 日志。

    变异锚点：去掉群聊 superuser 判定 → 静默用例红；use 误写用户选择 → 断言红。
    """

    @staticmethod
    def _admin():
        import importlib as _il

        return _il.import_module("plugins.qq_agent_adapter.admin")

    @staticmethod
    def _event(text, user_id="20002", group_id=None):
        class _Ev:
            def __init__(self):
                self._text = text
                self._uid = user_id
                self.group_id = group_id

            def get_message(self):
                return self._text

            def get_user_id(self):
                return self._uid

        return _Ev()

    @staticmethod
    def _stub_finish(monkeypatch, matcher, calls):
        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            raise FinishedException()

        monkeypatch.setattr(matcher, "finish", fake_finish)

    @staticmethod
    def _driver(monkeypatch, tmp_path):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.personas import PersonaManager

        (tmp_path / "a.md").write_text(
            "---\nname: default_p\ndefault: true\n---\nBODY", encoding="utf-8"
        )
        (tmp_path / "x.md").write_text(
            "---\nname: group_x\ndescription: 群人格\n---\nGROUP_BODY", encoding="utf-8"
        )
        memory = InMemoryMemoryStore()
        manager = PersonaManager(tmp_path)

        class _Driver:
            _agent_memory = memory
            _agent_persona_manager = manager

        return _Driver(), memory, manager

    @pytest.mark.asyncio
    async def test_group_non_superuser_is_silent_with_log(
        self, monkeypatch, tmp_path, caplog
    ):
        """群里非超管：不回话（不给探测口），但日志必须留痕。"""
        import logging

        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.persona_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: False)
        driver, memory, _ = self._driver(monkeypatch, tmp_path)
        monkeypatch.setattr(admin, "_get_driver", lambda: driver)

        with caplog.at_level(logging.WARNING, logger="plugins.qq_agent_adapter.admin"):
            # 不抛 FinishedException：静默 return
            await admin.handle_persona(
                self._event("/persona use group_x", group_id="999")
            )

        assert calls == []  # 一个字都没回
        assert await memory.get_group_persona("999") is None  # 也没改配置
        assert "persona command denied in group 999" in caplog.text
        assert "non-superuser 20002" in caplog.text

    @pytest.mark.asyncio
    async def test_group_superuser_sets_group_persona(self, monkeypatch, tmp_path):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.persona_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        driver, memory, _ = self._driver(monkeypatch, tmp_path)
        monkeypatch.setattr(admin, "_get_driver", lambda: driver)

        with pytest.raises(FinishedException):
            await admin.handle_persona(
                self._event("/persona use group_x", group_id="999")
            )
        assert await memory.get_group_persona("999") == "group_x"
        assert await memory.get_user_persona("20002") is None  # 不污染用户选择
        assert "已设置本群人格：group_x" in calls[0]

    @pytest.mark.asyncio
    async def test_group_reset_clears_group_persona(self, monkeypatch, tmp_path):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.persona_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        driver, memory, _ = self._driver(monkeypatch, tmp_path)
        monkeypatch.setattr(admin, "_get_driver", lambda: driver)

        await memory.set_group_persona("999", "group_x")
        with pytest.raises(FinishedException):
            await admin.handle_persona(self._event("/persona reset", group_id="999"))
        assert await memory.get_group_persona("999") is None
        assert "已清除本群人格" in calls[0]

    @pytest.mark.asyncio
    async def test_group_list_shows_group_persona(self, monkeypatch, tmp_path):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.persona_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        driver, memory, _ = self._driver(monkeypatch, tmp_path)
        monkeypatch.setattr(admin, "_get_driver", lambda: driver)

        await memory.set_group_persona("999", "group_x")
        with pytest.raises(FinishedException):
            await admin.handle_persona(self._event("/persona", group_id="999"))
        assert "当前群人格：group_x（管理员配置）" in calls[0]

    @pytest.mark.asyncio
    async def test_private_user_still_sets_own_persona(self, monkeypatch, tmp_path):
        """私聊不受影响：普通用户可以设置自己的全局选择。"""
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.persona_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: False)
        driver, memory, _ = self._driver(monkeypatch, tmp_path)
        monkeypatch.setattr(admin, "_get_driver", lambda: driver)

        with pytest.raises(FinishedException):
            await admin.handle_persona(self._event("/persona use group_x"))
        assert await memory.get_user_persona("20002") == "group_x"
        assert "已切换人格：group_x" in calls[0]

    @pytest.mark.asyncio
    async def test_group_use_unknown_persona(self, monkeypatch, tmp_path):
        admin = self._admin()
        calls = []
        self._stub_finish(monkeypatch, admin.persona_cmd, calls)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        driver, memory, _ = self._driver(monkeypatch, tmp_path)
        monkeypatch.setattr(admin, "_get_driver", lambda: driver)

        with pytest.raises(FinishedException):
            await admin.handle_persona(self._event("/persona use nope", group_id="999"))
        assert await memory.get_group_persona("999") is None
        assert "未找到人格：nope" in calls[0]


@pytest.mark.usefixtures("nb_driver")
class TestTwoFaCommandPrivacy:
    """REVIEW-1b16fa7..9c2d3c1 M1：/2fa 只许私聊。

    admin 响应器 priority=5 独立于群聊唤醒词纪律——superuser 在群里发 /2fa
    会把 5 分钟滚动码发进群（叠加 2FA 在线爆破面）。群聊触发必须拒绝。
    """

    SUPERUSER = "9001"

    @staticmethod
    def _event(group: bool, user_id: str = "9001"):
        from nonebot.adapters.onebot.v11 import (
            GroupMessageEvent,
            PrivateMessageEvent,
        )

        segs = [{"type": "text", "data": {"text": "/2fa"}}]
        base = {
            "time": 0,
            "self_id": 1,
            "post_type": "message",
            "user_id": int(user_id),
            "message": segs,
            "original_message": segs,
            "raw_message": "/2fa",
            "font": 0,
            "sender": {"user_id": int(user_id), "nickname": "", "card": ""},
            "to_me": False,
            "reply": None,
        }
        if group:
            return GroupMessageEvent.parse_obj(
                {
                    **base,
                    "sub_type": "group",
                    "message_type": "group",
                    "message_id": 1,
                    "group_id": 456,
                }
            )
        return PrivateMessageEvent.parse_obj(
            {**base, "sub_type": "friend", "message_type": "private", "message_id": 2}
        )

    async def _run(self, monkeypatch, event):
        import importlib as _il

        admin = _il.import_module("plugins.qq_agent_adapter.admin")
        monkeypatch.setattr(admin, "is_superuser", lambda uid: uid == self.SUPERUSER)
        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(str(msg))
            raise FinishedException()

        monkeypatch.setattr(admin.twofa_cmd, "finish", fake_finish)
        with pytest.raises(FinishedException):
            await admin.handle_2fa(event)
        return calls

    @pytest.mark.asyncio
    async def test_group_trigger_refused(self, monkeypatch):
        calls = await self._run(monkeypatch, self._event(group=True))
        assert calls == ["验证码只通过私聊发送：请私聊 bot 发 /2fa。"]

    @pytest.mark.asyncio
    async def test_private_trigger_returns_code(self, monkeypatch):
        calls = await self._run(monkeypatch, self._event(group=False))
        assert calls and "web 写操作验证码：" in calls[0]

    @pytest.mark.asyncio
    async def test_non_superuser_refused_everywhere(self, monkeypatch):
        calls = await self._run(monkeypatch, self._event(group=True, user_id="20002"))
        assert calls == ["只有管理员能获取验证码。"]
