# agent-demo 近期 Commit 评审报告

**评审范围**：1e0f5ae..a0a6bb4（10 个 commit）
**评审日期**：2026-10-12
**评审方式**：5 条并行子代理分线审查（线1 沙箱/权限/超管工具、线2 群统计功能面、线3 消息流水线/适配层、线4 wiki/检索与 RAG 隐私、线5 工具链/部署/CI/文档）+ 主代理逐条实证（实跑双套件、2 个 High 亲自复现、4 个 Medium 复现/仿真、其余逐条静态裁决）
**工作区状态**：git status 干净（评审全程零改动受版本控制文件；复现脚本均在 /tmp/opencode）

**变更概览**：10 commit、112 文件、+16726/-478。主题：权限三级制/run_shell/dangerous-admin-actions 落地（0b9f29e，上轮预审项入库）、裸唤醒短路（babf957）、wiki 在线直查（d14f011）、CI 修复（9364c99）、群聊身份锚点（d8d3974）、元流程自检脚本（3a2708f）、平台可移植性（823e352）、一键部署（66b2b14）、群发言榜/词云/点评/原文归档（076613e，本轮最大变更）、/reboot 环境快照（a0a6bb4）。

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 最高风险 | **H1** tar 条目扫描超 20 万条目「告警放行」→ 尾部恶意条目执行位保留，与 run_build_script 组成「curl 拉包→解压→执行攻击者脚本（继承完整进程环境）」链（已复现）；**H2** 群统计 PIL 渲染同步跑在事件循环（实测冻结心跳 853ms/正常 50ms，群友可触发、周报可放大） |
| 严重度分布 | H×2（均已复现）、M×10（4 复现/仿真 + 6 静态坐实）、L×28 |
| 测试 | 默认套件 **2322 passed / 58 skipped**（45.2s）；带 PG **2390 passed / 9 skipped**（56.1s，qqagent_test 库）；ruff check + format **双绿**（0.9.6，与 pin 一致） |
| 声称核对 | 10 个 commit 逐项核对：8 个属实无夸大；076613e 两处失真（「仅白名单群」遇空集 fail-open→M1、「话题词含行中 scrub」不实→M3）+ 计数漂移（210 vs 221→L）；823e352「四处 localtime 收敛」字面属实但收敛目标未达成（datetime 形态残留→M8）；9364c99 修法逐例相符但根因（conftest 泄漏源）未除→M6 |
| 上轮衔接 | REVIEW-de09478..workdir 的 H1/M1–M11 主体全部真修在位（0b9f29e 入库）；上轮未验证面 PG 套件本轮实跑销项；本轮 H1 是上轮 M1 执行位清理防线的**新旁路**（上限放行），非回归 |
| 门禁（§8.3） | 2 个 H 均在已提交主干代码：**不涉及回滚，但 H1/H2 必须立即进入 FIX 流程**（均为小改动），修复+回归前不宜发布 |

## 1. 已实证的问题

### H1｜tar 条目预扫描超 `_MAX_TAR_MEMBERS` 后 `break` 放行——条目守卫与 M1 执行位清理同时失效【已复现】

`agentcore/workspace/runner.py:233,1182-1188`：扫描循环 `if i > _MAX_TAR_MEMBERS: logger.warning(...); break`，超限后返回 `(None, 已扫描条目)`——**None 即放行解压**；随后 `strip_entry_exec_bits`（runner.py:1464-1469）只清理已扫描条目，垫底的恶意条目完全绕过。

复现（真实阈值 200_000，`/tmp/opencode/repro_tar_h.py`）：构造 200001 个填充条目 + 尾部 `scripts/evil.sh`（mode 0777）→ `_tar_scan` 耗时 6.8s 返回 `problem=None`、evil.sh 不在已扫描名单 → 真 `tar -xf` 解压 → `evil.sh` 落盘 mode `-rwx------`（对照组：无填充时同样条目被剥为 `-rw-------`）。

影响：medium+superuser 会话被注入诱导时，`curl`（https 公网白名单内）拉攻击者 tarball → `tar -xzf -C scripts` → `run_build_script("evil")` 直接执行攻击者脚本，且该技能**继承完整进程环境**（LLM_API_KEY/DATABASE_URL）。上轮 M1 修复（按条目表剥执行位）对正常包有效，但被「20 万填充条目 + 恶意条目垫底」的 DoS 上限旁路。zip 侧 `_zip_scan` 用 `namelist()` 全量扫描无上限，两侧行为不一致佐证应 fail-closed。

**修复建议**：超限改为返回拒绝原因（放弃解压），与 `_MAX_SYMLINK_SCAN` 一并统一「超限即拒」口径；回归用例用小阈值 monkeypatch + 变异复核。

### H2｜群统计三处 PIL 渲染同步跑在事件循环，实测冻结心跳 853ms【已复现】

`plugins/qq_agent_adapter/stats_route.py:149,164,478`：`render_rank_png` / `render_cloud_png` / `render_review_png` 全部同步调用；stats_route.py 与 agentcore/render/ 全目录 grep 无 `to_thread`/`run_in_executor`。

复现（`/tmp/opencode/repro_line2.py`）：`render_cloud_png` 80 典型词 **850ms**、80 长词最坏 **776ms**；`render_rank_png` 20 行 312ms；**决定性证据**——asyncio 心跳任务在同步渲染期间完全停跳，渲染前后心跳间隔 **853ms**（正常 50ms）。

影响：与历史 H1（表格渲染 7.3s 阻塞，REVIEW-46c85d1..6ec3f7c）同一不变量、同一违反方式，量级小 4~8 倍但落在**群友可触发**的 `/发言榜` `/词云` `/群评` 热路径：渲染期间同进程所有聊天/提醒/推送全部卡住约 1~2s；最坏输入可被群成员刻意构造（刷长词进词频表），周报 cron 按白名单群数串行放大。

**修复建议**：三处调用点包 `await asyncio.to_thread(render_xxx_png, ...)`（参数纯数据、返回 bytes，卸载安全）；回归断言「渲染期间心跳仍跳动」。

### M1｜群统计采集/原文归档白名单**空集 = 全采集**（fail-open），与 acl 同键语义相反，076613e「仅白名单群」声称失真【已复现】

`agentcore/groupstats/collector.py:68` 与 `raw.py:293` 同款：`if self._allowed and gid not in self._allowed: return`——空集恒不拦截。复现：`StatsCollector(store, allowed_groups=set())` 下 gid=999999（不在任何白名单）3 条消息照常写入 daily/words；`RawArchiveBuffer` 同配置下两个白名单外群的**消息原文** `insert_rows` 落库。而同一 env 键在聊天面是 fail-closed（`acl.py:82`：`return bool(ALLOWED_GROUPS) and str(group_id) in ALLOWED_GROUPS`，注释明说「空集合表示没有群允许」）。同 commit 内周报白名单又是「空 = 不推」（config.py:99），三套语义并存。触发组合：`AGENT_GROUP_STATS=1`（尤其叠 `AGENT_GROUP_ARCHIVE=1`）但未配 `ALLOWED_GROUPS` → bot 在任何群不聊天，却静默落库**所有群**消息（含原文）。

**修复建议**：对齐 acl 语义（空集 = 不采集），属行为变更需记 BACKLOG；至少在 `configure()/configure_archive()` 对「开关开而白名单空」打 WARNING 并修 README。补空集语义回归（现测试只锁解析格式一致）。

### M2｜jieba 首次词典初始化（实测 842ms）挂在采集热路径，冻结事件循环【已复现（量级）】

`agentcore/groupstats/tokenize.py:158`：`jieba.lcut` 首次调用承担词典加载（实测首切 **842ms**、第二次 0.05ms）；`collector.record` 由 matcher 群记录器在事件循环内同步调用 → 启动后**第一条群消息**冻结循环约 0.8~1s（冷缓存更久）。与 H2 同类，一次性，定 M。

**修复建议**：装配后 `to_thread` 预热 `jieba.initialize()`，或在文档注明。

### M3｜点评 prompt 话题词行未做行中 scrub，与「昵称/话题词/原句…含行中 scrub」声称不符【已坐实（静态）】

`agentcore/groupstats/review.py:87-88`：昵称(:64)、own_words(:73)、quote(:85) 都过 `_scrub_value`（打散行中 5+ 连字符 run），唯独话题词行裸拼。`fence_untrusted` 只中和整行形态伪造围栏，行中值可穿透。实际可利用性低（token 须过 `_acceptable`，分词器几乎不可能产出连字符 run）——但把围栏安全寄托在分词行为上属防线错位，且 commit 明确声称了。**修复建议**：一行补 `_scrub_value` + 逃逸用例。

### M4｜db_skills/shell_skill 注册期级别过滤不在模块内，单点依赖 builtin.py 调用行【已坐实（静态）】

`action_skills.py`/`workspace_skills.py` 均为模块内自过滤，而 `register_db_skills`（db_skills.py:238）与 `register_shell_skill`（shell_skill.py:99）自身不查级别，防线是 `builtin.py:140/147` 两行 `if at_least(...)`。当前生产路径双层齐全（handler 内兜底也在），但未来新增调用方漏掉这行 `if`，low/medium 档 schema 即注册——handler 兜底文案「当前权限级别未开放…」会向探测者**确认技能存在**，与 registry「无权限与 unknown 同文案、无探测口」语义相悖。**修复建议**：把 `if not at_least("high"): return` 收进两个 register 函数体（与 action/workspace 对齐）。

### M5｜sync_wiki_subset `--prune` 删除失败被静默吞掉：不计数、退出码 0【已坐实（静态）】

`scripts/sync_wiki_subset.py:181-189`：`delete_source` 失败 `continue`，不计入 `failed`（也不计 `pruned`）；总览行只见成功数；`main` 只 `if total["failed"]: sys.exit(1)`（:358）→ **删除失败退出码 0**，运维视角「清理成功」实际残留。违背「脚本失败退出码必须非零」纪律。**修复建议**：新增 `prune_failed` 并入退出码判定，补 `delete_error` 用例。

### M6｜9364c99 是补丁式修复，conftest `.env` 泄漏源未根治【已坐实（静态）】

`tests/conftest.py:87-100`：session 级 autouse `_normalize_superusers_env` 仍在 `.env` 存在时 `load_dotenv(override=False)`，把**全部键**（真实 DATABASE_URL/LLM_API_KEY/SUPERUSERS）注入测试进程。9364c99 对 7 个失败用例改显式 setenv 属实，但 `tests/test_db_skills.py:8-9` 注释自证新负担：「conftest 会把 .env 的 DATABASE_URL（生产）载入，故本文件 autouse fixture 一律删掉」——防线依赖每个新文件自觉，正是 #91 的结构性根因原样保留。另 `_guard_destructive_pg_tests` 只拦 `TEST_DATABASE_URL == DATABASE_URL`，不拦测试代码直连 `DATABASE_URL`。**修复建议**：conftest 统一 autouse `delenv("DATABASE_URL")`（或 `.env` 存在即告警），机制化而非约定化。

### M7｜容器备份把 PG 密码放进 docker CLI 进程 argv【已坐实（静态）】

`agentcore/backup/db_backup.py:125-136`：`return [*prefix[:idx], "-e", f"PGPASSWORD={password}", ...]`——密码驻留宿主机 `/proc/<pid>/cmdline`，同机其他本地用户可读；本机 pg_dump 路径走 env 是安全的。代码注释声称这是设计（容器内不继承环境变量，L15），但与「密码不进 argv」纪律同族（ssh_run 先例）。缓解：单用户部署、进程秒级。**修复建议**：改 `docker exec -i … sh -s` 由 stdin 注入；或显式入档豁免。

### M8｜时钟收敛不完整：datetime 形态残留 6 处（含用户可见语义层），机械守卫抓不到【已坐实（静态）】

`agentcore/render/overview.py:200`（`datetime.fromtimestamp(ts)` 无 tz——web 总览大时钟）、`:768`（`datetime.now()`）；`agentcore/skills/utility_skills.py:30,37,57,71`（`date.today()`/`fromtimestamp`/`datetime.now()`——「/日期」类回复与「明天」解析基准）。UTC 宿主机 + `AGENT_SCHEDULER_TZ=Asia/Shanghai` 时与日键差 8 小时，正是 823e352 要消灭的症状。守卫 `test_repo_guards.py:33` 正则只匹配 `time.localtime(`。823e352「四处 time.localtime 全部收敛」字面属实（grep 零残留），但收敛目标未达成。**修复建议**：六处改走 `agentcore.tz`；守卫扩为 `datetime.fromtimestamp\((?![^)]*tz)` + `date.today()`（豁免 tz.py/store.py）。

### M9｜deploy 的 .env 解析器与运行时权威（dotenv）口径分歧，check 检测可绕过【已复现】

`scripts/deploy.py:70-88`。仿真对照：`export SUPERUSERS=*` → deploy 解析出键 `'export SUPERUSERS'`（dotenv 解析出 `SUPERUSERS='*'`）；`FOO=bar # c` → deploy 把行内注释当值（dotenv 剥掉）。后果：`export` 写法下 strict check 误报「SUPERUSERS 为空」且 `*` 通配检测**漏报**（运行时有 acl 构造期剔除兜底，不致提权，故 M 不升 H）；行内注释使 check 判定值 ≠ 运行时值。`.env.example` 现无这两种写法，属放行门缺口而非现行漏洞。**修复建议**：剥 `export ` 前缀；docstring/README 声明值内禁行内注释。

### M10｜fresh clone 下 `systemd --install` 产出的 unit 首启即失败：ReadWritePaths 指向不存在的 data/【已坐实（静态）+ systemd 语义推演】

`scripts/deploy.py:142` `ReadWritePaths={repo}/data`，`cmd_systemd`（L385-461）无创建 data/ 步骤；`git check-ignore data` 退出码 1（data/ 本身不被忽略，运行时才创建）→ 干净 clone 无该目录。systemd.exec(5)：不带 `-` 前缀的不存在路径使 mount namespace 搭建失败、服务启动失败，报错不指向真正原因。**修复建议**：`--install` 时 `mkdir(exist_ok=True)`，或 unit 写 `ReadWritePaths=-{repo}/data`。

### L（28 条，压缩列出；批量关闭计划见 §6）

- **线1**：L1 run_command 技能描述漏列 tar（workspace_skills.py:97，与白名单不符）；L2 `test_env_layer_wildcard_discarded` 先 discard 后断言锁不住（test_permissions.py:140，防线本身成立）；L3 TestPrime 污染真实 os.environ 无清理（test_env_snapshot.py:33）；L4 docker_ps/proc_detail/system_status 不在引擎围栏名单（既有面延续，system_status 本轮收编 superuser 未同步加围栏）；L5 re-exec 入口 argv 读当前 environ，改 `AGENT_REBOOT_ENTRY` 后单次重启用旧入口
- **线2**：L6 `AGENT_GROUP_STATS_WORDS`>80 完全无效（config 钳 500、render 恒截 80、调用不传 max_words）；L7 groupstats/__init__ docstring「原文不落任何存储」与本包 raw.py 归档自相矛盾；L8 归档 retention 只在 flush 后触发，死群过期原文不清理（对比 memory archive 有 prune cron 先例）；L9 commit 210 例 vs BACKLOG §49 221 例漂移（少报不夸大，同提交自相矛盾）
- **线3**：L10 停机 stats/archive flush 无 deadline（lifecycle.py:57-65，仅 try/except；TCP 黑洞场景挂到 systemd SIGKILL）；L11 record_raw 不查 BLOCKED_USERS——被拉黑用户原文落库 30 天，L21 注释未覆盖此档取舍；L12 sender_card 24 字内祈使句残余 + engine 侧无归一契约声明；L13 save_image_atomic 覆盖旧 0644 .part 时 mode 不降权（media.py:336，O_CREAT mode 仅创建生效，窗口极窄）；L14 web 群统计端点 5s 轮询无 TTL 缓存（同文件 `_HISTORY_TTL` 有先例）；L15 群统计「无消息正文」负面断言偏弱（test_web.py:1134-1152 只锁三个密钥串）
- **线4**：L16 同名多来源部分 digest 匹配时重复灌入（自愈型一次性，子代理已仿真复现）；L17 链式重定向 A→B→C 起始别名丢失、清单标题误报缺页（子代理已仿真复现，警告非失败）；L18 `mark_read_only` 硬编码两站名，SITES 扩表后新站降级串行（fail-closed 方向）；L19 dry-run 把「替换」计入 imported（计数口径）；L20 `--site foo` 未知 id 走 KeyError traceback（退出码合规，体验差）
- **线5**：L21 unit 渲染对含空格路径不加引号（fail-loud，建议安装时拒绝）；L22 `cmd_env --force` 备份权限声称 0600 实为继承旧文件；L23 check 的 `os.environ` 兜底与 systemd `EnvironmentFile` 部署语义偏差（shell export 假绿）；L24 GENERAL-GUIDELINES.md:4-6 行数快照失准（违反其自身「数字必须实测」）；L25 check_review_docs 解析边界三处（子目录 glob 不到、文件数只数一层、xfail 口径）；L26 test_repo_guards 正则假阴面（`from os import getenv` 别名/拼接 finish，当前零现实暴露）；L27 check_review_docs.py 自身零自动化回归（归档闸故障模式是误阻断/误放行）；L28 `cmd_install` 单独执行无 Python 版本预检（浪费一步，fail-loud）

## 2. 被证伪的发现（防止重走弯路）

1. **裸唤醒短路绕过 ACL**（线3）：证伪——`is_allowed` 在 build_payload 之前，黑名单静默 deny；短路在防抖回调内（N 条裸 @ 只回 1 句）、保留出站节流；PING_REPLY 硬编码不可注入。
2. **身份锚点排除句可被群名片拼接劫持**（线3）：基本证伪——排除句是独立 append 的固定文本；名片清洗同口径+折叠连串、24 字上限、无换行能力；残余 24 字祈使句面归 L12。
3. **wiki URL/SSRF 面**（线4）：证伪——来源 URL 全编码（7 个恶意标题仿真全转义）、无 path 拼接、站点白名单内置、handler 无 URL 参数、未开 follow_redirects。
4. **wiki 输出未过围栏**（线4）：证伪——引擎围栏经 `is_wiki_skill_name` 精确判定，内容行 `-----` 全被打散（仿真残留为空）；KB 检索路径自带围栏头尾。
5. **run_shell/run_command/log_tail/docker_logs 未过围栏**（线1）：证伪——skill 层确无，但引擎层 `_UNTRUSTED_TOOL_RESULTS` 四工具全在（上轮 H1 修复在位）；db_query 在 skill 层自围栏。
6. **fs.write 覆盖管理员 0755 脚本可保执行位**（线1）：证伪——`os.replace` 换 inode，执行位必丢。
7. **systemctl 粘合 flag `-aHevil` 绕过 -H 拒绝**（线1）：证伪——粘合放行只认取值型 key flag 开头，布尔开头整体拒（实跑拒绝）。
8. **check_stats 在 CI 用错口径**（线5）：证伪——`run_pytest_summary` 显式剥离 TEST_DATABASE_URL。
9. **点评卡片发给别的群 / upsert_daily `$7` 参数错位 / InMemory 昵称排序漂移 / 周报推非白名单群**（线2）：全部证伪，见子代理报告。
10. **存疑（未列问题）**：proc_kill pid 复用 TOCTOU（代码注释已声明取舍，防线齐全，产品决策）；`_MAX_SYMLINK_SCAN` 超限尽力而为（链接条目已在预扫描拦截，建议随 H1 统一口径）；「变异 N/N 被抓」类历史过程声称均无法重放，但抽查对应测试断言形态（tripwire/位置/精确集合/负例）均具「改坏必红」结构。

## 3. 测试与文档状况

**实跑数据**：
- 默认套件：**2322 passed / 58 skipped / 71 warnings**（45.2s，HEAD=a0a6bb4 干净树）
- 带 PG 套件（`TEST_DATABASE_URL=…qqagent_test`，真建表/TRUNCATE）：**2390 passed / 9 skipped**（56.1s）
- ruff：`check` + `format --check` 全绿，版本 0.9.6 与 pyproject pin 一致
- BACKLOG DOCS-STATS（67 文件 / 2380：2322+58，2026-10-12）与本轮实跑逐位一致

**CI 盲区**：GitHub API 匿名限流 + gh 未登录，**本轮无法独立核实 CI 运行状态**（上轮 M4 类断链检查同样受阻）。9364c99 的「修复 7 个失败」仅做到修法与 message 逐例相符（线5 已核），CI 全绿未能复核。

**文档一致性**：README（+274 行）/ .env.example（+122 行）/ BACKLOG / AGENTS.md 主体与实现一致（线5 交叉核对，含 9 个敏感键负面参数化测试）；例外见 M1（README「仅白名单群」）、M3（commit 声称）、M8（823e352 收敛目标）、L9（计数漂移）、L24（行数快照）。

**依赖安全**：新增 `jieba>=0.42.1`——MIT、未检索到已知 CVE、下限锁风格与全仓一致、缺失降级 bigram 非静默（import 期 WARNING）；`wikitextparser`（d14f011）未发现已知漏洞。零未锁定版本。

**未验证面清单（§8.3）**：
- (a) CI 运行状态（API 限流）——下轮销项，非安全盲区
- (b) systemd ReadWritePaths 首启失败（M10）为 systemd.exec 文档语义推演，未实机启动验证
- (c) H2/M2 的生产硬件体验幅度（本机实测为准；生产机 CPU 更弱可能更差）
- (d) jieba 对连字符 run 的切分行为（M3 实际可利用性）——不影响定级（防线错位本身成立）
- (e) 真机 QQ 通道/生产 LLM 行为——常规盲区，本轮无 H/M 落此

**门禁结论**：无环境盲区型 H；两个 H 均已本地复现且可小改修复。**H1/H2 修复+回归并补 CI 核实前，不宜发布**；M1 涉及隐私口径（空集 fail-open），建议与 H 同批优先处理。

## 4. 已验证为「无问题」的关键项（摘要）

- **权限三级制**：action/workspace/db/shell 注册期+handler 双层齐全、ops 五件套本轮补上 handler 门；low 档 run_command 拒 zip/unzip/tar/curl（端到端）；registry 拒绝与 unknown 精确同文案；`SUPERUSERS=*` 全链路拒绝（checker 剔除+env 精确匹配+yaml 无 permissions 段回归锁）；级别键不进 web 写面
- **run_shell**：最小环境逐项核过无密钥来源（测试实测 LLM_API_KEY 不进 bash env）、120s 杀进程组、审计 repr 防换行伪造、不在只读名单
- **env_snapshot/reboot**（a0a6bb4）：prime() 顺序写死、capture 幂等、execve 只带快照、无快照退化 execv、回执 0600；5+4 条回归结构扎实
- **群统计**：越权面声称属实（schema 无 user_id、registry 对含 user_id/group_id 签名无条件覆盖）；预算熔断不缓存；同周缓存零 LLM；周报 fail-closed+∩ACL+ASCII 校验+时区同源；停机顺序与幂等（每步独立容错）；store 双实现参数化契约 + 非正 limit 空结果 + JSONB 出库反序列化 + DDL 索引断言（PG 套件真库跑过）；「仅 PG」降级 WARNING 不静默；合并转发不展开/bot 自身不采集；字体复用既有文件零新增
- **适配层**：ping 判定保守（段类型白名单）；@占位剥离不受影响；身份锚点三项声称逐字吻合（含「----- 放中段防 strip 假通过」的测试修正）；poke WARNING 含会话键；web.py 未触碰 D2-1 写面且新增守卫测试；群统计响应无 api_key/base_url/正文
- **wiki**：单级重定向按最终标题入库（有测试）、sha256 判重+先写后删、限速/退避/2MB 截断/24h 缓存、停机回收挂 extra_closers、ingest extra_meta 透传；d14f011 声称逐项属实
- **部署**：unit「零密钥」逐字段属实（有负例断言）、不自动启动/不写仓库外两条硬边界在位、root 检查在位、check 输出不泄 DSN；deploy 18 例属实
- **db_backup**：part 0600+原子替换+sha256 sidecar、恢复 fail-loud+三道门、`_scan_pids` /proc 容错在位

## 5. 与在库报告的衔接复核

| 上轮（REVIEW-de09478..workdir） | 状态 |
|---|---|
| H1 四超管工具输出入围栏 | ✅ 真修在位（引擎层 `_UNTRUSTED_TOOL_RESULTS`，线1 证伪条目 5 复核） |
| M1 tar/unzip 解压产物执行位 | ✅ 修复在位且对正常包有效——但本轮发现**新旁路**（条目数上限 break 放行）→ 本轮 H1 |
| M2 fs.write 0600 / M5 容器名自保护 / M6 run_build_script tar 链关键节 / M10/M11 双时区 | ✅ 均在位（线1 核对） |
| 上轮未验证面 PG 套件 | ✅ 本轮实跑 2390 passed / 9 skipped，销项 |
| FIX-de09478..workdir §16 的 33 项复核（3a2708f 落库） | 抽验 L1 注释残留修复在位；其余与本轮线1 结论一致 |
| L 系（上轮 21 条） | 未逐条重审（本轮范围按增量原则从 1e0f5ae 起）；FIX 文档声称 L1–L21 全处置 |

## 6. 修复优先级建议

| 批次 | 项 | 说明 |
|---|---|---|
| P0（立即） | H1、H2 | 均为小改动：H1 超限改拒绝（含 `_MAX_SYMLINK_SCAN` 统一口径）；H2 三调用点 `to_thread`。各带回归+变异复核 |
| P1（同批） | M1、M9、M10 | M1 隐私口径对齐 fail-closed（行为变更记 BACKLOG）或 WARNING+README；M9 剥 export 前缀；M10 mkdir 或 `-` 前缀。都是部署/隐私正确性 |
| P2 | M3、M5、M6、M4 | 各一两行+用例：scrub、prune 退出码、conftest delenv 夹具、注册期过滤收进函数体 |
| P3 | M2、M7、M8 | jieba 预热；PGPASSWORD 改 stdin 或入档豁免（需管理员决策）；时钟六处收敛+守卫扩展 |
| L 批量关闭计划（§3 收敛规则：本轮 L=28>10） | 下轮 FIX 必修：L1（能力描述失真）、L7（docstring 隐私口径自相矛盾）、L10（停机 deadline）、L11（黑名单取舍说明）、L13（.part 降权）、L18（mark_read_only 名单生成化）、L22（权限声称）、L24（行数快照）。显式遗留入档：L4/L5（既有面延续，需管理员决策围栏名单扩容）、L12（产品决策）、L14（性能，视群规模）、L25–L28（工具链打磨）。其余 L 随邻近修复顺带关闭；连续两轮遗留的 L 按规范必须修复或「有意保留」入档 |

---

*子代理原始报告：线1（沙箱/权限/超管工具）、线2（群统计）、线3（适配层）、线4（wiki/RAG）、线5（工具链/部署/CI/文档），均按「发现/已验证无问题/被证伪存疑」三节产出，主代理逐条复核后裁决定级；复现脚本存 /tmp/opencode（repro_tar_h.py、repro_line2.py、repro_m1b.py）。*
