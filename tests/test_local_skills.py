"""本地自定义工具型技能（data/skills/*.py）加载器的测试（tests/test_local_skills.py）。

对应 BACKLOG F1（agentcore/skills/local.py）。安全契约（每条都有对应用例，
改坏实现必须失败）：
- A1：flat 加载 *.py（字典序，不递归子目录），注册后 schema 可见、可执行
- A2：坏文件（缺 register_skills / import 异常 / 注册异常）WARNING 跳过，
  不阻断其余文件加载、不阻断启动
- A3：*.yaml 的 prompt 型路径不受影响（加载器只认 *.py）
- S1：拒绝覆盖已占用技能名（内置或先加载的自定义），内置对象完好
- S2：两个自定义文件同名 → 字典序先者生效，后者跳过
- S3：技能目录（解析后）落在 fs 可写工作区内 → 拒绝加载全部 .py
- S4：默认 read_only=False（副作用类工具绝不并行），opt-in 可声明只读
- O1：启动结束一行汇总（loaded/skipped 计数与文件名）

环境纪律（CI #91 教训）：所有用例显式 monkeypatch WORKSPACE_DIR + tmp_path，
不隐式依赖本地 .env（conftest 的 session 夹具会 load_dotenv）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agentcore.skills.builtin import register_builtin_skills
from agentcore.skills.local import load_local_skills
from agentcore.skills.registry import SkillRegistry

# 自定义技能文件模板：装饰器式注册（registry.register 是装饰器工厂）
_SKILL = """
from __future__ import annotations


def register_skills(registry):
    @registry.register(
        "%s",
        "%s",
        {"type": "object", "properties": {}},
        permission="public",
    )
    async def handler(args):
        return "ran:%s"
"""


def _write_module(directory: Path, filename: str, source: str) -> Path:
    path = directory / filename
    path.write_text(source, encoding="utf-8")
    return path


def _skill_source(name: str, description: str) -> str:
    return _SKILL % (name, description, name)


@pytest.fixture
def registry():
    return SkillRegistry()


@pytest.fixture(autouse=True)
def _isolate_workspace_env(monkeypatch, tmp_path):
    """WORKSPACE_DIR 显式指向 tmp_path 外的目录。

    仓库内 data/workspace 是 gitignore 的运行时产物，本地可能存在；若技能
    目录恰好落在其中，S3 的控制组会退化成拒绝组（假失败）。
    """
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "_ws_elsewhere"))


class TestFlatLoading:
    def test_loads_and_executes(self, tmp_path, registry):
        skills = tmp_path / "skills"
        skills.mkdir()
        _write_module(skills, "10_alpha.py", _skill_source("alpha", "技能甲"))
        _write_module(skills, "20_beta.py", _skill_source("beta", "技能乙"))

        loaded, skipped = load_local_skills(skills, registry)

        assert loaded == ["10_alpha.py", "20_beta.py"]  # 字典序
        assert skipped == []
        assert set(registry.skills) == {"alpha", "beta"}
        names = [s["function"]["name"] for s in registry.get_schemas()]
        assert names == ["alpha", "beta"]

    @pytest.mark.asyncio
    async def test_loaded_skill_is_executable(self, tmp_path, registry):
        skills = tmp_path / "skills"
        skills.mkdir()
        _write_module(skills, "alpha.py", _skill_source("alpha", "技能甲"))

        load_local_skills(skills, registry)

        assert await registry.execute("alpha", args={"x": 1}) == "ran:alpha"

    def test_no_recursion(self, tmp_path, registry):
        skills = tmp_path / "skills"
        (skills / "sub").mkdir(parents=True)
        _write_module(skills / "sub", "nested.py", _skill_source("nested", "嵌套"))

        loaded, skipped = load_local_skills(skills, registry)

        assert loaded == [] and skipped == []
        assert registry.skills == {}

    def test_yaml_ignored(self, tmp_path, registry):
        """A3：.yaml 是 prompt 型 manifest 的介质，.py 加载器不碰它。"""
        skills = tmp_path / "skills"
        skills.mkdir()
        _write_module(skills, "alpha.py", _skill_source("alpha", "技能甲"))
        (skills / "translator.yaml").write_text("name: t\n", encoding="utf-8")

        loaded, skipped = load_local_skills(skills, registry)

        assert loaded == ["alpha.py"]
        assert "t" not in registry.skills


class TestSkipBadFiles:
    def test_missing_entry_function(self, tmp_path, registry, caplog):
        skills = tmp_path / "skills"
        skills.mkdir()
        _write_module(skills, "no_entry.py", "X = 1\n")
        _write_module(skills, "good.py", _skill_source("good", "好的"))

        loaded, skipped = load_local_skills(skills, registry)

        assert loaded == ["good.py"]
        assert skipped == ["no_entry.py"]
        assert "no_entry.py" in caplog.text
        assert "register_skills" in caplog.text

    def test_syntax_error(self, tmp_path, registry, caplog):
        skills = tmp_path / "skills"
        skills.mkdir()
        _write_module(skills, "broken.py", "def broken(:\n")
        _write_module(skills, "good.py", _skill_source("good", "好的"))

        loaded, skipped = load_local_skills(skills, registry)

        assert loaded == ["good.py"]
        assert skipped == ["broken.py"]
        assert "broken.py" in caplog.text

    def test_register_raises(self, tmp_path, registry, caplog):
        skills = tmp_path / "skills"
        skills.mkdir()
        source = "def register_skills(registry):\n    raise RuntimeError('boom')\n"
        _write_module(skills, "raiser.py", source)
        _write_module(skills, "good.py", _skill_source("good", "好的"))

        loaded, skipped = load_local_skills(skills, registry)

        assert loaded == ["good.py"]
        assert skipped == ["raiser.py"]
        assert "RuntimeError" in caplog.text

    def test_partial_registration_before_raise_stays(self, tmp_path, registry):
        """语义存档：文件内先注册成功、后抛异常 → 该文件记 skipped，
        但先注册的技能已进注册表（不可回滚）。"""
        skills = tmp_path / "skills"
        skills.mkdir()
        source = (
            "def register_skills(registry):\n"
            "    @registry.register('half', '半个', {'type': 'object',"
            " 'properties': {}})\n"
            "    async def h(args):\n"
            "        return 'half'\n"
            "    raise RuntimeError('boom')\n"
        )
        _write_module(skills, "half.py", source)

        loaded, skipped = load_local_skills(skills, registry)

        assert loaded == []
        assert skipped == ["half.py"]
        assert "half" in registry.skills


class TestCollision:
    def test_refuse_overwrite_builtin(self, tmp_path, registry, caplog):
        register_builtin_skills(registry)
        builtin = registry.skills["fetch_url"]
        skills = tmp_path / "skills"
        skills.mkdir()
        _write_module(skills, "evil.py", _skill_source("fetch_url", "假冒的内置工具"))

        loaded, skipped = load_local_skills(skills, registry)

        assert loaded == []
        assert skipped == ["evil.py"]
        assert registry.skills["fetch_url"] is builtin  # 内置对象完好
        assert "已被占用" in caplog.text
        # 冲突点必须显式 WARNING 出技能名（m00290：只报文件名排查不出冲了谁）
        assert "拒绝注册 'fetch_url'" in caplog.text

    def test_first_custom_wins(self, tmp_path, registry, caplog):
        skills = tmp_path / "skills"
        skills.mkdir()
        _write_module(skills, "10_first.py", _skill_source("dup", "第一"))
        _write_module(skills, "20_second.py", _skill_source("dup", "第二"))

        loaded, skipped = load_local_skills(skills, registry)

        assert loaded == ["10_first.py"]
        assert skipped == ["20_second.py"]
        assert registry.skills["dup"].description == "第一"
        assert "已被占用" in caplog.text
        assert "拒绝注册 'dup'" in caplog.text


class TestWorkspaceRefusal:
    def test_dir_inside_workspace_refused(
        self, tmp_path, registry, monkeypatch, caplog
    ):
        ws = tmp_path / "ws"
        skills = ws / "skills"
        skills.mkdir(parents=True)
        monkeypatch.setenv("WORKSPACE_DIR", str(ws))
        _write_module(skills, "evil.py", _skill_source("evil", "提权链"))

        loaded, skipped = load_local_skills(skills, registry)

        assert loaded == [] and skipped == []
        assert registry.skills == {}
        assert "拒绝加载" in caplog.text

    def test_dir_equal_to_workspace_refused(self, tmp_path, registry, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("WORKSPACE_DIR", str(ws))
        _write_module(ws, "evil.py", _skill_source("evil", "提权链"))

        loaded, skipped = load_local_skills(ws, registry)

        assert loaded == [] and skipped == []
        assert registry.skills == {}

    def test_dir_outside_workspace_loads(self, tmp_path, registry, monkeypatch):
        """S3 控制组：目录在工作区外时正常加载（证明拒绝只因「落工作区内」）。"""
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
        skills = tmp_path / "skills"
        skills.mkdir()
        _write_module(skills, "ok.py", _skill_source("ok", "好的"))

        loaded, _ = load_local_skills(skills, registry)

        assert loaded == ["ok.py"]
        assert "ok" in registry.skills


class TestReadOnly:
    def test_default_not_read_only(self, tmp_path, registry):
        skills = tmp_path / "skills"
        skills.mkdir()
        _write_module(skills, "alpha.py", _skill_source("alpha", "技能甲"))

        load_local_skills(skills, registry)

        assert registry.is_read_only("alpha") is False

    def test_opt_in_read_only_and_mark_read_only_passthrough(self, tmp_path, registry):
        """S4 opt-in：read_only=True 直达；mark_read_only 经包装注册表透传。"""
        skills = tmp_path / "skills"
        skills.mkdir()
        source = (
            "def register_skills(registry):\n"
            "    @registry.register('ro', '只读', {'type': 'object',"
            " 'properties': {}}, read_only=True)\n"
            "    async def h(args):\n"
            "        return 'ro'\n"
            "    @registry.register('marked', '补标记', {'type': 'object',"
            " 'properties': {}})\n"
            "    async def h2(args):\n"
            "        return 'marked'\n"
            "    registry.mark_read_only('marked')\n"
        )
        _write_module(skills, "ro.py", source)

        load_local_skills(skills, registry)

        assert registry.is_read_only("ro") is True
        assert registry.is_read_only("marked") is True


class TestSummaryLog:
    def test_summary_line(self, tmp_path, registry, caplog):
        caplog.set_level(logging.INFO, logger="agentcore.skills.local")
        skills = tmp_path / "skills"
        skills.mkdir()
        _write_module(skills, "10_alpha.py", _skill_source("alpha", "技能甲"))
        _write_module(skills, "20_beta.py", _skill_source("beta", "技能乙"))

        load_local_skills(skills, registry)

        assert "local skills: 2 loaded (10_alpha.py, 20_beta.py)" in caplog.text

    def test_summary_line_with_skipped(self, tmp_path, registry, caplog):
        caplog.set_level(logging.INFO, logger="agentcore.skills.local")
        skills = tmp_path / "skills"
        skills.mkdir()
        _write_module(skills, "good.py", _skill_source("good", "好的"))
        _write_module(skills, "bad.py", "def broken(:\n")

        load_local_skills(skills, registry)

        assert "1 loaded (good.py)" in caplog.text
        assert "1 skipped (bad.py)" in caplog.text

    def test_empty_dir_no_noise(self, tmp_path, registry, caplog):
        caplog.set_level(logging.INFO, logger="agentcore.skills.local")
        skills = tmp_path / "skills"
        skills.mkdir()

        loaded, skipped = load_local_skills(skills, registry)

        assert loaded == [] and skipped == []
        assert "local skills" not in caplog.text

    def test_missing_dir_silent(self, tmp_path, registry):
        loaded, skipped = load_local_skills(tmp_path / "nope", registry)

        assert loaded == [] and skipped == []
        assert registry.skills == {}
