"""人格系统单测：OpenClaw 兼容的 SOUL.md 布局。

2026-10-28 改造（需求①）：从「frontmatter + name/description/default」改为
**目录隔离的 SOUL.md**——根级 ``SOUL.md`` 是默认人格，``personas/<name>/SOUL.md``
是可切换人格（目录名 = 人格名），与 OpenClaw/picoclaw 的 workspace 约定一致。
纯 markdown，无 frontmatter，以便两边文件直接互拷。

变异锚点（改坏实现必须红）：
1. 去掉 valid_persona_name 校验 → 路径穿越/非法名用例红
2. 去掉根级 SOUL.md 当默认 → default 相关用例红
3. 去掉 ABOUT.txt 读取 → 描述来源用例红
4. 去掉 save 后 refresh → 覆盖用例红
5. 去掉大小上限 → oversized 用例红
6. 去掉 _warn_once 去重 → 告警刷屏用例红
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from agentcore.memory.store import InMemoryMemoryStore
from agentcore.personas import PersonaManager
from agentcore.personas.manager import (
    _BODY_MAX,
    _DESCRIPTION_MAX,
    render_about,
    render_soul_md,
    valid_persona_name,
)
from plugins.qq_agent_adapter.persona_utils import parse_persona_cmd, persona_tokens


@pytest.fixture(autouse=True)
def _clean_persona_env(monkeypatch):
    """PERSONAS_DIR / PERSONAS_DEFAULT 会跨用例泄漏，逐个清。"""
    for key in ("PERSONAS_DIR", "PERSONAS_DEFAULT"):
        monkeypatch.delenv(key, raising=False)
    yield
    for key in ("PERSONAS_DIR", "PERSONAS_DEFAULT"):
        os.environ.pop(key, None)


def _write_soul(root: Path, name: str | None, body: str, about: str = "") -> Path:
    """写一份人格：name=None 表示根级默认人格，否则 personas/<name>/。"""
    d = root if name is None else root / "personas" / name
    d.mkdir(parents=True, exist_ok=True)
    soul = d / "SOUL.md"
    soul.write_text(body, encoding="utf-8")
    if about:
        (d / "ABOUT.txt").write_text(f"{about}\n", encoding="utf-8")
    return soul


@pytest.fixture
def personas_dir(tmp_path):
    """OpenClaw 布局：根 SOUL.md（默认）+ personas/fortune_teller/。"""
    _write_soul(tmp_path, None, "# 默认助手\n\n这是助手正文。", "默认助手")
    _write_soul(
        tmp_path, "fortune_teller", "# 玄学顾问\n\n这是玄学顾问正文。", "玄学顾问"
    )
    # 坏样本：目录名非法（含斜杠不可能，用点开头与空格）
    (tmp_path / "personas" / "bad name").mkdir(parents=True)
    (tmp_path / "personas" / "bad name" / "SOUL.md").write_text("x", encoding="utf-8")
    # 没有 SOUL.md 的目录不是人格，静默跳过
    (tmp_path / "personas" / "not_a_persona").mkdir(parents=True)
    return tmp_path


class TestSoulLayout:
    """目录布局与扫描：根级默认 + personas/<name>/ 可切换。"""

    def test_root_soul_is_default(self, personas_dir):
        pm = PersonaManager(personas_dir)
        default = pm.default()
        assert default is not None
        assert default.name == "default"
        assert default.default is True
        assert "这是助手正文。" in default.body

    def test_list_contains_both(self, personas_dir):
        pm = PersonaManager(personas_dir)
        names = [p.name for p in pm.list()]
        assert "default" in names
        assert "fortune_teller" in names
        # 非法目录名与无 SOUL.md 的目录都不算人格
        assert "bad name" not in names
        assert "not_a_persona" not in names

    def test_get_returns_full_body(self, personas_dir):
        pm = PersonaManager(personas_dir)
        p = pm.get("fortune_teller")
        assert p is not None
        assert "这是玄学顾问正文。" in p.body
        # 纯 markdown：不残留 frontmatter 痕迹
        assert not p.body.startswith("---")

    def test_get_blocks_path_traversal(self, personas_dir):
        pm = PersonaManager(personas_dir)
        for bad in ("../etc/passwd", "..", ".", "a/b", "", "   "):
            assert pm.get(bad) is None, bad

    def test_env_dir_override(self, personas_dir, monkeypatch):
        monkeypatch.setenv("PERSONAS_DIR", str(personas_dir))
        pm = PersonaManager("/nonexistent")
        assert pm.get("fortune_teller") is not None

    def test_unicode_dir_name_allowed(self, tmp_path):
        """OpenClaw 的 workspace 名不限 ASCII：中文目录名必须能用。"""
        _write_soul(tmp_path, "爱弥斯", "正文")
        pm = PersonaManager(tmp_path)
        assert pm.get("爱弥斯") is not None

    def test_description_prefers_about_txt(self, personas_dir):
        pm = PersonaManager(personas_dir)
        assert pm.get("fortune_teller").description == "玄学顾问"

    def test_description_falls_back_to_heading(self, tmp_path):
        _write_soul(tmp_path, "no_about", "# 我的标题 - Who You Are\n\n正文")
        pm = PersonaManager(tmp_path)
        assert pm.get("no_about").description == "我的标题"

    def test_oversized_soul_skipped(self, tmp_path):
        _write_soul(tmp_path, "big", "x" * (_BODY_MAX + 1))
        pm = PersonaManager(tmp_path)
        assert pm.get("big") is None

    def test_missing_dir_warns_not_crashes(self, tmp_path, caplog):
        pm = PersonaManager(tmp_path / "nope")
        with caplog.at_level(logging.WARNING, logger="agentcore.personas.manager"):
            assert pm.list() == []
        assert "not found" in caplog.text


class TestMemoryPersona:
    """存储层的用户/群人格选择（与文件格式无关，保持原有用例）。"""

    @pytest.mark.asyncio
    async def test_set_get_persona(self):
        store = InMemoryMemoryStore()
        assert await store.get_user_persona("u1") is None
        await store.set_user_persona("u1", "fortune_teller")
        assert await store.get_user_persona("u1") == "fortune_teller"
        await store.set_user_persona("u1", None)
        assert await store.get_user_persona("u1") is None


class TestPersonaCmdParse:
    """/persona 命令解析（QQ 侧交互，与文件格式无关）。"""

    def test_view_variants(self):
        for raw in [
            "/persona",
            "/personas",
            "/personas list",
            "/人格 列表",
            "persona ls",
        ]:
            assert parse_persona_cmd(raw) == ("list", None), raw

    def test_reset_variants(self):
        for raw in ["/personas reset", "/人格 清除", "persona 恢复默认"]:
            assert parse_persona_cmd(raw) == ("reset", None)

    def test_use_variants(self):
        assert parse_persona_cmd("/personas use yun") == ("use", "yun")
        assert parse_persona_cmd("/人格 使用 yun") == ("use", "yun")
        assert parse_persona_cmd("/personas yun") == ("use", "yun")

    def test_use_missing_name(self):
        assert parse_persona_cmd("/persona use") == ("use", "")

    def test_tokens_strip(self):
        assert persona_tokens("/personas  use  yun ") == ["use", "yun"]


class TestPersonaDefaultEnv:
    """PERSONAS_DEFAULT：本地覆盖默认人格。

    变异锚点：去掉 env 优先分支 → env 用例红；去掉不存在时的回退 → 回退用例红。
    """

    def test_env_overrides_root_soul(self, personas_dir, monkeypatch):
        monkeypatch.setenv("PERSONAS_DEFAULT", "fortune_teller")
        pm = PersonaManager(personas_dir)
        assert pm.default().name == "fortune_teller"

    def test_env_unknown_falls_back_to_root_soul(self, personas_dir, monkeypatch):
        monkeypatch.setenv("PERSONAS_DEFAULT", "gone")
        pm = PersonaManager(personas_dir)
        assert pm.default().name == "default"

    def test_blank_env_falls_back_to_root_soul(self, personas_dir, monkeypatch):
        for raw in ("", "   "):
            monkeypatch.setenv("PERSONAS_DEFAULT", raw)
            pm = PersonaManager(personas_dir)
            assert pm.default().name == "default"


class TestRefresh:
    def test_refresh_discovers_new_persona(self, personas_dir):
        pm = PersonaManager(personas_dir)
        assert pm.get("newbie") is None
        _write_soul(personas_dir, "newbie", "正文")
        pm.refresh()
        assert pm.get("newbie") is not None

    def test_refresh_picks_up_edited_body(self, personas_dir):
        pm = PersonaManager(personas_dir)
        assert "这是玄学顾问正文。" in pm.get("fortune_teller").body
        _write_soul(personas_dir, "fortune_teller", "改过的正文")
        pm.refresh()
        assert "改过的正文" in pm.get("fortune_teller").body


class TestScanWarningNoise:
    """L18：坏人格的告警不得每 2 秒刷一次；refresh 是显式重读，应再报一次。"""

    def _bad_dir(self, tmp_path):
        _write_soul(tmp_path, "good", "body")
        (tmp_path / "personas" / "bad name").mkdir(parents=True)
        (tmp_path / "personas" / "bad name" / "SOUL.md").write_text(
            "x", encoding="utf-8"
        )
        return tmp_path

    def test_repeated_scans_warn_once(self, tmp_path, caplog):
        """同一故障反复扫描只报一次。

        **必须把 TTL 打到 0**：``_ensure_loaded`` 在 TTL 内不重扫，默认 2s 下
        ``list()`` 六次只扫一次——那样即使去掉 ``_warn_once`` 的去重分支，
        计数也恒为 1，用例永远绿（恒真断言，2026-10-28 变异复核实测抓到）。
        """
        pm = PersonaManager(self._bad_dir(tmp_path))
        pm._ttl = 0.0  # 每次都真扫，去重分支才会被执行
        with caplog.at_level(logging.WARNING, logger="agentcore.personas.manager"):
            for _ in range(6):
                pm.list()
        assert caplog.text.count("invalid") == 1, caplog.text

    def test_refresh_reports_again(self, tmp_path, caplog):
        pm = PersonaManager(self._bad_dir(tmp_path))
        with caplog.at_level(logging.WARNING, logger="agentcore.personas.manager"):
            pm.list()
            pm.refresh()
        assert caplog.text.count("invalid") == 2, caplog.text

    def test_new_problem_still_reported(self, tmp_path, caplog):
        pm = PersonaManager(self._bad_dir(tmp_path))
        with caplog.at_level(logging.WARNING, logger="agentcore.personas.manager"):
            pm.list()
            (tmp_path / "personas" / "another bad").mkdir(parents=True)
            (tmp_path / "personas" / "another bad" / "SOUL.md").write_text(
                "x", encoding="utf-8"
            )
            pm.refresh()
        assert "bad name" in caplog.text
        assert "another bad" in caplog.text

    def test_default_env_missing_warns_once(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("PERSONAS_DEFAULT", "nope")
        pm = PersonaManager(self._bad_dir(tmp_path))
        with caplog.at_level(logging.WARNING, logger="agentcore.personas.manager"):
            for _ in range(5):
                pm.default()
        assert caplog.text.count("PERSONAS_DEFAULT") == 1, caplog.text


class TestPersonasDirEnvEdgeCases:
    def test_blank_env_falls_back_to_arg(self, personas_dir, monkeypatch):
        monkeypatch.setenv("PERSONAS_DIR", "   ")
        pm = PersonaManager(personas_dir)
        assert pm.get("fortune_teller") is not None

    def test_env_overrides_arg(self, personas_dir, monkeypatch):
        monkeypatch.setenv("PERSONAS_DIR", str(personas_dir))
        pm = PersonaManager("/definitely/not/exist")
        assert [p.name for p in pm.list()]

    def test_missing_env_dir_yields_empty_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERSONAS_DIR", str(tmp_path / "nope"))
        pm = PersonaManager(tmp_path / "nope")
        assert pm.list() == []


class TestValidPersonaName:
    """目录名要能安全拼路径：拒路径分隔符/点开头/空白，允许中文。"""

    def test_rejects_dangerous(self):
        for bad in ("../evil", "a/b", "a\\b", "", "   ", ".", "..", ".hidden", " "):
            assert not valid_persona_name(bad), bad

    def test_accepts_normal(self):
        for ok in ("aemeath", "fortune_teller", "爱弥斯", "x-1_2", "MyPersona"):
            assert valid_persona_name(ok), ok


class TestRenderSoulMd:
    """渲染函数：纯 markdown 无 frontmatter，上限校验。"""

    def test_no_frontmatter(self):
        text = render_soul_md("正文")
        assert not text.startswith("---")
        assert text == "正文\n"

    def test_strips_surrounding_blank(self):
        assert render_soul_md("\n\n  正文  \n\n") == "正文\n"

    def test_rejects_oversized(self):
        with pytest.raises(ValueError):
            render_soul_md("x" * (_BODY_MAX + 1))
        assert render_soul_md("x" * _BODY_MAX)  # 边界放行

    def test_about_max(self):
        assert render_about("x" * _DESCRIPTION_MAX)
        with pytest.raises(ValueError):
            render_about("x" * (_DESCRIPTION_MAX + 1))

    def test_about_blank_is_empty(self):
        assert render_about("   ") == ""


class TestPersonaSaveDelete:
    """save/delete：web 导入/删除人格的落盘层。

    变异锚点：去掉 valid_persona_name、去掉 refresh、去掉大小上限，都必须有用例红。
    """

    def test_save_creates_dir_layout(self, tmp_path):
        pm = PersonaManager(tmp_path)
        p = pm.save("tester", description="测试人格", body="第一行\n第二行")
        soul = tmp_path / "personas" / "tester" / "SOUL.md"
        about = tmp_path / "personas" / "tester" / "ABOUT.txt"
        assert soul.is_file() and about.is_file()
        assert p.name == "tester" and p.description == "测试人格"
        assert p.body == "第一行\n第二行" and p.default is False
        # 纯 markdown，无 frontmatter
        assert not soul.read_text(encoding="utf-8").startswith("---")
        # 0600：人格内容等同配置。**仅 POSIX 断言**——win32 的 os.open mode
        # 只保留只读位，落出来是 0666，权限位语义不成立（CI 是 Linux，走这条）。
        if os.name == "posix":
            assert soul.stat().st_mode & 0o777 == 0o600

    def test_save_without_description_has_no_about(self, tmp_path):
        pm = PersonaManager(tmp_path)
        pm.save("nodesc", body="x")
        assert not (tmp_path / "personas" / "nodesc" / "ABOUT.txt").exists()
        # 回落首个标题
        assert pm.get("nodesc").description == ""

    def test_save_clears_stale_about(self, tmp_path):
        pm = PersonaManager(tmp_path)
        pm.save("p", description="旧描述", body="# 标题\nx")
        assert pm.get("p").description == "旧描述"
        pm.save("p", description="", body="# 新标题\nx")
        assert not (tmp_path / "personas" / "p" / "ABOUT.txt").exists()
        assert pm.get("p").description == "新标题"

    def test_save_overwrites_without_manual_refresh(self, personas_dir):
        pm = PersonaManager(personas_dir)
        assert "这是玄学顾问正文。" in pm.get("fortune_teller").body
        pm.list()  # 建立扫描缓存
        pm.save("fortune_teller", body="新正文")
        assert "新正文" in pm.get("fortune_teller").body

    def test_save_rejects_invalid_name(self, tmp_path):
        pm = PersonaManager(tmp_path)
        for bad in ["../evil", "bad name", "a/b", "", "..", "default"]:
            with pytest.raises(ValueError):
                pm.save(bad, body="x")
        assert list(tmp_path.glob("*.md")) == []
        assert not (tmp_path / "personas").exists() or not any(
            (tmp_path / "personas").iterdir()
        )

    def test_save_rejects_oversized(self, tmp_path):
        pm = PersonaManager(tmp_path)
        with pytest.raises(ValueError):
            pm.save("big", body="x" * (_BODY_MAX + 1))
        pm.save("edge", body="x" * _BODY_MAX)
        with pytest.raises(ValueError):
            pm.save("wordy", description="x" * (_DESCRIPTION_MAX + 1))
        assert pm.get("edge") is not None

    def test_save_default_sets_marker_not_touching_root(self, personas_dir):
        pm = PersonaManager(personas_dir)
        pm.save("mine", body="我的人格", default=True)
        # 根级默认人格不被改动
        assert pm.default().name == "mine"
        assert (personas_dir / "SOUL.md").is_file()
        assert "这是助手正文。" in (personas_dir / "SOUL.md").read_text(
            encoding="utf-8"
        )

    def test_save_leaves_no_tmp_and_creates_dir(self, tmp_path):
        target = tmp_path / "fresh" / "deeper"
        pm = PersonaManager(target)
        pm.save("hello", body="hi")
        assert (target / "personas" / "hello" / "SOUL.md").is_file()
        assert list(target.rglob(".*tmp*")) == []

    def test_save_unicode_roundtrip(self, tmp_path):
        pm = PersonaManager(tmp_path)
        pm.save("yun", description="云端助手", body="你是温柔的知识伙伴。")
        text = (tmp_path / "personas" / "yun" / "SOUL.md").read_text(encoding="utf-8")
        assert "你是温柔的知识伙伴。" in text  # 不写 \u 转义
        assert pm.get("yun").body == "你是温柔的知识伙伴。"

    def test_delete_removes_and_reports_missing(self, personas_dir):
        pm = PersonaManager(personas_dir)
        pm.save("mine", body="x")
        assert pm.delete("mine") is True
        assert pm.get("mine") is None
        assert not (personas_dir / "personas" / "mine").exists()
        assert pm.delete("mine") is False
        assert pm.delete("never_existed") is False

    def test_delete_keeps_user_extra_files(self, tmp_path):
        """只删本机制生成的两个文件，用户放目录里的别的东西不能动。"""
        pm = PersonaManager(tmp_path)
        pm.save("mine", body="x")
        extra = tmp_path / "personas" / "mine" / "notes.md"
        extra.write_text("用户的笔记", encoding="utf-8")
        assert pm.delete("mine") is True
        assert extra.is_file()
        assert pm.get("mine") is None

    def test_delete_rejects_invalid_name(self, personas_dir):
        pm = PersonaManager(personas_dir)
        for bad in ["../assistant", "bad name", "", ".."]:
            with pytest.raises(ValueError):
                pm.delete(bad)
        assert pm.get("fortune_teller") is not None

    def test_delete_clears_default_marker(self, personas_dir):
        pm = PersonaManager(personas_dir)
        pm.save("mine", body="x", default=True)
        assert pm.default().name == "mine"
        pm.delete("mine")
        assert pm.default().name == "default"
