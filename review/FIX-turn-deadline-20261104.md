# FIX — 单回合总期限：等闸门那一眼必须有上界（2026-11-04）

> 来源：**线上事故**（非评审轮次）。生产 bot 升级 1.0.5 后私聊全静默、零日志，
> 命令与群消息照常。分支 `fix/turn-deadline`。
> 纪律：遵循 AGENTS.md §2——先分级、AC 先行、最小改动、回归用例进被测模块对应
> 主题测试文件（`tests/test_matcher.py`）、不自动 commit/push。

## 0. 修复总表

| # | 问题 | 等级 | 判定依据 |
|---|---|---|---|
| H1 | `_answer` 等并发配额（`async with _get_turn_semaphore()`）是**无超时等待**，而 `AGENT_TURN_TIMEOUT` 只包住 `engine.run`——卡在闸门之前时，全 bot 静默锁死且零日志 | **High**（并发/生命周期，可用性） | verified：见 §1 证据链 |
| M1 | 超时即转储未决任务栈（生产无 ptrace 权限，py-spy 用不了，事后只能推断） | Medium（可诊断性） | verified：变异 A 被抓 |

**不是 1.0.5 引入**：`git grep -c _turn_semaphore v1.0.4` = 7 处引用，保护缺口自该机制上线起一直存在，本次只是被某个上游挂死触发。

## 1. 事故证据链（全部本地实测）

生产环境：`/home/xiaji/apps/agent-demo`，1.0.5（`6867995`），systemd unit，PID 2556902（00:10:58 启动）。

| 时间 | 现象 | 证据 |
|---|---|---|
| 00:12:25 / 00:13:01 / 00:14:02 | 三条私聊 `[msg]`（在吗 / 在吗 / 测试消息） | `data/logs/agent.log` |
| — | 三条之后**无** `LLM step`、**无** `[reply]`、**无**任何 ERROR/WARNING | 同上；对照 11-03 正常轮次一定是 `[msg] → LLM step → [reply]` |
| — | `messages` 表近 30 分钟只有 2 行（复现脚本写的）——三个回合**一行未写** | PG 查询；引擎在调 LLM **之前**就 `append_message(user)`，故卡点在引擎之前 |
| — | PG 无锁等待、无 `idle in transaction`、bot 的 2 个连接全 `idle` | `pg_stat_activity` / `pg_locks` |
| — | 用生产同一套 `.env` 新起进程跑一轮 → 正常回复「在。这么晚了还没睡？」 | 复现脚本；代码路径本身无缺陷 |
| — | 优雅停机 120ms 完成、防抖队列已空 | 停机日志；说明回合已过 `asyncio.sleep(3)`、进入了 `_run_parts` |
| — | 180s 回合超时**未触发**（无 `turn timeout` 日志） | 说明卡点在 `wait_for` 覆盖范围之外 |
| — | `/update`、`/model`、`/update status` 全部正常；群消息持续被处理 | 命令不走聊天管线；事件循环活着（CPU 1 tick/5s，无自旋） |
| 00:37:54 | `SIGTERM` 优雅停机 → systemd 5s 后拉起新进程（PID 2590285），恢复 | `journalctl -u agent-demo` |

**根因**：`_answer` 的结构是

```python
async with _get_turn_semaphore():          # ← 无超时等待
    reply = await _run_and_format(...)     # ← 只有里面的 engine.run 有 180s 超时
```

等配额这一格没有任何期限。一个挂死（无论具体卡在哪）只要发生在闸门之前，180s 的
保护就一丝作用不发挥，表现为**全站静默 + 零日志**——正是 AGENTS.md 里"挂死的调用
会永久占用并发闸门、叠几次后整个 bot 假死"的那个场景，只是漏了闸门之前那一格。

**仍未定位**：第一次卡住的确切位置。静态分析已排除 PG（无挂起查询）、各网络调用
（均有 60s 超时且失败必留痕）、引擎内部（`wait_for` 会触发并留痕）；生产机
`ptrace_scope=1` 且无 sudo，py-spy 无法 attach，拿不到现场栈。本修复不依赖根因——
它把「等闸门 + 跑引擎」整段纳入同一期限，无论卡在哪一格都能自愈并留栈。

## 2. 改法

把「拿闸门 → 跑引擎」抽成 `_run_turn_locked()`，由 `_answer` 用同一个
`_turn_timeout_seconds()` 包整段：

```python
reply, engine_elapsed, engine_started = await asyncio.wait_for(
    _run_turn_locked(payload, combined, images, extraction_text), timeout=timeout)
```

- 取消时 `async with` 的 `__aexit__` 正常释放配额 → **后续回合立刻能进**；
- 卡在等配额/组装/embedding/LLM/PG 任何一格，总期限一到整轮取消；
- **出站投递刻意留在期限外**：取消一次进行中的发送会留下"结果不确定"，而铁律是
  结果不确定绝不重发（取消 → 重发 = 用户收两遍）；
- `AGENT_TURN_TIMEOUT=0` 仍可完全禁用（走无期限分支）。

配套 M1：`_log_turn_timeout()` 在超时时把当前所有未决任务的栈
（`task.get_stack()`，取末 4 帧）写进 ERROR 日志——把这次欠下的诊断债一次还清。

## 3. 行为变更（从 X 改为 Y）

| # | 之前 | 之后 |
|---|---|---|
| 1 | 卡在引擎之外的回合：无限等待、零日志、配额不放 | `AGENT_TURN_TIMEOUT`（默认 180s）后取消整轮，回复「出错啦，请稍后再试」，ERROR 日志含未决任务栈 |
| 2 | 引擎内部超时：`[echo] <用户原话>` | **不变**（`_run_and_format` 内的 `wait_for` 仍先生效，兜底文案不改） |
| 3 | `AGENT_TURN_TIMEOUT=0` | **不变**（仍表示不限制） |

## 4. 用例与变异复核

新增 `tests/test_matcher.py::TestTurnDeadline`（4 条，进被测模块对应主题文件）：

| 用例 | 断言 |
|---|---|
| `test_semaphore_wait_is_bounded` | 占住唯一配额 → 新回合 < 2s 返回「出错啦」；释放配额后后续回合能进 |
| `test_timeout_dumps_pending_stacks` | ERROR 日志含 `turn deadline exceeded` 与未决任务名/栈 |
| `test_zero_disables_deadline` | `AGENT_TURN_TIMEOUT=0` 时慢但完成的回合照常返回 |
| `test_delivery_is_outside_the_deadline` | 比期限更长的 `deliver_reply` 不被取消（锁住"投递在期限外"） |

**变异复核**（改坏实现 → 必须失败）：

| 变异 | 结果 |
|---|---|
| A：`_log_turn_timeout` 改成空操作 | ❌ `test_timeout_dumps_pending_stacks` 红 |
| B：把信号量挪回期限外（= 旧结构） | ❌ `test_semaphore_wait_is_bounded` **挂住被 timeout 杀（exit 124）**——证明它不是恒真断言 |

实施中自查发现并修掉的两处自身缺陷（均由复跑/变异捕获）：异常分支残留
`engine_started` 引用导致 `NameError`；把时长 `engine_elapsed` 当时间戳基准会把
`mode=failed` 的 total 算成几十小时。

## 5. 验证

```bash
.venv/bin/python -m pytest tests/test_matcher.py -q          # 88 passed
.venv/bin/python -m pytest -q                                 # 3830 passed / 77 skipped
.venv/bin/python -m ruff check agentcore plugins tests bot.py scripts
.venv/bin/python -m ruff format --check agentcore plugins tests bot.py scripts
.venv/bin/python scripts/check_review_docs.py                # EXIT=0
```

> 未跑 `TEST_DATABASE_URL` 套件：本次改动只在 `plugins/` 适配层，不触达 store 双实现。

## 6. 残留 / 未验证

- **第一次卡住的根因未知**（见 §1）。加固把故障从"永久静默"降为"180s 自愈 + 留栈"；
  若复发，`turn deadline exceeded` + 现场栈就是补齐根因的素材。建议复发时先看日志再重启。
- 引擎内部超时仍由内层 `wait_for` 先生效（同一阈值），外层实际覆盖"等配额 + 内层未命中的挂死"。
  两层同值是有意的：内层保留 `[echo]` 语义，外层兜住其余一切。
- 未评估：若挂死原因是 LLM 上游整体不可用，180s 内每个回合都会走到超时分支并回复
  「出错啦」——这是有意的（宁可明确失败，不要静默）。

## 7. 回滚

`git revert` 本分支单个 commit 即恢复旧结构（`_answer` 回到无期限等闸门）。无数据迁移、
无配置变更；`data/` 下无新增状态文件。
