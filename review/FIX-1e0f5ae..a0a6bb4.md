# FIX：REVIEW-1e0f5ae..a0a6bb4 逐条修复记录

对应评审：[`REVIEW-1e0f5ae..a0a6bb4.md`](REVIEW-1e0f5ae..a0a6bb4.md)（10 commit，2026-10-12）
修复日期：2026-10-12（同日）
批次：P0（H1/H2）→ P1（M1/M9/M10）→ P2（M3/M5/M6/M4）→ P3（M2/M7/M8）→ L 必修 8 条 + 顺带 5 条

**验收总口径**：默认套件 **2359 passed / 58 skipped**（46.6s）；带 PG（`qqagent_test`）**2427 passed / 9 skipped**（69.1s）；`ruff check` + `ruff format --check` 双绿（0.9.6，与 pin 一致）。每条修复均做过「改坏实现 → 新用例必红」的变异复核（见 §变异复核）。

---

## P0

### H1｜tar 条目扫描超限放行 → 超限即拒（fail-closed）

- **改动**：`agentcore/workspace/runner.py`
  - `_tar_scan`：超 `_MAX_TAR_MEMBERS` 由 `break`（返回 None = 放行解压）改为**返回拒绝原因**「压缩条目数超过扫描上限 …，拒绝解压」——垫底在扫描窗口外的恶意条目不再绕过链接/穿越拦截与执行位清理。
  - `strip_symlinks`：超 `_MAX_SYMLINK_SCAN` 返回 **-1（扫描不完整）**，unzip/tar 两个调用点把 -1 视同「检出链接」丢弃全部解压输出并明说「扫描上限」。zip 侧对链接条目没有解压前拦截，本函数是其唯一运行时防线，超限尽力而为等于放行——与 tar 侧统一「超限即拒」口径。
- **回归**：`tests/test_runner_security.py::TestH1ScanLimitFailClosed` 7 条（预扫描拒绝 / 端到端不 spawn 不落盘 / 阈值内正常解压对照 / strip_symlinks 超限 -1 与阈值内对照 / unzip+tar 调用点丢弃输出）。
- **变异**：`return` 改回 `break` → 2 条必红（已复核并还原）。

### H2｜群统计三处 PIL 渲染阻塞事件循环 → `to_thread` 卸载

- **改动**：`plugins/qq_agent_adapter/stats_route.py` 的 `render_rank_png` / `render_cloud_png` / `render_review_png` 三个调用点全部包 `await asyncio.to_thread(...)`（参数纯数据、返回 bytes，卸载安全）。
- **回归**：`tests/test_group_stats_route.py::TestRenderOffEventLoop` 5 条——行为级断言：渲染函数运行的线程 ident ≠ 事件循环线程 ident（rank/cloud/review 三处）+ 心跳任务在 0.3s 同步渲染期间保持跳动（≥3 tick，评审复现口径）。
- **变异**：`render_cloud_png` 改回同步调用 → 线程 ident 与心跳两条必红（已复核并还原）。

---

## P1

### M1｜群统计采集/归档白名单空集 fail-open → 对齐 acl fail-closed（行为变更）

- **改动**：`agentcore/groupstats/collector.py`（record）与 `raw.py`（record）：`if self._allowed and gid not in …` 改为 `if gid not in self._allowed`——**空集 = 不采集/不归档**，与 `acl.py` 同键语义一致。`groupstats/__init__.py` 的 `configure()` / `configure_archive()` 对「开关开而白名单空」打 WARNING 点名 `ALLOWED_GROUPS`。
- **行为变更入档**：从「空集 = 全采集」改为「空集 = 不采集」。README / `.env.example`（ALLOWED_GROUPS 与归档两处）已同步；`groupstats/__init__` docstring 隐私口径一并修正（见 L7/L8）。
- **回归**：`tests/test_group_stats.py`（空集不采集 + configure WARNING）、`tests/test_group_archive.py`（空集不归档）。
- **变异**：collector 改回旧形态 → 空集用例必红（已复核并还原）。

### M9｜deploy 的 .env 解析器与 dotenv 口径分歧

- **改动**：`scripts/deploy.py` `read_env_file`：①剥 `export ` 键前缀（dotenv 支持 shell 风格行；不剥则 `SUPERUSERS=*` 的通配检测漏报）；②未引值内的「 #」行内注释按 dotenv 口径剥掉（`KEY="a # b"` 引号内 `#` 保留；`KEY="bar" # c` 剥注释后再剥引号）。docstring 写明「值内需 `#` 请加引号」，`.env.example` 头部补写法约定。
- **回归**：`tests/test_deploy.py::TestEnvParser` 新增 2 条（export 前缀 / 行内注释与引号组合 4 形态）。
- **变异**：注释掉剥前缀分支 → export 用例必红（已复核并还原）。

### M10｜fresh clone 下 unit 的 ReadWritePaths 指向不存在的 data/

- **改动**：`scripts/deploy.py`：unit 模板改 `ReadWritePaths=-{repo}/data`（`-` 前缀容许目录缺席，systemd.exec(5)）；`cmd_systemd --install` 落盘前显式 `mkdir(exist_ok=True)` 创建 `data/`（bot 首启就要写）。
- **回归**：`tests/test_deploy.py::TestUnitRendering` 断言更新为 `-` 前缀形态。mkdir 分支无独立用例（cmd_systemd 落盘需 root/home 环境），由 `-` 前缀（根治项）锁定。

---

## P2

### M3｜点评 prompt 话题词行未行中 scrub

- **改动**：`agentcore/groupstats/review.py` `build_facts_block` 话题词行补 `_scrub_value`（与昵称/own_words/quote 同口径，打散行中 5+ 连字符 run）。
- **回归**：`tests/test_group_stats_review.py::TestPromptFence::test_topic_words_midline_injection_scrubbed`。
- **变异**：去掉 scrub → 必红（已复核并还原）。

### M5｜sync_wiki_subset `--prune` 删除失败静默退出码 0

- **改动**：`scripts/sync_wiki_subset.py`：新增 `prune_failed` 计数（`sync_site` counts 键 + main total），删除失败计入且 `total["failed"] or total["prune_failed"]` 判退出码 1；总览行增列「清理失败」。
- **回归**：`tests/test_wiki_sync.py::TestPrune::test_prune_delete_failure_counted_as_prune_failed`（用例首跑即抓到 counts 缺键——修复分两步落地被既有流程捕获）。

### M6｜conftest `.env` 泄漏源根治（#91 结构性根因）

- **改动**：`tests/conftest.py` 新增 session 级 autouse `_strip_prod_database_url`：`.env` 注入后统一剥离 `DATABASE_URL`（机制化，不再依赖每个测试文件自觉 delenv；定义在 guard 之后，破坏性检查仍能比对生产值）。`test_db_skills.py` 文件头注释同步（局部 autouse 降级为双保险）。
- **回归**：`tests/test_repo_guards.py::TestConftestEnvHygiene::test_database_url_absent_from_test_env`。
- **变异**：夹具删除动作置空 → 本地（有 .env）必红（已复核并还原）。

### M4｜db/shell 注册期级别过滤收进函数体

- **改动**：`agentcore/skills/db_skills.py::register_db_skills`（`if not at_least("medium"): return`）与 `shell_skill.py::register_shell_skill`（`high`）——模块内自过滤，与 action/workspace 对齐；未来新增调用方漏掉 builtin.py 的调用行 if 也不会把 schema 注册给低级别档（registry「无权限与 unknown 同文案、无探测口」语义不破）。
- **回归**：`tests/test_db_skills.py::TestRegisterLevelGate`（low 不注册 / 缺省 medium 注册）、`tests/test_shell_skill.py::TestRegisterLevelGate`（medium 与缺省 medium 不注册）；既有 `test_low_level_denied` / `test_medium_level_denied` 改为直接取 handler 闭包降级调用（handler 兜底层纵深仍在，但不经公开注册路径可达）。
- **变异**：db 侧级别检查置 False → low 用例必红（已复核并还原）。

---

## P3

### M2｜jieba 首切 842ms 挂采集热路径 → 装配后线程预热

- **改动**：`agentcore/groupstats/tokenize.py` 新增 `prewarm()`（线程内调 `jieba.initialize()` 后**重注册用户整词**——`initialize()` 会重建词典、清掉 import 期 `add_word`，这是修复中发现的连带坑）；`plugins/qq_agent_adapter/__init__.py::_init_group_stats` 装配后 `create_task(to_thread(prewarm))`。
- **回归**：`tests/test_group_stats.py::TestTokenize::test_prewarm_initializes_without_error_and_is_idempotent`——用 jieba `initialized` 标志做确定性断言（首切耗时会因进程内其他用例已触发初始化而测不出差异，时延断言已否决）；用户词典不丢由既有 `test_user_dict_merges_miscut_phrase` 锁（变异过程中它确实红了）。
- **变异**：prewarm 空转（不 initialize）→ 必红（已复核并还原）。

### M7｜容器备份 PGPASSWORD 进 argv → stdin 注入（restore 路径豁免入档）

- **改动**：`agentcore/backup/db_backup.py` 新增 `_container_script_wrap`：备份（pg_dump）容器路径改 `docker exec -i <c> sh -s`，**pg_dump 参数整段放进 stdin 脚本**（逐项 `_sh_quote` 转义）——不做 `sh -s cmd "$@"` 位置参数转发（实测 dash 不把首个 operand 当 `$0`，命令名混进 "$@" 直接炸 pg_dump "too many arguments"，真实容器复现）；argv 只剩无敏感参数。`_with_container_env`（`-e` 形态）**仅 restore 使用**并在 docstring 入档豁免：psql 的 stdin 被恢复 SQL 流占用，stdin 注入不可行；容器内 PGPASSFILE 落盘方案把「argv 秒级驻留」换成「容器文件系统驻留 + 三步失败分支」，不成比例。缓解：单用户部署、进程秒级。
- **回归**：`tests/test_backup.py::TestContainerPasswordNotInArgv` 4 条（argv 无密码 / 单引号转义 / 三种透传 / restore 豁免在位）+ `tests/test_persistence.py` 既有容器备份用例改写为新语义（stdin 脚本内容 + closed 断言）+ **真实容器端到端** `test_pg_dump_backup_and_restore` 通过（PG 套件）。
- **变异**：wrap 改回 argv 形态 → 2 条必红（已复核并还原）。

### M8｜时钟收敛 6 处 datetime 残留 + 守卫扩展（AST 级）

- **改动**：`agentcore/render/overview.py`（`fmt_clock` 的 `fromtimestamp` 带 `tz.zoneinfo()`；snapshot 兜底时钟 `datetime.now()` → `tz.now()`）；`agentcore/skills/utility_skills.py` 4 处（`date.today()` → `tz.today_date()`×2、`fromtimestamp` 带 tz、`now_text` → `tz.now()`）。`tests/test_repo_guards.py` 新增 **AST 级守卫** `find_naive_datetime_calls`（按行正则抓不到多行调用；`<x>.datetime.fromtimestamp` 带第二位置参数或 `tz=` 即合规，`now/utcnow/today` 一律违规）+ 全树零命中断言。823e352 的「收敛目标」本轮达成。
- **回归**：`TestUnifiedClock::test_naive_datetime_detector`（9 形态全抓/全放）+ `test_real_tree_has_no_naive_datetime`。
- **变异**：`fmt_clock` 改回无 tz → 全树守卫必红（已复核并还原）。

---

## L 批量处置（28 条）

### 必修 8 条（全修）

| 项 | 改动 | 回归 |
|---|---|---|
| L1 | `workspace_skills.py` run_command 描述补 `tar(仅 -x 解压到 -C 目录，条目经安全预扫描)`——描述与白名单一致 | grep 实查（无文案测试可挂） |
| L7/L8 | `groupstats/__init__` docstring「原文不落任何存储」改为「原文仅在 AGENT_GROUP_ARCHIVE=1 时落库」+ 空集语义（与 M1 同批） | README/`.env.example` 同步 |
| L10 | `lifecycle.py::_run_flush` 套 `asyncio.wait_for(deadline)`（与 debounce flush 共用 `AGENT_SHUTDOWN_FLUSH_TIMEOUT`，0=不限时）——TCP 黑洞不再挂到 systemd SIGKILL | `test_lifecycle.py` 新增 2 条（bounded+继续 aclose / 0 不限时）；变异去掉 wait_for 必红 |
| L11 | `record_raw` docstring 入档「有意不查 BLOCKED_USERS」取舍：聊天面黑名单只决定是否回应，不改变发言是群数据的一部分；归档按群粒度白名单，retention 到期清理 | 文档（评审定位即「取舍未入档」） |
| L13 | `media.py::save_image_atomic` O_CREAT 后补 `os.fchmod(fd, 0600)`——旧 0644 `.part` 残留被 O_TRUNC 复用时强制降权 | `test_media.py` 新增窗口期取证用例（monkeypatch os.replace 前一刻断言 0600）；变异去掉 fchmod 必红 |
| L18 | `builtin.py::mark_read_only` 的 wiki 两站名改为 `wiki_lookup.skill_names()` 生成（新增函数）；未知名字 registry 静默跳过，扩表新站自动进只读并行白名单 | `test_wiki_skill.py::TestReadonlyWhitelistGenerated` 3 条含端到端（模拟扩表 → register_builtin_skills → is_read_only）；变异改回硬编码必红 |
| L22 | `deploy.py cmd_env --force` 备份 rename 后补 `chmod 0600`（rename 继承旧文件权限，声称 0600 要真 0600） | `test_deploy.py::test_force_backs_up_and_rewrites` 扩展预置 0644 场景；变异去掉 chmod 必红 |
| L24 | `docs/GENERAL-GUIDELINES.md` 行数快照按 `wc -l` 实测重写（171/232/54）+ 注明快照日期 | 实测命令 |

### 顺带关闭 5 条（邻近修复捎带）

| 项 | 改动 |
|---|---|
| L2 | `test_permissions.py::test_env_layer_wildcard_discarded` 改锁 `acl._get_superusers`（实现内部的通配剔除），测试不再代劳 discard；monkeypatch `nonebot.get_driver` 强制 env fallback（全量套件下 nonebot 已被其它文件 init，原写法顺序依赖单文件绿全量红） |
| L3 | `test_env_snapshot.py` 两个 prime 用例显式 `os.environ.pop` 清理真实环境污染 |
| L6 | `AGENT_GROUP_STATS_WORDS` 贯通到渲染层（`stats_route._cloud_png` 把 `groupstats.words()` 传 `max_words`）——配 >80 不再无效；README 注释同步；回归断言 max_words 到达渲染 |
| L9 | BACKLOG 头部计数由 `check_review_docs.py --write-stats` 实测重生成（67 文件 / 2417 收集：2359+58） |
| L20 | `sync_wiki_subset.py` 未知 `--site` id 改可读报错 + exit 2（不再 KeyError traceback） |

### 显式遗留入档（需决策/工具链，按评审 §6 计划）

- **L4/L5**：引擎围栏名单扩容（docker_ps/proc_detail/system_status）与 re-exec 入口 argv 读当前 environ——既有面延续，需管理员决策围栏名单口径，下轮定夺。
- **L12**：sender_card 24 字祈使句残余——产品决策（文案口径）。
- **L14/L15**：web 群统计端点 5s 轮询 TTL 缓存、负面断言加固——性能/测试打磨，视群规模。
- **L16/L17/L19/L21**：wiki 工具链打磨（同名多来源部分 digest 重复灌入为自愈型一次性、重定向链起始别名、dry-run 计数口径、unit 路径引号）。
- **L23**：deploy check 的 `os.environ` 兜底与 systemd EnvironmentFile 语义偏差——部署语义决策。
- **L25–L28**：check_review_docs 解析边界、test_repo_guards 正则假阴面（当前零现实暴露）、check_review_docs 自身零回归、cmd_install Python 预检——工具链打磨。
- 依 REVIEW-WORKFLOW §3 L 级收敛规则：以上各项均已在本文档显式登记理由，连续两轮遗留者下轮必须修复或转「有意保留」。

---

## 验证记录

- 默认套件：`pytest -q` → **2359 passed / 58 skipped**（46.6s）
- PG 套件：`TEST_DATABASE_URL=…qqagent_test pytest -q` → **2427 passed / 9 skipped**（69.1s）——含真实容器 `sh -s` stdin 注入的 pg_dump 备份/恢复端到端
- ruff：`check` + `format --check` 全绿（0.9.6 与 pyproject pin 一致）
- `scripts/check_review_docs.py`：索引完整 / 产物位置 / 文档地图无断链；`--write-stats` 已重生成 BACKLOG 计数块
- 变异复核合计 **14 处**全部被抓后还原（H1×1、H2×1、M1×1、M2×2、M3×1、M4×1、M6×1、M7×1、M8×1、L6×1、L10×1、L13×1、L18×1、L22×1）

## 过程中发现并修正的测试缺陷

1. `test_naive_datetime_detector` 首版按 `ast.unparse` 全名匹配集合，`dt.datetime` 别名形态漏抓——重写为 owner 末段 + 方法名判定。
2. M2 首版用「预热后首切耗时 <50ms」断言——同进程其他用例已触发 jieba 初始化（全局状态顺序依赖），变异假阴性；改用 `initialized` 标志确定性断言。
3. L2 用例首版把剔除责任推给 `load_superusers`（实现中剔除在 acl 门层）且未处理全量套件下 nonebot driver 已初始化的顺序依赖——改为锁 `acl._get_superusers` + 显式炸 get_driver。
4. M7 的 `sh -s` 位置参数转发在真实容器（dash）下炸 pg_dump——PG 套件端到端用例抓到，改为参数整段进脚本（教训已写入 `_container_script_wrap` docstring 防回改）。
