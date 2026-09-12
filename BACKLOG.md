# agent-demo 功能完善清单（Backlog）

> **基线**：`main @ 97e4045` + 第四批工作区改动（未提交；M0–M5 已落地；M6 仍为空壳；M7 的**成本预算与日志归档已完成**，仅剩「定时内容推送」）
> **规模**：核心代码 13.8k 行（`agentcore` + `plugins` + `bot.py`，`wc -l` 实测 13800）/ 测试 38 个文件 13.4k 行
> （**923 收集：默认 871 通过 + 52 跳过**；设 `TEST_DATABASE_URL` 后 **914 通过 + 9 跳过**
> ——9 个跳过全部是 `RUN_PERF=1` 门控的性能用例。数字按本行基线用 `pytest -q` 实测，勿手写估算）
> **工具面**：24 个内置工具（`registry.register` 调用点）+ 4 个默认安装的 prompt 技能
>
> **更新说明（2026-09-11，按代码实测重写）**：上一版基线停在 `c0978c9`（314 测试），
> 其中「§0 已发现缺陷」「CI + pre-commit」「M5 RAG」「M7 的调度/归档/备份」**均已落地**，
> 原「建议落地顺序」已作废。旧版内容保留在 git 历史（`git show c0978c9:BACKLOG.md`）。
> 本版分两部分：§1–§4 是**还没做的事**，§6 是**评审已实证、属于必修而非新功能**的缺陷。

优先级图例：**P0** 影响正确性/数据/安全 → 立刻做；**P1** 影响可用性/可维护性 → 近期做；**P2** 锦上添花。

---

## 0. 已完成（仅记结论，不再展开）

| 项 | 状态 |
|---|---|
| 原 §0 全部缺陷（P0-1 历史窗口取反 / P0-2 索引 / P0-3 session 竞态 / P1-4 连接池泄漏 / P1-5 静默异常 / P1-6 停机回收 / P2-7 文档不一致） | ✅ 已修 |
| 工程安全网：GitHub Actions + pre-commit + ruff pin | ✅ 已加 |
| **M5 RAG** | ✅ 已完成，超出原计划（蒸馏 / 两层脱敏 / 不可信围栏 / `/kb`） |
| **M7 的调度面** | ✅ 已落地：`kb_digest`（03:00）、`archive_prune`（03:20）、`db_backup`（03:30）、`reminders`（30s tick）四个 job 全接线（`plugins/qq_agent_adapter/__init__.py:177-216`），`ReminderService` 已接 `sink`（`scheduler/reminder.py:199-209`） |
| **M7 的归档面** | ✅ 已落地：7 天滚动归档（`memory/archive.py`）+ pg_dump/JSONL 双路备份 + 异地镜像 + `restore` / `restore-archive` CLI |
| §6 双轨工具系统 | ✅ 旧 `agentcore/tools/` 已删除，收敛到 skills |
| `/status` 真实化、`/persona`、30+ 工具 | ✅ 已做 |

---

## 1. 下一步必要项（P0/P1）

### A 组：钱与容量 —— 从 demo 转可用的门槛

**A1（P0）token 用量统计 + `/cost` + 日预算熔断**　*已完成（`be13897`）*
- **现状（更新，2026-09-11）**：已落地 `agentcore/budget.py`（按日账本 + 软/硬闸门 + 成本估算），
  `LLMClient._post` 与两个 embedding 客户端均上报 usage，`/status` 展示当日用量；
  **残余**：独立 `/cost` 命令、per-user 维度、超限「降级换小模型」尚未实现（当前是拦截/告警）。
- **后果**：花费不可知；恶意刷消息线性放大成账单；`LLM_MAX_TOKENS=1024` 对 reasoning 模型本就偏小，改了也不知道代价。
- **做**：解析 usage（含 embedding 调用）→ `/cost` 出 per-user / per-day → 日预算上限，超限**降级**（换小模型/拒答）而非仅告警。
- **验收**：`/cost` 输出与上游账单同量级；预算命中时降级有日志与用户可见提示。
- **备注**：M7 的成本预算部分已完成；本条剩余项（`/cost` 命令、per-user 拆分、降级策略）转入 P2。

**A2（P0）历史裁剪 + 滚动摘要（启用 `sessions.summary`）**　*工作量：中*
- **现状**：`sessions.summary` 字段在 DDL（`memory/store.py:21`）里，**从未被写入**；`get_history(session_id, limit=20)` 无 token 预算。
- **后果**：长会话 + 图片**静默**顶爆上下文 —— 不报错，只是又贵又笨。
- **做**：按 token 预算裁剪历史；超窗口部分滚动压缩写入 `sessions.summary`；摘要参与下一轮 system prompt。
- **验收**：构造超长会话，模型仍能引用早期关键信息；单 turn 输入 token 有确定上界。

### B 组：用户能管自己的数据 —— 隐私底线

**B1（P1）`/memory list | forget <id> | clear`**　*工作量：小*
- **现状**：命令面仅 `reset / aihelp / status / skills / skillcatalog / skillinstall / skilluninstall / skillinfo / kb / persona`，**无任何记忆管理入口**。
- **后果**：用户看不到也删不掉关于自己的长期事实；而 facts 只增不减、无语义去重、无衰减。
- **做**：基于已有 `list_facts` / 删除接口暴露命令，输出只含调用者自己 session 的事实。
- **验收**：`forget` 后该事实不再进入任何 prompt；跨群不越权。

**B2（P2）`/export` 导出当前会话为 md**　*工作量：极小* — 复用 `send_markdown_file`，与归档天然配套。

### C 组：稳定性 —— QQ 场景必然踩到

| 编号 | 项 | 现状证据 | 做 |
|---|---|---|---|
| C1（P1） | **LLM 重试退避** | `client.py` 无 `retry`/`backoff`，失败只切一次 fallback 就抛 | 指数退避 + 错误分类（限流 / 鉴权 / 超时） |
| C2（P1） | **turn 级超时与取消** | 只有单次 LLM 60s 超时 | 一个 turn 的总预算；超时后释放 debounce 队列并回一句降级文案 |
| C3（P1） | **tool_calls 并行** | `loop/engine.py:329` 仍是 `for tc in ...` 串行 | 无副作用的工具 `asyncio.gather`，有写操作仍串行 |
| C4（P2） | **并发上限** | `Debouncer` 只保证同会话串行，无全局上限 | 全局 Semaphore + per-user 速率限制（与 A1 配套） |

---

## 2. 工程安全网（必要，但不是"功能"）

| 编号 | 项 | 为什么 | 做 |
|---|---|---|---|
| **2.1（P0）** | **PG 侧测试进 CI** ⭐ | 实测 32 个跳过**全部**是 `TEST_DATABASE_URL` 门控，CI 无 PG service → store / restore / replay 这些最危险的代码**零回归保护** | CI 加 `postgres:16`+pgvector service 并导出 `TEST_DATABASE_URL`（conftest 已有防误连生产库断言）；顺带加 3.11/3.12 矩阵 |
| **2.2（P0）** | **恢复演练脚本** ⭐ | 备份 / 镜像 / restore CLI 都有了，但**从未真恢复过一次**——没演练过的备份等于信仰 | `scripts/drill_restore.py`：定期把最新备份灌进 scratch 库，校验行数/向量一致性并输出报告 |
| 2.3（P1） | **Dockerfile** | README 自己写「根治方案是容器/独立低权用户运行」，但 compose 里只有 `db`，安全声明未兑现 | 单容器 + 非 root + `--cap-drop ALL` + 出口白名单；**不要挂 `docker.sock`**（改用镜像内 `postgresql-client` 直连 `db`） |
| 2.4（P2） | `/healthz` + 轻量 `/metrics` | fastapi driver 自带 HTTP 服务，成本很低 | turn 数、LLM 耗时、错误率、缓存命中 |
| 2.5（P2） | `config.yaml` pydantic 校验 + 结构化日志（turn_id 串联） | 字段写错只静默用默认值；一次 turn 的步骤散在各处 INFO | 启动 fail-fast；turn_id 串起 LLM/skill 调用 |

---

## 3. 里程碑状态

| 里程碑 | 状态 |
|---|---|
| M0–M4 基础链路 / 记忆 / 人格 / 工具 | ✅ 完成 |
| **M5 RAG** | ✅ 完成（含注入防护与脱敏） |
| **M6 多 Agent** | ⬜ **空壳**（`agentcore/multiagent/__init__.py`）—— 见 §5，**建议缓做** |
| **M7 调度 / 预算 / 归档** | 🟢 调度、**成本预算（`be13897`）**、日志归档均已完成；M7 仅剩「定时内容推送」 |

---

## 4. 建议迭代顺序

- **迭代 1（地基，1–2 天）**：A1 token 用量 → `/cost` → 预算熔断 → C4 限流
- **迭代 2（质量与隐私，1–2 天）**：A2 历史裁剪 + 滚动摘要 → B1 `/memory` → B2 `/export`
- **迭代 3（安全网，半天）**：2.1 PG 进 CI → 2.2 恢复演练
- **迭代 4（体验）**：C1 重试退避 → C3 工具并行 → C2 turn 超时 → 群聊免前缀窗口
- **之后**：2.3 Dockerfile → 落地后再评估 M6

> 一句话：功能面（记忆/人格/技能/RAG/沙箱/备份）已超出 demo 水准，
> 现在缺的是「能不能放心让它多说话」——看得见花钱、扛得住长会话、
> 用户能删掉自己的记忆、以及一份**真演练过**的备份。

---

## 5. 明确暂不做（含理由）

| 项 | 理由 |
|---|---|
| **M6 多 Agent** | 现有 30+ 工具 + 单 loop 已够跑通场景；多 Agent 会先抬高三样**当前正好看不见**的东西：延迟、成本、调试复杂度。建议先把 §1 A1 的账和 §1 C 组的稳定做掉 |
| MCP 客户端 | 能力扩张项，前提同上 |
| 语音 ASR/TTS、文生图 | 感知强但纯增量，且引入新的媒体下载/存储面（`media.py` 那套 SSRF 防护要再来一遍） |
| 敏感内容过滤 | 面向陌生公开群时才是必要项 |
| 流式输出 | QQ 分条发送体验提升有限，且与 debounce/分段逻辑耦合；可选 |
| 群聊免前缀窗口 | 纯体验项，改动局部，可随时穿插进任意一次提交 |

---

## 6. 已实证缺陷（必修，非新功能）

来源：[`review/REVIEW-8cfbf6d..a604023.md`](review/REVIEW-8cfbf6d..a604023.md)（5 线并行子代理 + 主代理逐条实证）。
这些是**已复现**的问题，优先于 §1 的新功能；修复阶段另出 `review/FIX-8cfbf6d..a604023.md` 记录改法与回归。
其中最后两条由 `tests/test_perf.py`（`RUN_PERF=1`）在 2026-09-11 实测确认并已修复，属**已量化**但真实场景影响小
（`_qq_plain` 输入为单条回复，通常 <8KB → 毫秒级；`merge_parts` 的 part 数受防抖窗口约束，通常个位数）。

| 级别 | 问题 | 位置 |
|---|---|---|
| **H** | 公共库数字脱敏的分隔符类不完整：22 种写法中 **18 种穿透**（`/ _ , ， 、 \| \ + ( ) ‧` 及零宽字符） | `rag/sanitize.py` |
| M | 沙箱 `curl` 的数字型 IP 字面量绕过内网判定（`2130706433` / `0177.0.0.1` / `0x7f000001` 实测真连上 127.0.0.1） | `workspace/runner.py` |
| M | 蒸馏两条静默路径：单条消息截断后水位线仍推进（尾部永久丢失、无日志）；极短消息占满 batch 窗口导致**永久停更** | `rag/distill.py` |
| M | 检索结果（`search_web` / `search_multi`）未套不可信围栏 | `skills/search.py` |
| M | 完整性链路断在恢复端：restore 从不校验 sidecar；镜像只复制主文件、不复制 `.sha256` | `backup/db_backup.py` |
| M | 双实现契约分歧：内存 `kb_add_chunks` 无去重（PG 有）、`list_facts` 排序不一致、私聊判定靠解析字符串 | `memory/store.py` |
| M | 人名/昵称防护在默认配置下几乎不生效（词表为空 + 句式覆盖窄） | `rag/sanitize.py` / `rag/distill.py` |
| L | 13 项（围栏标记未转义、沙箱 HOME 临时目录不回收、cron 失败重试丢周期语义、归档 id 截断等） | 见报告 §1 L 级表 |
| L | ✅ **已修**（2026-09-11）`_qq_plain` 超线性：原逐代码块 `str.replace` 全串还原为 O(n²)，实测 75k 63ms → 300k 1.28s → 600k 5.00s；改为单次 `re.sub` 回调后 600k **56ms**（线性，~89×） | `plugins/qq_agent_adapter/matcher.py` |
| L | ✅ **已修**（2026-09-11）`merge_parts` 图片去重 O(n²)：原 `img not in images` 列表扫描 + 返回时才裁剪；改为 `set` 判定并命中上限即停，800 part/16000 图 2005ms → **0.1ms** | `plugins/qq_agent_adapter/pipeline.py` |

> 另有 2 项被主代理**证伪**（子代理报的 `save_fact` 丢作用域条件、私人物件启发误杀 9/12），
> 不要按它们改代码 —— 详见报告 §2。

### 6.1 规范审查新增（来源：[`review/REVIEW-a604023..679c9b3.md`](review/REVIEW-a604023..679c9b3.md)，修复见 [`review/FIX-a604023..679c9b3.md`](review/FIX-a604023..679c9b3.md)）

4 条并行子代理取证 + 主代理逐条复核（5 条 H 全部亲自复现）。**H 级已全部修复**：

| 级别 | 问题 | 位置 | 状态 |
|---|---|---|---|
| **H** | 沙箱 `zip` 参数零校验 → `-T -TT`/`--unzip-command` 任意命令执行 | `workspace/runner.py` | ✅ 已修（白名单开关 + 禁 `=` 参数） |
| **H** | 沙箱 `curl` 只校验含 `://` 的参数 → 裸内网地址绕过判定实读 loopback | `workspace/runner.py` | ✅ 已修（所有非选项参数当 URL 校验） |
| **H** | `calc` 的 `pow()` 绕过静态守卫 → 阻塞事件循环 7.19s / 473MB（public 权限） | `skills/basic_tools.py` | ✅ 已修（`pow` 调用纳入守卫） |
| **H** | `httpx` 超时被判为"确定未送达" → NapCat 降级重发，用户收到两遍 | `skills/file_sender.py` | ✅ 已修（补 `httpx.TimeoutException`） |
| **H** | 停机钩子逆序 → flush 在连接池关闭后跑，重启必丢一批消息（降级 `[echo]`） | `lifecycle.py` + `bot.py` + 插件钩子 | ✅ 已修（顺序固定 + 幂等） |

**M 级已修 19 条**（注入面 3、存储契约 2、备份/归档/蒸馏 5、单位换算 2、大小写归一 1、并发/资源 6；见 FIX 文档第二、三批）。存储作用域 2 条已修。

**第四批（已完成，见 FIX 文档 §"第四批"）**：

| 项 | 结果 |
|---|---|
| `_backup_jsonl` 整表 `fetch` 进内存 | ✅ 改为 `conn.cursor(...)` 游标流式 + 每 500 行 `to_thread` 序列化；失败清理 `.part`；回归见 `tests/test_backup_jsonl.py`（7 条，含真库 1200 行用例） |
| **共享契约测试缺失**（内存 vs PG 各测各的） | ✅ 新增 `tests/test_store_contract.py`：同一批断言参数化跑两个实现（20 条）。**顺带查出并修掉 4 处此前未发现的漂移**：`list_facts`/`recall_facts` 负 limit 在 PG 直接抛 `LIMIT must not be negative`、`kb_search`/`kb_list_sources`/`messages_after`/`schedule_due` 非正 limit 两实现语义不同（内存"去掉最后 N 条" vs PG 抛错）、`kb_last_digest_watermark` 内存取 max 而 PG 取最新来源 |
| 假通过用例余项 | ✅ 全部加固并逐条变异复核（见下） |
| 文档/工程余项 | ✅ 本文件数字刷新 + 新增根 `AGENTS.md` 交接文档 |

**测试加固明细（第四批，均已用"改坏实现→用例必须失败"复核）**：
- `test_outbound.py`：恒真断言 `"超过软上限" not in caplog.text`（该字符串全仓不存在）→ 改为"该路径不得产生任何 WARNING"；`_repack_chunks` 真实覆盖（新增 `test_repack_enables_forward_instead_of_spamming`：禁用重打包必然落到逐条发送而失败）；nickname 参数传递（显式值 / env 兜底两条，忽略参数必然失败）
- `test_pipeline.py`：空消息与纯图片消息由 `assert text.strip()` 改为断言**具体文案**；补 `_display_key`（base64/内联数据掩码、文件名清洗截断）与 `_download_for_su`（非管理员只回显掩码出处、管理员落盘相对路径、下载失败备注）——两组此前**零引用**
- `test_file_sender.py`：`_napcat_upload_private_file` 此前**从未被执行**，补 5 条（base64 载荷字段/鉴权头/无 token/异常返回原文/公开入口优先走 NapCat 而不回落 OneBot）

**L 级 19 条**与**测试补强**（`acl` 私聊拒绝零覆盖、假通过用例、PG 契约测试未接入 CI、`ruff format` 未门禁、`CONTRIBUTING` 版本号、`.env.example` 缺 `AGENT_SKILLS_DIR`）见报告 §3–§4。

### 6.2 第四批复核中新发现的小项（非必修，记录以免丢失）

| 项 | 说明 |
|---|---|
| 带图占位分支不可达 | `pipeline.py` 的 `"（请结合用户发来的图片回答）"` 只在 `extra_images` 非空且 `text` 为空时命中，而每张进入 `extra_images` 的图片都必然先写一条 notes → 该占位实际不可达（保留作防御）。无图占位 `"（用户没有输入文字内容）"` 可达并已被断言锁定 |
| `_reconstruct_content_from_memory` 是死桩 | `skills/file_sender.py` 该函数恒返回 `""`（读 `driver._agent_memory` 后直接 `return ""`），当前无人调用 |
| `LLMClient.embeddings` 无调用方 | `agentcore/llm/client.py` 的 embeddings 方法无生产调用点（embedding 走 `agentcore/embedding/client.py`） |
