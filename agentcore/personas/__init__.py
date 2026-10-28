"""人格（Persona）系统：OpenClaw 兼容的 SOUL.md 目录布局。

根级 ``SOUL.md`` 是默认人格，``personas/<name>/SOUL.md`` 是可切换人格
（目录名 = 人格名）。详见 :mod:`agentcore.personas.manager`。
"""

from agentcore.personas.manager import (
    DEFAULT_PERSONAS_DIR,
    Persona,
    PersonaManager,
    load_manager,
)

__all__ = ["DEFAULT_PERSONAS_DIR", "Persona", "PersonaManager", "load_manager"]
