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

logger = logging.getLogger(__name__)

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
KEEP_ENV = "AGENT_LOG_KEEP_DAYS"
DIR_ENV = "AGENT_LOG_DIR"


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
    raw = (raw or "0").strip()
    try:
        keep = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，已禁用日志落盘（仅控制台）", KEEP_ENV, raw)
        return None
    if keep <= 0:
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
