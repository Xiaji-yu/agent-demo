# 修复记录：REVIEW-3ce6e0a..de09478（2H / 6M / L 项）

对应评审：[REVIEW-3ce6e0a..de09478.md](REVIEW-3ce6e0a..de09478.md)。
本记录覆盖：评审范围 4 个 commit 的 M 级修复 + 未提交变更（ssh_run / reboot 回执 /
图片贯穿 tool-loop）按门禁要求修完 H 后一并落地。修复方式、回归测试与验证结果逐条如下。

## H 级（P0，提交前必须）

### H1 `ss -K/--kill` 穿透 A 组免参数白名单 → 收敛为展示 flag 白名单
- **改法**：`ss` 移出 `_READONLY_OPS`，新增 `_SS_SAFE_FLAGS`/`_SS_SAFE_FLAG_KEYS`/
  `_SS_GLUED_SINGLE` 与 `_ss_ok()`（`agentcore/workspace/runner.py`）：只放行展示/
  过滤类 flag（`-tulnp` 等连写短 flag 拆单项逐个过表），`-K/--kill`/`-D`/`-A`
  及其粘合/`=` 变体一律不在表内即拒；位置参数（只读过滤器表达式）随全局四道闸走。
- **文档同步**：AGENTS.md §4 沙箱行、README `run_command` 段、`workspace_skills.py`
  工具描述（ss 移入「仅展示/过滤选项（-K/--kill 断链路，拒）」）。
- **回归**：`tests/test_runner_security.py::TestReadonlyOpsWhitelist::test_ss_kill_forms_rejected`
  （`-K`/`--kill`/`-K dst :8080`/`-Kx`/`--kill=1`/`-D`/`-A`/`--diag` 全拒）+
  正例扩充（`-tnp state established`、`-f inet`、`--family=inet6`）。
- **变异**：`_ss_ok` 改回无条件放行 → 该用例 FAILED ✅（git worktree 实测）。

### H2 ssh_run 远端输出未过围栏 → 入 `_UNTRUSTED_TOOL_RESULTS`
- **改法**：`agentcore/loop/engine.py` 围栏查表加 `"ssh_run"`（ssh_run 也可在
  skill 侧自围栏，取 engine 查表一处收口，与 search_* 同机制）。
- **文档同步**：README「SSH 远程只读诊断」补「输出按不可信内容处理」条目；AGENTS.md
  ssh_run 括注补「结果过 safety 围栏才进 prompt」。
- **回归**：`tests/test_engine.py::TestSearchResultFence::test_ssh_run_result_is_fenced`
  （断言 tool 消息含围栏头/尾与「不可信数据」，且注入文本保留在围栏内）。
- **变异**：查表移除 `ssh_run` → 该用例 FAILED ✅（worktree 实测）。

## M 级（P1）

### M1 atomic_write 窗口期 0644 承载完整 .env
- **改法**：`config_write.atomic_write` 改 `os.open(tmp, O_WRONLY|O_CREAT|O_TRUNC|
  O_NOFOLLOW, 0o600)` + `os.fdopen`——创建即 0600（umask 不再参与），最终态仍继承
  原文件权限；`O_NOFOLLOW` 顺带堵 tmp 名可预测的 symlink 预置。
- **回归**：`tests/test_config_write.py::TestReview3ce6e0a::test_atomic_write_tmp_never_wider_than_600`
  （取证点挂 in os.chmod，断言 chmod 前一刻落盘权限 == 0600）。
- **变异**：改回 `open(tmp, "w")` → 该用例 FAILED ✅（worktree 实测；首版用例
  取证点误挂 replace 时——chmod 之后——漏抓，已修正取证时机，教训入 §5「文档
  数字过期」同栏：**断言时机必须对准缺陷窗口**）。

### M2 reboot 停机编排漏传 scheduler
- **改法**：`reboot._shutdown_everything` 补 `scheduler=getattr(get_driver(),
  "_agent_scheduler", None)`（reboot 经 execv/_exit 退出，on_shutdown 钩子永不执行，
  这里是 reboot 路径唯一收尾）。
- **回归**：`tests/test_reboot.py::TestShutdownEverything::test_uses_lifecycle_shutdown_agent`
  断言 `scheduler` kwarg 在 seen。
- **变异**：移除该 kwarg → 该用例 FAILED ✅（worktree 实测）。

### M3 reboot C 步不观察子命令退出码
- **改法**：`perform_reboot` spawn 成功后观察 `_C_STEP_OBSERVE`（0.5s）：子命令
  秒退非零即记 ERROR 并回退 re-exec；替身返回 None 跳过观察（兼容测试）。README
  部署前提补观察窗说明。
- **回归**：`test_external_child_failed_falls_back_to_execv`（秒退非零 → execv）+
  `test_external_child_alive_exits`（观察窗内存活 → 照常退出让位）。

### M4 `LogLevel=ERROR` 抑制 TOFU 首连警告
- **改法**：`ssh_skill._base_argv` 删除 `LogLevel=ERROR`（默认 INFO，TOFU
  「Permanently added」Warning 可见；子进程输出本有 8KB/20s 帽）；docstring 与
  README 同步（注明刻意不设该选项的原因）。评审复现命令：对 sshd 实测 ERROR 级
  无 Warning、默认级有——修后该检测信号恢复。
- **回归**：`tests/test_ssh_skill.py::test_password_mode_never_in_argv` 补断言
  `not any(str(a).startswith("LogLevel=") for a in argv)`。

### M5 BACKLOG 头部规模数字失实 + 勘误行自相矛盾
- **改法**：BACKLOG 头部按实测重写（1865 收集：1817+48 默认；1856+9 带
  `qqagent_test`；53 文件），勘误行改为按实际新增文件枚举（+3：test_config_write/
  test_reboot/test_ssh_skill）并声明旧手写口径作废、以实测为准。

### M6 ssh_run builtin 接线零覆盖
- **改法**：`tests/test_tools.py` 注册名单与 superuser 名单各加 `"ssh_run"`——
  删掉 `builtin.py` 的 `register_ssh_skills` 调用即红。

## L 级（可闭环项全修；两项有意保留）

| # | 处置 | 说明 |
|---|---|---|
| L1 日志 reset 增量混排 | **修** | `incremental = after>0 && !d.reset`（web.py）；源码护栏 +2 断言 |
| L2 轮询无在途互斥 | **修** | `logInFlight` 守卫包住 logs 视图的 `load()`（原函数体改名 `doLoad`）；源码护栏 +1 断言 |
| L3 splitlines 行模型差 | **修** | `parse_env_value`/`locate_env_key`/`patch_env_text` 统一按 `\n` 切（CRLF 的 `\r` 保留在行尾）；回归 `test_write_verify_roundtrip_with_unicode_separators`（U+2028/\x0c 值写入→verify 成功） |
| L4 BACKLOG D2-1 计数过期 | **修** | 17/12 → 23/20（实测，含本轮新增 2 条） |
| L5 README 缺「不追补」口径 | **修** | README 日志段补：过滤只作用于新到行、轮转回尾部窗口重取 |
| L6 de09478 测试为源码护栏 | **部分修** | 服务端 reset 契约已有用例（test_web `test_after_cursor_reset_on_rotation`），本批补 L1/L2 源码护栏断言；headless DOM 行为矩阵仍缺——挂账 §6.2 |
| L7 reboot_delay 非有限值 | **修** | `math.isfinite` 护栏（inf/nan/1e400 回退默认）；回归 `TestRebootDelayGuard` |
| L8 web /reboot 回显完整 argv | **修** | 第一段响应 `cmd` 与审计同口径只回 `argv[:1]`；回归 `test_first_phase_cmd_same_scope_as_audit` |
| L9 死键 + 注释失真 | **修** | 删 `AGENT_REBOOT_UNIT`/`DEFAULT_UNIT`；白名单注释改为「PATH 解析失败 spawn 抛错 → 回退 re-exec」；测试 `_clean_env` 同步 |
| L10 .env.example 缺登记 | **修** | 补 `AGENT_REBOOT_ENTRY` / `AGENT_REBOOT_NOTICE_FILE`（注释形态，无 M7 行内注释陷阱） |
| L11 test_reboot 三缺口 | **修** | ① shutdown 断言补 scheduler kwarg（随 M2）；② `test_web_reboot_never_registers_notice`（scheduled == [None]）；③ LOADER_SCRIPT 加 `BOT_CONNECT_HOOKS` 探针 + `test_reboot_done_notice_hook_registered`（删 `on_bot_connect(_send_reboot_done)` 注册即红） |
| L12 密码不进日志无锁 | **修** | `test_password_mode_never_in_argv` 加 caplog 断言（`s3cret-pw` 真实存在于 env，非恒真陷阱） |
| L13 `--` 位置 + 注释定性 | **修** | `--` 移到 destination **之前**（host 正则允许 `-` 开头，终结本地选项解析）；测试断言 `argv[-3]=="--"`；注释改述为「防 `-` 开头 host 被当选项」 |
| L14 alias 枚举回显 | **有意保留** | 三层超管门内可达（schema 隐藏 + registry 检查 + handler 校验），评审已判「可接受，记录备查」——不改 |
| L15 known_hosts 落 fs 可写工作区 | **修** | `ssh_run` 内校验 `_known_hosts_path().resolve()` 不落在 `workspace_root()` 内，否则拒绝执行并提示改 `AGENT_SSH_KNOWN_HOSTS`；回归 `test_known_hosts_inside_workspace_rejected` |
| L16 hostname `-n/--node` dead entry | **修** | 从 `_HOSTNAME_SAFE_FLAGS` 移除；`test_hostname_query_only` 补负例 |
| L17 参数夹带换行不拒 | **修** | `_denied_for_shell` 加 `\n`/`\r` 拒绝（无 shell 下执行面惰性，堵审计日志伪造多行）；AGENTS.md 沙箱行措辞同步（禁 `|;&><` **与换行**）；全局闸用例 +2 |
| L18 facts 兜底模式误杀面 | **有意保留** | `被\s*@|有人\s*@` 是 M8「第三人称归属句入库」的兜底，误杀方向保守（只丢不错记）——移除会重开 M8 面；与上轮 L2 的权衡挂账 BACKLOG §6.2 一并决策 |

## 验证（主代理实跑，2026-10-02）

- 变异复核：H1/H2/M1/M2 四处在 git worktree 内改坏 → 对应新用例**全部 FAILED**（M1
  首版用例漏抓后修正取证时机，二次变异必红）。
- 默认套件：`pytest -q` → **1817 passed / 48 skipped**（1865 collected）。
- 带库套件：`TEST_DATABASE_URL=postgresql://qqagent:qqagent@127.0.0.1:5432/qqagent_test`
  → **1856 passed / 9 skipped**（生产库 `qqagent` 未触碰）。
- ruff：`ruff check` 全过；`ruff format --check` 148 files 全过（0.9.6 = pin；3 个
  本批改动文件先经 `ruff format` 重排后复检）。
- 依赖：`pyproject.toml` 零变化。

## 遗留 / 降级

- **L6（部分）**：浏览器 headless DOM 行为矩阵未建（CI 无 JS 执行环境），现有
  护栏 = 服务端契约用例 + 源码断言；挂账 BACKLOG §6.2。
- **L14 / L18**：有意保留（见上表），如需重开请在 BACKLOG §6.2 立项讨论。
- **部署面存疑项**（评审 §2 存疑，非本轮可解）：`ss -K` 跨用户 socket 可达面取决于
  进程特权（白名单不变量已收口，与特权无关）；C 步子进程 cgroup 收割行为取决于
  systemd `KillMode`；远端 musl/BSD 对 `--` 的处理（已移到 destination 前，两分支
  均无注入面）。
- 真机 QQ 通道 / 浏览器真机 UI：保持评审声明的未验证面，具备环境时补验。
