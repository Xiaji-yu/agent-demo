import pytest

from agentcore.memory.store import InMemoryMemoryStore
from agentcore.personas import PersonaManager
from plugins.qq_agent_adapter.persona_utils import parse_persona_cmd, persona_tokens


@pytest.fixture
def personas_dir(tmp_path):
    (tmp_path / "assistant.md").write_text(
        "---\nname: assistant\ndescription: 默认助手\ndefault: true\n---\n这是助手正文。",
        encoding="utf-8",
    )
    (tmp_path / "fortune_teller.md").write_text(
        "---\nname: fortune_teller\ndescription: 玄学顾问\n---\n这是玄学顾问正文。",
        encoding="utf-8",
    )
    (tmp_path / "broken.md").write_text("没有 frontmatter 的文件", encoding="utf-8")
    (tmp_path / "bad_name.md").write_text(
        "---\nname: bad name!\ndescription: x\n---\nbody", encoding="utf-8"
    )
    return tmp_path


class TestPersonaManager:
    def test_list_and_default(self, personas_dir):
        pm = PersonaManager(personas_dir)
        names = [p.name for p in pm.list()]
        assert "assistant" in names and "fortune_teller" in names
        assert "broken" not in names  # 无 frontmatter 跳过
        assert "bad name!" not in names  # 非法名跳过
        assert pm.default().name == "assistant"

    def test_get(self, personas_dir):
        pm = PersonaManager(personas_dir)
        p = pm.get("fortune_teller")
        assert p is not None and p.body == "这是玄学顾问正文。"
        assert pm.get("../etc/passwd") is None  # 路径穿越防护

    def test_env_dir_override(self, personas_dir, monkeypatch):
        monkeypatch.setenv("PERSONAS_DIR", str(personas_dir))
        pm = PersonaManager("/nonexistent")
        assert pm.get("assistant") is not None  # env 优先于参数


class TestMemoryPersona:
    @pytest.mark.asyncio
    async def test_set_get_persona(self):
        store = InMemoryMemoryStore()
        assert await store.get_user_persona("u1") is None
        await store.set_user_persona("u1", "fortune_teller")
        assert await store.get_user_persona("u1") == "fortune_teller"
        await store.set_user_persona("u1", None)
        assert await store.get_user_persona("u1") is None


class TestPersonaCmdParse:
    def test_view_variants(self):
        for raw in ["/persona", "/personas", "/personas list", "/人格 列表", "persona ls"]:
            assert parse_persona_cmd(raw) == ("list", None), raw

    def test_reset_variants(self):
        for raw in ["/personas reset", "/人格 清除", "persona 恢复默认"]:
            assert parse_persona_cmd(raw) == ("reset", None), raw

    def test_use_variants(self):
        assert parse_persona_cmd("/personas use yun") == ("use", "yun")
        assert parse_persona_cmd("/人格 使用 yun") == ("use", "yun")
        assert parse_persona_cmd("/personas yun") == ("use", "yun")

    def test_use_missing_name(self):
        assert parse_persona_cmd("/persona use") == ("use", "")

    def test_tokens_strip(self):
        assert persona_tokens("/personas  use  yun ") == ["use", "yun"]


class TestRefresh:
    def test_refresh_discovers_new_file(self, personas_dir):
        pm = PersonaManager(personas_dir)
        assert pm.get("newbie") is None
        (personas_dir / "newbie.md").write_text(
            "---\nname: newbie\ndescription: x\n---\nbody", encoding="utf-8"
        )
        pm.refresh()
        assert pm.get("newbie") is not None
