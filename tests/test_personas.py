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
            assert parse_persona_cmd(raw) == ("reset", None), raw

    def test_use_variants(self):
        assert parse_persona_cmd("/personas use yun") == ("use", "yun")
        assert parse_persona_cmd("/人格 使用 yun") == ("use", "yun")
        assert parse_persona_cmd("/personas yun") == ("use", "yun")

    def test_use_missing_name(self):
        assert parse_persona_cmd("/persona use") == ("use", "")

    def test_tokens_strip(self):
        assert persona_tokens("/personas  use  yun ") == ["use", "yun"]


class TestPersonaDefaultEnv:
    """PERSONAS_DEFAULT：本地覆盖默认人格（写在本机 .env，不入库）。

    变异锚点：去掉 env 优先分支 → env 用例红；去掉不存在时的回退 → 回退用例红。
    """

    def test_env_overrides_md_default(self, personas_dir, monkeypatch):
        monkeypatch.setenv("PERSONAS_DEFAULT", "fortune_teller")
        pm = PersonaManager(personas_dir)
        assert pm.default().name == "fortune_teller"

    def test_env_unknown_falls_back_to_md_default(self, personas_dir, monkeypatch):
        monkeypatch.setenv("PERSONAS_DEFAULT", "gone")
        pm = PersonaManager(personas_dir)
        assert pm.default().name == "assistant"  # md default: true

    def test_blank_env_falls_back_to_md_default(self, personas_dir, monkeypatch):
        for raw in ("", "   "):
            monkeypatch.setenv("PERSONAS_DEFAULT", raw)
            pm = PersonaManager(personas_dir)
            assert pm.default().name == "assistant"


class TestRefresh:
    def test_refresh_discovers_new_file(self, personas_dir):
        pm = PersonaManager(personas_dir)
        assert pm.get("newbie") is None
        (personas_dir / "newbie.md").write_text(
            "---\nname: newbie\ndescription: x\n---\nbody", encoding="utf-8"
        )
        pm.refresh()
        assert pm.get("newbie") is not None


class TestPersonasDirEnvEdgeCases:
    """补充：autouse fixture 会删除 PERSONAS_DIR，但本文件的用例显式覆盖两条分支，
    确保该配置读取路径不会「永不执行」而被静默回归。"""

    def test_blank_env_falls_back_to_arg(self, personas_dir, monkeypatch):
        # 空白值等同于未设置 → 回落到构造参数
        monkeypatch.setenv("PERSONAS_DIR", "   ")
        pm = PersonaManager(personas_dir)
        assert pm.get("assistant") is not None

    def test_env_overrides_arg(self, personas_dir, monkeypatch):
        monkeypatch.setenv("PERSONAS_DIR", str(personas_dir))
        pm = PersonaManager("/definitely/not/exist")
        assert [p.name for p in pm.list()]  # env 生效，参数被忽略

    def test_missing_env_dir_yields_empty_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERSONAS_DIR", str(tmp_path / "nope"))
        pm = PersonaManager(tmp_path / "nope")
        assert pm.list() == []


class TestPersonaSaveDelete:
    """save/delete：web 导入/删除人格的落盘层。

    变异锚点：去掉 _NAME_RE 校验、去掉 refresh、去掉大小上限，都必须有用例红。
    """

    def test_save_creates_file_with_frontmatter(self, tmp_path):
        pm = PersonaManager(tmp_path)
        p = pm.save("tester", description="测试人格", body="第一行\n第二行")
        path = tmp_path / "tester.md"
        assert path.is_file()
        # round-trip：写出去的文件要能被 _scan 原样读回
        assert p.name == "tester" and p.description == "测试人格"
        assert p.body == "第一行\n第二行" and p.default is False
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n") and "name: tester" in text
        assert "默认助手" not in text  # 不夹带其它 persona 的内容
        # 0600：persona 内容等同配置，不给同机其它用户读
        assert path.stat().st_mode & 0o777 == 0o600

    def test_save_overwrites_without_manual_refresh(self, personas_dir):
        pm = PersonaManager(personas_dir)
        assert pm.get("assistant").body == "这是助手正文。"
        pm.list()  # 建立扫描缓存（TTL 2s 内不会自动重扫）
        pm.save("assistant", body="新正文")
        # save 内部必须 refresh：否则缓存里的旧 body 会一直serving到 TTL 过期
        assert pm.get("assistant").body == "新正文"

    def test_save_rejects_invalid_name(self, tmp_path):
        pm = PersonaManager(tmp_path)
        for bad in ["../evil", "bad name", "a/b", "", "..", "中文名"]:
            with pytest.raises(ValueError):
                pm.save(bad, body="x")
        assert list(tmp_path.glob("*.md")) == []  # 一个文件都不许落

    def test_save_rejects_oversized(self, tmp_path):
        pm = PersonaManager(tmp_path)
        with pytest.raises(ValueError):
            pm.save("big", body="x" * 20001)  # body 每轮进 prompt，封顶 20000
        pm.save("edge", body="x" * 20000)  # 边界值必须放行
        with pytest.raises(ValueError):
            pm.save("wordy", description="x" * 501)  # description 封顶 500
        pm.save("desc_edge", description="x" * 500)
        assert pm.get("edge") is not None and pm.get("desc_edge") is not None

    def test_save_default_does_not_touch_other_files(self, personas_dir):
        pm = PersonaManager(personas_dir)
        pm.save("mine", body="我的人格", default=True)
        # 多 default 保持既有语义：默认 = 名称排序第一个，不动别人文件
        assert pm.get("assistant").default is True
        assert pm.default().name == "assistant"

    def test_save_leaves_no_tmp_and_creates_dir(self, tmp_path):
        target = tmp_path / "fresh" / "deeper"
        pm = PersonaManager(target)
        pm.save("hello", body="hi")
        assert (target / "hello.md").is_file()  # 目录不存在时自建
        assert list(target.glob(".*tmp*")) == []  # 原子替换不残留临时文件

    def test_save_unicode_roundtrip(self, tmp_path):
        pm = PersonaManager(tmp_path)
        pm.save("yun", description="云端助手", body="你是温柔的知识伙伴。")
        text = (tmp_path / "yun.md").read_text(encoding="utf-8")
        assert "description: 云端助手" in text  # allow_unicode：不写 \u 转义
        assert pm.get("yun").body == "你是温柔的知识伙伴。"

    def test_delete_removes_and_reports_missing(self, personas_dir):
        pm = PersonaManager(personas_dir)
        pm.save("mine", body="x")
        assert pm.delete("mine") is True
        assert pm.get("mine") is None
        assert not (personas_dir / "mine.md").exists()
        assert pm.delete("mine") is False  # 已删除：False 不抛
        assert pm.delete("never_existed") is False

    def test_delete_rejects_invalid_name(self, personas_dir):
        pm = PersonaManager(personas_dir)
        for bad in ["../assistant", "bad name", "", ".."]:
            with pytest.raises(ValueError):
                pm.delete(bad)
        # 关键：拒绝的是"删掉目录外文件"这条路，原文件必须完好
        assert pm.get("assistant") is not None
