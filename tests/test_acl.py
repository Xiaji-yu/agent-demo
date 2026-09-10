import os


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
