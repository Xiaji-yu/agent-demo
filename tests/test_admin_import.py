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
