import asyncio
import logging
import os

import pytest


class FakePrivateEvent:
    def __init__(self, user_id):
        self.user_id = user_id
        self.group_id = None

    def get_user_id(self):
        return str(self.user_id)


class FakeGroupEvent:
    def __init__(self, user_id, group_id):
        self.user_id = user_id
        self.group_id = group_id

    def get_user_id(self):
        return str(self.user_id)


class FakePrivateNoGroupAttr:
    """与真实 OneBot 私聊事件一致：**没有** group_id 属性。"""

    def __init__(self, user_id):
        self.user_id = user_id

    def get_user_id(self):
        return str(self.user_id)


class TestACL:
    def setup_method(self):
        # 保存原值，teardown 恢复——避免污染后续需要 SUPERUSERS 的测试（如 admin import 冒烟）
        self._orig = {k: os.environ.get(k) for k in ("SUPERUSERS", "ALLOWED_GROUPS")}
        os.environ["SUPERUSERS"] = "111,222"
        os.environ["ALLOWED_GROUPS"] = "333"
        # reload module to pick up env changes
        import importlib

        import plugins.qq_agent_adapter.acl as acl_mod

        importlib.reload(acl_mod)
        self.acl = acl_mod

    def teardown_method(self):
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_superuser_private_allowed(self):
        assert self.acl.is_allowed(FakePrivateEvent(111)) is True

    def test_non_superuser_private_denied(self):
        assert self.acl.is_allowed(FakePrivateEvent(999)) is False

    def test_superuser_group_allowed(self):
        assert self.acl.is_allowed(FakeGroupEvent(111, 333)) is True

    def test_non_superuser_allowed_group_allowed(self):
        assert self.acl.is_allowed(FakeGroupEvent(999, 333)) is True

    def test_non_superuser_denied_group_not_allowed(self):
        assert self.acl.is_allowed(FakeGroupEvent(999, 444)) is False

    def test_private_without_group_attr_denied(self):
        """私聊拒绝分支的真实覆盖（H：原用例走的是群分支，把 return False 改成
        return True 也不会失败）。"""
        assert self.acl.is_allowed(FakePrivateNoGroupAttr(999)) is False

    def test_private_without_group_attr_superuser_allowed(self):
        assert self.acl.is_allowed(FakePrivateNoGroupAttr(111)) is True


class TestStrictAllowed:
    """戳一戳等「会暴露本机信息」入口的判据：superuser **且** 群在白名单里。

    与 is_allowed 的差别就在「superuser 在没授权的群里」这一格——is_allowed 放行，
    is_strict_allowed 必须拒绝（否则本机概览会漏进任意群）。
    """

    def setup_method(self):
        self._orig = {k: os.environ.get(k) for k in ("SUPERUSERS", "ALLOWED_GROUPS")}
        os.environ["SUPERUSERS"] = "111,222"
        os.environ["ALLOWED_GROUPS"] = "333"
        import importlib

        import plugins.qq_agent_adapter.acl as acl_mod

        importlib.reload(acl_mod)
        self.acl = acl_mod

    def teardown_method(self):
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib

        import plugins.qq_agent_adapter.acl as acl_mod

        importlib.reload(acl_mod)

    def test_superuser_private_allowed(self):
        assert self.acl.is_strict_allowed(FakePrivateEvent(111)) is True

    def test_non_superuser_private_denied(self):
        assert self.acl.is_strict_allowed(FakePrivateEvent(999)) is False

    def test_superuser_in_whitelisted_group_allowed(self):
        assert self.acl.is_strict_allowed(FakeGroupEvent(111, 333)) is True

    def test_non_superuser_in_whitelisted_group_denied(self):
        """白名单群里的普通群友也不能触发（与 is_allowed 相反，这是刻意的）。"""
        assert self.acl.is_strict_allowed(FakeGroupEvent(999, 333)) is False

    def test_superuser_outside_whitelist_group_denied(self):
        assert self.acl.is_strict_allowed(FakeGroupEvent(111, 444)) is False

    def test_private_without_group_attr_non_superuser_denied(self):
        assert self.acl.is_strict_allowed(FakePrivateNoGroupAttr(999)) is False

    def test_empty_superusers_denies_everyone(self):
        """空 SUPERUSERS = 谁都不是（fail-closed），不能退化成"人人可戳"。"""
        import importlib

        os.environ["SUPERUSERS"] = ""
        import plugins.qq_agent_adapter.acl as acl_mod

        importlib.reload(acl_mod)
        assert acl_mod.is_strict_allowed(FakePrivateEvent(111)) is False
        assert acl_mod.is_strict_allowed(FakeGroupEvent(111, 333)) is False

    def test_empty_allowed_groups_denies_group(self):
        import importlib

        os.environ["ALLOWED_GROUPS"] = ""
        import plugins.qq_agent_adapter.acl as acl_mod

        importlib.reload(acl_mod)
        assert acl_mod.is_strict_allowed(FakeGroupEvent(111, 333)) is False


class TestBlockedUsers:
    """用户黑名单（BLOCKED_USERS）：白名单群里的特定人也能拦住。

    语义（会话「完善项目黑白名单机制」，2026-09 与用户确认）：
    黑名单检查放在 superuser 判断**之后** → superuser 豁免（防误拉黑锁死）；
    命中者所有 ``is_allowed`` 入口一律拒绝；空名单 = 不启用，行为与旧版一致。
    """

    def setup_method(self):
        self._orig = {
            k: os.environ.get(k)
            for k in ("SUPERUSERS", "ALLOWED_GROUPS", "BLOCKED_USERS")
        }
        os.environ["SUPERUSERS"] = "111,222"
        os.environ["ALLOWED_GROUPS"] = "333"
        os.environ["BLOCKED_USERS"] = "999"
        import importlib

        import plugins.qq_agent_adapter.acl as acl_mod

        importlib.reload(acl_mod)
        self.acl = acl_mod

    def teardown_method(self):
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib

        import plugins.qq_agent_adapter.acl as acl_mod

        importlib.reload(acl_mod)

    def _reload(self):
        import importlib

        importlib.reload(self.acl)

    def test_blocked_user_in_whitelisted_group_denied(self):
        """核心场景：旧实现里白名单群任意成员都放行，拉黑后必须拒绝。"""
        assert self.acl.is_allowed(FakeGroupEvent(999, 333)) is False

    def test_blocked_superuser_exempt(self):
        """superuser 豁免：222 同时在 SUPERUSERS 和黑名单里，仍放行（防锁死）。"""
        os.environ["BLOCKED_USERS"] = "999,222"
        self._reload()
        assert self.acl.is_allowed(FakeGroupEvent(222, 333)) is True
        assert self.acl.is_blocked(FakeGroupEvent(222, 333)) is False

    def test_blocked_user_private_denied(self):
        assert self.acl.is_allowed(FakePrivateEvent(999)) is False

    def test_blocked_user_in_unlisted_group_denied(self):
        assert self.acl.is_allowed(FakeGroupEvent(999, 444)) is False

    def test_other_members_unaffected(self):
        """黑名单只压名单内的人：同群其他人不受牵连。"""
        assert self.acl.is_allowed(FakeGroupEvent(12345, 333)) is True

    def test_empty_blocklist_is_noop(self):
        """空黑名单 = 不启用：放行/拒绝与旧版逐分支一致。"""
        os.environ["BLOCKED_USERS"] = ""
        self._reload()
        assert self.acl.is_allowed(FakeGroupEvent(999, 333)) is True
        assert self.acl.is_allowed(FakeGroupEvent(999, 444)) is False

    def test_is_blocked_predicate(self):
        assert self.acl.is_blocked(FakeGroupEvent(999, 333)) is True
        assert self.acl.is_blocked(FakeGroupEvent(12345, 333)) is False
        assert self.acl.is_blocked(FakePrivateEvent(999)) is True

    def test_strict_allowed_unaffected_by_blocklist(self):
        """is_strict_allowed 本就只给 superuser，而 superuser 豁免黑名单——
        两侧判定都不应因黑名单改变（黑名单在该入口是 no-op）。"""
        os.environ["BLOCKED_USERS"] = "999,222"
        self._reload()
        assert self.acl.is_strict_allowed(FakeGroupEvent(999, 333)) is False
        assert self.acl.is_strict_allowed(FakeGroupEvent(222, 333)) is True

    def test_nondigit_entry_warns_at_load(self, caplog):
        """全角数字等非法条目永远无法命中真实 user_id（黑名单静默失效是
        fail-open 方向的配置错误），加载时必须 WARNING 喊出来（AGENTS §5
        「全角数字通过 isdigit」坑）。"""
        os.environ["BLOCKED_USERS"] = "１２３,999"
        with caplog.at_level(logging.WARNING, logger="plugins.qq_agent_adapter.acl"):
            self._reload()
        assert "BLOCKED_USERS" in caplog.text
        assert "１２３" in caplog.text
        # 合法条目不受牵连，仍然生效
        assert self.acl.is_allowed(FakeGroupEvent(999, 333)) is False


class _FinishSentinel(Exception):
    """matcher.finish 替身信号（真实 NoneBot 里是 FinishedException）。"""


class _StubMatcher:
    """记录 finish 入参并抛哨兵：强制证明「finish 被调用且带/不带内容」。"""

    _UNSET = object()

    def __init__(self):
        self.finished_with = self._UNSET

    async def finish(self, message=None):
        self.finished_with = message
        raise _FinishSentinel


class TestDeny:
    """acl.deny 统一拒绝出口：黑名单命中静默（只记日志），越界维持原文案。

    断言设计防恒真：静默分支必须证明 finish 被**不带内容**调用（未调用或
    带内容调用都会让用例失败），日志必须含黑名单标识。
    """

    def setup_method(self):
        self._orig = {
            k: os.environ.get(k)
            for k in ("SUPERUSERS", "ALLOWED_GROUPS", "BLOCKED_USERS")
        }
        os.environ["SUPERUSERS"] = "111,222"
        os.environ["ALLOWED_GROUPS"] = "333"
        os.environ["BLOCKED_USERS"] = "999"
        import importlib

        import plugins.qq_agent_adapter.acl as acl_mod

        importlib.reload(acl_mod)
        self.acl = acl_mod

    def teardown_method(self):
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib

        import plugins.qq_agent_adapter.acl as acl_mod

        importlib.reload(acl_mod)

    def test_blocked_user_gets_silence_and_warning(self, caplog):
        m = _StubMatcher()
        with caplog.at_level(logging.WARNING, logger="plugins.qq_agent_adapter.acl"):
            with pytest.raises(_FinishSentinel):
                asyncio.run(self.acl.deny(m, FakeGroupEvent(999, 333), "无权限"))
        assert m.finished_with is None
        assert "黑名单" in caplog.text
        assert "999" in caplog.text

    def test_out_of_scope_gets_original_message(self):
        m = _StubMatcher()
        with pytest.raises(_FinishSentinel):
            asyncio.run(self.acl.deny(m, FakeGroupEvent(777, 444), "无权限"))
        assert m.finished_with == "无权限"

    def test_superuser_never_takes_silent_branch(self):
        """deny 的静默分支只对黑名单普通用户可达；superuser（豁免）即使
        误入黑名单也走明说分支——锁住优先级语义不被实现顺序翻转。"""
        m = _StubMatcher()
        with pytest.raises(_FinishSentinel):
            asyncio.run(self.acl.deny(m, FakeGroupEvent(222, 333), "无权限"))
        assert m.finished_with == "无权限"
