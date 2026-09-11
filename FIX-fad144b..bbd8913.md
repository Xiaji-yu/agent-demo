# agent-demo 评审修复记录

**对应评审**：[review/REVIEW-fad144b..bbd8913.md](review/REVIEW-fad144b..bbd8913.md)（条目编号沿用该报告 §1）
**修复日期**：2026-09-11
**修复范围**：该报告 §1 全部 M 级（M1–M5）。L 级 L1–L13 与多轮遗留项不在本轮（见「遗留」）
**验证结果**：pytest **636 passed / 32 skipped**；`ruff check agentcore plugins tests bot.py scripts` 全绿。M4 以可控时钟序列的回归测试锁定（修复前该测试会失败）

---

## M1 bbd8913 声称失真（gitignore 例外未删）

| 项 | 内容 |
|---|---|
| 修复方式 | `.gitignore` 删除 `!data/kb_samples/hello.md` 例外行与「hello.md 为最小演示样例，保留」过时注释，改为「本地自备，一律不入库」 |
| 验证 | 全文无 hello.md 残留；`git check-ignore` 确认 kb_samples 下全部忽略 |

## M2 批量导入脚本非幂等、无判重

| 项 | 内容 |
|---|---|
| 修复方式 | 导入前经 `list_sources` 取来源名集合（对齐 `/kb samples` 的 `_plan_samples`），同名打印「已入库，跳过」；新增文件统一标 `kind=sample` 与命令一致——失败重跑从「全量重复入库」变为幂等续传 |
| 验证 | 与 admin.py 判重逻辑逐行对照（同名集合、跨 kind） |

## M3 脚本 docstring「超限跳过」与行为不符

| 项 | 内容 |
|---|---|
| 修复方式 | 循环前 `stat` 预检，超限打印「跳过」且**不计入 failed、不影响退出码**——docstring 措辞此时为真 |
| 验证 | 代码路径对照 `agentcore/rag/ingest.py` 的 MAX_INGEST_BYTES 门槛 |

## M4 预算账本跨月午夜竞态（未提交 M7 内，提交前修复）

| 项 | 内容 |
|---|---|
| 修复方式 | `CostBudget.record()` 开头单次 `date.today()`，月份文件与日键同源贯穿 `_day(now)`/`_save(month)`；`today()`/`chat_blocked()`/`estimate_cost()` 同步改造 |
| 验证 | 回归 `test_record_month_boundary_uses_single_now`：可控时钟序列 9-30→10-01，断言账落 `usage-2026-09.json` 且 10 月文件不存在（修复前该写法必产生跨月污染） |

## M5 零磁盘 IO 断言恒真

| 项 | 内容 |
|---|---|
| 修复方式 | 断言从「`tmp_path/data` 不存在」（永真）改为「`glob("usage-*.json")` 为空」——落盘的唯一可观察痕迹 |
| 验证 | 测试即回归；`chat_blocked` 早退路径不触 `_day()` |

---

## 遗留（如实记录）

1. **L1–L13 未修**（本轮仅 M 级）：@路径唤醒词残留、完成通知 DM 可靠性、判重 1000 天花板、分批条数校验、embedding 用量展示、日志脏值回退等——见评审报告 §1。
2. **线上问题另修（非评审条目）**：`trigger_rule` 自行扫描全部 @ 段——适配器 `_check_at_me` 只认首/尾 @，「reply + @别人 + @bot」漏触发（生产消息 518483608 复现，回归 2 例）。
3. **仍未修**：fallback 切 DeepSeek thinking 模式的 `reasoning_content` 400（线上 451→400 链，见 20:20:00 日志）；上轮 M4 掩蔽测试；上上轮 H1/H2。
4. M7 本体（成本预算 + 日志归档）为新功能，随本轮修复一并首次提交。
