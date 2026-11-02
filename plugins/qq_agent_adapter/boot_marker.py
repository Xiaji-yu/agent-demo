"""启动标记：把「这一进程跑的是哪个提交」落成 ``data/state/boot.json``。

存在的理由：

- **回答「现在跑的是哪条基线」**。``/reboot`` 用 ``os.execve`` 自替换，PID 不变、
  ``systemctl show`` 的启动时间也不变——2026-11-02 排查时正是靠 journal 里的
  reboot 行才推出来的。有了这个文件，答案是一个 JSON。
- **给 ``/update`` 当健康信号**：更新脚本轮询它，看到 pid 变化且 sha 等于目标，
  才算「新代码真的起来了」；等不到就回滚。

写入失败只告警不抛：版本指纹缺失不是启动失败的理由（与 ``build_info`` 同口径）。
路径固定在 ``data/state/``（已 gitignore），不接受 env 覆盖——少一个配置面。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from agentcore import build_info

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("data/state/boot.json")


def boot_marker_path() -> Path:
    return DEFAULT_PATH


def write_boot_marker(path: Path | None = None, *, now: float | None = None) -> bool:
    """原子写启动标记，返回是否成功。"""
    target = path or boot_marker_path()
    payload = {
        "pid": os.getpid(),
        "sha": build_info.git_sha(),
        "branch": build_info.git_branch(),
        "version": build_info.version(),
        "started_at": time.time() if now is None else float(now),
    }
    tmp: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".boot-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        # 与 logs/2fa 同口径：该文件虽不含正文，但暴露部署版本与进程号
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        return True
    except Exception:
        logger.warning("boot marker write failed（版本指纹不可用）", exc_info=True)
        return False
    finally:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
