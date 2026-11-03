# FIX — a9a28cf..6867995 周期评审 P0 修复记录

> 评审来源：[REVIEW-a9a28cf..6867995.md](REVIEW-a9a28cf..6867995.md)
> 分支：`fix/review-a9a28cf-p0`（基：`main @ be4a8e3` = PR #18 合并 + 评审报告 `45b04cf`）。
> 纪律：AGENTS.md §2——先分级、AC 先行、最小改动、不自动 commit/push、回归用例进被测模块对应主题测试文件。

## 0. 修复总表（P0 七项，逐项一个 commit）

| # | 问题 | 等级 | commit | 判定依据 |
|---|---|---|---|---|
| FIX-1 | cron 一次性语义三处失守（M6/M7） | Medium（放权摩擦/误导回执） | `ac3ecfb` | verified：单次时间被转年度 cron、`trusted` 缺省 True + 立即启用、面板按「还在文件里」判没跑成 |
| FIX-2 | outbox 首建 0644（M12） | Medium（同机可读投递账） | `08e0736` | verified：`open(path,"a")` 走 umask 先落 0644，文件含 target（真实 QQ/群号） |
| FIX-3 | outbox_notice 先删后通知，失败即蒸发（M14） | Medium（丢账） | `08e0736` | verified：`take_uncertain` 读走即删，发送抛错后账目从文件里消失 |
| FIX-4 | reboot 收尾漏三槽位（M17） | Medium（丢数据） | `66ba348` | verified：`_shutdown_everything` 只递 scheduler + 3 连接关闭，插件 on_shutdown 有 stats/archive/growth |
| FIX-5 | 慢更新回执被 stale 吞 + watcher 预算压不过脚本（M2/M3） | Medium（回执丢失） | `62e1747` | verified：判据读 `started_at`、`_TERMINAL_WAIT=180` 与 fetch timeout 180 压线 |
| FIX-6 | /update 与 /reboot 无互锁（M4） | Medium（误回滚） | `62e1747` | verified：`grep update reboot.py` 零命中；execve 同 PID → boot_ready 永不上当 |
| FIX-7 | A4 声称落码实则从未进 main（M18） | Medium（声称失真+风险仍在） | `b883623` | verified：`git log --all -S'port_check' -- agentcore/loop/engine.py` 零命中，HEAD 仍 8 项 |

**验收命令**（本次实跑）：

```bash
.venv/bin/python -m pytest -q                                   # 3863 passed, 77 skipped
.venv/bin/python -m ruff check agentcore plugins tests scripts bot.py   # All checks passed
.venv/bin/python -m ruff format --check agentcore plugins tests scripts bot.py  # 262 files already formatted
.venv/bin/python scripts/check_review_docs.py --check-stats      # [ok]（计数块已 --write-stats 重算：3940 收集）
```

PG 契约面未动（本轮无 store/schema 变更），未跑 `TEST_DATABASE_URL` 套件。

## 1. FIX-1 cron 一次性语义（M6/M7，`ac3ecfb`）

### 改法

`agentcore/skills/cron_skills.py`：

- 单次时间（`parse_when` 给 `run_at`）**拒绝**并指路 `reminder_add`——原本转成
  `分 时 日 月 *` 的年度 cron，界面还标「单次执行」，明年同一时刻原样再触发；
- `cron_job_add` 的 `trusted` 缺省 True → **False**（与 `/cron add` 的放权摩擦对齐），
  schema 描述同步；
- 新任务落盘 `enabled=False`（一句自然语言就 `next_run_at_ms=0` 下 tick 首跑，
  把「创建」和「启用」两步确认压成一步），回执给「/cron run 试跑 · /cron on 启用」；
  `trusted=True` 时回执带 ⚠ 警示。

`plugins/qq_agent_adapter/web.py`：待决定区「待处理的一次性任务」判据从「还在文件里 +
开着」改为 **`state.json` 的 `lastStatus != "ok"`**——`deleteAfterRun` 从来没有删除实现
（jobs.json 已回归纯配置、运行态只写 sidecar），跑成的 job 一直躺在文件里，面板永久假阳性。

### 回归

- `tests/test_cron_skills.py`：更新 1 条既有用例（原断言就是缺陷本身：trusted 默认开 +
  立即启用）；新增 `test_one_shot_time_is_rejected`（拒绝 + 指路 reminder_add + 不落盘）、
  `test_trusted_can_be_requested_explicitly`（显式 trusted 仍可用 + ⚠ 警示）。
- `tests/test_web_overview.py::TestPendingOneShotJobs`：新增
  `test_succeeded_one_shot_drops_off`（lastStatus=ok 不再挂榜）、
  `test_failed_one_shot_stays_listed`（失败仍挂榜）。

### 变异复核

| 变异 | 结果 |
|---|---|
| 单次时间改回转年度 cron | `test_one_shot_time_is_rejected` **红** |
| 面板判据改回「还在文件里」 | `test_succeeded_one_shot_drops_off` **红** |

### 文档

`docs/features-interaction.md`（待决定区判定口径）、`docs/operations.md` 无需改动。

## 2. FIX-2/3 outbox 首建 0600 + 通知两阶段（M12/M14，`08e0736`）

### 改法

`agentcore/outbox.py`：

- `record()` 的 `open(path, "a")` → `os.open(path, O_WRONLY|O_CREAT|O_APPEND, 0o600)`
  （带 `O_NOFOLLOW` 拒符号链接），与 `_rewrite` / notify_budget 的「创建即收紧」同口径；
- 新增 `peek_uncertain()`（读不删）/ `drop_uncertain()`（删）两个原语；
  `take_uncertain()` 保留为两者组合（既有调用方/测试不变）。

`plugins/qq_agent_adapter/outbox_notice.py`：`_send_pending_notice` 改两阶段——
peek 读出 → **至少通知到一个管理员**才 drop；全失败保留，下次启动重试。

### 回归

- `tests/test_outbox.py`：新增 `test_first_create_is_0600`（`stat` 断言 0600）、
  `test_notice_failure_keeps_the_ledger`（全失败保留 + 协议恢复后销账）、
  `test_peek_and_drop_are_separate`（peek 不删、drop 才删、不碰 sent 行）。

### 变异复核

| 变异 | 结果 |
|---|---|
| `record` 改回 `open(path,"a")` | `test_first_create_is_0600` **红** |
| 通知改回「take 读走即删」 | `test_notice_failure_keeps_the_ledger` **红** |

## 3. FIX-4 reboot 收尾补三槽位（M17，`66ba348`）

### 改法

`plugins/qq_agent_adapter/reboot.py`：`_shutdown_everything` 递 `shutdown_agent` 时补
`stats_flush` / `archive_flush` / `growth_aclose` 三个槽位（都赶在 `memory.aclose` 之前，
与插件停机钩子 `__init__.py` 同序）。reboot 经 execv/_exit 退出，on_shutdown 永不执行，
此前每次 /reboot 丢 ≤1 个 flush 周期的群统计与原文归档、蒸发在飞的成长提议。

### 回归

- `tests/test_reboot.py::TestShutdownEverything`：新增 `test_flush_slots_wired`
  （递进去的正是 driver 上那三个对象——绑定方法每次访问都是新对象，`is` 不成立，
  故用「调用后作用到对应实例」的行为断言）与 `test_flush_slots_absent_driver_ok`
  （driver 无这些属性时槽位为 None，不炸）。

### 变异复核

| 变异 | 结果 |
|---|---|
| 删 `stats_flush` + `growth_aclose` 槽位 | 两条新用例均 **红** |

## 4. FIX-5 慢更新回执（M2/M3，`62e1747`）

### 改法

`plugins/qq_agent_adapter/update_cmd.py`：

- `is_stale_notice` 判据从 `started_at`（更新开始时刻）改为 **`finished_at`**（终态落盘
  时刻，缺省回退 started_at）。fetch 180s + merge 120s + uv sync 900s + 冒烟 180s 合法
  超过 600s 窗口时，旧判据把**本次**的成功/回滚当旧账跳过，`notified` 永不置位——
  之后每次重启同样 stale，回执永远丢失；
- `_TERMINAL_WAIT` 180 → **1500**（覆盖重启前最长合法链 1380s + 余量）。旧值与 git fetch
  的 timeout 压线，fetch 挂满后脚本约 182s 才写 failed，watcher 早已静默退出，
  「更新前失败」的回执面整个丢失。

`scripts/self_update.py`：`write_state` 对终态（`up_to_date/refused/failed/rolled_back/ok`）
自动补 `finished_at`（`setdefault`，显式值不覆盖；运行态不补）。

### 回归

- `tests/test_update_cmd.py`：`test_slow_update_result_is_not_stale`（慢更新不判 stale +
  真旧账仍 stale + 缺 finished_at 回退旧行为）、`test_watcher_outlives_slow_fetch_failure`
  （虚拟时钟跑**真实** `_watch_result`：终态 182s 处才出现，必须补发）。
- `tests/test_self_update.py`：`test_terminal_states_get_finished_at`（终态补、运行态不补、
  显式值不覆盖）。

### 变异复核

| 变异 | 结果 |
|---|---|
| stale 判据改回 `started_at` | `test_slow_update_result_is_not_stale` **红** |
| `write_state` 去掉自动补 | `test_terminal_states_get_finished_at` **红** |
| `_TERMINAL_WAIT` 改回 180 | `test_watcher_outlives_slow_fetch_failure` **红** |

### 文档

`docs/operations.md`「生产/开发分离与在线更新」补「回执的两条时间边界」小节。

## 5. FIX-6 /update 与 /reboot 互锁（M4，`62e1747`）

### 改法

`plugins/qq_agent_adapter/update_cmd.py` 新增 `update_in_progress()`：判据是
`data/state/update.lock` 的 **flock**（脚本从 `acquire_lock()` 到退出全程持锁），不是
state 里的 `restarting`——脚本被杀（机器重启清掉 user 单元）后那个状态会**永远**留下，
按它拦截会把 /reboot 这个恢复手段永久挡死。锁空且 state 停在 `_RUNNING` 时留一条
WARNING（这条更新不会有回执了）。

两个入口都查它：

- QQ `/reboot`（`reboot.py::handle_reboot`）：拒绝且**先于**「收到重启指令」回复——
  别让用户以为重启已经开始了；
- web `/api/reboot`（`web.py`）：409，且放在两段确认**之前**（不为注定被拒的重启烧
  confirm nonce）。

`reboot.py` 新增 `update_block_reason()`（懒导入 + 探测失败放行：重启是恢复手段）。

### 回归

- `tests/test_update_cmd.py::TestUpdateInterlock`：持锁拦 / 锁放行 / 僵尸态放行+WARNING /
  早期无 state 也拦（关掉 spawn 到写 restarting 之间的竞态）。
- `tests/test_reboot.py`：QQ 入口 `test_blocked_during_update`（拒绝先于回复、不调度重启）、
  web 入口同名用例（409 + detail）。

### 变异复核

| 变异 | 结果 |
|---|---|
| 判据改看 state 的 `restarting` | `test_dead_script_state_does_not_block` + `test_early_phase_also_blocks` **红** |
| 摘掉 QQ 入口检查 | QQ `test_blocked_during_update` **红** |
| 摘掉 web 入口检查 | web `test_blocked_during_update` **红** |

### 文档

`docs/operations.md` 补「`/reboot` 与 `/update` 互锁」段（含为什么看 flock 不看 state）。

## 6. FIX-7 五个主机状态技能补围栏（M18，`b883623`）

### 改法

`agentcore/loop/engine.py`：

- `_UNTRUSTED_TOOL_RESULTS` 8 → **13**：补 `system_status`/`proc_detail`/`disk_usage`/
  `port_check`/`service_status`（ps argv / unit Description / 监听对端地址是主机侧可控
  文本，未围栏直达持 `proc_kill`/`run_shell`/`fs_delete` 的工具循环）；
- trusted 通道的直通名单从循环内联字面量抽成模块级 `_TRUSTED_LOCAL_TOOLS`（十个工具）——
  两份独立字面量的结构性漂移正是上一轮 A4「声称落码实则丢失」的温床，抽出来同框可审计；
- 调用点改引该常量。

### 回归

- `tests/test_engine.py::TestSearchResultFence::test_superuser_tool_results_are_fenced`
  parametrize 5 → **10** 项（五个新技能各一条围栏断言）；
- 新增 `TestSuperuserTrustedChannelEngine::test_local_ops_tools_trusted_exempt`：
  十个工具逐个钉住「trusted 直通 + 非 trusted 围栏」。

### 变异复核（上一轮声称的 5 failed 本次**实际复现**）

| 变异 | 结果 |
|---|---|
| 围栏名单删 5 个新名字 | `test_superuser_tool_results_are_fenced[*]` **5 failed** |
| 直通名单删 5 个新名字 | `test_local_ops_tools_trusted_exempt[*]` **5 failed** |

### 文档

- `review/FIX-review-a9a28cf.md` §A4 补**事后修正**（原「改法」与「变异 5 failed」为失真
  声称：hunk 落 main 时丢失，`git log --all -S'port_check' -- engine.py` 零命中）；
- `AGENTS.md` §4 trusted 口径补上这五个技能。

## 7. 遗留 / 降级项（诚实记录）

| 项 | 状态 |
|---|---|
| P1：M1（user_mode preflight）、M5（回滚备份）、M8（围栏 neutralize + 措辞）、M9（jobs 写锁）、M13（预算预占或文档明示）、M15/M16/M19（文档与流程补账）、M10（`key=int` + 契约测试 ≥10 源） | **未修**，留待下一迭代（M19 的流程账部分由本文件与 BACKLOG §6.18 补） |
| P2：M11 + L 级批量计划 | 未修 |
| 评审报告 `45b04cf` 本身未 push（用户在本地 main 提交） | 已并入本分支；push 本分支时一并带上 |
| 生产善后：PR #20 合并后把 `AGENT_DEBOUNCE` 从 `0` 改回 `3.0` 并重启 | **已执行（2026-11-04）**：随 1.0.6 发布（PR #20 合入 `main` = `df1d7de`），生产 `/home/xiaji/apps/agent-demo` 已 ff 到该提交并 `uv sync --locked`（`agent-demo==1.0.6`）；`.env` 的 `AGENT_DEBOUNCE` 已由 `0` 改回 `3.0`（改前备份 `/tmp/opencode/prod.env.bak-20261104-pre36`）。重启走同用户 `SIGTERM` + systemd `Restart=always`（免 sudo），新进程 pid 2929778、bot 已连接、零 ERROR；`AGENT_DEBOUNCE=3.0` 经生产 venv 跑同一段 `_debounce_seconds()` 取值代码实证生效 |
| PG 契约套件 | 本轮无 store/schema 变更，未跑 `TEST_DATABASE_URL` 套件 |

## 8. 与生产停摆事故的衔接

本轮评审 §0「无 H，不阻断发布」的结论需订正：1.0.5 上线后私聊全静默的根因
（PR #17 的 `1301348` 把 `_get_debouncer` 改为 `async def` 却漏改调用点）属本周期引入，
被「无评审产物（M19）+ 测试全走 direct 分支（覆盖断链）」双重放过。修复在 PR #20
（`fix/turn-trace`，调用点修正 + `TestDebouncerDispatch` + `logging_setup.bridge_loguru()`）；
框架日志只进 journald 不进 `data/logs/agent.log`，排障必须同时看
`journalctl -u agent-demo`——这两条已由本周期后续提交补记。
