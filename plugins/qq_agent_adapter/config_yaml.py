"""``config.yaml`` 的受控写入：文本级逐键补丁（保注释/保顺序）+ 写后校验。

为什么不 ``yaml.safe_load`` → 改 → ``safe_dump``：那会把整份文件的注释、
空行与字段顺序全部抹掉——config.yaml 的注释本身就是运维文档（每个键为什么
这么设、单位是什么）。所以这里和 ``config_write.patch_env_text`` 同思路：
只替换目标行的**值**，行内注释、缩进、其余字节原样保留。

写入链与 .env 完全同构：symlink fail-closed → 整文件备份 → 手术式补丁 →
原子写 → **重新解析校验**（值真的落上且能被 YAML 解析回来）→ 失败即还原。

生效时机：``config.yaml`` 在启动时被读进 ``AgentEngine.config`` / KB /
scheduler，没有运行态回写钩子——所有键都是 ``boot``，保存后需重启。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config_write import (
    EnvWriteError,
    WriteResult,
    atomic_write,
    backup_dir,
    backup_file,
    read_env_text,
    restore_file,
)

logger = logging.getLogger(__name__)

CONFIG_ENV = "AGENT_CONFIG"
YAML_PREFIX = "yaml:"
_BACKUP_KEEP = 10
_FORBIDDEN = ("\r", "\n", "#")


@dataclass(frozen=True)
class YamlSpec:
    path: str  # 点号路径，如 agent.history_token_budget
    kind: str  # bool | int | float | str
    group: str
    label: str
    description: str = ""
    default: str = ""
    min_value: int | float | None = None
    max_value: int | float | None = None
    max_len: int | None = None
    writable: bool = True
    cluster: str = ""


YAML_OPTIONS: dict[str, tuple[str, ...]] = {
    "agent.history_token_budget": ("2000", "3000", "4000", "6000"),
    "agent.summary_max_tokens": ("2048", "4096", "8192"),
    "agent.history_stale_after": ("0", "3600", "21600", "86400"),
    "rag.top_k": ("2", "4", "6", "8"),
    "rag.threshold": ("0.2", "0.3", "0.4", "0.5"),
    "rag.max_chunks_per_source": ("200", "500", "1000", "2000"),
    "push.tick": ("10", "30", "60"),
    "push.max_chars": ("200", "500", "1000"),
}


def _s(path: str, kind: str, group: str, label: str, desc: str, default: str, **kw):
    if "cluster" not in kw:
        if path == "agent.extract_facts" or path.startswith("agent.memory_facts"):
            kw["cluster"] = "memory"
        elif path.startswith("agent."):
            kw["cluster"] = "history"
        elif path.startswith("archive."):
            kw["cluster"] = "archive"
        elif path.startswith("backup."):
            kw["cluster"] = "backup"
        elif path.startswith("rag."):
            kw["cluster"] = "kb"
        elif path.startswith("push."):
            kw["cluster"] = "push"
        else:
            kw["cluster"] = "advanced"
    return YamlSpec(path, kind, group, label, desc, default, **kw)


YAML_SPECS: tuple[YamlSpec, ...] = (
    # 记忆与历史（agent 段）
    _s(
        "agent.max_iterations",
        "int",
        "Agent",
        "最大工具轮数",
        "单轮对话最多调几次工具",
        "8",
        min_value=1,
        max_value=50,
    ),
    _s(
        "agent.extract_facts",
        "bool",
        "Agent",
        "事实抽取",
        "每轮从用户消息抽取长期事实",
        "true",
    ),
    _s(
        "agent.memory_facts_top_k",
        "int",
        "Agent",
        "召回事实条数",
        "每次组装 prompt 召回的相关事实数",
        "5",
        min_value=1,
        max_value=50,
    ),
    _s(
        "agent.memory_facts_threshold",
        "float",
        "Agent",
        "召回相似度下限",
        "低于该值的事实不注入",
        "0.15",
        min_value=0.0,
        max_value=1.0,
    ),
    _s(
        "agent.summary_enabled",
        "bool",
        "Agent",
        "滚动摘要",
        "超预算时把更早历史压缩进 sessions.summary",
        "true",
    ),
    _s(
        "agent.history_token_budget",
        "int",
        "Agent",
        "历史 token 预算",
        "单 turn 历史输入的保守上限",
        "3000",
        min_value=200,
        max_value=200000,
    ),
    _s(
        "agent.summary_max_tokens",
        "int",
        "Agent",
        "摘要输出上限",
        "摘要 LLM 调用的输出 token",
        "4096",
        min_value=256,
        max_value=65536,
    ),
    _s(
        "agent.summary_fetch_limit",
        "int",
        "Agent",
        "摘要取数窗口",
        "最近 N 条内做预算裁剪",
        "200",
        min_value=10,
        max_value=5000,
    ),
    _s(
        "agent.summary_max_chars",
        "int",
        "Agent",
        "摘要字数上限",
        "注入 prompt 的摘要硬截断",
        "2000",
        min_value=100,
        max_value=50000,
    ),
    _s(
        "agent.history_stale_after",
        "int",
        "Agent",
        "历史过时警示",
        "历史最后一条距今超过该秒数时注入时间线警示（0=关闭）",
        "21600",
        min_value=0,
    ),
    # 记录保全
    _s(
        "archive.dir",
        "str",
        "记录保全",
        "归档目录",
        "聊天记录 JSONL 落盘目录",
        "data/archive",
        writable=False,
    ),
    _s(
        "archive.keep_days",
        "int",
        "记录保全",
        "归档保留天数",
        "超过自动删除（0=关闭落盘）",
        "7",
        min_value=0,
        max_value=3650,
        writable=False,
    ),
    _s(
        "backup.dir",
        "str",
        "记录保全",
        "备份目录",
        "数据库备份产物目录",
        "data/backups",
        writable=False,
    ),
    _s(
        "backup.keep",
        "int",
        "记录保全",
        "备份保留份数",
        "保留最近 N 份",
        "7",
        min_value=1,
        max_value=1000,
        writable=False,
    ),
    _s(
        "backup.cron",
        "str",
        "记录保全",
        "备份 cron",
        "每日备份时间（同时触发归档轮转）",
        "30 3 * * *",
        writable=False,
    ),
    _s(
        "backup.mirror_dir",
        "str",
        "记录保全",
        "异地镜像目录",
        "为空则不做镜像",
        "",
        writable=False,
    ),
    # 知识库
    _s(
        "rag.enabled",
        "bool",
        "知识库",
        "知识库开关",
        "false 则既不检索也不蒸馏",
        "true",
    ),
    _s(
        "rag.top_k",
        "int",
        "知识库",
        "检索条数",
        "每次注入 prompt 的检索结果数",
        "4",
        min_value=1,
        max_value=20,
    ),
    _s(
        "rag.threshold",
        "float",
        "知识库",
        "检索相似度下限",
        "低于该值不注入",
        "0.3",
        min_value=0.0,
        max_value=1.0,
    ),
    _s(
        "rag.chunk_chars",
        "int",
        "知识库",
        "切块长度",
        "摄取切块目标长度",
        "600",
        min_value=100,
        max_value=5000,
    ),
    _s(
        "rag.max_chunks_per_source",
        "int",
        "知识库",
        "单来源块数上限",
        "超出部分丢弃并告警",
        "1000",
        min_value=10,
        max_value=100000,
    ),
    _s(
        "rag.digest_cron",
        "str",
        "知识库",
        "蒸馏 cron",
        "每天从记忆蒸馏入库的时间",
        "0 3 * * *",
    ),
    _s(
        "rag.digest_batch",
        "int",
        "知识库",
        "蒸馏批量",
        "每次最多处理多少条新消息",
        "200",
        min_value=1,
        max_value=10000,
    ),
    _s(
        "rag.max_entries",
        "int",
        "知识库",
        "蒸馏条数上限",
        "每次最多沉淀多少条知识",
        "8",
        min_value=1,
        max_value=200,
    ),
    _s(
        "rag.min_chars",
        "int",
        "知识库",
        "蒸馏最短长度",
        "新内容少于该长度则跳过",
        "200",
        min_value=0,
    ),
    _s(
        "rag.distill_max_tokens",
        "int",
        "知识库",
        "蒸馏输出上限",
        "蒸馏 LLM 调用的输出 token",
        "4096",
        min_value=256,
        max_value=65536,
    ),
    _s(
        "rag.distill_per_message_cap",
        "int",
        "知识库",
        "单条消息截断",
        "单条消息超过则截断并留痕",
        "1000",
        min_value=100,
    ),
    _s(
        "rag.distill_total_cap",
        "int",
        "知识库",
        "蒸馏 prompt 总长",
        "transcript 超过则停止收集",
        "20000",
        min_value=1000,
    ),
    # 定时推送（jobs 是列表，不在逐键编辑范围）
    _s(
        "push.enabled",
        "bool",
        "定时推送",
        "推送开关",
        "到点用 LLM 生成正文并投递",
        "true",
    ),
    _s("push.tick", "int", "定时推送", "轮询间隔", "秒（最小 5）", "30", min_value=5),
    _s(
        "push.retry_delay",
        "int",
        "定时推送",
        "失败重试基数",
        "秒（指数退避）",
        "300",
        min_value=1,
    ),
    _s(
        "push.max_failures",
        "int",
        "定时推送",
        "连续失败停用阈值",
        "连续失败这么多次就停用并告警",
        "5",
        min_value=1,
    ),
    _s(
        "push.max_retry_delay",
        "int",
        "定时推送",
        "退避上限",
        "秒",
        "86400",
        min_value=1,
    ),
    _s(
        "push.daily_cap_per_target",
        "int",
        "定时推送",
        "每目标每日上限",
        "0=不限",
        "1",
        min_value=0,
    ),
    _s(
        "push.max_chars",
        "int",
        "定时推送",
        "正文长度上限",
        "同时作为 LLM 输出上限（0=不限）",
        "500",
        min_value=0,
    ),
)

YAML_WRITABLE: dict[str, YamlSpec] = {s.path: s for s in YAML_SPECS if s.writable}


def config_path() -> Path:
    """``config.yaml`` 路径：``AGENT_CONFIG`` 可覆盖（与 engine 读取同源）。"""
    return Path(os.getenv(CONFIG_ENV, "config.yaml"))


def _scalar(value: object, kind: str) -> str:
    """把值渲染成 YAML 标量文本（必要时加双引号）。"""
    if kind == "bool":
        return "true" if value else "false"
    if kind in ("int", "float"):
        return str(value)
    text = str(value)
    if any(c in text for c in _FORBIDDEN):
        raise ValueError("值含换行/# 等行注入字符")
    risky_start = text[:1] in "-?:,[]{}#&*!|>'\"%@`"
    risky_inner = ": " in text or " #" in text or " " in text
    if risky_start or risky_inner or not text:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def validate_value(path: str, raw: object) -> tuple[YamlSpec, object, str]:
    """校验并归一化 YAML 标量；返回 ``(spec, normalized, file_text)``。"""
    spec = YAML_WRITABLE.get(path)
    if spec is None:
        raise ValueError("yaml_key_not_writable")
    s = str(raw if raw is not None else "").strip()
    if not s:
        raise ValueError("empty_value")
    if any(c in s for c in _FORBIDDEN):
        raise ValueError("value_contains_forbidden_chars")
    if spec.kind == "bool":
        low = s.lower()
        if low not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise ValueError("bool_expect_true_false")
        return (
            spec,
            low in {"1", "true", "yes", "on"},
            "true" if low in {"1", "true", "yes", "on"} else "false",
        )
    if spec.kind in ("int", "float"):
        try:
            value = int(s) if spec.kind == "int" else float(s)
        except ValueError:
            raise ValueError(f"{spec.kind}_expect_number") from None
        if spec.min_value is not None and value < spec.min_value:
            raise ValueError(f"{spec.kind}_below_min")
        if spec.max_value is not None and value > spec.max_value:
            raise ValueError(f"{spec.kind}_above_max")
        return spec, value, str(value)
    if spec.max_len is not None and len(s) > spec.max_len:
        raise ValueError("str_too_long")
    return spec, s, _scalar(s, "str")


def patch_yaml_text(text: str, path: str, value_text: str) -> tuple[str, str, str]:
    """只改目标行的值，返回 ``(new_text, old_line, new_line)``。

    定位规则：先找唯一的顶层 ``section:``，再在该块内找唯一的
    ``<缩进>key:``；0 个 → key_not_found，多个 → duplicate_key_lines。
    行内注释与缩进原样保留。
    """
    section, _, key = path.partition(".")
    if not section or not key or "." in key:
        raise ValueError("yaml_path_not_supported")
    lines = text.split("\n")
    # 顶层 section 行（列 0，允许行内注释）
    heads = [
        i
        for i, line in enumerate(lines)
        if re.match(rf"^{re.escape(section)}\s*:\s*(#.*)?$", line)
    ]
    if not heads:
        raise ValueError("yaml_section_not_found")
    if len(heads) > 1:
        raise ValueError("yaml_duplicate_section_lines")
    start = heads[0]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        # 下一个列 0 的非注释行 = 下一个顶层键
        if (
            lines[i]
            and not lines[i][0].isspace()
            and not lines[i].lstrip().startswith("#")
        ):
            end = i
            break
    found = [
        i
        for i in range(start + 1, end)
        if re.match(rf"^\s+{re.escape(key)}\s*:", lines[i])
    ]
    if not found:
        raise ValueError("yaml_key_not_found")
    if len(found) > 1:
        raise ValueError("yaml_duplicate_key_lines")
    idx = found[0]
    old_line = lines[idx]
    m = re.match(rf"^(\s*{re.escape(key)}\s*:\s*)(.*?)(\s*#.*)?$", old_line)
    if not m:
        raise ValueError("yaml_line_not_scalar")
    new_line = m.group(1) + value_text + (m.group(3) or "")
    new_lines = list(lines)
    new_lines[idx] = new_line
    return "\n".join(new_lines), old_line, new_line


def _verify(text: str, spec: YamlSpec, expected: object) -> bool:
    """写后校验：重新解析并确认该路径的值等于期望（类型也要对）。"""
    try:
        import yaml

        data = yaml.safe_load(text) or {}
        cur = data
        for part in spec.path.split("."):
            cur = cur[part]
        if spec.kind == "bool":
            return bool(cur) is expected
        if spec.kind == "int":
            return (
                isinstance(cur, int) and not isinstance(cur, bool) and cur == expected
            )
        if spec.kind == "float":
            return isinstance(cur, int | float) and float(cur) == float(expected)
        return str(cur) == str(expected)
    except Exception:
        return False


def write_yaml_key(spec: YamlSpec, normalized: object, file_text: str) -> WriteResult:
    """备份 → 逐键补丁 → 原子写 → 解析校验 → 失败还原。"""
    from agentcore.diagnostics import record as _record

    path = config_path()
    if path.is_symlink():
        raise ValueError("config_file_is_symlink")
    if not path.exists():
        raise EnvWriteError("config_file_missing")
    try:
        old_text = read_env_text(path)
    except OSError as e:
        raise EnvWriteError(f"config_read: {type(e).__name__}") from e
    backup = backup_file(path, backup_dir())
    try:
        new_text, old_line, _new_line = patch_yaml_text(old_text, spec.path, file_text)
    except ValueError:
        restore_file(backup, path)
        raise
    atomic_write(path, new_text)
    if not _verify(new_text, spec, normalized):
        restore_file(backup, path)
        _record(
            "config_write_rollback", key=YAML_PREFIX + spec.path, backup=backup.name
        )
        raise EnvWriteError("verify_failed_rolled_back")
    # 成功事件由调用方记（web 带 source_ip/nonce）；这里只记失败回滚。
    logger.info("config.yaml updated: %s -> %s", spec.path, file_text)
    return WriteResult(YAML_PREFIX + spec.path, old_line, file_text, backup, "yaml")
