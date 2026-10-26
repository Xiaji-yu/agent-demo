# AGENTS.md —— 交接文档（给下一位接手的人 / Agent）

本文件是**开工前必读**：项目是什么、边界在哪、改代码的硬性纪律、以及历史上踩过的坑。
面向"要动手改这个仓库"的人（或 Agent），不替代 README（用户视角）与 CONTRIBUTING（提交规范）。

---

## 1. 一句话与分层

QQ 机器人 Agent：**OneBot v11 协议端 ↔ NoneBot2（反向 WS）↔ 本仓库**。LLM 走 OpenAI 兼容接口，
从 `.env` 读 base_url / model / key，**不绑死任何供应商**。

```
┌ OneBot v11 协议端（局域网）  ── 反向 WebSocket ──┐
│                                                  ▼
│  plugins/qq_agent_adapter/   ← 薄适配层（唯一依赖 NoneBot 的地方）
│    matcher.py     路由/ACL/防抖/并发闸门/发送
│    pipeline.py    payload 组装、引用+转发解析、图片管线、最近图片缓冲
│    outbound.py    长回复分层投递（单条/合并转发/文件）+ 出站节流
│    group_context.py  群聊上下文环形缓冲（可选）
│    debounce.py    按 chat+user 防抖合并
│    lifecycle.py   停机顺序编排（scheduler → flush → aclose）
│    wakewords.py   唤醒词加载/匹配/剥离（触发与剥前缀共用）
│    acl.py / sink.py / admin.py / memory_route.py / scheduler 接线
│                    │
│                    ▼
│  agentcore/        ← 纯 Python，**不得 import nonebot**
│    llm/ embedding/ loop/ personas/ skills/ memory/ rag/
│    workspace/ backup/ scheduler/ budget/ safety.py multiagent/(空壳)
└─ tests/  review/  scripts/  data/(运行时产物，gitignore)
```

**分层铁律**：`agentcore` 保持 NoneBot 无关（可单测、可复用）；`plugins/` 只做平台适配。
平台相关的东西下沉到 `agentcore` 会让全部单测失效。
**唯一既存豁免**：`agentcore/skills/file_sender.py` 对 nonebot 做的是受保护的可选导入
（`try/except` + 降级为 `None`，`REVIEW-679c9b3..c472e56` L11），不要按本铁律"修复"它；
新代码仍不得在 `agentcore` 新增平台依赖（要平台能力就写进 `plugins/`）。

---

## 2. 工作纪律（硬性）

1. **先分级再动手**：Low（笔误/文案）→ 直接改；Medium（局部逻辑/契约/边界）→ 说明方案再改；
   High（安全边界、数据丢失、并发/生命周期、schema 迁移）→ 先给出**可复现证据**与方案，再改。
2. **AC 先行**：改之前写清"怎么算改好了"（一条可执行的验收命令或断言），改完必须真的跑过。
3. **只报可验证的结论**，并标注等级：
   - `verified` — 本次实际运行/复现得到（附命令与输出摘要）
   - `high` / `low` — 静态阅读代码得出的高/低置信推断
   - `unknown` — 无法在当前环境验证（说明为什么）
   **绝不编造测试输出、日志、行号或"已修复"结论**；没跑过就说没跑过。
4. **不要自动 commit / push**。本仓库的推送**只在用户明确要求时**执行；改完把 diff 与验证结果摊开即可。
   **分支纪律（2026-10-16 起）**：`main` 始终保持可发布；每个版本从 `main` 切 `release/<version>`（当前 `release/1.0.3-beta`），
   后续每次更新**开新分支**（`release/<下一个版本>` 或 `feat|fix/<主题>`）再合回 `main`，不直接在 `main` 或旧 release 分支上持续堆提交。
   版本号以 `pyproject.toml` 为准（PEP 440：`1.0.3b1` = beta），发版流程见 `CONTRIBUTING.md` 的「版本与分支管理」。
5. **最小改动**：不顺手重构、不批量改风格、不动与本任务无关的文件。历史行为要有意识保留
   （若确实要改语义，必须在 `review/FIX-*.md` 或 BACKLOG 里写明"从 X 改为 Y"与理由）。
6. **文档随代码同步**：改了行为就改 README / `docs/` / BACKLOG 里的对应描述与数字（README 是入口，细节在各主题文档）；注释只写"为什么"，
   不写"做了什么"（代码本身已经说了）。
7. **测试要能抓 bug**：新增/修改用例后，把实现**故意改坏**跑一遍，确认用例**真的失败**再还原
   （本仓库大量历史用例是"恒真断言"，详见 §5）。单文件变异可能假阴性，判定覆盖时以**全量套件**为准。
   回归用例**放进被测模块对应的主题测试文件**（文件头注明来源评审），不再按评审轮新开 `test_review_*` 批次文件；
   双实现行为断言只加在 `tests/test_store_contract.py`，`test_pg_store.py` 只收 PG 独有面（DDL/索引/池/损坏数据）。

---

## 3. 环境与常用命令

```bash
uv sync --locked --extra dev   # 按 uv.lock 装依赖，且校验 lock 新鲜度（与 CI 一致；无 uv 时退回 pip install -e ".[dev]"）
cp .env.example .env      # LLM_BASE_URL/API_KEY/MODEL、DATABASE_URL、协议端鉴权等全部 env 驱动
docker compose up -d db   # PostgreSQL + pgvector（M1+ 需要）

# 默认套件（秒级；PG 用例跳过）
.venv/bin/python -m pytest -q

# 带 PG 的完整套件（PG 用例是真的建表/TRUNCATE，绝不能用生产库）
TEST_DATABASE_URL="postgresql://qqagent:qqagent@127.0.0.1:5432/qqagent_test" \
  .venv/bin/python -m pytest -q

# 仅 PG 契约测试
TEST_DATABASE_URL=... .venv/bin/python -m pytest tests/test_pg_store.py tests/test_store_contract.py -q

# 需要临时验证库时（固定名 agent_demo_scratch，验证完删掉）
export $(grep -E '^DATABASE_URL=' .env | head -1) && .venv/bin/python scripts/scratch_db.py create --yes
```

**测试库策略（2026-10-15 定夺）**：测试库**必须是空库**，不要从生产库复制数据过来。
① DDL 错误在老库上永远测不出来（`CREATE TABLE IF NOT EXISTS` 是 no-op，正是 A2 的
`ba0b33f` 让 CI #43 全线红的成因，见 §5）——测试库要与 CI 的
`pgvector/pgvector:pg16` service **等价**（全新空库 + 同一套 DDL）；② 生产库是 624MB
真实用户隐私数据，复制一份等于多一份泄漏面与备份责任，而功能上并不需要：空库完全够
验证 store 双实现（`clear_persona_growth` 的 PG 实现即在新空库 + 变异复核下验证通过）。

- `qqagent_test`：**唯一**的测试库，跑测试前可 `DROP`/`CREATE` 重建保证 CI 等价
- `scripts/scratch_db.py create --yes`：临时验证一律用它（固定名 + `WITH (FORCE)`），
  **不要**再手工起名建库（`qqagent_scratch_contract`/`agent_demo_pgtest`/
  `qqagent_freshtest` 三个手工库已于 2026-10-15 清理）
- conftest 有硬断言：`TEST_DATABASE_URL` 不得等于 `DATABASE_URL`，且库名必须含
  `test`/`scratch`，否则拒绝跑破坏性测试

```bash
# 性能/泄漏基线（默认跳过）
RUN_PERF=1 .venv/bin/python -m pytest tests/test_perf.py -q
.venv/bin/python scripts/perf_baseline.py          # 与 perf/baseline.json 比对，劣化退出码 1

# Lint / 格式（CI 两者都门禁）
.venv/bin/python -m ruff check agentcore plugins tests bot.py scripts
.venv/bin/python -m ruff format --check agentcore plugins tests bot.py scripts

# 静态类型（CI 门禁；只查 agentcore，配置见 pyrightconfig.json）
.venv/bin/pyright agentcore

# 评审产物自检（归档 review/ 报告前必跑；pre-commit/CI 同款）
.venv/bin/python scripts/check_review_docs.py               # 索引完整/产物位置/文档地图（秒级）
.venv/bin/python scripts/check_review_docs.py --check-stats # 追加实跑 pytest 核对 BACKLOG 计数块
.venv/bin/python scripts/check_review_docs.py --write-stats # 测试数变化后重新生成 BACKLOG 计数块
```

- **`ruff` 版本必须与 `pyproject.toml` 的 pin 一致**：本地版本漂移会导致"本地绿、CI 红"或反之。
- **验收以 CI 为准**（`.github/workflows/ci.yml`：ruff check + ruff format --check + pytest，
  并起 `pgvector/pgvector:pg16` service 导出 `TEST_DATABASE_URL`）。本地绿 ≠ CI 绿。
- CI 运行查询（无需登录）：
  `curl -s https://api.github.com/repos/Xiaji-yu/agent-demo/actions/runs?per_page=3`。
  查具体日志需要 `gh` 登录（匿名拿不到）。

---

## 4. 必须守住的不变量（改代码前先对照）

| 领域 | 不变量 |
|---|---|
| 配置 | 全部通过 `.env`：`LLM_BASE_URL/API_KEY/MODEL/…`、`LLM_FALLBACK_*`（可选）、`EMBEDDING_*`。**禁止**在代码里加供应商预设分支。web 受控回写（`AGENT_WEB_WRITE=1`+CIDR）由 `settings_spec.py` 的 `ENV_SPECS`（`.env`，176 键，18 个设置簇）+ `config_yaml.py` 的 `YAML_SPECS`（`config.yaml` 标量）单一 schema 驱动；可写 114 env + 29 yaml；簇开关关闭时同簇非 gate 键**后端也拒写**（409）；聊天模型另有 `data/llm-providers.json`（0600，OpenAI/Anthropic 双协议，key 只写不回显，首次启动从 `LLM_MODEL(S)/FALLBACK` 迁移），**凭据/URL/ACL/权限级别/写面门禁/数据保留期/日志正文开关/SSRF 白名单/结构路径刻意不可写**（走 SSH；例外：`SEARCH_API_KEY`/`SEARCH_BASE_URL`/`EMBEDDING_BASE_URL`/`EMBEDDING_API_KEY` 四个凭据改在 web「模型」标签可写，只写不回显、改后重启生效；搜索另走 `data/search-providers.json`（0600，多源 CRUD + 按顺序 fallback，env 单配置为回落兼容），均与 llm-providers 同源）。写入只改配置文件介质（文本级逐键补丁，保注释）+ 同步 `os.environ`/budget/LLM 活跃位；`live=False` 的键返回 `boot` 并在页面提示重启。读取仍全部走 env/启动装配，不改变 env > config.yaml > 默认的优先级模型 |
| 路由 | 私聊直接响应；群聊**只认唤醒词（`AGENT_WAKE_WORDS`）或 @机器人**。旧 `ai/!ai//ai` 前缀已移除，不要恢复；触发判定与剥前缀共用 `wakewords.py`。用户黑名单 `BLOCKED_USERS` 在 `acl.is_allowed` 内统一生效（**superuser 豁免**）：命中者静默拒绝只记日志，其余越界明说——拒绝统一走 `acl.deny`，新命令别直接 `finish("无权限")` |
| 存储双实现 | `InMemoryMemoryStore` 与 `PgMemoryStore` 必须**语义一致**，由 `tests/test_store_contract.py` 参数化锁死；静态一致性（方法覆盖面/签名/共享辅助函数）由 `tests/test_store_parity.py` 锁死。改任一侧必须两侧都改并跑带 `TEST_DATABASE_URL` 的套件 |
| Schema 迁移 | **记忆库**的 DDL 唯一来源是 `agentcore/memory/migrations/*.sql`（经 `migrate.py` 以 advisory lock + 每迁移事务应用并记入 `schema_migrations`，`tests/test_migrations.py`）。**登记在案的例外**：群统计四表（`STATS_TABLES`：`group_msg_daily`/`group_word_daily`/`group_reviews`/`group_messages_raw`）仍由 `groupstats/store.py`、`raw.py` 在 `init()` 时 `CREATE TABLE IF NOT EXISTS` 自建，**不受校验和/版本守卫覆盖**（纳入版本链见 BACKLOG §2.8；表集合由 `test_stats_tables_match_embedded_ddl` 锁住）。**已应用的迁移文件不可再改**（校验和会拒绝启动）；结构变更必须新增迁移并在 `MIGRATIONS` 注册。版本守卫拒绝"库比代码新"与账本空洞；启动遇 `MigrationError` **fail-closed**（不降级内存）。破坏性迁移默认不执行，需 `allow_destructive`（CLI 双开关）。去重/唯一索引/vector 维度/HNSW 属幂等修复步骤（`schema_repair.py`），不占版本号。备份的 pg_dump/JSONL **排除 `schema_migrations`**，pg_dump 另写 `.schema.json` sidecar，`restore` 默认拒绝恢复比代码新的库（`--allow-newer-schema` 才放行） |
| 非正 limit/top_k | 所有 `limit` / `top_k` 参数：非正值 → **空结果**（不是"去掉最后 N 条"，也不是 DB 报错） |
| 记忆作用域 | 会话键命名空间化（`p:<uid>` / `g:<gid>:<uid>`）；facts 按 `(user, session)` 作用域隔离 + 同作用域内容去重；`list_facts` **最新优先**。人格解析：群聊 `group_persona`（群级，群内共享）> `user_state.persona`（用户级，跨群）> md 默认；私聊无群层。群人格仅 SUPERUSERS 可改（web 写面 + QQ 群命令），非超管静默 + 日志 |
| KB | `kb_add_chunks` 同来源内容去重（含批内去重），返回**实际写入条数**；蒸馏水位线 = **最新一条** distill 来源记录的进度 |
| 不可信内容 | 外部文本（引用/转发/网页/检索结果/知识块/长期事实）进 prompt 前必须过 `agentcore/safety.py` 的围栏与 `rag/sanitize.py` 的脱敏；围栏行本身不得被内容闭合 |
| 沙箱 | `agentcore/workspace/runner.py` 命令白名单：**无 shell**、禁 `|;&><` 与换行；`zip`/`curl` 等分支对参数逐项校验（曾经有 `-T -TT` RCE 与裸内网地址 SSRF）；IP 判定含私网/回环/链路本地/保留段/`100.64.0.0/10`。2026-09-30 起含只读运维命令（A 组免参数：ps/top(须 `-b`)/free/df/du/uptime/uname/nproc/whoami/id/netstat/lscpu；B 组白名单：ss 仅展示/过滤 flag 且拒 `-K/--kill` 断链路（REVIEW-3ce6e0a..de09478 H1）、systemctl 只读子命令且拒 `--host/-M`、journalctl/dmesg/hostname 只读 flag）；**`kill` 与 systemctl 动作类子命令在 run_command 刻意不放行**（重启走 `reboot.py`）。内网设备只读诊断走独立 `ssh_run` skill（host/凭据仅来自 `.env`、远端命令表写死、密码不进 argv/日志、结果过 safety 围栏才进 prompt、known_hosts 不得落在 fs 可写工作区内） |
| 权限级别 | 服务器操作类技能**只对 SUPERUSERS 开放**（schema 过滤 + handler `is_superuser` 双层——registry 层曾被 config.yaml `superusers: ["*"]` 通配击穿即 P0，该段已移除，勿恢复任何形式的 yaml superusers，含 env 层 `SUPERUSERS=*`，见 permissions.py 注释与 test_permissions.py 锁）；级别 `AGENT_PERMISSION_LEVEL`（low/medium/high，缺省 medium，`agentcore/skills/levels.py` 单点判定）只调节管理员上限：low=只读（run_command 经 `readonly_only` 拒 curl），medium=+写工作区/解压打包（`extract`/`pack_zip`，标准库实现 0600 落盘，2026-10-18 起取代外部 zip/unzip/tar）/服务容器控制（自保护：`service_ctrl` 拒自身 unit、`docker_ctrl` 拒 `PG_CONTAINER` 与自身容器，识别靠 `/proc/self/cgroup`，名字形态经 docker ps 映射补通道）+杀进程（两段确认）+构建脚本+db_query+`npm_update`（`AGENT_NPM_GLOBAL_PACKAGES` 登记表制全局包更新：LLM 只能精确选表内条目〔含 GitHub shorthand〕、真实 HOME 尊重 `~/.npmrc` prefix、不强制 `--ignore-scripts`，空表不注册；写入全局 prefix 不可回退），high=+`run_shell`（bash -c，**「无任意命令」不变量的唯一显式例外**，最小环境执行防 key 泄漏、120s 杀进程组、审计留 uid+全文）。注册期按级别过滤（低级别 schema 不可见）+ handler 内 `at_least` 复查双层；registry 对无权限者返回与 unknown 同文案（无探测口）；级别键**不进 web 写面**。旧目标白名单四键（`AGENT_SYSTEMCTL_UNITS`/`AGENT_DOCKER_CONTAINERS`/`AGENT_KILL_PATTERNS`/`AGENT_BUILD_SCRIPTS`）已移除，不要按旧文档恢复。`run_build_script` 的「没有内容权」**不成立**（同档 fs_write 可写 scripts/）：fs.write 0600 原子写剥执行位 + 解压产物一律 0600（extract 技能；外部 zip/unzip/tar 已退役——历轮 4 个 H 全部源于「Python 预扫描 vs 外部解压行为」语义鸿沟，REVIEW-6745556..d730827 H1）+ 输出围栏是真实防线（REVIEW-de09478..workdir M1）。**registry.execute 的 `name` 形参必须保持 positional-only（`/`）**：技能参数若叫 `name`（如 run_build_script/pack_zip），非 positional-only 时与关键字实参冲突 → 函数绑定期 TypeError、handler 内 try 兜不住，技能永远执行失败而直调 handler 的测试全绿（2026-10-18 发现修复） |
| 出站投递 | 长回复分层（单条 → 合并转发 → 文件）；**结果不确定时绝不重发**（超时/断连 → `FILE_UNCERTAIN`，见 `is_uncertain_send_error`）；合并转发段数超上限先"重打包"而不是逐条刷屏 |
| 停机 | 顺序固定 `scheduler → debounce flush → store.aclose`，且幂等（反了会在连接池关闭后 flush，重启必丢消息）；flush 有 `AGENT_SHUTDOWN_FLUSH_TIMEOUT`（默认 30s，0 = 不限）deadline——flush 要过全局 LLM 闸门，无界等待会被 systemd SIGKILL 反而全丢 |
| 成本 | LLM/embedding 调用上报 usage；预算软上限到点直接返回未发送结果（该分支**不打 WARNING**，是设计而非 bug） |
| 进程重启 | `/reboot`（superuser 直执行）与 web `/api/reboot`（写门禁+两段确认）共用 `reboot.py`：执行链 **C 外部白名单命令 → B 预校验后自替换（`os.execve` 只带 `bot.py` 启动时抓的出生环境快照，`.env` 派生值由新进程从文件重读）→ A 退出等 supervisor**，每步失败有退路；均先跑 `lifecycle.shutdown_agent`（幂等）并等回复发出；裸 nohup 部署退到 A 会死亡，README 有警示。**reboot.py 的 `on_command` 是模块级注册，必须由 `_load_plugin_modules()` import**（2026-09-30 漏接线事故：私聊 /reboot 落给聊天响应器；回归测试锁注册+路由）。`/reboot` 另有「重启完毕 + 耗时」回执：perform_reboot 前把目标会话+起始时间原子落盘 `data/reboot-notice.json`（`AGENT_REBOOT_NOTICE_FILE` 可覆盖），新进程由 `driver.on_bot_connect` 一次性消费，发回原会话；web 重启无会话上下文不回执。**execv 不允许继承当前 environ**：`load_dotenv()` 是 override=False，旧进程早已把旧 `.env` 注入 `os.environ`，继承即把旧值带过重启且新进程刷不掉（2026-10-12 事故：改 `.env` 加白名单群后 /reboot，戳一戳 ACL 读的仍是旧白名单；修复 `agentcore/env_snapshot.py`，回归见 BACKLOG §6.5）。带 systemd `EnvironmentFile=.env` 的部署除外——.env 内容由 systemd 在 unit 启动时进环境层，改 `.env` 仍需 `systemctl restart` |
| Web 总览 | `/agent-web` 默认只读；受控写入面（D2-1）**默认整体不挂载**，需 `AGENT_WEB_WRITE=1` + CIDR 白名单（见「配置」行）；未配 `AGENT_WEB_TOKEN` 则整个面不挂载（fail-closed）；token 只认 `Authorization: Bearer`（**不接受 URL query**）且用 `secrets.compare_digest`；备份下载是唯一例外：POST 验完 2FA 下发一次性 HttpOnly 票据（90s / 单次 / 绑定文件名与来源 IP），浏览器原生 GET 流式取文件（票据不进 URL，避免 183MB 进 JS Blob）；`agent.max_iterations`（「模型与 LLM」簇的「思考步数」）是 config.yaml 里唯一热更新的键（写入后同步运行中 engine，无需重启）；可选 `AGENT_WEB_ALLOW_CIDRS` 源 IP 白名单；响应体**不得**含 api_key/base_url/消息正文——**例外：日志查看面**（`/api/logs*`，2026-09-29 管理员决策展示原始日志，matcher 的 `[msg]`/`[reply]` 行含正文前 200 字，`docs/operations.md` 与 README 运维速查均已提示勿分享截图）**与 provider 管理面板**（后者仅鉴权 web 设置面返回完整 endpoint；`model_status()`/current_model skill 只含模型名与 endpoint host）。顶层标签：总览/设置/**主动**（沉默检测器状态 + 全参数编辑，两段确认 + 2FA + 审计 + 改前备份）/ **模型**（provider CRUD + M3 Embedding〔改后重启生效〕/ M4 多搜索源 CRUD〔按顺序 fallback，key 只写不回显、**保存后即时生效**（每次调用热读）〕，2026-10-20 从设置 llm 簇搬入、2026-10-21 增 embedding/搜索配置，设置里只留跳转指引）/**人格**（导入/编辑/删除，2FA + 审计 + 覆盖前备份，正文 ≤20000 字，default 拒删）/**备份**（本地备份列表 + 最新年龄/磁盘/镜像状态；立即备份、删除非最新一份、下载均 2FA + 审计，校验为只读动作——审计 + 并发互斥、无 2FA；下载内容是 pg_dump 明文，恢复仍只走 CLI）。web 写面两段确认升级为 **2FA（TOTP 5 分钟滚动码）**：第一段发 challenge（HMAC 绑定操作），用户私聊 bot 发 `/2fa` 获取滚动码填入，后端验证 TOTP 码 + challenge（HMAC 绑定操作 + 5 分钟时效，须覆盖私聊取码往返）+ nonce 防重放 + 同源 IP 15 分钟 5 次失败锁定（`agentcore/totp.py`，secret 存 0600 文件 `data/2fa-secret.json`，仅 superuser **私聊**可获取码）。加写入功能前必须同时补审计（`agentcore/diagnostics.py`）+ 2FA + 改前备份；每加一个 env 必须同步 `settings_spec.ENV_SPECS`（`tests/test_web.py::test_schema_covers_env_example_and_invariants` 会红） |
| 历史预算 | 单 turn 历史按 `agent.history_token_budget` 保守估算裁剪（保留最新后缀）；掉出窗口的消息滚动压缩进 `sessions.summary`（水位 `summary_upto_id`）。**摘要源自用户内容 = 不可信**，注入 system prompt 必须过围栏；摘要失败不得影响当轮回复。**历史有时间维度**：`get_history_window` 带内部 `ts`——每条历史 user/assistant 消息进 prompt 时转成自解释的发送时间前缀（同日 `（此消息发送于 HH:MM）`/跨日 `（MM-DD HH:MM）`，`ts` 本身不进 messages；2026-10-26 二次修正：第一版裸 `（HH:MM）` 被模型当成当前时间并模仿格式，故措辞必须自解释且工作流里点破「历史前缀≠当前时间」），最后一条距今超过 `agent.history_stale_after`（默认 6h，0=关闭）时再注入「对话时间线」警示——取数窗口本身没有时间维度，闲置数天的旧轮次会被模型当成"刚才"引用（2026-10-13 线上事故，BACKLOG §6.7）；`summary_enabled: false` 旧路径无 ts 故两者皆无。**prompt 时效锚点含日期+星期+时刻**（`tz.today_anchor`），且工作流要求时间类问题先调 `now`/`date_calc` 再算，禁止拿「今天是 …」猜时刻（2026-10-26 线上：凌晨 3:24 说成「离 14:30 还有十几分钟」，差 11 小时） |

---

## 5. 已踩过的坑（别重犯）

| 现象 | 根因 / 教训 |
|---|---|
| CI 红了 7 个提交才发现 | `_last_error_notify` 用 `0.0` 当"未通知过"哨兵，而 `time.monotonic()` 是**开机时长** → 刚启动时第一次告警被当成冷却期内。**"未初始化"的哨兵必须用 `None`**；告警类逻辑必须有"首次必发"用例 |
| 一批用例是"恒真断言" | 典型：`assert "某字符串" not in caplog.text`，而该字符串**全仓不存在**；`assert p["text"].strip()`（换成任意常量都通过）；只断言默认值（把参数改成忽略默认值也通过）。加固方式：断言"改坏实现后必然失败" |
| 单文件变异得出"无覆盖"的错误结论 | `permissions.is_allowed` 在单文件变异下看似没覆盖，实际由 `test_workspace_skills` 覆盖。**判定覆盖要看全量套件** |
| `zip -T -TT` / `curl 2130706433` | 白名单式参数校验必须**穷举**（禁 `-T/--unzip-command`、带 `=` 的开关必拒；**非开关操作数**是文件名，可含 `=`；curl 把所有非选项参数都当 URL 校验），黑名单必然漏 |
| `pow(2, 999999999)` 冻结事件循环 | 纯 Python 的"静态安全计算"必须同时限制**输入规模**（指数上限、运算计数） |
| `httpx` 超时被判"未送达" | 超时/断连属于**结果不确定**：既不能重发也不能当失败降级，否则用户收到两遍 |
| asyncpg JSONB 的 `tool_calls` 变成字符串 | 出库必须反序列化（`_deserialize_tool_calls`），否则下一轮 LLM 收到 `invalid type: string` |
| 全角数字 `"１２"` 通过 `isdigit()` | 需要 ASCII 的标识（QQ 号等）必须 `text.isascii() and text.isdigit()` |
| 文档数字过期 | BACKLOG 头部的测试/行数统计必须用命令实测后写入（`pytest -q`、`wc -l`），不要手写估算 |
| 老库测不出 schema 变更（CI 全新空库 Test 全线红） | DDL 里的类型/约束错误（如 `TIMESTZ` 笔误）在本地旧库上永远不触发——`CREATE TABLE IF NOT EXISTS` 是 no-op。**动过 DDL 必须对一个全新空 scratch 库跑一遍全套件**（等价 CI 起点），教训来自 A2 的 `ba0b33f`（CI #43 红） |
| 提交后 CI format 门禁意外红 | 本地 ruff 版本漂移时，**重排结果也随版本变**（0.16.6 认可的排版 0.9.6 可能不认，反之亦然）。提交前用 `pyproject.toml` pin 的版本跑一遍 `ruff format --check` 再推 |
| CI #91：7 个用例「本地绿 CI 红」 | conftest 的 autouse 夹具「`.env` 存在就 load_dotenv」把 `DATABASE_URL` 泄漏进本地测试进程（CI 无 `.env`）——restore 门用例在 CI 先 `SystemExit(2)`；run_shell 用例则依赖仓库里恰好存在 gitignore 掉的 `data/workspace`（cwd 不存在 spawn 即炸）。**教训：测试需要的环境必须用例内显式给（monkeypatch.setenv），绝不隐式依赖本地 `.env` 或 gitignore 的运行时产物；`.env` 存在与否正是本地/CI 的真实差异** |
| `FinishedException` 被 `except Exception` 吞 | NoneBot 的流程控制异常必须在最前面 `raise` |
| 变异测试用 `git checkout --` 还原，顺手丢掉同文件未提交修改 | 对**带未提交修改**的文件做变异还原，`git checkout -- <file>` 会把未提交的工作一并回退（2026-10-11 事故：BACKLOG.md 两处新修改被抹，直到自检脚本报「缺少计数块」才发现，靠重放编辑恢复）。还原临时篡改必须用**反向编辑**（sed 逆操作/精确字符串替换），或先 stash 未提交变更再变异；只有确认纯净的文件才可用 `git checkout --` |
| 改 `.env` 后 `/reboot` 静默不生效（新加白名单的群戳一戳无响应） | `load_dotenv()` 是 override=False，注入只发生在第一次启动；`os.execv` 继承**当前** environ 自替换，旧 `.env` 值被带过重启且新进程的 load_dotenv 刷不掉。修复：`bot.py` 启动时抓出生环境快照（`agentcore/env_snapshot.py`，prime() 把「先抓快照再 load_dotenv」写死在一起），re-exec 用 `os.execve` 只带快照。**教训：注入型配置 + 继承 environ 的进程自替换是隐性组合——「重启」要等价于「全新启动」，environ 必须先回出生状态再让新进程重读文件**；另注意静默 fail-closed 的 ACL（未授权只记 DEBUG）会把「配置没生效」伪装成「功能坏了」，诊断成本极高（同日定夺：戳一戳未授权改 WARNING 留痕含会话键，不回话不变） |

---

## 6. 文档地图

| 文件 | 内容 |
|---|---|
| `README.md` | **用户入口**（2026-10-16 拆分 + 正式化后 255 行）：简介/架构、环境要求、平台矩阵、快速开始、一键部署、配置速查、功能索引、运维速查、目录结构、里程碑、开发 |
| `docs/configuration.md` | 配置项详解（LLM / OneBot 反向 WS / 数据库 / 成本预算）——以 `.env.example` 为唯一事实源 |
| `docs/features-messaging.md` | 消息路由与唤醒词、防抖、群聊上下文、图片识别与引用/转发解析、长回复分层投递 |
| `docs/features-memory-and-kb.md` | 长期记忆、历史裁剪与滚动摘要、对话时间线、公共知识库（RAG）、MediaWiki 直查 |
| `docs/features-community.md` | 群发言榜与词云、群消息原文归档、定时提醒、定时内容推送 |
| `docs/features-skills.md` | 工具集（skills）、沙箱工作区、SSH 只读诊断、权限级别与管理员动作、run_shell、db_query |
| `docs/features-interaction.md` | 人格系统与成长层、点歌/放歌、Web 总览与全部设置、本机概览（戳一戳） |
| `docs/operations.md` | 日志归档、记录保全（归档 + 备份 + 恢复）、性能/内存基线 |
| `CONTRIBUTING.md` | 开发环境、风格、PR 规范 |
| `AGENTS.md`（本文件） | 给接手者/Agent 的边界、纪律、不变量、坑 |
| `BACKLOG.md` | 还没做的事（§1–§5）+ 已实证缺陷（§6，含 §6.1 规范审查、§6.2 小项） |
| `docs/memory-and-persona.md` | **记忆与人格详解（当前状态）**：七路数据来源、system prompt 组装顺序即优先级、人格/成长层机制、作用域隔离、安全约定、配置总表（含表关系与索引） |
| `docs/GENERAL-GUIDELINES.md` | 从本仓库纪律/评审规范提炼的**通用工程规范**（可迁移到其它项目；专属项映射见其 §8） |
| `review/REVIEW-*.md` | 各轮评审报告（按 commit 区间命名，问题都带复现证据） |
| `review/FIX-*.md` | 对应修复记录：改法、验证命令、剩余项 |
| `review/REVIEW-WORKFLOW.md` | 多子代理评审 → 主代理复核的流程约定 |

---

## 7. 交付前检查清单

- [ ] 默认套件绿：`.venv/bin/python -m pytest -q`
- [ ] 动过 `memory/store.py`、备份/恢复、归档 → 带 `TEST_DATABASE_URL` 再跑一遍
- [ ] 动过热路径/缓冲 → `RUN_PERF=1 ... tests/test_perf.py -q`（必要时更新 `perf/baseline.json`）
- [ ] `ruff check` + `ruff format --check` 全绿（版本与 pin 一致）
- [ ] `pyright agentcore` 全绿（版本与 pin 一致）
- [ ] 新增/修改用例都做过"改坏实现 → 必须失败"的复核
- [ ] 行为变更已同步 README / `docs/` / BACKLOG / `review/FIX-*.md` 的描述与数字
- [ ] 没有顺手重构、没有自动 commit/push
