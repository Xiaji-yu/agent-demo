"""一次性批量导入 data/kb_samples 下的 .md 文件到公共知识库。用法：

    .venv/bin/python scripts/ingest_kb_samples.py

它会读取 .env / config.yaml，初始化 store + embedding + KnowledgeBase，
然后遍历 data/kb_samples/*.md 调用 add_file。与 /kb samples 一致：同名来源
已入库即跳过（失败重跑安全）；单文件上限 2MB（超限跳过）、单来源最多 200 块
（超出部分丢弃，见 agentcore/rag/ingest.py 的上限常量）。
任一文件导入失败退出码为 1，全部成功为 0。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = PROJECT_ROOT / "data" / "kb_samples"


def _load_config() -> dict:
    import yaml

    cfg_path = os.getenv("AGENT_CONFIG", str(PROJECT_ROOT / "config.yaml"))
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


async def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    config = _load_config()

    from agentcore.embedding import load_embedding_client_from_env
    from agentcore.memory.store import InMemoryMemoryStore, PgMemoryStore

    embedding = load_embedding_client_from_env()
    try:
        dim = await embedding.probe_dim()
    except Exception:
        dim = getattr(embedding, "dim", 2048)

    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        memory = PgMemoryStore(db_url, dim=dim)
        await memory.init()
    else:
        memory = InMemoryMemoryStore()

    from agentcore.rag import KnowledgeBase

    kb = KnowledgeBase(memory, embedding, (config.get("rag") or {}).copy())

    files = sorted(SAMPLES_DIR.glob("*.md"))
    if not files:
        print(f"未找到文件：{SAMPLES_DIR}")
        sys.exit(0)

    from agentcore.rag.ingest import MAX_INGEST_BYTES

    # 判重对齐 /kb samples：同名来源已入库即跳过，失败重跑不会重复入库
    known = {s.get("name") for s in await kb.list_sources(limit=1000)}
    failed = 0
    for path in files:
        if path.name in known:
            print(f"⏭ {path.name}: 同名来源已入库，跳过")
            continue
        if path.stat().st_size > MAX_INGEST_BYTES:
            print(f"⏭ {path.name}: 超过 2MB 上限，跳过")
            continue
        try:
            result = await kb.add_file(str(path), kind="sample")
            print(f"✓ {path.name}: {result['chunks']} 块")
        except Exception as exc:
            print(f"✗ {path.name}: {exc}")
            failed += 1
    if failed:
        print(f"共 {failed} 个文件导入失败")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
