# AC — 主动联系（沉默检测器 / Nudge）

> 状态：**已落地 P1–P3**（`feat/proactive-nudge`：`ea9d7ab` P1 骨架 / `b97ce04` P2 热点 /
> `6144e4c` P3 事件 + Web）。本文是"怎么算改好了"的验收规格 + 现状记录，
> 每条都附**可执行断言**（测试名或命令）与**变异复核**结论。
>
> 设计来源：2026-10-23 私聊日志复盘（13 天 83 条、71% 集中在 3 天、最长空档 119.8h、
> 35% 的消息发生在 00:00–05:59、facts 里有"需要上晚班 / 会通宵 vibe coding"）。
> 结论：它必须是**沉默检测器，不是闹钟**。

## 0. 范围与分层

| 文件 | 层 | 职责 |
|---|---|---|
| `agentcore/scheduler/nudge.py` | 纯 Python | 配置、状态机、时间守卫、配额、退避、候选池 |
| `agentcore/scheduler/hotspot.py` | 纯 Python | 判重 / 周轮换 / 规则筛 / LLM 评分 / 围栏 |
| `agentcore/scheduler/events.py` | 纯 Python | 记忆事件与生日的确定性抽取 |
| `agentcore/loop/engine.py` | 纯 Python | 新增 `generate_proactive`（复用 system prompt，不写 user 消息） |
| `agentcore/memory/store.py` | 纯 Python | 新增 `schedule_set_params` / `last_user_message_ts`（双实现） |
| `plugins/qq_agent_adapter/__init__.py` | NoneBot | 接线 + interval job |
| `plugins/qq_agent_adapter/matcher.py` | NoneBot | 私聊消息重置倒计时（旁路，失败不影响回复） |
| `plugins/qq_agent_adapter/admin.py` | NoneBot | `/nudge` 命令 |
| `plugins/qq_agent_adapter/web.py` | NoneBot | 「主动」标签（只读状态 + 全参数编辑 + 试发） |
| `tests/test_nudge.py` / `test_hotspot.py` / `test_engine.py` / `test_matcher.py` / `test_store_contract.py` / `test_web.py` | — | A–G 组 |

**不碰**：DDL、`db_backup.py` 表清单、恢复顺序（复用 `schedules` 表，`action='nudge'`）。
**零新增依赖**。

运行时文件（均 gitignore、0600）：`data/nudge.json`（配置）、`data/nudge-hotspot.json`（候选池）。

---

## A. 沉默检测器：节奏与三重不打扰

**A1 倒计时从你最后一条消息起算**
第 1 条 = 最后一条消息后 **6–24h 随机**；你一发话就重置。
- 验收：`tests/test_nudge.py::TestTiming::test_first_nudge_waits_six_to_twenty_four_hours`
  （断言 `last+6h <= next_run <= last+24h`，且到点才发）
- 变异复核：✅ 已抓

**A2 静默时段跨零点（默认 23:30–10:00）**
按 `start <= cur < end` 判定，23:45 与 00:30 都算静默；`quiet_end_ts` 把 23:45 推到**次日** 10:00。
- 验收：`TestQuietWindow::test_cross_midnight_window` / `test_quiet_end_skips_to_next_morning`
- 变异复核：✅ 已抓（改成"全天不静默"）

**A3 晚睡保护（01:00 后说话 → 最早 max(10:00, 最后消息+6h)）**
两条约束可能互相触发（6h 落点仍在静默内），因此用**有界循环**反复推后，不是一次性判断。
- 验收：`TestEarliestAllowed::test_late_night_floor`（01:30→10:00、05:00→11:00、04:04→10:04）
- 变异复核：✅ 已抓（循环改 1 次）

**A4 活跃聊天保护（30 分钟内绝不主动）**
即使计划时刻已到，你 10 分钟前还在说也不发；31 分钟后放行。
- 验收：`TestTiming::test_active_chat_blocks_even_when_due`
- 变异复核：✅ 已抓

**A5 「在忙」暂停 24h 且不计未回复**
命中 `在忙|正忙|没空|别烦|不想聊|别发了|先不聊|在开会|工作中|别找我`；
"我不忙"不命中（词表里没有"不忙"）。
- 验收：`TestTiming::test_paused_and_busy_do_not_disable_the_row`、`test_busy_reply_is_conservative`
- 变异复核：✅ 已抓

**A6 暂停/止损绝不停用调度行**
用"不排下一次"实现；`schedule_mark_fired(None)` 是停用语义，用了它你先说话也醒不过来。
- 验收：上条用例断言 `enabled is True`；`_save` 只在 `next_run is not None` 时推进
- 变异复核：✅ 已抓

---

## B. 内容与历史写回

**B1 ≤60 字，按句截断**
`clip_at_sentence` 只在 `[limit*0.5, limit]` 内找最靠后句终符，找不到才硬截（不切半句）。
- 验收：`TestClipAndNickname::test_clip_at_sentence_boundary`

**B2 带人格 / 长期事实 / 关系成长 / 最近对话**
`generate_proactive` 复用 `_build_system_prompt` 与历史裁剪；不开放 tools、不写 user 消息、
不抽事实、不推进摘要水位。
- 验收：`tests/test_engine.py::TestProactiveGeneration::test_uses_persona_history_and_task_without_tools`
  （`tools is None`、人格正文在 system prompt、历史在 messages[1]、`get_history` 仍是 `["user"]`）
- 变异复核：✅ 已抓

**B3 输出预算有下限（512）**
`max_chars` 不能直接当 `max_tokens`：推理模型会把它全耗在思维链上
（实测 `max_tokens=60` → `reasoning_tokens=60`、`finish_reason=length`、`content=''`）。
正文长度仍由 B1 保证。
- 验收：`test_output_budget_has_a_floor_for_reasoning_models`
- 变异复核：✅ 已抓；线上复现命令见 `review/` 记录（step-3.7-flash 实测）

**B4 已送达才写回 assistant 历史**
你回一句"什么热点？"时引擎能接住上文；bot 自己的话不算"用户活跃"。
- 验收：`TestDelivery::test_sent_writes_back_assistant_history`
  （历史末条为 assistant、`last_user_message_ts` 不变）
- 变异复核：✅ 已抓

**B5 `uncertain` 不写历史、不重发、不计未回复**
只消耗当日配额并记住这句话（避免下次原样再发）。
- 验收：`TestDelivery::test_uncertain_does_not_resend_or_write_history`
- 变异复核：✅ 已抓

**B6 生成失败不发兜底模板**
主动消息一旦模板化就露馅；空输出 → 跳过并 1h 后重试。
- 验收：`TestDelivery::test_empty_generation_skips_without_template`

**B7 称呼从第一人称事实取，取不到回落默认**
只匹配 `用户昵称为/用户希望被称呼为/用户称呼自己为/用户要求称呼自己为/叫我`；
`用户提及的夏祭是其所在群的群成员` 这类第三人称不算。刻意**不按 session 过滤**——
"叫我夏祭"最可能来自群里。
- 验收：`TestNicknameResolution`（跨群会话取到、store 抛异常回落、显式配置优先）
- 线上实测：生产库 179 条 facts → 解析出"夏祭"（含群里那条）
- 变异复核：✅ 已抓（匹配放宽到任意事实）

---

## C. 热点与爆点

**C1 轮询与配额**
每 `hot_poll_hours`（4h）一轮，每轮最多 2 个查询；每日轮询上限 15；`hot_topics: false` 或
总开关关闭时完全不查（省搜索配额）。
- 验收：`TestHotspotPolling::test_poll_fills_pool_and_respects_interval` /
  `test_poll_cap_per_day` / `test_hot_topics_off_means_no_polling`
- 变异复核：✅ 已抓（不看间隔）

**C2 规则筛先于 LLM（不花钱也可单测）**
时效只认今天/昨天；禁区词（政治/伤亡/医疗/荐股/塌房…）与来源黑名单命中即丢；
必须命中领域词（tech）或重大词（general）；24h 内提过的不再入池。
- 验收：`tests/test_hotspot.py::TestRulePrefilter`（4 例）
- 变异复核：✅ 已抓（时效 / 相关性 / 禁区 / 去重 各 1 项）

**C3 LLM 评分解析失败一律按 0 分**
六维度（领域相关/影响范围/热度/来源权威/近期话题/新鲜度）；无括号、有括号但非法、
非数组、越界 index/score 全部按"没有评分"——宁可不发。
- 验收：`TestScoreParsing`（5 例，含 `[{"i":0,"score":}]` 这条曾漏覆盖的路径）
- 变异复核：✅ 已抓

**C4 分档与优先级**
≥8 = 爆点（事件驱动，**不被 6–24h 首次延迟挡住**）；6–7 = 普通候选（留 12h 当引子）；
<6 丢弃。事件 > 爆点 > 普通。
- 验收：`TestBreakingTakeover::test_breaking_ignores_first_delay_but_not_guards`
- 变异复核：✅ 已抓

**C5 爆点仍受三重保护，凌晨排到早上**
时间守卫不过就**不消费候选**，下个 tick 自然重试。
- 验收：`test_quiet_hours_queue_the_breaking_to_morning`
- 变异复核：✅ 已抓

**C6 配额：普通 1 + 爆点 1，总上限 3；≥9 分破例到 4**
- 验收：`test_breaking_daily_cap_and_override`
- 变异复核：✅ 已抓（第二条不拦 / 破例失效 各 1 项）

**C7 去重：同一条 24h 内只提一次；提过的话题 90 天内不再当普通候选**
爆点仍可按 24h 规则再提。
- 验收：`TestHotspotPick::test_take_breaking_skips_stale_and_recent` /
  `test_take_candidate_skips_recently_mentioned` / `test_consume_moves_to_mentioned`
- 变异复核：✅ 已抓

**C8 周轮换：科技周 70/30、综合周 50/50**
按 ISO 周奇偶（ Asia/Shanghai ），不用"距固定日期的天数"（会随启动时间漂移）。
- 验收：`TestWeekRotation::test_iso_week_parity` / `test_weights`
- 变异复核：✅ 已抓

**C9 外部内容一律过围栏，且只允许转述**
`candidate_block` 用 `fence_untrusted`；内容里的假围栏行被打散；生成指令禁止补充细节/断言真假。
- 验收：`TestCandidateBlock`（含 `test_fence_lookalike_is_neutralized`）
- 变异复核：✅ 已抓

---

## D. 记忆事件与生日

**D1 确定性抽取，宁可漏**
时间词与事件词必须同一小句；拒绝过去时/否定/周期性；只认日粒度时间词；只看 60 天内。
- 验收：`TestEventExtraction`（8 例：正例、过去时、周期、"昨天…明天"不连坐、
  下一句否定、今天已过、前瞻窗口、自定义词表）
- 变异复核：✅ 已抓（过去时不拒 / 周期不拒 / 不同句也抽 / 已过也抽 / 前瞻失效 各 1 项）

**D2 提前量按类型分档**
面试/考试/答辩 = 前一晚 20:00 + 当天 08:30；会议/出差/约见 = 当天 08:30；
截止/上线 = 前 24h；生日 = 前一天 20:00 + 当天 09:00。
- 验收：`test_triggers_by_kind`；变异复核：✅ 已抓

**D3 生日只认第一人称句式**
`(用户|我)(的)?生日是X月X日`；"用户的朋友生日是3月5日"不算。
- 验收：`TestBirthday::test_third_party_birthday_not_matched`
- 变异复核：✅ 已抓

**D4 事件优先级最高，但仍受三重保护**
凌晨的事件排到早上；当天第二个事件跳过（`event_cap`）。
- 验收：`TestEventFlow::test_event_wins_over_normal_and_breaking` /
  `test_event_respects_quiet_hours` / `test_event_daily_cap`
- 变异复核：✅ 已抓（不再最高优先 / 被首次延迟挡住 / 日配额不拦）

**D5 追问只一次；你开过口则不再追问**
问过没回 → 次日同时段再问一次 → 丢弃；你在事件之后开口 → 直接结案。
- 验收：`test_followup_once_then_done` / `test_user_reply_marks_event_done`
- 变异复核：✅ 已抓

**D6 结案要跨 tick 生效**
完成/过期的事件把 id 记进 `events_done`，扫描时跳过——否则同一条消息每轮都被重新抽出，
"问过了"永远不生效（会变成每天都问）。
- 验收：`test_scan_events_from_history`（扫两次只入池一次）、`test_prune_and_cap`
- 变异复核：✅ 已抓（不记 events_done / 扫描不去重 / 不同步回 row 各 1 项）

---

## E. 配额与退避（横切）

**E1 未回复上限 3 条，第 3 条固定是第 14 天的告别**
之后停到你先开口；绝不出现第 4 条。爆点/事件不占用告别名额。
- 验收：`TestUnansweredBackoff::test_three_strikes_then_goodbye_then_stop`
- 变异复核：✅ 已抓

**E2 第二条 = 第一条后 +48h**
- 验收：同上（断言 `next_run == last_sent_at + 48h`）

**E3 你回话即全部清零并从最新消息重排**
- 验收：`test_reply_resets_everything`

**E4 每日配额：普通 1 条（0 = 不限）**
配满顺延到**次日 00:05**（按天的条件不逐小时白跑）。
- 验收：`test_daily_cap_skips_but_keeps_schedule` / `test_daily_cap_zero_means_unlimited`

**E5 预算硬闸门：跳过并顺延，不停用、不告警**
- 验收：`test_budget_gate_skips_without_disabling`

**E6 投递失败指数退避，连续 N 次停用并告警**
- 验收：`TestDelivery::test_failure_backs_off_then_disables_with_alert`

**E7 目标没有会话记录 → 停用并告警（配错人）**
- 验收：`test_no_session_target_is_disabled_with_alert`

---

## F. 管理面

**F1 `/nudge` 命令（超管 only）**
`status|run [goodbye]|off|on|why|pause <小时>|help`；越权走统一拒绝文案；
未注册时给出可行动提示。
- 验收：`TestNudgeCommand`（解析 14 例 + 非超管被拒 + 状态/暂停流程 + 未启用提示）
- 变异复核：✅ 已抓（管理员门 / why 别名）

**F2 试发回执要能当"测试证明"**
`已发送（39 字，未回复 1/3，下次最早 10-25 17:10）`；告别后显示"已暂停，等你先开口"。
- 验收：`TestBreakingTakeover`/`TestManagement::test_run_now_bypasses_guards`
- 变异复核：✅ 已抓

**F3 管理命令必须留痕**
`[nudge-cmd] user=… action=… arg=…`（线上曾因命令不打日志，排查只能翻 DB）。
- 验收：`TestNudgeCommand::test_command_is_logged`；变异复核：✅ 已抓

**F4 Web「主动」页：只读状态不随写面门禁**
与 overview/backup 同口径；写入面（参数编辑 / 试发）照旧要 `AGENT_WEB_WRITE` + CIDR。
- 验收：`tests/test_web.py::TestNudgeWeb::test_get_requires_auth` /
  `test_get_returns_config_and_status`（响应体无 api_key）
- 变异复核：✅ 已抓（只读面被挂到写面门禁内）

**F5 Web 写入：两段确认 + 每次 2FA + 审计 + 改前备份**
HMAC 绑定"将要写入的配置文本"（拿到码也改不了别的内容）；备份落
`data/backups/nudge-YYYYmmdd-HHMMSS.json`（保留 10 份）。
- 验收：`test_save_requires_two_phase_and_totp`（含错码 400 `totp_invalid`、
  篡改值 400 `confirm_invalid_or_expired`）
- 变异复核：✅ 已抓（不校验 TOTP / 不备份）

**F6 Web 写入：后端收敛 + targets 非空**
非法值被 `_coerce` 钳制（`quiet_start=99:99` → 回退默认、`max_chars=99999` → 500）；
`targets` 为空 422。
- 验收：`test_save_clamps_bad_values` / `test_save_rejects_empty_targets`
- 变异复核：✅ 已抓（配置不收敛 / 不校验 targets）

**F7 Web 试发同样要 2FA**
错码 400；通过后因服务未注册返回 409（确认链路本身已过 2FA）。
- 验收：`test_run_requires_totp_and_audits`；变异复核：✅ 已抓

**F8 `/nudge on|off` 在 env 总开关面前要明说**
设了 `AGENT_NUDGE_ENABLED` 时它优先级更高，命令必须告知而不是假装成功。
- 验收：`TestNudgeCommand::test_on_warns_when_env_switch_overrides`

---

## G. 安全与不变量

**G1 目标只支持 `private:<QQ>`，且必须显式配置**
不做自动发现；群目标 v1 不支持（另一套 ACL 与体验问题）。`private:abc` / `private:１２３`
（全角）被拒。
- 验收：`TestConfig::test_targets_are_validated_and_deduped`

**G2 默认全关**
`enabled: false` + 空 `targets`；`AGENT_NUDGE_ENABLED` 可整体覆盖。
- 验收：`TestConfig::test_defaults_are_safe` / `test_file_overrides_and_env_kill_switch`

**G3 配置损坏不崩启动**
JSON 非法 → 告警并回退 seed/默认；文件被删 → 保留上一份配置（不静默回落默认）。
- 验收：`test_broken_file_falls_back_without_raising` / `test_config_hot_reload`

**G4 运行时文件 0600 + 原子写 + 不入库**
`data/nudge.json`、`data/nudge-hotspot.json`（`.part + os.replace`）。
- 验收：`test_save_is_atomic_and_private` / `test_state_file_roundtrip`

**G5 零 DDL、备份恢复自动覆盖**
复用 `schedules` 表（`action='nudge'`、`user_id='__nudge__'`），`db_backup.py` 表清单不变。
- 验收：`git diff` 无 `store.py` DDL 变更；`tests/test_pg_store.py` 全绿

**G6 新增 env 全部进 `.env.example` + `ENV_SPECS`**
`AGENT_NUDGE_ENABLED` / `_CONFIG` / `_TICK` / `_HOTSPOT_FILE`。
- 验收：`tests/test_repo_guards.py::TestEnvDocs` + `test_web.py::test_schema_covers_env_example_and_invariants`

**G7 旁路失败不影响主链路**
`note_user_message` 全链路 try 兜底；热点轮询失败只告警。
- 验收：`tests/test_matcher.py::TestNudgeHook::test_nudge_failure_does_not_break_the_turn` /
  `test_search_failure_does_not_break_tick`

**G8 群消息不参与倒计时**
v1 只做私聊；群消息调用 `note_user_message` 会污染私聊的沉默检测。
- 验收：`TestNudgeHook::test_group_message_does_not_notify_nudge`；变异复核：✅ 已抓

---

## H. 交付与回归（命令）

```bash
.venv/bin/python -m pytest -q                                    # 3038 passed / 62 skipped
TEST_DATABASE_URL="postgresql://qqagent:qqagent@127.0.0.1:5432/qqagent_test" \
  .venv/bin/python -m pytest -q                                  # 3113 passed / 9 skipped
.venv/bin/python -m ruff check agentcore plugins tests bot.py scripts
.venv/bin/python -m ruff format --check agentcore plugins tests bot.py scripts
.venv/bin/python scripts/check_review_docs.py
```

**变异复核**：19/19 被抓（A 组 3、B 组 4、C 组 8、D 组 6、F 组 7、G 组 1 项中的可变异项）。
复核方法：把每条 guard 故意改坏 → 对应用例必须红 → 反向编辑还原（不用 `git checkout`，
避免抹掉同文件未提交修改）。

**线上验收**（需真机器人）：

| 步骤 | 期望 |
|---|---|
| `/nudge run normal` | 收到一条 ≤60 字、带人格与记忆的搭话 + 回执含字数/未回复/下次最早 |
| `/nudge` | 状态含下次时间、上次内容、跳过原因、配额 |
| `/nudge why` | 只有下次时间与上次跳过原因 |
| `/nudge pause 72` → `/nudge` | 暂停至 72h 后 |
| `/nudge on` / `off` | 写入 `data/nudge.json`，下一 tick 生效 |
| Web「主动」页改 `max_chars` 保存 | 2FA 后落盘 + `data/backups/nudge-*.json` 备份 + 审计 |
| Web「立即试发」 | 2FA 后真发一条，toast 显示回执 |
| 静默时段内 `/nudge run` | 仍发（手动试发刻意忽略保护），自动链路不发 |

---

## I. 已知限制（如实披露）

1. **时效精度**：Tavily 只返回 `published_date`（日期，无小时），"4 小时内"只能落成
   "日期是今天/昨天"。这是数据源限制，不是实现取舍。
2. **热度是近似的**：没有热榜接口，"全网讨论热度"只能用同题结果数近似。
3. **事件扫描窗口**：走 `get_history_window(limit=200)`，超过 200 条的积压会漏扫
   （`last_scan_msg_id` 只推进到见过的最新 id）。
4. **单进程假设**：与 push 一致，第二个实例会双发。
5. **群主动发言未做**：另一套 ACL 与体验问题，v1 只做私聊。
6. **无周预算闸门**：1 亿 token/周仅作容量规划（nudge 实测占比 <0.2%），
   预算模块仍只有日维度。

---

## J. 未做项（下一步候选）

- 群主动发言（`group:<gid>` 目标 + 群人格 + ALLOWED_GROUPS 复用的 ACL）
- 事件词表/热点词表的 Web 可视化编辑已有，缺"候选人工确认"队列
- 长对话后的主动消息带上话题延续（当前只带最近 5 条防重复）
- 周预算硬闸门（自然周，对齐供应商额度）
