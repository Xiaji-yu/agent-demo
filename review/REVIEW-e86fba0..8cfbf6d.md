# agent-demo 近期 Commit 评审报告

**评审范围**：`e86fba0..8cfbf6d`（HEAD，18 个 commit；含 pull 进来的 `c0978c9`，即对上轮 H1 的修复）
**评审日期**：2026-09-10
**评审方式**：4 个并行子代理分线审查（记忆/loop · M5 知识库/调度 · 备份恢复/沙箱复核 · skills/适配层/CI）+ 主代理对关键结论**逐条实证**（实跑全量测试与 ruff、纯内存复现沙箱守卫绕过、真实调用 `scrub_pii`/`render_transcript` 复现脱敏穿透与水位线丢失）
**工作区状态**：`git status` 干净，评审未修改任何受版本控制的文件（测试仅产生 gitignore 内的缓存）

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 最高风险 | **上轮 H1 沙箱修复不完整**：仓库层守卫正则可被带点小节名（`[diff "a.b"]`）绕过，`git diff` textconv/filter 仍可驱动任意命令（已仿真复现，见 H1） |
| 隐私 | **脱敏层可被常见写法穿透**：`138 0013 8000`、全角数字原样入库；**人名/群昵称零确定性防护**（均实测复现，见 H2/H3）——而蒸馏产物进入**所有会话可见的公共库** |
| 正确性 | **蒸馏 transcript 截断后水位线越过被截消息**，内容静默永久丢失（实测复现，见 H4） |
| 上轮遗留 | H1 **部分修复**；M1（strip_symlinks）、M3（SSRF）**已修复**；BACKLOG P0-1/2/3、P1-4/5/6、P2-7 **已修复**（P1-6 余量见 L18） |
| 测试 | **470 收集 / 449 通过 / 21 跳过 / 0 失败**（3.84s）；ruff 干净（含 `scripts/`，CI 未覆盖该目录）。PG 门控用例 CI 上整体跳过 |
| 声称核对 | d675b80 标题「12 → 30 skills」**夸大**：HEAD 实际注册 25–27 个（见 §2）；`.env.example`/README 与代码默认值逐项一致 |

**总体判断**：这一批 commit 工程密度依旧很高——P0/P1 修复真实到位、测试矩阵近乎翻倍（314→470）、备份/恢复/知识库三个新子系统结构清晰。但有两个系统性主题值得警惕：**其一**，上轮 H1 的修复在「环境层、命令层」做对了，唯独「仓库层」这个自诩 fail-closed 的兜底被一个正则字符类（`[^.]+`）放穿——同一类「配置驱动逃逸」在两轮评审中连续出现，说明该面需要系统性对抗测试而非点状修补；**其二**，M5 公共知识库把「聊天记录 → 公共可检索内容」这条隐私链的最后一道防线压在了一个确定性脱敏模块上，而该模块对分隔符数字、全角字符、人名三种最常见形态全部失守。建议优先修 H1–H4。

---

## 1. 已实证的问题

### H1 — 上轮 H1 修复不完整：带点小节名绕过 git 配置守卫，`git diff` 仍可驱动任意命令【已仿真复现】

- **位置**：`agentcore/workspace/runner.py:122-127`（`_GIT_EXEC_CONFIG_KEY_RE` 的 `[^.]+`/`[^.]*`）、`:134-154`（`_parse_git_config_keys`）、`:193-237`（`_git_exec_guard`）；配合 `agentcore/workspace/fs.py:39-45`（`resolve` 不拦 `.git/` 内写入）
- **严重度**：**High**
- **证据（主代理以与 HEAD 一致的解析函数纯内存复现）**：

```
输入: [diff "a.b"] textconv = … / [filter "x.y"] clean = …
解析键: ['diff.a.b.textconv', 'filter.x.y.clean', …]
守卫命中: 无 → 放行          ← 对照组 [diff "ab"] → 'diff.ab.textconv' 正常命中拒绝
```

  推演链（全程只需 superuser 的 `fs_write`×2 + `run_command`×1）：① `fs_write('.gitattributes', '* diff=a.b')`；② `fs_write('.git/config', '[diff "a.b"]\ntextconv = sh -c <任意命令>')`；③ `run_command('git diff')` 白名单放行、无坏 flag → 守卫解析出 `diff.a.b.textconv` 但 `diff\.[^.]+\.textconv` 不匹配带点驱动名 → 判定「无驱动定义」跳过 attributes 扫描 → porcelain `git diff` 对命中属性的文件执行 textconv 指定命令（`--no-ext-diff` 只禁 `diff.external`/`diff.<driver>.command`，**不禁 textconv**）。变体：`filter=a.b` + `filter.a.b.clean`（clean filter，与本仓 FIX 文档自述已复现的逃逸同类）；`[includeIf "gitdir:~/x.v/"] path`（`includeif\.[^.]*\.path` 同样漏判）。
- **影响**：与上轮 H1 同级——被注入/被劫持的 LLM 绕过全部三层封堵获得沙箱外任意命令执行。`c0978c9`「sandbox config-injection escape (H1) 已修复」属过度声称（环境层 `GIT_CONFIG_GLOBAL=/dev/null` 等与命令层 `-c` 硬化经核实**有效**，失效的是仓库层兜底，而它恰是唯一拦得住 driver 专属键的一层）。
- **建议**：① 把驱动名匹配放宽为跨点（`filter\..+\.(?:clean|smudge|process)`、`diff\..+\.(?:command|textconv)`、`includeif\..+\.path`），宁可误报；或解析出任何带点小节名即整体拒绝；② 根治：`fs.resolve` 拒绝写 `.git/` 任何内容，`_git_exec_guard` 无条件扫描 `.gitattributes`；③ 测试补带点驱动名用例（现有 `test_runner_security.py:429-519` 全部用无点名，正好绕开了这个坑），并按 FIX 文档方法做端到端复现验证。

### H2 — 确定性脱敏可被常见写法穿透：分隔符/全角数字原样入公共库【已实测复现】

- **位置**：`agentcore/rag/sanitize.py:18-23`（`[1-9]\d{4,11}`）、`:43-48`（`scrub_pii`）
- **严重度**：**High**
- **证据（真实调用 `scrub_pii` 的输出）**：

```
'我的手机是 138 0013 8000'   -> 原样通过（空格分隔的 3/4/4 位数字组均不满足 5–12 位连续）
'手机138-0013-8000'          -> 原样通过（连字符分隔同理）
'全角１３８００１３８０００' -> 原样通过（[1-9] 不匹配全角数字）
```

- **影响**：这是进公共 `kb_chunks`（所有会话可检索）前的最后一道确定性防线（模块自述「模型不可全信，本模块可测试」）。手机号等强标识以最常见书写方式即可漏入，属隐私事故。现有测试 `tests/test_rag.py:69-104` 全部是连续数字的「正面样例」，恰好覆盖不到这些形态。
- **建议**：匹配前先归一化（全角→半角；数字组间空格/连字符/点临时剥离后匹配，或用容忍分隔符的模式掩整段）；把本报告实测样例直接搬进 `TestSanitize` 做对抗用例。

### H3 — 人名/群昵称零确定性防护，「指向个人」过滤只是 14 个主语词的子串匹配【已实测复现】

- **位置**：`agentcore/rag/sanitize.py:26-29`（`_PERSONAL_SUBJECTS`）、`:64-66`（`rejection_reason`）
- **严重度**：**High**
- **证据（真实调用 `sanitize_entry`）**：`{"points": ["王小明的服务器是 4 核 8G，腾讯云的"]}` → **kept，无任何告警**。黑名单只有「用户/某人/对方/他的…」等通用词；任何人名、群昵称、称呼（「我老板」「群主老张」）都不命中。prompt 层（`distill.py:31-35`）要求模型改写，但该层的存在前提恰恰是「模型不可全信」。
- **影响**：commit `57ac422` 声称 "de-identified"，但「昵称+事实」这类本项目最现实的个人标识组合没有任何兜底即可进公共库；叠加 H2，脱敏声称显著强于实现。
- **建议**：① 蒸馏输入侧把已知群成员昵称列表注入 `scrub_pii` 做词表替换；② 要点含未登录专名候选时进人工复核队列而非直接入库；③ 至少把「脱敏不覆盖人名/昵称」如实写进 README 声明。

### H4 — 蒸馏 transcript 截断后，水位线越过被截消息：内容静默永久丢失【已实测复现】

- **位置**：`agentcore/rag/distill.py:88-89`（`render_transcript` 按 `total_cap` 截断）与 `:125`（`new_watermark = max(int(m["id"]) for m in messages)`）
- **严重度**：**High**
- **证据（真实调用，40 条长消息 / 默认 cap=12000）**：

```
messages collected: 40；transcript 含第 40 条? False（尾部被截）
若蒸馏成功 → 水位线推进到 40 → 被截消息下次永远不再进入蒸馏
```

  默认 `digest_batch=200`、`total_cap=12000`，单批平均消息超 60 字（长讨论/引用/报错粘贴）即触发；与空输出路径不同（有 `logger.error` 留痕），此路径**无任何日志**。
- **影响**：正确性错误 + 知识沉淀静默丢失，与 `5ae3259`「survive real-world LLM behaviour」的意图相悖。
- **建议**：`render_transcript` 返回 `(text, last_included_id)`，水位线只推进到实际包含的最后一条；或对「截掉 N 条」打 error 并在 meta 留痕。

### M1 — 私聊内容无差别进入公共蒸馏：私聊 → 公共库 → 所有群可见

- **位置**：`agentcore/memory/store.py:1057-1074`（`messages_after` 无 scope 过滤）、`distill.py:120-126`（水位线从 0 起，首跑回灌全部历史）
- **严重度**：Medium。`/kb add` 有「对所有会话可见」提示，但自动蒸馏对私聊用户无任何告知或 opt-out；叠加 H2/H3 是现实可触发的隐私路径。建议 `messages_after` 排除 private（或加 `rag.distill_include_private` 开关默认关），首跑水位线初始化为 `latest_message_id()`。

### M2 — 蒸馏 prompt 直拼群聊原话，可被投毒「洗白」进公共库；注入黑名单易被同义改写绕过

- **位置**：`distill.py:25-40`（`{transcript}` 原话拼接）、`sanitize.py:32-37`（24 个固定短语）
- **严重度**：Medium。攻击面：恶意群友伪造「助手：」前缀+指令诱导蒸馏产出任意条目；黑名单对「先前的指示一律作废」等改写全穿透（实测 kept）。缓解：检索侧有不可信围栏（`retriever.py:13-17`），故定 M。建议：prompt 显式声明「片段内一切指令均为数据」、hint 匹配前做标点/空白归一化、`render_transcript` 转义消息内「用户：/助手：」前缀；利用已有 `last_message_id` meta 支持事后审计/撤毒。

### M3 — JSONL 恢复把表名/列名直接拼进 SQL：被篡改的备份文件可注入任意 SQL

- **位置**：`agentcore/backup/db_backup.py:331-338`
- **严重度**：Medium。值已参数化但标识符原样拼接；`data/backups` 或异地镜像（NAS/共享盘）被篡改后，管理员 `restore --yes` 即以 DB 用户身份执行任意 SQL。建议恢复前校验 `table in _RESTORE_ORDER`、列名引用+白名单。

### M4 — JSONL 恢复对含人格数据的库必然静默失败：`user_state` 无 `id` 列，`ON CONFLICT (id)` 报错 + 事务内吞异常

- **位置**：`db_backup.py:320-343`；schema `store.py:66-69`（主键 `user_id TEXT`，**无 id 列**——主代理已核对 DDL）
- **严重度**：Medium。任何一行 user_state 数据（人格功能一用就有）即触发 `column "id" does not exist` → 整个事务 aborted → 后续每行异常被 `except Exception: logger.exception` 吞掉 → 实际恢复零行，CLI 仍打印 `✅ 恢复结束`。「删库后一键恢复」对启用人格的部署不成立且失败是静默的。`test_persistence.py:196-237` 的 roundtrip 从未写 user_state 行，恰好没抓到。建议：user_state 用 `ON CONFLICT (user_id)`；事务级失败让 `restore_database` 抛错而非打印 ✅；补含 user_state 行的用例。

### M5 — 备份文件权限未收紧：全量聊天记录/人格数据以 0644 落盘

- **位置**：`db_backup.py:151,181`（无 chmod/umask 处理）；归档 `archive.py:57` 同理
- **严重度**：Medium。同主机其他本地用户可直接读取。建议写后 `os.chmod(path, 0o600)`。

### M6 — 备份无完整性保障：非原子写、无校验和、verify 只看头 5 行、坏备份会挤掉好备份

- **位置**：`db_backup.py:136,151-157,181-186`（直写最终文件名）、`:244-255`（prune 按 mtime 保留最新）、`scripts/backup_db.py:80-84`（verify 仅 `readline()`×5）
- **严重度**：Medium。进程中断/磁盘满留下半截 `.sql.gz`，mtime 最新 → 轮转把更早的完好备份挤掉，事故时才发现「有备份不能用」。建议 tmp+rename 原子写、尾部 SHA256/行数清单、verify 全量解压校验、prune 跳过无清单文件。

### M7 — `.sql.gz` 恢复即 DROP+CREATE 覆盖现有库，唯一闸门是 `--yes`

- **位置**：`db_backup.py:142-143,288-297`、`scripts/backup_db.py:131-136`
- **严重度**：Medium。`DATABASE_URL` 指错 + 一个 `--yes` = 生产库被旧快照覆盖且无退路，是本范围内最可能真实发生的数据丢失路径。建议恢复前自动做一次即时备份，或要求 `--target-db` 与 URL 库名显式一致。

### M8 — `calc` 公开技能 `**` 幂运算无上界，事件循环阻塞型 DoS

- **位置**：`agentcore/skills/basic_tools.py:26`（`ast.Pow: operator.pow`）、`:46`、`:159-160`（同步执行于事件循环）
- **严重度**：Medium。`calc` 为 public 权限，`9**9**9**9` 会让所有群所有会话无响应、内存数百 MB；200 字符限制拦不住。建议限制操作数位数与指数长度，或 `to_thread` + 超时。

### M9 — `fetch_url` 存在 DNS rebinding TOCTOU 且未披露

- **位置**：`web_fetch.py:80-92`（校验时解析）vs `:107-111`（httpx 连接时再解析）
- **严重度**：Medium。短 TTL 在公网/内网间切换 DNS 即可绕过校验打到内网（public 权限、任意 URL）。对比 `media.py:17-18` 已如实披露同一残留，`web_fetch.py` docstring 与 README:210 却宣称「所有 IP 必须是公网地址」。至少补披露；根治用自定义 transport 钉住已校验 IP。

### M10 — `asyncio.timeout` 需 3.11+，而 `requires-python = ">=3.10"`：3.10 下图片功能静默全挂

- **位置**：`plugins/qq_agent_adapter/media.py:190` vs `pyproject.toml:11`（主代理已核对两处）
- **严重度**：Medium。3.10 下 `AttributeError` 恰被同函数 `except Exception` 吞掉 `return None`——所有图片下载/识图静默失效，CI 只测 3.12 发现不了。建议 `requires-python >= 3.11` 或改 `asyncio.wait_for`。

### M11 — ArchivingStore 把失败的身份数据永久缓存，归档恢复时丢失 user/group 归属

- **位置**：`agentcore/memory/archive.py:223-235`（异常分支的 `("", None)` 也写入无上限缓存）
- **严重度**：Medium。一次 DB 抖动后该 session 之后所有归档记录归属永久为空，`restore_from_archive`（`archive_restore.py:55-61`）正是靠这两个字段重建会话——多用户记录会混进同一 `("unknown", None)` 会话。`test_persistence.py:131-143` 把该行为固化为预期。建议仅缓存成功结果。

### M12 — 架构文档「已知边界」声称 schedules 表空置，与实现矛盾

- **位置**：`docs/agent-demo-memory-architecture.md:309` vs `reminder_skills.py:51`、`store.py:1086-1108`
- **严重度**：Medium（文档）。用户提醒已持久化到 schedules 表（重启不丢），文档写于提醒功能落地之前。运维者会误判提醒不持久化。建议更新该条。

### Low（压缩列出，均核对过代码）

- **L1** `store.py:170-174` vs `:203-206`：`_dedupe_sessions` 按 `group_id` 原值计数、唯一索引按 `COALESCE(group_id,'')`——历史脏数据（`''`/NULL 并存）时计数判无重复→索引建不起来→P0-3 竞态保护失效且每次启动报错不收敛。统一用 COALESCE 口径。
- **L2** `store.py:757-758`：`PgMemoryStore.init()` 重复调用覆盖旧池不关闭（测试里就发生两次）。开头判空或先 close。
- **L3** `store.py:872-891`：`save_fact`（PG）check-then-insert 非原子，并发可重复入库（仅重复，影响小）。改单语句 `INSERT … SELECT … WHERE NOT EXISTS`。
- **L4** `store.py:480` vs `:827-829`：`limit<=0` 时内存 `[-0:]` 返回全部、PG `LIMIT 0` 返回空——契约漂移。入口 `limit = max(1, int(limit))`。
- **L5** `engine.py:54,63`：坏 JSONB（tool_calls 被写坏成 object）令 `_sanitize_history` 抛 AttributeError（matcher 兜底，不崩进程）。`_deserialize_tool_calls` 解析后校验 `isinstance(out, list)`。
- **L6** `archive.py:118-138`：`_MAX_SCAN_LINES` 计的是过滤后产出数，水位线落后时仍全量 `json.loads`；7 天滚动兜底，量级可控。计数移进 `iter_records`。
- **L7** `store.py:1036-1044`：`kb_delete_source` 两步删除无事务，中间崩溃留 0-chunk 空壳；另 shutdown 钩子未调 `PgMemoryStore.aclose()`（`__init__.py:210-217`）。
- **L8** `store.py:1069`：`messages_after` 对 NULL session_id 行产生字面量 `"None"`。`str(x or "")`。
- **L9** `sanitize.py:40`：`_NOISE_HINTS` 定义后从未使用，注释误导。接入或删除。
- **L10** `service.py:87-106` + `store.py:966-986`：蒸馏无并发互斥（手动 `/kb digest` 与 cron 可双跑重复入库）、chunk 无内容去重、`/kb forget` 最新源后水位线回退造成重复蒸馏。加 `asyncio.Lock` + 同文本跳过。
- **L11** `__init__.py:170`：`AGENT_REMINDER_TICK` 非整数（`"10s"`）启动崩溃；`0` 因 `or` 语义静默变 30。try/except 回退 + 文档写明最小 5。
- **L12** `rag/ingest.py:44-47`：`ingest_text` 写块失败不回滚，留孤儿 source 行（distill 有回滚，此处没有）。
- **L13** `db_backup.py:67-68,149-157`：并发备份无锁，cron 与手动同跑交叉写坏同一当日文件。文件锁或临时名原子替换。
- **L14** `db_backup.py:149-161`：pg_dump stdout 流式循环无整体超时、stderr 无人读，诊断输出 >64KB 可永久挂起。stderr 落临时文件。
- **L15** `db_backup.py:137-149,31`：docker exec 回退路径 PGPASSWORD 进不了容器（静默降级）；`schedules` 表不在备份范围，恢复后提醒全丢且文档未提。
- **L16** `runner.py:370-381`：沙箱 HOME 是 `/tmp` 下固定路径，不校验属主（多用户主机可被抢占；因 GIT_CONFIG_GLOBAL=/dev/null 危害有限）。stat 校验属主，不符换随机后缀。
- **L17** `scripts/scratch_db.py:38-68`：DROP 库名标识符 `"` 未转义（仅操作者自伤）；`"test" in low` 子串检查会误放行 `latest` 之类。引号翻倍或白名单字符集。
- **L18** `runner.py:493-499`：M1 残余——`strip_symlinks` 在解压完成后才执行，「解压期经 symlink 穿透写入」无防护（Info-ZIP 版本行为有差异，评 L）。检出 symlink 直接删整个输出目录更稳。
- **L19** `pipeline.py:193-200`：消息流水线留有 `fence_untrusted` 的本地复制，措辞已与 `safety.py` 漂移，违背该模块「所有注入点共用一个函数」的声明。
- **L20** `runner.py:352-366`：沙箱 curl 只有 scheme/flag 白名单，无 IP 层校验，与 `web_fetch` 加固不对称（可抓 169.254.169.254/内网 https，GET-only+截断限制危害）。
- **L21** `search.py:27,47` + `bot.py:46-52`：search 技能常驻 httpx 池停机不回收（与 P1-4 同型；P1-6「停机回收」因此算部分修复）。
- **L22** `info_skills.py:19-43`：`summarize_url` 未用统一 `fence_untrusted`，与 `safety.py` 声明不一致（同范围新增的 `fetch_url` 用了）。
- **L23** `debounce.py:60-75`：`_key_locks` 淘汰竞态（release 后、等待者唤醒前 pop），可破坏「同 key 串行」不变量，窗口一个事件循环迭代。改引用计数或不清理。
- **L24** `reminder_skills.py:39-59` + `reminder.py:204-226`：提醒无每用户数量上限（schedules 表可被刷爆）、cron 提醒可当群刷屏通道、投递失败无限顺延无放弃阈值。
- **L25** `admin.py:46-49,164-167,194-197`：既有 `/reset`、`/skill uninstall` 仅 `is_allowed`（白名单群成员即可清历史/全局卸载技能）——e86fba0 已存在、非本次引入，但本次新增的 /kb 变更类命令都正确加了 `is_superuser`，标准不一致。建议统一。
- **L26** `ci.yml:21`（lint 不含 `scripts/`——本次新增 258 行脚本不参与 lint，主代理实跑 `ruff check … scripts` 为干净）+ `.pre-commit-config.yaml:3` 固定 `rev: v0.9.6` 与 CI 最新版规则集漂移。

---

## 2. 被证伪的发现（避免误导后续评审）

- **「12 → 30 skills」计数夸大**（d675b80 标题）：HEAD 实际注册约 25–27 个（builtin 21 + data/skills 4 个 yaml，配搜索 key 后 27）；README 工具表也只列 27 个名字。
- **kb_search「先 top_k 后阈值」会漏块** → 证伪：按距离全序，语义正确（lane B 推演）。
- **recall_facts 的 PG 先 LIMIT 后过滤与内存不一致** → 证伪：score 与 `<=>` 距离单调等价，结果集相同。
- **无 target 的 `ON CONFLICT DO NOTHING` 需显式索引才能命中表达式唯一索引** → 证伪：DO NOTHING 对任意唯一冲突生效，P0-3 修复成立。
- **URL 内数字被数字模式先掩会破坏链接掩码** → 证伪：URL 模式最后执行且 `\S+` 覆盖占位符，实测整体仍变 `[链接已脱敏]`。
- **18 位身份证只掩前段、尾部泄漏** → 证伪：实测仅剩一个孤立 "0"，外观问题非泄漏。
- **curl `-q` 可能被参数顶掉首位** → 证伪：`argv = ["-q", *args]` 恒为首参。
- **`prune_backups` 会误删镜像侧文件** → 证伪：`list_backups` 按 tag+后缀过滤。
- **空输出 nudge 消息污染存储历史** → 证伪：只进本次 run 的内存 messages，不落库。
- **`_dedupe_sessions` 非事务会丢数据** → 证伪：先重指后删行，任何时点崩溃可由下次 init 幂等修复。

**存疑（未实证，需部署环境验证）**：H1 推演链中「porcelain `git diff` 执行 textconv」依据 git 官方语义与仓库 FIX 文档对同类 filter 逃逸的复现记录，正则漏判本身是纯代码事实（已复现），建议修复后按 FIX 文档方法端到端验证；`reminder_cancel/list` 在 `user_id` 空串时退化为管理员语义（当前调用链恒非空，不可达，防御偏弱）；Info-ZIP 各版本对 symlink 穿透的防护差异（L18）；asyncpg JSONB 返回 str/list 因 codec 而异（代码两种都兼容）。

---

## 3. 测试与文档状况（实跑数据）

- **测试**：`.venv/bin/python -m pytest -q` → **470 收集 / 449 通过 / 21 跳过 / 0 失败**（3.84s）。跳过全部为 `TEST_DATABASE_URL` 门控的 PG 用例与平台相关用例。矩阵演进：279（上轮）→ 314（BACKLOG 基线）→ 470（本次）。
- **lint**：`ruff check agentcore plugins tests bot.py scripts` 全绿（CI 命令不含 `scripts/`，见 L26）。
- **CI 盲区**：无 PG service，test_pg_store/test_persistence 的 PG 回归在 CI 全部静默跳过——「CI 绿」不代表 PG 路径被验证过；本次 M4（恢复必炸）正是落在该盲区。
- **文档一致性**：README / `.env.example` 与代码默认值**逐项一致**（20+ 环境变量逐个核对）；`docs/agent-demo-memory-architecture.md` 除 M12（schedules 表条目过期）外与实现一致；BACKLOG 第 0 节声称的 P0-1/2/3、P1-4/5/6、P2-7 经逐条核对**均已修复**（P2-7 的 /status 缺 DB 连通性探测，P1-6 余 search 池，见 L21）。

---

## 4. 已验证为「无问题」的关键项

- **沙箱其余两层有效**：环境层 `GIT_CONFIG_GLOBAL/SYSTEM=/dev/null`、`GIT_CONFIG_NOSYSTEM`、HOME 指向工作区外固定目录；命令层 12 组 `-c` 硬化、`--no-ext-diff`、curl `-q` 首参、env 从零构造（`runner.py:96-119,384-404,457-463`），有行为级测试锁定（`test_runner_security.py:305-427`）。
- **M1（strip_symlinks）、M3（SSRF）已修复**：strip 真实接线并有 spy 测试（残余见 L18）；media/web_fetch 白名单 fail-closed、逐跳 IP 校验与重定向白名单（残余 TOCTOU 见 M9）。
- **ops_skills 的 admin-only 是真强制**：`permission="superuser"` → `registry.execute`/`get_schemas` 双路径校验，engine pop 掉 LLM 参数里的伪造身份，workspace 层再加进程内 `is_superuser`；无 `shell=True`，unit/port/path/log 参数均有白名单，`log_tail` resolve 后限 allowlist（symlink 被 resolve 拒绝）。
- **SQL 注入面干净**：facts 作用域片段白名单化、值全参数化、`_ensure_vector_dim` 表名为硬编码字面量（`store.py:215-231` 等）。
- **c0441e0 历史清洗**：孤儿 tool/连续 tool/无响应降级各边界按位置配对处理，不误删合法交换、不改库内数据，有测试。
- **P0-1/2/3、P1-4/5/6 修复真实**（证据见 §3 与 lane 复核表）；legacy session 合并不丢消息、幂等，有 PG 回归测试。
- **备份链路的正确部分**：pg_dump/psql 全 list-form argv、密码走环境变量、异地镜像纯目录复制且失败不谎报成功、恢复 dry-run 语义真实、归档恢复 CLI 双闸门有 subprocess 级测试。
- **提醒可靠性**：schedules 表持久化、失败顺延重试、停机错过补投、cron 重排、时区一致，测试覆盖较全。
- **embedding 维度对齐**：启动 `probe_dim()` 真实探测，破坏性迁移需显式 `AGENT_MIGRATE_VECTOR=1`。

---

## 5. 与在库三份 REVIEW 的衔接复核

| 上轮结论 | 本次复核 |
|---|---|
| H1 沙箱配置注入逃逸 | **部分修复**：环境层/命令层有效；仓库层守卫可被带点小节名绕过（本次 H1，已复现）——同一 commit 声称「已修复」但端到端声称过强 |
| M1 strip_symlinks 从未调用 | **已修复**（`runner.py:493-499` 接线 + spy 测试；解压期窗口残余评 L18） |
| M3 SSRF 后缀匹配/空值放行 | **已修复**（media + web_fetch 全面加固；TOCTOU 残余 media 已披露、web_fetch 未披露） |
| BACKLOG P0-1/2/3、P1-4/5/6、P2-7 | **已修复**（64cd30e / f0a413b；P1-6 余 L21、P2-7 余 DB 探测） |

---

## 6. 修复优先级建议

1. **立刻（安全）**：H1 沙箱守卫——正则放宽为跨点匹配或拒绝带点小节名，并根治性禁止写 `.git/`；补带点驱动名的对抗测试。这是上轮同源问题第二次出现，建议把「git 配置驱动命令」的完整配置键面整理成参数化测试。
2. **立刻（隐私/正确性，同在 rag/ 可一并修）**：H2 数字归一化 + H3 昵称词表/硬校验 + H4 水位线只推进到 transcript 实际末尾；同时决策 M1（私聊是否进公共库，默认不进）。
3. **紧接（备份恢复可信度，事故时才暴露的一组）**：M4（user_state `ON CONFLICT (user_id)` + 失败必须抛错）、M3（恢复标识符白名单）、M5（0600）、M6（原子写+校验和+全量 verify）、M7（恢复前自动快照）。
4. **随后**：M8 calc 幂上界、M10 asyncio.timeout/3.10 声明、M2 蒸馏注入围栏、M9 DNS rebinding 披露/钉 IP、M11 归档身份缓存、M12 文档更新。
5. **顺手**：§1 Low 清单按文件归批处理（L1/L4/L5 属正确性兜底，L24/L25 属权限一致性，其余健壮性）。
