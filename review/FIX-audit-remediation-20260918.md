# agent-demo 全量代码审查修复记录

**对应工作**：会话内全量代码审查（P0×0 / P1×10 / P2×47+）后的逐条修复，P1 优先
**修复日期**：2026-09-18
**验证结果**：`pytest -q` = **1603 passed / 46 skipped**（基线 1558 通过，本轮净增 45 条回归用例，
删除 3 条随死代码一并移除的用例）；因动过 `agentcore/memory/archive.py`（归档，死代码删除），
另按纪律带 `TEST_DATABASE_URL` 跑全套：**1640 passed / 9 skipped**（9 个为 RUN_PERF 门控）；
`ruff check` + `ruff format --check`（0.9.6，与 pyproject pin 一致）全绿。
本轮未动 DDL（归档为文件产物，无 schema 变更），不触发全新空库验证条件。

> 纪律声明：每项修复均带"改坏实现必须失败"的回归测试或直接断言修复路径；
> 未做自动 commit/push；工作树中 AGENTS.md / BACKLOG.md / README.md / config.yaml /
> .env.example / memory/store.py / scheduler/reminder.py / skills/reminder_skills.py /
> help_render.py 等文件的**部分改动来自先前会话**，与本轮修复文件存在重叠时已在下文注明。

---

## P1 批（A1–A10，全部修复）

### A1 沙箱 curl 数字 IP / 内部主机名 SSRF（含 runner unzip 加固）

| 项 | 内容 |
|---|---|
| 修复 | `agentcore/safety.py`：`ip_literal_is_safe` 归一化 inet_aton 数字字面量（`2130706433`/`0x7f000001`/`127.1`）后按同一禁段判定；新增内部主机名 fail-closed（`localhost`/`*.localhost`/`*.local`/`*.internal`/`ip6-localhost` 等）。`runner.py`：curl 元字符检查只对选项参数（非选项操作数全部按 URL 校验）；unzip 分支加旗标白名单、`-d` 目标目录必须在根内、zip 条目拒绝 `..`/绝对路径/盘符/`.git*`（生成前置 `asyncio.to_thread` 校验）、`strip_symlinks` 加 5 万条扫描上限、rmtree 拒绝根目录 |
| 测试 | `tests/test_runner_security.py` 新增 `TestCurlNumericAndInternalHost`、`TestUnzipHardening`（95 通过） |
| 备注 | BACKLOG §6 此项标记为已修复 |

### A2 unzip 符号链接投毒

并入 A1（条目防护 + 符号链接扫描上限 + 端到端 `.git`/dotdot/符号链接丢弃用例）。

### A3 growth 接线 None 崩溃

| 项 | 内容 |
|---|---|
| 修复 | `plugins/qq_agent_adapter/__init__.py`：新 `_wire_growth_notify(growth, notify)`，`growth is None` 安全空转；调用点全部走它 |
| 测试 | `tests/test_admin_import.py::TestGrowthNotifyWiring`（47 通过） |

### A4 脏环境/脏配置导致启动崩溃

| 项 | 内容 |
|---|---|
| 修复 | 三处裸 `int()` 全部改为告警回退：`agentcore/rag/service.py` 新 `_resolve_int`/`_resolve_float`（nan/inf 拒绝、threshold 限 [0,1]、min_chars 允许 0）路由 7 个 KB 配置项；`agentcore/embedding/client.py` 的 `EMBEDDING_DIM`/`EMBEDDING_BATCH` 改走既有 `_env_clamped_int`（空串 env 是 .env 常见形态）；插件侧 `AGENT_ARCHIVE_KEEP_DAYS`/`AGENT_BACKUP_KEEP` 走新 `_int_or`（env > yaml > 默认） |
| 测试 | `tests/test_embedding.py::TestEnvLoaderRobustness`、`tests/test_rag.py::TestKnowledgeBaseConfigRobustness`、`tests/test_admin_import.py::TestIntOrHelper` |

### A5 catalog search_web 空桩覆盖真工具

| 项 | 内容 |
|---|---|
| 修复 | 三层收口：`catalog.py` 移除 search_web 条目（目录只收 prompt 型）；`registry.install` 对 tool 型无 handler **拒装**（ValueError）；`/skill install` 对 tool 型直接回复「内置工具，无需安装」。启动期对历史落盘坏 manifest 逐条 try/except 跳过并告警，不再反复炸启动 |
| 测试 | `tests/test_registry.py::TestInstallToolGuard`、`TestCatalogHygiene`；`tests/test_admin_import.py::TestInstallToolTypeGuard` |

### A6 事实抽取 embed_many 未走交互短预算

| 项 | 内容 |
|---|---|
| 修复 | `engine.py` 事实写入路径改 `embed_many(new_facts, interactive=True)`（批量退避 5×60s 会把一次普通发言挂 ~15 分钟）；同步 4 个测试假对象签名（否则 TypeError 被外层 except 静默吞掉） |
| 测试 | `tests/test_engine.py::TestFactsEmbedInteractiveBudget`（直接断言 interactive=True kwarg） |

### A7 file 段图片绕过最近图片复用门控

| 项 | 内容 |
|---|---|
| 修复 | `pipeline.py` 门控改 `had_image_segments or bool(direct_media)`（图片可以以 file 段发送，旧门控让 file 段图片消息落入复用分支：本条新图被旧缓存覆盖 + notes 整体二次拼接）；复用分支只追加复用提示一条 note |
| 测试 | `tests/test_pipeline.py::TestFileSegmentImageNoStaleReuse`（含提取失败清缓存用例；桩按 `fetch_image_bytes` 失败返回 None 的真实契约编写） |

### A8 图片链路漏 CGNAT 段

| 项 | 内容 |
|---|---|
| 修复 | `media.py::_is_forbidden_ip` 改走 `agentcore.safety.ip_in_forbidden_range`（判定函数提为公开 API，带"唯一禁段判定"文档） |
| 测试 | `tests/test_media.py` 补 `100.64.0.1`/`100.127.255.255` 用例 |

### A9 push 预算闸门误停用 prompt-only 任务

| 项 | 内容 |
|---|---|
| 修复 | `push.py::_content` 返回 `(内容, 是否临时不可用)`；预算闸门 + 无模板 → 重新调度返回 `capped`，真·无内容配置才停用。临时态不再触发停用告警 |
| 测试 | `tests/test_scheduler.py::TestBudgetGatePromptOnlyReschedules`（断言任务保留、next_run 在未来、零告警；真·空配置仍停用） |

### A10 push config.yaml 标量被静默丢弃

| 项 | 内容 |
|---|---|
| 修复 | `load_push_config` 新 `_scalar`/`_flag`（env > yaml > 内置默认，脏值告警回退）路由 enabled/tick/retry_delay/max_failures/max_retry_delay/daily_cap_per_target/max_chars 全部 7 个标量 |
| 测试 | `tests/test_scheduler.py::TestPushConfigYamlScalars`（yaml 生效、env 优先、脏值回退三用例） |

---

## P2 批（B/E/F/G，本轮已做部分）

### B1 llm/client 主模型瞬时故障重试（评审 C1）

`_PRIMARY_ATTEMPTS=2` + 退避 0.5s；`_is_retryable` 分类（TransportError / 408/429/5xx 可重试；
401/400 确定性错误直接降级）。测试 `tests/test_llm.py::TestPrimaryRetry`（4 用例）；
模块级 fixture 把退避归零（套件从 11.7s 降回 0.9s）。

### B2 只读工具并行执行（评审 C3）

`SkillRegistry` 增加 `read_only` 标记 / `mark_read_only` / `is_read_only`（默认 False，fail-closed）；
白名单集中在 `skills/builtin.py` 注册尾部（calc/fetch_url/summarize_url/now/date_calc/unit_convert/
random/search_web/search_multi/system_status/reminder_list/fs_list/fs_read + ops 五件），**唯一审计点**；
engine 在「一步内全部工具调用均为只读」时 `asyncio.gather` 并行、结果仍按声明顺序回填。
测试：`tests/test_engine.py::TestReadOnlyToolParallel`（3 用例，含顺序保持与副作用强制串行）、
`tests/test_registry.py::TestReadOnlyFlag`。

### B3 单回合超时（评审 C2）

`matcher.py` 新 `AGENT_TURN_TIMEOUT`（默认 180s，0=不限，脏值告警回退），`asyncio.wait_for` 包裹
`engine.run`——挂死调用不再永久占用并发闸门。取消只作用于引擎执行，出站投递在其后，无双发风险。
测试 `tests/test_matcher.py::TestTurnTimeout`（3 用例）。

### B4 引擎前奏并发收集 + get_schemas 移出 tool-loop

人格/成长/知识库三个纯读 gather；技能 schema 整轮不变，提到循环外只算一次。
既有 239 个引擎面用例全绿。

### E1 出网禁段判定收敛（安全卫生）

`agentcore/music/download.py` 与 `agentcore/skills/web_fetch.py` 的私有禁段清单全部改为
复用 `agentcore.safety.ip_in_forbidden_range`（保留 web_fetch 的代理网段前置放行）。
至此沙箱 curl / web_fetch / 图片下载 / 音乐下载四个出网入口共用一份禁段清单。
测试：web/music 四套件 224 通过。

### E2 .part 临时文件清理与权限

`workspace/fs.py::_write_sync`、`media.py::save_image_atomic`、`budget.py::_save` 三处原子写
统一补：中途失败 `unlink(missing_ok=True)` 清残骸；用户内容（工作区文件/识图落盘）0600。
测试：workspace/media/budget/scheduler 套件 208 通过。

### E3 push 日键时区统一

`_day_key` 改显式 `Asia/Shanghai`（无 tzdata 回落本地），与 `_today_anchor` 同口径——
UTC 服务器上「每日上限」的日界不再比推送正文的「今天」晚 8 小时重置。

### F 死代码清理

删除（生产代码零引用，`grep` 核实）：`MessageArchive.latest_message_id`/`MessageArchive.stats`
（连带 3 条只测死方法的用例）、`rag/distill.py::DistillResult`、`music_route.py::SilkCache.clear`。
`archive.days` 有 backup 引用，**保留**。持久化套件 376 通过。

### G 文档与配置漂移

- `admin.py::_HELP_TEXT`：`/help` → `/aihelp`（补「发帮助同效」）；删「群内发 ai + 内容」
  （前缀早已移除，默认唤醒词为空），改为 @我 / 唤醒词的准确描述
- `README.md`：管理指令列表更新为真实指令集（/aihelp /reset /status /skills /kb /usage /push
  + /skill 三连）；目录树注释同步
- `config.yaml`：删除无任何代码读取的 `acl:` 段（superusers/groups 实际由 .env 的
  SUPERUSERS / ALLOWED_GROUPS 驱动），留注释说明
- `BACKLOG.md` 头部测试计数更新为实测：1649 收集（1603 通过 + 46 跳过）
- `.env.example` 的 `LLM_FALLBACK_MAX_TOKENS` 幽灵键：工作树既有修改已移除，本轮核实无残留

---

## 搁置项（如实报告）

| 项 | 理由 |
|---|---|
| `_init_agent`（~400 行）拆分 | 纯结构重构、零行为收益；函数内调度闭包/归档接线/降级路径的局部变量流多，现有测试对其覆盖浅（需完整 nonebot driver）。风险 > 收益，留作专门重构轮 |
| 提醒 cron 的服务器本地时区语义 | `next_cron_time` 按服务器时区解释 cron 是部署者可见行为，改动会平移全部存量提醒，不属于缺陷修复 |
| 「双重 manifest」条目 | 与 A5 同源（catalog 重复内置工具元数据），A5 已从根上移除目录侧 tool 型条目，判定为已解决 |

## 遗留与后续

- C 批（24 稳健性）与 D 批（9 性能）的原始条目清单在上下文压缩中丢失，已另起子代理重审；
  重审结果中仍成立的新条目在后续轮次继续处理
- 工作树含先前会话的未提交改动，与本轮改动叠加；**未 commit/push**，等待用户审阅

---

## 第二轮：审计回收与重审（2026-09-26）

上下文压缩丢失了 C/D 批原始清单。回收方式：10 个原专项审查子代理仍处 ready 状态，
向其推送「已修复清单」让其重发仍成立条目；另起 1 个全新重审子代理交叉校验（其 14 条
发现中 4 条为本轮并行修复已覆盖项，均按重审 caveat 排除）。合并去重后 ~45 条，本轮
实际修复 35 条，剩余 10 条登记在下方「剩余项」。

### 已修复（全部带回归测试或既有套件锁定）

**P1 / 安全**
1. `engine.__init__` 裸 int/float 解析 config（`"3k"`/`null` 炸启动；`max_iterations`
   非整数每回合 TypeError 全部回复退化 echo）→ `_cfg_int`/`_cfg_float` 脏值告警回退
   （与 `rag/service._resolve_int` 同款纪律）。AC：脏 config 构造不抛、全部回默认。
2. `get_weather`：city 未 quote（`?/#` 改请求语义）+ 返回文本不过围栏（违反 §4）
   → `urllib.parse.quote(city, safe="")` + `_fence_weather`（三处返回路径全过围栏）。
3. `installer.uninstall` 名字零校验（`../foo` 路径逃逸删除）→ `RE_NAME.fullmatch`，
   与 install 的 `manifest.validate()` 同一名字契约。
4. **push 日键/锚点 UTC+8 实为 no-op**（`dt.ZoneInfo` 属性不存在，AttributeError 被
   `except Exception` 吞掉静默回落本地时钟——重审 verified，也是本轮自查揪出的
   「修复中的 bug」）→ 新建 `agentcore/tz.py` 统一「今天」口径：`AGENT_SCHEDULER_TZ`
   可覆盖（默认 Asia/Shanghai，无 tzdata 回落本地**带告警**），push `_day_key`/
   `_today_anchor`、budget 四处 `date.today()`、reminder `next_cron_time` 的 cron
   语义、engine 时效锚点全部收敛。AC：TZ=UTC 覆盖下 `day_key(UTC-12-31T16:30Z)`
   断言上海日期；`next_cron_time("0 9 * * *")` 在 UTC 覆盖下 09:00 UTC 触发。
5. push `register_jobs`：配置删任务后孤儿启用行照常投递（verified：删配置重启后
   tick 仍 sent=1）→ `_disable_orphans` 清扫，**空配置路径也执行**（首版被
   `if not cfg.jobs: return` 短路，测试抓到后修正）。

**P2 稳健性**
6. pipeline 识图字节预算被 URL 兜底架空（超限标注「已跳过」却以 URL 直传模型，
   verified 复现）→ 超预算 `continue`，URL 兜底仅保留给「拉取失败」；回归断言
   `images == []` 且无 URL 直传 note。
7. Sink.send 把超时/断连折叠成 False → 提醒/推送重复投递 → 新增 `send_once` 三态
   （`sent`/`failed`/`uncertain`，`is_uncertain_send_error` 为唯一判据）：换 bot 只对
   确定性失败；uncertain 像成功一样推进调度（宁可少发一次）并单独计数。
8. `web.py` 非 ASCII token 使 `compare_digest` 抛 TypeError（500 而非 401，整面不可用）
   → 挂载期 `isascii()` 校验，非 ASCII 拒挂 + ERROR 告警。
9. `/kb samples` 锁泄漏：`_plan_samples(materialize=True)` 一抛锁永久挂死 → 同步阶段
   try/except 释放；成功路径锁仍移交后台任务（「锁被持有=进行中」语义，首版
   `finally` 释放曾破坏该语义，`test_busy_lock_reports_progress` 抓到后修正）。
10. `/kb digest` 未被 KB 关闭门拦截 → 加入写操作元组。
11. web 预算缓存「先记 at 后算 total」→ 失败污染 history 60s 且 today 陪葬 → 先算后记。
12. poke 冷却时间戳发送前写入 → `_safe_reply` 送达成功后才记（失败重试不再吞冷却）。
13. `music_route` 条件导入缺容错（对齐 poke）→ try/except + logger.exception。
14. matcher 出错路径把异常文本发进聊天（可内嵌内网主机/URL）→ 固定文案，细节留日志。
15. search `SEARCH_MAX_RESULTS` 裸 int + `search_items` 未 clamp + manifest 漏 max_total
    → `_env_max_results()`（脏值告警回退 + [1,20] 夹紧），与 search_multi 同口径。
16. `run_command` args 传字符串被 `list()` 拆字符 → `shlex.split`；非序列显式报错。
17. random dice `items` 传 `"2d6"` 静默回退 1d6（schema 描述与实现矛盾）→ 接受裸字符串。
18. ops `log_tail`/`disk_usage` 含 `\0` 路径抛 ValueError 逃到通用兜底 → 前置拒绝 +
    `except (OSError, ValueError)`。
19. `registry.install` 生成的 schema 把整段参数定义塞进 properties（非标准 JSON
    Schema，严格网关可能整请求 400）→ 剥离 name/required 仅保留 schema 字段。
20. registry 缺必填参数与真异常同文案（模型无法自纠）→ `required` 缺失返回指名错误
    （不做类型强校验，避免打破 handler 已容忍的 `"5"` 形态）。
21. workspace 权限文案「仅管理员…」不匹配 `_PERMISSION_DENIED_RE` → 引擎不记
    denied、模型空转重试 → 统一为「无权限：…」前缀（6 处）。
22. config.yaml `skills.permissions.superusers` 从未被读取（"*" 全开语义无效）→
    接线为与 `.env` SUPERUSERS 取并集（文档承诺的行为成为现实）。
23. scheduler `add_cron` 只护 CronTrigger 解析不护 `add_job` → add_job 包裹返回 False。
24. logging `AGENT_LOG_KEEP_DAYS` 空串静默关落盘、负值无告警（docstring 承诺告警）
    → 空串按未设置走默认 14，负值告警后禁用。
25. distill 首跑水位线：读失败 ≠ 真空 → `_collect_messages` 返回 `read_failed`，
    仅读取成功且确认为空才落痕。
26. PG store 四个读方法对脏 `session_id` 裸 `int()` 抛 ValueError（内存实现返回空，
    同入参两种结果）→ `_sid()` 帮手按「会话不存在」返回空 + 契约测试（双实现）。
27. PG `init()` 建池成功但 DDL 失败后 pool 非 None → 重试短路成半成品 → 失败关池置
    None 再抛。
28. `schedule_due` 先取后筛：提醒积压 >limit 把 push 行整批挤出 → 双实现加 `action`
    过滤参数 + 契约测试；reminder/push 调用方改 store 侧过滤。

**P2 性能**
29. InMemory `kb_list_sources` O(来源×块)（40k 块实测 1.0s）→ `Counter` 单遍。
30. 备份 `.part` 窗口 0644 世界可读 → 三个创建点 `touch(0o600)`；硬 kill 残留
    `{tag}-*.part` prune 清理（≥6h 才清，新鲜 part 不误删）+ 回归。
31. media 图片拉取逐次新建 AsyncClient（每条带图消息重做 TLS）→ 进程级共享池
    `_shared_http_client`（SSRF 逐跳校验不变），`aclose_shared_client` 接入停机
    extra_closers。
32. embedding client 同病 → 实例级长生命周期 `AsyncClient`（`_http_client()` 惰性
    建、`aclose()` 幂等、停机接线）。
33. `EMBEDDING_INTERACTIVE_RETRY_COUNT` 默认值下恒不生效（60s 退避 > 30s 预算）
    → 退避钳制到剩余预算（耗尽才快速失败）；旧「超预算即弃」用例按新契约改写，
    新增「预算内真的重试」用例。
34. 本地 hash 降级批量同步跑双循环占住事件循环 → `asyncio.to_thread`。
35. push 每日上限命中每分钟打 WARNING（分钟级 cron = 1440 条/天）→ INFO。

**可维护性/文档**
36. LLM 恢复通知并发误报（主路径请求落在「别人刚降级」窗口误发「已恢复」并提前
    清标志）→ 进入时快照 `_using_fallback`，恢复通知只由亲历降级窗口的请求发出。
37. admin `/skill install|uninstall|info` 参数解析 `parts[-1]`：缺参静默找名为
    "install" 的 skill、`a b` 装成 b、用法分支死代码 → `_skill_arg()` 剥命令词取
    首参，缺参走用法提示。
38. 帮助菜单缺 `/usage`、`/persona` → admin `_HELP_TEXT` 与 help_render 图片版补齐。
39. config.yaml 死段：`database:`（url_env 零读者）与 `agent.session_policy` 删除。
40. BACKLOG C 组三行更新为已落地（含证据行刷新）；工具数 24→26、契约测试 20→36、
    backup 7→12（`--collect-only` 实测）；README 补主模型 2×0.5s 瞬时重试、
    embedding 线性退避措辞、push 弱上限语义、恢复通知并发语义、「完整键以
    .env.example 为准」；`.env.example` 补进程内缓冲区需重启生效注记。
41. 脚本：`backup_db --keep` 脏 env 连 `--help` 都崩（argparse 构造期解析）→ 字符串
    默认 + `_keep_int` 回退；`ingest_kb_samples` `.env override=True` 反覆盖 shell
    显式导出 → 与 backup_db 同语义（override=False）；docstring 补 exit 2 语义；
    清理路径计数口径注释改为与实际/测试锁定一致。

### 验证（全部实测）

- 默认套件：**1630 通过 + 48 跳过**（`pytest -q`，23.8s）
- 带 PG 全套（`TEST_DATABASE_URL`，store.py 动过必跑）：**1669 通过 + 9 跳过**
  （9 跳过 = RUN_PERF 门控）
- `ruff check` + `ruff format --check`（0.9.6 pin）：**141 文件全绿**
- `RUN_PERF=1`：9/9 通过；`perf_baseline.py`：**无劣化**（exit=0）
- 测试数增量：收集 1649 → **1678**（+29 条回归）

### 剩余项（登记不修，含理由）

| 项 | 级别 | 不修理由 |
|---|---|---|
| `_init_agent` ~400 行拆分 | P3 | 纯结构零行为收益、闭包接线多、风险>收益（两轮审查一致维持搁置） |
| render/table.py 与 overview.py 字体助手重复 | P2 可维护性 | 纯重构无行为收益；候选表漂移已有注释标记，另开轮次 |
| apscheduler 停机砍在途 job（send 与 mark_fired 之间） | P2 | 语义改动需评审（at-least-once → 预占 = 丢提醒换去重）；现状已有「重启即重试」兜底，登记为已知取舍 |
| replaced 行只停用不删（schedules 缓慢增长） | P2 可维护性 | 需 store 层新增删除 API（双实现 + 契约），收益仅为清理噪音；S1 去重已堵住主要来源 |
| PG `kb_add_chunks` 并发双写竞态（无唯一索引） | P2 | 现网单写者；修复需 DDL（唯一索引）触发全新空库迁移义务，风险/收益不匹配，登记另案 |
| `archive_restore` 逐行 execute 往返 | P2-low | id 容错已修；executemany 会失去每行 SAVEPOINT 隔离，7 天窗口数据量小 |
| web_fetch/music 每次新建 AsyncClient | P2-low | 非热路径（搜索偶发、点歌低频）；media 热路径已收敛 |
| engine turn 内 messages 无字符级截断 | P2 | 需要截断标记 + 落库保全量的双轨设计，属独立工作项；上游工具结果已有 8k 截断，风险敞口有限 |
| media `extract_media` / `_looks_like_forward_card` / `GroupContextBuffer.clear` | P3 | test-only 死代码，测试锚着行为；删除需同步迁移测试断言，另开轮次 |
| 用户本地 `.env:36 LLM_FALLBACK_MAX_TOKENS=4096` | P3 | 无代码读取（全仓 grep=0）；属用户本地文件，非仓库问题，`.env.example` 本就未收录 |
