-- 0001_baseline：当前 schema 的完整基线（从旧 DDL_TEMPLATE 原样搬入）。
--
-- 全部语句幂等：老库没有 schema_migrations 时也会安全"补记"这一版。
-- 基线**不接受修改**——任何结构变更都新开 0002_xxx.sql，checksum 会拦住偷改。
-- {dim} 由 runner 在**执行时**替换为运行期 embedding 维度；校验和按原始文本计算。
--
-- 刻意不在这里的三项（见 agentcore/memory/schema_repair.py，每次启动重跑）：
--   * sessions 唯一索引：老库可能有重复会话，必须先合并再建；
--   * HNSW 索引：旧版 pgvector 可能不支持，失败只降级不阻塞启动；
--   * vector 维度 TRUNCATE+ALTER：dim 是运行期配置而非固定 schema 版本，且会丢数据。

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    group_id TEXT,
    scope TEXT NOT NULL DEFAULT 'private',
    policy TEXT NOT NULL DEFAULT 'per_user',
    summary TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- A2 滚动摘要：摘要已覆盖到哪条消息（水位）；老库靠幂等 ALTER 补列
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary_upto_id INTEGER;

CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls JSONB,
    tool_call_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS facts (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding vector({dim}),
    source TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS kb_sources (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    location TEXT NOT NULL,
    meta JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES kb_sources(id),
    chunk TEXT NOT NULL,
    embedding vector({dim}),
    chunk_idx INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS schedules (
    id SERIAL PRIMARY KEY,
    cron TEXT NOT NULL,
    action TEXT NOT NULL,
    params JSONB,
    target TEXT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_state (
    user_id TEXT PRIMARY KEY,
    persona TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
-- 人格成长层：对话计数（达阈值触发回顾）与成长文本（滚动合并写入）
ALTER TABLE user_state ADD COLUMN IF NOT EXISTS chat_count INTEGER DEFAULT 0;
ALTER TABLE user_state ADD COLUMN IF NOT EXISTS persona_growth TEXT;

-- 群人格（2026-10-20）：群级默认人格，群内所有人共享。群维度独立于
-- user_state（用户选择仍是跨群全局偏好），解析优先级见 engine._load_persona_text：
-- 群人格 → 用户选择 → 全局默认
CREATE TABLE IF NOT EXISTS group_persona (
    group_id TEXT PRIMARY KEY,
    persona TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE messages ADD COLUMN IF NOT EXISTS tool_call_id TEXT;

-- 定时提醒：schedules 表补齐运行所需字段
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'once';
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS message TEXT;
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS next_run TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS schedules_due_idx ON schedules(enabled, next_run);

-- P0-2：常用查询路径的索引（避免全表扫描/全表距离计算）
CREATE INDEX IF NOT EXISTS messages_session_id_idx ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS facts_user_id_idx ON facts(user_id);
CREATE INDEX IF NOT EXISTS kb_chunks_source_id_idx ON kb_chunks(source_id);
