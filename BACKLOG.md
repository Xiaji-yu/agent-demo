# agent-demo 功能完善清单（Backlog）

> **基线**：`main @ ba0b33f`（A2 已提交）+ DDL 笔误修复；M0–M5 已落地；M6 仍为空壳；
> **M7 已全部落地**（调度面五个 job + 成本预算 + 日志归档 + 定时内容推送）
> **规模**：<!-- DOCS-STATS:BEGIN（机器生成勿手改：scripts/check_review_docs.py --write-stats 维护 / --check-stats 核对） -->测试 68 个文件（**2531 收集：2473 通过 + 58 跳过**——`pytest -q` 实测 2026-10-16）<!-- DOCS-STATS:END -->；上一基线 2026-10-07 REVIEW-de09478..workdir 全量修复
> （review/FIX-de09478..workdir.md）为 57 文件 / 2060 收集：2009 通过 + 51 跳过；
> 设 `TEST_DATABASE_URL`（`qqagent_test`）全量套件 2026-10-07 实测 **2051 通过 + 9 跳过**
> （2026-10-11 逐条复核实测 **2151 通过 + 9 跳过**（端口性整改 + 部署脚本落地后复跑），含 wiki/群身份锚点等后续新增用例；
> 2026-10-10 实测 PG 契约子集 `test_pg_store.py + test_store_contract.py` 45 通过），
> `TZ=Pacific/Midway`/`TZ=UTC` 错位时区复跑同数（2026-10-07）；**全新空库**要 `9e93472`（TIMESTZ 笔误修复）之后才能建表跑通。首行计数由脚本实测生成，
> 勿手写估算；PG / 错位时区数字为人工维护并各自注明实测日期。勘误：2026-09-28 的「50 文件 / 1699
> 收集」与 2026-09-30 的「+1 文件 / +12 收集」均为当时手写口径，与实测不符——该
> 区间实际新增 3 个测试文件（`tests/test_config_write.py`、`tests/test_reboot.py`、
> `tests/test_ssh_skill.py`），以本行实测数为准）。
> **工具面**：34 个内置工具（`@registry.register` 装饰器调用点，grep 实测，含
> 新增的 `run_shell`）+ 4 个默认安装的 prompt 技能 + search_web/search_multi
> （`install` 注册，配 `SEARCH_API_KEY` 才有）+ wiki_prts/wiki_blhx
> （`install` 注册，`AGENT_WIKI_SITES` 控制，缺省 `prts,blhx` 启用）
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
| **M7 的定时内容推送** | ✅ 已落地：`agentcore/scheduler/push.py`（LLM 生成 + 模板兜底 + 预算熔断回退）、复用 `schedules` 表（`action='push'`，**无 DDL 变更**）、`/push list\|off\|run\|help` 管理命令、ALLOWED_GROUPS ACL + 每目标每日上限 + 失败退避停用告警 |
| §6 双轨工具系统 | ✅ 旧 `agentcore/tools/` 已删除，收敛到 skills |
| `/status` 真实化、`/persona`、30+ 工具 | ✅ 已做 |
| **P0 权限缺口**：config.yaml `skills.permissions.superusers: ["*"]` 把 registry 层 superuser 门对所有人敞开，ops/log_tail/db_query 曾对白名单群全体成员可用（复现见 FIX 文档） | ✅ 已修（2026-10-05，见 `review/FIX-permission-levels-202610.md`） |
| **MediaWiki 在线直查外挂**：`wiki_prts`/`wiki_blhx` skill（三层降级检索 + wikitext 转 MD + 按预算截取；限速/24h 缓存/429 退避；engine `_result_needs_fence` 围栏）+ 精选子集落库 `scripts/sync_wiki_subset.py`（kind=`wiki`，转换后正文 sha256 判重、变更自动替换先写新后删旧、`--prune` 只动本站清单外来源、重定向按最终标题入库、有意不 PII 掩码保数值） | ✅ 已完成（2026-10-10；`tests/test_wiki_skill.py` 32 例 + `tests/test_wiki_sync.py` 14 例，5 处变异复核全被抓） |
| **CI #91 存量红修复**（7 个用例「本地绿 CI 红」，`0b9f29e`/`babf957` 引入、与 wiki 无关）：① conftest autouse「`.env` 存在就 load_dotenv」把 `DATABASE_URL` 泄漏进本地测试进程，CI 无 `.env` → restore 门用例先 `SystemExit(2)`；② run_shell 依赖仓库里恰好存在 gitignore 掉的 `data/workspace`（cwd 不存在 spawn 即炸）。修法：run_shell 按需创建工作区目录（对全新部署同样是真实修复）；两个测试文件改密闭夹具（`WORKSPACE_DIR` 指向不存在的 tmp 目录、显式 `monkeypatch.setenv DATABASE_URL`） | ✅ 已修（2026-10-10；变异复核：去掉 mkdir 后 4 用例必失败；教训入册 AGENTS.md §5） |
| **群聊身份锚点**（2026-10-10 群 1051425116 事故：模型把群上下文里其他成员的消息当成提问者本人的聊天记录来评价）：① 群上下文围栏后追加「均不是当前提问者本人发送」排除句；② 系统提示注入提问者群名片（`当前提问者：名片（QQ:号）`，名片按引用围栏同口径清洗 + 连串折叠防伪造分隔线，经 pipeline→matcher→engine.context 透传）；③ 群聊能力边界声明（「无法查看任何成员的普通群聊记录」） | ✅ 已完成（2026-10-10；`tests/test_pipeline.py::TestGroupContextIdentityAnchor` 6 例 + `tests/test_engine.py::TestGroupIdentityAnchor` 4 例，4 处变异复核全被抓——含 1 处测试串修正：`-----` 放首段会被 `strip("._-")` 顺带剥掉而漏测折叠逻辑） |
| **「你自己」身份锚点**（2026-10-15 群 1108838060 事故：用户引用 bot 的上一条回复说「我们没崩吧」，系统提示只有提问者身份、被引内容虽全文在围栏里但标题写「消息发送者 云崽（QQ:3629537600）」，模型把「云崽」当外部实体，理解成「用户和别人」——不是读不到内容，是认不出是自己说的）：① 系统提示注入 bot 自身 QQ 号锚点（`你自己的身份：你的 QQ 号是 <self_id>` + 「我们」默认含双方声明，self_id 经 pipeline→matcher→engine.context 透传，非 ASCII 数字不注入）；② 引用被引发送者 uid == self_id 时围栏头部追加「这条是你自己之前发的」（fail-closed：sender 缺失/脏 uid 不标注，错标比漏标更糟）。群流方案经代码核实取消——`_record_group_rule` 早已用 `_is_self_message` 排除 bot 自身发言 | ✅ 已完成（2026-10-15；`tests/test_pipeline.py::TestReplyResolution` 3 例 + `tests/test_engine.py::TestGroupIdentityAnchor` 3 例 + `tests/test_matcher.py::TestSelfIdPassedToEngine` 2 例，4 处变异复核全被抓：删标注 / 恒 True 错标别人消息 / 删引擎锚点 / 不透传 self_id） |
| **权限三级收束**：`AGENT_PERMISSION_LEVEL`（low/medium/high，缺省 medium）替代 `AGENT_SYSTEMCTL_UNITS`/`AGENT_DOCKER_CONTAINERS`/`AGENT_KILL_PATTERNS`/`AGENT_BUILD_SCRIPTS` 四个目标白名单；`run_shell`（high 专属）；`system_status` 收编管理员专属；服务/容器控制加自保护（拒 bot 自身 unit 与 `PG_CONTAINER`） | ✅ 已完成（同上） |
| **元流程门禁与不变量机械化**（2026-10-11，源自通用规范评审的三条建议落地）：① `scripts/check_review_docs.py` 把「M14 索引断裂 / §7 产物散落 / 文档地图断链 / M13 计数漂移」变成归档前必跑的自检（挂 pre-commit + CI `--check-stats`）；② `tests/test_repo_guards.py` 把三条高复发铁律变成 CI 可执行断言（`agentcore` 禁 import nonebot〔唯一豁免 file_sender 且必须 try 保护〕、plugins 禁直接 `finish(权限拒绝)` 必须走 `acl.deny`、代码读取的 env 必须出现在 `.env.example`〔唯一豁免 `LLM_EMBEDDING_MODEL` 已知死代码〕）；③ REVIEW-WORKFLOW 增加 L 级收敛规则（>10 条出批量关闭计划 / 同一 L 不得连续两轮遗留 / >15 触发专项整改）；④ BACKLOG 头部计数改为 DOCS-STATS 机器生成块；⑤ 补齐 4 个漏文档 env（`AGENT_PUSH_STATE_FILE`/`AGENT_SCHEDULER_TZ`/`AGENT_TURN_TIMEOUT`/`AGENT_WEB_BACKUP_DIR`） | ✅ 已完成（2026-10-11；`tests/test_repo_guards.py` 探测器均经投毒夹具验证，真实文件变异 3/3 被抓；自检脚本 3 个检查面变异 3/3 被抓） |
| **平台可移植性与模板安全整改**（2026-10-11，端口性评审落地；Windows 阻塞项按管理员决定暂缓）：① `_scan_pids` 补 `/proc` 缺失容错——macOS 上 proc_kill 不再抛 FileNotFoundError（回归用例 + 变异复核）；② 4 处 `time.localtime` 显示收敛到 `agentcore.tz.fmt_ts`（UTC 服务器 + 上海日键场景下显示与日键同源）+ `test_repo_guards.py` 新增「禁 localtime」机械守卫；③ `.env.example`/README 清除作者个人标识（`SUPERUSERS=2224513919,10002` → 留空 + 「填你自己的号」告警、istore/192.168.1.2/xiaji 的 ssh 示例 → myserver/10.0.0.2/you、ONEBOT token `123456` → 留空）——照抄模板不再把陌生人设成超管；④ README 新增「平台支持与移植边界」一节（七能力 × 三平台支持矩阵 / 移植到其它聊天平台的三步 / 部署假设：单账户、CWD 相对路径、默认时区、reboot 链）；⑤ `config.yaml` 顶部补相对目录按 CWD 解析的说明 | ✅ 已完成（2026-10-11；新用例变异 3/3 被抓，`backup_db.py list` 冒烟通过。遗留：Windows 2 处阻塞〔`os.O_NOFOLLOW` AttributeError、权限位语义缺失〕按决定暂缓，README 支持矩阵已如实标注） |
| **一键部署脚本 `scripts/deploy.py`**（2026-10-11）：子命令 `env / check / install / db / systemd / smoke / all`——幂等、纯标准库（venv 建成前就能引导自身）；两条硬边界：**不自动启动 bot**（`all` 到 smoke 为止，systemd 只生成/安装 unit 不 enable/start，fail-loud 防带着占位配置静默跑）、**不写仓库外文件**（`systemd --install` 唯一例外且需 root）；unit 零密钥（`EnvironmentFile=.env`）+ `ProtectSystem=strict`/`ReadWritePaths=<repo>/data` 硬化 + `Restart=always`（对齐 /reboot 执行链末端）；`check` 区分 strict（放行门）/soft（all 流程配置类降级为 warn），识别 `LLM_API_KEY=...` 占位符与 `SUPERUSERS=*` 残留；`tests/test_deploy.py` 18 例（env 解析/DSN/unit 安全面/不覆盖/soft 分级）+ 5 处变异复核全被抓（其中抓到真 bug 一处：`with_suffix` 对点文件 `.env` 不替换后缀导致备份名变成 `.env.env.bak-*`） | ✅ 已完成（2026-10-11；README「一键部署」小节同步） |
| **群发言榜 + 词云**（2026-10-12，opt-in 默认关）：`/发言榜`、`/词云` 群命令（PIL 渲染图片，base64 CQ:image 发送）+ 周一 9:00 cron 周报（Sink 三态投递）+ `/agent-web`「群统计」只读 tab + **`my_group_review` skill**（用户说「点评我一下」时由 LLM 判断调用，个人周报卡片图；schema 无 user_id 参数=越权面归零；昵称/话题词进 prompt 前过 `safety.fence_untrusted` 围栏（含行中值 scrub）；预算硬闸门熔断放弃生成且不缓存、事实卡照发；同周评语落 `group_reviews` 缓存，重复查看零 LLM 成本；usage 记 `group_stats_review` 路由）+ **周报推送白名单** `AGENT_GROUP_STATS_WEEKLY_GROUPS`（空=不推 fail-closed；只管推送，采集/命令/skill 仍按 ALLOWED_GROUPS；条目 ASCII 数字校验）+ **群消息原文归档** `AGENT_GROUP_ARCHIVE`（独立 opt-in，仅 PG：白名单群每条非 bot 消息落 `group_messages_raw`，保留 30 天；PK (group_id, message_id) 幂等去重 + 四条查询索引；合并转发照常归档但不展开内容、被引只记 reply_to——被引/被转发内容从不进词云与 prompt；停机 flush 不丢、kill -9 丢 ≤ flush 周期；聚合管线不变，归档用于对账/回溯/点评增强〔路线甲〕）+ **点评 archive 增强 5 项**（本人高频词〔归档 text 重切，与词云同口径〕/转发次数/发图次数/被引用次数/代表性原句；全部过围栏；归档关闭时 prompt 与增强前一致）。**行为变更（从 X 改为 Y）**：群聊上下文「群消息绝不落库」（group_context docstring）→ **白名单群聚合落库**——隐私收敛为三条：仅 `ALLOWED_GROUPS`、只存聚合（每用户每日条数/字数/小时直方图 + jieba 词频，**消息原文不进任何存储**）、35 天 retention；合并转发消息整条不采集（转发内容不是本人发言），引用照常。采集挂在既有 `group_recorder`（不新增 matcher，零额外事件开销）；存储独立模块 `agentcore/groupstats/`（PgStatsStore/InMemoryStatsStore 语义一致、参数化测试；PG 失败启动降级内存）；flush 走独立 interval job，停机 flush 插在 `lifecycle.shutdown_agent` 的 debounce flush 与 memory.aclose 之间（失败合并回内存，重试不双计）；jieba 为新增依赖（纯 Python，缺失降级 bigram）；词云用墨阶三色 + 确定性等环间距螺旋布局（无随机数，同输入同输出）；点评 skill 住 plugins 层注册（发图要 Sink，遵守 agentcore 不 import nonebot 铁律，照点歌 skill 先例） | ✅ 已完成（2026-10-12；`tests/test_group_stats.py` 64 例含 PG 双实现 + `test_group_stats_render.py` 27 例 + `test_group_stats_review.py` 33 例 + `test_group_stats_route.py` 49 例 + `test_group_archive.py` 37 例含 PG + `test_lifecycle.py` 5 例 + `test_web.py` 6 例；变异复核 49/49 被抓；README 功能小节 + `.env.example` 17 键同步） |
| **性能复查落地：内存后端向量检索 + 每轮重复 embedding**（2026-10-12）：① `InMemoryMemoryStore.recall_facts`/`kb_search` 原为**纯 Python O(N·dim) 且同步跑在事件循环里**（5000 块 × 2048 维实测单次检索 1.66s、2000 条 facts 召回 0.67s，期间整个 bot 冻结）→ 写入时预计算向量模长（新 `_pair_sim` 每候选只做一次点积，省两趟遍历）+ 快照后 `asyncio.to_thread` 外包（实测检索 1.66s→0.67s、召回 0.67s→0.26s，且不再阻塞事件循环；PG 后端走 HNSW 不受影响）；② 引擎每轮把**同一条 user_message** 分别用于 facts 召回与 KB 检索、各 `embed` 一次 → 改为算一次向量两处复用（`_recall_facts`/`_recall_knowledge`/`KnowledgeBase.retrieve` 均新增可选 `query_embedding`），并把 facts 召回并入原有 `asyncio.gather`（每轮少一次 embedding 往返）。**语义不变**：空向量/维度不符/零模仍按旧 `_cosine_sim` 口径得 score=0.0（threshold=0.0 时仍入列），双实现契约测试全绿 | ✅ 已完成（2026-10-12；`test_engine.py::test_query_embedded_once_per_turn_for_facts_and_kb` + `test_memory.py` 两条边界口径用例，变异复核 2/2 被抓） |
| **性能基线门禁抗抖动**（2026-10-12）：`scripts/perf_baseline.py` 单次采样受 CPU 竞争/调度影响，同一指标相邻两次可差 2 倍（实测 `qq_plain_ms` 30/60ms 交替，一次报 2.27x 劣化、另一次无劣化）→ 新增 `--repeat`（默认 3）**多次采样按指标取中位数**再对比，消除纯抖动假阳性 | ✅ 已完成（2026-10-12；`test_perf_baseline.py::TestMedianMetrics` 5 例；三次实测已无假阳性） |
| **F1 用户自定义工具型技能（`data/skills/*.py` 加载器）**（2026-10-16）：与 prompt 型（`*.yaml`）并列的**代码型**介质——`agentcore/skills/local.py` 按路径 importlib 加载目录下 `*.py`（flat 不递归、字典序），调模块级 `register_skills(registry)`；适配器 `_init_agent` 在 prompt 型 manifest 之后接线（两种介质同名都按「先到先得」，后者被拒+WARNING）。安全边界：① 包装注册表在 `register()` 入口拒同名（dict 赋值永不发生，内置对象完好——防本地 `ssh_run.py` 静默顶掉内置提权），**冲突点直接 WARNING 明示技能名**（加载器再报所在文件）；② 目录解析后落在 fs 可写工作区内（含相等）→ 拒绝加载全部 `.py`（否则聊天内容可经 `fs_write` 预埋代码，启动即执行）；③ 坏文件 WARNING 跳过不阻断启动；④ 默认 `read_only=False`（opt-in `mark_read_only` 透传）；⑤ 信任模型=部署者自担（同改源码/装插件），不做沙箱不管依赖。非目标：无热重载（重启生效）、`skillinstall` 不管 `.py`（文件系统即管理界面）。模板与 env key 纪律（key 只走 `.env`，tool call 参数全量落 `agent.log`）见 `docs/features-skills.md` 新节 | ✅ 已完成（2026-10-16；`tests/test_local_skills.py` 19 例覆盖 A1–A3/S1–S4/O1，变异复核 3/3 被抓〔去同名检查 → S1/S2 红；去掉 try/except → A2 全红；去冲突点 WARNING → S1/S2 红〕；全量默认套件 2473 通过 + 58 跳过；README 功能索引 + 目录树同步） |

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

**A2（P0）历史裁剪 + 滚动摘要（启用 `sessions.summary`）**　*✅ 已完成*
- **落地**：`get_history_window`（带 id/ts 窗口）/ `get_session_messages_between`（水位补漏）/
  `get_session_summary` / `save_session_summary` 四个存储原语（双实现 + 契约测试）；
  `sessions` 加 `summary_upto_id` 水位列（幂等 ALTER，老库自动补）；引擎按
  `agent.history_token_budget` 保守估算裁剪历史，掉出窗口的消息滚动压缩进
  `sessions.summary` 并以不可信围栏注入 system prompt（`summary_enabled` 可整体关闭）。
- **追加（2026-10-13，见 §6.7）**：历史补上**时间维度**——`get_history_window` 带内部
  `ts`，最后一条距今超过 `agent.history_stale_after`（默认 6h）时注入「对话时间线」警示；
  `summary_max_tokens` 默认 400 → 4096（推理型模型把预算耗在思维链上，空 content 曾让
  摘要水位静默停更），空正文改为 WARNING 留痕。
- **验收**：裁剪/水位推进/摘要失败不伤主流程/开关等价/围栏防闭合均有自动化测试
  （`tests/test_engine.py::TestRollingSummary`、`tests/test_store_contract.py`）；
  摘要质量依赖模型，真模型长会话 E2E 待线上观察（`unknown`）。

### D 组：Web 管理面演进（2026-09-28 规划，经两轮交互定稿）

**D1（已完成，P0）设置只读视图**　*✅ 2026-09-28*
`/agent-web/api/settings`：LLM 线路（复用 `model_status()`）/ 运行开关+唤醒词 /
预算 / 记忆摘要（`agent:` 段白名单键）/ RAG / 备份归档，每块标注生效时机
（即时 / 需重启）；白名单键、`base_url`/api_key 不进响应体、降级不炸页；
页面加「设置」tab（即时/重启两栏）。回归：`tests/test_web.py::TestSettingsView`
（9 条，5 个护栏全部变异复核）。

**D4（已完成）日志查看面**　*✅ 2026-09-29*
`/api/logs/files` + `/api/logs`（tail 窗口 64KB/上限 256KB + `after` 增量游标 +
轮转重置 + 级别/模块/字面量 grep 过滤，行对齐保证）+ `/api/logs/download`
（流式，>200MB 拒绝，`logs_downloaded` 审计）。**内容口径（管理员决策）：原始日志
不脱敏**——AGENTS.md §4 已注明这是"响应体不含消息正文"的唯一显式例外，README
提示勿分享截图。门禁与 settings 相同（Bearer + 可选 CIDR，无新配置项）。
回归：`tests/test_web.py::TestLogsView`（14 条），护栏变异复核 6/6（文件白名单、
行对齐记账、下载上限、级别过滤、下载审计、不脱敏锚点）。

**D2（P1）受控写入面**　*D2-1 ✅ 2026-09-29 完成；D2-2 待做*
- **D2-1（已完成）**：`config_write.py` 纯函数核心（校验/定位/补丁/备份/原子写/
  运行态同步）+ web 两段确认流水线（HMAC 确认码绑定 nonce+键+新值+30s 窗口、
  单次有效；改前快照滚动 10 份；原子写 + 写后验证失败自动还原 + 审计）。
  白名单 7 键全热生效（写 .env + os.environ + budget 属性双写）；门禁 =
  CIDR 已配 **且** `AGENT_WEB_WRITE=1`，缺一写路由不挂载。回归
  `tests/test_config_write.py`（23 条，REVIEW-3ce6e0a..de09478 补 M1 窗口
  权限 / L3 行模型 2 条）+ `TestSettingsWrite`（20 条），
  护栏变异 6/6（确认码/备份/运行态同步/白名单/回滚/原子写）。
  定Nonce 修正：确定性 HMAC 会让同窗口写同键同值的第二人被误判重放
  （实现期实测），挑战改带随机 nonce。
- **D5（已完成，2026-09-29）进程重启**：`reboot.py`（白名单 argv 解析 + C/B/A
  执行链 + 停机编排 + 后台任务强引用）+ `/reboot` 命令（superuser 直执行，黑名单
  静默）+ web `/api/reboot`（写门禁 + 两段确认 + `reboot_requested` 审计）+ 设置
  页「重启」按钮。回归 `tests/test_reboot.py` 28 条；护栏变异 8/8（白名单开放/路径
  形态/spawn 不回落/execv 不回落/预校验失效/编排颠倒/任务不强引用/不走 lifecycle）。
  部署前提：systemd unit 需 `Restart=always` 或 compose `restart: unless-stopped`
  （否则 C/B 都不可用时重启即死亡，README 已警示）。
- **D2-2（待做）**：drift 表（文件值 vs 运行值差异 + "待重启 N 项"提示条）、
  env 白名单扩容（出站/防抖等组，逐键核对读取时机后入册）。
- ~~**原定稿要点**~~：v2 定稿的「范围/写入语义/三件套/门槛」四条**已被
  D2-1 取代**（"改运行态不落盘"是 P1 时代的旧语义；实际实现 = 写 `.env` 文件 +
  运行态同步，见 [REVIEW-26fec4d..3ce6e0a](REVIEW-26fec4d..3ce6e0a.md) M9）。
  当前有效口径：
  - **范围**：`config_write.WRITABLE` 硬编码锁死的 7 个 env 键（vision /
    group_context 开关+条数+TTL / wake_words / budget 两键），运行期不可扩充；
    凭据与安全边界键（API_KEY/TOKEN/SUPERUSERS/黑白名单）**刻意不在表内**，走 SSH。
  - **写入语义**：手术式改 `.env`（唯一落盘）+ 运行态同步（os.environ / budget
    属性 / group_context 单例属性）；优先级模型不变（env > config.yaml > 默认）。
  - **不变量三件套**：审计（`config_write`，key+掩码旧新值+backup+source_ip+
    confirm_nonce）+ 两段确认（HMAC 绑定 nonce+键+值+30s 窗口，按 nonce 单次
    有效）+ 改前快照滚动 10 份 + 原子写 + 写后验证失败自动还原。
  - **门槛**：`AGENT_WEB_WRITE=1` **且** `AGENT_WEB_ALLOW_CIDRS` 已配置（双门，
    缺一写路由整体不挂载）。

**D3（P2）配置持久化**　*架构级，需单独评审*
config.yaml 受控回写（yaml 回写丢注释需取舍）或 DB 覆盖层（env 之上的
key-value override，动 store 双实现契约）；"动 .env" 触碰"全部 env 驱动"铁律，
除非单独给出方案，否则不做。

### E 组：run_command 白名单放宽 + SSH 远程只读诊断（2026-09-30 管理员决策，已完成）

**E1（已完成，P1）只读运维命令进 run_command 白名单**　*✅ 2026-09-30*
- **决策依据**：私聊日志（`data/logs/agent.log` 09-30 04:00/05:49）——管理员问
 「core 强制重启」「ssh 上 192.168.1.2 看内存」，bot 因白名单无 `ps`/`ssh` 只能
  转人工；实际只读运维面已有 6 个 superuser skill（proc_detail 等），缺的是
  **LLM 可自组合的原始命令**。三档商定：只读命令放开 / 进程控制不放开 / SSH 走
  专用技能。
- **落地**：A 组免参数白名单（ps/top/free/df/du/uptime/uname/nproc/whoami/id/
  ss/netstat/lscpu——无任何写向选项，全局四道闸覆盖）；B 组子命令/flag 白名单
  （systemctl 只读子命令且拒 `--host/-H/-M/--machine`；journalctl 拒
  `--file/--root/--directory/--rotate/--vacuum-*`；dmesg 拒 `-C/-c/-w/-n/-D/-E`；
  hostname 只查不改；top 必须 `-b`）。`run_command` 描述同步 + 引导 `/reboot`。
- **刻意不放行（从"可放开"改为"不开"）**：`kill` 与 systemctl 动作类子命令
  （restart/stop/mask/edit…）——重启本服务走 `/reboot`（C→B→A 三段式退路）；
  杀进程不可回退，不把执行权交给自由拼参数的 LLM。依据：AGENTS.md §2 High 级
  边界变更需可复现证据与方案，方案经三档交互定稿。
- **验收**：`tests/test_runner_security.py::TestReadonlyOpsWhitelist`（15 条：
  正例放行、反例逃逸向量——`systemctl --host=root@192.168.1.2`、
  `journalctl --file=/etc/shadow`、`top -W`、`hostname myhost`、`ps -o x;id`、
  `df /` 等，加真实 `ps/free` 执行）；护栏变异复核 5/5（systemctl 子命令闸 /
  journalctl flag 闸 / top -b 闸 / sshpass 密码位置 / 远端命令常量性）。

**E2（已完成，P1）ssh_run 专用技能（内网设备只读诊断）**　*✅ 2026-09-30*
- **落地**：`agentcore/skills/ssh_skill.py`——host/用户/端口只来自
  `AGENT_SSH_HOSTS`（`alias=user@host:port`，未配置 = 整体关闭，fail-closed）；
  远端命令是**代码写死的常量菜单**（free/uptime/hostname/uname/ps/ps_mem/top/
  meminfo/cpuinfo/disks/mounts/dmesg/logread/netstat/ifconfig/services/users），
  LLM 只报 action 名——ssh 把命令交给远端用户 shell 执行，自由拼接 = 远端 RCE
  权交给 LLM，故不容许任何插值；凭据走 `AGENT_SSH_KEY_<ALIAS>`（私钥，权限过宽
  按缺失处理）或 `AGENT_SSH_PASSWORD_<ALIAS>`（经 `SSHPASS` 环境变量进 sshpass，
  **不进 argv/日志/返回值**——tool call 参数会全量落 agent.log）；
  `BatchMode=yes`/`ConnectTimeout=5`/`accept-new`+专用 known_hosts（TOFU）/
  `-F /dev/null`/`IdentitiesOnly=yes`；20s 超时 + 8000B 截断。
- **已知残余（如实披露）**：`accept-new` 首次连接可被同网段 MITM（换来一次远程
  只读执行权），收紧需预置 known_hosts 指纹；远端 busybox/GNU 差异只影响可用性。
- **验收**：`tests/test_ssh_skill.py`（14 条：host 解析/脏条目跳过、alias 与
  action 双白名单、密码不进 argv、`--` 隔断、sshpass 位置、密钥模式、超时/截断、
  权限双层 fail-closed）；护栏变异复核见 E1。

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
| C1（P1）✅ | **LLM 重试退避** | ~~`client.py` 无 `retry`/`backoff`~~ 已落地：主模型瞬时故障重试（可重试分类 + 线性退避），重试耗尽才切 fallback | 原方案（指数退避）改为线性退避 `delay*attempt`，语义等价且可测 |
| C2（P1）✅ | **turn 级超时与取消** | ~~只有单次 LLM 60s 超时~~ 已落地：`AGENT_TURN_TIMEOUT`（默认 180s，0=不限）包住 engine.run | 超时回一句降级文案；debounce 队列不受影响 |
| C3（P1）✅ | **tool_calls 并行** | ~~串行 `for tc in ...`~~ 已落地：全只读工具 `asyncio.gather` 并行（fail-closed 白名单），含写操作仍串行 | `Skill.read_only` 标记 + `mark_read_only` 集中注册 |
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
| **2.6（P1）** | **Web 只读总览** | 运维只能靠 QQ 里 `/status` 和翻日志；`/healthz`+`/metrics` 一直没做 | **首期已完成**（`plugins/qq_agent_adapter/web.py`）：挂在现有 FastAPI 上、`/agent-web` 前缀、Bearer token（未配 `AGENT_WEB_TOKEN` 不挂载）+ 可选 IP 白名单 + 只读。展示 LLM 主/备实时线路、协议端连接、今日/历史 token 与成本（按模型）、组件与 KB 统计、`diagnostics` 最近事件。**后续（未做）**：`/healthz` `/metrics`、写入类功能（配置编辑 / 记忆删除 / skill 安装——须配审计+二次确认+改前备份） |
| **2.7（P1）** | **CI 全量套件用错位时区跑** | `agentcore.tz` 已把「今天」统一为 Asia/Shanghai，但任何新代码/测试桩只要用裸 `date.today()`/`datetime.now()` 写日键，就**只在 UTC runner 的北京 0–8 点窗口（=UTC 16–24 点）与生产错开一天**——CI 平时绿、撞窗口才红，三次实红：`0bc7905`（概览时钟）、`839ca7e`（每周提醒）、`f782812→2820b04`（budget/usage 账本） | CI 测试步骤加 env `TZ: Pacific/Midway`（UTC-11，任意时刻都与 Asia/Shanghai 不同日，每次运行都暴露日键错位，零新增时长）；本地复现同理 `TZ=Pacific/Midway pytest -q`。备选：双时区矩阵 job（pytest 时长翻倍，不推荐）。**验收**：把任一日键故意改回 `date.today()` 后该 job 必红 |

---

## 3. 里程碑状态

| 里程碑 | 状态 |
|---|---|
| M0–M4 基础链路 / 记忆 / 人格 / 工具 | ✅ 完成 |
| **M5 RAG** | ✅ 完成（含注入防护与脱敏） |
| **M6 多 Agent** | ⬜ **空壳**（`agentcore/multiagent/__init__.py`）—— 见 §5，**建议缓做** |
| **M7 调度 / 预算 / 归档 / 定时内容推送** | ✅ 全部完成：调度（`kb_digest`、`archive_prune`、`db_backup`、`reminders`、`push` 五个 job）、成本预算（`be13897`）、日志归档、定时内容推送（`agentcore/scheduler/push.py`） |

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
| 点歌语音的"真送达"确认 | SnowLuma issue #422：语音 highway 上传失败时协议端误报 ok + message_id，上层拿不到真失败。MVP 接受该风险：发送成功只记 message_id 与载荷大小（排查"用户说没收到"的唯一线索），且**绝不重发**；真失败需带外确认。见 `agentcore/music/sender.py` 与 `review/AC-music-playback.md` C4 |
| 点歌语音的多账号串号 | HTTP 发送用哪个账号由 `NAPCAT_HTTP_URL` 指向的协议端决定，无法指定"触发事件的 bot"。单账号部署无影响，记为已知取舍不修。见 `agentcore/music/sender.py` C5 |
| 敏感内容过滤 | 面向陌生公开群时才是必要项 |
| 流式输出 | QQ 分条发送体验提升有限，且与 debounce/分段逻辑耦合；可选 |
| 群聊免前缀窗口 | 纯体验项，改动局部，可随时穿插进任意一次提交 |
| **字体子集化瘦身（2026-10-16 实测后暂不做）** | 目标：`data/fonts/wqy-zenhei.ttc` 16.8 MB 占全部跟踪内容的 ~80%（`review/` 仅 892 KB，不动）。用 `pyftsubset --font-number=0` 实测三档：CJK+拉丁+标点 **7.3 MB**（覆盖损失 21082/42285 = **49.9%**：丢谚文 11172 + 扩展A 6582 + 非BMP 649 等）、+扩展A **9.7 MB**（丢 33.8%）、+谚文/全东亚 **11 MB**（丢 7.4%：西里尔/希腊/非BMP）。覆盖到的文本渲染逐像素一致（PIL 哈希相同），但本仓字体链是**单一字体、无逐字形回退**（`agentcore/render/fonts.py`），缺字即豆腐块——损失不可接受。替代方案：① **浅克隆**（已写入 README/CONTRIBUTING：`--depth 1` / `--filter=blob:none`）；② 若日后仍要减重，走**按需下载**（不发字体，缺字体时现有候选链会降级为文本，无豆腐块；需自托管一份带校验和的固定 URL）——未实施 |
| **点歌触发回退成确定性直路由** | 已按用户要求从「唤醒词 + 点歌/放歌 子命令」**改为 LLM skill**（`play_music`，模型判断是否放歌）。代价已知且接受：① 模型可能不触发/误触发（原 AC G 段"结构性消除"不再成立，改由 handler 四道硬闸兜底：群白名单 / 私聊 superuser / 歌名校验 / 账号级冷却）；② `superusers: ["*"]` 部署下未授权群从"静默"变"明拒"；③ 每次点歌多一次 LLM 往返。序号选歌保留确定性 matcher 兜底。若体验回退，可改回并在 `review/AC-music-playback.md` 记档 |

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

**M 级 19 条全部已修**（按报告 §2 表格行数分组：安全/注入 3、存储契约 4、备份/归档/蒸馏 6、并发/资源 6。
报告表头与 FIX 文档曾写「17 条」是批二时的已知数——存储第 4 条（负 `limit`/`top_k`）与备份第 6 条
（`_backup_jsonl` 整表 fetch）在第四批随共享契约测试 / 游标流式关闭，见 [`review/FIX-679c9b3..c472e56.md`](review/FIX-679c9b3..c472e56.md) 的口径统一说明）。

**第四批（已完成，见 FIX 文档 §"第四批"）**：

| 项 | 结果 |
|---|---|
| `_backup_jsonl` 整表 `fetch` 进内存 | ✅ 改为 `conn.cursor(...)` 游标流式 + 每 500 行 `to_thread` 序列化；失败清理 `.part`；回归见 `tests/test_backup.py`（原 `test_backup_jsonl.py`，b688317 更名；初建 7 条现 12 条，含真库 1200 行用例） |
| **共享契约测试缺失**（内存 vs PG 各测各的） | ✅ 新增 `tests/test_store_contract.py`：同一批断言参数化跑两个实现（初建 20 条，现 36 条）。**顺带查出并修掉 4 处此前未发现的漂移**：`list_facts`/`recall_facts` 负 limit 在 PG 直接抛 `LIMIT must not be negative`、`kb_search`/`kb_list_sources`/`messages_after`/`schedule_due` 非正 limit 两实现语义不同（内存"去掉最后 N 条" vs PG 抛错）、`kb_last_digest_watermark` 内存取 max 而 PG 取最新来源 |
| 假通过用例余项 | ✅ 全部加固并逐条变异复核（见下） |
| 文档/工程余项 | ✅ 本文件数字刷新 + 新增根 `AGENTS.md` 交接文档 |

**测试加固明细（第四批，均已用"改坏实现→用例必须失败"复核）**：
- `test_outbound.py`：恒真断言 `"超过软上限" not in caplog.text`（该字符串全仓不存在）→ 改为"该路径不得产生任何 WARNING"；`_repack_chunks` 真实覆盖（新增 `test_repack_enables_forward_instead_of_spamming`：禁用重打包必然落到逐条发送而失败）；nickname 参数传递（显式值 / env 兜底两条，忽略参数必然失败）
- `test_pipeline.py`：空消息与纯图片消息由 `assert text.strip()` 改为断言**具体文案**；补 `_display_key`（base64/内联数据掩码、文件名清洗截断）与 `_download_for_su`（非管理员只回显掩码出处、管理员落盘相对路径、下载失败备注）——两组此前**零引用**
- `test_file_sender.py`：`_napcat_upload_private_file` 此前**从未被执行**，补 5 条（base64 载荷字段/鉴权头/无 token/异常返回原文/公开入口优先走 NapCat 而不回落 OneBot）

**L 级 19 条**与**测试补强**见报告 §3–§4，其中 ✅ 已修：`acl` 私聊拒绝零覆盖、`ruff format` 门禁、
PG 契约测试接入 CI、`CONTRIBUTING` 版本号 3.11、`.env.example` 补 `AGENT_SKILLS_DIR`；
假通过用例上轮点名的 8 条中 6 条在三四批加固，**漏掉的 2 条已在本轮（FIX-679c9b3..c472e56）补修**。

### 6.2 第四批复核中新发现的小项（非必修，记录以免丢失）

| 项 | 说明 |
|---|---|
| 带图占位分支不可达 | `pipeline.py` 的 `"（请结合用户发来的图片回答）"` 只在 `extra_images` 非空且 `text` 为空时命中，而每张进入 `extra_images` 的图片都必然先写一条 notes → 该占位实际不可达（保留作防御）。无图占位 `"（用户没有输入文字内容）"` 可达并已被断言锁定 |
| `_reconstruct_content_from_memory` 是死桩 | `skills/file_sender.py` 该函数恒返回 `""`（读 `driver._agent_memory` 后直接 `return ""`），当前无人调用 |
| `LLMClient.embeddings` 无调用方 | `agentcore/llm/client.py` 的 embeddings 方法无生产调用点（embedding 走 `agentcore/embedding/client.py`）；环境变量 `LLM_EMBEDDING_MODEL` 因此未入 `.env.example` 文档——属已知死代码路径，记录结论，不补文档 |

### 6.3 线上实测：上下文丢失与记忆投毒三连（2026-09-28，已修复）

现象（`data/logs/agent.log` 15:45–15:50，群 1108838060）：用户引用他人消息问
「他是不是嘉豪」，连续两轮得到「消息里没标是谁发的」；追问「我明明艾特的还有
一个人」后，模型自称"**记忆抽取器**"并称"只看到 @ 和引用"。

| 级别 | 根因 | 位置 | 修复 |
|---|---|---|---|
| **M** | `at` 段被静默丢弃：`@bot @群友` 里被 @ 的群友对模型完全不可见，指代无法解析 | `pipeline._build_user_text` / `media.text_from_segments` | ✅ `at` 段文本化为 `[@QQ:号]` 占位（@bot 不重复占位；全角数字脏数据丢弃） |
| **M** | 引用围栏只说「其他用户发送」，无发送者归属，「他是不是XXX」无据可依 | `pipeline._resolve_reply` / `_build` | ✅ 围栏头部注明原发送者（群名片+QQ；昵称打散空白/去括号/截断，防围栏伪造） |
| **M** | **记忆投毒**：抽取器吃组装全文（含引用），把**别人**在引用里的话记成该用户的长期事实（海外/留学自述整段入库） | `engine.run` → `facts.extract` | ✅ 新增 `extraction_text` 参数，抽取只吃 `merge_user_texts`（用户本人消息）；召回仍用全文（读路径不产生记忆） |
| **M** | **角色名泄漏闭环**：抽取 prompt 自称「记忆抽取器」→ 该词随元语句存入 facts → 同轮先存后召回，立刻进 system prompt → 主模型自称"记忆抽取器" | `facts.EXTRACT_PROMPT` / `engine` M4 段 | ✅ prompt 改为"后台长期记忆标注程序（不是对话参与者）"并新增元语句禁令；`is_transient_fact` 补 `_META_PATTERNS` 代码级兜底；污染 facts 已清理 |

**遗留（未改，记录在案）**：`engine` 仍是"先存后召回"——当轮写入的幻觉事实当轮
即可被召回（本次"记忆抽取器"身份错乱的放大器）。改为"先召回后写入"会改变既有
语义（M4 注释表明原顺序系刻意保留），需单独立项评审。

**数据清理记录**：session 69 的 12 条污染 facts（id 236–247，含 11 条引用投毒 +
1 条角色名元语句）已删除；备份在 `data/backups/facts-cleanup-session69-20260928.json`。
清理脚本首次误删了同 session 7 条正常旧记忆（id 22/59/60/77–80），已从备份原样恢复
（文本/来源/created_at 不变，embedding 因备份未含向量而由 `EmbeddingClient` 重新生成——
与"换向量模型后旧向量失真"同理，短期召回质量可能略降，属已知代价）。

**回归用例**（均已做"改坏实现→必须失败"变异复核）：`test_pipeline.py::TestAtSegmentRendering`
/`TestQuotedSenderAttribution`/`TestMergeUserTexts`、`test_engine.py::TestExtractionScope`、
`test_facts.py::TestMetaFactFilter`。

### 6.4 线上事故：私聊 /reboot 不重启（2026-09-30，已修）

| 项 | 内容 |
|---|---|
| 现象 | 私聊发 `/reboot`，bot 不重启，反而由 LLM 作答「云崽没有重启键」（`data/logs/agent.log` 06:30）；此前 02:15 试 web 重启按钮返回 404 |
| 根因 1（代码） | `plugins/qq_agent_adapter/__init__.py` 的 `_load_plugin_modules()` 从未 import `.reboot`——reboot.py 的 `on_command` 是**模块级注册**，模块不进 sys.modules 就没有响应器；`8575d6a` 提交时只补了 web handler 内的懒 import，漏了命令面接线。消息于是落给 priority=10 的聊天响应器（LLM） |
| 根因 2（时序） | 事故进程 02:20:44 启动（systemd `ActiveEnterTimestamp`），而 reboot.py 02:37:30 才提交——鸡生蛋：/reboot 要用得先重启，重启原先没有可用入口 |
| 佐证 | `pkgutil.iter_modules(["plugins"])` 只发现顶层包（nonebot 不递归子模块）；模拟 `_load_plugin_modules()` 后 `TrieRule.prefix` 有 `/push` `/reset` 等而**没有** `/reboot` |
| 修复 | `_load_plugin_modules()` 补 `.reboot` import（try 隔离，与 poke 同口径：装载失败只降级该功能，web 重启的 handler 懒 import 仍可用）；`reboot_route` 全局句柄 |
| 回归 | `tests/test_reboot.py::TestRebootMatcherWiring` 3 条（子进程隔离跑生产加载路径：加载器接线 / trie 注册 / `handle_event` 端到端路由）。变异复核 3/3——删掉接线三条全红 |
| 教训 | 「模块级 on_command + 动态 import 加载」的接线没有测试锁过：旧测试只调 handler，从不验证**响应器真的注册**。新模块带 matcher 时必须同时锁注册与路由 |

### 6.5 线上事故：改 `.env` + `/reboot` 不生效，新白名单群戳一戳无响应（2026-10-12，已修）

| 项 | 内容 |
|---|---|
| 现象 | `.env` 的 `ALLOWED_GROUPS` 加入新群 `868209960` 后发 `/reboot`（strategy=execv），同群聊天一切正常，但戳一戳毫无反应——无回复、无 `[poke]` 日志、连冷却/未授权提示都没有 |
| 根因 | `bot.py` 的 `load_dotenv()` 是 override=False，旧进程早已把旧 `.env` 注入 `os.environ`；`reboot.py` 的 `os.execv` 用**当前** environ 自替换，旧值被原样带给新进程，新进程的 load_dotenv 对已存在的键不覆盖 → 运行中的 `ALLOWED_GROUPS` 停留在旧的 5 群。戳一戳走 `is_strict_allowed`（superuser 之外还要求群在白名单——它回的是整机概览图）→ 静默 return（仅 DEBUG 日志）；聊天走 `is_allowed` 对 superuser 无条件放行，所以故障只体现在戳一戳上 |
| 佐证 | `/proc/<pid>/environ` 实测 `ALLOWED_GROUPS` 5 群 vs 磁盘 `.env` 6 群（mtime 03:14:36）；journal 时间线：03:14:48 `/reboot`（execv）→ 03:17:04 戳 868209960 无 `[poke]` 日志 → 03:18:00 戳 1051425116 正常回图 68489 bytes；拿进程实际环境跑 `is_strict_allowed`：868209960=False / 1051425116=True；`load_dotenv`+execv 行为复现：execv 重启后新进程读到的仍是旧值 |
| 修复 | 新增 `agentcore/env_snapshot.py`：`prime()` 把「抓出生环境快照 → load_dotenv」的顺序写死在一起（bot.py 启动调用一次）；`reboot.py` 的 re-exec 改用 `os.execve` 只带出生快照（无快照时退化为原 `os.execv`）——`.env` 派生值全部丢弃、由新进程从文件重读。优先级模型不变：真实环境变量（systemd/shell 导出）本就在快照里且 load_dotenv 不会覆盖它 |
| 回归 | `tests/test_env_snapshot.py` 5 条（prime 顺序不变量 / override=False 语义 / 幂等 / 返回副本）+ `tests/test_reboot.py::TestReExecPristineEnv` 4 条（旧值不传给新进程 / 出生环境变量原样保留 / 无快照退化 execv / 端到端读到新 `.env`）。变异复核 3/3：execve 改传当前 environ、`env` 恒 None、prime 顺序反转分别 2/3/1 条必失败 |
| 教训 | ①「注入型配置 + 继承 environ 的进程自替换」是隐性组合：重启要等价于全新启动，environ 必须先回出生状态、再让新进程重读文件；②静默 fail-closed 的 ACL（未授权只记 DEBUG）会把「配置没生效」伪装成「功能坏了」，诊断成本极高——同日定夺：未授权改 **WARNING 留痕（含会话键）**，不回话的静默不变（回归：`test_poke.py::TestAcl::test_unauthorized_is_silent_but_warns`，主人会直接查日志或让 bot log_tail）；③「`/reboot` 与 `systemctl restart` 不等价」这件事 README/AGENTS.md 此前都没写，现已在两处补明 |

### 6.6 REVIEW-1e0f5ae..a0a6bb4 修复记录（2026-10-12，同日修复）

双 H（tar 超限放行、PIL 渲染阻塞事件循环）+ M×10 + L 必修 8 条全修，逐条修复方式、回归与 14 处变异复核见
[`review/FIX-1e0f5ae..a0a6bb4.md`](review/FIX-1e0f5ae..a0a6bb4.md)。两条行为变更在此入档：

| 项 | 变更 | 理由 |
|---|---|---|
| 群统计采集/原文归档白名单空集语义（M1） | 「空集 = 全采集」→「**空集 = 不采集/不归档**」（fail-closed），与 `ALLOWED_GROUPS` 在聊天面的 acl 语义对齐；开关开而白名单空时启动打 WARNING | 076613e 的 README 声称「仅白名单群」，旧实现空集放行等于静默落库所有群消息（含原文），隐私口径不能 fail-open |
| 容器备份 pg_dump 注入形态（M7） | `docker exec -e PGPASSWORD=…`（密码进 argv）→ `docker exec -i <c> sh -s` + stdin 脚本注入（密码不进 argv，参数整段进脚本） | 密码驻留宿主机 `/proc/<pid>/cmdline` 对同机其他本地用户可读；restore（psql）路径 stdin 被恢复 SQL 占用，stdin 注入不可行，**有意豁免**保留 `-e` 形态并已在 `db_backup.py::_with_container_env` docstring 入档，勿「顺手统一」 |

显式遗留 L（依 REVIEW-WORKFLOW §3 L 级收敛规则登记，下轮复核）：L4/L5（围栏名单扩容与 re-exec 入口 env，需管理员决策）、L12（文案口径，产品决策）、L14/L15（性能/断言打磨）、L16/L17/L19/L21（wiki 工具链）、L23（部署语义）、L25–L28（工具链打磨）。

### 6.7 线上事故：闲置 5 天的会话历史被当成"刚才"，回复援引古早话题（2026-10-13，已修）

| 项 | 内容 |
|---|---|
| 现象 | 群 `1076073471`，07:53:48 用户（QQ 2224513919）只发了"这是什么"+ 一张超识图预算被跳过的图（引用的是别人发的表情）。bot 回复"图片太大…不过结合**你刚才在聊 265K + Z890 + 缩缸** 的话题，盲猜你发的可能是 CPU-Z 截图…**刚才说的** PCIe/电压/频率设置"——而该话题是 **5 天前（10-08 02:11–02:24）** 的对话 |
| 根因 | 会话历史的取数窗口只有**条数**（`summary_fetch_limit=200`）与**预算**（`history_token_budget=3000`）两个维度，**没有时间维度**：`get_history_window` 不带时间戳，`_project_history` 也不发。该会话（`g:1076073471:2224513919` → session 71）自 10-08 02:24 起闲置 **125.5 小时**，07:53 那轮 prompt 里的历史正是 10-08 的 4 个来回（id 1599/1600/1605/1606/1607/1608，2570 token = 预算的 86%），紧贴着当前消息——模型看不到任何时间戳，只按消息相邻性把它当成"刚才"发生的事。图片被跳过（零视觉信息）只是放大了这个误判 |
| 佐证 | 用引擎真实函数对库内 session 71 的消息复算：kept = [1599,1600,1605,1606,1607,1608]；当前用户消息（id 1733）全文只有引用表情 + "这是什么" + `[图片1 超出识图大小预算，已跳过]`；排除其它来源：`facts` 表无 265K/Z890/缩缸 相关事实（该用户 114 条全是鸣潮角色/八字类）、`persona_growth` 只提"聊过硬件"、KB 检索 top4（相似度 0.59~0.61）全是原神/阴阳师语录、缩缸块（chunk 137584）未进 top4 |
| 附带缺陷（同轮查出，已修） | **滚动摘要静默停更**：`summary_max_tokens=400` 对推理型模型（step-3.7-flash）太小——实调 API 返回空 content + `finish_reason=length`，`_summarize_messages` 静默返回 `(None, None)`：水位永不推进、旧摘要持续注入、日志零记录。session 71 的摘要从 09-30 起卡死在水位 1379、内容只剩 27 字垃圾（`**AMD RX 9070 GRE 核心参数：**\n-`），全表 30 个会话水位全部落后（session 73：1147 vs 1732）。水位 1379→窗口 1599 之间的 21 条消息既不在历史也没进摘要，静默丢失 |
| 修复 | ① `get_history_window` 双实现带出内部 `ts`（InMemory `time.time()` / PG `created_at.timestamp()`，与 `id` 一样不进最终 messages）；② 新增 `agent.history_stale_after`（默认 21600 秒，0=关闭）：保留窗口最后一条距今超阈值时，system prompt 注入「对话时间线」警示——明说上下文发生在多久以前（"5 天前（10-08 02:24）"）+ "除非用户明确在继续旧话题，不要把历史内容当作刚刚发生的上下文来引用，也不要用它猜测当前消息的指代"。历史**不丢弃**（用户说"接着上面说"时信息仍在）；③ `summary_max_tokens` 默认 400 → **4096**（与 `distill_max_tokens` 同源教训：推理型模型会把输出预算耗在思维链上），空正文分支补 **WARNING 留痕**（finish_reason + max_tokens） |
| 回归 | `tests/test_engine.py::TestStaleHistoryTimeline` 5 条（超阈值注入警示含距今天数/新鲜不注入/阈值 0 关闭/无 ts 不猜/空历史不炸）+ `TestRollingSummary::test_empty_summary_text_warns_and_keeps_watermark` 1 条（WARNING + 水位不动 + 不伤当轮回复）+ `test_sent_history_has_no_internal_fields` 增加 `ts` 不得外发断言 + `tests/test_store_contract.py::test_history_window_contract` 增加 ts 契约断言（双实现）。**变异复核 5/5**：警示恒不注入 / 去掉阈值 0 关闭路径 / 空摘要静默 / ts 外发 / InMemory 不返回 ts 分别被对应用例抓住，还原后全量复跑 |
| 验收 | 默认套件 `pytest -q`：**2375 通过 + 58 跳过**；带 `TEST_DATABASE_URL` 全量：**2443 通过 + 9 跳过**（PG 契约子集 45 通过）；ruff check + format --check 全绿（0.9.6） |
| 教训 | ①「最近 N 条」≠「最近的语境」：只有条数/预算维度的历史窗口，会把闲置数天的旧轮次无缝拼到当前消息前，模型必然误判时间关系——**持久会话上下文必须带时间维度**；②推理型模型的输出上限不能按"摘要几百字"给（distill 侧早有注释，summary 侧漏改），且**空返回必须留痕**，否则"静默停更"能伪装成"功能正常"数周；③识图预算跳过图片时，模型只能拿上下文做模式匹配，陈旧上下文的危害被放大——这两处风险是同源的 |

### 6.8 人格权重被成长层稀释：注入位置降级为数据块 + 提议 prompt 禁降格描述（2026-10-15，已改）

| 项 | 内容 |
|---|---|
| 现象 | 管理员反馈「因为成长导致原本的人格权重有些降低」——`yun` 人格的语气/个性表现变弱 |
| 根因 | 成长层原本**紧贴人格段之后**注入（`engine.py` 的 `_build_system_prompt`），而它框定的职责是「仅用于调整语气与相处方式」，与人格段**完全重叠**；人格 md 是叙事型长文（`yun.md` 1516 字），成长层是指令密度更高的短文本（≤500 字），近因叠加下后者在"怎么说话"这件事上更容易赢。而回顾 prompt 默认就会提炼出「相处从客气变随意」这类**降格**文本，直接对冲人格的「有态度/小毒舌/不是应声虫」。且当时只有 `AGENT_PERSONA_GROWTH_INTERVAL` 一个旋钮，`max_growth_chars` 写死 500——除了整体关闭没有任何调温手段 |
| 改动（从 X 改为 Y） | ① **注入位置**：成长层从「人格段之后」改为「facts / 知识库等用户派生数据之后」，从第二份人格指令**降级为数据块**；② **人格段**：新增主权声明「以上性格特征是人格的主干：后续任何关于用户关系、共同经历或相处方式的描述，都不得覆盖、弱化或替换它们」；③ **成长层框架句**：从「仅用于调整语气与相处方式」改为「称呼偏好与共同经历，仅用于补充相处细节；它不改变你的人格、性格与语气特征，与人格设定冲突时以人格设定为准」；④ **收尾回锚**：原回锚句只覆盖工作流，成长层之后新增「以上工作流、长期记忆与关系成长记录都不改变人格设定」；⑤ **提议 prompt**（`_REVIEW_SYSTEM`）：删掉「相处模式的变化」这一提炼项，新增禁降格硬约束（点名「更随意/不用敬语/更亲近/变温柔了」）；⑥ **合并 prompt**（`_MERGE_SYSTEM`）：新增「若新进展与助手既有人格特征相冲突，丢弃新进展」 |
| 新增旋钮 | `AGENT_PERSONA_GROWTH_MAX_CHARS`（默认 500，< 50 由 `GrowthManager` 构造层 `max(50,...)` 兜底）——与人格段争"怎么说话"的唯一调温手段，调小直接降低成长层的覆盖面积 |
| 存量数据蒸馏（2026-10-15 22:39 已执行） | 改注入逻辑只能防**新**文本，库里 2 条旧记录仍含降格描述。按新约束用 LLM 重写（删"描述助手自身语气/风格变化"的句子，保留称呼偏好与共同经历）：`2224513919` 500→**315 字**、`3865067623` 148→**67 字**。执行前全量备份到 `data/backups/persona_growth-20261015-223903.jsonl`（需回滚时手工 UPDATE 还原）；写入侧同样过 `neutralize_fence_lookalikes` 并截断到 500 |
| 新增命令 | `/growth [QQ]` 查看成长层原文、`/growth clear [QQ]` 清空（均仅 SUPERUSERS）——此前 `/reset` 只清消息不动 `user_state.persona_growth`，要清它只能手工连库写 SQL。清空前回显原文前 200 字；对无记录用户幂等提示、不谎报成功；轮数计数不重置（清的是文本不是相处历史） |
| 回归 | `tests/test_persona_growth.py` 新增 `TestGrowthDoesNotDilutePersona`（6 条：位置必须排在 facts 之后 / 人格段主权声明 / 成长层框架让人格 / 段后回锚 / 无成长层不出回锚 / 无人格无成长层措辞）+ `TestReviewPromptForbidsPersonaDilution`（5 条：删掉相处模式项 / 禁词清单 / 范围收窄 / 合并丢弃冲突 / 约束真的到达 LLM 调用）+ `TestGrowthMaxCharsClamp`（2 条构造层兜底）；`tests/test_store_contract.py` 成长层契约补 `clear_persona_growth` 双实现一致性 4 断言；`tests/test_admin_import.py` 新增 `TestGrowthCmdRule`（14 条：6 种合法形态 / 8 种非法形态含全角数字与过短过长 QQ / 权限门 / self 过滤挂在 rule 上）+ `TestGrowthCmdHandler`（6 条：清空/查看 × 有/无记录 × 显式 QQ + 存储失败只回一句）+ `TestGrowthMaxCharsSemantics`（4 条 env 解析）。**变异复核 4/4**：成长层移回 facts 之前 / 删掉「以人格设定为准」/ 删掉禁降格约束 / 把 finish 圈回 try 内（重犯 §5 老坑，实测管理员会收到「已清空…」+「操作失败」两条矛盾回复）分别被对应用例抓住，还原后与备份逐字节一致 |
| 验收 | 默认套件 `pytest -q`：**2447 通过 + 58 跳过**；**带 PG 全量套件：2515 通过 + 9 跳过**（PG 用例全部跑起，比默认多 68 个）；ruff check + format --check 全绿。**PG 双实现已验证（2026-10-15）**：`tests/test_store_contract.py` 在 `qqagent_test` 与全新空库上各 **36 passed**（含 `test_persona_growth_contract[pg]`，`clear_persona_growth` 的 4 条契约断言）；变异复核追加 1 项——去掉 PG 侧 `WHERE persona_growth IS NOT NULL` 后仅 `[pg]` 红、`[memory]` 仍绿，证明契约真的锁死了双实现一致（而非恒真断言）。测试库已重建为严格 CI 等价的空库，三个手工 scratch 库（`qqagent_scratch_contract`/`agent_demo_pgtest`/`qqagent_freshtest`）已清理，测试库策略写入 AGENTS.md §3 |
| 教训 | ①**同一职责域的两段文本挨着放，短的那段赢**：人格 md 靠叙事与细节立人设，关系成长靠密指令说话，把后者放在前者正后方等于让它覆盖前者——"分层注入"不等于"平级注入"，派生数据必须放在结构上更低的位置；②生成侧的约束要和注入侧的约束**同时**收，只改注入框架不改生产 prompt，LLM 仍会持续产出降格文本；③只有一个开关（整体开/关）的功能不配谈"调优"，阈值类旋钮至少要暴露上限 |
| 追加整改（2026-10-15 23:00，回应"会不会随时间变臃肿"） | ①**截断改句子边界**：旧实现两处裸切片（`proposal[:300]` 与 `merged[:500]`），LLM 不守字数时会把最后一句话切成半句（实测落库 500 字、末尾「…第19句话」）。新增 `_clip_at_sentence`：只在 `[limit*0.7, limit]` 区间内找最靠后句终符，标点稀疏时退化为硬截断——否则两段式记录会被整段砍掉（实测把 limit 前唯一的换行符当边界，500 字记录只剩 5 字）。`_propose` 的提议截断是同款缺陷的另一个入口，由端到端用例抓住；②**合并 prompt 给出写入优先级**：旧措辞「保留仍然有效的…去掉过时或重复的」是空话，LLM 保守偏差下倾向全留 → 500 字额度被最旧话题占据。改为「称呼偏好 > 最近进展 > 较早话题，字数不够主动删最旧的」；③**字数曲线日志**：`persona growth merged: user=%s chars=%d (old=%d proposal=%d cap=%d)`，配合 `/growth` 区分"该调温"与"该清理"。**长度不会膨胀**（两道截断封顶，实测第 2 次确认即到顶、12 轮模拟无增长）；会恶化的是语义信噪比，这是当前已知的残留取舍 |

### 6.9 解压白名单展平 flag 击穿条目表执行位清理 + 评审 L 批修（2026-10-15，已修）

| 项 | 内容 |
|---|---|
| 来源 | [REVIEW-a0a6bb4..6745556.md](review/REVIEW-a0a6bb4..6745556.md) H1（线1 子代理发现、主代理实包复现）+ L 批量关闭计划（必修 12 条） |
| 现象 | `tar --strip-components=1` / `unzip -j`（均在白名单内）把 `pad1/pad2/evil.sh` 展平落成 `out/evil.sh`，`strip_entry_exec_bits` 按扫描期条目名找不到文件——实包复现 0755/0777 存活（对照组 0644/0666 清理正常），重组「curl 拉包→解压→run_build_script（继承完整环境）」链。涉事 flag 为既有白名单成员：上轮 H1 修复（扫描超限即拒）未覆盖此门 |
| 修复（H1） | 两 flag 移出白名单（解压前拒绝；非必需能力）；runner 模块 docstring/注释同步移除原因；L13 不可达防御分支文案不再渲染「-1 个」 |
| 回归（H1） | `tests/test_runner_security.py::TestH1FlattenFlagsRefused`：4 形态 permitted 负例 + tar/unzip 端到端「拒绝且零落盘」2 条 + 2 处白名单正向断言翻负；**变异复核：flag 加回白名单 → 6/6 红**；评审报告 §1 复现脚本翻转验收（0755/0777 → 解压前拒绝、零落盘） |
| 同批 L 必修（12） | L1 jieba 预热任务强引用（`_BACKGROUND_TASKS`，仿 growth._spawn/debounce 纪律）；L2 runner 注释「告警放行」→「即拒」；L7 stale 跨时钟源注记 + 契约不断言 ts 单调（部署假设不入契约）；L8 low 档启动日志后缀改「仅只读三件，控制类未注册」；L9 启动日志清单守卫测试（`TestStartupLogListGuard`，变异 1/1 抓死）；L10 conftest 夹具序 load_dotenv→guard→strip（.env 来源 DATABASE_URL 的防误连比对真正生效）+ 剥离夹具 docstring 修正；L11 行数快照实测更新；L12 双重装饰器删除；L13 防御分支文案；L14 `_pair_sim` docstring 如实化（仅内存侧锁定、与 PG NaN 口径有意分歧）+ PG score 注释；L15 kb_search sources 快照化 `dict()` + 注释如实；L16 embed 失败路径代价注记 |
| 遗留（4，理由入档） | L3 `_CONTAINER_RE` 首字符 `-`（行为无害，随下次 runner/skills 触碰收紧）；L4 `growth.aclose` 无调用方（停机接线属 lifecycle 语义决策）；L5 self 引用标注兜底路径缺失（需 resolve_quoted_media 扩展返回 sender，跨模块接口）；L6 raw-dump 全文落日志（调试开关限长属管理员可视性取舍）——明细见 [FIX-a0a6bb4..6745556.md](review/FIX-a0a6bb4..6745556.md) |
| 验收 | 默认套件 **2454 通过 + 58 跳过**；带 PG 全量 **2522 通过 + 9 跳过**（`qqagent_test`，无 DDL 变更未重建）；ruff check + format 双绿（0.9.6）；复现翻转：两攻击面均「解压前拒绝、零落盘」 |
