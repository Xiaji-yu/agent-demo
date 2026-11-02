"""cron 定时任务的自然语言 LLM 技能测试（cron_job_add / list / cancel）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcore.diagnostics import recent
from agentcore.scheduler import jobs as jobs_mod
from agentcore.skills.cron_skills import register_cron_skills
from agentcore.skills.permissions import PermissionChecker
from agentcore.skills.registry import SkillRegistry

_SU = "2224513919"


def _make_reg(jobs_path: Path, superusers=None):
    su = set(superusers) if superusers is not None else {_SU}
    reg = SkillRegistry(permission_checker=PermissionChecker(superusers=su))
    register_cron_skills(reg, jobs_path=jobs_path)
    return reg


class TestCronSkills:
    @pytest.mark.asyncio
    async def test_cron_job_add_natural_language_private(self, tmp_path):
        """自然语言时间（每天14:30）+ 私聊缺省 target → private:<user_id>，trusted 默认开。"""
        p = tmp_path / "jobs.json"
        reg = _make_reg(p)

        res = await reg.execute(
            "cron_job_add",
            user_id=_SU,
            group_id=None,
            when="每天下午2点半",
            task="读取 memorandum.md 发送给用户",
            name="每日备忘录",
        )
        assert "已创建并启用定时任务" in res
        assert "30 14 * * *" in res
        assert f"private:{_SU}" in res
        assert "已开启超管权限" in res

        jobs, _ = jobs_mod.load(p)
        assert len(jobs) == 1
        j = jobs[0]
        assert j.name == "每日备忘录"
        assert j.cron == "30 14 * * *"
        assert j.to == f"private:{_SU}"
        assert j.owner == _SU
        assert j.trusted is True
        assert j.enabled is True

        # 审计
        evs = [
            e
            for e in recent()
            if e.get("kind") == "cron_updated" and e.get("via") == "llm_skill"
        ]
        assert evs and evs[-1]["by"] == _SU and evs[-1]["action"] == "add"

    @pytest.mark.asyncio
    async def test_cron_job_add_group_target_and_5part_cron(self, tmp_path):
        """标准 5 段 cron 表达式 + 群聊缺省 target → group:<group_id>。"""
        p = tmp_path / "jobs.json"
        reg = _make_reg(p)

        res = await reg.execute(
            "cron_job_add",
            user_id=_SU,
            group_id="1108838060",
            when="0 9 * * 1-5",
            task="群内工作日巡检并播报",
        )
        assert "已创建并启用定时任务" in res
        assert "0 9 * * 1-5" in res
        assert "group:1108838060" in res

        jobs, _ = jobs_mod.load(p)
        assert len(jobs) == 1
        assert jobs[0].cron == "0 9 * * 1-5"
        assert jobs[0].to == "group:1108838060"
        assert jobs[0].owner == _SU

    @pytest.mark.asyncio
    async def test_cron_job_list_and_cancel_flow(self, tmp_path):
        """创建后 list 检查，再 cancel 移除。"""
        p = tmp_path / "jobs.json"
        reg = _make_reg(p)

        await reg.execute(
            "cron_job_add",
            user_id=_SU,
            when="每天9点",
            task="任务1",
            name="任务一",
        )
        await reg.execute(
            "cron_job_add",
            user_id=_SU,
            when="每天10点",
            task="任务2",
            name="任务二",
        )

        listed = await reg.execute("cron_job_list", user_id=_SU)
        assert "当前共有 2 个定时自主任务" in listed
        assert "任务一" in listed and "任务二" in listed

        jobs, _ = jobs_mod.load(p)
        first_id = jobs[0].id

        cancel_res = await reg.execute("cron_job_cancel", user_id=_SU, job_id=first_id)
        assert f"已成功删除定时任务 #{first_id}" in cancel_res

        jobs_after, _ = jobs_mod.load(p)
        assert len(jobs_after) == 1
        assert jobs_after[0].id != first_id

    @pytest.mark.asyncio
    async def test_non_superuser_denied_everywhere(self, tmp_path):
        """非超管：私聊/群聊均无 schema，execute 均返回 unknown skill。"""
        p = tmp_path / "jobs.json"
        reg = _make_reg(p)

        for gid in (None, "1108838060"):
            schemas = reg.get_schemas("999999", group_id=gid)
            names = [s["function"]["name"] for s in schemas]
            assert "cron_job_add" not in names
            assert "cron_job_list" not in names
            assert "cron_job_cancel" not in names

            res = await reg.execute(
                "cron_job_add", user_id="999999", group_id=gid, when="每天9点", task="x"
            )
            assert "unknown skill" in res

    @pytest.mark.asyncio
    async def test_superuser_visible_in_group_and_private(self, tmp_path):
        """超管：私聊和群聊均可见（sessions='all'）。"""
        p = tmp_path / "jobs.json"
        reg = _make_reg(p)

        for gid in (None, "1108838060"):
            schemas = reg.get_schemas(_SU, group_id=gid)
            names = [s["function"]["name"] for s in schemas]
            assert "cron_job_add" in names
            assert "cron_job_list" in names
            assert "cron_job_cancel" in names
