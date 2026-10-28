# agent-demo

![Python](https://img.shields.io/badge/python-%3E%3D3.11-blue)
![Version](https://img.shields.io/badge/version-1.0.3--beta-blue)
![Status](https://img.shields.io/badge/status-beta-orange)
![License](https://img.shields.io/badge/license-MIT-green)

**一个模块化、可自托管的 QQ 智能体**：OneBot v11 协议端 ↔ NoneBot2（反向 WebSocket）↔ 本项目。
LLM 走 OpenAI 兼容接口（从 `.env` 读取，**不绑死任何供应商**）；核心 `agentcore` 与平台解耦，
插件层只做协议适配，可单测、可替换。

**核心能力**

- **对话与路由**：私聊直答；群聊只认唤醒词或 @机器人；防抖合并、群聊上下文、图片识图与引用/转发解析
- **记忆与知识**：长期事实（按会话作用域隔离）、滚动摘要与对话时间线警示、pgvector 语义检索、每日蒸馏、MediaWiki 直查
- **工具与安全**：命令白名单沙箱、权限三级制（low/medium/high）、不可信内容围栏与脱敏、SSH 只读诊断、文字/歌词渲染成项目风格长图
- **群组与运营**：发言榜/词云、原文归档、定时提醒与内容推送、主动联系（沉默检测器）、Web 总览与全部设置、成本预算
- **可运维**：三层记录保全（归档 + 备份 + 异地镜像）、性能与内存基线、启动自检

## 文档

README 只讲「是什么、怎么跑、必配什么」，细节走下面的链接。

| 文档 | 内容 |
|---|---|
| [docs/deploy-nas.md](docs/deploy-nas.md) | **NAS 部署实操**（绿联 DX4600：init 引导 / `.env` / 拉镜像 / 切换 NapCat / HA 迁移 / 回滚 / 验收清单） |
| [docs/operations.md](docs/operations.md) | 运维：容器化部署与挂载边界、日志归档与耗时排查、记录保全（归档 + 备份 + 恢复）、Schema 迁移、性能测试规范 |
| [docs/development.md](docs/development.md) | **代码地图**：目录结构、平台支持与移植边界、开发与测试命令 |
| [docs/configuration.md](docs/configuration.md) | 配置项详解：LLM（不绑死供应商）/ OneBot 反向 WS / 数据库 / 成本预算 |
| [docs/features-messaging.md](docs/features-messaging.md) | 消息路由与唤醒词、防抖合并、群聊上下文、图片识别与引用/转发解析、长回复分层投递 |
| [docs/features-memory-and-kb.md](docs/features-memory-and-kb.md) | 长期记忆、历史裁剪与滚动摘要、对话时间线、公共知识库（RAG）、MediaWiki 直查 |
| [docs/features-community.md](docs/features-community.md) | 群发言榜与词云、群消息原文归档、定时提醒、定时内容推送、主动联系 |
| [docs/features-skills.md](docs/features-skills.md) | 工具集（skills）、LLM 沙箱工作区、SSH 只读诊断、权限级别与管理员动作、run_shell、db_query、自定义 *.py 工具技能 |
| [docs/features-interaction.md](docs/features-interaction.md) | 人格系统与成长层、点歌/放歌、Web 总览与全部设置、本机概览（戳一戳） |
| [docs/memory-and-persona.md](docs/memory-and-persona.md) | 记忆与人格详解（当前状态）：七路数据来源、system prompt 组装顺序即优先级 |
| [docs/GENERAL-GUIDELINES.md](docs/GENERAL-GUIDELINES.md) | 从本仓库纪律提炼的通用工程规范（可迁移到其它项目） |
| [AGENTS.md](AGENTS.md) / [CONTRIBUTING.md](CONTRIBUTING.md) / [BACKLOG.md](BACKLOG.md) | 接手者边界与不变量 / 提交流程 / 待办与已实证缺陷 |
| [review/REVIEW-WORKFLOW.md](review/REVIEW-WORKFLOW.md) | 增量评审规范与历史评审/修复索引 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更与阶段里程碑 |

## 架构总览

```
QQ ──► OneBot v11 协议端 ──► NoneBot2（事件/权限/路由）
                              └─ qq_agent_adapter ──► agentcore
                                                           │
                              handle(request) → reply   +   sink(主动推送)
                                                           │
                              PostgreSQL(pgvector)：会话/消息/记忆向量/知识库/定时任务
```

**设计原则**：`agentcore` 是纯 Python 包，不感知 QQ；NoneBot 只当消息通道。将来想拆独立 HTTP 服务，只换壳，不动核心。

## 环境要求

- Python >= 3.11
- **操作系统：Linux 一等公民**（systemd / docker / `/proc` / 文件权限位均为 Linux 语义）；macOS 可跑核心对话；**Windows 有已知阻塞项，暂不支持**（完整矩阵见 [docs/development.md](docs/development.md#平台支持与移植边界)）
- Docker & Docker Compose（用于 PostgreSQL + pgvector）
- OneBot v11 兼容协议端已运行，并配置为**反向 WebSocket** 连到 NoneBot
- 宿主机部署才需要 **ffmpeg**（点歌 silk 转码）；走 Docker 则镜像内已含

## 快速开始

### NAS / Docker（推荐）

镜像由 GitHub Actions 自动构建推送 `ghcr.io/<owner>/agent-demo:latest`，NAS 只拉不建。
**一条 init 生成全部配置文件**，不用自己找模板、改名、建目录：

```bash
mkdir agent-demo && cd agent-demo
curl -O https://raw.githubusercontent.com/<owner>/agent-demo/main/docs/deploy-nas/docker-compose.yml

# 生成 .env（含随机数据库密码）/ config.yaml / data/ 14 个子目录
docker run --rm -v "$PWD:/out" ghcr.io/<owner>/agent-demo:latest init /out

# 按提示编辑 .env：填 QQ 号、群号、LLM key、NapCat 地址（这几项换机器就不一样，无法自动化）
docker compose up -d db bot
```

`.env` 里必须填的只有 5 项：`SUPERUSERS` / `ALLOWED_GROUPS` / `LLM_API_KEY` /
`ONEBOT_ONEV11_ACCESS_TOKEN` / `NAPCAT_HTTP_URL`。**已存在的 `.env` / `config.yaml`
绝不会被 init 覆盖**，重复跑是安全的。

端口规划、NapCat 反向 WS 切换、HA 迁移、验收清单见 [docs/deploy-nas.md](docs/deploy-nas.md)。

### 本机开发

```bash
git clone https://github.com/<owner>/agent-demo.git && cd agent-demo
uv sync --locked --extra dev        # 与 CI 完全一致，版本不漂移
cp .env.example .env                # 然后编辑：填 LLM 配置、数据库地址、协议端鉴权
docker compose up -d db             # 起本地 PostgreSQL + pgvector
python bot.py
```

> 只想跑起来不想拉全历史：`git clone --depth 1 <url>`（仓库跟踪内容约 21 MB，其中中文字体 16.8 MB）。

宿主机常驻部署（systemd）用 `python scripts/deploy.py all`（幂等，**刻意不自动启动 bot`），
子命令与硬边界见 [docs/operations.md](docs/operations.md) 的「容器化部署」与
[AGENTS.md](AGENTS.md) §3。

## 配置速查

全部配置项、默认值与语义见 [docs/configuration.md](docs/configuration.md)；`.env.example` 是**唯一事实源**（每个键一行注释）。

**必配**（不设置则该功能不可用）：

| 配置项 | 必配原因 |
|---|---|
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 对话与判定的模型端点（OpenAI 兼容，不绑死供应商） |
| `DATABASE_URL` | PostgreSQL + pgvector；留空 = 内存模式（重启清零，仅适合试跑） |
| `SUPERUSERS` / `ALLOWED_GROUPS` | 管理员与允许响应的群；**留空 = 无**（fail-closed） |
| `ONEBOT_ONEV11_ACCESS_TOKEN` | 协议端启用鉴权时必填 |

选配项（唤醒词、语义向量、权限级别、Web 总览、异地镜像、点歌等）分散在各功能文档的「配置一览」表中，见 [docs/configuration.md](docs/configuration.md) 的「选配项去向」。

## 功能索引

每个功能一节文档，配置项与限制随节给出。

| 主题 | 功能 |
|---|---|
| 消息与媒体 | 私聊直答 / 群聊唤醒词或 @ / 防抖合并 / 群聊上下文 / 图片识别 / 引用与转发解析 / 长回复分层投递与出站节流 |
| 记忆与知识库 | 长期事实（按会话作用域） / 历史裁剪与滚动摘要 / 对话时间线警示 / 公共知识库（摄取·蒸馏·检索） / MediaWiki 直查 |
| 群组与社区 | 群发言榜与词云（opt-in） / 群消息原文归档（opt-in、仅 PG） / 定时提醒 / 定时内容推送 / 主动联系（沉默检测器，默认关闭） |
| 工具、沙箱与权限 | skills 工具集 / 工作区与命令白名单 / SSH 只读诊断 / 权限级别（low/medium/high）与管理员动作类 / run_shell / db_query / 自定义 *.py 工具技能（data/skills） |
| 人格、语音与总览面 | 人格系统与 per-user 成长层 / 群人格（每群独立、群内共享） / 人格 Web 导入 / 点歌放歌 / Web 总览与全部设置（`/agent-web`） / 本机概览（戳一戳出图） |

## 运维速查

```bash
python scripts/backup_db.py backup                 # 立即备份（自动选 pg_dump / JSONL）
python scripts/backup_db.py list                   # 列出已有备份
python scripts/backup_db.py restore <file> --yes   # 从备份恢复（显式确认）
python scripts/migrate.py status                   # 只读：已应用 / 待应用 / 缺口 / 校验和
```

- **重启**：私聊 `/reboot`（superuser），或 web `/api/reboot`。systemd 部署改了 `.env` 需 `systemctl restart`；容器内改了 `.env` 需 `docker compose restart`（`/reboot` 不重读文件）
- **日志**：`data/logs`（含聊天明文，**勿分享截图**）；`AGENT_LOG_CONTENT=0` 可关正文
- **耗时排查**：`LLM step` / `skill done` / `[reply]` 三层耗时，见 [docs/operations.md](docs/operations.md)
- **备份加密 / 异地镜像 / 恢复细节**：见 [docs/operations.md](docs/operations.md) 的「记录保全」

## 开发

```bash
uv run --no-sync pytest -q                      # 默认套件（PG 用例自动跳过）
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync pyright agentcore              # 只查平台无关层
```

完整的测试矩阵（带 PG 的套件、覆盖率门禁、性能基线）与目录结构见
[docs/development.md](docs/development.md)；提交规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

MIT — 详见 [LICENSE](LICENSE)。
