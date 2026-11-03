# agent-demo 近期 Commit 评审报告

**评审范围**：`a9a28cf..6867995`（48 个 commit；上游 1.0.4 修复批次之后 → 1.0.5 定稿，2026-11-02..2026-11-03）
**评审日期**：2026-11-04
**评审方式**：5 个并行子代理分线审查（线1 在线更新链 / 线2 信任模型与 cron·技能面 / 线3 记忆星云与时间衰减 / 线4 调度器 superagent 优化 / 线5 修复复核·群统计·CI·文档）+ 主代理逐条实证（默认与 PG 双套件实跑、ruff/pyright/RUN_PERF、3 组 worktree 变异、git 考古、`/tmp` 权限复现）
**工作区状态**：干净（评审开始与结束时 `git status` 均无改动；本轮产物仅本报告与 `REVIEW-WORKFLOW.md` §9 索引一行）

> 评审纪律：子代理只读，结论不照单全收；下文每条 H/M 均经主代理读码复核，**已复现**与**推演**严格区分。

---

## 0. 结论摘要

| 维度 | 结论 |
|---|---|
| **最高风险** | **无 H**；**M×19**（其中 1 条为「上轮修复未落 main」，1 条为流程断链） |
| 在线更新链（b60488e 等 7 commit） | 核心安全设计成立：闸门双向传递、argv 零 shell、回滚目标自持、boot.json `pid+sha` 双条件、独立 cgroup。缺口集中在**可用性/回执可靠性**：user_mode 部署白跑必回滚、慢链与慢失败回执被吞、/reboot 无互锁、回滚 `reset --hard` 语义与被放行的运行态改动相悖 |
| 信任模型（a457e17 / 0b59c53） | 判定边界精确（群聊 superuser 不豁免）、权限身份与数据身份分离干净、trusted/owner fail-closed 四象限有测试；变异「豁免无条件」→ **13 红**。M 集中在 cron 一次性语义（`deleteAfterRun` 死字段 + 「单次执行」标签年重复 + trusted 默认 True 立即启用）、trusted 通道手拼围栏未过 `neutralize_fence_lookalikes`、jobs.json 读-改-写无互斥 |
| 记忆星云/衰减（2abca82 / f944f07） | 隐私契约三层 canary 全过（聚合面无正文/embedding/群号，用户标签 4 位哈希，详情审计不含正文）；衰减「只改排序不改资格」有变异测试锁。M 为 **kb 抽样簇排序键双实现分歧**（str vs int，≥10 源必现；契约测试只建 1 源，盲区）与 facts 层无抽样上限 |
| 调度器优化（795ff2a / 1523c6f） | outbox 是 append-only 记账、无发送/重放能力，**双发风险证伪**；乐观锁与两段确认叠加顺序正确。M 为 outbox.jsonl 首建 **0644**（已实测复现，同 commit 的 notify_budget 已是 0600）、预算 check→record 跨 await TOCTOU、悬空账目「先消费后通知」 |
| 声称核对 | **1301348 5 项失真**（人格三项在 main 无对应机制、compose_init 站点不存在、KEEP=0/find|wc 当时未修）；**CHANGELOG 1.0.5 漏记 6 组用户可见变更**（第三次同型）；**上轮 A4 修复在 main 上不存在**（FIX 声称「8→13 + 变异 5 failed」） |
| 测试实跑 | 默认套件 **3826 passed / 77 skipped**（收集 3903，与 9dc5a6a DOCS-STATS 吻合）；PG 空库套件 **3918 passed / 9 skipped**；ruff check + format 全绿（262 文件）；pyright **0 errors**；RUN_PERF 9 passed、perf 基线无劣化；`check_review_docs.py --check-stats` 绿 |
| 变异抽查（主代理） | 3 组全部被抓：trusted_channel 无条件豁免 → test_engine **13 红**；群统计 split-try 回退 → **1 红**；星云详情泄漏 user_id → **1 红** |
| CI @6867995 | CI、Docker arm64、Docker Publish 三工作流均 **success**（API 匿名核对） |
| **门禁结论** | **无 H，不阻断发布**；M 批建议在下一版本前修（P0 见 §6）；未验证面 6 项已按 §8.3 声明 |

---

## 1. 已实证的问题

### M1【已验证（代码现场）+ 链路推演】user_mode 部署下 /update 白拉代码、必然回滚，且检查发生在改代码之后

`scripts/self_update.py:129-134` 的 `main_pid()` 查的是 **system 总线**：

```python
def main_pid() -> int | None:
    proc = _run(["systemctl", "show", "-p", "MainPID", "--value", UNIT], timeout=10)
```

而 `scripts/deploy.py` 支持 `--user` 部署（`deploy.py:580-591` 用 `systemctl --user`），且 `deploy.py:175` 对两种形态**无条件**注入 `Environment=AGENT_UPDATE_OK=1`。user_mode 下：handler 闸门过 → 脚本闸门过 → fetch/merge/`uv sync` 全部执行 → `_restart_and_wait` 拿不到 MainPID → **代码已被 merge 才发现无法重启** → `reset --hard` 回滚；回滚后的重启同样拿不到 MainPID。同机若残留同名 system unit，还会拿到错误 unit 的 PID 并发 SIGTERM。

**影响**：deploy.py 官方支持的部署形态下，/update 是「白拉代码 + 白同步两遍依赖再滚回去」；commit 声称的前置 fail-closed 对此项不成立（检查时点太晚）。
**建议**：preflight 增加 `main_pid()` 可得性探测，失败在 fetch 之前拒绝；user_mode 按 unit 位置补 `--user`。

### M2【已验证（代码 + 常量）】慢更新（>600s）的终态回执被 stale 窗口吞掉，且永不补发

`plugins/qq_agent_adapter/update_cmd.py:100-112` 用 **updated 开始时刻**与新进程 boot 时刻比较，窗口 `_STALE_WINDOW = 600.0`（:50）：

```python
started = state.get("started_at")
...
return float(started) < float(boot_started) - window
```

而 `scripts/self_update.py:253` 的 `uv sync` 超时上限是 **900s**。fetch+merge+sync+停机+启动总计合法地超过 600s 时，新进程 `_notify_after_boot` 把**本次**成功/回滚结果判为 stale，只 `logger.info("update notice skipped (stale result)")`；`notified` 永不置位，之后每次重启同样 stale → **永远无回执**。用户只收到「🔄 开始更新……完成后会在这里回执」。

**建议**：stale 判定改用终态落盘时刻（`finished_at`），或把窗口放到覆盖两遍 `uv sync` 的最坏链（≥2000s）。

### M3【已验证（常量算术）+ 推演】旧进程 watcher 的 180s 预算压不过脚本自身的超时预算，慢失败回执丢失

`update_cmd.py:47` `_TERMINAL_WAIT = 180.0`（:233 起算），与 `self_update.py:327` `git fetch` 的 `timeout=180` **压线相等**（再加 merge 120s、多次子进程开销）。fetch 挂满后脚本才写 `failed`（≈182s+），watcher 已静默退出；此时 bot 没重启，`on_bot_connect` 不会触发，拖到下次重启又落入 M2 的 stale 窗口。

**影响**：「更新前失败」正是设计指派给旧进程 watcher 的回执面，其时间预算小于被观察对象的超时预算。
**建议**：`_TERMINAL_WAIT` ≥ 各步超时之和下界（≥420s），或改「state 文件 mtime 仍在更新」的存活判断。

### M4【已验证（代码现场）】/update 与 /reboot 无互锁：更新窗口内重启必触发误回滚且状态失真

`grep update plugins/qq_agent_adapter/reboot.py` 无任何命中；`update_cmd.py:183-184` 用 `--no-block` 让 handler 立即返回。更新窗口内管理员 /reboot（`os.execve`，**PID 不变**）→ `self_update.py:240` 的 `if pid and pid != old_pid and sha == target_sha` 因 PID 相同永不满足 → 90s 后 `reset --hard` 回滚，状态写 `rolled_back`（真实原因是并发重启）。flock（:179）只挡并发脚本，不覆盖 /reboot 与 web `/api/reboot`。

**建议**：`perform_reboot` 前读 `update.json`，`_RUNNING` 时拒绝/延迟；或 boot_ready 增加「pid 相同但 sha 已变」的 execve 形态（需与旧进程自写 marker 区分）。

### M5【已验证（代码现场）】回滚 `reset --hard` 丢弃被放行的已跟踪运行态改动

`self_update.py:52` `RUNTIME_PREFIXES = ("data/", "agentcore/personas/")`，`:166` 把这些前缀下的**已跟踪文件**修改排除出「dirty 阻塞」；但回滚 `:284` `git("reset", "--hard", prev)` 会把这些修改全部丢弃。经核实丢失面：web 人格编辑写未跟踪的 `data/personas`（另有 `data/backups/personas`）、技能安装是新增未跟踪文件——**不丢**；会丢的是手工改过的 `data/skills/*.yaml` 示例技能、`agentcore/personas/assistant.md` 等出厂模板（`git ls-files` 确认被跟踪），无备份、静默。

**建议**：`_rollback` 前对 RUNTIME_PREFIXES 内的已跟踪修改 `git stash` 或落 `data/backups/update-rollback/`；文档写明取舍。

### M6【已验证（代码现场）】cron「一次性任务」语义不存在：死字段 + 「单次执行」标签实为每年重复 + trusted 默认 True 立即启用

两个子代理从不同角度独立撞出同一根因，合并如下。

1. **单次创建转成年度 cron**（`agentcore/skills/cron_skills.py:59-68`）：

```python
if run_at:
    dt_val = dt.datetime.fromtimestamp(run_at, tz=tz.zoneinfo())
    cron_expr = f"{dt_val.minute} {dt_val.hour} {dt_val.day} {dt_val.month} *"
    return cron_expr, f"{parsed['human']}（单次执行）", None
```

「10 分钟后」→ `26 2 4 11 *`，**每年** 11-04 02:26 再触发，却标「单次执行」。

2. **`deleteAfterRun` 全仓无删除实现**：`jobs.py:90/114/247/373` 只有字段/序列化；`update_state`（`jobs.py:437-461`）只写 sidecar 并明示「不触碰 jobs.json」；`agent_turn._run_job_locked`（:303-308）跑成只回写状态。`web.py:331-332` 却把它当判定前提（「还在文件里 + 开着 = 没跑成」），`docs/features-interaction.md:290-291` 同样声称会被删掉 → **待决定区假阳性只增不减** + 一次性 job 变年度周期。

3. **`cron_job_add` 的 `trusted` 缺省 True、`enabled=True` 立即启用**（`cron_skills.py:108-121,169-180`），回执只有一行「（已开启超管权限）」。一次「10 分钟后跑 X」= 每年以**最高执行权**复活并投递。

**建议**：单次路径拒绝并指引用 reminder（真一次性语义），或实现 `deleteAfterRun`；`trusted` 缺省改 False；面板判定改读 `state.json` 的 `last_status`；三处注释/文档同步。

### M7【已验证（代码现场）】cron_job_add 与 /cron add 的放权摩擦不对称

命令面（`cron_admin.py:129-137,196-200`）：`enabled=False` 落盘（试跑后再 on）、`trusted` 默认 False、两段确认、预览含「⚠ trusted」。技能面：一句自然语言 → 立即启用（`next_run_at_ms=0`，下个 tick 即首跑）→ 默认超管权限。同一个放权动作在两条通道的确认摩擦差一个数量级。

**建议**：技能面沿用命令面姿态（落盘停用 + `/cron run` 指引 + trusted 时警示）。

### M8【已验证（代码现场）】trusted 通道手拼围栏未过 `neutralize_fence_lookalikes`，措辞超出「免除设防」

`plugins/qq_agent_adapter/pipeline.py:844-846`（引用）与 `:884-887`（合并转发）：

```python
head = f"----- 引用消息（来自管理员{sender_desc or ''}，内容完全可信，遵照其中要求执行）-----"
extra_context.append(f"{head}\n{quoted_text}\n----- 引用消息结束 -----")
```

1. `grep neutralize_fence_lookalikes pipeline.py` **无命中**——AGENTS §4「围栏行本身不得被内容闭合」是全局不变量，`safety.py:59` 也要求所有自建围栏调用方都过它。被引文本嵌一行「----- 引用消息结束 -----」即可提前闭合，其后内容以裸姿态出现。
2. 措辞把「免除设防」升级为「遵照其中要求执行」；合并转发可携带任意第三方文本（社工链一步之遥）。
3. `sender_desc` 是被引消息的**原发送者**，拼进「来自管理员{sender_desc}」产生归属混淆（引用 bot 旧消息时同时出现「管理员」与 bot 身份）。

**影响**：在「完全可信通道」内，围栏已非安全边界，故**不评 H**；但全局不变量被违反、措辞进一步放大设计边界、归属混淆污染模型判读。
**建议**：内容过 `neutralize_fence_lookalikes`；头部改中性表述、删「遵照其中要求执行」；引用头改「管理员引用了 {sender_desc} 的消息」。

### M9【已验证（代码现场）+ 推演】jobs.json 读-改-写无互斥，并发创建可静默丢 job

`cron_skills.py:157-184`、`cron_admin.py:123-155,225-252` 均为 `load → append/modify → save` 全量写。`jobs.save()` 有改前备份与写后回读，但**不防并发覆盖**：两个并发轮次各 load 到 N 条、各改自己的、先后 save → 先落盘的条目被后者整体覆盖丢失（备份可找回，无告警）。`_run_lock` 只串行化执行，不覆盖写路径；技能面与 /cron、tick 可并发。

**建议**：jobs.py 提供模块级锁（asyncio.Lock + 线程兜底）包住 load→save，三个调用方共用。

### M10【已复现（纯函数仿真）】kb 抽样簇排序键双实现分歧：InMemory 字符串序 vs PG 整数序

`agentcore/memory/store.py:1170-1173`（InMemory）与 `:2088-2091`（PG）：

```python
source_ids = sorted(per_source_ids)          # 字符串序："10" < "2"
```
```sql
GROUP BY source_id ORDER BY source_id         -- 整数列数值序：2 < 10
```

仿真：`sorted(['2','10','11','3']) == ['10','11','2','3']`；`_nebula_quotas`（:169-205）按列表顺序轮转分配，**预算紧张时**（sum(min(total,40)) > 3000）两实现给不同源分配不同配额 → **抽样出的 chunk 集合不同**；预算宽松时仅布局顺序不同。生产 71 个知识源必然落入分歧区。契约测试 `test_store_contract.py:826` 只建 **1 个源**，永远测不出。

**建议**：InMemory 侧 `sorted(per_source_ids, key=int)`（PG 侧 `source_id` 为整数列，已确认）；契约测试补 ≥10 源的排序断言。

### M11【已验证（代码现场）】facts 层完全无抽样上限，payload 与前端交互随数据量无护栏

`store.py:1164`（InMemory）与 `:2085-2087`（PG `SELECT id, user_id FROM facts`）均取**全量**，`nebula.py:105-116` 全部落点（`shown = len(ids)`）。kb 层有 `max_chunks=3000`，facts 没有；`web.py:4232` 文案自认「全部上屏」（刻意设计），但 `web.py:4100-4112` 的 `nebPick` 对全部 points 逐点投影、hover 路径无 rAF 节流。facts 增长到万级后刷新响应体与鼠标帧率退化，且无告警阈值。

**建议**：给 facts 层加同型 per-user 抽样上限；hover 路径套 rAF 节流。

### M12【已复现（/tmp 实测 0o644）】outbox.jsonl 首次创建 0644，与同 commit 刚修的「创建即 0600」纪律矛盾

`agentcore/outbox.py:70-74`：

```python
with open(path, "a", encoding="utf-8") as f:   # O_CREAT mode 0666 & ~umask
```

实测（umask 022）：首次创建 **0o644**；文件内容含真实投递目标（QQ 号/群号）。同 commit 795ff2a 恰恰为同类状态文件合并了 `open_part_0600`（`notify_budget.py:93-106`，注释原文即「这些状态里含 target……对同机其它本地用户可读」）。只有被 `_rewrite` 一次后才变 0600；`tests/test_outbox.py` 无权限断言（对比 `test_notify_budget.py:187/197`）。

**建议**：首次创建改 `os.open(O_WRONLY|O_CREAT|O_APPEND, 0o600)` + `os.fdopen`（沿用 Windows 容错口径），补权限用例。

### M13【已验证（代码现场）+ 推演】全局打扰预算 check→record 跨 await 的 TOCTOU，并发流可超发

三处闸门都在投递**前** check、送达**后** record：`push.py:572-584`（`_cap_reached` → `send_once` → `_bump_delivered`）、`nudge.py:1667/1760`、`agent_turn.py:231/298`。`NotifyBudget._lock` 只保护单方法内部，check 与 record 之间隔着整个 `await sink.send_once(...)`；push/nudge/agent_turn 是三个独立 interval job，同 loop 并发。同一目标多路同时到点：两边 check 都看到 `spent=limit-1` → 双双送达 → 超限 1。

**影响**：有界超发（并发窗口内条数），是防骚扰预算而非安全边界，但「天花板」声称在并发下不严格成立。
**建议**：check 通过即预占一格（uncertain/failed 回滚），把判定+占用收进锁内；或文档明示软约束。

### M14【已验证（代码现场）】outbox_notice「先消费后发送」：通知失败 = 悬空账目永久丢失

`plugins/qq_agent_adapter/outbox_notice.py:56-74`：

```python
entries = outbox.take_uncertain()   # 读走即删（原子消费）
if not admins:
    logger.warning("...未配置 SUPERUSERS，无法通知", len(entries)); return
try:
    await bot.send_private_msg(...)
except Exception:
    logger.exception("...发送失败", uid)   # 两条失败路径下账已销
```

未配 SUPERUSERS / 发送异常（bot 刚重连时并不罕见）时，uncertain 行已从文件删除，下次启动不再认账，web 面板也看不到（`recent()` 只读当前文件）。模块 docstring 目标「新进程启动后能拿到悬空清单通知主人」在这两条路径不成立。

**建议**：两阶段——先 `recent()` 过滤 uncertain → 通知 → 全部成功后 `take_uncertain` 清账；或失败回写。

### M15【已验证（git show 现场）】1301348 的 commit message 有 5 项「声称面 > 落码面」

| # | 声称 | 核实 |
|---|---|---|
| a | 人格三项（`.default_persona` marker 的 `_read()` 兜底、`ABOUT.txt` 空行守卫、`save()` 两文件非事务告警） | **main 无对应机制**：`grep -rn 'default_persona\|ABOUT' agentcore/personas/` 为空；main 人格是单文件 `data/personas/<name>.md`（`manager.py:215-260` 单文件原子写），这些是分支 a7e221b 的目录布局 |
| b | 「创建即 0600」含 `compose_init.py`（模板与兜底两分支） | **`scripts/compose_init.py` 在 main 不存在**（`git log --all --diff-filter=A` 仅 UGREEN 分支）；落 main 的站点为 6 个 |
| c | 备份脚本 KEEP=0 假成功「已改为 case 拒 0」、`find\|wc` 失败「已显式接住」 | **1301348 的树仍是旧形态**（`git show 1301348:scripts/backup_usb.sh` 的 case 无 `| 0)`，尾部仍嵌算术）；两项直到 **89b7eaa** 才落（HEAD `:30-35`、`:206-211` 已正确） |

**影响**：接手者按 message 检索这批修复会找不到本体；「评审候选全部修掉」按字面不成立。
**建议**：在对应 FIX 文档补「事后修正」节（照 e019db6 先例），逐项标注「落 main / 仅分支 WIP / 后续 commit 补落」。

### M16【已验证（grep 实测）】CHANGELOG 1.0.5 漏记 6 组用户可见变更（同型第三次复发）

`grep -cE '在线更新|/update|时间衰减|自然语言' CHANGELOG.md` = **0**。1.0.5 节缺：① `/update` 在线更新全链（7 commit）；② cron 自然语言创建 + trusted；③ superuser 可信通道语义（**信任边界变化**）；④ 长期事实时间衰减（排序行为变化）；⑤ 记忆星云视图主体（只记了尾部的双指缩放）；⑥ cron 装配接线断裂修复。

**影响**：从 1.0.4 升级看不到最大新增功能；信任边界变化不进 CHANGELOG 对部署者不可见。
**建议**：补六组条目；把「CHANGELOG 漏记」并入 `check_review_docs.py` 的静态检查面。

### M17【已验证（代码现场）】/reboot 与 web `/api/reboot` 收尾不收群统计/归档/growth，与「停机 flush 不丢」声称不符

`plugins/qq_agent_adapter/reboot.py:301-308` 的 `shutdown_agent` 只传 `scheduler` 与 `extra = [_close_shared_llm, _close_search, _close_wiki]`，**无** `stats_flush`/`archive_flush`/`growth_aclose`；而插件停机钩子 `__init__.py:1146-1155` 三者齐全。注释自认「这是 reboot 路径**唯一**的收尾」。

**影响**：每次 /reboot（用户主动重启主路径）丢群统计与原文归档各 ≤1 个 flush 周期（默认 30s），growth 在飞回顾提议蒸发。属历史存量，但本周期「growth.aclose 接进 lifecycle」（1301348）只闭合了三条停机路径之一。
**建议**：reboot.py 的调用对齐 `__init__.py` 三槽位传参（各一行 getattr）。

### M18【已验证（git 全历史 + 代码）】上一轮 A4 修复未落 main：`_UNTRUSTED_TOOL_RESULTS` 声称「8→13」实为 8，风险仍在

`review/FIX-review-a9a28cf.md`（§A4、索引行）与 `da12779` commit message 均声称把 `system_status`/`proc_detail`/`disk_usage`/`port_check`/`service_status` 加入 `_UNTRUSTED_TOOL_RESULTS`（8 → 13 项），并称「变异：删 5 个名字 → 5 failed」。实测：

- HEAD `agentcore/loop/engine.py:103-113` 仍是 **8 项**（search_web/search_multi/ssh_run/run_shell/run_command/log_tail/docker_logs/docker_stats）；
- `git log --all -S'port_check' -- agentcore/loop/engine.py` **零命中**（该字符串从未进过此文件，任何分支都没有）；
- `da12779:agentcore/loop/engine.py` 同样是 8 项；`test_engine.py` 无这些技能的围栏断言；技能实现内也无围栏。

**影响**：原 A4 的 L 级风险（主机侧可控文本——ps argv / unit Description / netstat 对端地址——不过围栏直达持 proc_kill/run_shell 的工具循环）**仍在**；FIX 的「变异 5 failed」不可复现，属失真声称。疑似 UGREEN 分支 WIP 的 hunk 在落 main 时丢失（与 REVIEW-pr6 记录的「覆盖式 merge 丢修复」同族）。
**建议**：重新落码 + 按 5 个技能各写一条围栏断言；FIX 文档补事后修正。

### M19【已验证（目录 + grep）】流程：1301348 的「fork 六路评审第二轮」无报告、无 FIX 文档、未入 §9 索引

`1301348` 自述「上游 a9a28cf 评审 + 本 fork 六路只读子代理并行评审 + 主代理自审」，但 `review/` 下既无对应 `REVIEW-*`，也无 `FIX-*`（`git diff a9a28cf..HEAD -- review/` 仅新增 4 个文件，均属其他轮次）；`REVIEW-upstream-a9a28cf.md` 只覆盖上游 + 差异面。§9 索引因此在本轮入口处断裂：**本轮「衔接复核」缺少原始发现清单，只能以 commit message 为输入**（而 M15 已证其失真 5 项）。

**建议**：补一份报告（含发现清单与处置）入索引，或在 §9 明确标注该轮「仅 commit 记录、无报告」。

### L 级（22 条，按 §3 收敛规则给批量关闭计划）

> 单轮 L > 15 触发收敛要求；下列按「随 M 同批修 / 独立修 / 显式遗留」三分，同一条不得连续两轮遗留。

| 编号 | 位置 | 问题 |
|---|---|---|
| L1 | `update_cmd.py:125` | bot 侧 state tmp 固定名，与脚本侧 `mkstemp` 不一致，连发 /update 可写出交错 JSON |
| L2 | `boot_marker.py:28` | boot.json 相对 cwd，update_cmd/self_update 用 ROOT 绝对路径；非 unit 启动时读写错位 |
| L3 | `update_cmd.py:81-97` | 回执把 git 输出 tail 拼进 OneBot 消息，`[CQ:` 未转义 |
| L4 | `build_info.py:42` | sha 用 `--short=12`（可更长）与 `[:12]` 严格比较；PID 复用残余误回滚 |
| L5 | `self_update.py:253` | `uv sync` 替换 .venv 期间旧进程仍服务，懒 import 可能混载新旧依赖 |
| L6 | `update_cmd.py` | 脚本启动慢时第二次 /update 双发「开始更新」+ 一条 refused（flock 已兜底） |
| L7 | `agent_turn.py:267-274` | `cron_trusted_run` 审计只进内存 50 条环形，重启即丢、不落日志 |
| L8 | `file_sender.py:350-363` | path 分支鉴权吃投递身份（delivery 覆盖后判 is_superuser）；当前无利用面 |
| L9 | `engine.py:670-677` | 群目标 trusted 轮次注入「当前提问者是 superuser」锚点，无人值守语义错位 |
| L10 | `cron_skills.py` | 导入 jobs 私有 `_slug`；永触发失败回包直出内部错误码；无条数上限（reminder 有 20） |
| L11 | `store.py:1196-1203` vs `:2092-2096` | 0 消息会话：InMemory 不计、PG LEFT JOIN 计入 → `total`/空簇双实现分歧 |
| L12 | `nebula.py:67-70` / `web.py:4859-4863` | 4 位哈希两份拷贝，无一致性断言锁定 |
| L13 | `nebula.py:67-70` | 4 位哈希碰撞面（400 连续号实测 1 对）；仅视觉歧义，不合并不泄漏 |
| L14 | `web.py:4072-4079` | 三指松开一指后 pinch 基准未重建，zoom 跳变（边缘） |
| L15 | `store.py:1170-1172` | InMemory kb 抽样依赖列表序而非 id 序（正常 API 路径恒等，测试替身乱序时分歧） |
| L16 | `nudge.py:1723` | nudge 投递不传 outbox `ref`，悬空清单不可归因 |
| L17 | `web.py:937-978,1029` | `_settings_rev` 逐键全量重读 .env/解析 YAML，被 5s 轮询放大 150+/次 |
| L18 | `web.py:988` | 空 base_rev 直接放行乐观锁（BACKLOG 记载为向后兼容，备案） |
| L19 | `notify_budget.py:318-331` | 预算文件损坏按空处理（fail-open，有告警；利用前提是本地写权限） |
| L20 | `AGENTS.md:150` | 「178 键」漂移：实测 `.env.example` 184 键、`ENV_SPECS` 184 键（双向零差集），簇数 18 未漂 |
| L21 | `scripts/seed_dev_data.py` | 「同一批数据」不含绝对时间戳（now 缺省取当前）；`--truncate` 只清 5 表（help 称「业务表」）；不带 truncate 重复 seed 非幂等 |
| L22 | `test_group_stats.py` 口径 | split-try 的「daily 失败=一行没进库」前提只在 PG 侧成立，InMemory 无事务（理论缺口） |

**批量关闭计划**：
- **随 M 同批修（9）**：L1、L2、L4（M1/M2/M3 同文件）、L7（信任面审计）、L11（契约测试补）、L12（哈希去重）、L16（一行）、L20（数字改 184 或改「以 ENV_SPECS 为准」）、L21（help 文案）。
- **独立修（5）**：L8（鉴权用 perm_user_id）、L9（措辞按 unattended）、L15（`key=int` 与 M10 同一行）、L17（rev 加 mtime 缓存）、L22（docstring 声明）。
- **显式遗留入档（8）**：L3（超管面，攻破 origin 即更大威胁）、L5（在线更新通病，文档标注）、L6（UX，flock 兜底）、L10（工程瑕疵）、L13（视觉歧义）、L14（三指边缘）、L18（有意设计）、L19（低风险）。

---

## 2. 被证伪的发现

| 怀疑 | 证伪依据 |
|---|---|
| 星云详情端点可拉任意用户记录（越权） | 整条数据面挂 `_auth`（401 有测试），与备份下载/日志面同属管理员权限域；AGENTS §4 已明文登记「单条详情 + 每次审计」为既定契约 |
| 衰减导致 top_k>66 召回变少 | 2000 轮随机仿真：分数全序 + 阈值单调切割下两实现等价；退化路径集合不变仅顺序变 |
| 阈值判定可能用加权分 | 两侧过滤都在加权前（`store.py:827` / `:1709`），`test_recall_threshold_stays_cosine` 锁死 |
| PG 窗口函数与 InMemory 每源截取不对齐 | 每源内「id 最小 q 条」两侧一致；**源间排序**才是分歧点（见 M10） |
| /update 回滚会抹掉 web 编辑的人格 | web 写未跟踪的 `data/personas`，另有 `data/backups/personas`；`reset --hard` 不碰未跟踪文件（丢失面收窄为 M5） |
| PID 复用可骗过 boot_ready 判成功 | 需同时 `sha == target_sha`；反向形态（复用 old_pid）只会误回滚，概率可忽略（并入 L4） |
| `.env` 的 GIT_CONFIG_* 可注入 git | systemd-run 用户单元环境独立于 bot 进程；`--setenv` 只传一枚标记 |
| outbox 会重放造成双发 | outbox 是 append-only 记账、无发送/重放能力；`take_uncertain` 只通知不补发 |
| 乐观锁会绕过两段确认 | HMAC 绑定 nonce\|key\|值\|ts，nonce 单次消费；锁内 stale 检查在写盘前，测试锁定 |
| LLM 可夹带 `delivery_group_id` 把文件投到白名单外 | `execute` 具名形参与 `**func_args` 冲突抛 TypeError（副作用兜底，建议显式剥离并加用例） |
| 群聊 superuser 会被误豁免 | `matcher.py:517` 与 `pipeline.py:834` 均含 `not group_id` |
| 「单次」cron 会永触发 | 方向相反：表达式正常触发（问题是每年重复，见 M6） |
| 1301348「修掉除架构债外全部项」 | 按字面证伪（见 M15）；核心修复（群统计/沙箱/识图/配置）经抽查 9 项属实 |
| 星云聚合面可能含正文/群号/embedding | `build_payload` 输出白名单 + 三层 canary（web/store/契约）全过 |
| 三指手势 / 双指缩放实现有泄漏或误触发 | `pointerup/pointercancel` 均 delete + 清理 + 重锚定；捏合期间 `moved=999` 抑制点击 |

**存疑（无法在本环境判定真伪）**：
- 1301348「新增 74 用例全部变异复核」：禁止跑测试复验；旁证矛盾——`test_same_batch_duplicate_rows_merge[pg]` 按 32a702e 自述在其后的 CI 上即红，说明当时复核口径不含 PG 侧。
- `9dc5a6a` 的「3903 收集」：文件数静态精确吻合（`tests/test_*.py` = 102），收集数按只读约束无法精确复算；主代理实跑 3826+77=3903 吻合。
- 414040 短哈希在仓库内歧义，其时点计数未复核（不影响 HEAD 口径）。

---

## 3. 测试与文档状况

### 3.1 实跑数据（本机 Linux，Python 3.12.3）

| 项 | 结果 |
|---|---|
| `.venv/bin/python -m pytest -q` | **3826 passed / 77 skipped**（99.65s；收集 3903） |
| `TEST_DATABASE_URL=...qqagent_test pytest -q`（空库） | **3918 passed / 9 skipped**（110.00s） |
| `ruff check` / `ruff format --check` | All checks passed / 262 files already formatted |
| `pyright agentcore` | **0 errors, 0 warnings** |
| `RUN_PERF=1 pytest tests/test_perf.py -q` + `scripts/perf_baseline.py` | 9 passed；无劣化（最大 1.77x 在噪声下限内） |
| `scripts/check_review_docs.py` / `--check-stats` | 索引完整 / 产物位置 / 文档地图 / 计数块 全 ok |
| CI @`6867995`（GitHub API） | CI success + Docker arm64 success + Docker Publish success |
| 变异抽查（worktree，主代理） | trusted_channel 无条件豁免 → 13 红；split-try 回退 → 1 红；星云 user_id 泄漏 → 1 红（3/3 被抓） |

### 3.2 CI 盲区与结构性问题

- 本周期无 CI 结构性失败；但 **PG 门控用例的“本地绿 ≠ 提交时绿”风险再次出现**：1301348 的群统计契约用例在其后 CI 上即红（32a702e 修复），说明该批提交时未按「动 store/契约必须带 `TEST_DATABASE_URL`」执行。
- 页面 JS（`nebula_dom_test.js`）由 `TestNebulaJs` 挂载，node 缺失时 skip——与 modal 测试同策略，可接受。

### 3.3 文档一致性

| 面 | 结论 |
|---|---|
| `.env.example` ↔ `ENV_SPECS` | 184 = 184，**双向零差集**；本周期 6 个新键与代码默认值一致，可写面声明正确 |
| BACKLOG DOCS-STATS | 102 文件 / 3903 收集，与实跑吻合 |
| CHANGELOG 1.0.5 | **漏记 6 组**（M16） |
| AGENTS.md | 178 键 → 实测 184（L20）；其余不变量行与实现一致 |
| docs/features-interaction.md | 待决定区「跑成会从文件删掉」失真（M6） |
| docs/features-memory-and-kb.md / operations.md | 星云与衰减描述已同步 |

### 3.4 未验证面（§8.3 Quality Gate）

1. **真实 `/update` 端到端**：会改工作树/重启进程，评审纪律禁止；M1–M5 为代码现场 + 链路推演。
2. **user_mode 部署形态**：本机无 user systemd 部署，M1 的具体复现属环境盲区（结论为「涉及 M 且无法本地复现」→ 显式声明，具备环境时补验）。
3. **trusted job 到点以 owner 调 run_shell 的端到端**：需真实 LLM + 权限级别配置。
4. **生产规模下 `nebula_inventory` 墙钟**：本机为空测试库，624MB 生产库按纪律不碰。
5. **移动端真机三指手势**（L14）。
6. **CI 日志正文**：匿名 API 只能取结论，无法核对 step 明细。

**门禁结论**：全部发现中**无 H** → 不阻断发布；上述 2 为唯一「M 级 + 无法本地复现」，已声明为未验证面并在下一轮复核。上轮「未验证面」逐项复核见 §5。

---

## 4. 已验证为「无问题」的关键项

1. **在线更新安全面**：全部 subprocess 为 argv 列表零 shell；`--notify-to` 经 `isdigit()` 校验；五项前置检查齐全且在任何 git 写操作前 return（比声称多两项）；`AGENT_UPDATE_OK` 双向传递（handler → `--setenv` → 脚本复查）；手工 systemd-run 拉脚本被脚本闸门挡。
2. **git 配置注入面**：fetch/merge/reset 只取 objects+refs，不写 `.git/config`、不触发本地未定义 hooks/filter；`core.quotepath=false` 统一。
3. **停机顺序**：/update 的 SIGTERM → nonebot on_shutdown → 全参数 `shutdown_agent`（scheduler → flush → aclose）；本周期 lifecycle 仅新增 growth_aclose 且插位正确。
4. **trusted_channel 判定边界**：`matcher.py:517` / `pipeline.py:834` 的 `is_su and not group_id` 精确；变异无条件豁免 → 13 红。
5. **cron trusted 四象限**：`agent_turn.py:264` `job.trusted and job.owner`；测试覆盖非 trusted 无 perm / trusted+owner 带 perm+审计 / trusted 无 owner fail-closed / 非 trusted+owner 不放权。
6. **权限身份/数据身份分离**：`perm_user_id` 只进 get_schemas/execute；会话键走 session_override、预算走 route_override、growth 由 unattended 跳过。
7. **群目标 trusted 轮次看不到 superuser 私有技能**（sessions 轴优先于 perm，仿真四象限）。
8. **LLM 无法伪造 delivery_*/user_id/owner**：具名形参冲突抛 TypeError（建议显式化）；轮次伪造 owner 被 `parse_jobs` 拒 + 回读校验回滚。
9. **永触发表达式三道闸**：装载期 `_validate_job` + run_job `_NEVER_FIRE` + save 回读。
10. **回显豁免面精确**：`engine.py:1643` 白名单只给本地运维回显，search/ssh/wiki 在 trusted 通道仍围栏；`trusted_channel` 写点仅 matcher 与 agent_turn 两处。
11. **`name` positional-only 不变量**：`registry.py:154` 保持，cron_job_add 的 `name` 参数依赖它。
12. **星云隐私契约**：聚合面无正文/embedding/群号；详情 4 位哈希 + 审计只含 entry+id；三层 canary。
13. **衰减语义**：只改排序不改资格、阈值纯余弦、缺 created_at 中性、PG ORDER BY 纯余弦（HNSW 不失效）、候选窗等价性经 2000 轮仿真。
14. **布局确定性**：blake2b 播种、无内置 hash/全局随机、排序键显式 tuple；刷新不漂移。
15. **outbox 无双发**：append-only、无发送能力；崩溃最多丢一行记账。
16. **outbox 落盘原子性**：`_rewrite` 走 0600 tmp + `os.replace`；同进程单事件循环无交错。
17. **web 乐观锁与两段确认叠加**：第一道 stale 在发码前、第二道在写锁内写盘前；HMAC 绑定值+nonce 单次；审计/改前备份照常。
18. **push legacy 记账导入幂等**：仅当共享计数为 0 时导入，同日重启不重复。
19. **reminder 空 user_id 入口拒绝**（上轮 FIX-1）仍在位。
20. **boolenv 统一**：认不出的值告警一次后取默认；写门禁同样迁移。
21. **`_UNTRUSTED_TOOL_RESULTS` 现有 8 项覆盖**与 wiki 动态前缀判定的精确性（但 5 个主机状态技能缺失见 M18）。
22. **分层铁律**：本周期新增 agentcore 模块（outbox/notify_budget/nebula/decay/build_info/cron_skills）零 nonebot import；`file_sender` 仍是唯一受保护豁免。
23. **测试隔离**：conftest 的 `_isolate_notify_budget`/`_isolate_outbox` 防真实 QQ 号写入仓库 `data/state`。

---

## 5. 与在库报告的衔接复核

### 5.1 上一轮修复状态（`FIX-review-a9a28cf.md`）

| 项 | 状态 | 复核方式 |
|---|---|---|
| FIX-1 reminder 空 uid 入口拒绝 | ✅ 在位 | `reminder_skills.py:83` |
| FIX-2 布尔 env 六站点迁 boolenv | ✅ 在位 | 66 处 env_flag/env_bool 调用点 |
| FIX-3 cron 永触发表达式 | ✅ 在位 | `agent_turn.py:66/87` `_NEVER_FIRE` |
| FIX-4 sink 死代码（防 uncertain 重发） | ✅ 在位 | `sink.py:118-125` 薄委托 |
| FIX-5 `AGENT_GROUP_EXTRA_SKILLS` 入 .env | ✅ 在位 | 184=184 零差集 |
| FIX-6 `AGENT_CRON_DIR` 订正 | ✅ 在位 | `.env.example:134` = data/cron |
| A1 `.gitignore` 状态文件/目录 | ✅ 在位 | 3 路径 `git check-ignore` 全命中 |
| A2 `reserved_root` 尾部点/空格 | ✅ 在位 | `fs.py:59` `rstrip(" .")` |
| A3 冷备 `state_file()` 双路径 | ✅ 在位 | `backup_usb.sh:99-109` 二选一 + 缺失 exit 1 |
| **A4 5 个主机状态技能入围栏** | ❌ **未落 main** | 见 **M18**（`_UNTRUSTED_TOOL_RESULTS` 仍 8 项；全历史无该字符串） |

### 5.2 上轮「未验证面」销项

| 上轮未验证面 | 本轮结论 |
|---|---|
| PG 套件（本机无容器） | ✅ 销项：`qqagent_test` 空库实跑 3918 passed / 9 skipped |
| CI（API 限流） | ✅ 销项：6867995 三工作流 success（匿名 API 逐条核对） |
| 真实外发/真机通道 | 仍未验证（§3.4-1/3/5），按规则声明 |
| perf 基线 | ✅ 销项：RUN_PERF 9 + baseline 无劣化 |

### 5.3 评审后的远端尾巴

本地评审终点为 `6867995`；远端 main 另有 2 个 commit（`99b9472` 单回合总期限修复 + merge `be4a8e3`，含 `review/FIX-turn-deadline-20261104.md`），CI 已 success。**其 matcher 单回合期限缺口自 v1.0.4 即存在、非本周期引入**，且已带 FIX 文档与回归用例，故列入下一轮衔接而非本轮立案。注意：该次的 FIX 索引行与本次 §9 索引追加在 `REVIEW-WORKFLOW.md` 同一表区，合回时需按文件解冲突。

---

## 6. 修复优先级建议

**P0（下个版本前必修，均可在数行内完成）**
1. M6/M7：cron 一次性语义（`deleteAfterRun` 落地或单次路径拒绝 + `trusted` 缺省 False + 面板判定改读 state.json）。
2. M12：outbox 首建 0600（一处 `os.open` + 一条权限用例）。
3. M14：outbox_notice 两阶段（先读后删）。
4. M17：reboot 收尾补 stats/archive/growth 三槽位（一行 getattr ×3）。
5. M2/M3：回执预算（stale 改 finished_at 或窗口 ≥2000s；`_TERMINAL_WAIT` ≥420s）。
6. M4：/update 与 /reboot 互锁。
7. M18：重新落 A4 + 5 条围栏断言（上轮声称的修复缺席是本轮最该先补的账）。

**P1（本迭代内）**：M1（user_mode preflight）、M5（回滚备份）、M8（围栏 neutralize + 措辞）、M9（jobs 写锁）、M13（预算预占或文档明示）、M15/M16/M19（文档与流程补账）、M10（`key=int` + 契约测试 ≥10 源）。

**P2（排期）**：M11（facts 抽样上限 + hover 节流）、§1 的 L 批量计划（随修 9 / 独立 5 / 遗留 8）。

**发布门禁**：无 H，可发布；但建议 P0 全部落地后再切下一版（1.0.6），其中 M6/M7/M12/M18 属安全语义与既有承诺面，不宜跨版本遗留两轮。
