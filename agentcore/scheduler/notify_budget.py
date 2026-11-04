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

被全局预算**拦下**的投递另记在 ``blocked`` 段（同构、同窗口、同样封顶），
与 ``days`` 分开：被拦下的投递没有送达，混进 ``days`` 会让 ``spent`` 虚高、
把上限提前用尽。面板据此回答「今天有多少主动投递被预算挡下了」。
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


class Reservation:
    """一次预算预占的结算句柄（M13）。

    ``NotifyBudget.reserve`` 在锁内完成「判定 + 计数 +1」——投递还没发生，
    额度已经占住，并发流不可能同时看到同一个空位。调用方在投递有结果后二选一：

    - 确认送达：**什么都不做**（预占的计数正好是这次投递，``commit`` 只是标记）；
    - 失败 / ``uncertain``（没到用户手上）：``rollback()`` 归还额度。

    ``rollback`` 幂等且绝不抛（记账是弱约束，不能反噬投递）。
    """

    __slots__ = ("_budget", "_day", "_key", "_kind", "_settled")

    def __init__(self, budget: NotifyBudget, day: str, key: str, kind: str) -> None:
        self._budget = budget
        self._day = day
        self._key = key
        self._kind = kind
        self._settled = False

    def commit(self) -> None:
        """标记「这次预占对应一次确认送达」（计数已占，无需改动）。"""
        self._settled = True

    def rollback(self) -> None:
        """归还预占的额度（幂等；失败只记日志）。"""
        if self._settled:
            return
        self._settled = True
        try:
            with self._budget._lock:
                bucket = self._budget._days.get(self._day, {}).get(self._key)
                if not bucket:
                    return
                left = int(bucket.get(self._kind, 0)) - 1
                if left > 0:
                    bucket[self._kind] = left
                else:
                    bucket.pop(self._kind, None)
                    if not bucket:
                        self._budget._days.get(self._day, {}).pop(self._key, None)
                self._budget._save(self._day)
        except Exception:
            logger.warning(
                "notify_budget: 预占归还失败（%s %s）",
                self._key,
                self._kind,
                exc_info=True,
            )


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
        # 被全局预算拦下的计数（与 _days 同构，但不参与 spent：拦下=没送达）
        self._blocked: dict[str, dict[str, dict[str, int]]] = {}
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

        **并发窗口**：本方法只判定不占额度。投递方若需要严格的天花板（不许
        超发），用 :meth:`reserve`——它把「判定 + 计数」收进同一把锁。
        """
        if kind not in self.gated_kinds:
            return True
        return self.allowed(target, now)

    def reserve(
        self, target: str, kind: str, now: float | None = None
    ) -> tuple[bool, Reservation | None]:
        """原子地「判定 + 预占一格」（M13）。返回 ``(放行与否, 预占句柄)``。

        为什么不是 ``check`` + ``record``：三个闸门都在投递**前** check、送达
        **后** record，两步之间隔着整个 ``await sink.send_once(...)``——
        push / nudge / agent_turn 是三个独立 interval job、同一事件循环并发，
        同一目标多路同时到点时两边都看到 ``spent = limit-1`` → 双双放行 →
        超限 1。把判定和占用收进同一把锁，这个窗口关闭。

        句柄语义：投递**确认送达**后无需操作（预占的计数正好是这次投递）；
        失败或 ``uncertain``（没到用户手上）调 ``handle.rollback()`` 归还，
        与旧口径「只记确认送达」一致。非 gated 类型与 ``daily_limit<=0``
        （不限）都返回 ``(True, None)``——没有可占的额度，也不必结算。
        """
        if kind not in self.gated_kinds or self.daily_limit <= 0:
            return True, None
        ts = time.time() if now is None else now
        day = self.day_key(ts)
        key = str(target)
        with self._lock:
            spent = sum(self._days.get(day, {}).get(key, {}).values())
            if spent >= self.daily_limit:
                return False, None
            bucket = self._days.setdefault(day, {}).setdefault(key, {})
            bucket[kind] = int(bucket.get(kind, 0)) + 1
            self._prune(day)
            self._save(day)
            return True, Reservation(self, day, key, kind)

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

    def record_blocked(self, target: str, kind: str, now: float | None = None) -> None:
        """记一次**被全局预算拦下**的主动投递（只可能是 ``gated_kinds`` 三类）。

        与 ``record`` 分开记账：拦下 = 没有送达，混进 ``days`` 会让 ``spent`` 虚高、
        把上限提前用尽（自己把自己的额度占掉）。写失败同样只告警——记账是弱约束，
        不能反噬一次本该发生的投递判断。
        """
        ts = time.time() if now is None else now
        day = self.day_key(ts)
        key = str(target)
        try:
            with self._lock:
                bucket = self._blocked.setdefault(day, {}).setdefault(key, {})
                bucket[kind] = int(bucket.get(kind, 0)) + 1
                self._prune(day)
                self._save(day)
        except Exception:
            logger.warning(
                "notify_budget: 被拦计数失败（%s %s）", key, kind, exc_info=True
            )

    def blocked_by_kind(self, target: str, now: float | None = None) -> dict[str, int]:
        """某目标当日被全局预算拦下的分类型计数（没被拦过就是空 dict）。"""
        day = self.day_key(now)
        with self._lock:
            raw = self._blocked.get(day, {}).get(str(target), {})
            return {k: int(v) for k, v in raw.items() if int(v) > 0}

    def blocked_snapshot(self, now: float | None = None) -> dict:
        """当日被拦计数全量视图（总览面板「待决定区」用），按被拦次数降序。

        返回 ``{"day", "limit", "targets": {target: {"total": n, <kind>: n}}}``；
        ``limit=0`` 表示不限。目标是真实 QQ/群号，别进日志正文。
        """
        day = self.day_key(now)
        with self._lock:
            rows = {
                t: {
                    "total": sum(counts.values()),
                    **{k: int(v) for k, v in counts.items()},
                }
                for t, counts in self._blocked.get(day, {}).items()
            }
        return {
            "day": day,
            "limit": self.daily_limit,
            # 被拦最多的排前面：面板只有几行，先看「谁被挡得最狠」
            "targets": dict(
                sorted(rows.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
            ),
        }

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
            for bucket in (self._days, self._blocked):
                for stale in [k for k in bucket if not low <= k <= day]:
                    bucket.pop(stale, None)
        self._cap_targets(self._days.get(day), day, "记账")
        self._cap_targets(self._blocked.get(day), day, "被拦计数")

    def _cap_targets(
        self, targets: dict[str, dict[str, int]] | None, day: str, what: str
    ) -> None:
        """单日目标数封顶：超限时丢计数最少的，保留信息量最大的子集。

        ``days`` 与 ``blocked`` 两个桶共用：两者都是「按目标计数」的字典，
        封顶规则必须一致，否则坏手能挑容量大的那半边把文件撑爆。
        """
        if not targets or len(targets) <= MAX_TARGETS_PER_DAY:
            return
        ordered = sorted(targets.items(), key=lambda kv: sum(kv[1].values()))
        for target, _ in ordered[: len(targets) - MAX_TARGETS_PER_DAY]:
            targets.pop(target, None)
        logger.warning(
            "notify_budget: 当日%s目标数超过 %d，已丢弃最低频的一部分（%s）",
            what,
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
        if not isinstance(raw, dict):
            return
        self._days = self._clean_section(raw.get("days"))
        self._blocked = self._clean_section(raw.get("blocked"))
        self._prune(self.day_key())

    @staticmethod
    def _clean_section(section: object) -> dict[str, dict[str, dict[str, int]]]:
        """``{day: {target: {kind: n}}}`` 清洗：脏结构与非正整数计数一律丢弃。

        ``days`` 与 ``blocked`` 同构，共用一份清洗（手改 state 文件是允许的，
        但类型必须是这个形状）。
        """
        out: dict[str, dict[str, dict[str, int]]] = {}
        if not isinstance(section, dict):
            return out
        for day, targets in section.items():
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
                out[day] = bucket
        return out

    def _save(self, day: str) -> None:
        """原子写（.part + os.replace，0600）。失败只告警。"""
        payload = {
            "version": _STATE_VERSION,
            "days": {d: self._days[d] for d in sorted(self._days)},
            "blocked": {d: self._blocked[d] for d in sorted(self._blocked)},
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
