"""用量统计命令（/usage）：表格生成 + 边界。"""

import pytest


@pytest.fixture(scope="module")
def nb_driver():
    import nonebot

    # admin.py 的 on_command 构造需要 NoneBot 已初始化（同 test_admin_import）
    nonebot.init(_env_file=None, superusers={"10000"})
    from nonebot import get_driver

    return get_driver()


@pytest.fixture(scope="module")
def render_table(nb_driver):
    """NoneBot 初始化后才能 import admin（on_command 构造）。"""
    from plugins.qq_agent_adapter.admin import _render_usage_table

    return _render_usage_table


from agentcore.budget import CostBudget  # noqa: E402


def _seeded(tmp_path):
    b = CostBudget(root=tmp_path)
    b.record("chat", 3000, 2000, model="step-3.7-flash", route="group:123")
    b.record("chat", 1000, 500, model="step-3.7-flash", route="private:456")
    b.record("chat", 100, 50, model="fallback-model", route="group:123")
    b.record("embedding", 500)
    return b


@pytest.mark.usefixtures("nb_driver")
class TestRenderUsageTable:
    def test_contains_today_and_total(self, tmp_path, render_table):
        table = render_table(_seeded(tmp_path))
        assert "# 用量统计" in table
        assert "| 对话 token | 6,650 | 6,650 |" in table
        assert "| embedding token | 500 | 500 |" in table

    def test_route_and_model_sections(self, tmp_path, render_table):
        table = render_table(_seeded(tmp_path))
        assert "## 今日按路由" in table
        assert "group:123" in table  # 群号对群成员本可见，不脱敏
        assert "## 今日按模型" in table
        assert "step-3.7-flash" in table
        assert "fallback-model" in table  # fallback 模型名自然区分

    def test_private_route_is_masked(self, tmp_path, render_table):
        """L25：白名单群任意成员都能跑 /usage，私聊用户 QQ 号不得原样出图。"""
        table = render_table(_seeded(tmp_path))
        assert "private:456" not in table, "私聊 QQ 号不得原样暴露"
        assert "private:4***6" in table, "应显示脱敏后的路由键"

    def test_empty_budget_renders_without_sections(self, tmp_path, render_table):
        table = render_table(CostBudget(root=tmp_path))
        assert "# 用量统计" in table
        assert "| 对话 token | 0 | 0 |" in table
        assert "## 今日按路由" not in table  # 空明细不渲染章节

    def test_cost_shown_when_priced(self, tmp_path, render_table):
        b = CostBudget(
            root=tmp_path, price_prompt_per_m=1.0, price_completion_per_m=3.0
        )
        b.record("chat", 1_000_000, 0)
        table = render_table(b)
        assert "估算成本" in table
        assert "≈ 1.0000 元" in table


# ==========================================================================
# REVIEW-6ec3f7c..a36ea1d 修复回归
#   L20 账本明细值损坏不得让 /usage 抛 TypeError
#   L25 非管理员不得看到按路由明细
#   M3/L22 handler 必须卸载渲染且有异常隔离（旧实现该 coroutine 零覆盖）
# ==========================================================================


def _corrupt_ledger(tmp_path):
    """手工写入"值不是 dict"的明细桶（模拟账本被编辑/异版本写坏）。"""
    import json

    day = {
        "prompt": 10,
        "completion": 5,
        "embedding_tokens": 0,
        "chat_requests": 1,
        "embedding_requests": 0,
        "by_route": {
            "group:1": 5,
            "group:2": {"prompt": 1, "completion": 1, "requests": 1},
        },
        "by_model": {"m": "x"},
    }
    (tmp_path / "usage-2026-09.json").write_text(
        json.dumps({"days": {"2026-09-19": day}}), encoding="utf-8"
    )
    return CostBudget(root=tmp_path)


@pytest.mark.usefixtures("nb_driver")
class TestUsageRobustness:
    def test_corrupt_breakdown_does_not_crash(self, tmp_path, render_table):
        """L20：旧实现会在这里抛 TypeError: 'int' object is not subscriptable。"""
        table = render_table(_corrupt_ledger(tmp_path))
        assert "# 用量统计" in table
        # 结构合法的项仍要显示，损坏项被跳过
        assert "group:2" in table
        assert "group:1" not in table or "| 5 |" not in table

    def test_include_routes_false_omits_section(self, tmp_path, render_table):
        """L25：非管理员视图不得含按路由章节。"""
        table = render_table(_seeded(tmp_path), include_routes=False)
        assert "## 今日按路由" not in table
        assert "private:" not in table
        assert "## 今日按模型" in table, "按模型不含标识符，仍应显示"


class TestUsageHandlerOffload:
    """M3/L22：handler 之前零覆盖，且渲染是同步 CPU 独占事件循环。"""

    def _admin(self):
        import importlib as _il

        return _il.import_module("plugins.qq_agent_adapter.admin")

    def _event(self, user_id="10000"):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent

        segs = [{"type": "text", "data": {"text": "/usage"}}]
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
                "raw_message": "/usage",
                "font": 0,
                "sender": {"user_id": user_id, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )

    @pytest.mark.asyncio
    async def test_render_is_offloaded_to_thread(self, nb_driver, monkeypatch):
        """渲染必须经 asyncio.to_thread（上轮 H-1 的同一手法）。"""
        import asyncio

        admin = self._admin()
        calls = []
        offloaded = []

        async def fake_sleep(*a, **k):
            return None

        real_to_thread = asyncio.to_thread

        async def spy_to_thread(fn, *args, **kwargs):
            offloaded.append(getattr(fn, "__name__", str(fn)))
            if fn.__name__ == "render_table_png":
                return b"\x89PNG fake"
            return await real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(admin.asyncio, "to_thread", spy_to_thread)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        class _B:
            def today(self):
                return {
                    "date": "d",
                    "total": 0,
                    "prompt": 0,
                    "completion": 0,
                    "embedding_tokens": 0,
                    "chat_requests": 0,
                    "embedding_requests": 0,
                    "by_route": {},
                    "by_model": {},
                }

            def total(self):
                return dict(self.today())

            def estimate_cost(self):
                return None

        monkeypatch.setattr("agentcore.budget.get_budget", lambda: _B())

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            from nonebot.exception import FinishedException

            raise FinishedException()

        monkeypatch.setattr(admin.usage_cmd, "finish", fake_finish)

        from nonebot.exception import FinishedException

        with pytest.raises(FinishedException):
            await admin.handle_usage(self._event())
        assert "render_table_png" in offloaded, "渲染必须卸载到线程"
        assert calls, "必须有回复"

    @pytest.mark.asyncio
    async def test_render_exception_degrades_to_text(self, nb_driver, monkeypatch):
        """M3：渲染抛错要降级为纯文本，而不是异常冒泡、用户零回复。"""
        admin = self._admin()
        calls = []

        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        def boom(_table, **kw):
            raise RuntimeError("pillow exploded")

        monkeypatch.setattr("agentcore.render.table.render_table_png", boom)

        class _B:
            def today(self):
                return {
                    "date": "d",
                    "total": 0,
                    "prompt": 0,
                    "completion": 0,
                    "embedding_tokens": 0,
                    "chat_requests": 0,
                    "embedding_requests": 0,
                    "by_route": {},
                    "by_model": {},
                }

            def total(self):
                return dict(self.today())

            def estimate_cost(self):
                return None

        monkeypatch.setattr("agentcore.budget.get_budget", lambda: _B())

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            from nonebot.exception import FinishedException

            raise FinishedException()

        monkeypatch.setattr(admin.usage_cmd, "finish", fake_finish)

        from nonebot.exception import FinishedException

        with pytest.raises(FinishedException):
            await admin.handle_usage(self._event())
        assert calls and isinstance(calls[0], str), "必须回退纯文本而非抛穿"
        assert "用量统计" in calls[0]

    @pytest.mark.asyncio
    async def test_ledger_read_failure_replies_error(self, nb_driver, monkeypatch):
        admin = self._admin()
        calls = []
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        def boom():
            raise RuntimeError("ledger broken")

        monkeypatch.setattr("agentcore.budget.get_budget", boom)

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            from nonebot.exception import FinishedException

            raise FinishedException()

        monkeypatch.setattr(admin.usage_cmd, "finish", fake_finish)

        from nonebot.exception import FinishedException

        with pytest.raises(FinishedException):
            await admin.handle_usage(self._event())
        assert calls == ["读取用量账本失败，请稍后再试。"]

    def test_usage_cmd_has_self_filter_rule(self, nb_driver):
        """L2：usage_cmd 的组合 Rule 里必须挂着 _not_self_message。

        断言组合后的 Rule 对象（而非源码文本）——源码排版变了也不会误报/漏报。
        """
        import importlib as _il

        admin = _il.import_module("plugins.qq_agent_adapter.admin")
        checkers = getattr(admin.usage_cmd.rule, "checkers", set())
        names = {getattr(getattr(c, "call", None), "__name__", "") for c in checkers}
        assert "_not_self_message" in names, (
            f"usage_cmd 缺少 self 过滤（其余 on_command 都有）：{sorted(names)}"
        )

    def _rule_checker_names(self, obj) -> set:
        """Rule 上挂的检查器名字集合。

        NoneBot 把每个检查器包成 ``Dependent(call=...)``：``call`` 可能是函数
        （有 ``__name__``，如 ``_not_self_message``），也可能是**实例**
        （``Command``，只有类型名）——两者都要收，否则识别不出 on_command。
        """
        checkers = getattr(getattr(obj, "rule", None), "checkers", set())
        names = set()
        for c in checkers:
            call = getattr(c, "call", None)
            if call is None:
                continue
            names.add(getattr(call, "__name__", "") or type(call).__name__)
        return names

    def test_all_on_commands_have_self_filter(self, nb_driver):
        """把"新增 on_command 必须带该 rule"变成可执行约束（L2 的根因是漏加）。

        注意：NoneBot 的匹配器对象 ``type()`` 是元类 ``MatcherMeta``（``isinstance``
        也不是 ``Matcher``），所以必须用鸭子类型识别——否则本用例会**空转**（曾如此）。
        末尾的 ``inspected >= 10`` 就是防它再次退化成恒真断言。
        """
        import importlib as _il

        admin = _il.import_module("plugins.qq_agent_adapter.admin")
        missing = []
        inspected = 0
        for attr in dir(admin):
            obj = getattr(admin, attr)
            # 鸭子类型：on_command/on_message 产物都有 rule + handle + type
            if not (
                hasattr(obj, "rule")
                and hasattr(obj, "handle")
                and getattr(obj, "type", None) == "message"
            ):
                continue
            names = self._rule_checker_names(obj)
            # 只看 on_command 产物（带 Command 检查器）；on_message 的两个确认门
            # 用的是自定义 rule（内部自行过滤 self，另有各自用例覆盖）
            if not any(n.startswith("Command") for n in names):
                continue
            inspected += 1
            if "_not_self_message" not in names:
                missing.append(attr)
        assert inspected >= 10, f"只扫到 {inspected} 个 on_command，用例可能已空转"
        assert not missing, f"以下 on_command 缺 self 过滤：{missing}"


class TestHelpRenderOffloaded:
    """M3 第三处：/aihelp 的同步 Pillow 也要卸载到线程。"""

    @pytest.mark.asyncio
    async def test_help_render_offloaded(self, nb_driver, monkeypatch):
        import asyncio
        import importlib as _il

        admin = _il.import_module("plugins.qq_agent_adapter.admin")
        offloaded = []
        real = asyncio.to_thread

        async def spy(fn, *a, **kw):
            offloaded.append(getattr(fn, "__name__", str(fn)))
            return await real(fn, *a, **kw)

        monkeypatch.setattr(admin.asyncio, "to_thread", spy)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)

        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            from nonebot.exception import FinishedException

            raise FinishedException()

        monkeypatch.setattr(admin.help_cmd, "finish", fake_finish)

        import nonebot.adapters.onebot.v11 as ob11

        segs = [{"type": "text", "data": {"text": "/aihelp"}}]
        event = ob11.GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 1,
                "post_type": "message",
                "sub_type": "group",
                "user_id": 10000,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": segs,
                "original_message": segs,
                "raw_message": "/aihelp",
                "font": 0,
                "sender": {"user_id": 10000, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )
        from nonebot.exception import FinishedException

        with pytest.raises(FinishedException):
            await admin.handle_help(event)
        assert "render_help_image" in offloaded, "帮助图片渲染必须卸载到线程"


class TestBackgroundJobsHaveRoutes:
    """L21：蒸馏/人格成长的 LLM 调用必须带 route，否则 /usage 对不上账。"""

    def test_distill_wraps_call_in_route_context(self):
        import importlib as _il

        mod = _il.import_module("agentcore.rag.distill")
        assert hasattr(mod, "route_context"), "蒸馏模块必须引入 route_context"
        src = (
            __import__("pathlib")
            .Path("agentcore/rag/distill.py")
            .read_text(encoding="utf-8")
        )
        assert 'route_context("kb:distill")' in src

    def test_growth_wraps_calls_in_route_context(self):
        import importlib as _il

        mod = _il.import_module("agentcore.personas.growth")
        assert hasattr(mod, "route_context")
        src = (
            __import__("pathlib")
            .Path("agentcore/personas/growth.py")
            .read_text(encoding="utf-8")
        )
        assert 'route_context("admin:growth-propose")' in src
        assert 'route_context("admin:growth-confirm")' in src

    @pytest.mark.asyncio
    async def test_growth_propose_actually_enters_route_context(self):
        """行为级：跑一次 _propose，route_context 必须被调用（源码断言之外的保险）。"""
        import agentcore.personas.growth as gmod
        from agentcore.memory.store import InMemoryMemoryStore

        entered: list[str] = []
        real = gmod.route_context

        def spy(route):
            entered.append(route)
            return real(route)

        gmod.route_context = spy
        try:
            store = InMemoryMemoryStore()
            for i in range(4):
                store.messages.setdefault("s1", []).append(
                    {
                        "id": i,
                        "role": "user",
                        "content": f"m{i}",
                        "tool_calls": None,
                        "tool_call_id": None,
                    }
                )

            class _LLM:
                async def chat(self, messages, tools=None, max_tokens=None):
                    return {"choices": [{"message": {"content": "他喜欢被叫老板"}}]}

            g = gmod.GrowthManager(store, _LLM(), interval=3)
            await g._propose("u1", "s1")
        finally:
            gmod.route_context = real
        assert entered == ["admin:growth-propose"], f"实际进入：{entered}"


class TestLayeredRobustnessIndependently:
    """L20 双层防御（budget 源头 + admin 渲染）必须**各自**可测。

    只测一端时，单层变异会被另一端遮蔽而存活（实测：屏蔽 budget 规范化后
    admin 的过滤兜住了；反过来屏蔽 admin 过滤时 budget 的规范化兜住了）。
    """

    def _stub_budget(self, today: dict):
        class _B:
            def today(self_inner):
                return today

            def total(self_inner):
                return dict(today)

            def estimate_cost(self_inner):
                return None

        return _B()

    def test_admin_layer_survives_corrupt_budget_object(self, render_table):
        """绕过 budget 规范化，直接给 admin 一个"脏"的 today() 结果。"""
        bad_today = {
            "date": "2026-09-19",
            "total": 3,
            "prompt": 2,
            "completion": 1,
            "embedding_tokens": 0,
            "chat_requests": 2,
            "embedding_requests": 0,
            "by_route": {"group:1": 5},  # 值不是 dict
            "by_model": {"m": "x", "ok": {"prompt": 1, "completion": 1, "requests": 1}},
        }
        table = render_table(self._stub_budget(bad_today))
        assert "# 用量统计" in table
        assert "ok" in table, "结构合法的项仍应渲染"
        assert "| 5 |" not in table, "损坏项不得进表"

    def test_budget_layer_normalizes_at_source(self, tmp_path):
        """【budget 层】today() 的明细值必须已是 dict（不依赖调用方过滤）。"""
        import json

        from agentcore.budget import CostBudget

        day = {
            "prompt": 1,
            "completion": 1,
            "by_route": {"g": 5, "h": {"prompt": 1, "completion": 1, "requests": 1}},
            "by_model": {"m": "x"},
        }
        (tmp_path / "usage-2026-09.json").write_text(
            json.dumps({"days": {"2026-09-19": day}}), encoding="utf-8"
        )
        today = CostBudget(root=tmp_path).today()
        for bucket_key in ("by_route", "by_model"):
            for k, v in today[bucket_key].items():
                assert isinstance(v, dict), f"{bucket_key}[{k!r}] 未规范化：{v!r}"
                assert set(v) == {"prompt", "completion", "requests"}
        assert "g" not in today["by_route"], "损坏项应被丢弃"
        assert "h" in today["by_route"]


class TestNonAdminRouteVisibility:
    """L25 的**接线**层：非管理员视图必须真的不带路由明细。

    只测 `_render_usage_table(include_routes=False)` 是不够的——那测的是函数参数，
    不是 handler 的判定；把 handler 改成恒 True 也不会失败（实测如此）。
    """

    @pytest.mark.asyncio
    async def test_non_superuser_reply_has_no_route_rows(
        self, nb_driver, monkeypatch, tmp_path
    ):
        import importlib as _il

        from agentcore.budget import CostBudget

        admin = _il.import_module("plugins.qq_agent_adapter.admin")
        b = CostBudget(root=tmp_path)
        b.record("chat", 10, 5, model="m", route="private:456789")

        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: False)
        monkeypatch.setattr("agentcore.budget.get_budget", lambda: b)
        # 渲染必然失败 → 直接回退文本，便于检查文本内容
        monkeypatch.setattr(
            "agentcore.render.table.render_table_png", lambda *a, **k: None
        )

        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            from nonebot.exception import FinishedException

            raise FinishedException()

        monkeypatch.setattr(admin.usage_cmd, "finish", fake_finish)

        import nonebot.adapters.onebot.v11 as ob11

        segs = [{"type": "text", "data": {"text": "/usage"}}]
        event = ob11.GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 1,
                "post_type": "message",
                "sub_type": "group",
                "user_id": 99999,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": segs,
                "original_message": segs,
                "raw_message": "/usage",
                "font": 0,
                "sender": {"user_id": 99999, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )
        from nonebot.exception import FinishedException

        with pytest.raises(FinishedException):
            await admin.handle_usage(event)
        text = calls[0]
        assert "## 今日按路由" not in text, "非管理员不得收到按路由明细"
        assert "456789" not in text
        assert "仅管理员可见" in text

    @pytest.mark.asyncio
    async def test_superuser_reply_keeps_route_rows(
        self, nb_driver, monkeypatch, tmp_path
    ):
        import importlib as _il

        from agentcore.budget import CostBudget

        admin = _il.import_module("plugins.qq_agent_adapter.admin")
        b = CostBudget(root=tmp_path)
        b.record("chat", 10, 5, model="m", route="group:456")

        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        monkeypatch.setattr("agentcore.budget.get_budget", lambda: b)
        monkeypatch.setattr(
            "agentcore.render.table.render_table_png", lambda *a, **k: None
        )

        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            from nonebot.exception import FinishedException

            raise FinishedException()

        monkeypatch.setattr(admin.usage_cmd, "finish", fake_finish)

        import nonebot.adapters.onebot.v11 as ob11

        segs = [{"type": "text", "data": {"text": "/usage"}}]
        event = ob11.GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 1,
                "post_type": "message",
                "sub_type": "group",
                "user_id": 10000,
                "message_type": "group",
                "message_id": 1,
                "group_id": 456,
                "message": segs,
                "original_message": segs,
                "raw_message": "/usage",
                "font": 0,
                "sender": {"user_id": 10000, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )
        from nonebot.exception import FinishedException

        with pytest.raises(FinishedException):
            await admin.handle_usage(event)
        assert "## 今日按路由" in calls[0]
        assert "group:456" in calls[0]
