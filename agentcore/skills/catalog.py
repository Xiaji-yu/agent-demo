"""内置 skill 目录：用户可一键安装的预设 skill。"""
from agentcore.skills.manifest import SkillManifest

CATALOG: dict[str, SkillManifest] = {
    "translator": SkillManifest(
        name="translator",
        description="中英互译专家，只做翻译，保留原意与语气。",
        type="prompt",
        prompt="你是一个专业翻译。只做翻译，不做其他。保留原文语气、术语与格式。",
        parameters=[
            {"name": "text", "type": "string", "description": "要翻译的文本"},
            {"name": "target_lang", "type": "string", "description": "目标语言，如 en 或 zh"},
        ],
        permission="public",
    ),
    "summarizer": SkillManifest(
        name="summarizer",
        description="长文本摘要专家，输出 concise bullet summary。",
        type="prompt",
        prompt="你是一个摘要专家。将输入文本压缩为简洁的要点摘要，保留关键数字与结论。",
        parameters=[
            {"name": "text", "type": "string", "description": "要摘要的文本"},
            {"name": "max_bullets", "type": "integer", "description": "最大要点数，默认 5"},
        ],
        permission="public",
    ),
    "polisher": SkillManifest(
        name="polisher",
        description="润色助手，改善文本表达与可读性。",
        type="prompt",
        prompt="你是一个润色助手。改善文本的表达与可读性，保留原意，不添加新信息。",
        parameters=[
            {"name": "text", "type": "string", "description": "要润色的文本"},
            {"name": "style", "type": "string", "description": "风格，如 professional / casual / concise"},
        ],
        permission="public",
    ),
    "coder_reviewer": SkillManifest(
        name="coder_reviewer",
        description="代码审查专家，给出问题、风险与改进建议。",
        type="prompt",
        prompt="你是一个资深代码审查员。审查代码，指出 bug、可读性、性能与安全问题，给出修复建议。",
        parameters=[
            {"name": "code", "type": "string", "description": "代码内容"},
            {"name": "language", "type": "string", "description": "编程语言"},
        ],
        permission="public",
    ),
}
