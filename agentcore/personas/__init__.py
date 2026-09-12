"""人格（Persona）系统：目录下每个 md 文件定义一种人格。"""

from agentcore.personas.manager import (
    DEFAULT_PERSONAS_DIR,
    Persona,
    PersonaManager,
    load_manager,
)

__all__ = ["DEFAULT_PERSONAS_DIR", "Persona", "PersonaManager", "load_manager"]
