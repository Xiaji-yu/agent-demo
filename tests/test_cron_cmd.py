"""cron QQ 命令通道与 /openclaw 导入（P2b）。

命令解析与动作执行（run_cron_action/parse_cron_cmd）是纯函数，与 nonebot
matcher 解耦——matcher 侧只做 ACL 与文本搬运。superuser+私聊的 ACL 由
matcher 分支保证（is_superuser_id + group_id 检查），这里的单测聚焦业务。
"""

from __future__ import annotations

import json

from agentcore.scheduler.cron_admin import (
    _apply_add,
    import_openclaw,
    parse_cron_cmd,
    run_cron_action,
)

_JOB_JSON = json.dumps(
    {
        "id": "sample",
        "name": "示例任务",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"},
        "payload": {
            "kind": "agent_turn",
            "message": "早安报告",
            "channel": "onebot",
            "to": "group:123456789",
        },
    }
)


class TestParse:
    def test_actions(self):
        assert parse_cron_cmd("")[0] == "list"
        assert parse_cron_cmd("list")[0] == "list"
        assert parse_cron_cmd("add {}") == ("add", "{}")
        assert parse_cron_cmd("del  j1 ") == ("del", "j1")
        assert parse_cron_cmd("on j1") == ("on", "j1")
        assert parse_cron_cmd("off j1") == ("off", "j1")
        assert parse_cron_cmd("run j1") == ("run", "j1")
        assert parse_cron_cmd("confirm tok1") == ("confirm", "tok1")
        # 未知动词回落 list（防注入面最小的宽容口径：只读）
        assert parse_cron_cmd("随便写点什么")[0] == "list"


class TestCronActions:
    def _seed(self, tmp_path):
        p = tmp_path / "jobs.json"
        from agentcore.scheduler import jobs as jobs_mod

        jobs, _ = jobs_mod.parse_jobs({"jobs": []})
        jobs_mod.save(jobs, p)
        return p

    def _seed_with_sample(self, tmp_path, *, enabled=True):
        p = self._seed(tmp_path)
        from agentcore.scheduler import jobs as jobs_mod

        parsed = jobs_mod.parse_jobs({"jobs": [json.loads(_JOB_JSON)]})[0]
        job = parsed[0]
        job.enabled = enabled
        jobs_mod.save([job], p)
        return p

    def test_add_two_phase_then_enable(self, tmp_path):
        p = self._seed(tmp_path)
        # 第一段：add 只进预览 + confirm token，**不落盘**（两段确认防手滑）
        reply = run_cron_action("add", _JOB_JSON, jobs_path=p)
        assert "确认落盘" in reply and "停用状态" in reply
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["jobs"] == [], "未 confirm 不得落盘"
        # 第二段：confirm 走 _apply_add → 停用状态入库
        from agentcore.scheduler import jobs as jobs_mod

        parsed = jobs_mod.parse_jobs({"jobs": [json.loads(_JOB_JSON)]})[0]
        job = parsed[0]
        ok, msg = _apply_add(job, jobs_path=p)
        assert ok is True, msg
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["jobs"][0]["enabled"] is False, "新任务必须先停用（试跑后再开）"
        run_cron_action("on", "sample", jobs_path=p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["jobs"][0]["enabled"] is True
        assert "已启用" in run_cron_action("on", "sample", jobs_path=p)

    def test_del(self, tmp_path):
        p = self._seed_with_sample(tmp_path)
        reply = run_cron_action("del", "sample", jobs_path=p)
        assert "已删除" in reply
        assert (
            run_cron_action("del", "sample", jobs_path=p)
            == "没有 id 为 sample 的任务。"
        )

    def test_off(self, tmp_path):
        p = self._seed_with_sample(tmp_path, enabled=True)
        assert "已停用" in run_cron_action("off", "sample", jobs_path=p)

    def test_apply_add_rejects_duplicate(self, tmp_path):
        p = self._seed(tmp_path)
        from agentcore.scheduler import jobs as jobs_mod

        parsed = jobs_mod.parse_jobs({"jobs": [json.loads(_JOB_JSON)]})[0]
        job = parsed[0]
        ok, msg = _apply_add(job, jobs_path=p)
        assert ok is True, msg
        ok2, msg2 = _apply_add(job, jobs_path=p)
        assert ok2 is False and "id 已存在" in msg2

    def test_add_bad_json_message(self, tmp_path):
        self._seed(tmp_path)
        reply = run_cron_action("add", "{不是json", jobs_path=tmp_path / "jobs.json")
        assert "解析失败" in reply


class TestOpenclawImport:
    def _make_openclaw(self, root, *, soul=True, jobs=True, memory=True):
        root.mkdir(parents=True, exist_ok=True)
        if soul:
            (root / "SOUL.md").write_text(
                "name: soul_persona\n你是一个从 OpenClaw 迁来的助手。\n",
                encoding="utf-8",
            )
        if jobs:
            (root / "cron").mkdir(exist_ok=True)
            (root / "cron" / "jobs.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "jobs": [
                            {
                                "id": "oc-job",
                                "name": "OpenClaw 任务",
                                "enabled": False,
                                "schedule": {"kind": "cron", "expr": "0 12 * * *"},
                                "payload": {
                                    "kind": "agent_turn",
                                    "message": "午报",
                                    "channel": "onebot",
                                    "to": "group:123456789",
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        if memory:
            (root / "memory").mkdir(exist_ok=True)
            (root / "memory" / "2026.md").write_text("记忆内容", encoding="utf-8")

    def test_import_soul_and_cron(self, tmp_path):
        oc = tmp_path / "openclaw"
        self._make_openclaw(oc)
        cron_jobs = tmp_path / "jobs.json"
        from agentcore.scheduler import jobs as jobs_mod

        jobs_mod.save([], cron_jobs)
        report = import_openclaw(
            str(oc), persona_manager=_StubPM(tmp_path / "personas"), jobs_path=cron_jobs
        )
        assert "SOUL.md → soul_persona" in report
        assert "导入 1 条" in report
        # 人格落到了 data/personas 风格的目录（stub 的 tmp_path/personas）
        assert (tmp_path / "personas" / "soul_persona.md").exists()
        # cron 合并成功且为纯配置
        data = json.loads(cron_jobs.read_text(encoding="utf-8"))
        assert data["jobs"][0]["id"] == "oc-job"
        assert "state" not in data["jobs"][0]
        assert "memory" in report  # 明确提示不支持，不静默吞

    def test_import_never_overwrites_existing(self, tmp_path):
        oc = tmp_path / "openclaw"
        self._make_openclaw(oc)
        # 现网已有同名人格与同 id 任务
        pm = _StubPM(tmp_path / "personas")
        pm.save("soul_persona", body="现网现有人格，不能被覆盖", default=False)
        cron_jobs = tmp_path / "jobs.json"
        from agentcore.scheduler import jobs as jobs_mod

        jobs, _ = jobs_mod.parse_jobs(
            {
                "jobs": [
                    {
                        "id": "oc-job",
                        "name": "现网任务",
                        "enabled": True,
                        "schedule": {"kind": "cron", "expr": "0 8 * * *"},
                        "payload": {
                            "kind": "agent_turn",
                            "message": "现网",
                            "channel": "onebot",
                            "to": "group:123456789",
                        },
                    }
                ]
            }
        )
        jobs_mod.save(jobs, cron_jobs)
        before = (tmp_path / "personas" / "soul_persona.md").read_text(encoding="utf-8")
        report = import_openclaw(str(oc), persona_manager=pm, jobs_path=cron_jobs)
        assert "已存在，跳过" in report
        after = (tmp_path / "personas" / "soul_persona.md").read_text(encoding="utf-8")
        assert before == after, "已存在的人格不得被覆盖"
        data = json.loads(cron_jobs.read_text(encoding="utf-8"))
        assert data["jobs"][0]["name"] == "现网任务", "同 id 任务不得被覆盖"

    def test_import_missing_dir(self, tmp_path):
        report = import_openclaw(
            str(tmp_path / "nope"),
            persona_manager=None,
            jobs_path=tmp_path / "jobs.json",
        )
        assert "目录不存在" in report

    def test_import_chinese_persona_name_is_folded_not_crash(self, tmp_path):
        """M4（REVIEW-63f9444..858afcc）：中文人格名不再让导入崩溃。

        旧实现把 frontmatter 原名直接交给 pm.save——PersonaManager 的
        ^[A-Za-z0-9_-]+$ 校验抛 ValueError 且无人捕获：命令崩溃、无回复、
        无审计（已复现 ValueError: invalid persona name: '夏祭的助手'）。
        """
        oc = tmp_path / "openclaw"
        oc.mkdir()
        (oc / "SOUL.md").write_text(
            "name: 夏祭的助手\n你是一个从 OpenClaw 迁来的助手。\n", encoding="utf-8"
        )
        report = import_openclaw(
            str(oc), persona_manager=_StubPM(tmp_path / "personas")
        )
        assert "已折叠" in report
        assert (tmp_path / "personas" / "soul.md").exists(), (
            "纯非法人格名折叠后回落 soul"
        )

    def test_imported_jobs_are_disabled(self, tmp_path):
        """M8（REVIEW-63f9444..858afcc）：导入条目一律先停用。

        旧实现原样保留 enabled=true：无 state 即 next=0，下个 tick（≤60s）
        立即向目标投递 LLM 产物，无人审核——与 _apply_add「先停用试跑再
        启用」的姿态不一致。
        """
        oc = tmp_path / "openclaw"
        self._make_openclaw(oc)
        src = oc / "cron" / "jobs.json"
        payload = json.loads(src.read_text(encoding="utf-8"))
        payload["jobs"][0]["enabled"] = True
        src.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        cron_jobs = tmp_path / "jobs.json"
        from agentcore.scheduler import jobs as jobs_mod

        jobs_mod.save([], cron_jobs)
        report = import_openclaw(str(oc), jobs_path=cron_jobs)
        assert "均已停用" in report
        data = json.loads(cron_jobs.read_text(encoding="utf-8"))
        assert data["jobs"][0]["enabled"] is False, "导入条目必须先停用"


class _StubPM:
    """PersonaManager 的最小替身：get/save 落 tmp 目录（导入测试不依赖包内模板）。"""

    def __init__(self, dir_path):
        self.dir_path = dir_path
        self.dir = dir_path
        self.dir.mkdir(parents=True, exist_ok=True)

    def get(self, name):
        f = self.dir_path / f"{name}.md"
        return None if not f.exists() else {"name": name}

    def save(self, name, description="", body="", default=False):
        (self.dir_path / f"{name}.md").write_text(
            f"---\nname: {name}\n---\n{body}", encoding="utf-8"
        )
