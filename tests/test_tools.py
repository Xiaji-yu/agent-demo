"""工具集扩充后的测试：安全加固 / 实用类 / 运维类 / 提醒。

重点覆盖「不该发生的事」：SSRF 被拒、计算器逃逸被拒、日志越权被拒、
提醒解析不被模型算错（确定性）、投递失败不丢提醒。
"""
import datetime as dt
import time

import pytest

from agentcore.safety import fence_untrusted

# ---------- 安全加固：SSRF ----------


class TestFetchGuard:
    @pytest.mark.asyncio
    async def test_blocks_lan_loopback_and_metadata(self):
        from agentcore.skills.web_fetch import url_rejection_reason

        for url in (
            "http://192.168.1.2:6086/my/",       # 内网 WebDAV
            "http://127.0.0.1:8080/",
            "http://localhost:5432",
            "http://169.254.169.254/latest/meta-data/",  # 云元数据
            "http://10.0.0.5/",
            "http://172.16.3.4/",
        ):
            reason = await url_rejection_reason(url)
            assert reason, f"{url} 应被拒绝"

    @pytest.mark.asyncio
    async def test_rejects_non_http_scheme(self):
        from agentcore.skills.web_fetch import url_rejection_reason

        for url in ("file:///etc/passwd", "ftp://x/y", "gopher://x/1", "javascript:alert(1)"):
            assert await url_rejection_reason(url)

    @pytest.mark.asyncio
    async def test_public_host_allowed(self):
        from agentcore.skills.web_fetch import url_rejection_reason

        # 8.8.8.8 是公网地址（跳过 DNS）
        assert await url_rejection_reason("https://8.8.8.8/") is None

    @pytest.mark.asyncio
    async def test_proxy_range_is_configurable(self, monkeypatch):
        """透明代理 fake-IP 段默认放行；置空则严格模式。"""
        from agentcore.skills import web_fetch

        monkeypatch.setenv("AGENT_FETCH_ALLOW_RANGES", "")
        assert web_fetch._ip_is_reachable("198.18.0.11") is False
        monkeypatch.setenv("AGENT_FETCH_ALLOW_RANGES", "198.18.0.0/15")
        assert web_fetch._ip_is_reachable("198.18.0.11") is True
        # 内网始终为假
        assert web_fetch._ip_is_reachable("192.168.1.2") is False


class TestUntrustedFence:
    def test_fence_marks_untrusted(self):
        out = fence_untrusted("网页内容", "正文", "外部网站抓取")
        assert "不可信数据" in out and "不要执行" in out
        assert out.startswith("----- 网页内容开始")
        assert out.endswith("----- 网页内容结束 -----")


# ---------- 实用类 ----------


class TestCalculator:
    def test_basic_and_functions(self):
        from agentcore.skills.basic_tools import evaluate

        assert evaluate("2*(3+4)") == "14"
        assert evaluate("10%3") == "1"
        assert evaluate("2**10") == "1024"
        assert evaluate("round(3.14159,2)") == "3.14"
        assert evaluate("sqrt(16)") == "4"

    def test_escape_attempts_rejected(self):
        from agentcore.skills.basic_tools import evaluate

        for expr in (
            '__import__("os").system("id")',
            "open('/etc/passwd').read()",
            "().__class__.__bases__",
            "eval('1+1')",
            "1 if True else 2",
            "x + 1",
        ):
            assert evaluate(expr).startswith("Error:"), expr

    def test_empty_and_too_long(self):
        from agentcore.skills.basic_tools import evaluate

        assert evaluate("").startswith("Error:")
        assert evaluate("1+" * 200).startswith("Error:")


class TestDatetimeSkills:
    def test_weekday_and_between(self):
        from agentcore.skills.utility_skills import date_calc_text

        today = dt.date(2026, 9, 10)  # 周四
        assert "周四" in date_calc_text("weekday", "2026-09-10", today=today)
        assert "252" in date_calc_text("between", "2026-01-01", "2026-09-10", today=today)
        assert "09-18" in date_calc_text("add", "明天", days=7, today=today)

    def test_relative_words_and_formats(self):
        from agentcore.skills.utility_skills import parse_date

        today = dt.date(2026, 9, 10)
        assert parse_date("今天", today) == today
        assert parse_date("明天", today) == dt.date(2026, 9, 11)
        assert parse_date("昨天", today) == dt.date(2026, 9, 9)
        assert parse_date("3天后", today) == dt.date(2026, 9, 13)
        assert parse_date("9-12", today) == dt.date(2026, 9, 12)
        assert parse_date("2026年9月12日", today) == dt.date(2026, 9, 12)

    def test_bad_date_raises(self):
        from agentcore.skills.utility_skills import parse_date

        with pytest.raises(ValueError):
            parse_date("下周三")


class TestUnitConvert:
    def test_length_mass_data_temp(self):
        from agentcore.skills.utility_skills import convert

        assert "62.1371" in convert(100, "km", "mile")
        assert "1024 MB" in convert(1, "GB", "MB")
        assert "37.78" in convert(100, "F", "C")
        assert "32 F" in convert(0, "C", "F")
        assert "500 g" in convert(1, "斤", "g")
        assert "1.60934" in convert(1, "mile", "km")

    def test_cross_category_rejected(self):
        from agentcore.skills.utility_skills import convert

        assert "不是同一类单位" in convert(1, "km", "kg")
        assert "不能与其它类别混用" in convert(1, "C", "km")

    def test_unknown_unit_lists_available(self):
        from agentcore.skills.utility_skills import convert

        out = convert(1, "光年", "km")
        assert "不认识单位" in out and "km" in out


class TestRandom:
    def test_pick_respects_count_and_pool(self):
        from agentcore.skills.utility_skills import random_text

        out = random_text("pick", ["a", "b", "c"], count=2)
        assert out.startswith("抽签结果：")
        assert len([x for x in out.replace("抽签结果：", "").split("、")]) >= 2
        assert "错误" in random_text("pick", [])

    def test_number_range_and_dice_and_coin(self):
        from agentcore.skills.utility_skills import random_text

        for _ in range(20):
            out = random_text("number", low=1, high=3)
            assert int(out.split("：")[-1]) in (1, 2, 3)
        dice = random_text("dice", ["2d6"])
        assert "骰子" in dice
        assert random_text("coin") in ("抛硬币：正面", "抛硬币：反面")

    def test_bad_mode(self):
        from agentcore.skills.utility_skills import random_text

        assert "错误" in random_text("whatever")


# ---------- 运维类 ----------


class TestOpsSkills:
    @pytest.mark.asyncio
    async def test_port_check_validates_range(self):
        from agentcore.skills.ops_skills import port_check

        assert "1~65535" in await port_check(0)
        assert "整数" in await port_check("abc")

    @pytest.mark.asyncio
    async def test_port_check_reports_listening(self):
        from agentcore.skills.ops_skills import _listening_ports, port_check

        listening = _listening_ports()
        if not listening:  # 容器里可能读不到 /proc/net/tcp
            pytest.skip("读不到 /proc/net/tcp")
        port = sorted(listening)[0]
        assert "正在监听" in await port_check(port)
        assert "未在监听" in await port_check(port + 1 if port < 65535 else port - 1)

    @pytest.mark.asyncio
    async def test_log_tail_must_stay_in_allowlist(self, tmp_path, monkeypatch):
        from agentcore.skills.ops_skills import log_tail

        allowed = tmp_path / "logs"
        allowed.mkdir()
        (allowed / "app.log").write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
        monkeypatch.setenv("AGENT_LOG_ALLOWLIST", str(allowed))

        out = await log_tail(str(allowed / "app.log"), lines=5)
        assert "line 99" in out and "line 95" in out
        assert "line 94" not in out
        # 关键字过滤
        assert "line 42" in await log_tail(str(allowed / "app.log"), lines=5, keyword="line 42")

        # 越权路径
        assert "拒绝" in await log_tail("/etc/passwd")
        assert "拒绝" in await log_tail(str(tmp_path / "other.log"))
        assert "路径不合法" in await log_tail(str(allowed / ".." / "app.log"))

    @pytest.mark.asyncio
    async def test_service_name_validation(self):
        from agentcore.skills.ops_skills import service_status

        assert "非法字符" in await service_status("nginx; rm -rf /")
        assert "非法字符" in await service_status("../../etc/passwd")

    @pytest.mark.asyncio
    async def test_disk_usage_path_validation(self):
        from agentcore.skills.ops_skills import disk_usage

        assert "绝对路径" in await disk_usage("../etc")
        assert "路径不存在" in await disk_usage("/no/such/path/here")

    @pytest.mark.asyncio
    async def test_proc_detail_sort_validation(self):
        from agentcore.skills.ops_skills import proc_detail

        assert "cpu 或 mem" in await proc_detail(3, "gpu")


# ---------- 提醒：时间解析 ----------


class TestReminderParsing:
    NOW = dt.datetime(2026, 9, 10, 21, 30)  # 周四

    def _when(self, text):
        from agentcore.scheduler.reminder import parse_when

        return parse_when(text, self.NOW)

    def test_relative_offsets(self):
        assert self._when("10分钟后")["ok"]
        assert self._when("2小时后")["ok"]
        r = self._when("2小时30分钟后")
        got = dt.datetime.fromtimestamp(r["run_at"])
        assert got == self.NOW + dt.timedelta(hours=2, minutes=30), got
        assert dt.datetime.fromtimestamp(self._when("30秒后")["run_at"]) == self.NOW + dt.timedelta(seconds=30)

    def test_relative_days_with_time(self):
        r = self._when("3天后 8点")
        assert dt.datetime.fromtimestamp(r["run_at"]) == dt.datetime(2026, 9, 13, 8, 0)

    def test_tomorrow_and_today(self):
        assert dt.datetime.fromtimestamp(self._when("明天8点")["run_at"]) == dt.datetime(2026, 9, 11, 8, 0)
        assert dt.datetime.fromtimestamp(self._when("明天早上8点")["run_at"]) == dt.datetime(2026, 9, 11, 8, 0)
        assert dt.datetime.fromtimestamp(self._when("今天22点")["run_at"]) == dt.datetime(2026, 9, 10, 22, 0)
        # 明确说了今天却已过去 → 必须报错，不能偷偷改到明天
        bad = self._when("今天20:00")
        assert not bad["ok"] and "已经过去" in bad["error"]

    def test_bare_time_rolls_to_tomorrow(self):
        assert dt.datetime.fromtimestamp(self._when("9点")["run_at"]) == dt.datetime(2026, 9, 11, 9, 0)
        assert dt.datetime.fromtimestamp(self._when("21:45")["run_at"]) == dt.datetime(2026, 9, 10, 21, 45)

    def test_period_hints(self):
        assert dt.datetime.fromtimestamp(self._when("下午3点")["run_at"]).hour == 15
        assert dt.datetime.fromtimestamp(self._when("中午12点")["run_at"]).hour == 12
        assert dt.datetime.fromtimestamp(self._when("后天 7点半")["run_at"]).minute == 30

    def test_daily_and_weekly_cron(self):
        assert self._when("每天9点")["cron"] == "0 9 * * *"
        assert self._when("每天早上8点")["cron"] == "0 8 * * *"
        assert self._when("每天22点")["cron"] == "0 22 * * *"
        # apscheduler 的 day_of_week：0=周一 … 6=周日
        assert self._when("每周一 9点")["cron"] == "0 9 * * 0"
        assert self._when("每周日 10点")["cron"] == "0 10 * * 6"
        assert self._when("工作日 9点")["cron"] == "0 9 * * 1-5"

    def test_weekly_next_run_lands_on_right_weekday(self):
        """回归：星期编号差一位会把「每周一」算成周二。"""
        r = self._when("每周一 9点")
        got = dt.datetime.fromtimestamp(r["run_at"])
        assert got.weekday() == 0, got  # 0 = 周一
        assert got == dt.datetime(2026, 9, 14, 9, 0)
        r2 = self._when("每周日 10点")
        assert dt.datetime.fromtimestamp(r2["run_at"]).weekday() == 6

    def test_absolute_dates(self):
        assert dt.datetime.fromtimestamp(self._when("9月12日 9点")["run_at"]) == dt.datetime(2026, 9, 12, 9, 0)
        assert dt.datetime.fromtimestamp(self._when("2026-09-15 08:00")["run_at"]) == dt.datetime(2026, 9, 15, 8, 0)
        # 月日已过 → 顺延到明年
        assert dt.datetime.fromtimestamp(self._when("1月5日 9点")["run_at"]).year == 2027
        # 明确日期但已过去 → 报错
        bad = self._when("9月10日 20:00")
        assert not bad["ok"] and "已经过去" in bad["error"]

    def test_unparseable(self):
        assert not self._when("随便说点什么")["ok"]
        assert not self._when("")["ok"]


# ---------- 提醒：存储 + 投递 ----------


class _FakeSink:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent: list[tuple[str, str]] = []

    async def send(self, target: str, message: str) -> bool:
        self.sent.append((target, message))
        return self.ok


class TestReminderService:
    @pytest.mark.asyncio
    async def test_fires_once_and_disables(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.reminder import ReminderService

        store = InMemoryMemoryStore()
        sink = _FakeSink()
        svc = ReminderService(store, sink)
        sid = await store.schedule_add(
            kind="once", target="private:1", message="吃药", user_id="1", next_run=time.time() - 1
        )
        out = await svc.tick()
        assert out == {"due": 1, "sent": 1, "failed": 0}
        assert sink.sent and "吃药" in sink.sent[0][1]
        # 一次性提醒已停用，不会重复触发
        assert await store.schedule_list("1") == []
        assert await svc.tick() == {"due": 0, "sent": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_cron_reminder_reschedules(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.reminder import ReminderService

        store = InMemoryMemoryStore()
        sink = _FakeSink()
        svc = ReminderService(store, sink)
        await store.schedule_add(
            kind="cron", target="group:9", message="开会", user_id="1",
            cron="0 9 * * *", next_run=time.time() - 1,
        )
        await svc.tick()
        rows = await store.schedule_list("1")
        assert len(rows) == 1 and rows[0]["next_run"] > time.time()

    @pytest.mark.asyncio
    async def test_delivery_failure_retries_instead_of_dropping(self):
        """机器人没连接时不能把提醒弄丢——旧 sink 是静默 pass。"""
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.reminder import ReminderService

        store = InMemoryMemoryStore()
        sink = _FakeSink(ok=False)
        svc = ReminderService(store, sink, retry_delay=120)
        await store.schedule_add(
            kind="once", target="private:1", message="重要", user_id="1", next_run=time.time() - 1
        )
        out = await svc.tick()
        assert out == {"due": 1, "sent": 0, "failed": 1}
        rows = await store.schedule_list("1")
        assert len(rows) == 1, "失败后应保留并顺延，而不是标记完成"
        assert rows[0]["next_run"] > time.time()

    @pytest.mark.asyncio
    async def test_not_due_yet_is_untouched(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.reminder import ReminderService

        store = InMemoryMemoryStore()
        sink = _FakeSink()
        await store.schedule_add(
            kind="once", target="private:1", message="未来", user_id="1", next_run=time.time() + 3600
        )
        assert await ReminderService(store, sink).tick() == {"due": 0, "sent": 0, "failed": 0}
        assert sink.sent == []


class TestReminderSkills:
    @pytest.mark.asyncio
    async def test_add_list_cancel_flow(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.skills.registry import SkillRegistry
        from agentcore.skills.reminder_skills import register_reminder_skills

        store = InMemoryMemoryStore()
        sink = _FakeSink()
        reg = SkillRegistry()
        register_reminder_skills(reg, store, sink)

        added = await reg.execute("reminder_add", user_id="7", group_id=None,
                                 when="10分钟后", text="喝水")
        assert "已登记提醒" in added
        listed = await reg.execute("reminder_list", user_id="7")
        assert "喝水" in listed
        sid = listed.split("#")[1].split(" ")[0]
        assert "已取消" in await reg.execute("reminder_cancel", user_id="7", schedule_id=sid)
        assert "没有待触发" in await reg.execute("reminder_list", user_id="7")

    @pytest.mark.asyncio
    async def test_group_target_and_ownership(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.skills.registry import SkillRegistry
        from agentcore.skills.reminder_skills import register_reminder_skills

        store = InMemoryMemoryStore()
        reg = SkillRegistry()
        register_reminder_skills(reg, store, _FakeSink())
        await reg.execute("reminder_add", user_id="7", group_id="888",
                          when="明天9点", text="开会")
        rows = await store.schedule_list()
        assert rows[0]["target"] == "group:888"
        # 他人不能取消
        assert "没找到" in await reg.execute("reminder_cancel", user_id="999", schedule_id=rows[0]["id"])
        # 不能理解的时间要给出提示而不是瞎登记
        bad = await reg.execute("reminder_add", user_id="7", group_id=None, when="看情况", text="x")
        assert "没能理解时间" in bad
        assert len(await store.schedule_list()) == 1


class TestSkillRegistration:
    """确认新工具都真的注册进注册表（模型才看得见）。"""

    def test_new_skills_registered(self, monkeypatch):
        from agentcore.skills.builtin import register_builtin_skills
        from agentcore.skills.registry import SkillRegistry

        monkeypatch.setenv("SEARCH_API_KEY", "")
        reg = SkillRegistry()
        register_builtin_skills(reg)
        names = set(reg.skills)
        for expected in (
            "fetch_url", "calc", "get_weather", "now", "date_calc", "unit_convert", "random",
            "summarize_url", "proc_detail", "disk_usage", "port_check", "service_status", "log_tail",
        ):
            assert expected in names, expected
        # 运维类必须是非 public（仅管理员可见）
        for admin_only in ("proc_detail", "disk_usage", "port_check", "service_status", "log_tail", "run_command"):
            assert reg.skills[admin_only].permission == "superuser", admin_only

    def test_translator_manifest_is_valid(self):
        from agentcore.skills.manifest import SkillManifest

        manifest = SkillManifest.from_yaml(open("data/skills/translator.yaml", encoding="utf-8").read())
        assert manifest.name == "translator" and manifest.type == "prompt"
        assert [p["name"] for p in manifest.parameters] == ["text", "target_lang"]
