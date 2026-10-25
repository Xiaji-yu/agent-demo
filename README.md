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

| 文档 | 内容 |
|---|---|
| [docs/configuration.md](docs/configuration.md) | 配置项详解：LLM（不绑死供应商）/ OneBot 反向 WS / 数据库 / 成本预算 |
| [docs/features-messaging.md](docs/features-messaging.md) | 消息路由与唤醒词、防抖合并、群聊上下文、图片识别与引用/转发解析、长回复分层投递 |
| [docs/features-memory-and-kb.md](docs/features-memory-and-kb.md) | 长期记忆、历史裁剪与滚动摘要、对话时间线、公共知识库（RAG）、MediaWiki 直查 |
| [docs/features-community.md](docs/features-community.md) | 群发言榜与词云、群消息原文归档、定时提醒、定时内容推送、主动联系 |
| [docs/features-skills.md](docs/features-skills.md) | 工具集（skills）、LLM 沙箱工作区、SSH 只读诊断、权限级别与管理员动作、run_shell、db_query、自定义 *.py 工具技能 |
| [docs/features-interaction.md](docs/features-interaction.md) | 人格系统与成长层、点歌/放歌、Web 总览与全部设置、本机概览（戳一戳） |
| [docs/operations.md](docs/operations.md) | 日志归档与耗时排查（`LLM step` / `skill done` / `[reply]` 三层耗时）、记录保全（归档 + 备份 + 恢复）、性能/内存基线 |
| [docs/memory-and-persona.md](docs/memory-and-persona.md) | 记忆与人格详解（当前状态）：七路数据来源、system prompt 组装顺序即优先级 |
| [docs/GENERAL-GUIDELINES.md](docs/GENERAL-GUIDELINES.md) | 从本仓库纪律提炼的通用工程规范（可迁移到其它项目） |
| [AGENTS.md](AGENTS.md) / [CONTRIBUTING.md](CONTRIBUTING.md) / [BACKLOG.md](BACKLOG.md) | 接手者边界与不变量 / 提交流程 / 待办与已实证缺陷 |
| [review/REVIEW-WORKFLOW.md](review/REVIEW-WORKFLOW.md) | 增量评审规范与历史评审/修复索引 |

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
- **操作系统：Linux 一等公民**（systemd / docker / `/proc` / 文件权限位均为 Linux 语义）；macOS 可跑核心对话、管理面部分降级；**Windows 当前不支持**（已知阻塞项见[平台支持与移植边界](#平台支持与移植边界)）
- Docker & Docker Compose（可选，用于 PostgreSQL + pgvector）
- OneBot v11 兼容协议端已运行，并配置为**反向 WebSocket**连到 NoneBot

## 平台支持与移植边界

**一句话**：为 Linux 部署设计；macOS 可开发/试用；Windows 有已知阻塞项，暂不支持。

### 各平台现状

| 能力 | Linux | macOS | Windows |
|---|---|---|---|
| 对话 / 记忆 / RAG / 调度 / Web 总览 | ✅ | ✅ | ⚠ 有阻塞项（见下） |
| 备份恢复（pg_dump / JSONL） | ✅ | ✅（无 psql 时走 JSONL / docker） | ⚠ 文件锁降级 + 权限位语义缺失 |
| 运维技能（端口 / 进程 / systemctl） | ✅（`/proc` + systemd） | ⚠ 无 `/proc` → 端口列举/进程扫描返回空；无 systemctl → 服务控制不可用 | ⚠ 同 macOS |
| `proc_kill` / `run_shell` | ✅ | ✅（POSIX 信号 / bash 可用） | ❌ 信号语义不同，未适配 |
| 点歌语音（silk） | ✅（ffmpeg） | ⚠ 需自编 silk-python（官方仅 cp312 manylinux x86_64 wheel） | ⚠ 同上 |
| 文件权限不变量（落盘 0600 / 剥执行位） | ✅ | ✅ | ❌ Windows 无权限位语义，两条不变量静默失效 |
| Web 受控写入（`.env` + `config.yaml` 回写） | ✅ | ✅ | ❌ `os.O_NOFOLLOW` 在 Windows 不存在，保存即 500 |

### 移植到其它聊天平台

`agentcore/`（引擎 / 记忆 / RAG / 技能 / 预算 / 安全）是平台无关的纯 Python；平台耦合全部收敛在 `plugins/qq_agent_adapter/`（OneBot v11 适配）。移植步骤：

1. 新写适配层替代 `plugins/qq_agent_adapter/`：入站消息 → payload（text / images / user_text / ping_only / sender_card…）→ `engine.run(context, …)`；出站复用 `agentcore` 的 sink / 节流 / 分层投递接口
2. ACL 层当前假设用户 ID 是**纯数字**（`acl.py` 的 `isascii()+isdigit()` 校验，防全角数字穿透）——用户 ID 含字母的平台需同步放宽
3. 群语义（唤醒词 / @bot / 群上下文 / 群文件上传 / 合并转发）是 OneBot 形态，需按目标平台重做；`agentcore` 的围栏 / 脱敏 / 工具循环可直接沿用

### 部署假设（换环境前先读）

- **一个进程服务一个 bot 账号**：`self_id` 取连接中的协议端；`/reboot` 重启影响全部会话；预算是按日全局账本。多开请多进程 + 各自 `.env` / 数据目录 / 端口
- **相对路径按 CWD 解析**：`config.yaml` 与 env 默认的 `data/*` 目录随启动目录落盘，部署请改绝对路径（`config.yaml` 头部有完整清单）
- **默认时区 Asia/Shanghai**：提醒 / cron / 日键 / 每日上限都走 `agentcore.tz`；非中国部署设 `AGENT_SCHEDULER_TZ`（IANA 名或 `local`），显示层与日键同源
- **`/reboot` 执行链**末端是「退出等 supervisor 拉起」：systemd / docker / supervisorctl 可用；裸 `nohup` 部署退到退出即死亡（「工具集」节的 `/reboot` 部署前提有时序警示）

## 快速开始

```bash
git clone git@github.com:Xiaji-yu/agent-demo.git
cd agent-demo

# 推荐：安装 uv 后按锁文件装依赖（与 CI 完全一致，版本不会漂移）
uv sync --locked --extra dev
source .venv/bin/activate

# 备选（无 uv 时）：pip 按 pyproject 的下限解析，版本可能与 CI 不同
# python3 -m venv .venv
# pip install -e ".[dev]"

cp .env.example .env
# 编辑 .env：填入 LLM 配置、数据库地址、协议端鉴权

docker compose up -d db   # M1+ 需要持久化，M0 可跳过

python bot.py
```

> 依赖锁定：`uv.lock` 是唯一锁文件，升级依赖请改 `pyproject.toml` 后重新 `uv lock`，
> 由 Dependabot 每月提 PR（必须过全量 CI 才会合入）。

> **只想要能跑的最新快照、不想拉全历史**（省流量，仓库跟踪内容约 21 MB，其中中文字体 16.8 MB）：
> 用浅克隆 `git clone --depth 1 https://github.com/Xiaji-yu/agent-demo.git`；
> 开发者要按需拉取大文件可用 `git clone --filter=blob:none <url>`（部分克隆）。

## 一键部署

不想手动分步执行时，用部署脚本一条龙（幂等可反复跑；**刻意不自动启动 bot**——启动前必须人工填好 `.env`）：

```bash
python scripts/deploy.py all          # env → 预检 → 装依赖 → 起本地 PG → 冒烟
```

分步命令（`all` 按此顺序执行，可分别单跑）：

| 命令 | 作用 |
|---|---|
| `check` | 只读预检：Python≥3.11、平台提示、LLM 三件套 / `SUPERUSERS` 占位 / PG 可达性、时区与 CWD 提醒。**放行门**（strict）：配置类问题必须处理 |
| `env` | `.env.example → .env`（已存在则拒绝覆盖；`--force` 先备份再重建，备份 0600） |
| `install [--dev]` | 建 `.venv` + `uv sync --locked`（`--dev` 连带 pytest/ruff）；**需要 uv**，缺失时明确报错，不回退 pip（避免绕过 `uv.lock`） |
| `db [--down]` | `docker compose up -d db` 并轮询到 healthy；`--down` 只停、保留数据卷 |
| `systemd [--install] [--user] [--run-as NAME]` | 生成常驻 unit（绝对路径 `WorkingDirectory`、`EnvironmentFile=.env`、`Restart=always` + `ProtectSystem=strict` 等硬化）；默认只预览，`--install` 才落盘（system 级需 sudo），**不 enable/start** |
| `smoke` | `import bot` + `config.yaml` 解析 + 仓库不变量守卫测试 |

**两条硬边界**：脚本不自动启动 bot、不写仓库外文件（`systemd --install` 是唯一例外且需显式 flag，system 级要求 root）；生成的 unit 内**零密钥**（全靠 `EnvironmentFile` 指向 `.env`）；全部子进程以 argv 列表起，不经过 shell。部署假设（单账户、CWD 相对路径、默认时区、reboot 执行链）见[平台支持与移植边界](#平台支持与移植边界)。

## 配置速查

全部配置项、默认值与语义见 [docs/configuration.md](docs/configuration.md)；`.env.example` 是**唯一事实源**（每个键一行注释，含「值内出现 `#` 需加引号」的写法约定）。

> **标注约定**：**必配** = 不设置则该功能不可用（示例值只是占位符）；**选配** = 有可用默认值或仅用于调优。

### 必配

| 配置项 | 必配原因 |
|---|---|
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 对话与判定的模型端点（OpenAI 兼容，不绑死供应商）；多 provider 配置（`data/llm-providers.json`，OpenAI / Anthropic 双协议，web 可配 key 但只写不回显）+ `/model` 运行期切主 |
| `DATABASE_URL` | PostgreSQL + pgvector；留空 = 内存模式（重启清零，仅适合试跑） |
| `SUPERUSERS` / `ALLOWED_GROUPS` | 管理员与允许响应的群；**留空 = 无**（fail-closed：管理面/群聊不可用） |
| `ONEBOT_ONEV11_ACCESS_TOKEN` | 协议端启用鉴权时必填 |

### 选配

按主题分散在各文档的「配置一览」表中（含**必配/选配**标注与默认值），例如：
`AGENT_WAKE_WORDS`（唤醒词）、`EMBEDDING_*`（语义向量，留空降级本地 hash）、
`AGENT_PERMISSION_LEVEL`（权限级别）、`AGENT_WEB_TOKEN`（挂载 Web 总览）、
`AGENT_BACKUP_MIRROR_DIR`（异地镜像，推荐）、`AGENT_MUSIC_API_URL`（点歌）——
完整清单见 [docs/configuration.md](docs/configuration.md) 的「选配项去向」。

## 功能索引

每个功能一节文档，配置项与限制随节给出；下表按主题指向对应文档。

| 主题 | 功能 |
|---|---|
| 消息与媒体 | 私聊直答 / 群聊唤醒词或 @ / 防抖合并 / 群聊上下文 / 图片识别 / 引用与转发解析 / 长回复分层投递与出站节流 |
| 记忆与知识库 | 长期事实（按会话作用域） / 历史裁剪与滚动摘要 / 对话时间线警示 / 公共知识库（摄取·蒸馏·检索） / MediaWiki 直查 |
| 群组与社区 | 群发言榜与词云（opt-in） / 群消息原文归档（opt-in、仅 PG） / 定时提醒 / 定时内容推送 / 主动联系（沉默检测器，默认关闭） |
| 工具、沙箱与权限 | skills 工具集 / 工作区与命令白名单 / SSH 只读诊断 / 权限级别（low/medium/high）与管理员动作类 / run_shell / db_query / 自定义 *.py 工具技能（data/skills） |
| 人格、语音与总览面 | 人格系统与 per-user 成长层 / 群人格（每群独立、群内共享） / 人格 Web 导入（`/agent-web`「人格」标签） / 点歌放歌 / Web 总览与全部设置（`/agent-web`，含「模型」provider 管理 + Embedding/搜索配置标签） / 本机概览（戳一戳出图） |

## 运维速查

- **重启**：私聊 `/reboot`（superuser）；或 web `/api/reboot`（写门禁 + 两段确认）。systemd 部署改了 `.env` 需 `systemctl restart`（部署假设见[平台支持与移植边界](#平台支持与移植边界)）。
- **备份 / 恢复**（数据库只有一份副本时，一次误删就全没了；三层保全与异地镜像见 [docs/operations.md](docs/operations.md)）：

  ```bash
  python scripts/backup_db.py backup                 # 立即备份（自动选 pg_dump / JSONL）
  python scripts/backup_db.py list                   # 列出已有备份
  python scripts/backup_db.py restore <file> --yes   # 从备份恢复（显式确认）
  ```

  web `/agent-web` 的「备份」标签可看列表/最新年龄/磁盘，手动备份、删除非最新一份、
  下载（均 2FA + 审计；校验为只读动作只审计；下载的是备份原文件——普及时含聊天明文，
  开启 `AGENT_BACKUP_ENCRYPT=age` 后为密文；恢复仍只走 CLI）。大文件下载走一次性
  票据 + 浏览器原生流式落盘，避免把整个备份读进网页内存。
  NAS 之外可再加 VPS 异地镜像（WireGuard 内网，`KEEP_DAYS=30` + `MAX_GB=9`），
  见 [docs/operations.md](docs/operations.md)。

- **数据库结构迁移**（启动时自动执行；已应用的迁移不可修改，破坏性迁移需显式双开关）：

  ```bash
  python scripts/migrate.py status   # 只读：已应用 / 待应用 / 缺口 / 校验和
  python scripts/migrate.py up       # 应用待执行迁移 + 幂等修复步骤
  ```

  细节（校验和、版本守卫、fail-closed、备份 sidecar）见
  [docs/operations.md](docs/operations.md) 的「Schema 迁移」。

- **日志**：`AGENT_LOG_DIR`（默认 `data/logs`，保留策略见 [docs/operations.md](docs/operations.md)）。`AGENT_LOG_CONTENT`（默认 `1`）置 `0` 后 `[msg]`/`[reply]` 等适配层日志**以及引擎侧内容日志**（LLM 输出、技能参数、事实/蒸馏原始输出、提醒与主动消息正文）只记 `<len=N>` 与段类型、不落正文。web 的 `/api/logs*` 是「响应体不含消息正文」的**唯一显式例外**（含正文前 200 字）——**勿分享截图**。

## 目录结构

```text
agent-demo/
├─ agentcore/              # 纯 Python 包，不依赖 NoneBot
│  ├─ llm/                 # LLM 客户端（供应商无关）
│  ├─ embedding/           # Embedding 客户端（OpenAI 兼容 / 本地降级）
│  ├─ loop/                # tool-loop 引擎
│  ├─ personas/            # 人格系统（md 定义 + manager）
│  ├─ skills/              # 技能注册表 + 内置工具 + data/skills/*.py 自定义加载器（旧 agentcore/tools 已并入）
│  ├─ memory/              # 会话/记忆存储（内存/PG）+ 归档
│  ├─ rag/                 # RAG 摄取/检索/脱敏/蒸馏（M5）
│  ├─ workspace/           # LLM 沙箱工作区（白名单命令 + fs）
│  ├─ backup/              # 数据库备份/恢复
│  ├─ render/              # 回复渲染成图（MD 表格 → PNG；本机概览卡片 → PNG）
│  ├─ dashboard.py         # 看板只读 API 客户端（概览/曲线取数）
│  ├─ multiagent/          # 多 Agent 编排（M6，空壳）
│  └─ scheduler/           # 定时任务（M7）
├─ plugins/
│  └─ qq_agent_adapter/    # NoneBot 薄插件
│     ├─ matcher.py        # 消息路由（私聊 / 群唤醒词 / @机器人）+ 防抖 + 并发闸门
│     ├─ pipeline.py       # payload 组装、引用/转发解析、图片管线、最近图片缓冲
│     ├─ outbound.py       # 长回复分层投递 + 出站节流
│     ├─ debounce.py       # 按 chat+user 防抖合并（条数上限即结算）
│     ├─ group_context.py  # 群聊上下文环形缓冲（可选）
│     ├─ lifecycle.py      # 停机顺序编排（scheduler → flush → aclose）
│     ├─ wakewords.py      # 唤醒词加载/匹配/剥离（触发与剥前缀共用）
│     ├─ acl.py            # 权限控制（含戳一戳用的严格判据）
│     ├─ sink.py           # 主动推送
│     ├─ poke.py           # 戳一戳 → 本机概览图（notice 事件，不走 LLM）
│     ├─ memory_route.py   # /memory 查看/删除/清空自己的长期记忆
│     └─ admin.py          # /aihelp /reset /status /skills /kb /usage /push /skill …
├─ data/kb_samples/        # 知识库样例语料（本地自备，gitignore 不入库）
├─ data/budget/            # 成本用量账本（运行时生成，gitignore）
├─ data/logs/              # 落盘日志（运行时生成，含聊天明文，gitignore）
├─ scripts/                # 运维脚本（备份恢复 / 批量导入样例语料 / wiki 子集同步）
├─ docs/                   # 主题文档：配置 / 消息与媒体 / 记忆与知识库 / 群组 / 技能与权限 / 人格语音 / 运维 + 通用规范
├─ review/                 # 全部评审产物：REVIEW-*.md 报告 + FIX-*.md 修复记录 + REVIEW-WORKFLOW.md
├─ tests/                  # pytest 测试
├─ bot.py                  # NoneBot 启动入口
├─ AGENTS.md               # 交接文档：分层边界、硬性纪律、不变量、历史坑
├─ config.yaml             # Agent 行为配置
├─ docker-compose.yml      # PostgreSQL + pgvector
├─ pyproject.toml
└─ README.md
```

## 阶段里程碑

| 阶段 | 目标 | 状态 |
|---|---|---|
| M0 | 回声跑通，协议端 ↔ NoneBot ↔ 薄插件互通 | ✅ |
| M1 | 单 Agent 无工具，会话入 PG/内存，ACL | ✅ |
| M2 | 工具调用（fetch_url / 天气 / 计算） | ✅ |
| M3 | 联网搜索（博查/Tavily） | ✅ |
| M4 | 长期记忆（facts 抽取 + pgvector 召回） | ✅ |
| M5 | RAG 知识库（摄取 / 检索 / 每日蒸馏） | ✅ |
| M6 | 多 Agent（supervisor + expert） | ⏳ |
| M7 | 调度（蒸馏/备份/提醒 + 成本预算 + 日志归档） | ✅ 成本预算 ✅ / 日志归档 ✅ / 调度面 ✅（kb_digest、archive_prune、db_backup、reminders、push、nudge 六个 job 已接线）/ **定时内容推送 ✅**（LLM 生成 + 模板兜底，见 [docs/features-community.md](docs/features-community.md)） |
| M8 | 主动联系（沉默检测器） | ⏳ P1–P3 已落地（静默/晚睡/活跃三重保护、6–24h→+48h→第 14 天告别、历史写回、热点轮询 + 爆点抢占、记忆事件、Web「主动」页全可编辑 + 2FA）；默认关闭。2026-10-24 加固（爆点不占告别名额、生成后复查活跃、事件追问可达、生日跨年、解析与围栏、停用行可恢复等，见 [review/AC-proactive-nudge.md](review/AC-proactive-nudge.md) §K） |
> 另：M2 期间同步落地了通用 **Skill 系统**（动态安装/卸载、权限控制、YAML 清单自装），当前全部内置能力均以 skill 形式注册。

## 开发

```bash
# 格式化
ruff format .

# Lint
ruff check .

# 测试
pytest

# 性能 / 内存泄漏基线（默认跳过，显式启用）
RUN_PERF=1 pytest tests/test_perf.py -s
```

详细开发规范见 [CONTRIBUTING.md](CONTRIBUTING.md)（含[版本与分支管理](CONTRIBUTING.md#版本与分支管理)：`main` 保持可发布，每个版本开 `release/<version>` 分支，新改动走新分支）。

## License

MIT — 详见 [LICENSE](LICENSE)。
