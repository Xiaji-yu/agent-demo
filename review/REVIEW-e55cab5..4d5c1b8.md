# agent-demo 近期 Commit 评审报告

**评审范围**：e55cab5..4d5c1b8（28 commit，83 文件，+7577/−464；含 1.0.3-beta 收口、web 下载/2FA 修复、词云内容词、tool-loop 收尾与抓取熔断、思考步数热更新、ruff 0.17.1 与 CI actions 升级）
**评审日期**：2026-10-26
**评审方式**：5 线并行子代理（只读：Web 写面/下载票据、引擎 tool-loop、群统计词云与文档一致性、依赖/CI/发布、上轮修复复核+记忆/迁移/日志/HA）+ 主代理逐条实证（实跑默认套件与 PG 全新空库套件、ruff 0.17.1 双门禁、coverage 门禁、RUN_PERF、CI API 核对；3 项 M 亲自复现）
**工作区状态**：评审全程干净（`git status` 无未提交变更；评审产物仅本文件与 REVIEW-WORKFLOW.md §9 索引行）

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 最高风险 | **M-1**：`test_poll_fills_pool_and_respects_interval` 依赖 ISO 周奇偶，偶数周（本周）必红 → **main CI 红、不可发布**（【已复现】，CI 双时点实证） |
| 主题结论 | web 票据下载/2FA 窗口：安全模型成立（M-3 文案/展示偏差）；tool-loop 收尾与抓取熔断：方向正确（M-2 预算闸旁路）；词云内容词：与声称总体相符（M-4/M-5 两处实缺陷）；依赖/CI：ruff 0.17.1 三方一致、零 CVE 命中（M-6 机制缺失）；上轮 1H/9M/14L 修复：**逐条落码、无假通过** |
| 测试 | 默认 3227 passed / **1 failed（M-1）** / 73 skipped；PG 全新空库 3313 passed / 9 skipped / 1 failed（同一例）；coverage 85.64% ≥ 80（deselect 后验证）；RUN_PERF 9 无劣化 |
| Lint/类型 | ruff **0.17.1** check + format 双绿；pyright 在 CI Type check 步通过（job 失败点在其后的 Test 步） |
| 声称核对 | 77e21ca/88fd7fd/82657e2/97a08af/4d5c1b8 逐条对照基本相符；88fd7fd 的「3228 passed」为提交时点实测（周 43），今日口径翻转为 3227+1failed，根因立案 M-1，不属夸大；AGENTS/docs 三处数字漂移立案（L13/L14） |
| 门禁（§8.3） | **M-1 修复并复验前，不从 main 切 release / 不发布**；其余 M 均本地可复现，无环境盲区阻断项 |

## 1. 已实证的问题

### M-1【已复现】词云轮询用例依赖 ISO 周奇偶：偶数周必红，main CI 当前为红

- 位置：`tests/test_nudge.py:1371-1383`；根因链 `agentcore/scheduler/hotspot.py:67-86`（`week_kind` 按 ISO 周奇偶分 tech/general 周 + `pick_category` 按权重抽类别）与 `tests/test_nudge.py:93-103`（`_MinRng.random()` 恒 0）。
- 证据：① 本地复现——今日（10-26，ISO 第 44 周=general）`_MinRng.random()=0.0 < 0.5` 选 general，`_FakeSearch` 只有 tech 查询词「AI 大模型」的映射 → 检索空 → 候选池空 → `assert len(svc._hot["pending"]) == 1` 失败；② CI API 双时点实证——`d3d989e`（10-25 22:07 CST，周 43=tech）**success**，`4d5c1b8`（10-26 00:08 CST，周 44=general）**failure**，两 commit 之间仅差 dependabot/ruff/actions 4 文件，无测试代码变更。
- 影响：本周（至 11-01）默认套件每天必红，`Review docs audit` 步不再执行；main 不可发布；`--check-stats` 门禁同源失效。
- 建议：纯测试修复——用例内把 `week_kind` 桩成固定值（或让 `_FakeSearch` 同时覆盖两类查询词）；顺带给 `week_kind` 的周奇偶语义补一条纯函数用例。

### M-2【已复现】步数耗尽后的收尾 LLM 调用绕过当日预算硬闸

- 位置：`agentcore/loop/engine.py:1251-1259`（循环内 step>0 判闸）vs `:1597-1601`（收尾调用前无任何判闸）。
- 证据（主代理独立复现）：预算桩「LLM 调用 >8 次即 blocked」——8 步 probe 后 `spent=9`、`chat_blocked()=(True,'over budget')`，收尾调用仍以 `tools=None` 发出并返回正文。
- 影响：M1 修复确立的「当日不再发起调用」不变量在「最后一步检查通过→LLM/工具把用量推过线→退出循环」这一正常路径上失效；量级 1 次调用（收尾 prompt 携带全部 8 步工具结果，token 不小），账本如实记录。
- 建议：收尾调用前复用同款判闸，blocked 则跳过收尾直接落既有兜底；补「收尾受硬闸拦截」回归用例。

### M-3【已复现】设置总览把「思考步数」显示为启动期旧值，且归在「重启生效」区——与热更新声称同页矛盾

- 位置：`plugins/qq_agent_adapter/web.py:665-667`（`_memory_summary_from` 硬编码 `effective: "boot"`、值取 `engine.config` 启动期快照）、`:1147-1154`（数据源）、`:2955-2963`（JS 把该行 push 进 boot 面板）。
- 证据：内存仿真 `_memory_summary_from({'max_iterations': 8})` → `effective='boot'`；全仓无对 `engine.config['max_iterations']` 的运行时写——热更新只改 `engine.max_iterations` 属性。
- 影响：用户在「模型与 LLM」簇改成 20 并收到「已生效」，同页 5s 轮询的总览面板永远显示 8 且标注重启生效——「改了没生效」的误判（功能真实生效，纯展示层）。
- 建议：`_AGENT_CFG_KEYS` 排除 `max_iterations`，或 `_memory_summary_from` 特判读 `engine.max_iterations` 并标 live；JS 行移到 live 区。

### M-4【已复现】英文大写缩写被停用词小写匹配系统性误杀，且无豁免通道

- 位置：`agentcore/groupstats/tokenize.py:243`（`w.lower() in _EN_STOPWORDS`）。
- 证据（内存实测）：`IT/US/CAN/May/One/No/Do` 全部被 `_acceptable` 拒绝；`AI/OK/ChatGPT/deepseek/byd` 保留。`AGENT_GROUP_STATS_STOPWORDS` 只能追加停用词，无法豁免。
- 影响：与「保留英文技术词」声称存在实际偏差（CAN 总线、IT 运维这类大写缩写恰是技术词常态）；群 1051425116 这类技术群的词云会系统性丢词。
- 建议：`len(w) >= 2 and w.isupper()` 跳过小写匹配；或维护独立的「英文停用词精确匹配（区分大小写）」表。

### M-5【已复现（jieba 源码）+推演】prewarm 重建词性表在锁外：窗口内 `cut()` 全词标 `x` 被静默丢弃

- 位置：`agentcore/groupstats/tokenize.py:99-107`（prewarm 在线程池调 `pseg.dt.initialize()`）与 `.venv/.../jieba/posseg/__init__.py:93-99`（`initialize` = 锁内 `tokenizer.initialize` + **锁外** `load_word_tag`：先 `word_tag_tab = {}` 再逐行填充 ~35 万行）。
- 证据：jieba 源码（上）；`pseg.cut` 读 `word_tag_tab.get(word, 'x')`，空表窗口内全部标 `x` → `_pos_ok('x')==False` → 该消息全部候选词被丢弃，无日志无告警。窗口在每次启动的 prewarm 期出现。
- 影响：启动后头几条群消息的词频可能整批静默丢失（榜单条数/字数不受影响）；与旧版「同窗口只影响切分质量、最坏等待」的语义相比是新的静默丢失面。
- 建议：装配期（线程池）直接完成一次 `pseg.dt.initialize()`，`prewarm` 幂等确认；或 `cut()` 检测 `word_tag_tab` 为空且已初始化时告警并触发重载。

### M-6【推演】dependabot 会周期性 bump ruff，pre-commit rev 无任何联动守卫——97a08af 的漂移会复发

- 位置：`.github/dependabot.yml`（uv 生态无 ruff ignore）；`.pre-commit-config.yaml` rev 与 `pyproject.toml` pin 的一致性无任何测试/脚本断言（全仓 grep 零命中）。
- 证据：f032220 实证 dependabot 会同步更新 pyproject+uv.lock，但 pre-commit 不在其生态；97a08af 是人工收拾。
- 影响：下次 bump 后本地钩子用旧规则集、CI 用新规则集，「本地绿 CI 红」复发。
- 建议：① dependabot uv 生态 `ignore: [ruff]` 改人工三方联动；或 ② 加秒级断言（单测或并入 check_review_docs）：pre-commit rev == pyproject pin == uv.lock version。

### L 批（主代理逐条核过，压缩列出）

- **L1** 备份下载 `FileResponse` 宣告 `accept-ranges: bytes`（实测响应头）但票据单次——断点续传必 403，弱网中断后无提示地要求重走 2FA。建议标 `accept-ranges: none` 或文档注明。
- **L2** 票据 IP 绑定 fail-open 分支：`web.py:392-393` 签发/消费任一侧 IP 为空即跳过校验（当前 TCP 部署不可达；建议与 `_ip_allowed` 同口径：配置了 CIDRs 时空 IP 一律拒）。
- **L3** preview/写第一段对 live 键静态承诺「立即生效」，engine 未装配时第二段才如实回落 boot（建议 preview 做只读探测，或文档注明）。
- **L4** 票据 cookie 的 `Secure` 依赖反代传 `X-Forwarded-Proto`；未传时 HTTPS 终结场景下票不带 Secure（票 90s/单次/256-bit 限级；建议部署文档要求该头，或加显式开关）。
- **L5** 收尾调用不检查 `finish_reason==length`：截断回答被当完整答案返回并写入 memory（此路径无追问补全机会）。
- **L6** 抓取熔断后 `tool_schemas` 仍含两抓取工具：换 URL 的 blocked 调用每个签名独立计数 1，`_MAX_IDENTICAL_CALLS` 兜不住，每步仍烧一次 LLM（建议熔断时重建 schema 或把 blocked 回包计入失败统计）。
- **L7** blocked 回包绕过 `_done`（无 `skill done` 行）且以 `Error:` 开头却不计入失败统计——与 denied 分支的日志/统计口径不一致。
- **L8** `docs/features-messaging.md` ⑤ 未列第四个失败前缀「摘要失败：」（实现 `_FETCH_FAILURE_RE` 含之）；nudge 文案「目标站点可能在拒绝访问」对摘要类失败有误导。
- **L9** 收尾路径的 memory 写入/growth 触发零断言（静态变异存活）；该写入正是 `_abort` docstring 记载的「历史挂半截 tool_call」教训的关键防线，值得一条断言。
- **L10** 词云口径语义变更（全词→内容词）未按 AGENTS §2.5 落 BACKLOG「从 X 改为 Y」（仅 CHANGELOG）。
- **L11** web 群统计词云固定 Top30（`_GROUP_STATS_WORDS`），与 `AGENT_GROUP_STATS_WORDS`「贯通到渲染」的声称（仅 QQ 链路成立）口径分裂且文档未提。
- **L12** `eng` 全放行下 `org/src/com` 等路径/域片段可进词云；`AGENT_GROUP_STATS_STOPWORDS` 豁免通道未在注释/文档点名。
- **L13** `AGENTS.md:132`「17 个设置簇」实为 **18**（实测 `len(CLUSTERS)`；键数 176 改对了、簇数改反了）。
- **L14** `docs/features-interaction.md:329`「165 个键」实为 **176**（存量漂移，本 diff 未触碰该行）。
- **L15** `logging_setup.py:39`「内容类日志的唯一出口」注释与两处不一致：`personas/manager.py:91`（frontmatter 前 60 字）、`config_yaml.py:578`（写后值回显）——均非消息正文，M5 覆盖面声称不夸大，但注释口径不准。
- **L16** HA `_SENSITIVE_DOMAINS` 含 `alarm_control_panel` 但动作枚举无该域（不可达防御纵深；两表一致性备查）。
- **L17** `backup_usb.sh` 的 `KEEP`/`MAX_AGE_H` 无数字校验（mirror 侧 M11 同型面；`$((KEEP+1))` 算术上下文存在命令替换执行面；env 在信任边界内，存量）。
- **L18** 流程：同一 feature 在 release 线与 main 各 merge 一次（43a1d35/6bcd95a 与 d3d989e/0be5986），dependabot 5 个 merge 叠在 release 线——历史噪音 + CI 双触发；与「不在旧 release 分支堆提交」的纪律有出入（无内容损失）。
- **L19** `migrate()` 的 `_validate` 在包装 try 之外（`migrate.py:158`）；`_validate` 内部已把 ValueError 转 `MigrationRegistryError`，仅剩文件 stat 的 TOCTOU 理论漏网——备查。

## 2. 被证伪的发现（防止后续评审重走弯路）

- 票据并发双下载存在竞态窗口——证伪（pop 前无 await，1000 并发重放仅 1 成功）。
- `_WRITE_WINDOW` 同时用作票据 TTL（耦合）——证伪（票据 TTL 独立 `_DOWNLOAD_TICKET_TTL=90`）。
- 2FA 窗口 300s 放大爆破面——证伪（尝试率由 IP 锁定主导，期望成功概率与 30s 时代同阶）。
- 热更新与进行中 loop 存在步数撕裂——证伪（`range()` 进入时求值）。
- `apply_runtime` 与写文件跨 await 乱序——证伪（同临界区内同步执行）。
- `_FETCH_FAILURE_RE` 误判成功结果/对抗样例（「正文内提到抓取失败：HTTP 403」）——证伪（10 用例仿真；成功串以「来源：」/围栏头开头）。
- blocked 回包触发 permission/unknown 误计数——证伪（两个正则均不匹配）。
- `AI/OK` 被大小写变体误伤——证伪（实测保留；真正误伤是 IT/US/CAN，立案 M-4）。
- `jieba.add_word` 的 tag 对 posseg 无效——证伪（共享词典 + user_word_tag_tab 合并，实测整词带 tag）。
- 改 STOPWORDS/GENERIC 后 `_word_pos_ok` 缓存陈旧——证伪（词表不在该缓存键路径）。
- `dict(raw_words)` 重复词覆盖丢计数——证伪（`word_freq` 双实现均已聚合去重）。
- 覆盖率阈值被新测试稀释/变严——证伪（固定下限；范围内部新增代码均有配套测试）。
- dependabot 会把 pyproject 与 uv.lock 的 ruff pin 拧脱节——证伪（两者同步更新；缺的是 pre-commit 联动，立案 M-6）。
- `.env.example` 新增键漏登 ENV_SPECS——证伪（未注释键 176==176 精确相等）。
- 跨站页面借 `samesite=lax` 偷下载内容——证伪（顶级导航下载只落受害者盘，跨源读不到）。
- `_init_memory` 对建池失败应 fail-closed——与实现相反（docstring 如实声明降级内存），非缺陷。
- `/memory` 确认码群聊可被冒用——证伪（`_pending_clear` 以触发者 user_id 为键）。

## 3. 测试与文档状况

- 默认套件（ruff 0.17.1 + pydantic 2.14 + websockets 17.3 环境实测）：**3227 passed / 1 failed（M-1）/ 73 skipped**。
- PG 全新空库（`scripts/scratch_db.py create --yes`，验后已删）：**3313 passed / 9 skipped / 1 failed（同 M-1）**——记忆契约/迁移/PG 独有面全绿。
- ruff 0.17.1：check + format 双绿（224 文件）；coverage：`--cov-fail-under=80` 实测 **85.64%**（deselect M-1 用例后验证）。
- RUN_PERF：9 passed；`perf_baseline.py` 无劣化（merge_parts 1.45x < 1.5x 阈值）。
- CI（API 核实）：`d3d989e`/`88fd7fd1`（10-25 周 43）**success**；`4d5c1b8`/`97a08af`（10-26 周 44）**failure**（Test 步，即 M-1）——上轮「CI 由 API 核实」的销项手段本轮恢复可用。
- 文档数字漂移：AGENTS「17 簇」→实 18（L13）；features-interaction「165 键」→实 176（L14）；BACKLOG DOCS-STATS「3228」与今日实测漂移（M-1 同源，修复后 `--check-stats` 应恢复）。
- 未验证面：① 真机群内 `/memory` 全流程（无法本地复现，延续上轮）；② `websockets 17.3` / `pydantic 2.14` 下 bot 进程的长稳——**运行中进程仍是旧版本模块**（本机 `uv sync` 已升级磁盘版本，下次重启才加载），重启后需观察；③ `ruff 0.17.1` 在 CI 的最终结论（当前被 M-1 挡在 Test 步，Lint/Format/Type 三步已过）。
- 门禁结论（§8.3）：M-1 本地已复现、非环境盲区，但 **main CI 红 = 不可发布**；修复（纯测试改动）并复验前不切 release。无 H 级发现。

## 4. 已验证为「无问题」的关键项（压缩）

票据模型（256-bit/90s/单次原子消费/文件名绑定/换 IP 拒绝/惰性清理有界/CIDR 复核全分支/delete_cookie 实测/前端错误分支）；2FA 300s（HMAC 绑定、nonce 600s 记账覆盖窗口、失败锁定）；热更新（竞态语义、int 归一、审计一致性、llm 簇无 gate 放行）；收尾路径（memory 序列完整无孤儿 tool_call、usage 照记、growth 一致、异常安全、空内容判别）；`_FETCH_FAILURE_RE` 精确匹配（含对抗样例）；max_iterations 热更新语义；词云（posseg 降级对称、用户词典 tag 生效、缓存无词表陈旧、`filter_words` limit 不变量、三处读取接线齐全、`_POS_OK`/词表双层自洽、render 变更属 typing 重构无夹带）；依赖（ruff 三方一致、pyright pin 自洽、CI 步骤与声称一致、lock 零 git/URL 依赖、关键依赖 CVE 粗查零命中、备份脚本历史修复零回退、deploy/migrate 口径、.env.example 176==176）；上轮修复（H1 MigrationError 五类异常仿真、M1 /memory 三方负例、M2 sidecar 读序、M3 清账本前缀、M4 同源/同事务/取锁、M5 落点全量核对、M6–M9 落位；HA 技能安全面 23 条测试）。

## 5. 与在库报告的衔接复核

| 上轮（REVIEW-ee39c8e..e55cab5） | 本轮状态 |
|---|---|
| H1 迁移异常统一 MigrationError | ✅ 真修（五类异常仿真全包装 + 启动 fail-closed 回归；3de6946 收口回滚用例） |
| M1–M9 | ✅ 全部落码（/memory 作用域三方负例、sidecar 读序、restore 清账本、dim 同源/同事务/取锁、日志开关下沉全量核对、deploy --locked、stats 四表守卫、CI --locked/--no-sync） |
| L×14 | 11 条随修全部找到落码；L7/L10 显式遗留声明属实（L10 CancelledError 仍不走 `_abort`，与声明一致） |
| 上轮未验证面 | (a) PG 集成 → **本轮销项**（全新空库绿）；(b) POSIX age → Linux 本地套件已覆盖（销项）；(c) CI → 本轮核实为 **failure**（M-1，回流立案）；(d) 真机群内 /memory → 仍未验证（保留，下轮销项） |

## 6. 修复优先级建议

1. **M-1（当天）**：纯测试修复（桩 `week_kind` 或补齐 FakeSearch 映射）→ main 恢复绿 → `--check-stats` 恢复 → 恢复发布资格。
2. **M-2 / M-3（本迭代）**：收尾前判闸（blocked 落兜底）；总览读 engine 属性并挪出 boot 区。
3. **M-4 / M-5（本迭代）**：大写缩写豁免；pseg 初始化时机前移。
4. **M-6（机制化）**：ruff 三方一致性断言或 dependabot ignore。
5. **L 批**：L1/L8/L10/L11/L13/L14 随下批顺手修；L2/L3/L4/L9 记录在案；L5/L6/L7/L12 观察一次线上行为再定。
