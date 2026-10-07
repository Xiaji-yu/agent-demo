"""工作区技能（fs_* / run_command）。个人服务器：全部仅管理员可用，单一共享目录。

级别（AGENT_PERMISSION_LEVEL，见 skills/levels.py）：
- low    ：注册 fs_list/fs_read + run_command（handler 内传 readonly_only，
           runner 拒绝 zip/unzip/tar/curl）
- medium+：追加注册 fs_write/fs_mkdir/fs_delete（删除保留聊天确认码）
注册期过滤（low 档 schema 不可见）+ handler 内 at_least 复查双层——与
「registry 层曾被通配符击穿」的 P0 教训一致：单一闸门不够。
"""

from __future__ import annotations

from pathlib import Path

from agentcore.skills.levels import at_least
from agentcore.skills.registry import SkillRegistry
from agentcore.workspace.confirm import get_gate
from agentcore.workspace.fs import WorkspaceFS
from agentcore.workspace.runner import CommandRunner
from agentcore.workspace.utils import is_superuser, workspace_root

# 用户可见结果标记：生产与测试共用（文案改动只需改这里）
MSG_REJECTED = "拒绝"

_LOW_WRITE_DENIED = (
    "当前权限级别为 low：{}需要 medium 及以上（AGENT_PERMISSION_LEVEL，改后重启生效）。"
)


def _root() -> Path:
    # 单一事实来源：与 admin.py / 图片落盘共用 workspace_root()
    return workspace_root()


def _fs() -> WorkspaceFS:
    return WorkspaceFS(_root())


def _deny_or_fs(user_id: str) -> WorkspaceFS | None:
    if not is_superuser(user_id):
        return None
    return _fs()


def register_workspace_skills(registry: SkillRegistry) -> None:
    # ------------------------------------------------------------------
    # 只读面（low+）
    # ------------------------------------------------------------------
    @registry.register(
        "fs_list",
        "列出服务器工作区目录内容（仅管理员）。路径如 . 或 sub/dir，不能越出工作区。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径，默认 ."}
            },
            "required": [],
        },
        permission="superuser",
    )
    async def fs_list_skill(path: str = ".", user_id: str = "") -> str:
        fs = _deny_or_fs(user_id)
        if fs is None:
            return "无权限：仅管理员可使用工作区。"
        try:
            return await fs.list(path)
        except ValueError as e:
            return f"{MSG_REJECTED}：{e}"
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
        permission="superuser",
    )
    async def fs_read_skill(path: str, user_id: str = "") -> str:
        fs = _deny_or_fs(user_id)
        if fs is None:
            return "无权限：仅管理员可使用工作区。"
        try:
            return await fs.read(path)
        except ValueError as e:
            return f"{MSG_REJECTED}：{e}"
        except Exception:
            return "(读取失败，请稍后再试)"

    @registry.register(
        "run_command",
        "在服务器工作区执行白名单命令（仅管理员）。"
        "允许：git 只读子命令(安全选项)、grep/find(仅搜索动作)/cat/ls/head/tail/wc/pwd、"
        "只读运维命令（ps/top(须-b)/free/df/du/uptime/uname/nproc/whoami/id/netstat/lscpu 免参数、"
        "ss 仅展示/过滤选项（-K/--kill 断链路，拒）、"
        "systemctl(仅 is-active/is-enabled/is-failed/status/show/list-units/list-unit-files/list-jobs)、"
        "journalctl/dmesg/hostname 仅只读选项）、zip、unzip(-d 指定目录)、curl(GET-only https)。"
        "权限级别为 low（AGENT_PERMISSION_LEVEL）时仅允许其中的只读子集"
        "（zip/unzip/tar/curl 拒绝）。"
        "禁止组合命令与命令替换；含 / 的参数必须位于工作区内；"
        "find 不支持 -exec/-delete 等动作；python3/node/npm/bash 已禁用。"
        "没有 kill 与 systemctl 动作类子命令（restart/stop 等）：需要重启本服务时，"
        "请让管理员在聊天中发送 /reboot。排查远端内网机器用 ssh_run。",
        {
            "type": "object",
            "properties": {
                "executable": {
                    "type": "string",
                    "description": "白名单内的命令名，如 git / grep / curl",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "参数列表，如 [status]",
                },
            },
            "required": ["executable"],
        },
        permission="superuser",
    )
    async def run_command_skill(
        executable: str, args: list[str] | None = None, user_id: str = ""
    ) -> str:
        if not is_superuser(user_id):
            return "无权限：仅管理员可执行命令。"
        # 模型偶发把 args 传成字符串；list("status") 会静默变形为 ['s','t',…]
        if isinstance(args, str):
            import shlex

            args = shlex.split(args)
        elif args is not None and not isinstance(args, list | tuple):
            return "错误：args 必须是字符串数组"
        try:
            runner = CommandRunner(_root())
            return await runner.run(
                executable,
                list(args or []),
                uid=user_id,
                # low 档只读子集：zip/unzip/tar/curl 由 runner.permitted 拒绝
                readonly_only=not at_least("medium"),
            )
        except Exception:
            return "(执行失败，请稍后再试)"

    # ------------------------------------------------------------------
    # 写入面（medium+）：级别不够就不注册——schema 不可见、幻觉调用得 unknown
    # ------------------------------------------------------------------
    if not at_least("medium"):
        return

    @registry.register(
        "fs_write",
        "把内容写入服务器工作区文件（仅管理员），自动创建父目录。",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对路径，如 report.md 或 notes/a.txt",
                },
                "content": {"type": "string", "description": "要写入的完整内容"},
            },
            "required": ["path", "content"],
        },
        permission="superuser",
    )
    async def fs_write_skill(path: str, content: str, user_id: str = "") -> str:
        if not at_least("medium"):
            return _LOW_WRITE_DENIED.format("写入")
        fs = _deny_or_fs(user_id)
        if fs is None:
            return "无权限：仅管理员可使用工作区。"
        try:
            return await fs.write(path, content)
        except ValueError as e:
            return f"{MSG_REJECTED}：{e}"
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
        permission="superuser",
    )
    async def fs_mkdir_skill(path: str, user_id: str = "") -> str:
        if not at_least("medium"):
            return _LOW_WRITE_DENIED.format("创建目录")
        fs = _deny_or_fs(user_id)
        if fs is None:
            return "无权限：仅管理员可使用工作区。"
        try:
            return await fs.mkdir(path)
        except ValueError as e:
            return f"{MSG_REJECTED}：{e}"
        except Exception:
            return "(创建失败，请稍后再试)"

    @registry.register(
        "fs_delete",
        "删除服务器工作区内的文件或空目录（仅管理员）。需用户在聊天中回复确认码才真正执行。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要删除的相对路径"}
            },
            "required": ["path"],
        },
        permission="superuser",
    )
    async def fs_delete_skill(path: str, user_id: str = "") -> str:
        if not at_least("medium"):
            return _LOW_WRITE_DENIED.format("删除")
        fs = _deny_or_fs(user_id)
        if fs is None:
            return "无权限：仅管理员可使用工作区。"
        try:
            abs_path = fs.resolve(path)
            code = await get_gate().request(user_id, str(abs_path))
            return (
                f"删除请求已登记：{path}\n"
                f"请在 10 分钟内回复确认删除 {code} 以执行；否则自动失效。"
            )
        except ValueError as e:
            return f"{MSG_REJECTED}：{e}"
        except Exception:
            return "(操作失败，请稍后再试)"
