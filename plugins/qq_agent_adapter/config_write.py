"""web 受控写入核心（D2-1）：``.env`` 手术式回写 + 运行态同步。

设计定稿（BACKLOG §1 D 组，2026-09-29 两轮交互）：

- **白名单锁死**：只有 :data:`WRITABLE` 里的键可写，运行期不可扩充；凭据与
  安全边界键（API_KEY / TOKEN / SUPERUSERS / 黑白名单等）**刻意不在表内**，
  那类变更必须走 SSH，否则"web 被打穿 → 换门锁"是完整攻击链。
- **持久化 = 改文件本身**：写 ``.env``（唯一落盘），优先级模型不变
  （env > config.yaml > 默认）；web 改的就是 env 层。
- **运行态同步**：本表白名单键全部是"调用时读 env"或 budget 可变属性——
  写文件后同步 ``os.environ`` / budget 属性即热生效；不引入任何新的
  优先级层，也不做 DB 覆盖。
- **值注入防御**：``.env`` 是行格式，值里出现换行等于注入任意新键，
  引号/``#`` 会改变 dotenv 解析边界——一律在落盘前拒绝。
- 备份 / 原子写 / 写后验证的编排由调用方（``web.py``）完成，本模块只出
  可单测的纯函数。
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

ENV_FILE_ENV = "AGENT_ENV_FILE"
# 与各读取点的解析口径对齐：vision/budget 的真值集合都接受 true（group_context
# 接受一切非 {"0","false","no","off"}），canonical 写 "true"/"false" 两边都认。
_TRUE_TEXT = "true"
_FALSE_TEXT = "false"
_FORBIDDEN_CHARS = ("\r", "\n", "#", '"', "'")
_BACKUP_KEEP = 10
_MASK_MAX = 120


@dataclass(frozen=True)
class KeySpec:
    key: str
    kind: str  # "bool" | "int" | "csv"
    min_value: int | None = None
    max_value: int | None = None
    max_items: int | None = None
    item_max_len: int | None = None
    budget_attr: str = ""  # 非空 = 写入后同步 budget 实例属性（热生效）
    description: str = ""


WRITABLE: dict[str, KeySpec] = {
    "AGENT_VISION": KeySpec("AGENT_VISION", "bool", description="图片识别总开关"),
    "AGENT_GROUP_CONTEXT": KeySpec(
        "AGENT_GROUP_CONTEXT", "bool", description="群聊上下文记录/注入"
    ),
    "AGENT_GROUP_CONTEXT_LINES": KeySpec(
        "AGENT_GROUP_CONTEXT_LINES", "int", 1, 100, description="群上下文保留条数"
    ),
    "AGENT_GROUP_CONTEXT_TTL": KeySpec(
        "AGENT_GROUP_CONTEXT_TTL", "int", 1, 604800, description="群上下文保留秒数"
    ),
    "AGENT_WAKE_WORDS": KeySpec(
        "AGENT_WAKE_WORDS",
        "csv",
        max_items=20,
        item_max_len=32,
        description="群聊唤醒词（逗号分隔）",
    ),
    "AGENT_BUDGET_DAILY_TOKENS": KeySpec(
        "AGENT_BUDGET_DAILY_TOKENS",
        "int",
        0,
        10**9,
        budget_attr="daily_tokens",
        description="日预算上限（0 = 不设限）",
    ),
    "AGENT_BUDGET_ENFORCE": KeySpec(
        "AGENT_BUDGET_ENFORCE",
        "bool",
        budget_attr="enforce",
        description="预算硬闸（超限拦截）",
    ),
}

# 群上下文两键的运行时承载是单例实例属性（record/snapshot 都读它们，不是
# os.environ）——apply_runtime 需要知道往哪个属性回写（REVIEW M2）
_GROUP_CONTEXT_ATTRS = {
    "AGENT_GROUP_CONTEXT_LINES": "max_lines",
    "AGENT_GROUP_CONTEXT_TTL": "ttl",
}


class ValueValidationError(ValueError):
    """值不合法：message 面向管理员，直接进 HTTP 422。"""


def validate_value(key: str, raw: object) -> tuple[KeySpec, object, str]:
    """校验并归一化。返回 ``(spec, normalized, file_text)``；不合法抛
    :class:`ValueValidationError`。

    ``file_text`` 是写进 .env 的规范字符串（与 normalized 语义等价，
    读取端解析回来必须得到同一个 normalized）。
    """
    spec = WRITABLE.get(key)
    if spec is None:
        raise ValueValidationError("key_not_writable")
    s = str(raw if raw is not None else "").strip()
    if not s:
        raise ValueValidationError("empty_value")
    if any(c in s for c in _FORBIDDEN_CHARS):
        # 行格式注入面：换行=注入任意新键；# 引号改变 dotenv 解析边界
        raise ValueValidationError("value_contains_forbidden_chars")
    if spec.kind == "bool":
        low = s.lower()
        if low not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise ValueValidationError("bool_expect_true_false")
        normalized = low in {"1", "true", "yes", "on"}
        return spec, normalized, _TRUE_TEXT if normalized else _FALSE_TEXT
    if spec.kind == "int":
        try:
            normalized = int(s)
        except ValueError:
            raise ValueValidationError("int_expect_integer") from None
        if spec.min_value is not None and normalized < spec.min_value:
            raise ValueValidationError("int_below_min")
        if spec.max_value is not None and normalized > spec.max_value:
            raise ValueValidationError("int_above_max")
        return spec, normalized, str(normalized)
    if spec.kind == "csv":
        items = [x.strip() for x in s.split(",") if x.strip()]
        if not items:
            raise ValueValidationError("csv_empty")
        if spec.max_items is not None and len(items) > spec.max_items:
            raise ValueValidationError("csv_too_many_items")
        if spec.item_max_len is not None and any(
            len(x) > spec.item_max_len for x in items
        ):
            raise ValueValidationError("csv_item_too_long")
        normalized = items
        return spec, normalized, ",".join(items)
    raise ValueValidationError("unknown_kind")  # pragma: no cover - 防未来漏加


def env_file_path() -> Path:
    """``.env`` 路径：``AGENT_ENV_FILE`` 可覆盖（测试注入），默认 CWD 下 .env——
    与 bot.py 的 ``load_dotenv()`` 默认搜索位置一致。"""
    return Path(os.getenv(ENV_FILE_ENV, ".env"))


def parse_env_value(text: str, key: str) -> str | None:
    """从 .env 文本取 KEY 的值（最后一次出现为准）；不存在返回 None。

    行模型只用 ``\\n`` 切（dotenv 与文件迭代同口径）：``splitlines()`` 还会切
    U+2028/\\x0c/\\x85 等不可见分隔符，会把单行合法值腰斩（REVIEW L3）。
    """
    value: str | None = None
    prefix = key + "="
    for line in text.split("\n"):
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
    return value


def locate_env_key(text: str, key: str) -> int:
    """KEY= 行的行号（0 基）；不存在返回 -1；重复出现返回 -2（歧义拒绝）。

    行模型与 :func:`parse_env_value` 一致（只按 ``\\n`` 切）。
    """
    hits = [i for i, line in enumerate(text.split("\n")) if line.startswith(key + "=")]
    if not hits:
        return -1
    if len(hits) > 1:
        return -2
    return hits[0]


def patch_env_text(text: str, key: str, file_text: str) -> tuple[str, str]:
    """手术式替换：只动目标行，其余字节原样保留。

    返回 ``(new_text, mode)``，mode 为 ``replace``（原地改）或 ``append``
    （键不存在，追加到文件尾并注释来源）。
    """
    idx = locate_env_key(text, key)
    if idx == -2:
        raise ValueValidationError("duplicate_key_lines")
    if idx == -1:
        base = text if text.endswith("\n") or not text else text + "\n"
        return (
            base + f"# via agent-web\n{key}={file_text}\n",
            "append",
        )
    lines = text.split("\n")  # 与 parse/locate 同一行模型，保住 CRLF 的 \r
    lines[idx] = f"{key}={file_text}" + ("\r" if lines[idx].endswith("\r") else "")
    return "\n".join(lines), "replace"


def backup_file(path: Path, backup_dir: Path) -> Path:
    """整文件快照到备份目录，滚动保留 :data:`_BACKUP_KEEP` 份。

    文件名带纳秒后缀：同一秒内连续写入（测试/连环改键）不互相覆盖；
    定宽零填充保证字典序 = 时间序，裁剪排序不失真。
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**6:06d}"
    target = backup_dir / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, target)
    backups = sorted(backup_dir.glob(f"{path.name}.*.bak"))
    for old in backups[:-_BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return target


def atomic_write(path: Path, text: str) -> None:
    """同目录临时文件 + fsync + ``os.replace``：任意时刻断电/崩溃不留半个文件。

    临时文件**创建即 0600**（``os.open`` 显式 mode）：open("w") 走 umask 会先以
    0644 承载完整 .env 新文本（含凭据）、chmod 在写入之后——崩溃/被杀就留下
    0644 残留（REVIEW M3 只修对了最终态，REVIEW-3ce6e0a..de09478 M1 堵窗口）。
    ``O_NOFOLLOW`` 防 tmp 名可预测被预置 symlink 截断目标。最终态继承原文件
    权限（0600 的 .env 不放宽）；文件不存在时（理论路径：写路由有 exists
    预检）保持 0600 兜底。
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    tmp = path.with_name(path.name + ".tmp.write")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def restore_file(backup: Path, path: Path) -> None:
    """写后验证失败时的还原路径（备份在验证之前生成，永不丢原值）。"""
    shutil.copy2(backup, path)


def read_env_text(path: Path) -> str:
    """newline=""：禁用 Python 通用换行翻译——否则 CRLF 文件会被整体静默转成
    LF，违背"只动目标行、其余字节原样保留"的承诺（审查探针实锤）。"""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def verify_env(path: Path, key: str, expected_file_text: str) -> bool:
    """写后验证：从磁盘重读并确认新值真的落上了。"""
    try:
        return parse_env_value(read_env_text(path), key) == expected_file_text
    except OSError:
        return False


def apply_runtime(spec: KeySpec, normalized: object, file_text: str) -> str:
    """运行态同步。本表白名单键全部可热生效，返回 ``live``。

    env 同步用 ``file_text``（文件与 os.environ 永远是同一字面值，drift 只可能
    来自进程外的手工 .env 修改）；budget 键额外同步实例属性（账本立即生效）；
    群上下文两键回写单例属性——``GroupContextBuffer`` 在 import 期固化
    max_lines/ttl（record/snapshot 都读实例属性），只改 os.environ 对缓冲无效
    （REVIEW M2：README "全部保存即生效" 的正确性依赖这次回写）。
    """
    os.environ[spec.key] = file_text
    if spec.budget_attr:
        from agentcore.budget import get_budget

        setattr(get_budget(), spec.budget_attr, normalized)
    attr = _GROUP_CONTEXT_ATTRS.get(spec.key)
    if attr:
        from plugins.qq_agent_adapter.group_context import group_context

        setattr(group_context, attr, normalized)
    return "live"


def masked(value: object) -> str:
    """审计用掩码口径：本期白名单键全非敏感，但机制先立——超长截断，
    形似凭据（sk- 前缀）只留形状。"""
    s = str(value if value is not None else "")
    if s.startswith("sk-"):
        return "***"
    return s[:_MASK_MAX]
