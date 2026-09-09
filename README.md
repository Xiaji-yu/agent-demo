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

## 目录结构

```
agent-demo/
├─ agentcore/              # 纯 Python 包，不依赖 NoneBot
│  ├─ llm/                 # LLM 客户端（供应商无关）
│  ├─ loop/                # tool-loop 引擎
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
| M1 | 单 Agent 无工具，会话入 PG/内存，ACL | 🔨 |
| M2 | 工具调用（fetch_url / 天气 / 计算） | 🔨 |
| M3 | 联网搜索（博查/Tavily） | ⏳ |
| M4 | 长期记忆（facts 抽取 + pgvector 召回） | ⏳ |
| M5 | RAG 知识库（摄取 / 检索） | ⏳ |
| M6 | 多 Agent（supervisor + expert） | ⏳ |
| M7 | 定时推送 + 成本预算 + 日志归档 | ⏳ |

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
