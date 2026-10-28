# 配置说明

> 本文自 `README.md` 拆分而来（2026-10-16）；返回 [README](../README.md)。


核心配置在 `config/agent.json`（容器部署）或 `.env`（本机开发 / systemd 部署），
加上 `config.yaml`。**完整键列表以 `.env.example` 为准**——键名两边完全一致（本节只列
常用项；缓冲区/调度类键的生效时机见各文件内注释，改动 `AGENT_RECENT_IMAGE_*`、
`AGENT_GROUP_CONTEXT_*` 等进程内缓冲区键需重启生效）。

> 新手照着填走 [config-quickstart.md](config-quickstart.md)（极简 JSON 模板 + 排障），
> 本文讲语义与边界。两者同时存在时优先级是
> **真实环境变量 > `config/agent.env`（web 写面）> `agent.json` > `.env`**。

## 必配 / 选配 约定

- **必配**：不设置则对应功能不可用——示例值只是占位符，照抄不会工作。
- **选配**：有可用默认值，或仅用于调优/限流/开关；不设置也能工作（opt-in 功能标「启用该功能时必须设置」）。

### 必配清单

| 配置项 | 说明 |
|---|---|
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | OpenAI 兼容端点与模型（不绑死供应商） |
| `DATABASE_URL` | PostgreSQL + pgvector；留空 = 内存模式（重启清零，仅适合试跑） |
| `SUPERUSERS` / `ALLOWED_GROUPS` | 管理员与允许响应的群；**留空 = 无**（fail-closed：管理面/群聊不可用） |
| `ONEBOT_ONEV11_ACCESS_TOKEN` | 协议端启用鉴权时必填 |

### 选配项去向

| 分组 | 键 | 详解 |
|---|---|---|
| LLM 主备与采样 | `LLM_FALLBACK_*` · `LLM_TEMPERATURE` · `LLM_MAX_TOKENS` · `LLM_FALLBACK_NOTIFY*` | 本文下文 |
| 服务端与 Web | `DRIVER` · `HOST` · `PORT` · `AGENT_WEB_*` | 本文下文 + [features-interaction.md](features-interaction.md) |
| 消息与媒体 | `AGENT_WAKE_WORDS` · `AGENT_DEBOUNCE*` · `AGENT_VISION*` · `AGENT_REPLY_*` · `AGENT_OUTBOUND_*` 等 | [features-messaging.md](features-messaging.md) |
| 记忆与知识库 | `EMBEDDING_*` · `AGENT_KB_*` · `AGENT_WIKI_*` · `SEARCH_*` | [features-memory-and-kb.md](features-memory-and-kb.md) |
| 群组与社区 | `AGENT_GROUP_STATS*` · `AGENT_GROUP_ARCHIVE*` · `AGENT_PUSH_*` · `AGENT_REMINDER_TICK` | [features-community.md](features-community.md) |
| 工具与权限 | `AGENT_PERMISSION_LEVEL` · `WORKSPACE_DIR` · `AGENT_SSH_*` · `AGENT_DB_*` · `AGENT_LOG_ALLOWLIST` | [features-skills.md](features-skills.md) |
| 人格 / 语音 / 总览 | `AGENT_PERSONA_GROWTH_*` · `AGENT_MUSIC_*` · `AGENT_DASHBOARD_*` · `AGENT_POKE_*` | [features-interaction.md](features-interaction.md) |
| 运维与预算 | `AGENT_LOG_*` · `AGENT_ARCHIVE_*` · `AGENT_BACKUP_*` · `AGENT_BUDGET_*` · `AGENT_SHUTDOWN_FLUSH_TIMEOUT` | [operations.md](operations.md) |

> `.env.example` 是**键名的唯一事实源**：每个键一行注释、给出默认值。JSON 里用同样的
> 键名，值一律字符串（数字/布尔也要加引号）；`.env` 里「值内出现 `#` 需加引号」这条
> 写法约定只对 `.env` 成立，JSON 没有这个问题。
## LLM（不绑死供应商）｜**必配**

```env
LLM_BASE_URL=https://api.stepfun.com/v1
LLM_API_KEY=你的key
LLM_MODEL=step-1-flash   # 代码内置默认；部署模板值见 .env.example（当前 step-3.7-flash）
LLM_TEMPERATURE=0.7
LLM_MAX_TOKENS=1024

# 多 provider：2026-10-28 起直接写在 config/agent.json 的 LLM_PROVIDERS 数组里
# （活跃位 LLM_ACTIVE），不再有独立文件。下面这个键是**遗留迁移专用**：从旧版升级时
# 指向旧的独立 providers 文件，首次启动把它并进 agent.json 后即不再读取。
AGENT_LLM_PROVIDERS_FILE=

# 候选模型池（逗号分隔，**legacy**）：仅用于首次迁移到 provider 文件。
# 条目语法：模型[@base_url[|api_key]]；空 base_url/key 表示用主端点/主 key。
LLM_MODELS=

# 运行期切模型的探测超时（秒）；/model 与 web 写 LLM_MODEL 前各探测一次，0 = 不限时
AGENT_MODEL_PROBE_TIMEOUT=20

# 可选 fallback，主线路不通自动切换
LLM_FALLBACK_BASE_URL=
LLM_FALLBACK_API_KEY=
LLM_FALLBACK_MODEL=

# 主备切换/恢复时私聊通知主人（SUPERUSERS）；持续降级按冷却周期提醒
LLM_FALLBACK_NOTIFY=1
LLM_FALLBACK_NOTIFY_COOLDOWN=1800

# Web 总览与全部设置（默认只读；写面需 AGENT_WEB_WRITE=1 + CIDR）。留空 = 整个 web 面不挂载
AGENT_WEB_TOKEN=
AGENT_WEB_ALLOW_CIDRS=
```

**多 provider**：聊天模型配置在 `config/agent.json` 的 `LLM_PROVIDERS` 数组里（见
[features-interaction.md](features-interaction.md#运行期切换模型多-provider)），
支持 OpenAI Chat 与 Anthropic Messages 两种协议。`LLM_MODEL(S)` 与
`LLM_FALLBACK_*` 只用于没有该数组时的回落（legacy）：数组存在时以它为准。
`LLM_FALLBACK_MODEL` 在设置页标为"已迁移"并拒绝写入（409 `legacy_model_key`），
热切换只走 Providers 面板或 `/model`。降级顺序：active → 刚换下的旧主 →
其余 enabled provider（数组序）；只有 active 保留 2 次瞬时重试。`LLM_MODEL`（活跃位）可写 `模型[@base_url]` 形态，
**不接受 key**——key 只属于池条目，因为活跃位的值会进审计日志、web 回包和
`/model` 回执。改池需要重启（池在进程启动时装配）；运行期只能切活跃位。

**主备切换与通知**：每次请求都先打主模型，瞬时故障（超时/限流/5xx）先做
最多 2 次、间隔 0.5s 的快速重试，仍失败才临时切备用（对话不中断）。
切换是逐请求的，所以"当前在用哪个"由客户端自己记，用于两类私聊通知
（推给 `SUPERUSERS`，文案含主/备模型名与错误类型，**不含** key）：

- **已切换备用模型**：主模型不可用时发；持续故障按 `LLM_FALLBACK_NOTIFY_COOLDOWN`
  周期提醒"还在降级"（调到 86400 就近似每次故障只提醒一次）
- **主模型已恢复**：降级窗口内进入的请求在主路径重新成功时发一次
  （恢复通知只由亲历降级窗口的请求发出，并发下不误报）

两类通知**各自记冷却**——恢复通知不会被切换的冷却压掉；抖动时同一类型最多每
冷却期一条。推送失败（QQ 不可达）只记日志，不影响对话。`LLM_FALLBACK_NOTIFY=0`
可整体关闭。用量统计里 fallback 以其模型名自然区分。

**问"你用什么模型"**：直接问 bot（如「你是什么模型」「用的什么接口」）即可——
它会调 `current_model` skill 报出**当前生效**的型号与线路（降级中会说明主模型
当前不可用、正在用哪个，并按降级链顺序报出后面的候选）。模型对自己型号的
自我认知不可靠（会按训练知识自称），所以这句回答以 skill 返回为准，prompt 里
也明确禁止它猜测。

**运行期切模型**：superuser 发 `/model`（查看）、`/model <模型[@base_url]>`
（切换）、`/model reset`（回启动时的装配值）。切换前会发一次 16 token 的探测
请求，失败就不改动；通过后写 web 写面落点（`config/agent.env`）并同步运行态，
重启后仍生效。详见
[features-interaction.md](features-interaction.md#运行期切换模型)。

## OneBot 反向 WS｜**必配**（协议端启用鉴权时）

NoneBot 作为 WS 服务端监听，协议端主动连过来。

- 默认地址：`ws://<本机IP>:8080/onebot/v11/`
- 配置里配鉴权（键名 `.env` 与 JSON 通用）：

  ```env
  DRIVER=nonebot.drivers.fastapi
  HOST=0.0.0.0
  PORT=8080
  ONEBOT_ONEV11_ACCESS_TOKEN=123456
  ```

## 数据库｜**必配**（生产；留空 = 内存降级）

```env
DATABASE_URL=postgresql://qqagent:qqagent@127.0.0.1:5432/qqagent
```

`docker compose up -d db` 的凭据与上一致（`POSTGRES_USER`/`POSTGRES_PASSWORD` 环境变量
覆盖，默认 qqagent/qqagent）。自定义密码若含 `@` 等特殊字符，DATABASE_URL 里必须
URL 编码（`@` → `%40`）——asyncpg 按第一个 `@` 分区，不编码会把密码截断、host 解析失败
（表现为 PG init 失败后静默降级为内存存储）。**密码不要提交进 git。**

留空则使用**内存存储**（M0 可用，重启丢失）。


## 成本预算（M7）｜**选配**

LLM/embedding 每次调用的 token 用量（响应 `usage` 字段）按日累计落盘到
`data/budget/usage-YYYY-MM.json`（按月一个文件、原子写、重启不丢）。`/status` 可查当日
**对话**用量与估算成本，并单独展示 embedding 用量（embedding 不计入对话预算）。

**`/usage`（别名 `/用量`）**——渲染一张用量统计图（表格转 PNG，失败回退文本），包含：

- 今日 / 历史总：对话 token（输入/输出）、embedding token、请求数
- **今日按路由**：每个群 / 私聊的请求与 token（明细自启用新版本起累积）
- **今日按模型**：每个模型的请求与 token（fallback 以其模型名自然区分）

```env
AGENT_BUDGET_DAILY_TOKENS=0      # 每日 token 预算（prompt+completion 合计）；0 = 只记录不限流
AGENT_BUDGET_ENFORCE=0           # 1 = 超预算后聊天/蒸馏直接返回提示（硬闸门）；0 = 只告警
AGENT_PRICE_PROMPT_PER_M=        # 可选单价（元/百万 token），用于 /status 估算当日成本
AGENT_PRICE_COMPLETION_PER_M=
AGENT_BUDGET_DIR=data/budget     # 账本目录（相对路径按 CWD；部署建议用绝对路径）
```

超预算的默认行为是**打 WARNING 继续**；只有显式 `AGENT_BUDGET_ENFORCE=1` 才拦截——
命中时新的对话轮次直接收到「今日预算已用完」提示（**不含内部 token 数字**，避免向普通
成员泄露成本档位）、当日蒸馏跳过（`reason: daily budget exceeded`），次日按日键自动恢复。
闸门在轮次入口**与 tool-loop 每一步**各判一次，中途越过预算会立即中止本轮。

配置值写错（如 `1,000,000`、`1e6`、负数）会**打 WARNING 后回退**而不是静默生效，避免
「以为开了硬闸、其实没开」。账本文件结构损坏时按空账本处理并告警，不会让对话报错。

