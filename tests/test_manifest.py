import pytest

from agentcore.skills.manifest import SkillManifest


class TestSkillManifest:
    def test_valid_prompt_manifest(self):
        m = SkillManifest(
            name="translator",
            description="翻译",
            type="prompt",
            prompt="你是一个翻译",
            parameters=[{"name": "text", "type": "string"}],
            permission="public",
        )
        assert m.name == "translator"
        assert m.type == "prompt"

    def test_invalid_name(self):
        with pytest.raises(ValueError):
            SkillManifest(name="INVALID", description="x", type="prompt", prompt="p")

    def test_invalid_type(self):
        with pytest.raises(ValueError):
            SkillManifest(name="x", description="x", type="bad", prompt="p")

    def test_prompt_skill_requires_prompt(self):
        with pytest.raises(ValueError):
            SkillManifest(name="x", description="x", type="prompt", prompt="")

    def test_invalid_permission(self):
        with pytest.raises(ValueError):
            SkillManifest(
                name="x",
                description="x",
                type="prompt",
                prompt="p",
                permission="admin",
            )

    def test_from_yaml(self):
        text = """
name: summarizer
description: 摘要
type: prompt
prompt: |
  你是一个摘要。
parameters:
  - name: text
    type: string
permission: public
"""
        m = SkillManifest.from_yaml(text)
        assert m.name == "summarizer"
        assert len(m.parameters) == 1

    def test_from_yaml_invalid(self):
        with pytest.raises(ValueError):
            SkillManifest.from_yaml("name: INVALID")

    def test_round_trip_yaml(self):
        m = SkillManifest(
            name="polisher",
            description="润色",
            type="prompt",
            prompt="润色文本",
            parameters=[],
            permission="public",
        )
        yaml_text = m.to_yaml()
        parsed = SkillManifest.from_yaml(yaml_text)
        assert parsed.name == m.name
        assert parsed.type == m.type
        assert parsed.prompt == m.prompt
