# agent-demo 近期 Commit 评审报告
**评审范围**：`fad144b..bbd8913`（7 个 commit）**+ 工作区未提交的 M7 改动**（budget.py/engine/bot 等 11 个文件，单独标注）
**评审日期**：2026-09-11
**评审方式**：4 个并行只读子代理分线审查（唤醒词流水线 / 知识库命令与后台导入 / embedding 批量与成本预算 / 文档一致性与声称核对）+ 主代理逐条实证（实跑 pytest/ruff、复现、证伪、声称核对）
**工作区状态**：**不干净**——M7（成本预算 + 日志归档）未提交，已一并纳入审查并单独标注；`data/kb_samples/` 现有 36 个本地语料文件（不入库）

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | 无 H。已提交部分最高为 **M1 bbd8913 声称失真**（commit message 说删了 gitignore 例外，实际没删——评审者自己犯的，被评审抓出来）与 **M2 批量导入脚本非幂等**（失败重跑会全量重复入库）；未提交部分最高为 **M4 预算账本跨月竞态** |
| 唤醒词（073d077） | 触发语义真值表 12 配置×16 文本零失配；剥前缀主体正确；**@机器人路径带前导空格时唤醒词残留**（L，修复一句话） |
| /kb samples（d7ff110） | 并发正确性扎实（锁快路径原子性 200/200 仿真、无双跑、无锁泄漏、cancel 安全）；**完成通知承诺不可靠**（DM 失败静默、失败清单只走 DM） |
| 批量导入脚本 | **非幂等无判重** + **docstring「超限跳过」与行为不符**——与 `/kb samples` 的判重能力不对齐 |
| 成本预算（未提交） | 分账/闸门/持久化主体正确；**跨月午夜竞态污染新月账本**（已复现）；**一个测试断言恒真** |
| embedding 分批（c54b514） | 切片/顺序/probe_dim 正确；分批后「上游 200 但条数不足」会静默错位全部后续向量（推演，建议加校验） |
| 测试与文档 | **633 passed / 32 skipped**；ruff 全绿；**依赖安全（§4 新条款）：范围内 pyproject 零变化**；README 重排内容零丢失、TOC/交叉引用全对 |
| 质量门禁（§8.3） | 未验证面：本地 embedding 服务真实链路、DashScope 错误体实际内容、真机多账号通知送达、systemd 部署 CWD——均不影响已提交功能，M7 提交前无需补验 |

---

## 1. 已实证的问题

### M1（已提交 bbd8913）commit message 声称删除 gitignore 例外，实际未删
- **位置**：`bbd8913`（「chore: drop hello.md sample and stale gitignore exception」）；`.gitignore:80,82`
- **证据**（主代理【已复现】）：`git show bbd8913 --stat` 仅含 hello.md 删除；HEAD 的 .gitignore 仍保留 `!data/kb_samples/hello.md`（:82）与「hello.md 为最小演示样例，保留」注释（:80）。hello.md 已删，两者均为死配置。
- **影响**：按 §3「声称与实现不符」记 M——且与上一轮 M3（commit message 失真）同类，说明声称管理需要固化成习惯。
- **修复建议**：补 chore commit 真正删除例外行并改写注释；message 引用本条编号。

### M2（已提交 d7ff110）批量导入脚本非幂等、无判重：「失败→重跑」会全量重复入库
- **位置**：`scripts/ingest_kb_samples.py:60-70`
- **证据**（线2【推演】+ 主代理核对）：脚本直接 `kb.add_file`，无 `list_sources` 判重；`failed>0` 时 `sys.exit(1)`。当前目录 36 个文件全部 <2MB 都会被导入，任何一次失败后按退出码提示重跑，已成功的文件会**再次整篇入库**（重复 embedding 花费 + 检索同文双命中）。`/kb samples` 有判重，脚本没有——两条路径能力不对齐。
- **修复建议**：入库前与 `_plan_samples` 同款按名判重；或 docstring 明示「重跑会重复入库」。

### M3（已提交 d7ff110）脚本 docstring「超限跳过」与实际行为不符
- **位置**：`scripts/ingest_kb_samples.py:6-8` 对照 `agentcore/rag/ingest.py:77-78`
- **证据**（主代理复核【已复现】）：docstring 写「单文件上限 2MB（超限跳过）」，实际 >2MB 由 `ingest_file` 抛 ValueError → 计入 `failed` → **exit 1**。本 commit 的卖点就是「docstring 如实」，结果措辞仍失实。
- **修复建议**：循环前 stat 预检真跳过（对齐 `/kb samples`），或改措辞为「超限视为失败」。

### M4（未提交 M7）预算账本跨月午夜竞态：污染新月文件、旧月缺最后一笔
- **位置**：`agentcore/budget.py:120-137`（`record()` 内 `_day()` 与 `_save()` 各自独立取 `date.today()`）
- **证据**（线3【已复现】仿真）：`_day()` 取到 9-30、`_save()` 取到 10-01 时，9 月最后一笔记进 `usage-2026-10.json`，且 `_loaded_month` 仍是 9 月，后续 10 月记录会把 9 月残留读回并永久留在 10 月文件里。
- **影响**：账本统计失真；不影响闸门判定（仅统计用途），概率为每月一次的午夜窗口。
- **修复建议**：`record()` 开头取一次 `now = date.today()`，month 贯穿 `_day(now)`/`_save(month)`。**M7 提交前修**。

### M5（未提交 M7）`test_no_budget_never_blocks_without_reading_disk` 断言恒真
- **位置**：`tests/test_budget.py:38-41`
- **证据**（线3【已复现】路径演算）：`root=tmp_path` 时违规写盘的落点是 `tmp_path/usage-*.json`，而断言查的 `tmp_path/data` 永远不存在——即使实现写了盘测试也通过，没有验证「零磁盘 IO」。
- **修复建议**：改为 `assert not list(tmp_path.glob("usage-*.json"))`。

### L 级（压缩列出）
- **L1**（已提交）@机器人路径唤醒词残留：at 段移除后 text 段带前导空格，`startswith` 失配，唤醒词留在 prompt（主代理【已复现】：`" 小助手 帮我查天气"` 未剥）；修复=先 `strip()` 再剥。触发语义上该路径触发源是 @ 而非唤醒词，不算违约，属体验不一致。
- **L2**（已提交）`_start_samples_job` 里 `size_mb` 二次 `stat()`：文件被删时 OSError 上抛 → 回复「操作出错」，但后台任务实际已在跑（线2【已复现】）。
- **L3**（已提交）判重 `list_sources(limit=1000)` 天花板：来源超 1000 后窗口外旧同名判重失效（长尾）。
- **L4**（已提交）完成通知承诺不可靠：DM 失败（bot 掉线/非好友）静默吞掉，**失败清单只走 DM**；锁释放与 DM 发出之间查进度显示「没有任务」。建议 DM 失败置状态位 + 话术改为「以 /kb samples 查询为准」。
- **L5**（已提交）失败 DM 携带上游 embedding 错误体 80 字符片段——仅管理员私聊可见，可接受；建议聊天侧只回状态码+批次区间，响应体留日志。
- **L6**（已提交）`LLMClient.embeddings` 全仓无调用方（死代码），且未分批——未来接线会复现 c54b514 修的 400；建议删除或委托 EmbeddingClient。
- **L7**（已提交）文档口径过时：README 目录树仍列 `data/kb_samples/ # 示例知识库语料`（fresh clone 无此目录，git 不跟踪空目录）；FIX 文档「616 passed」早于其自身记录的 embedding 回归（现 633）。
- **L8**（未提交）bot.py 日志：`AGENT_LOG_KEEP_DAYS` 脏值 `int()` 直接崩启动；`AGENT_LOG_DIR` 相对路径依赖 CWD（systemd 需配 WorkingDirectory）；TimedRotatingFileHandler 实际留存 ≈ N+1 天、不支持多进程同写。建议脏值 warning+回退，README 部署段注明。
- **L9**（未提交）`_remote_embed` 拼批前不校验 `len(ordered)==len(batch)`：上游 200 但条数不足时该批起所有向量整体错位、静默检索降质。建议不等即抛（与 400 同款处理）。
- **L10**（未提交）预算 `record()` 在事件循环内同步写盘（工具循环每步一写）；`.part` 固定名在多进程部署下互踩。建议 `to_thread` + pid 后缀，或文档声明单进程假设。
- **L11**（未提交）部署后**首次**蒸馏若逢预算拦截日，恢复日重跑时水位线初始化为当时的 latest_message_id，被拦期间的消息按「存量不回灌」永久跳过。边缘场景，README 注明即可。
- **L12**（未提交）`test_budget.py` 未隔离 `AGENT_BUDGET_*`/`AGENT_PRICE_*` 环境变量（脏环境假失败）；`AGENT_BUDGET_DIR` 未写进 `.env.example`。
- **L13**（未提交）embedding 用量分账落盘了，但 `/status` 与成本估算都不展示——「分账」无法核对。

---

## 2. 被证伪的发现

| 怀疑 | 结论 | 原因 |
|---|---|---|
| 两条消息同时到 → `/kb samples` 双跑 | **被证伪** | 线2 仿真 200/200：Python 3.12 `asyncio.Lock.acquire` 快路径无 await，二次检查+获取原子，恰好一个启动 |
| 首轮完成 DM 被第二轮 `state.update` 覆盖 | **被证伪** | summary 在 release 同一事件循环步内求值，无插入窗口 |
| 唤醒词委托后触发语义漂移 | **被证伪** | 线1：12 配置 × 16 文本真值表 0 失配【已复现】 |
| `max(key=len)` 并列/Unicode 大小写长度变化导致错剥 | **被证伪** | 等长并列必同词；`İ`/`K` 用例切片正确 |
| README 重排丢失内容/TOC 锚点失效 | **被证伪** | 线4 逐块比对：除 4 处有意修改外逐字保留；9 个锚点全部有效 |
| workflow 修订后 §8/§9 编号残留错引 | **被证伪** | 线4 全文 grep：交叉引用全部指向正确 |
| 脚本（kind=file）与 /kb samples（kind=sample）双重入库 | **被证伪** | 判重按 name 不按 kind |
| `daily_tokens=0` 时每次调用仍写盘属 bug | **被证伪** | 「只记录不限流」是文档化的设计行为（代价见 L10） |
| 时区/跨日（23:59 记录 00:00 落盘）写错日 | **被证伪** | 月串相同落回同月文件；仅月界触发 M4 |

**存疑**：上游「200 但条数不足」是否实际发生（M9 按推演定级）；plan 与 job 之间被 `/kb file` 抢注同名的 TOCTOU 窗口（需管理员并发，未复现）；损坏账本文件的非 dict 结构（被钩子吞掉、/status 静默少一行）。

---

## 3. 测试与文档状况

### 实证数据
- **全量测试**：633 passed / 32 skipped（上轮报告时点 616 → +6 唤醒词回归 +4 embedding 批量 +7 预算）
- **lint**：ruff 全绿（含 scripts）
- **依赖安全（§4 新条款，本轮首次执行）**：`git diff fad144b..HEAD -- pyproject.toml` 为空，零新增/升级依赖
- **CI 盲区**：无 PG service、仅 py3.12；32 skipped 为 PG 门控用例（既有）

### 未验证面（§8.3 门禁声明）
本地 embedding 服务真实链路（Ollama/TEI 的 usage 字段与分批行为）、DashScope 错误体实际内容、真机多账号下完成通知的送达率、systemd 部署 CWD——均不阻断 M7 提交，但 `AGENT_LOG_DIR`/`AGENT_BUDGET_DIR` 建议部署时用绝对路径。

### 文档一致性
- README/`/kb samples`/EMBEDDING_BATCH/outbound 三条陈述与 HEAD 代码逐项吻合（线4 抽查）
- 过时项：README 目录树 kb_samples 描述（L7）、FIX 测试数字口径（L7）、.gitignore 例外（M1）

---

## 4. 已验证为「无问题」的关键项

| 要点 | 验证方式 |
|---|---|
| 唤醒词触发语义等价（委托重构零漂移） | 线1 真值表仿真【已复现】 |
| 群剥/私不剥的 group 判定（getattr 与 isinstance 在可达路径不分歧） | 线1 代码核查 + 推演 |
| /kb samples 并发：无双跑、无锁泄漏、cancel 安全、summary 不被覆盖 | 线2 仿真 A/B/C/D【已复现】 |
| ingest to_thread：scrub/chunk 线程安全、异常传播等价 | 线2 代码核查 |
| embedding 分批切片/顺序/probe_dim/批次下限 | 线3 读码 + 已提交测试核对 |
| usage 上报无双计（fallback 只记一次、失败不记） | 线3 控制流核对 |
| `chat_blocked` 默认配置零磁盘 IO（早退路径） | 线3 读码 |
| 蒸馏闸门：水位线不动、cron 与手动同闸、非首跑断点续蒸 | 线3 读码 + 既有测试 |
| 依赖安全：零新增/升级依赖 | 线4 git diff【已复现】 |
| 上轮 P1 修复的文档闭环（README/.env.example 三条 outbound + 唤醒词） | 线4 逐句对照 |

---

## 5. 与在库报告的衔接复核

上轮 `REVIEW-436629d..fad144b.md` §6 P1 项兑现情况：

| 上轮问题 | 状态 | 证据 |
|---|---|---|
| P1-1 M1 唤醒词不剥前缀 | **已修复**（073d077） | 线1 确认；残余：@路径前导空格（本轮 L1） |
| P1-2 M5/M6/L7 脚本+gitignore | **已修复但引入新问题** | docstring/退出码/glob 已改；新发现 M2（无判重）、M3（措辞仍失实）；gitignore 收编 ✓ 但例外未随 hello.md 删除清理（本轮 M1） |
| P1-3 M2/M3 文档闭环 | **已修复** | 线4 逐句核对 README/.env.example ✓ |
| M4 掩蔽性测试（P2） | **未修复** | test_matcher.py 两处 setenv 仍无效（线1 确认遗留） |
| 上上轮 H1 `split_message` 边界 strip / H2 死代码 | **仍未修复** | 本轮未触及，继续遗留 |
| 本轮新增待修 | M1（声称）/M2/M3（脚本）/M4-M5（未提交 M7） | 见 §6 |

---

## 6. 修复优先级建议

1. **P1**：兑现 bbd8913 声称——删 `.gitignore` 例外行+改注释（M1）
2. **P1**：脚本对齐 `/kb samples`——同名判重 + 「超限跳过」真跳过或改措辞（M2/M3）
3. **P1**：M7 提交前修预算跨月竞态（M4）与恒真断言（M5）
4. **P1**：完成通知可靠性——DM 失败置状态位供进度查询提示，话术改为「以 /kb samples 查询为准」（线2 M1）
5. **P2**：@路径先 `strip()` 再剥唤醒词（L1）；`_remote_embed` 拼批前校验条数（L9）；`/status` 补 embedding 用量（L13）；size_mb 降级（L2）
6. **P3**：判重分页（L3）、死代码 `LLMClient.embeddings`（L6）、日志脏值回退（L8）、预算写盘 to_thread（L10）、测试环境隔离（L12）
7. **重申（多轮遗留）**：`split_message` 边界 `.strip()`（上上轮 H1）、`_reconstruct_content_from_memory` 死代码（上轮 H2）、AGENT_PREFIX 掩蔽测试（上轮 M4）
