"""RAG 子系统（M5+）：公共知识库 —— 摄取、检索、每日从记忆蒸馏。

模块：
- chunker    文本切块
- sanitize   公共库的脱敏与防注入过滤
- distill    从记忆增量蒸馏知识条目
- ingest     文本/文件摄取
- retriever  语义检索 + 不可信数据围栏
- service    KnowledgeBase 服务门面（engine / admin / scheduler 共用）
"""
from agentcore.rag.chunker import chunk_text
from agentcore.rag.distill import distill_from_memory, summarize
from agentcore.rag.ingest import ingest_file, ingest_text
from agentcore.rag.retriever import format_block, retrieve
from agentcore.rag.sanitize import sanitize_entry, scrub_pii
from agentcore.rag.service import KnowledgeBase

__all__ = [
    "KnowledgeBase",
    "chunk_text",
    "distill_from_memory",
    "format_block",
    "ingest_file",
    "ingest_text",
    "retrieve",
    "sanitize_entry",
    "scrub_pii",
    "summarize",
]
