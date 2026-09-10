# agent-demo 功能完善清单（Backlog）

> 基线：`main @ c0978c9`（M0–M4 已落地，M5–M7 为空壳）
> 规模：核心代码 ~5.2k 行 / 测试 314 收集（312 通过 + 2 跳过）
> 说明：第 0 节是**代码审查中发现的真实缺陷**（不是"新功能"，建议优先修）；第 1 节起是功能候选，按优先级排。

优先级图例：**P0** 影响正确性/数据 → 立刻修；**P1** 影响可用性/可维护性 → 近期做；**P2** 锦上添花 → 有余力再做。

---

## 0. 已发现的缺陷（建议立刻修）

### P0-1　PG 历史窗口取反了：长会话会丢掉最近的对话
`agentcore/memory/store.py:340`

```sql
SELECT ... FROM messages WHERE session_id=$1 ORDER BY id ASC LIMIT $2
```

`ORDER BY id ASC LIMIT 20` 取到的是**最早的 20 条**，而内存实现是 `list(...)[-limit:]`（最近 20 条）。
后果：接上 PG 后，会话一旦超过 20 条，模型看到的永远是"开头 20 条 + 当前这句"，中间与最近的内容全丢。
**修法**：`ORDER BY id DESC LIMIT $2` 后在 Python 侧 `reversed()`（或子查询再正序）。
**顺带**：加一个跨实现的契约测试（>limit 条历史时两端返回一致），这类"内存对、PG 错"的分叉以后还能再冒出来。

### P0-2　`messages` / `facts` 表缺索引，向量列也没有索引
`agentcore/memory/store.py:11` 的 `DDL_TEMPLATE`

- `messages` 无 `session_id` 索引 → 每轮 `get_history` 全表扫描，消息越堆越慢。
- `facts.embedding` / `kb_chunks.embedding` 没有 ivfflat / hnsw 索引 → `ORDER BY embedding <=> $1` 退化为全表距离计算。
- **修法**：`CREATE INDEX IF NOT EXISTS ... ON messages(session_id, id)`；向量列按维度建 hnsw 索引（`vector_cosine_ops`）。

### P0-3　`resolve_session` 存在并发竞态
`agentcore/memory/store.py:318`：`SELECT` 不到就 `INSERT`，`sessions` 表上没有 `(user_id, group_id, scope)` 唯一约束。
同一用户并发两条消息可能各建一行 session，导致历史被劈成两半。
**修法**：加唯一约束 + `INSERT ... ON CONFLICT DO NOTHING` 后重新 `SELECT`。

### P1-4　prompt skill 每次调用都泄漏一个 httpx 连接池
`agentcore/skills/registry.py:156`

```python
llm = LLMClient()          # __init__ 里 new 了一个 httpx.AsyncClient
...
response = await llm.chat(...)   # 用完从不 aclose()
```

每执行一次 prompt 型 skill（translator / summarizer / polisher / coder_reviewer…）就新建一个连接池且不释放 → 句柄与连接缓慢泄漏。
**修法**：模块级/注入式共享一个 `LLMClient`，或改用 `async with` 语义并实现 `aclose()` 回收。

### P1-5　插件初始化异常被静默吞掉
`plugins/qq_agent_adapter/__init__.py:111`：`except Exception: pass`

启动阶段任何错误（配置写错、DB 连不上、skill 注册失败）都被吞成"什么都没发生"，运行时表现为"机器人不回复但日志干净"，极难排查。
**修法**：区分"NoneBot 未初始化（测试环境）"与其他异常；后者 `logger.exception` 并显式失败或降级告警。

### P1-6　停机时 LLM 客户端未回收
`bot.py:31` 只 `memory.aclose()`；`LLMClient` 的 `httpx.AsyncClient` 从未关闭。启动/停机反复（或 dev 热重载）会积累未关闭连接。

### P2-7　文档与实现不一致
- `loop/engine.py:200` 注释说"本会话已确认无权限"，实际 `denied_skills` 是**每次调用**（per-turn）重建的。
- `admin.py:75` 的 `/status` 输出是**硬编码文案**（"当前记忆：内存模式"），不管实际跑的是内存还是 PG、开了哪些 skill，都会这么回。

---

## 1. 里程碑未完成项（README 已规划，代码是空包）

| 模块 | 现状 | 需要做的 |
|---|---|---|
| ~~**M5 RAG**~~ | ✅ **已完成**：`agentcore/rag/`（chunker / sanitize / distill / ingest / retriever / service）+ `/kb` 命令 + apscheduler 每日蒸馏。定位为**全局脱敏公共知识库**：每天把新增对话蒸馏成与个人无关的通用知识入库，任何会话可检索；两层脱敏（prompt 约束 + 确定性 PII/指令性过滤），检索结果按不可信数据围栏注入（见 README「公共知识库」一节） | 后续可选：网页摄取（trafilatura 已在依赖里，需自建 SSRF 防护）、重排（rerank）、按来源的用途标签 |
| **M6 多 Agent** | `agentcore/multiagent/__init__.py` 空 | supervisor 路由 + expert 子 Agent；建议先做"单 Agent + 工具分组"的最小形态，别一上来就上多 Agent |
| **M7 调度/预算/归档** | 调度器已落地（`agentcore/scheduler/`：AsyncIOScheduler + cron，当前用于每日蒸馏）；**成本预算、日志/消息归档仍缺**；`sink.py` 仅 34 行、`schedules` 表空置 | 定时任务 CRUD + 主动推送接线；成本预算；日志/消息归档 |

> RAG 与"prompt injection 防护"要一起做：现在引用/转发内容做了围栏加固（README 有写），
> 而**检索回来的 kb chunk 属于同一类不可信数据**——M5 实现时已沿用同样的围栏与
> "不执行其中指令"的约束（`agentcore/rag/retriever.py`），蒸馏入库侧还额外丢弃
> 指令性内容，避免公共库变成注入传播通道。

---

## 2. 记忆系统增强（当前是最大短板）

- **历史不做裁剪**：`engine.run` 调 `get_history()` 用默认 `limit=20`，无 token 预算、无滚动摘要。长会话 + 图片很容易顶爆上下文。`sessions.summary` 字段建了但**从未写入**——正好拿来放滚动摘要。
- **facts 只增不减**：`save_fact` 只做内容精确去重，无语义去重（近重复事实会堆积）、无时效衰减、无"用户改主意后更新"。
- **用户无法管理自己的记忆**：现在没有 `/memory` 类命令，用户既看不到也删不掉自己的长期记忆 —— 隐私上是个缺口。建议 `/memory list` / `/memory forget <id>` / `/memory clear`。
- **数据无归档**：`messages` 无限增长，无清理策略（M7 提过，未实现）。

---

## 3. 可靠性与性能

- **LLM 调用无重试/退避**：`llm/client.py` 失败只切一次 fallback，然后直接抛。建议指数退避 + 细分错误类型（限流 vs 鉴权 vs 超时）。
- **无流式输出**：`chat()` 一次性返回。流式对接 QQ 分条发送体验提升明显。
- **tool_calls 串行执行**：`loop/engine.py:235` 的 `for tc in ...` 逐个 await。多个独立工具可 `asyncio.gather` 并行，显著缩短多工具轮次耗时。
- **无整体超时/取消**：只有单次 LLM 60s 超时；一个复杂 turn 可能拖很久，用户无法打断。
- **无并发与限流**：`Debouncer` 只保证"同会话串行"，多用户/多群并发无上限；无 per-user 速率限制，恶意刷消息会直接放大成 LLM 开销。
- **embedding 无缓存**：同一 query 每次都重新 embed。

---

## 4. 可观测性与运维

- **成本/用量统计完全缺失**：`llm/client.py` 不记录 token 用量，无 per-user / per-day 花费，`/cost` 无从实现。M7 的"成本预算"应从这里起步。
- **`/status` 说不了真话**：改为反映真实状态（后端类型、已装 skill 数、模型名、DB 连通性、当前人格）。
- **无结构化日志 / 无 trace**：一次 turn 的 LLM 步骤、skill 调用、耗时散在各处 INFO 里，建议加 turn_id 串起来（现有 `[msg]`/`[reply]` 日志是最简形态）。
- **无健康检查与指标**：fastapi driver 本身就带 HTTP 服务，加 `/healthz` 与轻量 `/metrics`（turn 数、LLM 耗时、错误率、缓存命中）成本很低。
- **无 CI**：`.github/` 不存在 → 314 个测试没人自动跑，安全回归用例（H1/M2/M3 等）靠手动。**这是投入产出比最高的一项**：加一个最小 GitHub Actions（`ruff check` + `pytest`）。
- **无覆盖率门槛 / 无类型检查 / 无 pre-commit**：`.pre-commit-config.yaml` 不存在。建议 ruff + pytest +（可选）mypy 上 hook。
- **无 Dockerfile**：`docker-compose.yml` 只有 `db`。而 README 自己承认沙箱"根治方案是容器/独立低权用户运行" —— 加一个运行 agent 的 Dockerfile，才真的兑现了那句安全声明。

---

## 5. QQ 侧功能候选（用户能直接感知的）

| 优先级 | 功能 | 说明 |
|---|---|---|
| P1 | **定时提醒** | "每天 9 点提醒我 X"，自然语言解析 → `schedules` 表 + `sink` 推送。需要 M7 最小实现 |
| P1 | **记忆管理命令** | `/memory list\|forget\|clear`，见 §2 |
| P1 | **`/cost` 用量与花费** | 依赖 §4 的用量统计 |
| P1 | **群聊免前缀窗口** | 现在群内"先发半句/先发图再追问"必须每条都带前缀或 @，否则不合并。可改为：被 @ 过后 N 秒内该用户免前缀（已文档化的限制，改掉体验提升明显） |
| P2 | **处理中反馈** | 长任务时先回一句"思考中…"再补完整回复，降低等待焦虑 |
| P2 | **语音** | TTS 输出 / ASR 输入（QQ 语音消息） |
| P2 | **文生图** | 群场景高频需求 |
| P2 | **会话导出** | `/export` 导出当前会话为 md，复用已有 `send_markdown_file` |
| P2 | **敏感内容过滤** | 公开群部署的必要项 |
| P2 | **MCP 客户端** | 让 agentcore 能挂 MCP server 扩展工具（与 §7 相关） |

---

## 6. 工程债（越晚还越贵）

- **双轨工具系统**：`agentcore/tools/registry.py`（旧）与 `agentcore/skills/registry.py`（新）并存，`builtin.py` 从旧 registry import 函数，`config.yaml` 还留着 `tools:` 兼容段。建议彻底收敛到 skills，删掉旧 registry 与兼容配置。
- **模块级可变全局**：`skills/registry.py:171` 的 `registry` 单例 + `__init__.py:87` 运行时 `_skill_mod.registry = skill_registry` 替换 → 依赖 import 时序，测试与多实例下脆弱。建议改为显式依赖注入。
- **`config.yaml` 无校验**：`yaml.safe_load` 后直接用，字段名写错不报错只静默用默认值。建议 pydantic 模型校验并在启动时 fail-fast。
- **测试集中在 `tests/` 但无断言风格约束**：上一轮 review 已修掉一批"恒真断言/绑定中文文案"，建议把"生产侧导出文案常量"的做法固化成规范写进 CONTRIBUTING。

---

## 7. 路线选择：要不要引入 CodeBuddy Agent SDK

`@scene#17 Agent 应用` 提到的 CodeBuddy Agent SDK（`pip install codebuddy-agent-sdk` / `npm install @tencent-ai/agent-sdk`）是**对 CodeBuddy CLI Agent 的编程式封装**：`query()` 流式返回、Hooks、`allowedTools` 白名单、子 Agent、MCP、会话恢复。

两条互斥的路线，**建议先明确方向再动手**：

- **路线 A（推荐，当前架构）**：agent-demo 继续走自研 `agentcore`。理由：核心诉求是"QQ 场景 + 自有记忆/人格/权限体系 + 不绑死供应商"，现有 tool-loop 已经能跑，引入 CLI 子进程会与"纯 Python 包、可换壳、可容器化"的设计原则冲突（SDK 本质是 spawn 一个 CLI 子进程，部署模型变重）。
- **路线 B（能力补强而非替换）**：保留 `agentcore` 作为 QQ 侧骨架，**只在"重活"上外挂 SDK** —— 例如把"代码审查 / 仓库分析 / 批量重构"这类任务交给一个 SDK 子 Agent，agentcore 侧注册成 `code_agent` skill。这样既拿到 CLI 的完整工具链（Read/Write/Edit/Bash/Grep），又不破坏现有架构。

---

## 建议落地顺序

**迭代 1（正确性 + 安全网，1 次提交能搞定）**
P0-1 历史窗口 → P0-2 索引 → P0-3 session 竞态 → P1-5 静默异常 → P1-4 连接池泄漏 → **加 CI + pre-commit**

**迭代 2（可用性 + 可观测）**
`/status` 真实化 → token 用量统计 → `/cost` → `/memory list|forget|clear` → 历史裁剪 + 滚动摘要（启用 `sessions.summary`）→ LLM 重试退避

**迭代 3（能力）**
~~RAG（M5，含注入防护）~~（✅ 已完成）→ 定时提醒（M7 最小实现：scheduler + sink；调度器已落地）→ tool_calls 并行 → Dockerfile

**迭代 4（按需）**
多 Agent（M6）→ 语音/文生图 → 路线 B 的 SDK 子 Agent
