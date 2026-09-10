---
title: agent-demo 记忆存储结构
tags:
  - agent-demo
  - 架构
  - 记忆系统
  - RAG
  - 备份
created: 2026-09-10
source: main @ dcf9fb6
---

# agent-demo 记忆存储结构

> **一句话**：会话与事实按「用户 × 会话（群/私聊）」隔离，公共知识库全局脱敏共享；
> 数据同时落在三处——PostgreSQL（权威）、JSONL 归档（实时日志）、每日备份（快照）。

---

## 1. 总体架构

```mermaid
flowchart TB
    subgraph QQ["QQ 侧"]
        MSG["消息 / 事件"]
        PIPE["matcher → pipeline<br/>组装 payload"]
    end

    subgraph CORE["agentcore 引擎"]
        ENG["engine.run()"]
        SAN["_sanitize_history<br/>净化历史窗口"]
    end

    subgraph MEM["记忆读写面"]
        HIST["会话历史<br/>get_history / append_message"]
        FACT["长期事实<br/>save_fact / recall_facts"]
        KB["公共知识库<br/>kb_search / kb_add_*"]
    end

    WRAP["ArchivingStore<br/>写时双写包装"]
    STORE["MemoryStore（权威）<br/>PgMemoryStore · InMemoryMemoryStore"]
    KBSVC["KnowledgeBase 门面<br/>摄取 / 检索 / 蒸馏"]

    PG[("PostgreSQL + pgvector")]
    AR["data/archive/<br/>messages-YYYY-MM-DD.jsonl<br/>7 天滚动"]
    BK["data/backups/<br/>每日快照 · 保留 7 份"]
    MI["异地镜像<br/>NAS / 第二块盘（可选）"]

    MSG --> PIPE --> ENG
    ENG --> HIST
    ENG --> FACT
    ENG --> KBSVC
    KBSVC --> KB
    HIST --> SAN
    HIST --> WRAP
    FACT --> WRAP
    KB --> STORE
    WRAP --> STORE
    STORE --> PG
    WRAP -.->|每条消息追加| AR
    PG -.->|每日 03:30 备份| BK
    BK -.->|可选镜像| MI
```

---

## 2. PostgreSQL 表关系

```mermaid
erDiagram
    sessions ||--o{ messages : "session_id"
    sessions ||--o{ facts : "session_id"
    kb_sources ||--o{ kb_chunks : "source_id"

    sessions {
        int id PK
        text user_id
        text group_id
        text scope
        text policy
        text summary
        timestamptz created_at
    }

    messages {
        int id PK
        int session_id FK
        text role
        text content
        jsonb tool_calls
        text tool_call_id
        timestamptz created_at
    }

    facts {
        int id PK
        int session_id FK
        text user_id
        text content
        vector embedding
        text source
        timestamptz created_at
    }

    kb_sources {
        int id PK
        text name
        text kind
        text location
        jsonb meta
        timestamptz created_at
    }

    kb_chunks {
        int id PK
        int source_id FK
        text chunk
        vector embedding
        int chunk_idx
        timestamptz created_at
    }

    user_state {
        text user_id PK
        text persona
        timestamptz updated_at
    }

    schedules {
        int id PK
        text cron
        text action
        jsonb params
        text target
        boolean enabled
        timestamptz created_at
    }
```

### 关键索引

| 索引 | 表 | 作用 |
|---|---|---|
| `sessions_user_scope_key` (UNIQUE) | sessions | `(user_id, COALESCE(group_id,''), scope)`，防并发重复建会话 |
| `messages_session_id_idx` | messages | `(session_id, id)`，历史窗口不再全表扫描 |
| `facts_user_id_idx` | facts | 按用户召回 |
| `facts_embedding_idx` (hnsw) | facts | 向量相似度检索 |
| `kb_chunks_source_id_idx` | kb_chunks | 按来源删除/统计 |
| `kb_chunks_embedding_idx` (hnsw) | kb_chunks | 知识库语义检索 |

> `vector` 的维度 = 启动时探测到的 embedding 模型输出维度（当前 **1024**）。
> 与库中已有列不一致时只告警、不静默改列（避免悄悄清空记忆）。

---

## 3. 一次对话的记忆读写时序

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant M as matcher / pipeline
    participant E as engine.run
    participant S as MemoryStore
    participant A as JSONL 归档
    participant K as 公共知识库
    participant L as LLM

    U->>M: 发消息
    M->>M: 组装 payload（引用 / 转发 / 图片）
    M->>E: run(context, text, images)
    E->>S: resolve_session(user_id, group_id)
    S-->>E: session_id
    E->>S: get_history(session_id, 20)
    S-->>E: 最近 20 条
    E->>E: _sanitize_history（丢弃孤儿 tool 消息）
    E->>S: save_fact(...session_id)（抽取长期事实）
    E->>S: recall_facts(user, 向量, session_id)
    S-->>E: 本会话内的事实
    E->>K: retrieve(本轮内容)
    K->>S: kb_search(向量)
    S-->>K: top-k 片段
    K-->>E: 命中片段（包裹「不可信数据」围栏）
    E->>L: system prompt + 历史 + 本轮
    L-->>E: 回复 / tool_calls
    E->>S: append_message(...)
    S->>A: 同一条记录追加到当天 JSONL
    E-->>M: 最终回复
    M->>U: 发送（优先用触发事件的 bot）
```

---

## 4. 作用域隔离（易错点）

```mermaid
flowchart LR
    subgraph U1["用户 2224513919"]
        direction TB
        PA["私聊会话<br/>messages + facts"]
        GA["群 A 会话<br/>messages + facts"]
        GB["群 B 会话<br/>messages + facts"]
        PE["人格 persona<br/>user_state"]
    end
    KBALL["公共知识库<br/>脱敏 · 全局共享"]

    PA -.隔离.-> GA
    GA -.隔离.-> GB
    PA --> KBALL
    GA --> KBALL
    GB --> KBALL
    PE -.->|跨群全局| PA
    PE -.->|跨群全局| GA
    PE -.->|跨群全局| GB
```

| 数据 | 键 | 隔离范围 | 说明 |
|---|---|---|---|
| 会话历史 `messages` | `session_id` = （user_id, group_id, scope） | **某人在某群 / 私聊** | 群 A ≠ 群 B ≠ 私聊 |
| 长期事实 `facts` | `user_id` + `session_id` | **跟随会话** | 群 A 说过的事不会召回到群 B；去重也按会话 |
| 人格 `user_state` | `user_id` | **跨群全局** | 有意为之：人格是用户偏好，不是聊天记忆 |
| 公共知识库 `kb_*` | 无（全局） | **所有会话共享** | 入库前脱敏；检索结果按不可信数据围栏 |
| 归档 / 备份 | 无（全局） | **进程外文件** | 与数据库解耦，任何针对库的误操作都碰不到 |

---

## 5. 每日蒸馏流水线（成长型 RAG）

```mermaid
flowchart LR
    W["水位线<br/>last_message_id"] --> Q1["数据库<br/>messages_after(w)"]
    W --> Q2["归档<br/>read_since(w)"]
    Q1 --> MG["按消息 id 去重合并"]
    Q2 --> MG
    MG --> TR["render_transcript<br/>PII 掩码 · 丢 tool 噪声"]
    TR --> LLM["LLM 蒸馏<br/>严格脱敏 prompt"]
    LLM --> SAN["sanitize 后置过滤<br/>PII 掩码 + 丢弃指向个人 / 指令性内容"]
    SAN --> SRC["kb_sources<br/>kind=distill<br/>meta.last_message_id=新水位线"]
    SAN --> CH["kb_chunks<br/>chunk + embedding"]

    SAN -.->|失败回滚，不推进水位线| SRC
    LLM -.->|空响应则重试一次，仍空则跳过前进| SRC
```

要点：

- 水位线存放在**最后一条 `kind=distill` 的 kb_sources.meta** 里；删除该来源会重置水位线
- 输入是 **数据库 ∪ 归档**：库被清空后知识仍能从归档继续沉淀
- 失败语义：写入失败 → 回滚来源（不推进水位线，下次重试）；LLM 空响应 → 重试一次后**跳过前进**（否则水位线永久卡死）

---

## 6. 三份存储与恢复路径

```mermaid
flowchart TB
    PG[("PostgreSQL + pgvector<br/>权威数据")]
    AR["data/archive/*.jsonl<br/>每条消息实时追加 · 7 天滚动"]
    BK["data/backups/<br/>每日 03:30 · 保留 7 份"]
    MI["异地镜像<br/>NAS / 第二块盘"]

    PG -->|每条消息实时追加| AR
    PG -->|pg_dump 优先，失败降级 JSONL| BK
    BK -->|可选| MI

    BK -->|backup_db.py restore --yes| PG
    MI -->|异地恢复演练| PG
    AR -->|backup_db.py restore-archive --yes| PG
```

| 存储 | 位置 | 内容 | 更新频率 | 恢复命令 |
|---|---|---|---|---|
| PG + pgvector | 容器 `agent-demo-db-1` | 会话 / 消息 / 事实 / 知识库 / 人格 | 实时 | — |
| JSONL 归档 | `data/archive/` | 每条消息（含 user_id / group_id / ts） | 逐条追加 | `restore-archive` |
| 备份快照 | `data/backups/` + 镜像 | 整库 | 每日 | `restore --yes` |

> 归档与备份目录都在 `.gitignore` 里（含隐私内容）。
> 归档从**启用时刻**开始记录，更早的历史由数据库备份覆盖。

---

## 7. 后台任务与关键参数

```mermaid
flowchart LR
    CRON1["每天 03:00"] --> J1["kb_digest<br/>记忆 → 知识蒸馏入库"]
    CRON2["每天 03:30"] --> J2["db_backup<br/>整库备份 + 镜像 + 归档轮转"]
```

| 配置段 | 参数 | 默认 | 含义 |
|---|---|---|---|
| `agent` | `extract_facts` | `true` | 每轮是否抽取长期事实 |
| `agent` | `memory_facts_top_k` | `5` | 召回事条数 |
| `agent` | `memory_facts_threshold` | `0.15` | 召回相似度下限 |
| `rag` | `top_k` / `threshold` | `4` / `0.3` | 知识库注入条数与下限 |
| `rag` | `chunk_chars` | `600` | 摄取切块长度 |
| `rag` | `digest_cron` | `0 3 * * *` | 蒸馏时间 |
| `rag` | `distill_max_tokens` | `2048` | 蒸馏输出上限（推理模型烧思维链，别调小） |
| `rag` | `min_chars` | `200` | 新内容不足则跳过且**不推进**水位线 |
| `archive` | `keep_days` | `7` | 归档滚动保留天数 |
| `backup` | `keep` / `cron` | `7` / `30 3 * * *` | 备份保留份数与时间 |
| `backup` | `mirror_dir` | 空 | 异地镜像目录（建议填写） |

---

## 8. 已知边界

- `sessions.summary` 字段已建但**从未写入**（滚动摘要未实现）
- `schedules` 表：**用户定时提醒已持久化在这里**（重启不丢）；进程内 cron 任务
  （每日蒸馏/备份）仍由 apscheduler 调度、未落库
- 蒸馏水位线依赖 `kb_sources` 记录，不是独立状态表
- 归档不做回填：启用之前的历史不在归档里
- 单机备份挡得住误删、挡不住盘坏 → 需配置 `AGENT_BACKUP_MIRROR_DIR`
