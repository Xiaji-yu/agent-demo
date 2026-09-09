"""工作区技能（fs_* / run_command）。个人服务器：全部仅管理员可用，单一共享目录。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

from agentcore.skills.registry import SkillRegistry
from agentcore.workspace.confirm import get_gate
from agentcore.workspace.fs import WorkspaceFS
from agentcore.workspace.runner import CommandRunner
from agentcore.workspace.utils import is_superuser


def _root() -> Path:
    return Path(os.getenv("WORKSPACE_DIR", "data/workspace")).resolve()


def _fs() -> WorkspaceFS:
    return WorkspaceFS(_root())


def _deny_or_fs(user_id: str) -> WorkspaceFS | None:
    if not is_superuser(user_id):
        return None
    return _fs()


def register_workspace_skills(registry: SkillRegistry) -> None:
    @registry.register(
        "fs_list",
        "列出服务器工作区目录内容（仅管理员）。路径如 . 或 sub/dir，不能越出工作区。",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对路径，默认 ."}},
            "required": [],
        },
        permission="public",
    )
    async def fs_list_skill(path: str = ".", user_id: str = "") -> str:
        fs = _deny_or_fs(user_id)
        if fs is None:
            return "仅管理员可使用工作区。"
        try:
            return await fs.list(path)
        except ValueError as e:
            return f"拒绝：{e}"
        except Exception:
            return "(操作失败，请稍后再试)"

    @registry.register(
        "fs_read",
        "读取服务器工作区内的文件内容（仅管理员）。",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对路径"}},
            "required": ["path"],
        },
        permission="public",
    )
    async def fs_read_skill(path: str, user_id: str = "") -> str:
        fs = _deny_or_fs(user_id)
        if fs is None:
            return "仅管理员可使用工作区。"
        try:
            return await fs.read(path)
        except ValueError as e:
            return f"拒绝：{e}"
        except Exception:
            return "(读取失败，请稍后再试)"

    @registry.register(
        "fs_write",
        "把内容写入服务器工作区文件（仅管理员），自动创建父目录。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径，如 report.md 或 notes/a.txt"},
                "content": {"type": "string", "description": "要写入的完整内容"},
            },
            "required": ["path", "content"],
        },
        permission="public",
    )
    async def fs_write_skill(path: str, content: str, user_id: str = "") -> str:
        fs = _deny_or_fs(user_id)
        if fs is None:
            return "仅管理员可使用工作区。"
        try:
            return await fs.write(path, content)
        except ValueError as e:
            return f"拒绝：{e}"
        except Exception:
            return "(写入失败，请稍后再试)"

    @registry.register(
        "fs_mkdir",
        "在服务器工作区创建目录（仅管理员）。",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对路径"}},
            "required": ["path"],
        },
        permission="public",
    )
    async def fs_mkdir_skill(path: str, user_id: str = "") -> str:
        fs = _deny_or_fs(user_id)
        if fs is None:
            return "仅管理员可使用工作区。"
        try:
            return await fs.mkdir(path)
        except ValueError as e:
            return f"拒绝：{e}"
        except Exception:
            return "(创建失败，请稍后再试)"

    @registry.register(
        "fs_delete",
        "删除服务器工作区内的文件或空目录（仅管理员）。需用户在聊天中回复确认码才真正执行。",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "要删除的相对路径"}},
            "required": ["path"],
        },
        permission="public",
    )
    async def fs_delete_skill(path: str, user_id: str = "") -> str:
        fs = _deny_or_fs(user_id)
        if fs is None:
            return "仅管理员可使用工作区。"
        try:
            abs_path = fs.resolve(path)
            code = await get_gate().request(user_id, str(abs_path))
            return (
                f"删除请求已登记：{path}\n"
                f"请在 10 分钟内回复确认删除 {code} 以执行；否则自动失效。"
            )
        except ValueError as e:
            return f"拒绝：{e}"
        except Exception:
            return "(操作失败，请稍后再试)"

    @registry.register(
        "run_command",
        "在服务器工作区执行白名单命令（仅管理员）。"
        "允许：git(status/log/diff/show 等只读)、grep/find/cat/ls/head/tail/wc/pwd、"
        "zip、unzip(-d 指定目录)、curl(仅 https)。"
        "禁止组合命令（无管道/分号/重定向），参数不能含绝对路径或 ..；python3/node/npm 已禁用。",
        {
            "type": "object",
            "properties": {
                "executable": {"type": "string", "description": "白名单内的命令名，如 git / grep / curl"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "参数列表，如 [status]"},
            },
            "required": ["executable"],
        },
        permission="public",
    )
    async def run_command_skill(executable: str, args: List[str] | None = None, user_id: str = "") -> str:
        if not is_superuser(user_id):
            return "仅管理员可执行命令。"
        try:
            runner = CommandRunner(_root())
            return await runner.run(executable, list(args or []))
        except Exception:
            return "(执行失败，请稍后再试)"
