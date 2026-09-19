"""点歌：群白名单 + 账号级冷却测试。

来源：AC-music-playback.md 的 B 组。
"""

import pytest

from agentcore.music import gate


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "AGENT_MUSIC_ALLOWED_GROUPS",
        "AGENT_MUSIC_COOLDOWN",
        "ALLOWED_GROUPS",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """default_cooldown 是进程级单例，用例之间必须隔离，否则冷却状态互相污染。"""
    gate._default_cooldown = None
    yield
    gate._default_cooldown = None


# ---------- B1 群白名单 ----------
class TestGroupWhitelist:
    def test_empty_means_all_denied(self, monkeypatch):
        """默认全关：冷门功能走 opt-in，不该因为配了 ALLOWED_GROUPS 就自动开放。"""
        monkeypatch.setenv("ALLOWED_GROUPS", "111,222")
        assert gate.allowed_groups() == set()
        assert gate.is_group_allowed("111") is False

    def test_listed_group_allowed_others_denied(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "111, 222")
        assert gate.allowed_groups() == {"111", "222"}
        assert gate.is_group_allowed("111") is True
        assert gate.is_group_allowed("333") is False

    def test_does_not_inherit_allowed_groups(self, monkeypatch):
        """绝不能继承 ALLOWED_GROUPS：否则以后往 ALLOWED_GROUPS 加群就意外获得点歌能力。

        只设 ALLOWED_GROUPS、不设 AGENT_MUSIC_ALLOWED_GROUPS——继承路径只在
        后者缺失时才可能走到，两个都设的话变异不会暴露。
        """
        monkeypatch.setenv("ALLOWED_GROUPS", "999")
        monkeypatch.delenv("AGENT_MUSIC_ALLOWED_GROUPS", raising=False)
        assert gate.allowed_groups() == set()
        assert gate.is_group_allowed("999") is False

    def test_rejects_full_width_digits(self, monkeypatch):
        """全角 "１２" 能过 isdigit()，放进集合只会让白名单静默失效（§5 的坑）。"""
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "１２３,111")
        assert gate.allowed_groups() == {"111"}

    def test_rejects_non_numeric(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "abc,111")
        assert gate.allowed_groups() == {"111"}

    def test_private_chat_not_gated_here(self, monkeypatch):
        """group_id 为空（私聊）不由本函数把关，交给路由层。"""
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "")
        assert gate.is_group_allowed(None) is True
        assert gate.is_group_allowed("") is True

    def test_accepts_int_group_id(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_ALLOWED_GROUPS", "111")
        assert gate.is_group_allowed(111) is True


# ---------- B2 冷却哨兵 ----------
class TestCooldownSentinel:
    def test_first_use_allowed_even_when_clock_near_zero(self):
        """§5 的坑：monotonic() 是开机时长，哨兵若用 0.0 会让首次请求被误判在冷却内。

        注入一个刚启动的小时钟值（0.5s），若实现用 0.0 当哨兵，
        ``0.5 - 0.0 = 0.5 < 30`` 会恒成立 → 首次必被拒。
        """
        cd = gate.PlayCooldown(cooldown=30, clock=lambda: 0.5)
        assert cd.remaining() == 0.0
        assert cd.try_acquire() == 0.0

    def test_first_use_allowed_at_large_clock(self):
        cd = gate.PlayCooldown(cooldown=30, clock=lambda: 987654.0)
        assert cd.try_acquire() == 0.0


# ---------- B3 冷却拦截 ----------
class TestCooldownRejects:
    def test_second_request_rejected_with_remaining(self):
        now = {"t": 1000.0}
        cd = gate.PlayCooldown(cooldown=30, clock=lambda: now["t"])
        assert cd.try_acquire() == 0.0

        now["t"] = 1010.0
        left = cd.try_acquire()
        assert left == pytest.approx(20.0)

    def test_recovers_after_cooldown_elapses(self):
        now = {"t": 0.0}
        cd = gate.PlayCooldown(cooldown=30, clock=lambda: now["t"])
        cd.try_acquire()
        now["t"] = 31.0
        assert cd.try_acquire() == 0.0

    def test_reset_clears_state(self):
        now = {"t": 0.0}
        cd = gate.PlayCooldown(cooldown=30, clock=lambda: now["t"])
        cd.try_acquire()
        now["t"] = 5.0
        assert cd.try_acquire() > 0
        cd.reset()
        assert cd.try_acquire() == 0.0

    def test_zero_cooldown_disables(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_COOLDOWN", "0")
        assert gate.cooldown_seconds() == 0
        cd = gate.PlayCooldown(cooldown=0)
        cd.try_acquire()
        assert cd.try_acquire() == 0.0

    def test_default_is_30s(self):
        assert gate.cooldown_seconds() == 30

    @pytest.mark.parametrize("raw", ["abc", "-5"])
    def test_bad_values_fall_back_to_default(self, monkeypatch, raw):
        monkeypatch.setenv("AGENT_MUSIC_COOLDOWN", raw)
        assert gate.cooldown_seconds() == 30

    def test_valid_value_used(self, monkeypatch):
        monkeypatch.setenv("AGENT_MUSIC_COOLDOWN", "45")
        assert gate.cooldown_seconds() == 45


# ---------- B4 账号级 ----------
class TestAccountLevelCooldown:
    def test_singleton_shared_across_callers(self):
        assert gate.default_cooldown() is gate.default_cooldown()

    def test_one_group_blocks_another_immediately(self):
        """核心断言：A 群放过歌后 B 群立刻请求要被拒。

        否则两个群会各发一条约 7.8s 的上传，在账号级串行的 highway 上互相排队，
        把彼此的延迟顶到 16s。per-group 冷却挡不住这个。
        """
        cd = gate.default_cooldown()
        assert cd.try_acquire() == 0.0
        assert cd.try_acquire() > 0, "账号级冷却应让第二个请求被拒（无论来自哪个群）"

    def test_remaining_is_read_only(self):
        """remaining() 只读、不记账：查询剩余时间不该顺带重置冷却。"""
        now = {"t": 0.0}
        cd = gate.PlayCooldown(cooldown=30, clock=lambda: now["t"])
        cd.try_acquire()
        now["t"] = 10.0
        cd.remaining()
        cd.remaining()
        assert cd.try_acquire() == pytest.approx(20.0)


# ---------- B5 无队列 ----------
class TestNoQueue:
    def test_repeated_requests_rejected_immediately_not_queued(self):
        """额度没腾出来时，后续请求必须**立刻被拒**，而不是阻塞排队等它腾出来。

        这是"不需要 asyncio.Lock / 队列"的根源：冷却 30s 大于最坏任务耗时
        （编码 ~1s + 上传 ~8s），所以只可能拒绝、不可能积压。
        """
        now = {"t": 0.0}
        cd = gate.PlayCooldown(cooldown=30, clock=lambda: now["t"])
        assert cd.try_acquire() == 0.0
        # 时钟不推进：额度没恢复，连续请求必须全部立刻被拒
        for _ in range(5):
            assert cd.try_acquire() > 0
