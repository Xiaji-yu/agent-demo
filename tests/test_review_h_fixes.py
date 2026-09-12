"""针对 REVIEW-a604023..679c9b3 五条 H 的回归测试。

每条都先复现过原缺陷，再锁定修复后的行为（防止回退）。
"""

from __future__ import annotations

import time

import httpx
import pytest


# ---------------------------------------------------------------- H1 zip
class TestZipWhitelist:
    def test_rejects_unzip_command_execution(self):
        from agentcore.workspace.runner import permitted

        for args in (
            ["o.zip", "a.txt", "-T", "-TT", "id"],
            ["o.zip", "a.txt", "-T", "--unzip-command=touch /tmp/x"],
            ["o.zip", "a.txt", "--unzip-command=touch x"],
            ["o.zip", "a.txt", "-TT", "sh -c id"],
        ):
            ok, reason = permitted("zip", args)
            assert not ok, args
            assert "zip" in reason

    def test_rejects_destructive_flags(self):
        from agentcore.workspace.runner import permitted

        for bad in ("-m", "-d", "-@", "-P", "-e", "-x", "--out"):
            assert not permitted("zip", ["o.zip", "a.txt", bad])[0], bad

    def test_allows_plain_packaging(self):
        from agentcore.workspace.runner import permitted

        assert permitted("zip", ["o.zip", "a.txt", "b.txt"])[0]
        assert permitted("zip", ["-r", "-q", "o.zip", "dir"])[0]


# ---------------------------------------------------------------- H2 curl
class TestCurlUrlValidation:
    def test_bare_internal_address_rejected(self):
        from agentcore.workspace.runner import permitted

        # 原缺陷：https 诱饵 + 裸内网地址 → 判定通过并实连 loopback
        for args in (
            ["https://example.com/", "127.0.0.1:8000/"],
            ["https://example.com/", "169.254.169.254/latest/meta-data/"],
            ["https://example.com/", "localhost:8000/"],
            ["127.0.0.1:8000/"],
        ):
            ok, reason = permitted("curl", args)
            assert not ok, args
            assert "https" in reason or "内网" in reason

    def test_plain_http_rejected(self):
        from agentcore.workspace.runner import permitted

        assert not permitted("curl", ["http://example.com/"])[0]

    def test_upload_and_proxy_flags_rejected(self):
        from agentcore.workspace.runner import permitted

        for args in (
            ["-T", "secret.txt", "https://example.com/"],
            ["--upload-file", "secret.txt", "https://example.com/"],
            ["-x", "http://127.0.0.1:8080", "https://example.com/"],
            ["--proxy=http://127.0.0.1:8080", "https://example.com/"],
            ["-k", "https://example.com/"],
            ["-K", "cfg", "https://example.com/"],
        ):
            assert not permitted("curl", args)[0], args

    def test_allows_plain_https_get(self):
        from agentcore.workspace.runner import permitted

        assert permitted("curl", ["-s", "https://example.com/x"])[0]
        assert permitted("curl", ["-s", "https://example.com/x", "--max-time=5"])[0]

    def test_loopback_ip_https_still_rejected(self):
        from agentcore.workspace.runner import permitted

        assert not permitted("curl", ["https://127.0.0.1/"])[0]


# ---------------------------------------------------------------- H3 pow
class TestPowBomb:
    def test_binop_pow_still_rejected(self):
        import ast

        from agentcore.skills.basic_tools import _reject_pow_bomb

        with pytest.raises(ValueError):
            _reject_pow_bomb(ast.parse("2**999999999", mode="eval"))

    def test_pow_call_rejected_statically(self):
        import ast

        from agentcore.skills.basic_tools import _reject_pow_bomb

        for expr in ("pow(2, 999999999)", "pow(2, n)", "pow(2, 99999)"):
            with pytest.raises(ValueError), pytest.MonkeyPatch.context():
                _reject_pow_bomb(ast.parse(expr, mode="eval"))

    @pytest.mark.asyncio
    async def test_evaluate_pow_bomb_fails_fast(self):
        from agentcore.skills.basic_tools import evaluate

        start = time.perf_counter()
        out = await evaluate("pow(2, 999999999)")
        cost = time.perf_counter() - start
        assert "错误" in out or "过大" in out
        assert cost < 1.0, f"静态守卫应在 1s 内拒绝，实测 {cost:.2f}s"

    @pytest.mark.asyncio
    async def test_small_pow_still_works(self):
        from agentcore.skills.basic_tools import evaluate

        assert "1024" in await evaluate("pow(2, 10)")


# ---------------------------------------------------------------- H4 超时判据
class TestUncertainSendError:
    def test_httpx_timeouts_are_uncertain(self):
        from agentcore.skills.file_sender import is_uncertain_send_error

        for exc in (
            httpx.ReadTimeout(""),
            httpx.ConnectTimeout(""),
            httpx.WriteTimeout(""),
            httpx.PoolTimeout(""),
        ):
            assert is_uncertain_send_error(exc), type(exc).__name__

    def test_plain_timeout_still_uncertain(self):
        from agentcore.skills.file_sender import is_uncertain_send_error

        assert is_uncertain_send_error(TimeoutError("x"))
        assert is_uncertain_send_error(TimeoutError())

    def test_non_timeout_not_uncertain(self):
        from agentcore.skills.file_sender import is_uncertain_send_error

        assert not is_uncertain_send_error(ValueError("bad arg"))


# ---------------------------------------------------------------- H5 停机顺序
class _Recorder:
    def __init__(self, name, order, fail=False):
        self.name = name
        self.order = order
        self.fail = fail

    def shutdown(self, wait=False):
        self.order.append(f"{self.name}:stop")
        if self.fail:
            raise RuntimeError(f"{self.name} stop failed")

    async def flush_all(self):
        self.order.append(f"{self.name}:flush")
        if self.fail:
            raise RuntimeError(f"{self.name} flush failed")

    async def aclose(self):
        self.order.append(f"{self.name}:aclose")
        if self.fail:
            raise RuntimeError(f"{self.name} aclose failed")


class TestShutdownOrder:
    @pytest.mark.asyncio
    async def test_flush_before_aclose(self):
        from plugins.qq_agent_adapter.lifecycle import shutdown_agent

        order: list[str] = []
        await shutdown_agent(
            debouncer=_Recorder("deb", order),
            memory=_Recorder("mem", order),
            scheduler=_Recorder("sched", order),
        )
        assert order == ["sched:stop", "deb:flush", "mem:aclose"]

    @pytest.mark.asyncio
    async def test_later_steps_survive_earlier_failure(self):
        from plugins.qq_agent_adapter.lifecycle import shutdown_agent

        order: list[str] = []
        closed: list[str] = []

        async def closer():
            closed.append("extra")

        await shutdown_agent(
            debouncer=_Recorder("deb", order, fail=True),  # flush 抛错
            memory=_Recorder("mem", order, fail=True),  # aclose 也抛错
            scheduler=_Recorder("sched", order, fail=True),
            extra_closers=(closer,),
        )
        assert order == ["sched:stop", "deb:flush", "mem:aclose"]
        assert closed == ["extra"], "单步失败不得阻断后续收尾"

    @pytest.mark.asyncio
    async def test_idempotent_second_call(self):
        from plugins.qq_agent_adapter.lifecycle import shutdown_agent

        order: list[str] = []
        deb, mem = _Recorder("deb", order), _Recorder("mem", order)
        await shutdown_agent(debouncer=deb, memory=mem)
        await shutdown_agent(debouncer=deb, memory=mem)
        assert order == ["deb:flush", "mem:aclose", "deb:flush", "mem:aclose"]

    @pytest.mark.asyncio
    async def test_none_objects_are_noop(self):
        from plugins.qq_agent_adapter.lifecycle import shutdown_agent

        await shutdown_agent()  # 不应抛错
