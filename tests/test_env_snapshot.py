"""env_snapshot（出生环境快照）测试：锁 prime() 的顺序不变量与快照内容。

来源：2026-10-12 线上事故——改 .env 给 ALLOWED_GROUPS 加白名单群后 /reboot，
戳一戳 ACL 读的仍是进程里旧的白名单（``load_dotenv`` override=False 注入的旧值
被 execv 原样带给新进程，且新进程的 load_dotenv 刷不掉）。修复把「抓快照 →
load_dotenv」收进 :func:`~agentcore.env_snapshot.prime`，reboot 的 re-exec 只带
出生 environ。reboot 路径的回归在 ``test_reboot.py::TestReExecPristineEnv``。
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _reset_snapshot(monkeypatch):
    """每个用例都从「无快照」开始——快照是模块级全局，防跨用例泄漏。"""
    from agentcore import env_snapshot

    monkeypatch.setattr(env_snapshot, "_snapshot", None)
    yield


class TestPrime:
    def test_capture_happens_before_dotenv_load(self, monkeypatch, tmp_path):
        """顺序不变量：快照里不能有 .env 派生键，os.environ 里要有。

        顺序反了（先 load_dotenv 再 capture）快照就被污染，execv 重启仍拿旧值
        ——2026-10-12 事故的根因侧。
        """
        from agentcore import env_snapshot

        monkeypatch.delenv("AGENT_TEST_PRIME_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("AGENT_TEST_PRIME_KEY=from-file\n", encoding="utf-8")

        env_snapshot.prime(env_file)

        assert os.environ["AGENT_TEST_PRIME_KEY"] == "from-file"
        assert "AGENT_TEST_PRIME_KEY" not in (env_snapshot.pristine_environ() or {})
        # L3（REVIEW-1e0f5ae..a0a6bb4）：prime 直接写真实 os.environ（绕过
        # monkeypatch），不清理会泄漏进同进程的后续用例
        os.environ.pop("AGENT_TEST_PRIME_KEY", None)

    def test_prime_keeps_override_false_semantics(self, monkeypatch, tmp_path):
        """出生时就有的键（真实环境变量）不被 .env 覆盖，且原样进快照——
        env > .env 的优先级模型不能因修复改变。"""
        from agentcore import env_snapshot

        monkeypatch.setenv("AGENT_TEST_PRIME_KEY", "from-shell")
        env_file = tmp_path / ".env"
        env_file.write_text("AGENT_TEST_PRIME_KEY=from-file\n", encoding="utf-8")

        env_snapshot.prime(env_file)

        assert os.environ["AGENT_TEST_PRIME_KEY"] == "from-shell"
        snapshot = env_snapshot.pristine_environ() or {}
        assert snapshot.get("AGENT_TEST_PRIME_KEY") == "from-shell"
        # L3：同上——setenv 的键由 monkeypatch 恢复，但 prime 可能注入的
        # 其它键不会；显式清掉本用例引入的键（setenv 键 pop 后 monkeypatch
        # 仍会按记录恢复，双保险无害）
        os.environ.pop("AGENT_TEST_PRIME_KEY", None)


class TestCapture:
    def test_pristine_environ_none_before_capture(self):
        from agentcore import env_snapshot

        assert env_snapshot.pristine_environ() is None

    def test_capture_is_idempotent(self, monkeypatch):
        """重复调用沿用第一次结果——启动路径上的二次调用不会混入 .env 派生值。"""
        from agentcore import env_snapshot

        monkeypatch.delenv("AGENT_TEST_PRIME_KEY", raising=False)
        first = env_snapshot.capture()
        monkeypatch.setenv("AGENT_TEST_PRIME_KEY", "late")
        second = env_snapshot.capture()
        assert "AGENT_TEST_PRIME_KEY" not in first
        assert "AGENT_TEST_PRIME_KEY" not in second

    def test_returned_copy_does_not_mutate_global(self, monkeypatch):
        """capture()/pristine_environ() 返回副本——调用方改动不污染全局快照。"""
        from agentcore import env_snapshot

        snap = env_snapshot.capture()
        snap["AGENT_TEST_INJECTED"] = "x"
        assert "AGENT_TEST_INJECTED" not in (env_snapshot.pristine_environ() or {})
