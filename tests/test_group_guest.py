"""where 轴（AC-group-guest-mode D1）与访客面守卫（P1/P4 批）。

判据：``SkillRegistry._is_allowed`` 的 sessions 轴——superuser-only 技能注册
时**自动**视为 ``private``（群聊不可见），``AGENT_GROUP_EXTRA_SKILLS`` 显式
加开（只对 private 生效，典型用途：群内只读运维类）。拒绝文案与 unknown
同款（无探测口），execute 拒绝路径留 WARNING（含会话键）。

访客准入四标准（写进 AGENTS §4）：确定性或有界 / 按 user 隔离 / 无持久化
副作用（reminder 除外——按 user 隔离+条数上限）/ 输出过围栏脱敏。
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

from agentcore.skills.registry import SkillRegistry, _group_extra_skills

ROOT = Path(__file__).resolve().parents[1]


async def _handler(**kw):
    return "ok"


class _AllowAllChecker:
    """放行一切（隔离 where 轴本身的判定，避免被权限级别干扰）。"""

    def is_allowed(self, name, user_id, group_id, skill_permission="public"):
        return True


@pytest.fixture()
def registry():
    r = SkillRegistry(_AllowAllChecker())
    r.register("public_tool", "公开技能", {})(_handler)
    r.register("ops_tool", "运维技能", {}, permission="superuser")(_handler)
    return r


class TestWhereAxis:
    def test_a1_group_denies_superuser_skill(self, registry):
        """A1：群聊里 superuser 技能从 schema 消失、execute 拒绝（unknown 同文案）。"""
        names = [s["function"]["name"] for s in registry.get_schemas("1", "999")]
        assert "ops_tool" not in names and "public_tool" in names

    @pytest.mark.asyncio
    async def test_a1_execute_same_error_as_unknown(self, registry):
        denied = await registry.execute("ops_tool", user_id="1", group_id="999")
        unknown = await registry.execute("no_such_tool", user_id="1", group_id="999")
        # 无探测口：两者都是同一模板（只有名字不同），拒绝不给任何额外信息
        assert denied == "Error: unknown skill ops_tool"
        assert unknown == "Error: unknown skill no_such_tool"

    def test_a2_group_guest_public_skill_still_works(self, registry):
        names = [s["function"]["name"] for s in registry.get_schemas("guest", "999")]
        assert "public_tool" in names

    @pytest.mark.asyncio
    async def test_a3_private_superuser_skill_works(self, registry):
        assert "ops_tool" in [
            s["function"]["name"] for s in registry.get_schemas("1", None)
        ]
        r = await registry.execute("ops_tool", user_id="1", group_id=None)
        assert r == "ok"

    def test_a4_extra_skills_env_opens_group(self, registry, monkeypatch):
        monkeypatch.setenv("AGENT_GROUP_EXTRA_SKILLS", "ops_tool")
        names = [s["function"]["name"] for s in registry.get_schemas("1", "999")]
        assert "ops_tool" in names
        # 白名单只加开 private——不会改变 public 技能的判定
        assert "public_tool" in names

    @pytest.mark.asyncio
    async def test_a5_deny_logs_warning_with_session_key(self, registry, caplog):
        with caplog.at_level(logging.WARNING):
            await registry.execute("ops_tool", user_id="1", group_id="999")
        assert "where axis" in caplog.text
        assert "group=999" in caplog.text

    def test_default_sessions_derivation(self):
        """缺省推导：superuser → private，其余 → all；显式传值覆盖推导。"""
        r = SkillRegistry()
        r.register("a", "d", {})(_handler)
        r.register("b", "d", {}, permission="superuser")(_handler)
        r.register("c", "d", {}, sessions="private")(_handler)
        assert r.skills["a"].sessions == "all"
        assert r.skills["b"].sessions == "private"
        assert r.skills["c"].sessions == "private"


class TestGuestSkillDrift:
    """D1/D2：生产源码的 superuser 注册集与文档不得互相矛盾（名单漂移守卫）。

    源码侧用 AST 扫 @registry.register(...) 的字面量：permission="superuser"
    的技能名集合；文档侧解析 features-skills.md 表格中标注 superuser 的名字。
    双向 diff 为零——漏标会让读者以为某高危技能群内可用，误标则相反。
    """

    _DOC = ROOT / "docs" / "features-skills.md"

    @staticmethod
    def _ast_superuser_skills() -> set[str]:
        found: set[str] = set()
        for path in list((ROOT / "agentcore").rglob("*.py")) + list(
            (ROOT / "plugins").rglob("*.py")
        ):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Attribute) and fn.attr == "register"):
                    continue
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                name = node.args[0].value
                if not isinstance(name, str):
                    continue
                # 位置实参形态：register(name, desc, schema, permission)——
                # 线1-L1（REVIEW-63f9444..858afcc）：只认 keyword 会漏掉位置
                # 传 permission 的注册（非字面量形态仍不识别，见方法 docstring）
                if (
                    len(node.args) >= 4
                    and isinstance(node.args[3], ast.Constant)
                    and node.args[3].value == "superuser"
                ):
                    found.add(name)
                for kw in node.keywords:
                    if kw.arg == "permission" and isinstance(kw.value, ast.Constant):
                        if kw.value.value == "superuser":
                            found.add(name)
        return found

    @staticmethod
    def _doc_superuser_lines() -> list[str]:
        text = (ROOT / "docs" / "features-skills.md").read_text(encoding="utf-8")
        return [
            line
            for line in text.splitlines()
            if "superuser" in line.lower()
            and (line.startswith("|") or line.startswith("- "))
        ]

    @classmethod
    def _doc_superuser_names_loose(cls) -> set[str]:
        """正向用：标注行里出现的**全部** backtick 词（名字可在任意列/组写法）。"""
        names: set[str] = set()
        for line in cls._doc_superuser_lines():
            for chunk in re.findall(r"`([^`]+)`", line):
                for part in chunk.split("/"):
                    part = part.strip()
                    if re.fullmatch(r"[a-z][\w-]*", part):
                        names.add(part)
        return names

    @classmethod
    def _doc_superuser_names_strict(cls) -> set[str]:
        """反向用：每行**第一个** backtick 词（名字位；防文档整行写错名/虚报）。"""
        names: set[str] = set()
        for line in cls._doc_superuser_lines():
            # 混合权限行（如「`a` / `b` | public / **superuser**」）无法按行
            # 唯一归属——只参与正向 loose 判定，不参与严格反向校验
            if re.search(r"\bpublic\b", line, re.I):
                continue
            chunks = re.findall(r"`([^`]+)`", line)
            if not chunks:
                continue
            first = chunks[0].split("/")[0].strip()
            if re.fullmatch(r"[a-z][\w-]*", first):
                names.add(first)
        return names

    def test_drift_between_source_and_docs(self):
        src = self._ast_superuser_skills()
        doc = self._doc_superuser_names_loose()
        # 文档必须覆盖源码注册的每一个 superuser 技能（漏标 = 读者被误导）
        missing_doc = src - doc
        assert not missing_doc, (
            f"这些 superuser 技能未在 docs/features-skills.md 标注：{sorted(missing_doc)}"
        )
        # 文档标注的 superuser 必须在源码注册（防文档虚报/改名未同步；
        # 用严格提取——每行首个 backtick 词，避免描述里的参数名噪声）
        missing_src = self._doc_superuser_names_strict() - src
        assert not missing_src, (
            f"文档标注 superuser 但源码无此注册（改名/删除未同步？）：{sorted(missing_src)}"
        )

    def test_extra_skills_names_exist_in_source(self):
        """AGENT_GROUP_EXTRA_SKILLS 的文档示例必须是真实注册名（防白名单错字）。"""
        # 该 env 的清单语义由 registry._group_extra_skills 解析；这里只验证
        # 文档里的示例名在源码存在（含 .env.example 的注释示例）
        env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
        mentioned = re.findall(r"AGENT_GROUP_EXTRA_SKILLS[^\n]*", env_text)
        if not mentioned:
            pytest.skip("AGENT_GROUP_EXTRA_SKILLS 尚无文档示例")
        parsed = set()
        for line in mentioned:
            body = line.split("=", 1)[-1].split("#", 1)[0]
            parsed.update(x.strip() for x in body.split(",") if x.strip())
        assert parsed, "示例行应按 csv 列出技能名"
        src_text = "\n".join(
            p.read_text(encoding="utf-8") for p in (ROOT / "agentcore").rglob("*.py")
        )
        for name in parsed:
            assert f'"{name}"' in src_text, f"白名单示例含未注册技能：{name}"

    def test_group_extra_skills_empty_by_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_GROUP_EXTRA_SKILLS", raising=False)
        assert _group_extra_skills() == set()
        monkeypatch.setenv("AGENT_GROUP_EXTRA_SKILLS", " a, b ,,a")
        assert _group_extra_skills() == {"a", "b"}


class TestSecretPathGuards:
    """敏感路径必须被 gitignore（H3 的 main 变体 + cgroup/personas 新目录）。"""

    REQUIRED_IGNORES = (
        "data/2fa-secret.json",
        "data/llm-providers.json",
        "data/search-providers.json",
        "data/nudge.json",
        "data/cron/",
        "data/personas/",
        "data/state/",
        "data/mirror/",
        "data/ssh/",
    )

    def test_gitignore_covers_secret_paths(self):
        import subprocess

        for path in self.REQUIRED_IGNORES:
            r = subprocess.run(["git", "check-ignore", "-q", path], cwd=str(ROOT))
            assert r.returncode == 0, f"{path} 未被 .gitignore 覆盖（含凭据/个人内容）"

    def test_no_secret_files_tracked(self):
        import subprocess

        out = subprocess.run(
            ["git", "ls-files", "data/cron", "data/personas", "data/state"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert out == "", f"敏感目录有文件入库：{out}"
