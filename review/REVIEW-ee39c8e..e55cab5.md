# agent-demo 近期 Commit 评审报告

**评审范围**：`ee39c8e..e55cab5`（1 个 commit，分支维度 `main..HEAD`）+ 未提交 M2–M4 预审（45 个已跟踪文件修改 + 5 个未跟踪文件）
**评审日期**：2026-10-25
**评审方式**：5 个并行子代理分线审查（结构拆分/记忆与隐私/备份恢复/测试与 CI/文档与依赖）+ 主代理逐条实证（复现脚本、全量套件、ruff/format/pyright/uv lock 实测）
**工作区状态**：**不干净**——`45 M + 5 ??`。未跟踪的 `uv.lock`、`pyrightconfig.json`、`.github/dependabot.yml`、`plugins/qq_agent_adapter/memory_route.py`、`tests/test_memory_route.py` 是本次变更的运行前提（见 M9）。

> **范围说明（流程缺口）**：按 WORKFLOW §1 增量链，上一份 REVIEW 的终点是 `9c2d3c1`；其后到 `ee39c8e` 还有 18 个 commit（nudge P1–P3、at 分段渲染、1.0.1 合流等），没有独立的 REVIEW 报告，仅 `AC-proactive-nudge.md` 覆盖了 P1–P3。本轮按用户指定的分支维度只审 `main..HEAD`，不重复审查已入 main 的历史提交；该缺口记为 §1-L0，建议下一轮补 `REVIEW-9c2d3c1..ee39c8e.md`。

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | **H1**：迁移 SQL 执行期异常不是 `MigrationError` → 启动路径降级内存存储，`fail-closed` 承诺被绕过 |
| **H1–H4 结构拆分** | 拆分本身未发现行为回归：web 路由清单、engine 全量用例、双存储 44 方法契约、迁移器校验和/版本守卫均绿 |
| **M4 隐私闭环** | `/memory` 跨**用户**隔离成立；跨**会话**（私聊事实在群里回显）不成立（M1） |
| **备份加密** | 加密流水线本身合格（不落明文、不回退 JSONL、超时杀进程）；版本门禁在 `.age` 路径失效（M2），恢复后账本残留（M3） |
| **测试** | 默认套件 `187 failed / 2955 passed / 89 skipped`（Windows 环境性失败，非本轮回归）；PG 与 POSIX age 用例本地跳过（**unknown**） |
| **门禁** | ruff check / format / pyright 全绿；`uv lock --check` exit 0（72 包，lock 新鲜） |
| **声称核对** | README「uv.lock 唯一锁文件」与 deploy 的 pip 安装不符（M6）；AGENTS「migrations 是唯一 DDL 来源」与 groupstats 内嵌 DDL 不符（M7）；`AGENT_LOG_CONTENT` 覆盖面被文档夸大（M5） |
| **门禁结论** | **H1 阻断合并/发布**；M1–M4、M9 应在合并前修复；M5–M8 记入下一轮，M9 为交付硬前提 |

---

## 1. 已实证的问题

### H1（已复现）迁移/修复异常未归入 `MigrationError`，启动静默降级内存

**位置**：`agentcore/memory/migrate.py:156`（docstring 承诺「失败一律抛 `MigrationError`」）、`:208-219`（事务内 `await conn.execute(sql)` 未包裹）；`agentcore/memory/store.py:1173-1179`（`migrate` 与 `repair_schema` 直接调用）；`plugins/qq_agent_adapter/__init__.py:294-317`；`docs/operations.md:196`。

**证据（已复现）**：用 fake pool 让 `conn.execute(sql)` 抛 `RuntimeError("DDL typo failed")`，调用 `migrate(pool, dim=8)`：

```
RuntimeError | is MigrationError: False | 'DDL typo failed'
```

`PgMemoryStore.init` 会把该异常原样抛出，但 `_init_memory` 只对 `MigrationError` fail-closed（`:294`）；其余 `Exception` 走降级分支（`:306-317`），返回 `InMemoryMemoryStore()`。因此**迁移 SQL 的语法/权限/运行期错误**（以及 `repair_schema` 中 vector SQL 的错误）都会静默降级为内存存储。

**影响**：用户以为长期记忆/归档在 PG，实际只在进程内，重启即丢；这正是 `docs/operations.md:196` 声称要避免的场景。属于 §3「正确性错误导致数据静默丢失」类，按 **H** 处理。

**建议**：在 `migrate()` / `repair_schema()` 内把非 `MigrationError` 的执行异常统一包装为 `MigrationError`（保留 `__cause__`）；或把 `_init_memory` 的降级条件收窄为连接/鉴权类异常。回归测试：注入一个 `execute` 必抛的 fake pool，断言启动路径**不返回** `InMemoryMemoryStore`。

---

### M1（已复现）`/memory` 跨会话列出/删除，文档与实现相反

**位置**：`plugins/qq_agent_adapter/memory_route.py:79`（`list_facts_detailed(uid, limit=...)` 未传 `session_id`）、`:99`（`delete_fact(uid, id)` 未传 `session_id`）；`agentcore/memory/store.py:750/767/784` 的 `session_id=None` 语义为**全部会话**；`docs/features-memory-and-kb.md:79` 声称「跨用户/跨会话均失败」。

**证据（已复现）**：内存存储中写入 `p:1` 与 `g:2:1` 两条事实后，`list_facts_detailed("1", limit=20)` 同时返回两条；在群会话上下文里用私聊事实的 id 调 `delete_fact("1", id)` 成功删除。

**影响**：用户在群里执行 `/memory`，机器人会把**私聊/其他群**的事实正文回显到当前群（第三方可见）；`/memory forget` 也能删掉当前会话之外的记忆。用户是触发者，但泄露对象是群成员，且与文档承诺直接冲突。

**建议**：命令侧用 `resolve_session(uid, group_id)` 得到当前会话键并显式传入 `list_facts_detailed` / `delete_fact` / `clear_facts`；若产品确实想管理全部记忆，增加显式 `/memory all` 并提示回显范围。回归：群上下文不得列出/删除私聊事实（当前 `tests/test_memory_route.py` 只造 `PrivateMessageEvent`，无此负例）。

---

### M2（已复现）age 加密备份的 schema sidecar 命名失配，版本守卫失效

**位置**：`agentcore/backup/db_backup.py:247-248`（sidecar = `f"{path}.schema.json"`，`path` 为 `.sql.gz.age`）、`:258-259`（读取同样拼 `f"{path}.schema.json"`）、`:889-904`（restore 先拒绝 `.age`，用户解密成 `.sql.gz` 后读取 sidecar）；`scripts/backup_usb.sh:57`（只复制 dump，不含 `.schema.json`）。

**证据（已复现）**：临时目录生成加密备份后，sidecar 为 `a.sql.gz.age.schema.json`；解密产物为 `a.sql.gz`，`_read_schema_sidecar(Path("a.sql.gz"))` 返回 `None` → `restore_database` 跳过「备份版本高于代码」守卫。U 盘脚本的必含清单（`:76`）只有 `$DUMP_NAME / .env / config.yaml`，同样不复制 sidecar。

**影响**：推荐使用的加密备份形态下，恢复进旧代码的版本守卫实际不生效；U 盘冷备也没有版本信息。属于「声称有守卫但路径未覆盖」。

**建议**：sidecar 命名规范化（去掉 `.age` 后的 base 作为 sidecar 名），读取时同时兼容旧名；`backup_usb.sh` 把 `$DUMP.schema.json` 纳入复制与必含清单；补加密路径的 sidecar 命中测试。

---

### M3（读码+推演）恢复进非空库时 `schema_migrations` 不被重建

**位置**：`agentcore/backup/db_backup.py:445-462`（`pg_dump --clean --if-exists --exclude-table=schema_migrations`）；`docs/operations.md:204-205`（声称「账本由恢复后的首次启动按基线幂等重建」）。

**证据（推演，逻辑链）**：`schema_migrations` 被排除出 dump，`--clean` 也不会为它生成 `DROP`。恢复到**同一个已有库**（灾难恢复的常见形态）时，旧账本保留且通常已是当前代码版本；启动期 `migrate` 看到「已应用到最新」即跳过，而业务表已回到备份时的旧结构。

**影响**：恢复后 schema 与代码错位；文档的「首次启动重建账本」只对**空库**成立。可能表现为缺列/缺表报错，或迁移被错误跳过。

**建议**：restore 前 `DELETE FROM schema_migrations`（或在恢复事务内清空），或提供 `migrate --rebuild-ledger`；文档明确「恢复进空库」前提。补 PG 集成测试：非空 scratch 库恢复旧备份后，启动必须重新应用缺失迁移。

---

### M4（读码+推演）CLI 迁移维度与运行时探测维度可能不一致；维度修复非原子

**位置**：`scripts/migrate.py:60-67`（`_dim()` 只读 `EMBEDDING_DIM`，默认 2048）；`plugins/qq_agent_adapter/__init__.py:836`（运行时用 `embedding.probe_dim()`）；`agentcore/memory/schema_repair.py:101-104`（`TRUNCATE` 与 `ALTER` 分两条裸执行）、`:173-204`（修复步骤不在 advisory lock 内、无 `lock_timeout`）。

**证据（推演）**：运行时维度来自 provider 探测；CLI `up/repair` 用 env 默认值。两者不一致时：默认路径只记 error 不修复（写入随后失败）；显式 `--allow-destructive --yes` 时会把两张表 TRUNCATE 到**错误的维度**；若 `ALTER` 失败，TRUNCATE 已生效，表被清空但类型未变。

**影响**：维度迁移可能改错维度或半完成；后者是显式授权下的数据丢失。

**建议**：CLI 与运行时共用同一维度解析（探测 provider，或强制校验 `EMBEDDING_DIM` 与探测结果一致）；`TRUNCATE + ALTER` 放同一事务；`repair_schema` 也取 advisory lock + `lock_timeout`。回归：CLI/运行时 dim 一致性测试；变异「去掉事务」必须红。

---

### M5（已复现）`AGENT_LOG_CONTENT=0` 只覆盖适配层，engine 仍落正文

**位置**：`plugins/qq_agent_adapter/matcher.py:156-176`（开关与 `_log_text` 仅在此文件）；`agentcore/loop/engine.py:695`（`logger.exception("save fact failed: %s", content[:80])`）、`:1349`（`logger.info("skill call: %s %s", func_name, func_args)`）、`:1271`；`.env.example:272-276`、`docs/operations.md:12` 的表述是「不落消息正文」。

**证据（读码+抽样）**：`AGENT_LOG_CONTENT` 在 `agentcore/` 零命中；把开关置 0 后，engine 的长期事实内容与技能参数仍原样进日志。

**影响**：隐私开关名不副实；用户按文档置 0 后仍可能在日志里看到对话内容（`skill call` 的参数常含用户文本）。

**建议**：把「内容开关 + 截断」helper 下沉到平台无关层（如 `agentcore/safety.py`），engine 全部内容日志统一调用；或把文档改为「仅覆盖适配层日志」并列出未覆盖路径。优先前者。回归：置 0 时 engine 日志断言不含正文。

---

### M6（读码）一键部署绕过 `uv.lock`

**位置**：`scripts/deploy.py:346-352`（`pip install -e .` / `.[dev]`）；`README.md:109`（「`uv.lock` 是唯一锁文件」）、`:116-130`（一键部署入口）。

**证据（读码）**：CI 走 `uv sync --frozen`，部署走 `pip install -e`，两条安装路径的解析器与锁文件完全不同。

**影响**：生产部署不可复现；CI 绿不代表部署环境一致，且与 README 的锁文件承诺冲突。

**建议**：deploy 改用 `uv sync --locked --extra dev`（无 uv 时显式提示或引导安装）；若保留 pip 路径，README 改为「CI 使用 uv 锁，部署路径不锁」并给出核对命令。

---

### M7（读码）AGENTS「migrations 是唯一 DDL 来源」与 groupstats 内嵌 DDL 不符

**位置**：`AGENTS.md:135`；`agentcore/groupstats/store.py:30-62`（`group_msg_daily` / `group_word_daily` / `group_reviews`）；`agentcore/groupstats/raw.py:38`（`group_messages_raw`）；`agentcore/memory/schema.py:52`（`STATS_TABLES`）。

**证据（读码）**：四张群统计表通过 `CREATE TABLE IF NOT EXISTS` 内嵌创建，不在 `agentcore/memory/migrations/` 版本链，也不受迁移校验和/版本守卫覆盖。

**影响**：schema 变更审计存在盲区；新接手者按 AGENTS 的「唯一来源」理解会漏掉 stats 表。

**建议**：把 stats DDL 纳入版本化迁移（推荐），或修订 AGENTS 措辞并在 BACKLOG 登记「待迁移」；漂移测试覆盖 stats 表集合。

---

### M8（已复现）CI 的 `--frozen` 不校验 lock 新鲜度，`uv run` 可自动重锁

**位置**：`.github/workflows/ci.yml:53`（`uv sync --frozen --extra dev`）、`:56-76`（`uv run ...` 未带 `--frozen/--locked/--no-sync`）；`README.md:109`。

**证据（已复现）**：本地 `uv lock --check` exit 0、72 个包，lock 当前是新鲜的；但 `--frozen` 的语义是「照现有 lock 安装、不检查是否落后于 pyproject」，CI 没有 `--locked` 或独立 `uv lock --check` 步骤。

**影响**：目前没有实际漂移，但「锁定」承诺只依赖「没人改 pyproject」；一旦改了依赖而忘更新 lock，CI 可能静默使用旧版本。

**建议**：install 改 `uv sync --locked --extra dev`，或加 `uv lock --check` 步骤；`uv run` 加 `--no-sync`（或统一 `--locked`）。

---

### M9（已复现）5 个未跟踪文件是本次变更的运行前提

**位置**：`git status --porcelain` 的 `??` 项：`uv.lock`、`pyrightconfig.json`、`.github/dependabot.yml`、`plugins/qq_agent_adapter/memory_route.py`、`tests/test_memory_route.py`；依赖点：`ci.yml:53`（`uv sync --frozen` 需要 `uv.lock`）、`:65`（pyright 需要 `pyrightconfig.json`）、`plugins/qq_agent_adapter/__init__.py:225`（`/memory` 需要 `memory_route.py`）。

**证据（已复现）**：`git status --porcelain | awk '{print $1}'` → `5 ?? / 45 M`。

**影响**：若按惯例 `git add -u` 提交，CI 会在安装/类型检查步直接失败，`/memory` 功能与对应测试也不会进入仓库。

**建议**：提交时显式 `git add` 这 5 个文件；交付清单加一条 `git status --porcelain | grep '^??'` 自检。

---

### L 级问题（14 条，含批量关闭计划）

| # | 位置 | 问题 | 本轮处置 |
|---|---|---|---|
| L0 | 增量评审链 | `9c2d3c1..ee39c8e` 18 个 commit 无独立 REVIEW，仅 AC 覆盖 P1–P3 | 显式遗留：建议下轮补 `REVIEW-9c2d3c1..ee39c8e.md` |
| L1 | `db_backup.py:808-821`；`backup_mirror.sh:84-87` | `_remove_backup` docstring 称删 sidecar，实际只删 `.sha256`，`.schema.json` 成孤儿；远端 prune 同样不匹配 | 随 M2 修 |
| L2 | `scripts/migrate.py:91-100` | `status` 对 `unknown`/账本空洞只打印，不非零退出（仅 checksum 不符退出 1） | 随 M4 修 |
| L3 | `scripts/migrate.py:175-177` | `create` 是纯文件操作，却在分支前调用 `_db_url()`；无 `DATABASE_URL` 时 exit 2（已复现） | 随 M4 修 |
| L4 | `db_backup.py:258-270` | sidecar 损坏/缺字段时只 warning 并返回 None → 版本守卫静默跳过 | 随 M2 修（应 fail-loud 或明确降级日志） |
| L5 | `tests/test_memory_route.py`；`__init__.py:224-227` | `/memory` 无 `_load_plugin_modules` 接线测试（与 reboot 漏接线事故同类）；测试直接 import 模块 | 随 M1 修 |
| L6 | `test_store_parity.py:68/88-102`；`test_web.py:108-129` | parity 只检查「调用了某函数」+ `>=40` 下限，内联复制可绕过；web 路由金标只比 `(method,path)`，不查鉴权/重复注册 | 本轮修 |
| L7 | `matcher.py:156-164` | 脏值 fail-open：只有 `{0,false,no,off}` 视为关闭，`disabled`/`False\n` 等按开启 | 显式遗留（与全仓 bool env 解析口径统一另开） |
| L8 | `docs/operations.md:254`；`.env.example:272` | 文档写 `text_len`，代码实际输出 `<len=N>` | 本轮修 |
| L9 | `AGENTS.md:132` | `ENV_SPECS` 写 173 键，实测 176（可写 114 正确） | 本轮修 |
| L10 | `db_backup.py:499` 附近 | age 子进程清理路径对 `CancelledError` 不保证，取消时可能留下进程/临时文件 | 显式遗留（仅 POSIX 可复现，并入 age 用例条件） |
| L11 | `memory_route.py:128-129` | 确认码在 `clear_facts` 前 pop；存储失败会吃掉确认码，用户需重新发起 | 随 M1 修 |
| L12 | `engine.py:1271/1349` | LLM step / skill 参数日志无长度上限，且不走开关 | 随 M5 修 |
| L13 | `test_memory_route.py:45` | 只构造 `PrivateMessageEvent`，无群会话负例（M1 的测试根因） | 随 M1 修 |
| L14 | `schema_repair.py:173-204` | 修复步骤不在 advisory lock 内、无 `lock_timeout`，多实例启动可并发 dedupe/DDL | 随 M4 修 |

**批量关闭计划**：本轮随 M 修复 11 条（L1–L6、L8、L9、L11–L14），显式遗留 2 条（L7、L10，理由见上），流程补录 1 条（L0）。未超过 §3 的 L 收敛阈值。

---

## 2. 被证伪 / 降级为观察项的发现

| 怀疑 | 结论 | 验证方式 |
|---|---|---|
| 「age 开启但 pg_dump 不可用会静默回退明文 JSONL」 | **证伪**：`_backup_encryption` + `db_backup.py:319/338` 直接抛错，不回退 | 读码 + `TestAgeEncryption` 用例 |
| 「restore 会把 `.age` 当明文灌库」 | **证伪**：`:889-894` 显式拒绝并给出解密命令 | 读码 + 用例 |
| 「web 拆分改变了路由集合或鉴权」 | **证伪**：只读/可写路由金标清单全绿 | `tests/test_web.py` 路由清单用例 |
| 「engine.run 拆分改变了对话行为」 | **证伪**：`test_engine.py` 全绿 | 全量套件 |
| 「迁移器会重复应用或忽略 checksum」 | **证伪**：checksum/版本/空洞守卫用例通过 | `tests/test_migrations.py` |
| 「groupstats 自建表与迁移表重名冲突」 | **证伪**：四张表无重名；但「唯一 DDL 来源」的文档声称失真（M7） | 读码 + `schema.py` 表集合 |
| 「M2 的 60 文件改动含大量行为变更」 | **降级**：抽样为注解/类型收窄；`agentcore/render/overview.py:469` 一处 `assert` 非纯类型，未构成回归 | `git diff` 抽样 + 全量套件 |
| 「uv.lock 已与 pyproject 漂移」 | **证伪**：`uv lock --check` exit 0（72 包） | 实测 |
| 「`/memory` 可跨用户删除」 | **证伪**：跨用户删除被拒 | 用例 + 复现 |
| 「`/memory clear` 确认码可重放」 | **证伪**：码执行前 pop，重放返回无效 | 用例 |

---

## 3. 测试与文档状况

### 3.1 实跑数据

```text
默认套件：187 failed, 2955 passed, 89 skipped in 199.40s
ruff check：All checks passed!
ruff format --check：220 files already formatted
pyright agentcore：0 errors, 0 warnings, 0 informations
uv lock --check：Resolved 72 packages（exit 0）
上轮 H1 回归：tests/test_persistence.py::TestFeedRestoreStdinFilter —— 2 passed
```

**Windows 环境性失败**：抽样核对 12 个失败，均为存量问题（`chmod 0600` 语义、`os.O_NOFOLLOW`、GBK 解码、缺 `psql/rsync/age`），未发现落在 `test_memory_route.py` / `test_migrations.py` / `test_store_parity.py` / `test_engine.py` 的失败。**未逐条 triage 全部 187 个（unknown）**。

### 3.2 未验证面（§8.3）

| 面 | 状态 | 原因 |
|---|---|---|
| PG 迁移/恢复集成（M3、M4） | **unknown** | 本机无 PG 容器，`TEST_DATABASE_URL` 未设；PG 用例整体跳过 |
| age 真实加解密与 POSIX 清理（M2、L10） | **unknown** | `test_backup.py` 的 age 集成用例 `skipif(os.name != "posix")`，本地 Windows 跳过 |
| CI | **unknown** | 分支未推送，GitHub Actions 无对应 run |
| 真机 QQ 群内 `/memory` 回显 | **unknown** | 未接协议端；M1 已用内存存储复现 |

**门禁结论**：M3/M4 涉及数据安全但本地无法实跑 → 按 §8.3 必须声明为「未验证面」，**修完后需在 CI/带 PG 环境复验**；H1 已本地复现，**阻断合并**。

### 3.3 文档一致性

| 文档 | 与实现不符点 | 对应 |
|---|---|---|
| `docs/operations.md:196` | 只承诺 `MigrationError` fail-closed，未覆盖 SQL 执行期原始异常 | H1 |
| `docs/features-memory-and-kb.md:79` | 「跨会话均失败」不成立 | M1 |
| `docs/operations.md:204-205` | 「恢复后首次启动重建账本」只对空库成立 | M3 |
| `.env.example:272-276` / `docs/operations.md:12` | 「不落消息正文」只覆盖适配层 | M5 |
| `README.md:109` | 「uv.lock 唯一锁文件」与 deploy 的 pip 路径冲突 | M6 |
| `AGENTS.md:135` | 「migrations 是唯一 DDL 来源」不成立 | M7 |
| `docs/operations.md:254` | `text_len` vs 代码 `<len=N>` | L8 |
| `AGENTS.md:132` | 173 键 vs 实测 176 | L9 |
| `AGENTS.md:109`（§7 清单） | 未提醒未跟踪的 `uv.lock`/`pyrightconfig.json` 必须入库 | M9 |

---

## 4. 已验证为「无问题」的关键项

- **H1 web 拆分**：只读/可写路由清单金标逐项相等；2FA/审计/备份入口未变。
- **H2 engine 拆分**：`run` / `_init_agent` / nudge 表驱动拆分后，`test_engine.py` 全绿；nudge 与 growth 接线未断。
- **H3 双存储收敛**：共享辅助函数后，`test_store_parity.py` 覆盖 44 个抽象方法；`test_store_contract.py` 参数化契约通过。
- **H4 迁移器**：advisory lock、每迁移事务、checksum/版本/空洞守卫、破坏性门控均有用例锁定；上轮 H1 的 `_feed_restore_stdin` COPY 块状态机回归测试存在且通过（2 passed）。
- **age 加密流水线**：pg_dump stdout 流式进 age stdin，不落明文；加密开启时 JSONL 回退被禁止；restore 显式拒绝 `.age` 直读；子进程有超时与 kill。
- **`/memory` 用户隔离**：跨用户删除被拒、清空不越用户、确认码一次性、过期拒绝、第一段不删除，均有用例。
- **依赖**：`uv lock --check` 通过（72 包）；运行时依赖只收窄 nonebot 上限，新增 `pytest-cov` 为 dev 依赖；**本轮未做 CVE 扫描（unknown）**。

---

## 5. 与在库报告的衔接复核

| 上轮项 | 本轮状态 | 依据 |
|---|---|---|
| `REVIEW-1b16fa7..9c2d3c1` 的 4H/19M/18L | 已由 `4af8168` 合入 main；**本轮未逐条复核（unknown）** | `git log` 显示合流；`FIX-1b16fa7..9c2d3c1.md` 在档 |
| 上轮 H1（restore 子串过滤误删 COPY 行） | **回归在位且通过** | `tests/test_persistence.py::TestFeedRestoreStdinFilter` → 2 passed |
| 上轮未验证面：PG 套件 | 本轮仍为 unknown（本机无 PG） | §3.2 |
| 增量链 `9c2d3c1..ee39c8e` | **未覆盖**，无独立 REVIEW | §1-L0 |

---

## 6. 修复优先级建议

1. **P0（阻断）**：H1 异常归类 + 回归；M9 把 5 个未跟踪文件纳入提交。
2. **P1（合并前）**：M1 `/memory` 会话作用域 + 群负例；M2 sidecar 命名/USB 复制；M3 恢复清账本；M4 CLI/运行时维度同源 + 原子修复。
3. **P2（下一轮）**：M5 日志开关下沉；M6 deploy 锁文件；M7 stats DDL 入迁移；M8 CI `--locked`。
4. **L**：按 §1 批量计划随对应 M 修复；L7/L10 显式遗留并记录理由。
