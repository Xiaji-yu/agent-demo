"""本地自定义工具型技能加载器（data/skills/*.py）。

与 prompt 型（*.yaml，SkillInstaller 管理）并列的第二种用户扩展介质：**代码**型，
让部署者写自己的工具技能（如 Home Assistant / 自建后台 API 封装）。模块约定：

.. code-block:: python

    # data/skills/my_tool.py
    def register_skills(registry):
        @registry.register("my_tool", "描述", {...schema...})
        async def my_tool(args):
            ...

**信任模型**：这些文件与「改本仓库源码 / 装插件」同信任级——由部署者完全自担，
加载器不做沙箱、不隔离依赖。安全边界只做两件事：

1. **拒绝覆盖已占用技能名**：``registry.register`` 是装饰器工厂，dict 赋值发生在
   装饰器应用时——静默覆盖会让内置工具被同名实现顶替（模型只能对着 JSON 编造结果），
   若同名来自落盘 manifest 则每次重启重新覆盖（持久损坏）。加载器用包装注册表在
   ``register()`` 入口拒同名（含两个自定义文件之间：字典序先者生效），dict 赋值
   永不发生，既有对象完好。
2. **技能目录不得落在 fs 可写工作区内**：fs_write/run_build_script 可写工作区，
   目录若在其中，聊天侧即可预埋 .py 等下次重启执行（提权链）。目录（解析符号链接
   后）等于或落在 ``workspace_root()`` 内时，拒绝加载全部 .py 并 WARNING。

其余为可用性而非安全：坏文件（import 异常 / 缺 ``register_skills`` / 注册异常）
WARNING 跳过不阻断启动；加载结束打一行汇总（INFO）。默认 ``read_only=False``——
引擎据此判断一步内多个工具调用可否并行执行，副作用类工具绝不能并行；自定义技能
要自行 ``read_only=True`` 声明（opt-in，与内置 ``mark_read_only`` 同一立场）。

非目标：无热重载（改文件重启生效）、无依赖管理（import 失败即跳过）、不递归子目录。
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

from agentcore.skills.registry import SkillRegistry
from agentcore.workspace.utils import workspace_root

logger = logging.getLogger(__name__)

#: 自定义技能模块必须提供的模块级函数签名：register_skills(registry) -> None
ENTRY_FUNCTION = "register_skills"


class _GuardedRegistry:
    """拒绝同名覆盖的注册表包装；其余 API（mark_read_only 等）原样透传。"""

    def __init__(self, registry: SkillRegistry):
        self._registry = registry

    def register(
        self,
        name: str,
        description: str,
        params_schema: dict,
        permission: str = "public",
        manifest: Any = None,
        read_only: bool = False,
    ):
        # 在 dict 赋值之前拒绝：内置/先加载的同名技能对象不被触碰。
        # WARNING 必须打在冲突点：加载器的「跳过 <文件>」一行只说哪个文件没加载，
        # 排查「技能没生效」时要能直接看到被拒的技能名与原因（m00290 用户要求）。
        if name in self._registry.skills:
            logger.warning(
                "local skills: 拒绝注册 %r——该技能名已被内置或先加载的自定义技能"
                "占用（防静默覆盖；两个自定义文件同名时字典序先者生效）",
                name,
            )
            raise ValueError(
                f"技能名 {name!r} 已被占用（内置或先加载的自定义技能），拒绝覆盖"
            )
        return self._registry.register(
            name, description, params_schema, permission, manifest, read_only
        )

    def __getattr__(self, item: str) -> Any:
        return getattr(self._registry, item)


def _refuse_reason(skills_dir: Path) -> str | None:
    """技能目录不可加载 .py 时返回原因，安全就绪返回 None。"""
    try:
        resolved = skills_dir.resolve()
    except OSError as e:
        return f"目录解析失败（{e}），拒绝加载"
    root = workspace_root()
    if resolved == root or root in resolved.parents:
        return (
            f"技能目录 {resolved} 落在 fs 可写工作区 {root} 内，"
            "拒绝加载（聊天侧可写工作区即等于可预埋待执行代码）"
        )
    return None


def _import_file(path: Path) -> Any:
    """按路径 import 一个 .py 文件（不进 sys.modules，不依赖包结构）。"""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path.name} 创建 import spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_local_skills(
    skills_dir: Path, registry: SkillRegistry
) -> tuple[list[str], list[str]]:
    """加载 skills_dir 下 *.py 自定义技能，返回 (loaded, skipped) 文件名列表。"""
    skills_dir = Path(skills_dir)
    if not skills_dir.is_dir():
        return [], []
    reason = _refuse_reason(skills_dir)
    if reason is not None:
        logger.warning("local skills: %s", reason)
        return [], []
    guard = _GuardedRegistry(registry)
    loaded: list[str] = []
    skipped: list[str] = []
    for path in sorted(skills_dir.glob("*.py")):
        try:
            module = _import_file(path)
            entry = getattr(module, ENTRY_FUNCTION, None)
            if entry is None:
                raise AttributeError(f"缺少模块级 {ENTRY_FUNCTION}(registry)")
            entry(guard)
        except Exception as e:
            # 坏文件不阻断启动：跳过并告警，等部署者看日志清理
            logger.warning(
                "local skills: 跳过 %s（%s: %s）", path.name, type(e).__name__, e
            )
            skipped.append(path.name)
            continue
        loaded.append(path.name)
    if loaded or skipped:
        parts = [f"{len(loaded)} loaded ({', '.join(loaded)})"]
        if skipped:
            parts.append(f"{len(skipped)} skipped ({', '.join(skipped)})")
        logger.info("local skills: %s", ", ".join(parts))
    return loaded, skipped
