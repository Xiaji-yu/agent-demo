"""人格改前备份的共用落点（web 写面与 QQ 命令通道共用，M7 抽取）。

从 plugins/qq_agent_adapter/web.py 原样下沉：人格域逻辑本就该在 agentcore
（分层铁律），且 QQ 通道（REVIEW-63f9444..858afcc M7）此前完全没有改前备份、
回执却谎称有——两个通道必须同一套备份语义（唯一名 / 0600 / 滚动保留）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

PERSONA_BAK_KEEP = 5
_bak_seq = 0


def persona_backup_path(bdir: Path, name: str) -> Path:
    """人格改前备份落点：秒级时间戳 + pid + 进程内单调序号的唯一名。

    上轮遗留 M1：旧实现纯秒级时间戳——同秒两次保存（web 连点/脚本重放）
    后者覆盖前者，改前备份只剩最后一份。仅靠 while-exists 探测不够：轮转删掉
    旧份后计数器会复位，跨进程重启更是从零开始，故把 pid 与单调序号编进名字。
    """
    global _bak_seq
    stamp = time.strftime("%Y%m%d-%H%M%S")
    while True:
        _bak_seq += 1
        candidate = bdir / f"{name}.md.{stamp}.{os.getpid()}.{_bak_seq}.bak"
        if not candidate.exists():
            return candidate


def atomic_copy_0600(src: Path, dst: Path) -> None:
    """复制为 0600：``os.open`` 显式 mode 一步到位（无 write_bytes 的 0644 窗口），
    O_NOFOLLOW 防符号链接替换（上轮遗留 M1）。"""
    data = src.read_bytes()
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def prune_persona_backups(bdir: Path, name: str) -> None:
    """每个人格只保留最近 PERSONA_BAK_KEEP 份 .bak（按 mtime），其余删
    （上轮遗留 M1：旧实现无滚动，personas/ 备份目录无界增长）。"""
    olds = sorted(
        bdir.glob(f"{name}.md.*.bak"), key=lambda p: p.stat().st_mtime, reverse=True
    )[PERSONA_BAK_KEEP:]
    for p in olds:
        p.unlink(missing_ok=True)
