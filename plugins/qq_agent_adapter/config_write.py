"""web 受控写入核心（D2-1）：``.env`` 手术式回写 + 运行态同步。

设计定稿（BACKLOG §1 D 组，2026-09-29 两轮交互）：

- **白名单**：可写清单与元数据在 :mod:`settings_spec`（单一事实源）。凭据、
  安全边界、数据保留期、日志正文开关、SSRF 白名单与结构路径**刻意不可写**，
  那类变更必须走 SSH，否则"web 被打穿 → 换门锁"是完整攻击链。
- **持久化 = 改文件本身**：写 ``.env``（唯一落盘），优先级模型不变
  （env > config.yaml > 默认）；web 改的就是 env 层。
- **运行态同步**：``spec.live=True`` 的键（调用时读 env，或 budget/群上下文
  这类有专门回写钩子的）保存即生效；其余返回 ``boot``，由 UI 提示重启。
  不引入任何新的优先级层，也不做 DB 覆盖。
- **值注入防御**：``.env`` 是行格式，值里出现换行等于注入任意新键，
  引号/``#`` 会改变 dotenv 解析边界——一律在落盘前拒绝。
- 备份 / 原子写 / 写后验证的编排由调用方（``web.py``）完成，本模块只出
  可单测的纯函数。
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .settings_spec import ENV_SPECS, EnvSpec

logger = logging.getLogger(__name__)

ENV_FILE_ENV = "AGENT_ENV_FILE"
# 与各读取点的解析口径对齐：vision/budget 的真值集合都接受 true（group_context
# 接受一切非 {"0","false","no","off"}），canonical 写 "true"/"false" 两边都认。
_TRUE_TEXT = "true"
_FALSE_TEXT = "false"
_FORBIDDEN_CHARS = ("\r", "\n", "#", '"', "'")
_BACKUP_KEEP = 10
_MASK_MAX = 120


# 白名单与元数据来自 settings_spec（单一事实源）；这里只保留"怎么写"。
KeySpec = EnvSpec
WRITABLE: dict[str, KeySpec] = {s.key: s for s in ENV_SPECS if s.writable}

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
    if spec.kind == "float":
        try:
            normalized = float(s)
        except ValueError:
            raise ValueValidationError("float_expect_number") from None
        if spec.min_value is not None and normalized < spec.min_value:
            raise ValueValidationError("float_below_min")
        if spec.max_value is not None and normalized > spec.max_value:
            raise ValueValidationError("float_above_max")
        return spec, normalized, repr(normalized)
    if spec.kind == "str":
        if spec.max_len is not None and len(s) > spec.max_len:
            raise ValueValidationError("str_too_long")
        if spec.validator is not None:
            try:
                return spec, s, spec.validator(s)
            except ValueError as e:
                # 键专属校验（如 LLM_MODEL 的条目语法）自带面向管理员的原因
                raise ValueValidationError(str(e)) from None
        return spec, s, s
    raise ValueValidationError("unknown_kind")  # pragma: no cover - 防未来漏加


def env_file_path() -> Path:
    """``.env`` 路径：``AGENT_ENV_FILE`` 可覆盖（测试注入），默认 CWD 下 .env——
    与 bot.py 的 ``load_dotenv()`` 默认搜索位置一致。"""
    return Path(os.getenv(ENV_FILE_ENV, ".env"))


def backup_dir() -> Path:
    """改前快照目录（测试经 ``AGENT_WEB_BACKUP_DIR`` 注入 tmp）。"""
    return Path(os.getenv("AGENT_WEB_BACKUP_DIR", "data/backups/config"))


class EnvWriteError(RuntimeError):
    """写 .env 失败（IO / 写后验证未过）。message 面向管理员。"""


@dataclass(frozen=True)
class WriteResult:
    key: str
    old: str | None  # 改前的值（键不存在时为 None）
    new: str  # 落盘的规范文本
    backup: Path
    mode: str  # "replace" | "append"


def write_env_key(spec: KeySpec, file_text: str) -> WriteResult:
    """把白名单键写进 ``.env``：备份 → 手术式补丁 → 原子写 → 写后验证 → 还原。

    web 写面与 QQ ``/model`` 命令共用这一套，因为两边必须守同一组不变量：
    symlink fail-closed（受管 secrets 部署下写链接本体=改了却没改）、0600
    原子写（不留半个文件/不留 0644 残留）、只动目标行（CRLF 与注释原样）、
    写后验证失败即还原并留审计。

    函数内**没有 await**：在单事件循环里它就是原子的，不需要额外的写锁——
    两个调用方（web 路由 / 命令 handler）不可能交错进这段临界区。
    """
    from agentcore.diagnostics import record as _record

    path = env_file_path()
    if path.is_symlink():
        # symlink .env（受管 secrets/部署符号链接）：直接写会替换链接本体、
        # 真实目标不更新而接口还返回 ok——REVIEW M4 复现实锤，fail-closed
        raise ValueValidationError("env_file_is_symlink")
    if not path.exists():
        raise EnvWriteError("env_file_missing")
    try:
        old_text = read_env_text(path)
    except OSError as e:
        raise EnvWriteError(f"env_read: {type(e).__name__}") from e
    backup = backup_file(path, backup_dir())
    try:
        new_text, mode = patch_env_text(old_text, spec.key, file_text)
    except ValueValidationError:
        # 定位歧义（如 .env 里同键重复出现）是**合法请求撞上脏文件**，
        # 还原并让调用方按 409 让管理员手工处理
        restore_file(backup, path)
        raise
    atomic_write(path, new_text)
    if not verify_env(path, spec.key, file_text):
        # 最后防线：写上了却读不回来 → 立即还原并留痕
        restore_file(backup, path)
        _record("config_write_rollback", key=spec.key, backup=backup.name)
        raise EnvWriteError("verify_failed_rolled_back")
    old_value = parse_env_value(old_text, spec.key)
    # 成功事件由调用方记（web 要带 source_ip/confirm_nonce，QQ /model 要带 by），
    # 这里只负责失败回滚的留痕，避免同一次写入产生两条口径不同的 config_write。
    return WriteResult(spec.key, old_value, file_text, backup, mode)


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

    ``LLM_MODEL`` 回写共享 ``LLMClient`` 的活跃位：模块级 ``_CFG`` 在 import
    期装配，只改 os.environ 对运行中的进程无效。同步失败只记 ERROR 不回滚——
    文件已是权威值，重启后自然一致，但"改了却没生效"必须留痕。
    """
    os.environ[spec.key] = file_text
    if spec.budget_attr:
        from agentcore.budget import get_budget

        setattr(get_budget(), spec.budget_attr, normalized)
    attr = _GROUP_CONTEXT_ATTRS.get(spec.key)
    if attr:
        from plugins.qq_agent_adapter.group_context import group_context

        setattr(group_context, attr, normalized)
    if spec.key == "LLM_MODEL":
        try:
            _sync_llm_active(file_text)
        except Exception:
            logger.exception("LLM_MODEL 运行态同步失败（重启后生效）")
    # live=False 的键是启动期装配：文件已改，但进程内对象要到重启才重建。
    # 不能谎报 live——设置页据此提示"重启生效"，用户才知道现在还没变。
    return "live" if spec.live else "boot"


def _sync_llm_active(file_text: str) -> None:
    """把 ``LLM_MODEL`` 的规范文本套到共享 LLMClient 的活跃位上。"""
    from agentcore.llm.client import parse_model_spec
    from agentcore.skills.registry import get_shared_llm_client

    get_shared_llm_client().set_active(parse_model_spec(file_text, allow_key=False))


def masked(value: object) -> str:
    """审计用掩码口径：本期白名单键全非敏感，但机制先立——超长截断，
    形似凭据（sk- 前缀）只留形状。"""
    s = str(value if value is not None else "")
    if s.startswith("sk-"):
        return "***"
    return s[:_MASK_MAX]
