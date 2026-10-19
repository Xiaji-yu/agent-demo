# AC Plan — 设置簇框架 + 多 LLM Provider（OpenAI / Anthropic）

> 决策来源：2026-10-19 与用户的 9 轮问答。本文是**验收标准**，不是设计随笔：
> 每条 AC 都必须能用命令或断言验证；执行时按 Phase A → Phase B 推进。

## 0. 已定决策

| 议题 | 决策 |
|---|---|
| 簇粒度 | 按业务簇（12 草稿，按"一簇一个开关"原则拆成 17 个，见 §1） |
| 导航 | 单页折叠面板 + 顶部簇锚点 + 跨簇搜索 |
| 无布尔开关的簇 | **新增显式布尔 env**：`AGENT_MUSIC_ENABLED`、`SEARCH_ENABLED`、`AGENT_PERSONA_GROWTH_ENABLED` |
| 灰置拦截 | **前后端都拒写**：gate 为关时写同簇非 gate 项 → 409 |
| LLM 协议 | **OpenAI 兼容 + Anthropic Messages**（两套 adapter） |
| Provider 存储 | 独立 `data/llm-providers.json`（0600、原子写、备份、schema version） |
| Provider key | **允许 web 写，write-only**：永不回显；只在写入请求里出现 |
| 旧配置 | 首次加载自动从 `LLM_MODELS` / `LLM_FALLBACK_*` / `LLM_MODEL` 迁移 |
| 验收 | AC 落本文；测试/临时实例全绿后，**经用户确认时间**重启线上 |

## 1. 簇定义（17）

`gate` = 该簇首项开关；`gate=None` 表示不做灰置。gate 为 `只读` 的簇，其余项本来也不可写。

| id | 标题 | gate | 说明 |
|---|---|---|---|
| `llm` | 模型与 LLM | — | 独立框架（多 provider / 协议 / key / 测试连接 / 排序） |
| `embedding` | Embedding 与向量 | — | 与聊天模型解耦；切换会破坏索引，不随 provider 池切换 |
| `messaging` | 消息与交互 | — | 唤醒词 / 防抖 / 群上下文 / 戳一戳 / 帮助菜单 |
| `vision` | 图片与视觉 | `AGENT_VISION` | 识图限额 / 最近图片 / 媒体配额 |
| `memory` | 长期记忆 | `agent.extract_facts` | facts 抽取与召回 |
| `history` | 历史与摘要 | `agent.summary_enabled` | 裁剪预算 / 摘要参数 / 过时警示 |
| `kb` | 知识库 | `AGENT_KB_ENABLED` | 检索 / 蒸馏 / 入库上限 |
| `search` | 搜索 | `SEARCH_ENABLED`（新） | provider / 条数 / 语言（key 仍只读） |
| `music` | 点歌 | `AGENT_MUSIC_ENABLED`（新） | 指令 / 时长 / 下载 / 缓存 / 白名单（只读） |
| `stats` | 群统计 | `AGENT_GROUP_STATS` | 榜单 / 词云 / 周报 |
| `outbound` | 出站与长回复 | — | 分层投递 / 节流 / 表格转图 |
| `archive` | 聊天记录归档 | `AGENT_ARCHIVE_ENABLED`（只读） | 目录 / 保留期（只读） |
| `backup` | 数据库备份 | `AGENT_BACKUP_ENABLED`（只读） | 目录 / 保留 / cron（只读） |
| `group_archive` | 群原文归档 | `AGENT_GROUP_ARCHIVE`（只读） | 保留 / flush / 缓冲（只读） |
| `push` | 定时推送 | `AGENT_PUSH_ENABLED` | push.* 标量 + tick / 重试 |
| `persona` | 人格 | `AGENT_PERSONA_GROWTH_ENABLED`（新） | 成长层轮数 / 字数 |
| `advanced` | 高级（只读） | — | 协议端 / 数据库 / Web / 工具与沙箱 / 运维 |

> 相对 12 簇草稿的拆分理由：`agent.extract_facts` 与 `agent.summary_enabled` 是**两个独立功能**，
> 不能共用一个 gate；归档 / 备份 / 群原文归档也是三个独立开关。否则"关一个、灰一片"会误伤。

## 2. 新增开关语义

| 键 | 默认 | 读取点 | 生效 | 关闭行为 |
|---|---|---|---|---|
| `AGENT_MUSIC_ENABLED` | `1` | `music_route.play_music_skill` 每次调用 | live | 返回"点歌功能已关闭"，不发起下载 |
| `SEARCH_ENABLED` | `1` | `agentcore/skills/search.py` 的 `search_web` / `search_multi` 每次调用 | live | 返回"搜索功能已关闭"；不读 key、不发请求 |
| `AGENT_PERSONA_GROWTH_ENABLED` | `1` | 插件初始化构造 `GrowthManager` 前 | boot | 不构建 GrowthManager（等价 interval=0） |

三者都进 `settings_spec.ENV_SPECS` 与 `.env.example`（schema 守卫要求两边集合一致）。

## 3. AC — 簇与灰置（Phase A）

- **A1** `settings_spec.CLUSTERS` 覆盖 `ENV_SPECS + YAML_SPECS` 的每一项；每项 `cluster` 非空且属于 `CLUSTERS`。
- **A2** `/api/settings` 返回 `clusters: [{id,title,description,gate_key,gate_value,order,items:[...]}]`；有 gate 的簇里 gate 项**排第一**。
- **A3** gate 为 `false` 时：
  - `POST /api/settings/write` 写同簇非 gate 键 → **409** `detail=cluster_disabled:<gate>`；
  - gate 自身仍可写（否则无法重新开启）；
  - 只读 gate 的簇（archive/backup/group_archive）同理。
- **A4** gate 值从**文件**读取（`.env` / `config.yaml`），不从 `os.environ`——否则"已存盘待重启"的关闭状态会被旧进程值绕过。
- **A5** 前端：顶部簇锚点；每簇 `<details>`；gate 关闭时同簇非 gate 控件 `disabled` 且灰置；gate 为 boot 时 badge 显示"重启生效"。
- **A6** 搜索命中跨簇；命中的簇自动展开，未命中的簇隐藏。
- **A7** 新增三开关的行为用例（开/关各一），且默认值 = 开。
- **A8** 变异复核：去掉后端 gate 校验 / 把 gate 排到非首位 / gate 读 `os.environ` 而非文件 / 前端不 disabled —— 均必须被用例抓住。

## 4. AC — 多 LLM Provider（Phase B）

### 4.1 存储

`data/llm-providers.json`（`AGENT_LLM_PROVIDERS_FILE` 可覆盖）：

```json
{
  "version": 1,
  "active": "stepfun",
  "providers": [
    {"id": "stepfun", "name": "StepFun", "protocol": "openai",
     "base_url": "https://api.stepfun.com/v1", "model": "step-3.7-flash",
     "api_key": "sk-...", "enabled": true},
    {"id": "claude", "name": "Claude", "protocol": "anthropic",
     "base_url": "https://api.anthropic.com", "model": "claude-...",
     "api_key": "sk-ant-...", "enabled": true}
  ]
}
```

- **B1** 0600 + 同目录原子写 + 改前备份（滚动）+ symlink fail-closed + 写后 `json.load` 校验 + 失败还原。
- **B2** `GET /api/settings` 的 provider 只出 `id/name/protocol/base_url/model/enabled/key_set`，**永不出 key**；`base_url` 在 provider 框架内是管理员可见字段（与"响应体不含 base_url"的旧约束冲突 → 记录为例外：仅 `llm.providers` 面、仅 web 管理员，且 AC 断言 key 不出现）。
- **B3** `POST /api/settings/llm/providers`：新增 / 编辑 / 删除 / 排序 / 启用 / 设 active；`api_key` 缺省或空 = 保持不变；`clear_key=true` 才清空。
- **B4** `POST /api/settings/llm/providers/{id}/test`：用该 provider 的协议发一次最小请求；失败返回状态码，不改配置。

### 4.2 协议适配

- **B5** `agentcore/llm/protocols.py`：`OpenAIProtocol`（现状）与 `AnthropicProtocol`（`/v1/messages`、`x-api-key`、`anthropic-version`）。
- **B6** Anthropic 对话映射：
  - `system` 消息 → 顶层 `system`；
  - assistant `tool_calls` → `tool_use` content block；
  - `role=tool` + `tool_call_id` → `tool_result` content block（连续结果合并进同一条 user）；
  - 响应 `text` + `tool_use` → 统一回 OpenAI 形状（`choices[0].message.content` 与 `tool_calls[].function.{name,arguments}`）；
  - `stop_reason=tool_use` → `finish_reason=tool_calls`；
  - `usage.input_tokens/output_tokens` → `prompt_tokens/completion_tokens/total_tokens`。
- **B7** 两种协议都能完成一次**带 tool call** 的往返（用假 HTTP 传输断言请求体与归一化响应，不花真实 key）。
- **B8** Embedding 不纳入 provider 池（Anthropic 无 embeddings）；`EMBEDDING_*` 独立，切 provider 不影响它。

### 4.3 路由与迁移

- **B9** 降级链：active → 刚换下的旧主 → 其余 **enabled** provider（数组序）→ legacy `LLM_FALLBACK_*`（仅在迁移缺失/失败时）。disabled 跳过。
- **B10** 首次加载且文件缺失：从 `LLM_MODEL` + `LLM_MODELS` + `LLM_FALLBACK_*` 自动生成 JSON；幂等（已存在不覆盖）；生成后 legacy 键在设置页标"已迁移（只读）"。
- **B11** 文件损坏/不可读：**不崩启动**，告警并回落 env 旧路径（`_CFG`）。
- **B12** `/model` 命令仍可用：`/model <id|model 名>`、`/model reset`；测试连接失败不改 active。
- **B13** 每次请求的 usage 按**实际 provider.model** 记账。

## 5. 验证与交付

- **V1** 默认套件全绿；ruff check + format --check；`check_review_docs.py --check-stats` 一致。
- **V2** 变异复核：gate 校验、gate 排序、文件读取、前端 disabled、key 回显、Anthropic 工具映射、迁移幂等 —— 每项改坏必红。
- **V3** 临时实例截图：簇 + 灰置 + LLM provider 面板。
- **V4** 线上重启（用户确认时间）后：设置页可见 16 簇；关闭 `AGENT_VISION` 后视觉簇灰置且后端拒写；LLM provider 列表显示已迁移项；`/model` 正常。
- **V5** 文档同步：`.env.example`、`docs/configuration.md`、`docs/features-interaction.md`、`AGENTS.md`、`BACKLOG.md`。

## 6. 风险与回滚

| 风险 | 缓解 |
|---|---|
| Anthropic 工具调用映射错 → 工具轮次坏 | 归一化层单测覆盖 text/tool_use/tool_result 混合；保留 OpenAI 路径默认 |
| Provider key 进日志/回包 | key 只进 0600 JSON；API 只回 `key_set`；审计用 `masked`；断言响应体不含 key |
| 迁移把旧配置写坏 | 只在文件缺失时生成；原 `.env` 不改；损坏时回落 env 路径 |
| Gate 误灰置 | gate 与簇的映射写在 schema；A1 全量覆盖测试 |
| 线上重启中断 | 先跑测试与临时实例；重启走既有 `reboot.py`（flush → aclose） |

## 7. 执行顺序

1. Phase A：schema cluster + 3 开关 + 后端 gate + 前端簇/灰置 + 测试。
2. Phase B：provider 存储 + 协议 adapter + 迁移 + API + 前端 LLM 面板 + 测试。
3. V1–V5，最后经用户确认重启线上。


## 8. 执行结果（2026-10-19）

**Phase A（簇 + 开关）**
- `settings_spec.py`：`EnvSpec.cluster` + `ClusterSpec/CLUSTERS`（17 簇）+ `cluster_of_env`；`ENV_SPECS` 165 键（可写 108）。
- `config_yaml.py`：`YamlSpec.cluster`；`YAML_SPECS` 35 项（可写 29）。
- 新开关：`AGENT_MUSIC_ENABLED` / `SEARCH_ENABLED`（调用时生效）、`AGENT_PERSONA_GROWTH_ENABLED`（启动期；抽 `_growth_active()` 以便单测关闭分支）。
- 后端 gate：`_gate_value` 从**文件**读；`_ensure_cluster_enabled` 在 preview/write 都执行；`cluster_disabled:*` → 409。
- 前端：17 簇导航 + 单簇 panel（一次一簇，同 S1/S2 的 framework 块）+ 跨簇搜索；gate 排第一；关闭时同簇非 gate 行 `is-locked` 灰置、按钮替换为"簇已关闭"。

**Phase B（多 provider）**
- `agentcore/llm/providers.py`：JSON 存储（0600、备份、写后校验、symlink fail-closed）、迁移、public 无 key。
- `agentcore/llm/protocols.py`：OpenAI Chat + Anthropic Messages（system 提升 / tool_use↔tool_calls / 连续 tool_result 合并 / usage+stop_reason 归一化）。
- `LLMClient`：provider 链（active → 旧主 → 其余 enabled）、协议分派、usage 按实际 provider、`reload_providers` 跟随文件 active。
- web：`/api/settings/llm/providers`（save/delete/set_active/reorder/test，两段确认；key 只写不回显）；设置页 provider 面板（设主/测试/编辑/上下移/删除/新增）。
- `/model` 兼容：`/model <id|model>`、`/model reset`。
- 启动迁移：插件初始化 `ensure_migrated()`（幂等）；真实 `.env` 预览迁移出 2 个 provider（StepFun active + DeepSeek fallback），未落盘（重启时执行）。

**验证**
- 默认套件 **2683 通过 + 58 跳过**；ruff check + format --check 全绿；`check_review_docs --check-stats` 一致。
- 变异复核：Phase A 7/7 + Phase B 7/7 = **14/14**。
- 临时实例截图：17 簇导航 + LLM Providers（StepFun / Claude / Local-disabled）+ 通用设置行；JS `node --check` 通过。
- 未触碰真实 `.env` / `config.yaml` / `data/`；provider 文件只在启动时生成。

**线上验证与追加修复（23:29 重启后）**
- 重启成功：`agent-demo.service` active，`data/llm-providers.json` 0600，迁移出 2 个 provider（StepFun active + DeepSeek），OneBot WS 重连。
- `/api/settings`：17 簇、0 errors、provider 列表只含 `key_set`、响应体 `sk-`/`api_key` 命中 0。
- **发现并修复 1 处缺陷**：provider 模式下写 `LLM_MODEL` 静默无效——`apply_runtime → set_active(ModelEntry)` 只改 legacy 活跃位，而 `chat()` 走 `provider_chain()`。修复：迁移后 `LLM_MODEL`/`LLM_FALLBACK_MODEL` 标记 `legacy` 并拒写（409 `legacy_model_key`），前端显示"已迁移 → Providers"；`/model` 的 provider 匹配扩展到 id/model/展示名/唯一前缀。回归：`test_llm_model_is_legacy_and_readonly_when_providers_exist`、`test_switch_by_unique_prefix`。
- Providers 面板 `set_active` 线上实测：step-3-7-flash → deepseek-flash → step-3-7-flash 全部 200。
- **交互增强（后续）**：簇导航 + 单簇 panel（不再长列表）；provider 切换 toast/行高亮/忙碌态；设置编辑改模态表单，布尔与常用档位用下拉（未设置预选默认值）；provider 编辑从连续 prompt 改为表单；**两段确认改为页内模态框**（不弹浏览器原生 confirm/prompt），Esc/点遮罩取消。
