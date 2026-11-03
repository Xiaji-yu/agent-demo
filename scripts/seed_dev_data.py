#!/usr/bin/env python
"""往**开发库**灌确定性合成数据：会话 / 消息 / 长期事实 / KB 块。

为什么需要：星云视图、时间衰减、召回排序在空库上看不出效果；而复制生产数据会
带来第二份隐私副本与备份责任（AGENTS §3「测试库必须是空库」）。这里的内容全部
虚构，seed 固定即可复现同一批数据。

安全边界（这些不是建议，是硬门）：

- 只认库名含 ``dev`` / ``scratch`` / ``test`` 的库；生产库名（如 ``qqagent``）直接拒绝；
- 必须显式 ``--yes``；
- 不读生产目录、不写 ``data/``；事实的 ``source`` 固定为 ``dev_seed``，便于清理。

用法::

    python scripts/seed_dev_data.py --yes
    python scripts/seed_dev_data.py --yes --truncate --users 50 --facts 3000
    python scripts/seed_dev_data.py --help
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_MARKERS = ("dev", "scratch", "test")
FACT_SOURCE = "dev_seed"
FAKE_QQ_BASE = 700_000_000
FAKE_GROUP_BASE = 800_000_000

_CITIES = ("江南市", "临海市", "北岭市", "青川市", "雾港市")
_FOODS = ("手冲咖啡", "螺蛳粉", "番茄牛腩", "梅干菜扣肉", "冰粉")
_PROJECTS = ("一个 QQ 机器人", "一个记账脚本", "一个静态博客", "一个小爬虫")
_SKILLS = ("Rust", "Go", "Kotlin", "SQL", "TypeScript")
_PETS = ("团子", "年糕", "布丁", "麻薯")

_FACT_TMPL = (
    "用户住在{city}",
    "用户喜欢{food}",
    "用户正在做{project}",
    "用户最近在学{skill}",
    "用户养了一只叫{pet}的猫",
    "用户对{skill}比较熟",
    "用户不太喜欢太甜的{food}",
)
_USER_MSG = (
    "今天在忙{project}",
    "推荐个{food}？",
    "{city}这两天天气怎么样",
    "最近在学{skill}，有点难",
    "我家{pet}又把杯子碰倒了",
)
_BOT_MSG = (
    "听起来不错，{food}我也喜欢",
    "那{project}进展怎么样了",
    "{city}那边应该挺舒服的",
    "{skill}的话，先从一个小项目开始",
    "猫叫{pet}，名字挺可爱",
)


# ---------------------------------------------------------------- 纯逻辑（可单测）


def is_dev_database(dbname: str) -> bool:
    """库名是否明确属于开发/临时用途。生产库名不带这些标记 → 拒绝。"""
    return any(marker in str(dbname).lower() for marker in DEV_MARKERS)


def database_name(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).path.lstrip("/")


def guard_url(url: str) -> str | None:
    """返回拒绝原因；None 表示可以继续。"""
    if not url.strip():
        return "缺少 DATABASE_URL（或用 --url 指定）"
    name = database_name(url)
    if not name:
        return "DATABASE_URL 里没有库名"
    if not is_dev_database(name):
        return (
            f"库名 {name!r} 看起来不是开发库：只允许名字含 dev / scratch / test 的库；"
            "生产库一律拒绝"
        )
    return None


def sample_age_days(rng: random.Random, days: float) -> float:
    """时间分布：一半在 30 天内、三成在 30–180 天、两成更旧。"""
    span = max(1.0, float(days))
    roll = rng.random()
    if roll < 0.5:
        return rng.uniform(0.0, min(30.0, span))
    if roll < 0.8:
        return rng.uniform(min(30.0, span), min(180.0, span))
    return rng.uniform(min(180.0, span), span)


def _random_vec(rng: random.Random, dim: int) -> list[float]:
    vec = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _fmt_vec(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _fill(rng: random.Random, template: str) -> str:
    return template.format(
        city=rng.choice(_CITIES),
        food=rng.choice(_FOODS),
        project=rng.choice(_PROJECTS),
        skill=rng.choice(_SKILLS),
        pet=rng.choice(_PETS),
    )


def build_dataset(
    *,
    seed: int,
    users: int,
    sessions_per_user: int,
    facts: int,
    messages: int,
    kb_sources: int,
    kb_chunks: int,
    days: float,
    dim: int,
    now: float | None = None,
) -> dict:
    """生成整批数据（不碰数据库，便于确定性单测）。"""
    rng = random.Random(seed)
    now = time.time() if now is None else float(now)

    sessions: list[dict] = []
    for u in range(max(1, users)):
        user_id = str(FAKE_QQ_BASE + u)
        sessions.append(
            {
                "user_id": user_id,
                "group_id": None,
                "scope": "private",
                "created_at": now - rng.uniform(0.0, max(1.0, days)) * 86400,
            }
        )
        for g in range(max(0, sessions_per_user - 1)):
            if rng.random() < 0.35:
                continue  # 不是每个用户都在群里，分布更像真实情况
            sessions.append(
                {
                    "user_id": user_id,
                    "group_id": str(FAKE_GROUP_BASE + u * 10 + g),
                    "scope": "group",
                    "created_at": now - rng.uniform(0.0, max(1.0, days)) * 86400,
                }
            )
    if not sessions:
        raise SystemExit("users 至少为 1")

    msgs: list[dict] = []
    for i in range(max(0, messages)):
        s = sessions[i % len(sessions)]
        role = "user" if rng.random() < 0.55 else "assistant"
        pool = _USER_MSG if role == "user" else _BOT_MSG
        msgs.append(
            {
                "session_index": i % len(sessions),
                "role": role,
                "content": _fill(rng, rng.choice(pool)),
                "created_at": now - rng.uniform(0.0, max(1.0, days)) * 86400,
            }
        )
    msgs.sort(key=lambda m: m["created_at"])

    facts_rows: list[dict] = []
    for i in range(max(0, facts)):
        s = sessions[i % len(sessions)]
        age = sample_age_days(rng, days)
        facts_rows.append(
            {
                "session_index": i % len(sessions),
                "user_id": s["user_id"],
                "content": _fill(rng, rng.choice(_FACT_TMPL)),
                "embedding": _fmt_vec(_random_vec(rng, dim)),
                "source": FACT_SOURCE,
                "age_days": age,
                "created_at": now - age * 86400,
            }
        )

    sources: list[dict] = []
    for i in range(max(0, kb_sources)):
        sources.append(
            {
                "name": f"开发样本手册 {i + 1}",
                "kind": "manual",
                "location": f"dev://synthetic/{i + 1}",
            }
        )
    chunks: list[dict] = []
    for i in range(max(0, kb_chunks)):
        if not sources:
            break
        chunks.append(
            {
                "source_index": i % len(sources),
                "chunk": f"这是一段合成的知识库样例内容 #{i + 1}：用于撑起星云与检索的体量。",
                "embedding": _fmt_vec(_random_vec(rng, dim)),
            }
        )

    return {
        "sessions": sessions,
        "messages": msgs,
        "facts": facts_rows,
        "kb_sources": sources,
        "kb_chunks": chunks,
        "now": now,
    }


def tier_counts(facts: list[dict], now: float) -> dict[str, int]:
    """按 decay.retention 的口径粗分一档，方便确认三种时间跨度都覆盖到了。"""
    from agentcore.memory import decay

    out = {"hot": 0, "warm": 0, "cold": 0}
    for f in facts:
        age = (now - float(f["created_at"])) / 86400.0
        out[decay.tier(age)] += 1
    return out


# ---------------------------------------------------------------- 写库


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=UTC)


async def _seed(url: str, dim: int, data: dict, *, truncate: bool) -> None:
    from agentcore.memory.store import PgMemoryStore

    store = PgMemoryStore(url, dim=dim)
    await store.init()
    try:
        async with store.pool.acquire() as conn:
            async with conn.transaction():
                if truncate:
                    await conn.execute(
                        "TRUNCATE messages, facts, sessions, kb_chunks, kb_sources "
                        "RESTART IDENTITY CASCADE"
                    )
                session_ids: list[int] = []
                for s in data["sessions"]:
                    sid = await conn.fetchval(
                        "INSERT INTO sessions(user_id, group_id, scope, policy, created_at) "
                        "VALUES($1, $2, $3, 'per_user', $4) RETURNING id",
                        s["user_id"],
                        s["group_id"],
                        s["scope"],
                        _dt(s["created_at"]),
                    )
                    session_ids.append(int(sid))

                if data["messages"]:
                    await conn.executemany(
                        "INSERT INTO messages(session_id, role, content, created_at) "
                        "VALUES($1, $2, $3, $4)",
                        [
                            (
                                session_ids[m["session_index"]],
                                m["role"],
                                m["content"],
                                _dt(m["created_at"]),
                            )
                            for m in data["messages"]
                        ],
                    )

                if data["facts"]:
                    await conn.executemany(
                        "INSERT INTO facts(session_id, user_id, content, embedding, "
                        "source, created_at) VALUES($1, $2, $3, $4::vector, $5, $6)",
                        [
                            (
                                session_ids[f["session_index"]],
                                f["user_id"],
                                f["content"],
                                f["embedding"],
                                f["source"],
                                _dt(f["created_at"]),
                            )
                            for f in data["facts"]
                        ],
                    )

                source_ids: list[int] = []
                for s in data["kb_sources"]:
                    sid = await conn.fetchval(
                        "INSERT INTO kb_sources(name, kind, location) "
                        "VALUES($1, $2, $3) RETURNING id",
                        s["name"],
                        s["kind"],
                        s["location"],
                    )
                    source_ids.append(int(sid))
                if data["kb_chunks"]:
                    await conn.executemany(
                        "INSERT INTO kb_chunks(source_id, chunk, embedding) "
                        "VALUES($1, $2, $3::vector)",
                        [
                            (
                                source_ids[c["source_index"]],
                                c["chunk"],
                                c["embedding"],
                            )
                            for c in data["kb_chunks"]
                        ],
                    )
    finally:
        await store.aclose()


# ---------------------------------------------------------------- CLI


def _read_env_file() -> dict[str, str]:
    path = REPO_ROOT / ".env"
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _resolve_url(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    env = os.environ.get("DATABASE_URL", "").strip()
    if env:
        return env
    return _read_env_file().get("DATABASE_URL", "").strip()


def _resolve_dim(explicit: int | None) -> int:
    if explicit:
        return int(explicit)
    raw = os.environ.get("EMBEDDING_DIM") or _read_env_file().get("EMBEDDING_DIM") or ""
    try:
        return int(raw)
    except ValueError:
        return 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="往开发库灌合成数据")
    parser.add_argument("--url", help="开发库连接串（默认取 DATABASE_URL 或 .env）")
    parser.add_argument("--dim", type=int, help="向量维度（默认 EMBEDDING_DIM）")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--sessions-per-user", type=int, default=2)
    parser.add_argument("--facts", type=int, default=800)
    parser.add_argument("--messages", type=int, default=1200)
    parser.add_argument("--kb-sources", type=int, default=3)
    parser.add_argument("--kb-chunks", type=int, default=200)
    parser.add_argument("--days", type=float, default=720.0, help="数据时间跨度（天）")
    parser.add_argument("--seed", type=int, default=20261103)
    parser.add_argument("--truncate", action="store_true", help="先清空业务表")
    parser.add_argument("--yes", action="store_true", help="确认目标确实是开发库")
    args = parser.parse_args(argv)

    url = _resolve_url(args.url)
    reason = guard_url(url)
    if reason:
        print(f"[拒绝] {reason}", file=sys.stderr)
        return 2
    if not args.yes:
        print(
            f"[需要确认] 将向 {database_name(url)} 写入合成数据；确认后加 --yes",
            file=sys.stderr,
        )
        return 2

    dim = _resolve_dim(args.dim)
    data = build_dataset(
        seed=args.seed,
        users=args.users,
        sessions_per_user=args.sessions_per_user,
        facts=args.facts,
        messages=args.messages,
        kb_sources=args.kb_sources,
        kb_chunks=args.kb_chunks,
        days=args.days,
        dim=dim,
    )
    print(
        f"[seed] db={database_name(url)} dim={dim} seed={args.seed} "
        f"users={args.users} sessions={len(data['sessions'])} "
        f"messages={len(data['messages'])} facts={len(data['facts'])} "
        f"kb={len(data['kb_sources'])}/{len(data['kb_chunks'])}"
    )
    print(f"[tiers] {tier_counts(data['facts'], data['now'])}")
    asyncio.run(_seed(url, dim, data, truncate=args.truncate))
    print("[done] 合成数据已写入（事实 source=dev_seed，便于清理）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
