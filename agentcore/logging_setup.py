"""日志落盘初始化：控制台之外按天轮转（由 bot.py 启动时调用）。

抽成独立模块是为了**可测**：入口脚本 ``bot.py`` 在 import 期会做 NoneBot 初始化，
测试不便直接 import。评审 REVIEW-bbd8913..f6dffcc.md 的 M12 要求
「脏值 / 不可写目录不得让进程起不来」，这里把该行为做成可回归的纯函数。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from agentcore.boolenv import env_bool

logger = logging.getLogger(__name__)

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
KEEP_ENV = "AGENT_LOG_KEEP_DAYS"
DIR_ENV = "AGENT_LOG_DIR"
CONTENT_ENV = "AGENT_LOG_CONTENT"


def log_content_enabled() -> bool:
    """日志正文开关（``AGENT_LOG_CONTENT``，默认开）；逐条动态读 env。

    实现放在平台无关层，是为了让适配层与 agentcore 共用**同一份**判定：
    开关此前只存在于 ``plugins/qq_agent_adapter/matcher.py``，置 0 时引擎仍把
    长期事实与技能参数写进日志（REVIEW-ee39c8e..e55cab5 M5）。
    """
    # G（REVIEW 2026-11-02）：走 boolenv——旧手解把「认不出的脏值」静默按**开**
    # 处理（正文继续进日志），与关正文的隐私意图正好相反；boolenv 认不出时告警
    # 一次并回落默认值（默认开）。
    return env_bool(CONTENT_ENV, default=True)


def log_text(text: str, max_len: int | None = 200) -> str:
    """内容类日志的唯一出口：关闭正文时只留长度，正文永不出现在日志里。
    「内容」指对话/用户侧文本（消息、模型输出、技能参数等）；运维与配置回显类
    日志（如「config.yaml updated: k -> v」、人格文件名）不在此列。

        默认保持旧行为（记截断正文，运维靠它复盘）；``AGENT_LOG_CONTENT=0`` 供
        隐私敏感部署关闭。``max_len=None`` 表示诊断场景不截断（仍受开关约束）。
    """
    if not log_content_enabled():
        return f"<len={len(text)}>"
    if max_len is None or len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def bridge_loguru(handler: logging.Handler) -> bool:
    """把 loguru 的日志也送进 ``handler``（NoneBot 的框架日志走这条路）。

    2026-11-04 线上事故：`Running Matcher ... failed. AttributeError: 'coroutine'
    object has no attribute 'push'` 只出现在 journald，`data/logs/agent.log` 里
    **一条都没有**——NoneBot 的 logger 就是全局 loguru（`nonebot/log.py`:
    `logger = loguru.logger`），直接写 stdout，**不经过 `logging` 模块**，挂在
    root 上的文件 handler 天生收不到。故障因此隐形两个多小时，只能靠 DB 副作用反推。

    这里给 loguru 加一个 sink，把记录转成 `LogRecord` 交给同一个 handler：
    loguru 自己的 stdout sink 保持不动（控制台/journald 仍有），文件里多一份副本。
    任何失败只告警不抛——桥接是增益，不能影响启动。
    """
    try:
        import loguru

        def _sink(message) -> None:
            rec = message.record
            try:
                level = logging.getLevelName(str(rec["level"].name))
                if not isinstance(level, int):
                    level = logging.INFO
                record = logging.LogRecord(
                    str(rec["name"]),
                    level,
                    str(rec["file"].path),
                    int(rec["line"] or 0),
                    str(rec["message"]),
                    (),
                    rec["exception"],
                )
                handler.emit(record)
            except Exception:
                # 单条转换失败不能影响其它日志（loguru 默认也会兜住）
                pass

        loguru.logger.add(_sink, level="INFO")
        return True
    except Exception:
        logger.warning("loguru 日志桥接失败，框架日志仍只进控制台", exc_info=True)
        return False


def setup_file_logging(
    *,
    keep_raw: str | None = None,
    log_dir: str | Path | None = None,
    root: logging.Logger | None = None,
) -> logging.Handler | None:
    """启用按天轮转的文件日志；返回 handler（禁用或失败时返回 None，**绝不抛异常**）。

    - ``keep_raw`` 为 None 时读 ``AGENT_LOG_KEEP_DAYS``（默认 ``"14"``）；``0`` 表示关闭
    - 脏值 / 负数：告警后放弃落盘
    - 目录不可写 / handler 构造失败：告警后降级为仅控制台
    """
    target = root if root is not None else logging.getLogger()
    raw = keep_raw if keep_raw is not None else os.getenv(KEEP_ENV, "14")
    # 空串 = 未设置（走默认 14），而不是 int("")→静默关落盘
    raw = (str(raw) or "").strip() or "14"
    try:
        keep = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，已禁用日志落盘（仅控制台）", KEEP_ENV, raw)
        return None
    if keep <= 0:
        if keep < 0:
            logger.warning("%s=%r 为负数，已禁用日志落盘（仅控制台）", KEEP_ENV, raw)
        return None

    path = Path(log_dir if log_dir is not None else os.getenv(DIR_ENV, "data/logs"))
    try:
        path.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.TimedRotatingFileHandler(
            path / "agent.log", when="midnight", backupCount=keep, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(FORMAT))
        target.addHandler(handler)
        return handler
    except Exception:
        logger.warning("日志落盘初始化失败，降级为仅控制台输出", exc_info=True)
        return None
