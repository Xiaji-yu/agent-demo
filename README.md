# agent-demo

![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-alpha-orange)

> **临时项目名，后续可改。**
> 在现有 NapCat + NoneBot2 架构上，跑通「自研 agent 核心 + 薄适配插件」核心链路。

## 架构总览

```
QQ ──► NapCat(OneBot11) ──► NoneBot2（事件/权限/路由）
                              └─ qq_agent_adapter ──► agentcore
                                                           │
                              handle(request) → reply   +   sink(主动推送)
                                                           │
                              PostgreSQL(pgvector)：会话/消息/记忆向量/知识库/定时任务
```

**设计原则**：`agentcore` 是纯 Python 包，不感知 QQ；NoneBot 只当消息通道。将来想拆独立 HTTP 服务，只换壳，不动核心。

## 环境要求

- Python >= 3.10
- Docker & Docker Compose（可选，用于 PostgreSQL + pgvector）
- NapCat 已运行，并配置为**反向 WebSocket**连到 NoneBot

## 快速开始

```bash
git clone git@github.com:Xiaji-yu/agent-demo.git
cd agent-demo

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"

cp .env.example .env
# 编辑 .env：填入 LLM 配置、数据库地址、NapCat 鉴权

docker compose up -d db   # M1+ 需要持久化，M0 可跳过

python bot.py
```

## 配置说明

核心配置在 `.env` 和 `config.yaml`。

### LLM（从 .env 读取，不绑死供应商）

```env
LLM_BASE_URL=https://api.stepfun.com/v1
LLM_API_KEY=你的key
LLM_MODEL=step-1-flash
LLM_TEMPERATURE=0.7
LLM_MAX_TOKENS=1024

# 可选 fallback，主线路不通自动切换
LLM_FALLBACK_BASE_URL=
LLM_FALLBACK_API_KEY=
LLM_FALLBACK_MODEL=
```

### OneBot 反向 WS

NoneBot 作为 WS 服务端监听，NapCat 主动连过来。

- 默认地址：`ws://<本机IP>:8080/onebot/v11/`
- `.env` 里配鉴权：
  ```env
  DRIVER=nonebot.drivers.fastapi
  HOST=0.0.0.0
  PORT=8080
  ONEBOT_ONEV11_ACCESS_TOKEN=123456
  ```

### 数据库

```env
DATABASE_URL=postgresql://qqagent:qqagent@127.0.0.1:5432/qqagent
```

留空则使用**内存存储**（M0 可用，重启丢失）。

### 长期记忆（M4）

对话中自动抽取「事实」（如姓名、城市、偏好）存库，下次对话按语义召回注入 prompt。
配置任一 OpenAI 兼容 `/embeddings` 服务可获得语义召回；留空则用本地 hash 降级（仅词面近似）：

```env
EMBEDDING_BASE_URL=
EMBEDDING_API_KEY=
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=2048
```

启动会探测模型真实维度并按此建表。**若库中已有 vector 列的维度与模型不一致**：默认只打告警、不改库（避免悄悄清空记忆）；确认可接受清空 `facts`/`kb_chunks` 后，设 `AGENT_MIGRATE_VECTOR=1` 才会执行 TRUNCATE + ALTER 迁移（有数据丢失风险）。

行为参数在 `config.yaml` 的 `agent:` 段：`extract_facts`、`memory_facts_top_k`、`memory_facts_threshold`。

### 人格系统（Persona）

`agentcore/personas/` 下每个 `.md` 文件定义一种人格，frontmatter 提供元数据，正文是注入给模型的行为指南：

```markdown
---
name: fortune_teller
description: 玄学顾问人格
default: false
---

现在你是一位温和专业的玄学顾问。……（注入 system prompt 的行为指南）
```

- 用户级切换，选择结果持久化到 PG（`user_state` 表；内存模式进程内保留）
- 命令：`/persona`（查看）、`/persona use <名字>`、`/persona reset`
- 目录可用环境变量 `PERSONAS_DIR` 覆盖；新增人格 = 放一个新 md，然后 `/persona list` 即可看到

### LLM 沙箱工作区（个人服务器）

`data/workspace/`（可用 `WORKSPACE_DIR` 覆盖，已 gitignore）是 LLM 的临时/缓存/产物目录。相关技能**仅管理员（SUPERUSERS）可用**：

- `fs_list / fs_read / fs_write / fs_mkdir`：读写工作区，路径锁定（`..` / 绝对路径拒绝）
- `fs_delete`：需在聊天中回复「确认删除 XXXX」二次确认
- `run_command`：白名单命令执行（**不经 shell**、20s 超时、输出截断、全程日志）
  - 允许：`git`(status/log/diff/show 等只读)、`grep/find/cat/ls/head/tail/wc/pwd`、`zip`、`unzip -d`、`curl`(仅 https)
  - **已禁用**：`python3` / `node` / `npm run`（运行任意脚本 ≈ 任意代码，风险高）、shell 组合、绝对路径

## 目录结构

```
agent-demo/
├─ agentcore/              # 纯 Python 包，不依赖 NoneBot
│  ├─ llm/                 # LLM 客户端（供应商无关）
│  ├─ embedding/           # Embedding 客户端（OpenAI 兼容 / 本地降级）
│  ├─ loop/                # tool-loop 引擎
│  ├─ personas/            # 人格系统（md 定义 + manager）
│  ├─ tools/               # 工具注册表 + 内置工具
│  ├─ memory/              # 会话/记忆存储（内存/PG）
│  ├─ rag/                 # RAG 摄取/检索（M5+）
│  ├─ multiagent/          # 多 Agent 编排（M6+）
│  └─ scheduler/           # 定时任务（M7+）
├─ plugins/
│  └─ qq_agent_adapter/    # NoneBot 薄插件
│     ├─ matcher.py        # 消息路由（私聊/群前缀/@）
│     ├─ acl.py            # 权限控制
│     ├─ sink.py           # 主动推送
│     └─ admin.py          # /help /reset /status
├─ data/kb_samples/        # 示例知识库语料
├─ bot.py                  # NoneBot 启动入口
├─ config.yaml             # Agent 行为配置
├─ docker-compose.yml      # PostgreSQL + pgvector
├─ pyproject.toml
└─ README.md
```

## 阶段里程碑

| 阶段 | 目标 | 状态 |
|---|---|---|
| M0 | 回声跑通，NapCat ↔ NoneBot ↔ 薄插件互通 | ✅ |
| M1 | 单 Agent 无工具，会话入 PG/内存，ACL | ✅ |
| M2 | 工具调用（fetch_url / 天气 / 计算） | ✅ |
| M3 | 联网搜索（博查/Tavily） | ✅ |
| M4 | 长期记忆（facts 抽取 + pgvector 召回） | ✅ |
| M5 | RAG 知识库（摄取 / 检索） | ⏳ |
| M6 | 多 Agent（supervisor + expert） | ⏳ |
| M7 | 定时推送 + 成本预算 + 日志归档 | ⏳ |

> 另：M2 期间同步落地了通用 **Skill 系统**（动态安装/卸载、权限控制、YAML 清单自装），当前全部内置能力均以 skill 形式注册。

## 消息路由规则

- **私聊**：直接对话，无需前缀
- **群聊**：使用前缀 `ai ` 或 `!ai ` 或 `/ai `，或直接 @机器人
- **管理指令**：`/help`、`/reset`、`/status`

## 开发

```bash
# 格式化
ruff format .

# Lint
ruff check .

# 测试
pytest
```

详细开发规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

MIT — 详见 [LICENSE](LICENSE)。
