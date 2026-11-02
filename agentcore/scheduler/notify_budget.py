"""共享打扰预算（P3，2026-11-02）：同一目标同日的**无人值守主动投递**共用一个
计数器与一个全局上限。

## 为什么要有这个模块

四类主动投递原本各有一套上限，且互不知晓：

- ``push``（定时内容推送）：``AGENT_PUSH_DAILY_CAP_PER_TARGET``，默认 1/目标/日
- ``nudge``（沉默检测主动搭话）：``max_daily_total``，默认 3/目标/日
- ``agent_turn``（jobs.json 到点让 agent 跑一轮）：**完全没有上限**——一条
  ``* * * * *`` 的 job 就能把一个群每分钟刷一遍
- ``reminder``（用户自己预约的提醒）：一次性，用户显式要求，无全局闸

单独看每个面都很克制，合起来却可以给同一个目标一天发 20+ 条。本模块把四类投递
统一记到同一个按 (日, 目标, 类型) 计数的状态文件，并提供全局日上限
``AGENT_NOTIFY_DAILY_LIMIT``（默认 8，``0`` = 不限）。

## 记账与限流分离

- **记账**：四类全都记（含 ``remind``）。``/status`` 与面板因此能回答「今天给
  这个目标主动发过几条、都是什么类型」。
- **限流**：只拦 ``push`` / ``nudge`` / ``agent_turn`` 三类「无人值守主动搭话」
  （``GATED_KINDS``）。``remind`` 只记不拦——用户显式预约的提醒被全局预算吞掉
  是语义错误（他说了几点就是几点）。

## 与各面自有上限的关系

全局预算是**天花板**，不替代各面自己的细分上限：push 仍受
``daily_cap_per_target`` 管，nudge 仍受 ``max_daily_total`` 管，两者取先命中者。
被全局预算拦下时各面按自己的既有语义顺延（不重试、不停用、不打 WARNING 轰炸）。

## 状态文件

``data/state/notify-budget.json``（``AGENT_NOTIFY_BUDGET_FILE`` 可覆盖），0600、
原子写（``.part`` + ``os.replace``）。内容形如::

    {"version": 1, "days": {"2026-11-02": {"group:123": {"push": 1, "nudge": 2}}}}

只保留 ``KEEP_DAYS`` 天、单日最多 ``MAX_TARGETS_PER_DAY`` 个目标：这是记账文件，
不是账本，无限增长会在长时间运行的部署里变成一个隐藏问题。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import IO

from agentcore import tz

logger = logging.getLogger(__name__)

#: 状态文件默认位置（``AGENT_NOTIFY_BUDGET_FILE`` 覆盖）
DEFAULT_BUDGET_FILE = "data/state/notify-budget.json"

#: 记账类型。``remind`` 只记不限（见模块 docstring）。
KINDS = ("push", "nudge", "agent_turn", "remind")

#: 受全局上限约束的投递类型
GATED_KINDS = frozenset({"push", "nudge", "agent_turn"})

#: 状态文件里保留的天数（含今天）：按日清理，防止文件无限增长。
KEEP_DAYS = 3

#: 单日目标数上限：防「给上万个群各发一条」把状态文件撑爆。
MAX_TARGETS_PER_DAY = 2000

_STATE_VERSION = 1


def _env_int(name: str, default: int) -> int:
    """整数 env：脏值告警后回退默认，并钳制下限（与 push 的 _env_int 同款）。"""
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退 %d", name, raw, default)
        return default
    return max(0, value)


def open_part_0600(part: Path) -> IO[bytes]:
    """创建即 0600 的 ``.part`` 写入口（C4，REVIEW 2026-11-02）。

    原实现 ``write_text`` → ``chmod`` → ``replace``：``write_text`` 走 umask 先落
    0644，``chmod`` 之前那一跳对同机其它本地用户可读；这些状态里含 target（真实
    QQ/群号）与投递计数。push / nudge / 本模块三处原各有一份私抄，P3 合并到这里。
    """
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        # Windows 无 POSIX 权限语义（只读位除外）：别让写入因此失败
        logger.debug("notify_budget: fchmod 0600 skipped for %s", part, exc_info=True)
    return os.fdopen(fd, "wb")


class NotifyBudget:
    """按 (日, 目标, 类型) 计数的打扰预算账本。

    ``path`` 优先于 ``AGENT_NOTIFY_BUDGET_FILE``，两者都没有才退回
    ``DEFAULT_BUDGET_FILE``。``daily_limit=None`` 时读
    ``AGENT_NOTIFY_DAILY_LIMIT``（默认 8，``0`` = 不限）。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        daily_limit: int | None = None,
        gated_kinds=GATED_KINDS,
    ) -> None:
        env = (os.getenv("AGENT_NOTIFY_BUDGET_FILE") or "").strip()
        # 显式形参赢过部署 env：调用方给了路径就是要这个路径（库内复用 / 测试），
        # env 是给「不传参的生产装配」用的覆盖层。
        self.path: Path = (
            Path(path) if path else Path(env) if env else Path(DEFAULT_BUDGET_FILE)
        )
        self.daily_limit: int = (
            _env_int("AGENT_NOTIFY_DAILY_LIMIT", 8)
            if daily_limit is None
            else max(0, int(daily_limit))
        )
        self.gated_kinds = frozenset(gated_kinds)
        self._lock = threading.RLock()
        # day -> target -> kind -> count
        self._days: dict[str, dict[str, dict[str, int]]] = {}
        self._load()

    # ------------------------------------------------------------------ 读

    def day_key(self, now: float | None = None) -> str:
        """日键与 ``tz.day_key`` 同源（日界漂移曾让 UTC 服务器上「每日上限」到
        本地早 8 点才重置）。"""
        return tz.day_key(time.time() if now is None else now)

    def by_kind(self, target: str, now: float | None = None) -> dict[str, int]:
        """某目标当日的分类型计数（没记过就是空 dict）。"""
        day = self.day_key(now)
        with self._lock:
            raw = self._days.get(day, {}).get(str(target), {})
            return {k: int(v) for k, v in raw.items() if int(v) > 0}

    def spent(self, target: str, now: float | None = None) -> int:
        """某目标当日的主动投递总条数（含 ``remind``）。"""
        return sum(self.by_kind(target, now).values())

    def allowed(self, target: str, now: float | None = None) -> bool:
        """全局上限放行与否（``daily_limit<=0`` = 不限）。"""
        if self.daily_limit <= 0:
            return True
        return self.spent(target, now) < self.daily_limit

    def remaining(self, target: str, now: float | None = None) -> int:
        """当日余量；``-1`` 表示不限。"""
        if self.daily_limit <= 0:
            return -1
        return max(0, self.daily_limit - self.spent(target, now))

    def check(self, target: str, kind: str, now: float | None = None) -> bool:
        """``kind`` 这一次投递放不放行。

        非 ``gated_kinds`` 的类型（当前只有 ``remind``）恒 True——只记账不拦。
        """
        if kind not in self.gated_kinds:
            return True
        return self.allowed(target, now)

    def snapshot(self, now: float | None = None) -> dict:
        """当日全量视图（``/status`` 与总览面板用）。

        返回 ``{"day", "limit", "targets": {target: {"total": n, <kind>: n}}}``；
        ``limit=0`` 表示不限。目标是真实 QQ/群号，别进日志正文。
        """
        day = self.day_key(now)
        with self._lock:
            targets = self._days.get(day, {})
            return {
                "day": day,
                "limit": self.daily_limit,
                "targets": {
                    t: {
                        "total": sum(counts.values()),
                        **{k: int(v) for k, v in counts.items()},
                    }
                    for t, counts in sorted(targets.items())
                },
            }

    # ------------------------------------------------------------------ 写

    def record(self, target: str, kind: str, now: float | None = None) -> None:
        """记一次主动投递。任何失败都不影响投递本身（记账是弱约束）。"""
        ts = time.time() if now is None else now
        day = self.day_key(ts)
        key = str(target)
        try:
            with self._lock:
                bucket = self._days.setdefault(day, {}).setdefault(key, {})
                bucket[kind] = int(bucket.get(kind, 0)) + 1
                self._prune(day)
                self._save(day)
        except Exception:
            # 预算记账失败绝不能让一次已经成功的投递变成异常
            logger.warning("notify_budget: 记账失败（%s %s）", key, kind, exc_info=True)

    # ------------------------------------------------------------------ 内部

    def _prune(self, day: str) -> None:
        """只留以 ``day`` 结尾的 ``KEEP_DAYS`` 天窗口，并给单日目标数封顶。

        窗口以 ``day`` 为锚点算，**不是**「排序取最后 N 天」：后者遇到未来的日键
        （时钟跳快 / 测试注入的时间戳）会把今天整桶挤出去——记账静默丢失，
        上限凭空变松（实测：一份带 11-06/11-16 的 state 文件让当天的 push 计数
        每次 record 后就被清掉，每日上限完全失效）。

        未来日键与过期日键一起丢：这是防骚扰的天花板，丢计数只会让它更宽松，
        绝不会把该发的消息拦下来；而文件大小因此有硬上界。
        """
        try:
            anchor = dt.date.fromisoformat(day)
        except ValueError:
            # 脏日键（手改 state 文件）：不动其他天，只封顶当天目标数
            anchor = None
        if anchor is not None:
            low = (anchor - dt.timedelta(days=KEEP_DAYS - 1)).isoformat()
            for stale in [k for k in self._days if not low <= k <= day]:
                self._days.pop(stale, None)
        targets = self._days.get(day)
        if targets and len(targets) > MAX_TARGETS_PER_DAY:
            # 超限时丢计数最少的：保留「最常被打扰的目标」这个信息量最大的子集
            ordered = sorted(targets.items(), key=lambda kv: sum(kv[1].values()))
            for target, _ in ordered[: len(targets) - MAX_TARGETS_PER_DAY]:
                targets.pop(target, None)
            logger.warning(
                "notify_budget: 当日目标数超过 %d，已丢弃最低频的一部分（%s）",
                MAX_TARGETS_PER_DAY,
                day,
            )

    def _load(self) -> None:
        """读回状态；损坏按空处理（最坏是当天上限宽松一点，绝不因此起不来）。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            logger.warning("notify_budget: 状态文件损坏，按空处理（%s）", self.path)
            return
        days = raw.get("days") if isinstance(raw, dict) else None
        if not isinstance(days, dict):
            return
        for day, targets in days.items():
            if not isinstance(day, str) or not isinstance(targets, dict):
                continue
            bucket: dict[str, dict[str, int]] = {}
            for target, counts in targets.items():
                if not isinstance(counts, dict):
                    continue
                clean = {
                    str(k): int(v)
                    for k, v in counts.items()
                    if isinstance(v, int) and int(v) > 0
                }
                if clean:
                    bucket[str(target)] = clean
            if bucket:
                self._days[day] = bucket
        self._prune(self.day_key())

    def _save(self, day: str) -> None:
        """原子写（.part + os.replace，0600）。失败只告警。"""
        payload = {
            "version": _STATE_VERSION,
            "days": {d: self._days[d] for d in sorted(self._days)},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            part = Path(f"{self.path}.part")
            with open_part_0600(part) as fh:
                fh.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            os.replace(part, self.path)
        except OSError:
            logger.warning(
                "notify_budget: 状态写入失败（%s）", self.path, exc_info=True
            )


_default: NotifyBudget | None = None
_default_lock = threading.Lock()


def get_notify_budget() -> NotifyBudget:
    """进程级共享实例（懒建：env 在 import 之后才设置的部署也能读到手）。"""
    global _default
    with _default_lock:
        if _default is None:
            _default = NotifyBudget()
        return _default
