# agent-demo 近期 Commit 评审报告

**评审范围**：a0a6bb4..6745556（7 个 commit：45c429e 上轮逐条修复 / 066a69b 检索性能化 / a0d2a18 历史时间维度 / 1332f9d docker_stats+身份锚点 / c16fc54 日志清单 / 3be4411 成长层降级 / 6745556 文档）
**评审日期**：2026-10-15
**评审方式**：4 线并行子代理分线审查（线1 沙箱/权限/备份、线2 记忆/存储双实现、线3 loop/人格注入/QQ 适配、线4 测试/文档/声称核对）+ 主代理逐条实证（实跑三套测试与 lint、复现 H、证伪存疑、服务器侧销项）。收线说明：4 线均在评审窗口内回传最终报告（其间因墙钟超限被主代理两次收线指令并中断一次）；四线全部结论为静态推演级，均经主代理逐条复裁定后采信/升级/证伪。
**工作区状态**：评审全程受版本控制文件零改动；变异复核的临时篡改以精确反向编辑还原（还原后 git diff 为空、全量用例复绿）；评审产物仅本报告与 REVIEW-WORKFLOW.md §9 索引行。

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 最高风险 | **H1**（已复现）：白名单内 tar --strip-components / unzip -j 使「解压后按条目表清执行位」防线失配，重组「curl 拉包→解压→run_build_script 执行（继承完整环境）」链——上轮 H1 修复声称封死的链路经由更简单的门保持畅通（详见 §1/§5） |
| M | 0 条 |
| L | 16 条（跨 4 主题，单主题最多 5 条，无失控主题；>10 条触发批量关闭计划，见 §1 末） |
| 测试（主代理实跑） | 默认套件 **2447 passed / 58 skipped**（49s）；**全新空库 qqagent_test（CI 等价）PG 全量 2515 passed / 9 skipped**（60s）；RUN_PERF=1 9 passed；perf_baseline.py 与基线比对**无劣化** |
| Lint | ruff check + ruff format --check 双绿（0.9.6，与 pyproject pin 一致） |
| CI | **#102（HEAD 6745556）success**（GitHub API 实查，#100/#101 亦绿）——上轮未验证面 (a) 销项 |
| 依赖 | pyproject/lock 零 diff，零新增 |
| 声称核对 | 7/7 commit 声称与实现一致、自测数字与实测完全一致，无夸大；唯 45c429e 的 H1「封链」声称按本轮 H1 口径记不符（见 §5） |
| 门禁（§8.3） | **H1 修复（含回归负例）并复验前，不得发布**；主干已含该代码，发布门不因已合并而豁免。其余不阻断 |

## 1. 已实证的问题

### H1｜【已复现】tar --strip-components / unzip -j（均在白名单内）使条目表执行位清理完全失配——「拉包→解压→执行」链未闭合

**位置**：agentcore/workspace/runner.py:229（_TAR_SAFE_FLAG_KEYS 含 --strip-components）、:206（_UNZIP_SAFE_FLAGS 含 -j）、:1271-1287（strip_entry_exec_bits 按**扫描期条目名**定位）、:1493-1498（清理以条目表为唯一依据）。
**来源**：线1 子代理发现（【推演】，置信 high），主代理实包复现升级定案。

**复现**（真实 tar/unzip 落盘，工作区内临时目录；攻击包仅含一个 0777 的 pad1/pad2/evil.sh）：

```
TAR-ATTACK   tar --strip-components=2 -xf evil.tar -C out1
  → out1/evil.sh   = 0755   ← 执行位存活（清理按 pad1/pad2/evil.sh 找不到文件）
TAR-CONTROL  tar -xf evil.tar -C out2
  → out2/pad1/pad2/evil.sh = 0644  ← 对照组清理正常
ZIP-ATTACK   unzip -j evil.zip -d out3
  → out3/evil2.sh  = 0777   ← 清理完全未触及
ZIP-CONTROL  unzip evil.zip -d out4
  → out4/pad1/pad2/evil2.sh = 0666 ← 对照组清理正常
```

**影响**：strip_entry_exec_bits 是 AGENTS.md §4 明文声称的「真实防线」（REVIEW-de09478..workdir M1），对两个白名单内 flag 完全失效。展平落盘的 0755 脚本可直接落进 <ws>/scripts/，被 run_build_script（medium+ 注册、superuser handler）以**完整进程环境**执行（action_skills.py:441-468）——与上轮 H1 同链同前提（medium 档 run_command + superuser + 注入诱导）。**按在库先例定 H**（REVIEW-1e0f5ae..a0a6bb4 的 H1 即同链经扫描阈值旁路，定级 H）。
**性质注记**：涉事白名单与清理逻辑均为**既有代码**（非本区间引入）；但本区间 45c429e 的 H1 修复以「统一『超限即拒』口径」声称封死该链，实际链路经本门保持畅通——属修复未达声称目标，衔接复核见 §5。
**修复建议（最小）**：把 --strip-components 与 unzip -j 移出白名单（非必需能力，一行收缩 + 8 形态负例用例）；或出现展平 flag 时放弃条目表、改对输出目录做目录级执行位清理（需解决「宿主预置 scripts/*.sh 执行位必须保留」的冲突），或对含执行位条目的包直接拒绝。修复后必须以本节复现脚本翻转验证（0755/0777 → 不可复现）。

### L（16 条，压缩列出）

| # | 位置 | 一句话 |
|---|---|---|
| L1 | plugins/qq_agent_adapter/__init__.py:312 | jieba 预热 create_task 未持强引用（违背本仓 growth._spawn/debounce 既有纪律），GC 回收即静默退回首条消息 ~0.8s 事件循环冻结（45c429e-M2 目标偶发失效）。主代理与线3 独立发现，收敛 |
| L2 | agentcore/workspace/runner.py:232 | 注释仍写「超限告警放行」，与 H1 新行为（超限即拒）相悖，防照旧注释改回 break（同文件 :405 已改） |
| L3 | agentcore/skills/action_skills.py:49 | _CONTAINER_RE 允许首字符 -，--format/-a 形态容器名可入 argv；后果仅 flag 歧义报错（无 shell、无写面、围栏在），不构成逃逸；建议首字符锚定字母数字 |
| L4 | agentcore/personas/growth.py:96 | GrowthManager.aclose 全仓无调用方（lifecycle closers 只含 embedding/media），docstring 声称的「lifecycle 编排调用」不存在；在飞 _propose 停机时以 Task destroyed 收场 |
| L5 | plugins/qq_agent_adapter/pipeline.py:753 + engine.py:526 | 「你自己」引用标注在 get_msg/raw_message 兜底路径系统性缺失（reply_obj 为 None 时无 sender 可判），engine 对模型的承诺「引用围栏头部会注明」在该路径不成立（措辞过头）；漏标方向符合 fail-closed 取向 |
| L6 | plugins/qq_agent_adapter/matcher.py:168 | AGENT_DEBUG_RAW_DUMP 开启后每条群消息**全文**落 INFO 日志（超出既有 200 字日志卫生口径，/api/logs 原样展示）；默认关+显式 env+仅群聊缓解 |
| L7 | agentcore/loop/engine.py:663 + tests/test_store_contract.py:418 | stale 判定跨时钟源（应用机 time.time() − PG created_at/DB 机时钟）未注记；契约测试断言「ts 单调不减」强于双实现真实保证（NTP 步回/事务起点 vs id 序），理论 flake 点 |
| L8 | agentcore/skills/builtin.py:134-139 | low 档启动日志自相矛盾：「docker_ps, docker_logs, docker_stats（low 档：动作类不注册）」——同行既列已注册三件又宣称不注册；本区间两次编辑该行均未纠正后缀 |
| L9 | agentcore/skills/builtin.py（无测试） | 启动日志技能清单硬编码串仍无守卫测试；本区间该串已实际漂移过一次（1332f9d 漏改、c16fc54 补） |
| L10 | tests/conftest.py:62/:108 | 防误连 guard（定义序先于 load_dotenv）对 **.env 来源**的 DATABASE_URL 失明（比对退化恒过，仅 shell export 值被比对）；新增剥离夹具 docstring「破坏性检查仍能读到生产值完成比对」与机制不符。失明在区间前即存在（顺序未变），docstring 为本区间新增 |
| L11 | docs/GENERAL-GUIDELINES.md:4-8 | 行数快照称 REVIEW-WORKFLOW.md 232 行，45c429e 时点实测已 233（自违「数字必须实测」规则；本报告归档后行数再 +1，修复时一并更新） |
| L12 | tests/test_shell_skill.py:81-82 | 双重 @pytest.mark.asyncio 装饰器（本区间引入，无害噪音） |
| L13 | agentcore/workspace/runner.py:1427/1466 | 不可达防御分支（out_dir==root 保留现场）在 removed<0 判断前 return，若达会渲染「含符号链接（-1 个）」；permitted 已拒 root，仅文案瑕疵 |
| L14 | agentcore/memory/store.py:191-198 | _pair_sim docstring 称「契约测试锁定」，实际零模/维度边界仅内存侧用例锁定，且 threshold=0.0 时与 PG 天然分歧（pgvector 零向量距离 NaN→排除 vs 内存 0.0 判入）——「双实现一致」表述名不副实 |
| L15 | agentcore/memory/store.py:948-951 | kb_search「快照」注释失实：只快照 chunks，sources 仍传活引用（GIL 点读原子、良性读偏，无正确性问题）；「kb_add_chunks 重建列表」描述也与实现不符（append；重建在 kb_delete_source） |
| L16 | agentcore/loop/engine.py:846-852 | embedding 故障路径单轮 embed 尝试 2→3 次（预 embed 失败后 facts/KB 各自回退再嵌），死端点上前奏 gather 多一轮完整重试延迟与异常日志；结果语义不变 |

**L 批量关闭计划（本轮 16 条 > 10，按 §3 收敛规则给出）**：
- **必修（12，均为小改）**：L1（存强引用+done_callback，两行）、L2（注释改写）、L7（注释注记 + 契约断言放宽为「存在且>0」）、L8（后缀改「low 档：仅只读三件，控制类未注册」）、L9（守卫测试一条：注册集 vs 日志串比对）、L10（docstring 修正 + guard 挪到 load_dotenv 之后或直接解析 .env 比对）、L11（改 233，随本报告归档更新）、L12（删重复装饰器）、L13（调整判断顺序）、L14（docstring 改「仅内存侧锁定，与 PG 有意分歧：NaN→排除」）、L15（sources = dict(...) 或注释如实化）、L16（失败哨兵或注释说明）。
- **显式遗留（4，需接线/产品决策或触发面低，理由入档）**：L3（行为无害，随下次 runner/skills 触碰顺带收紧）、L4（lifecycle 接线属停机语义决策，需与 scheduler/flush 顺序一并考虑）、L5（需 resolve_quoted_media 扩展返回 sender，跨模块接口改动）、L6（调试开关的限长取舍属管理员可视性决策，随下一次日志卫生整理处理）。

## 2. 被证伪的发现（防误导后续评审）

1. 「await deny( 后无 return 会 fall-through 越权回复」→ 证伪：acl.deny 两条路径均以 matcher.finish 抛 FinishedException（acl.py:120-138），且该分支经 rule 层已不可达。
2. 「成长层仍紧贴人格段注入」→ 证伪：实际拼接顺序人格→facts→summary→timeline→群/私聊边界→knowledge→growth→回锚（engine.py:395-508），并有 TestGrowthDoesNotDilutePersona 锁位。
3. 「内部 ts/id 会泄进 LLM messages」→ 证伪：timeline 在 _project_history 之前计算（engine.py:824-828），投影只留 role/content/条件 tool 字段，测试锁 id/ts 双不外发。
4. 「clear_persona_growth 双实现语义分叉（空串记录）」→ 证伪：InMemory pop(...) is not None 与 PG WHERE persona_growth IS NOT NULL 对空串同样返回 True，四情形逐字对齐（线2 ②-6 与主代理独立一致）。
5. 「zip 侧 namelist() 无上限 = H1 同型扫描缺口」→ 证伪：namelist 全量返回无部分窗口；残余仅病态大包内存峰值（medium+ 可达的 DoS 面，建议后续对齐上限，本轮不列为发现）。主代理曾怀疑新增注释「与 tar 侧超限即拒同口径」措辞失实，裁定为易误读但非错误（指「全量扫描」口径），并入观察不单列。
6. 「tar -f -（stdin 包）绕过预扫描」→ 证伪：- 解析为 root 下不存在路径，tarfile.open 抛错放行 spawn 后 tar 从继承 stdin 读 EOF 报错；管道符被全局拒绝，无法喂入。
7. 「M7 stdin 注入脚本可经日志/异常泄漏密码」→ 证伪：脚本不入日志，stderr 尾部来自 pg_dump/psql。
8. 「except TimeoutError 在 3.10 漏捕」→ 证伪：requires-python>=3.11，asyncio.TimeoutError 即内建别名。
9. 「timeline 警示文本含用户可控内容需过围栏」→ 证伪：块内仅 gap 人话 + tz 定长时间戳，无用户可控字符。
10. 「摘要水位口径回归上轮 H1（区间取数降序致旧消息永久丢）」→ 证伪：get_session_messages_between 双实现均升序，fed=lines[:200] 取最旧、fed[-1][0] 即真实喂入末条。
11. 「web 写面被扩成第 8 键破坏『白名单 7 键』不变量」→ 证伪：history_stale_after 只进只读总览键表，config_write.py 未动。
12. 「句子边界截断会把两段式记录整段砍掉/末句切半」→ 证伪：0.7 下限设计正确（具体字符串推演：孤立早换行被拒、恰硬截 limit 字）。
13. 「/growth 查看他人构成越权」→ 证伪：整条命令（含查看）superuser-only，比宽松读法更严。
14. 「neutralize_fence_lookalikes 双重应用（写入侧+引擎侧）可能破坏文本」→ 主代理实证：幂等（5/5 样本二次应用 no-op，含已打散输入）。
15. 「debounce 合并丢弃 self_id 致合并轮次锚点缺失」→ 证伪：_answer 以 parts[0] 为 base payload（matcher.py:318/334/345），self_id 保留。
16. 「c16fc54 后清单仍与注册集不一致」「新 env 键行内注释被 dotenv 当值」「check_review_docs 与 AGENTS §3/§6 声称不符」「BACKLOG 计数块自相矛盾（M5 类）」「区间新开 test_review_* 批次文件」「memory-and-persona.md §2 组装顺序与 engine 漂移」→ 均证伪（线4 ②-3/②-7/②-6/②-5/②-8 与主代理互证）。
17. 「PG 侧 clear_persona_growth 是否真带 IS NOT NULL（线4 存疑④）」→ 主代理亲读 store.py:1516-1528 确认在位，契约测试四断言锁定，销项。
18. 「三个手工 scratch 库未清理 / 蒸馏备份不存在（线4 存疑②）」→ 主代理服务器侧实证：pg_database 仅 postgres/qqagent/qqagent_test/template*；data/backups/persona_growth-20261015-223903.jsonl（1960B, 22:39）在盘。**销项**。

## 3. 测试与文档状况

- **实跑**：默认 2447+58；PG 全量（DROP/CREATE 重建的全新空库 qqagent_test，等价 CI 起点）2515+9，其中契约测试双侧全过（含 clear_persona_growth 四断言与 ts 契约）；RUN_PERF 9 passed；perf_baseline 与 perf/baseline.json 比对**无劣化**（1.56x 项为 0.088s→0.137s，低于 5ms 噪声下限，不判劣化）。ruff 双绿。
- **CI**：#102 (HEAD) success、#100/#101 success（GitHub API 实查）。**上轮未验证面 (a) 销项**。
- **文档一致性**：README/AGENTS.md §3/§6/BACKLOG §6.7/§6.8/.env.example/config.yaml/docs/memory-and-persona.md 与实现逐项一致（线4 全量对码 + 主代理抽验）；BACKLOG DOCS-STATS 块与实测一致（2505=2447+58）；check_review_docs.py 归档闸 [ok]。仅 L11 一处行数快照差 1。
- **测试质量**：四线合计抽查 40+ 新用例，均有正例+反例+对照例结构，未发现恒真断言/只测默认值/mock 掉被测逻辑；零新增测试文件、无 test_review_* 批次（符合 §2 归并纪律）；test_repo_guards 防恒真结构（探测器投毒+真树零命中）是加分项。
- **未验证面**（§8.3 门禁口径）：
  - (a) 各 commit 自称的变异复核共 31 处——主代理亲验 2 处（tar 超限→break：2 failed；strip_symlinks→break：1 failed，均被抓死、还原后复绿），其余信其在 FIX 台账的记录（过程声称，无法重放）。**降级记录**。
  - (b) 真机 QQ 通道行为与生产库实际效果（stale 警示观感、人格稀释改善度、蒸馏后人格表现）——环境盲区，评审不触生产库。**降级记录**。
  - (c) 066a69b 的绝对性能数字（1.66s→0.67s 等）未复测——perf 套件与基线比对已覆盖「无劣化」，绝对值为单机实测口径。**降级记录**。
  - **门禁结论：H1 存在 → 阻断发布直至修复并复验；无未声明的环境盲区 H/M。**

## 4. 已验证为「无问题」的关键项（四线 + 主代理合并）

1. 上轮 **H1 修复本身**为真修且入口唯一（_tar_scan 超限 return 拒绝、strip_symlinks 返回 -1、两调用点丢弃输出、超限拒绝早于解压、薄包装无第二入口）；主代理变异复核 2/2 被抓死。**但其意图封死的链路经本轮 H1 的另一扇门仍畅通，见 §5。**
2. 上轮 **H2** 为真修无漏网：三处 to_thread + 全仓渲染调用点清点（poke/admin/outbound 均已覆盖）+ 行为级用例。
3. 上轮 **M1–M10 全部真修**（主代理逐项亲验：空白名单 fail-closed 三处、jieba 预热、话题词行中 scrub、注册期级别门收进函数体、prune_failed 退出码、conftest 剥离夹具、pg_dump stdin 注入〔restore 豁免入档+测试锁〕、时钟收敛+AST 守卫、deploy dotenv 口径、mkdir data）。
4. 本区间新功能面：docker_stats（--no-stream 写死/--all 互斥/容器名校验/low 注册+superuser 二次闸/围栏+只读双名单/日志串同步）；self_id 透传链（pipeline:692→matcher:398→engine 唯一调用点，ASCII+isdigit 双门 fail-closed，错标方向堵死）；stale 时间线（口径成立/0 关闭/无 ts 不猜/无用户可控字符）；成长层降级（位置/主权声明/回锚/禁降格 prompt/截断边界）；/growth（双层 ACL、ASCII 数字、FinishedException 不被吞、clear 幂等不谎报）；store 双实现 ts/clear/模长预计算/to_thread 快照（含「无回填致模长过期」「无撕裂迭代」「六处非正 limit 守卫」核查）；lifecycle 停机顺序未动+新 flush deadline（except TimeoutError 于 3.11 成立）；web 只读键不动写面。
5. 声称管理：7 commit 的自测数字与主代理实跑**逐字一致**（2447+58 / 2515+9 / 2375+58 / 2397+58 等），逐 commit 测试增量可复算（+37/+10/+6/+22），无夸大。

## 5. 与在库报告的衔接复核

上轮 [REVIEW-1e0f5ae..a0a6bb4.md](REVIEW-1e0f5ae..a0a6bb4.md)（2H/10M/28L）→ [FIX-1e0f5ae..a0a6bb4.md](FIX-1e0f5ae..a0a6bb4.md)：

| 上轮项 | 本轮复核结论 |
|---|---|
| H1 tar 扫描超限放行 | **修复为真**（拒绝+(-1) 语义+变异 2/2 抓死）；**但其意图封死的执行链经 --strip-components / unzip -j 仍可达 → 本轮 H1**（新旁路，非回归：涉事 flag 均为既有白名单成员，上轮 H1 只修了扫描超限面） |
| H2 渲染阻塞 | 真修，无漏网调用点 |
| M1–M10 | 全部真修在位（明细见 §4.3；M7 的 restore 路径 argv 密码驻留为入档豁免，声称与实现一致） |
| L 必修 8+顺带 5 | 修复在位（fchmod 0600、只读名单站点表生成、sync_wiki 未知站点 exit 2、cmd_env chmod 0600 等）；其余 L 显式遗留入档，符合收敛规则 |

索引链检查：上轮报告已入 §9 索引（REVIEW-1e0f5ae..a0a6bb4.md），增量链连续；本报告归档后续接。

## 6. 修复优先级建议

1. **立即**：H1——白名单收缩（--strip-components、unzip -j 移出）+ 8 形态负例 + 本报告 §1 复现脚本翻转验收；修复记录入 FIX-a0a6bb4..6745556.md 并引用本编号。
2. **同批（L 必修 12 条）**：见 §1 批量关闭计划；L10 的 guard 顺序修正建议与 L2/L14/L15 注释类合一个 PR，逐项变异复核。
3. **择机（遗留 4 条）**：L3/L4/L5/L6 按入档理由排期；L4（aclose 接线）建议与下次 lifecycle 触碰同批。
4. 修复完成后照例：全量 pytest + ruff + PG 套件 + check_review_docs.py --check-stats。
