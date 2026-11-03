# FIX — 上游 a9a28cf 遗留项 + 本 fork 差异面修复

> 评审来源：[REVIEW-upstream-a9a28cf.md](REVIEW-upstream-a9a28cf.md)
> 分支：`fix/hardening-review-a9a28cf`（从 `fix/guest-mode-p0` 的未提交 WIP 基线上切出；**未 commit、未 push**）。
> 纪律：遵循 AGENTS.md §2——先分级、AC 先行、最小改动、不自动 commit/push、回归用例进被测模块对应主题测试文件。

## 0. 修复总表

| # | 问题 | 等级 | 判定依据 |
|---|---|---|---|
| FIX-1 | reminder 空 uid 落管理员哨兵 → 列/消他人提醒 | **High**（跨用户数据面） | verified：修前 `reg.execute("reminder_list", None, None)` 返回 3 条含他人正文；`reg.execute("reminder_cancel", None, None, schedule_id="2")` → `已取消提醒 #2`，Bob 剩余 0 |
| FIX-2 | 6 处布尔 env 写法错/漏告警 | Medium（静默失效） | verified：真值表逐站点实测，见 §2 |
| FIX-3 | cron 永触发表达式每 60s 烧一次完整 LLM 回合 | **High**（成本/失控） | verified：repro A4 三个 tick 各跑一次完整 agent turn + 投递，`nextRunAtMs` 恒 0，永不标记 failed |
| FIX-4 | `sink.py send()` return 后 11 条不可达代码 | Medium（潜在双发） | verified：`sys.settrace` 运行时证明三种输入执行行号恒为 `[104]` |
| FIX-5 | `.env.example` 缺 `AGENT_GROUP_EXTRA_SKILLS` → env-doc 守卫红 | Low（门禁红） | verified：修前 `tests/test_repo_guards.py` → 1 failed, 22 passed |
| FIX-6 | `.env.example` 的 `AGENT_CRON_DIR` 与代码真值脱节 | Low（文档误导） | verified：代码 `_DEFAULT_DIR="data/cron"`、settings_spec 默认 `"data/cron"`，示例写 `data/workspace/cron` |
| A1 | `.gitignore` 不覆盖 compose 重定向后的状态文件/目录 | **High**（隐私泄漏） | verified：`git check-ignore -v` 修前 9 路径全 NOT IGNORED，修后 11 路径全命中 |
| A2 | 保留名可被 Win32 尾部点绕过 | Medium（Windows-only） | verified：全新工作区 `cron./jobs.json` 首写成功并落盘 |
| A3 | 冷备只读 `data/` 根状态文件 | Medium（静默缺件） | verified：`sed` 抽函数实跑 + 变异对照（见 §9-A3） |
| A4 | 5 个宿主机状态技能输出不过围栏 | Low | verified：注册位置 + `_UNTRUSTED_TOOL_RESULTS` 集合实读 |

**验收命令**（Windows 存量环境失败口径见 §7）：

```bash
.venv/Scripts/python.exe -m pytest tests/test_tools.py tests/test_scheduler_jobs.py tests/test_scheduler.py \
    tests/test_outbound.py tests/test_poke.py tests/test_help_render.py tests/test_pipeline.py \
    tests/test_rag.py tests/test_repo_guards.py -q
# 预期：3 failed, 692 passed, 2 skipped（3 条失败 = Windows 存量环境失败，见 §7）
.venv/Scripts/python.exe -m ruff check agentcore plugins tests bot.py scripts
.venv/Scripts/python.exe -m ruff format --check agentcore plugins tests bot.py scripts
.venv/Scripts/python.exe scripts/check_review_docs.py   # EXIT=0
```

## 1. FIX-1 reminder 空 uid 越权

### 改法

`agentcore/skills/reminder_skills.py` 两处入口显式拒绝空 `user_id`，与 `reminder_add_skill`（`:70`）同款：

- `:103-107` list：`if not user_id: return "错误：拿不到会话信息，无法查看提醒"`；
  查询改 `store.schedule_list(user_id)`，并去掉 `not user_id or` 的空操作过滤；
- `:133-137` cancel：`if not user_id: return "错误：拿不到会话信息，无法取消提醒"`；`uid = user_id`。

### 根因

`user_id or None` 把空会话落成 store 的 `None` 管理员哨兵。`schedule_list(None)` 取全量、
`schedule_cancel` 的 `user_id is None or row["user_id"] == user_id` 判真 → 跨用户视图 + 跨用户取消。
`registry.execute` 是公开 API，handler 默认值 `user_id=""` 生效即获得该视图。

### store 契约（勿改，两实现一致）

`agentcore/memory/store.py:476` docstring「只能取消自己的，除非 user_id 为 None 表示管理员操作」；
内存 `:1059-1069` `(user_id is None or r["user_id"] == user_id)`、`:1080-1089`；
PG `:1817-1830`/`:1832-1845` `user_id is None` 时**不加 WHERE 子句**；
`tests/test_store_contract.py:400/433` 锁死。

### 测试

- `tests/test_tools.py:701 class TestReminderEmptyUidRejected`：两个用户 `10001`/`10002` 各建一条 → 空 uid 的
  list/cancel 均被入口拒绝、`user_id=None` 显式传入同样被拒、对方提醒 `enabled is True` 不受影响、
  正常 uid 仍只见自己。
- 改写 `TestReminderPushSeparation::test_push_rows_hidden_from_reminder_tools`：原文借空 uid 当管理员来断言
  「取消定时推送任务时返回 `/push off` 指引」——手段错误但意图真实。新写法：空 uid → 断言 `"拿不到会话信息"`；
  真实意图改由「行属于调用者自己」触达（同 `user_id="7"` 下再 `schedule_add` 一条 `action="push"`，
  取消它仍得 `"/push off" in own_answer` 且 `"已取消" not in own_answer` 且该行 `enabled is True`）。

### 变异复核（AGENTS.md §2.7）

| 变异 | 结果 |
|---|---|
| list 守卫 `if not user_id:` → `if False:` + `schedule_list(user_id or None)` | `1 failed`：`AssertionError: assert '当前没有待触发的提醒。' == '错误：拿不到会话信息，无法查看提醒'`（tests\test_tools.py:787） |
| cancel 守卫同样变异 | `1 failed`：`AssertionError: assert '拿不到会话信息' in '#2 是定时推送任务，不是提醒；请让管理员用 /push off 2 关闭。'`（tests\test_tools.py:744） |

两处均用**反向编辑**还原（未用 `git checkout --`）。
修后 `pytest tests/test_tools.py -q -k "Reminder or reminder"` → **22 passed, 65 deselected**（原 21）。

### 与上游的差异（有意，勿当漏修）

上游 M1 另有超管分支（`_is_admin(uid)` + admin 时逐条标注归属 QQ 与目标会话）。
本 fork reminder 无超管分支，故只移植核心守卫。

## 2. FIX-2 布尔 env 六站点迁移到 `agentcore.boolenv`

### 改法（6 文件，全部 `from agentcore.boolenv import ...` + 委托）

| 文件 | 位置 | 旧写法的问题 |
|---|---|---|
| `plugins/qq_agent_adapter/web.py` | `:98 _write_enabled` | `(... or "").strip() == "1"` 只认字面 `"1"` → `AGENT_WEB_WRITE=true/yes/on` 静默不挂整个 web 写面 |
| `plugins/qq_agent_adapter/pipeline.py` | `:92 vision_enabled` | **漏 `.lower()`** → `TRUE/True/ON/Yes` 静默关识图；这是 settings_spec vision 簇 gate，gate 判错则真门失效 |
| `plugins/qq_agent_adapter/help_render.py` | `:51 image_enabled` | 空串→False 尽管默认 True（保留：显式空串 = 关） |
| `plugins/qq_agent_adapter/poke.py` | `:64 enabled` | 同款 |
| `agentcore/rag/distill.py` | `:75 _env_flag` | 集合缺 `"on"` → `AGENT_KB_DISTILL_PRIVATE=on` 旧口径 False（真分歧） |
| `agentcore/rag/service.py` | `:55 _env_bool` | `AGENT_KB_ENABLED=disabled` → **False**，全仓唯一「脏值把默认 True 翻成 False」 |

同时删除两处已无使用者的 `_TRUE = {"1","true","yes","on"}` 模块常量（`help_render.py`、`poke.py`）。

### 关键语义约束

`image_enabled`/`enabled` 的「显式空串 = 关」**不能**直接交给 `env_bool`（其口径是空值取默认 = 开）。
实现为先判 `raw is None` 再走 `env_flag`：空串 → `parse_bool` 返 None → 取 `default=False`。

### 有意行为变化（1 处）

`AGENT_KB_ENABLED=` 空串：旧口径 False、新口径取默认（`config.yaml` 的 `rag.enabled`）。
「设了但留空」= 没配好，不是关掉——与 boolenv 及其余全部布尔 env 一致。

### 实测真值表（verified）

`env_flag(name, raw, default)` 对 `1/true/TRUE/True/yes/on/ON/' on '/false/FALSE/no/off/OFF/0` 均按值判；
`disabled`/`n`/`''`/`'  '` → 取 default 且**告警一次**（去重键 `(name, value)`）；`None` → default 不告警。

| 站点 | unset | `0`/`disabled`/`''` | `1`/`true`/`TRUE`/`on`/`ON`/`yes` |
|---|---|---|---|
| `pipeline.vision_enabled` | False | False | True |
| `help_render.image_enabled` / `poke.enabled` | **True** | False | True |
| `distill._env_flag('AGENT_KB_DISTILL_PRIVATE')` | False | False | True |
| `service._env_bool('AGENT_KB_ENABLED', True)` | True | **True**（disabled 不再静默关 KB） | True |
| `web._write_enabled`（配 `AGENT_WEB_ALLOW_CIDRS=127.0.0.1/32`） | False | False | True |

六个站点对 `disabled` 各打一条 WARNING（去重后 1 条/站点）。

### 测试（按主题文件归位）

- `tests/test_poke.py::TestEnvToggles`：`test_truthy_synonyms_and_case_insensitive`（parametrize `["1","true","TRUE","True","yes","on","ON"," on "]`）+ `test_unrecognized_value_warns_once_then_defaults`（`disabled` → False，`caplog` 断言 `logger="agentcore.boolenv"` 且 `caplog.text.count("AGENT_POKE_ENABLED") == 1`、`"已按 关 处理" in caplog.text`）。
- `tests/test_help_render.py::TestReviewC472HelpRender`：同两款。
- `tests/test_pipeline.py` 新开 `class TestVisionGate`（docstring 写明旧写法漏 `.lower()` 且这是 vision 簇 gate）：`test_default_off`、`test_truthy_synonyms_and_case_insensitive`、`test_falsy_and_empty`（parametrize `["0","false","FALSE","no","off","OFF",""]`）、`test_unrecognized_value_warns_once_then_off`。
- `tests/test_rag.py::TestKbDisabledL7`：`test_unrecognized_value_does_not_silently_disable`（`disabled` → `kb.enabled is True` + 告警一次 + `"已按 开 处理"`）、`test_case_insensitive_enable`（`TRUE` → True）。
- `tests/test_rag.py::TestDistill`：`test_include_private_env_flag_on_synonym`（`on` 走完整 `distill_from_memory` 两轮：首轮 `skipped`、次轮 `ok` 且 `chunks == 1`）、`test_include_private_env_flag_unrecognized_warns`（`disabled` → False + 告警一次）。

### 迁移范围裁定（为什么只迁 6 处）

全仓 AST + 逐站点实测共 29 处旧写法，分 7 组：**B 真错 6（已迁）**、C 纯静默 5（值不变）、
D 默认 False 的 `in {TRUE}` 5（安全向但静默）、E 已有 WARNING 3（**盲迁改行为**：脏值忽略 env
回退配置文件而非 default）、F JSON `enabled` 字段 3（非 env，且**空串已被特判为 False**，迁移必须保留）、
G 排除（三态 mode / LLM 参数 / 仅回显）。详见 REVIEW 文档 §2-FIX-2 的完整分组表。

## 3. FIX-3 cron 永触发表达式 → 每 60s 烧一次完整 LLM 回合

### 根因

`0 0 30 2 *`（2 月 30 日）`CronTrigger.from_crontab` **成功**，但 `get_next_fire_time` 永远 None。
`_next_run_ms` 返 0，而 tick 的到点判断 `if job.next_run_at_ms and job.next_run_at_ms > now_ms: continue`
把 **0 当「首次运行」→ 永不跳过**。于是每 60s 跑一次完整 LLM agent turn + 投递，无限烧 token，
且 job 永远不会被标记 failed。

对照：非法 cron 语法在 `_validate_job` 就被拒（走 errors 通道），**不是**热循环；
非法时区 `_zone_for` 回退进程 tz 仍算得出；apscheduler 缺失时 `_next_run_ms` 返 0 且 `parse_jobs` 抛 ImportError。

### 改法（两处都改）

**装载期拒绝** `agentcore/scheduler/jobs.py:238-245`：

```python
trigger = CronTrigger.from_crontab(cron)   # 原为裸调，不接返回值
...
if trigger.get_next_fire_time(None, agent_tz.now()) is None:
    return None, f"{name}: cron 表达式永触发（不存在满足条件的日期）：{cron!r}"
```

**执行期兜底** `agentcore/scheduler/agent_turn.py`：

- `:63 _NEVER_FIRE = -1`（0 仍表示「从没跑过/依赖缺失」，负值 = 永触发）；
- `:85` `return int(nxt.timestamp() * 1000) if nxt else _NEVER_FIRE`；
- `:169-171` `run_job` 入口守卫：`if _next_run_ms(job.cron, job.tz) == _NEVER_FIRE: self._warn_dead_once(job); return "failed"`；
- `:126-137 _warn_dead_once` 按 job.id 去重（tick 60s 一轮，不去重刷 1440 条/天）；
- 新增 `from agentcore import tz as agent_tz`（jobs.py，放在 `from agentcore.scheduler.push import parse_target` 前，`agentcore/tz.py` 无回环）。

**为什么两处都改**：单改装载期不够——`run_job`/`tick` 可能收到绕过 `_validate_job` 直接构造的 `CronJob`；
单改执行期也不够——装载期不拒的话 job 会先跑一次完整 LLM 回合才发现。

**守卫放 `run_job` 而非只放 tick 的理由**（测试 docstring 原文）：「run_job 是执行一条 job 的唯一入口
（tick 也走它），所以守卫放这里而不是只放 tick——放 tick 的版本在装载期修复后不可达，用例会退化成
恒真断言（删掉守卫照样绿）」。

**不推进 next_run**：一条永远不会有下次执行的 job，写任何 `next_run_at_ms` 都是假的。

### 测试（`tests/test_scheduler_jobs.py`）

`:442 test_never_fire_returns_negative_sentinel`（`_next_run_ms("0 0 30 2 *", None) == _NEVER_FIRE` 且 `_NEVER_FIRE < 0`）、
`:447 test_leap_day_is_not_never_fire`（`0 0 29 2 *` 是闰日，非永触发）、
`:113 test_rejects_never_fire_cron_expr`、`:121 test_accepts_leap_day_cron_expr`、
:`585 test_run_job_rejects_never_fire_expr`（`status == "failed"`、eng/sink 零调用、`caplog.text.count("永触发") == 1`）。

### 实测（verified）

`_next_run_ms`：`0 0 30 2 *` → `-1`、`0 0 31 4 *` → `-1`、`0 0 29 2 *` → `1835366400000`（2028 闰年）、
`0 */3 * * *` → `1793592000000`、`not a cron` → `0`。
`J.parse_jobs`：`0 0 30 2 *` → `jobs=0 errors=["任务: cron 表达式永触发（不存在满足条件的日期）：'0 0 30 2 *'"]`；
`0 0 29 2 *`/`0 */3 * * *` 正常装载；`bad expr` 仍走原「cron 无法解析」。

## 4. FIX-4 `sink.py send()` return 后 11 条不可达代码

### 改法

`plugins/qq_agent_adapter/sink.py`：删除 `send()`（`:95-129`）里 `return (await self.send_once(target, message)) == "sent"`
之后的 11 条不可达语句（target 解析/校验、kind 检查、`_bots()`、`throttle.acquire`、bot 循环、末尾 warning、
`return False`），docstring 扩写为「三态收敛成布尔；**这里绝不能自己实现重试**——一旦绕开 `send_once`
直接遍历 bot，`uncertain`（超时/断连，请求可能已送达）会被当成确定性失败换 bot 重发，用户收到两遍」。
`send_once`（`:41-93`，正确三态实现）未动。

### 测试（`tests/test_outbound.py::TestSinkThrottle`）

- `:1018 test_uncertain_is_not_retried_on_next_bot`：两个 bot，第一个抛 `TimeoutError("ws timeout after send")` →
  `await sink.send("group:1", ...) is False` 且 `tried == ["timeout-bot"]`（OkBot 永不被调用）+ `send_once(...) == "uncertain"`；
- `:1045 test_send_has_no_retry_logic_of_its_own`：`ast.parse(textwrap.dedent(inspect.getsource(Sink.send)))`，
  断言除 docstring 外**唯一语句是 Return**，且源码不含 `send_group_msg` / `_bots()`。

### 变异复核（verified）

| 变异 | 结果 |
|---|---|
| `send_once` 的 `return "uncertain"` → `logger.warning("mutated...")` 落穿到 `continue` | `1 failed`：`assert True is False`（tests\test_outbound.py:1040） |
| 在 `send()` 的 return 前插一条 `logger.info(...)` | `1 failed`：`AssertionError: ['Expr', 'Return']`（tests\test_outbound.py:1067） |

两处均用**反向编辑**还原。修后 `pytest tests/test_outbound.py -q` → **80 passed**。

## 5. FIX-5 / FIX-6 `.env.example` 两处订正

- **FIX-5** `:471-474`：`AGENT_PERMISSION_LEVEL=medium` 之后插入 3 行注释 + `AGENT_GROUP_EXTRA_SKILLS=`
  （群内 superuser 也看不到 superuser 级技能；需额外放开时列**只读**技能名；群内普通成员永不可见；
  该键不进 web 写面）。修掉 `TestEnvDocs::test_real_tree_env_vars_documented` 的红。
- **FIX-6** `:537`：`AGENT_CRON_DIR=data/workspace/cron` → `data/cron`（代码真值 `jobs.py:67 _DEFAULT_DIR`、
  `settings_spec.py:1302` 默认同为 `data/cron`），注释块同步 `data/workspace/cron/jobs.json` → `data/cron/jobs.json`
  并补「运行期产物（gitignore），模板 `docs/examples/cron-jobs.example.json`，首启由 `compose_init` 复制」。

**FIX-5 验证**：`pytest tests/test_repo_guards.py -q` → **23 passed**（原 1 failed, 22 passed）；
`pytest tests/test_web.py -k schema_covers_env_example -q` → 1 passed。

## 6. 门禁与套件对比

| 项 | 结果 |
|---|---|
| `ruff check`（6 个改动文件 + sink.py/test_outbound.py） | All checks passed |
| `ruff format --check`（同上） | 已格式化 |
| `pyright agentcore` | **未跑**（Windows 存量阻塞见 §7） |
| `scripts/check_privacy.py` | **未跑**（本次未新增含真实 ID 的入库文本） |
| `scripts/check_review_docs.py` | EXIT=0 |
| **全量套件** | **167 failed, 3470 passed, 84 skipped**（135.39s） |
| 基线（改动前） | 167 failed, 3423 passed, 84 skipped |
| 结论 | **失败数零变化，+47 passed**（FIX-1 1 条、FIX-2 参数化 39 条、FIX-4 2 条、FIX-3 若干） |

失败逐文件分布与基线**完全一致**（`--tb=no` + 计数）：test_web.py 62、test_backup.py 14、
test_action_skills.py 14、test_runner_security.py 13、test_archive_skills.py 7、test_config_write.py 6、
test_ssh_skill.py 5、test_shell_skill.py 5、test_reboot.py 5、test_totp.py 4、test_search_providers.py 4、
test_nudge.py 4，其余 1-3。

### 6.1 A 组修复后的复跑（2026-11-02）

| 项 | 结果 |
|---|---|
| `ruff check agentcore plugins tests bot.py scripts` | **All checks passed!** |
| `ruff format --check`（同上） | **243 files already formatted** |
| `pyright agentcore` | 4 errors，**全部在我未触碰的文件**（`agentcore/skills/search.py:158/:417` `"timeout" is not a known attribute of module "asyncio"`、`agentcore/tz.py:35` `"UTC" is not a known attribute of module "datetime"`）= Python 版本相关的存量失败，与本次改动无关 |
| 全量套件 | **168 failed, 3480 passed, 84 skipped**（152.11s） |
| 相对上一次全量（167 failed, 3470 passed） | 失败 **+1**（test_web.py 62→61，即**少 1 条失败**；test_backup.py 14→17 = 新增 3 条 A3 用例），通过 **+10** |
| 新增 3 条 A3 用例为何红 | `TestUsbScriptGuards` 全类在本机跑不通：`rsync: command not found` + Git Bash `Bash/E_ACCESSDENIED`，与既有 4 条同因（§7 环境限制） |
| `scripts/check_review_docs.py` | EXIT=0 |

## 7. 环境限制（Windows 存量失败，非本次引入）

`docs/development.md:65-82` 明确 Windows 暂不支持。目标套件的 3 条失败：

1. `tests/test_tools.py::TestOpsHandlerAdminGate::test_superuser_passes_handler_gate` — `os.O_NOFOLLOW` 缺失；
2. `tests/test_scheduler.py:1139 TestPushDailyCapPersistence::test_state_file_atomic_and_private` —
   `assert os.stat(state_file).st_mode & 0o777 == 0o600` → `AssertionError: assert (33206 & 511) == 384`（无 POSIX 权限位）；
3. `tests/test_pipeline.py:1501 TestH2UntrustedEchoSanitizing::test_download_for_su_admin_saves_and_reports_relative_path` —
   `['[图片1 已保存到工作区 media\\a.jpg]'] != ['[图片1 已保存到工作区 media/a.jpg]']`（路径分隔符）。

**`os.O_NOFOLLOW` 未设防站点（`git show main:<path>` 确认 main 上同样存在）**：`agentcore/totp.py:74`、
`plugins/qq_agent_adapter/config_write.py:328`、`plugins/qq_agent_adapter/web.py:495`、
`agentcore/skills/search_providers.py:231`。已设防惯用法：`personas/manager.py:370`、`llm/providers.py:342`、
`scheduler/jobs.py:348`、`skills/archive_skills.py:49`（`getattr(os, "O_NOFOLLOW", 0)`）。**定夺：不修**。

## 8. 剩余项（未修，已入 REVIEW §4 选题池）

tick 串行致触发时刻漂移（实测第 k 个 job 漂移 = 前面所有 job 耗时之和，无界；`max_instances=1` +
`misfire_grace_time=max(30, seconds)` 下 tick 超时被丢弃）；cron 私聊投递目标零准入；`deleteAfterRun` 死字段；
invalid-token 与 TOTP 共享限速窗口；冷备失败可观测性；Dockerfile `mkdir` 预建清单无守卫；
`deploy.py` 的 `.env` 盖过 shell（`cmd_check :249-251`、`cmd_db :392-394` 等）；`_login` 重复 import ×4；
`search` 双实现重复；C/D/E/F 组布尔 env（值不变或行为不同）。

## 9. 架构评审轮 A 组 4 项（2026-11-02）

用户在前六项之外追问「架构上还有什么问题」，遂做一轮**架构层**评审（只读子代理 ×4 + 主代理复核，
产物见 [REVIEW-upstream-a9a28cf.md](REVIEW-upstream-a9a28cf.md) §8）。用户圈定只修 **A 组安全边界 4 项**，
B/C/D 组（配置单一事实源、并发生命周期、架构议题）本轮不做。

| # | 问题 | 等级 | 修复落点 |
|---|---|---|---|
| A1 | `.gitignore` 不覆盖 compose 重定向后的 7 个状态文件 + 2 个目录 → NAS 机器 `git add -A` 把 TOTP secret / 搜索源 key / 真实 QQ 目标暂存进公开库 | **High**（隐私泄漏，只能重写全历史补救） | `.gitignore` + `tests/test_compose_init.py` |
| A2 | 工作区保留名（`SOUL.md`/`personas`/`cron`）可被 Win32 尾部点绕过：`cron./jobs.json` 首写成功 | Medium（Linux 不剥尾部点，故 Windows-only） | `agentcore/workspace/fs.py` + `tests/test_workspace.py` |
| A3 | `backup_usb.sh` 只从 `data/` 根读状态文件，compose 下落点在 `data/state/` → 冷备静默缺 2FA secret / provider key | Medium（静默缺件，MANIFEST 双道校验照样全绿） | `scripts/backup_usb.sh` + `tests/test_backup.py` |
| A4 | 5 个宿主机只读状态技能（`system_status`/`proc_detail`/`disk_usage`/`port_check`/`service_status`）输出不过围栏 | Low（仅 superuser 可见，但未围栏直达持 `proc_kill`/`run_shell`/`fs_delete` 的循环） | `agentcore/loop/engine.py` + `tests/test_engine.py` |

### A1 `.gitignore` 缺口（含守卫）

**改法**：删 `data/state/llm-providers.json` 单行（保留 `data/llm-providers.json` 供旧版升级一次性迁移读取）；
新增 `data/reboot-notice.json`、`data/push-state.json`（data/ 根形态此前**完全没被忽略**）；
新增整目录 `data/state/`、`data/mirror/`、`data/ssh/` + 8 行注释记录 2026-10-28 重定向只补一行导致另外 6 个
状态文件 + 2 个目录全漏。

**守卫**（`tests/test_compose_init.py::TestRealTemplate::test_state_redirects_covered_by_gitignore`）：
落点集合从 `ci.STATE_REDIRECTS`（7 键）+ `data/` 根同名文件 + `data/mirror|ssh` 推导，
覆盖判据含**目录前缀**（`rel.startswith(pat + "/")`——`tests/test_repo_guards.py:224-248 unignored_secrets`
的同名判据不支持前缀，这是它查不出这个缺口的原因）。主张：新增状态文件不用再想起来补 `.gitignore`。

**实测**：`git check-ignore -v` 11 个路径全部命中（`.gitignore:93 data/state/` ×7、`:95 data/mirror/`、
`:96 data/ssh/`、`:82/:83` 两个根文件），EXIT=0。修前同样 9 个路径全 NOT IGNORED（exit 1）。

### A2 保留名尾部点绕过

**改法**（`agentcore/workspace/fs.py:43-59 reserved_root`）：先 `if not rel.parts: return False`，再
`rel.parts[0].rstrip(" .").casefold() in _RESERVED_ROOT_NAMES`（旧写法直接 `rel.parts[0].casefold()`）。
单一判定点，`_assert_writable`（`:60-74`）与 `agentcore/skills/archive_skills.py:108 _resolve_dest` 都复用它。

**回归**（`tests/test_workspace.py:241-270`）：`test_write_trailing_dot_rejected` 用**全新**工作区对
`cron./jobs.json`、`SOUL.md.`、`personas./default/SOUL.md`、`mkdir('cron.')`、`'cron /jobs.json'`（尾空格同形态）
逐个断言 `ValueError: 保留名`，并断言工作区零残留；`test_trailing_dot_non_reserved_still_allowed` 钉
「归一不能过头」——`notes./readme.txt` 仍可写。两条都不依赖 Windows（Linux 不剥尾部点，钉的是
「先归一再比对」这条不变量）。

### A3 冷备状态文件路径

**改法**（`scripts/backup_usb.sh:115-135`）：新增 `state_file()` 助手，按
`$SRC/data/state/$f` → `$SRC/data/$f` 顺序取第一个存在的；循环里 `if p="$(state_file "$f")"` 命中则 rsync，
否则 `log "WARN: 未找到 data/state/$f 或 data/$f，冷备不包含它"`（原来是静默跳过）。

**本机限制（verified）**：`rsync` 在本机不存在、Git Bash 对同一条命令报 `Bash/E_ACCESSDENIED`，
故 `TestUsbScriptGuards` 全类（既有 4 条 + 新增 3 条）在本机无法跑通——与 `docs/development.md` 的
「Windows 暂不支持」一致。**替代验证**：`sed` 把 `state_file` 从脚本抽出来单独跑——
compose 形态（只在 `data/state/`）三个文件全部 FOUND；宿主机形态（只在 `data/` 根）FOUND 且缺失文件返回 1；
**变异对照**：把函数换回旧的单路径写法，compose 形态三个文件全部 MISSING（即缺陷原形）。
`bash -n scripts/backup_usb.sh` 语法通过。

### A4 宿主机状态族围栏

**改法**（`agentcore/loop/engine.py:102-121`）：`_UNTRUSTED_TOOL_RESULTS` 由 8 项扩到 13 项，
新增 `system_status`/`proc_detail`/`disk_usage`/`port_check`/`service_status` + 5 行注释
（注册位置 `ops_skills.py:188/212/227/244` 与 `system_status.py:87`；ps argv / unit Description /
netstat 对端地址是主机侧可控文本；仅 superuser 可见故 low，但未围栏直达持 `proc_kill`/`run_shell`/`fs_delete` 的循环）。

**回归**：`tests/test_engine.py:1820-1860 test_superuser_tool_results_are_fenced` 的 parametrize 由 5 项扩到 10 项。

> **事后修正（2026-11-04，评审 M18 实证）**：上述「改法」与下方「变异 5 failed」均为**失真
> 声称**——该 hunk 在落 main 时丢失（UGREEN 分支 WIP 覆盖式 merge 同族事故），
> `git log --all -S'port_check' -- agentcore/loop/engine.py` 零命中，`da12779` 的 engine.py
> 同样是 8 项、`test_engine.py` 无这 5 个技能的围栏断言。真正的落码在
> `fix/review-a9a28cf-p0`（M18 commit）：`_UNTRUSTED_TOOL_RESULTS` 补 5 项，
> `test_superuser_tool_results_are_fenced` parametrize 扩到 10 项，另把 trusted 通道的
> 直通名单从循环内联字面量抽成 `_TRUSTED_LOCAL_TOOLS`（两份名单正是漂移丢修复的结构
> 性原因），并新增 `test_local_ops_tools_trusted_exempt` 十个工具逐个钉住
> 「trusted 直通 + 非 trusted 围栏」。变异复核本次**实际复现**：删围栏名单 5 项 → 5 failed；
> 删直通名单 5 项 → 5 failed。

### A 组变异复核（改坏实现必须转红）

| 变异 | 结果 |
|---|---|
| A4：从 `_UNTRUSTED_TOOL_RESULTS` 删掉 5 个新名字 | **5 failed**（`test_superuser_tool_results_are_fenced[system_status|proc_detail|disk_usage|port_check|service_status]`），还原后 15 passed |
| A2：`reserved_root` 去掉 `rstrip(" .")` | **`test_write_trailing_dot_rejected` DID NOT RAISE**，还原后 2 passed |
| A1：删 `.gitignore` 的 `data/state/` 行 | **`test_state_redirects_covered_by_gitignore` 红**（列出 7 个 `AGENT_*=data/state/*` 落点），还原后 35 passed |
| A3：`state_file` 换回单路径写法 | compose 形态三个文件 **MISSING**（缺陷原形），见上 |

## 10. 本记录未覆盖的既有 WIP

本分支的 WIP（用户未提交的工作）不在本记录范围：人格落点迁移（`agentcore/migrate_layout.py`）、
群聊访客 where 轴、jobs.json 模板化、隐私 CI 门禁（`scripts/check_privacy.py`）。
对应记录见 `review/AC-group-guest-mode.md` / `review/FIX-group-guest-mode.md`。
