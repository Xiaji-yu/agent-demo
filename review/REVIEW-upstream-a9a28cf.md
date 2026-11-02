# REVIEW — 上游 main `a9a28cf`（1.0.4 评审修复批次）+ 本 fork 差异面评审

> 范围：上游 `Xiaji-yu/agent-demo` main 顶端 merge commit `a9a28cf7779fea8389cf510d077c12a8166a275a`
> （parents `858afcc` = 旧 main、`f786183` = `fix/review-63f9444-858afcc`；相对 `858afcc` 净变更
> **32 文件 +1149/-123**）。同时覆盖本 fork（`Daiyimo/agent-demo`）与上游的**差异面**——
> 本 fork main 停在 `3648164`，落后上游 main **45 个 commit**。
> 分支：`fix/hardening-review-a9a28cf`（从 `fix/guest-mode-p0` 的未提交 WIP 基线上切出，未 commit / 未 push）。
> 修复记录：[FIX-review-a9a28cf.md](FIX-review-a9a28cf.md)。

## 0. 结论摘要

| 维度 | 结论 |
|---|---|
| 上游 a9a28cf 本体 | 评审质量高：H1/M1–M12 全部有复现证据，L 批 13 条逐条落码，收尾实测 3478 passed/73 skipped + ruff 0.17.1 双绿 + pyright 0 errors + PG 契约 57 passed |
| 上游**显式遗留**的 19 条 | 逐条复核：**3 条为真且本 fork 同样未修**（已修，见 §2）；**5 条前提为假/不适用**（§3 证伪）；其余为低优先级或既有设计取舍（§4） |
| 本 fork 自身引入的新面 | 4 项（where 轴 D1/D4、人格落点迁移、jobs.json 模板化、隐私 CI 门禁）与上游同题并行实现，**结论一致**；另有 2 项 `.env.example` 与代码真值脱节（§5，已修） |
| 门禁结论 | 无 H 阻断；已修 6 项（FIX-1–FIX-6），遗留项见 §4 |

**上游评审的原始产物**（已入库 a9a28cf，`git show a9a28cf:review/REVIEW-63f9444..858afcc.md` 可取）：
H×1、M×12、L×29，证伪 20+ 项；H1 阻断发布。本报告不重复其内容，只记录**对其的复核**与**其外的发现**。

## 1. 对上游 a9a28cf 的复核

### 1.1 H1（USB 冷备 jobs.json 缺失）——复核为真，形态比报告更细

`scripts/backup_usb.sh` 的 cron jobs.json 只进 `REQUIRED_FILES` 必含清单、没有 rsync 复制语句。
主代理双形态实测：

- **形态 A（CWD=$SRC，即出货 unit 的 `WorkingDirectory` 生产形态）**：每周冷备必 `exit 1`；
- **形态 B（CWD≠$SRC）**：静默缺件（备份“成功”但没有 jobs.json）。

上游修复 = 判定与复制都用 `$SRC` 前缀 + `mkdir -p $TMP/data/cron`。**复核意见**：该修复正确，
但根因是「必含清单」与「复制清单」两份名单靠人工同步——第三份目录清单同类问题在
`Dockerfile` 的 `mkdir` 预建清单上同样存在（上游遗留项之一，见 §4）。

### 1.2 M1–M12 抽查复核

| 项 | 上游结论 | 本代理复核 |
|---|---|---|
| M1 reminder 空 uid 落管理员哨兵 | 入口显式拒绝空 user_id | **为真**。本 fork 因基线更早，`reminder_list/cancel` 仍是 `user_id or None` 旧写法 → FIX-1 移植其核心守卫（差异见 §2-FIX-1） |
| M2 cron 私聊轮次可见全部超管技能 | 轮次身份改伪用户 + `unattended=True` | 为真（推演）。本 fork 已由 WIP 的 D4/M2 修复（`user_id="cron:<job_id>"`、`group_id=None`、`owner=True`、`isolated=True`），等价 |
| M3 growth 写入真实用户 | `engine._tool_loop` 增 `unattended` 形参 | 为真（推演）。本 fork 由 WIP 的 `isolated=True` 达成同效果 |
| M4 `/openclaw import` 中文人格名崩溃 | 折叠非法字符 + try/except 逐条报告 | 为真（已复现） |
| M5 `_slug` 保留 unicode 但 `_ID_RE` 只认 ASCII | `_ID_RE` 放宽为 `^\w[\w-]{0,63}$` | 为真（已复现：纯中文 name 无显式 id 的 job 全被拒） |
| M6 `/persona new\|edit` 多行正文压成单行 | `persona_utils` 新增 `_strip_cmd` + DOTALL 整体匹配 | 为真（已复现） |
| M7 QQ 通道 `/persona edit` 无改前备份但回执谎称有 | 三个备份助手下沉 `agentcore/personas/backup.py` | 为真（静态） |
| M8 `/openclaw import` 原样保留 `enabled: true` | 导入一律 `j.enabled=False` | 为真（已复现） |
| M9 `/cron run` 试跑 create_task 无锁 | `AgentTurnRunner` 加懒加载 `asyncio.Lock` | 为真（推演） |
| M10 web 登录审计缺 `source_ip` | 补 + 测试 | 为真（已复现） |
| M11 `group_context._fmt_wall` 跨年失真 | 补第三档 `%Y-%m-%d %H:%M` | 为真（静态） |
| M12 屏显臻宋 3.35MB 无许可文件 | 新增 `data/fonts/README.md` | 为真 |

**上游显式保留的有意行为**（复核认可，勿当 bug 修）：试跑推进 `next_run`（M9 备注）、
`AGENT_KB_ENABLED=` 空串语义（见 §2-FIX-2 注）。

## 2. 上游遗留项中「为真且本 fork 同样未修」的三条 → 已修

### FIX-1 reminder 空 uid 越权（上游 M1 的移植）

上游 M1 的修复比“入口拒绝”多走一步：**超管看全量时逐条标注归属 QQ 与目标会话**
（`_is_admin(uid)` + `schedule_list(None if admin else uid)` + 行尾 `（归属 QQ …，…）`）。
本 fork 的 reminder **没有超管分支**（既存差异，非本次回归），故 FIX-1 只移植核心守卫：

- `agentcore/skills/reminder_skills.py:103-107` list：空 uid → `错误：拿不到会话信息，无法查看提醒`，
  并去掉 `not user_id or` 的空操作过滤；
- `agentcore/skills/reminder_skills.py:133-137` cancel：空 uid → `错误：拿不到会话信息，无法取消提醒`；
- 与 `reminder_add_skill`（`:70`）同款口径。

**为什么这不是“恒真防御”**：`registry.execute` 是公开 API，handler 默认值 `user_id=""` 生效时
即静默获得跨用户视图。`engine.py:1466-1467` 在 execute 前 `pop("user_id")` 使 LLM 永不能注入，
主链路也恒非空——但这个口子不该靠“调用方恰好都传了”来守（latent privilege-escalation primitive）。

**store 的 `None` = 管理员哨兵契约（两实现一致，勿改）**：
`agentcore/memory/store.py:476` docstring；内存 `:1059-1069` / `:1080-1089`；
PG `:1817-1830` / `:1832-1845`（`user_id is None` 时**不加 WHERE 子句**）；
`tests/test_store_contract.py:400/433` 契约锁死。

**连带**：`tests/test_tools.py` 原 `test_push_rows_hidden_from_reminder_tools` 借空 uid 当管理员
来断言“取消定时推送任务时返回 `/push off` 指引”——手段错误但意图真实，已改为“行属于调用者自己”触达。

### FIX-2 布尔 env 未迁移（上游说“三处”，实为 29 处 / 6 处真错）

上游 `REVIEW-63f9444..858afcc.md:103/:166` 只点 `web.py:1031`/`rag/service.py:43`/`rag/distill.py:70`
（且 `web.py:1031` 已位移到 `web.py:990`）。**全仓 AST + 逐站点实测**后实际分布：

| 组 | 站点数 | 性质 |
|---|---|---|
| **B 真错** | 6 | `web.py:98 _write_enabled`（只认字面 `"1"`，`true/yes/on` 静默不挂写面）；`pipeline.py:92 vision_enabled`（**漏 `.lower()`**，`TRUE/True/ON/Yes` 静默关识图，且这是 settings_spec vision 簇 gate，gate 判错则真门失效）；`help_render.py:51 image_enabled`（空串→False 尽管默认 True）；`poke.py:64 enabled`（同款）；`rag/distill.py:75 _env_flag`（集合缺 `"on"`，`AGENT_KB_DISTILL_PRIVATE=on` 旧口径 False）；`rag/service.py:55 _env_bool`（`AGENT_KB_ENABLED=disabled` → **False**，全仓唯一「脏值把默认 True 翻成 False」） |
| C 纯静默 | 5 | `logging_setup.py:30`、`__init__.py:565` / `:808`、`web.py:415`、`web.py:990 _gate_value`（`disabled` 两边都 True，只是 legacy 无日志） |
| D 默认 False 的 `in {TRUE}` | 5 | `matcher.py:149`、`media.py:84`（SSRF 邻近）、`pipeline.py:105`、`budget.py:160`（成本闸静默关）、`schema_repair.py:66`（破坏性迁移，刻意 fail-safe） |
| E 已有 WARNING | 3 | `outbound.py:122`、`nudge.py:141/:158`、`config_write.py:76` / `config_yaml.py:453`（脏值**忽略 env 回退配置文件**，盲迁改行为） |
| F JSON `enabled` 字段 | 3 | `llm/providers.py:192`、`skills/search_providers.py:138`、`scheduler/jobs.py:212`（同形：**空串已被特判为 False**，迁移必须保留） |
| G 排除 | — | `db_backup.py:75`（三态 mode，刻意 fail-loud）、`action_skills.py:563`（LLM 参数非 env）、`push.py:147`（有告警）、`admin.py:1857/1870`（仅回显） |

**已迁移的只有 B 组 6 处**（C/D/E/F 组不动：C/D 值不变、E 行为不同、F 非 env 且空串语义须保留）。

**关键语义约束（两处）**：`help_render.image_enabled` / `poke.enabled` 的“显式空串 = 关”**不能**
直接交给 `env_bool`（其口径是空值取默认 = 开）。实现为先判 `raw is None` 再走 `env_flag`：
空串 → `parse_bool` 返 None → 取 `default=False`。

**有意行为变化（1 处，已入档）**：`AGENT_KB_ENABLED=` 空串旧口径 False、新口径取默认
（`config.yaml` 的 `rag.enabled`）。“设了但留空”= 没配好，不是关掉——与 boolenv 及其余全部布尔 env 一致。

**`_gate_value` 的连带风险（未改，仅记录）**：`web.py:990 _gate_value` 的 cluster gate 判
`not in {"0","false","no","off"}`，`disabled` 会让 `_ensure_cluster_enabled` 的真门失效
（前端灰置只是提示）。属 C 组，值不变，故本次不动；建议下轮随 C 组一起迁移。

### FIX-3 cron 永触发表达式 → 每 60s 烧一次完整 LLM 回合

上游遗留项原文「`_next_run_ms` 失败返 0 与『立即执行』耦合」。**主代理实跑复现**：

- 耦合点 `agentcore/scheduler/agent_turn.py`（改前）`if job.next_run_at_ms and job.next_run_at_ms > now_ms: continue`
  —— **0 为假 → 永不跳过**；
- 两种失效形态**分化**：**非法 cron 语法**在 `agentcore/scheduler/jobs.py:228-234`
  （`_validate_job` 的 `CronTrigger.from_crontab`）就被拒，走 errors + tick 告警，**不是**热循环；
  **可解析但永触发**（`0 0 30 2 *` = 2 月 30 日）`from_crontab` 成功但 `get_next_fire_time` 永远 None
  → `_next_run_ms` 返 0 → **真 bug**：repro A4 三个 tick 各跑一次完整 LLM agent turn + 投递，
  `nextRunAtMs` 恒 0，永不标记 failed，60s 一次无限烧 token；
- **非法时区两者都不算**：`agent_turn.py:36-55 _zone_for` 回退进程 tz 仍算得出（副作用：该 warning
  无去重，每次 `run_job` 调两次）；
- **apscheduler 缺失**：`_next_run_ms` 返 0 且 `jobs.py:229` 无保护 import 让 `parse_jobs` 抛
  ImportError，逃出 `agent_turn.py:220` 的 `except jobs_mod.JobsError` 直达 `AgentScheduler._guard`。

**修复（两处都改，理由：单改装载期不够——`run_job`/`tick` 可能收到绕过 `_validate_job` 直接构造的
`CronJob`；单改 tick 也不够——装载期不拒的话 job 会先跑一次完整 LLM 回合才发现）**：

1. `agentcore/scheduler/jobs.py:238-245 _validate_job`：`from_crontab` 后判
   `if trigger.get_next_fire_time(None, agent_tz.now()) is None: return None, f"{name}: cron 表达式永触发（不存在满足条件的日期）：{cron!r}"`；
2. `agentcore/scheduler/agent_turn.py:63 _NEVER_FIRE = -1`（0 仍表示“从没跑过/依赖缺失”，
   负值 = 永触发）；`:85` 末行 `return int(nxt.timestamp() * 1000) if nxt else _NEVER_FIRE`；
   `:169-171 run_job` 入口守卫 `if _next_run_ms(job.cron, job.tz) == _NEVER_FIRE: self._warn_dead_once(job); return "failed"`
   （不跑引擎也不推进 next_run——一条永远不会有下次执行的 job，写任何 `next_run_at_ms` 都是假的）；
   `:126-137 _warn_dead_once` 按 job.id 去重（tick 60s 一轮，不去重刷 1440 条/天）。

**守卫放 `run_job` 而非只放 tick 的理由**（测试 docstring 原文）：「run_job 是执行一条 job 的唯一入口
（tick 也走它），所以守卫放这里而不是只放 tick——放 tick 的版本在装载期修复后不可达，用例会退化成
恒真断言（删掉守卫照样绿）」。

### FIX-4 `sink.py send()` return 后 11 条不可达代码

上游遗留项原文「`sink.py send() return` 后不可达代码」。**主代理用 `sys.settrace` 运行时证明**：
`sink.py:104 return (await self.send_once(target, message)) == "sent"`，同函数体 `:98-129`
共 11 条语句全部不可达（target 解析/校验、kind 检查、`_bots()`、`throttle.acquire`、bot 循环、
末尾 warning、`return False`）——这是 `send_once` 三态化之前 `send()` 的**旧布尔实现残留**。
三种输入（`private:abc`/`private:123`/`bogus:1`）实际执行行号恒为 `[104]`，均返回 False。

**隐患（为什么必须删而不是留着）**：一旦提前 return 被移除/重排，`send()` 会对 uncertain 异常
换 bot 重发，违反 AGENTS.md §4「结果不确定绝不重发」。修复 = 删除 11 条死代码 + docstring 写明
「这里绝不能自己实现重试」。

## 3. 上游遗留项中被证伪 / 不适用于本 fork 的（防止下轮重复调查）

| 上游遗留项 | 裁定 | 证据 |
|---|---|---|
| `create_task` 未存引用 + `_pending` 不清扫 | **REFUTED** | `agentcore/scheduler/` 零个 `create_task`/`ensure_future`，全部 inline await（`agent_turn.py:238/175/194`）；全仓 6 处 create_task 都持强引用（`personas/growth.py:89-95 self._tasks: set` + `add_done_callback(discard)`、`debounce.py:35-45`、`__init__.py:365 _prewarm_task`、`web.py:3949 _backup_job["_task"]`、`admin.py:902 state["task"]`、`reboot.py:429`） |
| `AGENT_WEB_SESSION_TTL_MIN` 无上限 | **REFUTED（变量不存在）** | 全仓 `grep SESSION_TTL` 0 命中。web 是静态 bearer token 无服务端 session（`web.py:175 _web_token()`、`:5110-5144 secrets.compare_digest`、浏览器侧 sessionStorage `:1763-1782`）。唯一时间窗全是硬编码模块常量：`_HISTORY_TTL=60.0`(:66)、`_WRITE_WINDOW=300`(:77)、`_DOWNLOAD_TICKET_TTL=90`(:367)、`_CONFIRM_FAIL_WINDOW_S=900`(:88)。真实存在的 TTL env 只有**下界** clamp 无上界：`group_context.py:57`、`pipeline.py:93` |
| `deploy.py _docker_data_root` 不复刻 compose 优先级 | **REFUTED（函数不存在）** | `git grep docker_data_root` 0 命中、`git grep AGENT_DATA_DIR -- '*.py'` 0 命中；数据根纯是 compose 层变量（`docker-compose.yml:114-128`）。**但同族缺陷真实**：`scripts/deploy.py:249-251`（cmd_check）`value = env.get(key) or os.environ.get(key) or ""` 是 **.env 盖过 shell**，与 compose 及 `load_dotenv(override=False)` 都相反；`:392-394`（cmd_db）`user = (read_env_file(ENV_FILE).get("POSTGRES_USER") if ENV_FILE.is_file() else None) or "qqagent"` 只读 `.env` 从不看 `os.environ` → 实测 shell `POSTGRES_USER=shelluser` + `.env=envuser` 时 deploy 用 `pg_isready -U envuser` 而库实际是 shelluser → **假 FAIL**。同型 `:257` SUPERUSERS、`:268` DATABASE_URL、`:282` AGENT_SCHEDULER_TZ |
| `run_cron_action` jobs_path 注入陷阱 | **REFUTED（函数不存在）** | 残留风险真实但形态不同：`jobs.py:66 _DEFAULT_DIR="data/cron"`、`:127-134 cron_dir/jobs_path` CWD 相对无守卫，`:483 p = path or jobs_path()`，写路径唯一拒绝是 `:402-403 if p.is_symlink(): raise JobsError("jobs_file_is_symlink")`。生产写入方只有 `__init__.py:761-767 AgentTurnRunner(..., jobs_file=jobs_path())`。现有纪律 = docstring `:466-471`（记 2026-10-28 事故）+ 测试显式传 path |
| 超管群聊 `reminder_list` 全量视图可广播他人提醒正文 | **不适用** | 本 fork reminder 无超管分支（FIX-1 只移植核心守卫，未引入全量视图） |
| 禁写死架构守卫漏相对路径与部分 tuple | **洞真实，但“tuple 形态漏检”被推翻** | `tests/test_repo_guards.py:50-64 find_nonebot_imports`。实测**抓住**：`import nonebot`/`import nonebot.abc`/`as nb`/`import os, nonebot`/`from nonebot import x`/`from nonebot.abc import X`/`from nonebot import (a, b)`（括号形态 AST 与无括号相同）/`from nonebot import *`/`from .nonebot import x`（误报方向）。**真盲区**：`from . import nonebot`、`from .. import nonebot`（ImportFrom module=None、level>0，`:59` 只得 `[""]`，且 `node.names` 里的 alias 从不被检查）；`importlib.import_module("nonebot")`、`__import__("nonebot")`（非 import 节点，整体逃检；agentcore 现无此类用法）。真实树扫描只有 `agentcore/skills/file_sender.py:17,18`（guarded=True）= 已登记豁免，**无当前违规** |

**同文件其它守卫盲区（静态，未修）**：`find_localtime_uses`（行正则 `time.localtime(`）6 种违形态只抓 1 种
（漏跨行、`from time import localtime` + 裸调、`import time as t`、getattr）；`find_naive_datetime_calls` 漏别名
`from datetime import datetime as DT; DT.now()` 与函数别名；`find_env_names` 8 种读取形态只认 2 种
（docstring `:175-177` 自认盲区 + 未记 `os.environ.setdefault`）；`unignored_secrets` 是 gitignore 近似
（无 `!` 否定、无 `**`）；`_broken_json_fences` 只认 json 围栏。

## 4. 上游遗留项中「为真但本次不修」的（建议下轮选题池）

| 项 | 性质 | 本次不修的理由 |
|---|---|---|
| `tick` 串行致触发时刻漂移 | 实测 3 个到期 job × 0.5s → 启动偏移 +0.003s/+0.578s/+1.260s，第 k 个 job 漂移 = 前面所有 job 耗时之和，无界；叠加 `scheduler/__init__.py:73 max_instances=1` + `:71 misfire_grace_time=max(30, seconds)` → tick 超时被**丢弃**（实测日志 `skipped: maximum number of running instances reached (1)`）。对照 `nudge.py:804` 有 `self._lock` | 属并发改造，超出“最小改动”；需先定“并行跑 agent_turn 是否可接受”的产品问题 |
| cron 私聊投递目标零准入 | 私聊目标（`_target_allowed` 直接 `return True`）可投给任意 QQ | 收窄至 `SUPERUSERS ∪ 历史会话` 是行为变更，需用户拍板 |
| `deleteAfterRun` 死字段 | 装载/校验读了但执行链无消费者 | 低危；删字段是 schema 变更 |
| invalid-token 与 TOTP 共享限速窗口 | `_login_fails` 同一把计数器 | 需先确认是否刻意同源限速 |
| 冷备失败可观测性 | `OnFailure=` 只落 journal；建议 `data/state/usb-backup-status.json` + web 备份页展示 | 需新增运行期文件 + web 面，超出本次范围 |
| M9 试跑推进 `next_run` | 有意保留（上游明确备注） | 设计取舍 |
| 群聊 `/cron` 拒绝文案口径 | 措辞 | Low |
| `_login` 内重复 import ×4 / `_next_run_ms` 每次 `run_job` 调两次 | 卫生 | Low |
| Dockerfile `mkdir` 预建清单是第三份目录清单无守卫 | 与 H1 同根因（名单靠人工同步） | 需先定“单一事实源”方案 |
| `uv.lock` revision 5→3 两版本互踩 | 工具链 | 需 uv 版本对齐 |
| `search` 8 格式 URL/headers/payload 双实现重复 | 重构 | 违反最小改动 |
| `features-messaging` 与 AGENTS 路由行表述张力 | 文档 | Low |
| `deploy.py` 的 `.env` 盖过 shell（§3 已证真实） | 假 FAIL / 配置优先级倒置 | 与 §3 同族，建议与 `cmd_db` 一并修 |

## 5. 本 fork 差异面的发现（已修）

### FIX-5 `.env.example` 缺 `AGENT_GROUP_EXTRA_SKILLS`（守卫红）

`tests/test_repo_guards.py::TestEnvDocs::test_real_tree_env_vars_documented` 在 WIP 上**红**：
`AssertionError: 代码读取的 env 未出现在 .env.example：['AGENT_GROUP_EXTRA_SKILLS']`。
读取点 `plugins/qq_agent_adapter/__init__.py:415`，`settings_spec.py:1692` 与 `docs/features-skills.md:24`
都有，唯 `.env.example` 缺（`review/FIX-group-guest-mode.md:134` 已记「仍红——等用户补」）。
已在 `AGENT_PERMISSION_LEVEL=medium` 之后插入 3 行注释 + `AGENT_GROUP_EXTRA_SKILLS=`
（`.env.example:471-474`）。修后 `pytest tests/test_repo_guards.py -q` → **23 passed**（原 1 failed, 22 passed）。

### FIX-6 `.env.example` 的 `AGENT_CRON_DIR` 与代码真值脱节

`.env.example` 写 `data/workspace/cron`，而代码真值是 `agentcore/scheduler/jobs.py:67
_DEFAULT_DIR = "data/cron"`、`settings_spec.py:1302` 默认 `"data/cron"`（WIP 的 P2 已把 cron 搬出沙箱）。
已订正为 `data/cron`（`.env.example:537`）并补「运行期产物（gitignore），模板
`docs/examples/cron-jobs.example.json`，首启由 `compose_init` 复制」。

## 6. 环境限制（影响所有验证数字的口径）

本机 Windows，`docs/development.md:65-82` 明确「Windows 有已知阻塞项，暂不支持」：
平台矩阵第 77 行「Web 受控写入：`os.O_NOFOLLOW` 在 Windows 不存在，保存即 500」；
第 79-82 行「默认测试套件有约 200 条存量环境失败（`os.O_NOLLOW` 缺失、CRLF、缺
systemctl/free/docker/psql），**不是代码缺陷**」。

- 基线（本次改动前）：`167 failed, 3423 passed, 84 skipped`；其中 152 条 traceback 是
  `AttributeError: module 'os' has no attribute 'O_NOFOLLOW'`。
- **`O_NOFOLLOW` 未设防站点（`git show main:<path>` 确认 main 上同样存在，非 WIP 引入）**：
  `agentcore/totp.py:74`、`plugins/qq_agent_adapter/config_write.py:328`、
  `plugins/qq_agent_adapter/web.py:495`、`agentcore/skills/search_providers.py:231` 四处裸用；
  已设防惯用法见 `personas/manager.py:370`、`llm/providers.py:342`、`scheduler/jobs.py:348`、
  `skills/archive_skills.py:49`（`getattr(os, "O_NOFOLLOW", 0)`）。**定夺：不修**（违背最小改动，
  且上游显式决定 Windows 暂不支持）。
- 因此 `scripts/check_review_docs.py --write-stats`（要求退出码 0）在 Windows 上跑不了，
  BACKLOG 计数块只能在套件绿的 Linux 上重新生成。
- 真门禁以 CI 为准（`.github/workflows/ci.yml`：ruff check + ruff format --check + pytest +
  `pgvector/pgvector:pg16` service 导出 `TEST_DATABASE_URL`）。

## 7. 未验证面

- **CI 未跑**（分支未推送；按 AGENTS.md §2.4 不自动 push）。
- **PG 套件未跑**（本机无容器）。本次改动未触及 store 双实现 / DDL，PG 契约不受影响。
- **真机未验**：reminder 空 uid 越权、cron 永触发、boolenv 六站点、sink 不可达代码均为
  内存/静态/注入时钟复现，未在真实 QQ 群 + 真实 LLM 端点上演。
- **上游 a9a28cf 的 CI 结论**：经 GitHub API 匿名查询确认（无需登录）。

## 8. 架构评审轮（2026-11-02，用户追问「架构上还有什么问题」）

前六项修完后用户追问架构层问题，遂做一轮**架构评审**（只读子代理 ×4：分层缝 / 事实源完整性 /
横切不变量 / 并发生命周期，加主代理逐项复核）。已修 **A 组 4 项**（FIX 文档 §9）；
B/C/D 组经用户确认**本轮不做**，此处存档供下轮选题。

### 8.1 规模与正向结论（先记录「已经干净的部分」，避免下轮重复调查）

- **分层铁律干净**：`agentcore/` 下零个 import plugins（仅 `groupstats/config.py:154`、`groupstats/raw.py:76`
  两条注释自述纪律）；109 个 agentcore 模块逐个 import 全部成功、无环；外部依赖实测只有
  httpx/asyncpg/PIL/apscheduler/dotenv/jieba/pysilk/trafilatura/wikitextparser/yaml + 2 处已登记 nonebot
  受保护可选导入（`agentcore/skills/file_sender.py`，AGENTS.md §1 唯一既存豁免）。
  `pyproject.toml` 把 `apscheduler>=3.10` 列为**硬依赖** → 「apscheduler 缺失致 ImportError」的严重度
  应降级为 packaging 不变量，**不是缺陷**。
- **统一时钟零绕过**（AST 枚举）：tz.py 之外非 tests 代码零个 `datetime.now()/utcnow()/localtime()/date.today()`；
  `fromtimestamp` 14 处全走 `tz.zoneinfo()`（唯一例外 `memory/store.py:108` 是 PG timestamptz 绑定，合规）；
  `time.time()` 123 处全为 epoch/elapsed/TTL。
- **权限三层未发现第四条绕过路径**：`registry.execute` 全仓仅 `engine.py:1515` 一处调用；owner 仅来自
  cron 回合且带 `ALLOWED_GROUPS` 闸；46 个注册技能的 superuser/guest 标注核验通过。
- **真死 env 键 = 0**（早前「68 个死键」是扫描伪影）。
- **规模**：agentcore+plugins 136 个 .py / 51021 行；tests 95 个 .py / 57116 行（测试:源码 = 1.12:1）。
  最大单文件：`plugins/qq_agent_adapter/web.py` 5165 行 / 54 个顶层函数 / 0 个类 / 26 个路由，
  全部经 8 个闭包 `@app.get/post` 注册（`:3585 _mount_core_routes`、`:3884 _mount_backup_write_routes`、
  `:4034 _mount_settings_write_routes`、`:4223 _mount_llm_provider_routes`、`:4438 _mount_search_provider_routes`、
  `:4624 _mount_nudge_write_routes`、`:4784 _mount_persona_write_routes`、`:5052 _mount_reboot_routes`）。
- **静态引用分析的教训**：AST 枚举 import 求「从未被 import 的模块」误报 4 个
  （`memory_route.py`/`music_route.py`/`stats_route.py`/`bot.py`）——它们经
  `plugins/qq_agent_adapter/__init__.py:208 _load_plugin_modules()` 的 `importlib.import_module`
  字符串式条件装载。**本仓做静态引用分析必须把字符串实参也算作引用边**。

### 8.2 A 组（已修，细节见 FIX §9）

| # | 问题 | 一句话根因 |
|---|---|---|
| A1 | 状态文件不进 gitignore | 2026-10-28 做 compose 重定向时只补了 `data/state/llm-providers.json` 一行，另外 6 个文件 + `data/mirror`、`data/ssh` 全漏；`tests/test_repo_guards.py:224-248 unignored_secrets` 的覆盖判据不支持目录前缀，所以守卫也查不出 |
| A2 | 保留名尾部点绕过 | Win32 剥尾部点 + `fs.py:91-97 resolve()` 对不存在路径只做词法归一 → `cron./jobs.json` 首写那一瞬间绕过 `reserved_root` |
| A3 | 冷备缺状态文件 | `backup_usb.sh:115` 只查 `data/` 根，compose 下落点在 `data/state/`；缺失时静默跳过，MANIFEST 双道校验照样全绿 |
| A4 | 宿主机状态技能不过围栏 | `_UNTRUSTED_TOOL_RESULTS` 收录了 `log_tail`/`docker_logs`/`docker_stats`，漏了同性质的 `system_status`/`proc_detail`/`disk_usage`/`port_check`/`service_status` |

### 8.3 B 组「配置单一事实源」（本轮不做，下轮选题池）

- **两套 schema 权威**（AGENTS.md §4 已登记为例外、BACKLOG §2.8 追踪）：记忆库 DDL 唯一来源是
  `agentcore/memory/migrations/0001_baseline.sql`（账本 + 校验和 + 版本守卫）；群统计四表明抄在
  `groupstats/store.py:34/44/53` 与 `groupstats/raw.py:40`，由 `init()` 时 `CREATE TABLE IF NOT EXISTS` 自建。
  `memory/schema_repair.py:187 repair_schema` 只覆盖记忆表，**够不到统计四表** → restore 的「拒绝比代码新的库」
  守卫对统计四表的版本漂移完全无感知。
- **18 个生产 env 键不在 `ENV_SPECS`**（`AGENT_REBOOT_*`/`AGENT_SSH_*`/`AGENT_HA_*`/`AGENT_SKILLS_DIR` 等），
  全部靠 `.env.example` **注释**骗过 `undocumented_envs` 的子串判据；`find_env_names` 还漏 boolenv 包装 6 键
  与「键名在第二位置参」的 `_flag/_scalar` 包装 → **用 AGENTS.md 鼓励的 `env_bool` 形态写新 env，守卫永远不红**。
- **spec default 与代码 default 三处不一致**：`LLM_MODEL` spec `step-3.7-flash`（`settings_spec.py:255`）vs
  代码 `step-1-flash`（`llm/client.py:182`，且 `:196/:198` 两处回落同值）→ **代码是离群值**；
  `AGENT_WAKE_WORDS` spec `小助手,助手` vs 代码 `''`（`wakewords.py:11`）；
  `AGENT_WEB_WRITE` kind=str 却按 boolenv 读（`web.py:98`）。
- **`.env.example` 自身同键两值**：`AGENT_REBOOT_NOTICE_FILE` `:66` vs `:124`、`AGENT_PUSH_STATE_FILE` `:67` vs `:526`。
- **AGENTS.md:132 配置行与真实配置模型脱节**：`agentcore/config_json.py:1-46` 的完整优先级是
  **真实环境变量 > web 写面文件（`config/agent.env`）> JSON（`config/agent.json`）> .env**，
  由 `env_snapshot.prime` 的 `before_dotenv` 钩子保证顺序；AGENTS.md 只写「env > config.yaml > 默认」，
  完全没提 agent.json / agent.env 两层 → 照 AGENTS.md 会推理错优先级。
- **运行期目录清单 6 份拷贝**：`Dockerfile:183-186` mkdir 15 个 / 两份 compose 挂载 /
  `compose_init.py:42-58 DATA_DIRS` / `.gitignore` / `deploy.py:159` unit 注释。
- **备份实为 4 条线不是 2 条**：`backup/db_backup.py`（应用内，age 加密 fail-loud、keep=7 天）+
  `backup_usb.sh`（KEEP=8、MAX_AGE_H=30）+ `backup_mirror.sh`（rsync、KEEP_DAYS=30、REQUIRE_PRIVATE_IP）+
  `_mirror_backup`（本地 copy2，与 mirror.sh 功能重叠）。三条宿主机线的新鲜度都依赖应用内 backup.cron，
  停摆只有 journal 一行、无全局告警。
- **约 10 处 `kind=bool` 键仍手解绕过 boolenv**（C/D 组分类见 FIX-2 §2）。

### 8.4 C 组「并发生命周期」（本轮不做）

- **`engine.py:1284 self.memory.get_persona_growth(user_id)` 是 `gather` 四支里唯一无兜底的**
  （另三支 `_recall_facts` `:771-789` 返 `[]`、`_load_persona_text` `:850-871` 返 `""`、
  `_recall_knowledge` `:883-888` 返 `""`；`:1540` 的 gather 实际安全，`_exec_tool` 自带 try/except `:1513-1527`）。
  子代理「gather 缺 `return_exceptions` 的系统性问题」**过宽**，实为一个具体未兜底调用点。
- **`agent_turn.py:207-213 except Exception: logger.exception(...); text = ""`** → 只打「产出为空，不投递」
  WARNING，异常类型丢失。
- **`growth.py:97-101 aclose()` 零调用方**（docstring 自称「lifecycle 编排调用；幂等」）；
  `debounce.py:124-128 cancel_all` 仅 `tests/test_perf.py:224` 调，`lifecycle.py:44` 只 flush 不 cancel。
- **`web.py:357/:3932-3954 _backup_job["_task"]` 与 `admin.py:902 state["task"]` 既不 await 也不 cancel**；
  `__init__.py:1157-1160` 的 closers 只有 emb + media 两个。
- **cron 轮次不过并发闸门**：`matcher.py:393-427 _turn_semaphore` 只包聊天窗口，
  `agent_turn.py:207 text = await self.engine.run(...)` 不过闸。
- **预算命中四种日志待遇**：`engine.py:1068-1071` INFO、`:1800-1802` **完全静默 return**、
  `:1383-1387` WARNING、`:1740-1744` INFO → 同一事件 1 静默 + 2 INFO + 1 WARNING。
- **`groupstats/config.py:42/:193` 的 `minimum=1`** 会被 `add_interval` 静默抬到 5（其余 tick 已普遍钳制且有测试）。
- **跨层无类型 dict 契约的具体漂移**：engine 只读 8 个 key（`group_id`/`user_id`/`sender_card`/`self_id`/
  `nickname`/`owner`/`isolated`/`route`），三个生产者形状互不相同（`matcher.py:508-518` 无 nickname/owner/isolated/route；
  `nudge.py:1851-1856` 无 sender_card/self_id；`agent_turn.py:198-204` 无 platform/sender_card/self_id/nickname）；
  **`platform` 被 2 处写入、0 处读取**。
- **`agentcore/skills/file_sender.py:24-25` 模块级读 env**（`NAPCAT_HTTP_URL`/`NAPCAT_HTTP_TOKEN` 冻结在 import 时），
  对比 `search_providers.py` 是热读；AGENTS.md §1 的文档豁免只覆盖 `:16-21` 的 nonebot 可选导入，
  **未覆盖这两个 env 冻结**。
- **`agentcore/multiagent/` 是纯占位空壳**（`__init__.py` 仅 64 字节 docstring + `__all__ = []`），
  成本是导航噪音。

### 8.5 D 组「架构议题」（本轮不做）

- **web.py 单体 + 闭包挂载**：5165 行 / 0 类 / 26 路由全在 8 个闭包里注册 → API 面在模块层不可见、
  静态枚举不到；auth 依赖 `_auth` 靠闭包形参手工传递而非模块级 `Depends`。
  **审计 seam 被 79 个缩进 import 中的 16 个同一行 `from agentcore.diagnostics import record as _record` 重复**
  （`:3626/3731/3804/3955/4022/4196/4264/4418/4473/4605/4723/4773/4883/4959/5039/5090`）；
  `agentcore/diagnostics.py:11-16` 只 import threading/time/collections/typing，**无循环风险 → 16 个局部 import 全不必要**。
  上游曾把同型 `_login` 重复 import ×4 当 L 项修掉，此处是同形态的 4 倍。
- **web.py docstring 与实现脱节**：`:1` 「Web 只读总览…（**零写入接口**）」、`:14-16` 「**只读**：没有任何写接口」，
  同文件却挂 8 个闭包写路由组。
- **文档漂移 3 处**：① `docs/development.md:25-38` 目录树只列 12 个 adapter 文件，实际 25 个
  （缺 web.py/settings_spec.py/media.py/config_write.py/config_yaml.py/model_switch.py/music_route.py/
  stats_route.py/reboot.py/help_menu.py/help_render.py/text_image.py/persona_utils.py）；
  `agentcore/` 树缺 `groupstats/`、`music/` 两包 + boolenv/budget/config_json/diagnostics/env_snapshot/
  logging_setup/migrate_layout/runtime_env/safety/tz/totp 11 个单文件模块；② `AGENTS.md:28` 同款缺失；
  ③ AGENTS.md 配置行脱节（见 §8.3）。
- **组合根是字符串式条件装载**：`__init__.py:208 _load_plugin_modules()` 按字符串名装载 8 个路由模块，
  matcher 与 admin **无 try 保护**（失败=整插件失败）；模块级 `on_command` 只在被 import 时注册，
  **命令面无法静态枚举**（也是 2026-09-30「漏接线事故」的结构性成因）。
- **无 ADR / 无 CONTEXT.md**：`docs/` 有 13 个主题文档但无 `docs/adr/`；决策散落在 68 份 review 报告 +
  BACKLOG 558 行 + AGENTS.md 216 行三处，`REVIEW-WORKFLOW.md` §9/§9.1 只按 commit 区间索引、
  不按主题/不变量索引 → 无法回答「关于 X 我们定过什么、为什么」；每轮评审都要重新推导同一批不变量
  （reminder None 哨兵语义、O_NOFOLLOW、Windows 暂不支持被反复重新提出即是症状）。
