# agent-demo 评审修复记录

**对应评审**：[REVIEW-bbd8913..f6dffcc.md](REVIEW-bbd8913..f6dffcc.md)（条目编号沿用该报告 §1）
**修复日期**：2026-09-11
**修复范围**：该报告 §1 的 **H1 + M1–M12 + 可低成本闭环的 L 项**；另含用户要求的"评审产物归档约定"工程变更
**验证结果**：`pytest` **673 通过 / 32 跳过 / 0 失败**（修复前 636/32）；`ruff check agentcore plugins tests bot.py scripts` **全绿**
**工作区状态**：修复期间 bot 进程（PID 1580245）在运行；未触碰其数据目录，`data/` 下产物均被 gitignore

---

## 一、按条修复

### H1 知识库摄取 200 块上限静默砍尾【已修】

| 项 | 内容 |
|---|---|
| 修复方式 | ① `MAX_CHUNKS_PER_SOURCE` 保持 200 默认，但新增 `max_chunks_per_source()` 读 `AGENT_KB_MAX_CHUNKS_PER_SOURCE`；`KnowledgeBase` 支持 `rag.max_chunks_per_source`（config.yaml 已设 **1000**）；② `ingest_text` 不再 `[:N]` 后静默返回——改为先全量切块、计算 `dropped`，打 WARNING 并在返回值/meta 中暴露 `chunks_total`/`dropped`/`sha256`/`truncated`；③ `ingest_file` 透传 `max_chunks`/`scrub` |
| 改动文件 | `agentcore/rag/ingest.py`、`agentcore/rag/service.py`、`config.yaml`、`.env.example`、`scripts/ingest_kb_samples.py`、`plugins/qq_agent_adapter/admin.py`、`README.md` |
| 回归测试 | `tests/test_rag.py::TestChunkLimitH1`（6 例：env 覆盖/脏值回退/丢弃上报/提高上限保住尾部/默认上限仍生效/指纹稳定） |
| 存量数据 | 已执行 `scripts/ingest_kb_samples.py --replace` 重灌 36 个语料文件（见文末「重灌结果」） |

### M1 硬闸门只在 `run()` 入口判一次【已修】
- `engine.py` 的 tool-loop 每一步（`step > 0`）复查 `chat_blocked()`，命中即返回提示并 `logger.warning`；入口注释与 `budget.py` docstring 同步改为准确表述。
- 回归：`tests/test_engine.py::TestAgentEngine::test_hard_gate_blocks_at_entry`、`…::test_hard_gate_rechecked_inside_tool_loop`（后者断言 LLM 只被调用 1 次）。

### M2 `_env_int` 脏值静默 fail-open【已修】
- `_env_int` 改为：空值→默认；`ValueError`→**WARNING + 回退**；负数→**WARNING + 回退**。
- 回归：`tests/test_budget.py::TestEnvRobustness`（4 例，含「脏值确实关闸但必须留告警」）。

### M3 账本无进程互斥 / 整月覆盖写【部分修复，如实记录】
- 已修：`.part` 临时名加 `pid` 后缀（`usage-YYYY-MM.json.<pid>.part`），消除多进程互踩同一临时文件。
- **未修**：仍无文件锁/合并读改写；多进程部署下「读改写整月覆盖」仍可能丢账。当前 `bot.py` 是单进程，README 已说明账本目录与单进程假设。
- 回归：既有 `TestCostBudgetLogic` 全过（行为未变）。

### M4 账本结构异常导致闸门抛错（fail-open）【已修】
- `_ensure_loaded` 改为显式结构校验：顶层非 dict / `days` 非 dict / 日条目非 dict 一律告警并按空账本处理；`_day` 用 `_blank_day()` 补齐缺失或类型错误的键。
- 回归：`tests/test_budget.py::TestCorruptLedger`（6 种损坏结构参数化 + 非字典日条目隔离，均不得抛 `AttributeError`/`KeyError`/`TypeError`）。

### M5 判重无内容指纹【已修（脚本侧），命令侧为提示】
- `ingest_text` 现返回并落库 `meta.sha256`（`content_digest()` = strip 后原文 sha256）。
- `scripts/ingest_kb_samples.py`：按**指纹**判重——相同跳过；不同（含历史存量无指纹）默认只提示，`--replace` 才删旧重灌，并新增 `--dry-run`。
- `admin.py` 的 `/kb samples`：`_plan_samples` 同步改为指纹判重，返回 `dup`（未变）/`changed`（已变或无指纹）；**命令侧只提示不删数据**，并在回复里指向 `--replace`。
- 回归：`tests/test_rag.py::TestKbSamplesIngest::test_plan_dedup_changed_and_oversize`、`…::test_changed_content_is_not_silently_replaced`（断言来源 id 不变）。

### M6 改名留僵尸来源【已修（脚本侧）】
- 新增 `--prune`：只删 `kind='sample'` **且** `location` 位于 `data/kb_samples/` 前缀内、文件已不存在的来源（`_is_sample_location`），避免误删 `manual`/`distill`。

### M7 预算 env 未做测试隔离【已修】
- `tests/conftest.py` 新增 autouse fixture `_isolate_budget_env`：清空 5 个 `AGENT_BUDGET_*`/`AGENT_PRICE_*` env，并把 `budget._default` 指向 `tmp_path`。
- 验证：脏环境（`AGENT_BUDGET_DIR=<超限账本> AGENT_BUDGET_DAILY_TOKENS=1 AGENT_BUDGET_ENFORCE=1`）下 `test_engine.py` 由修复前 **24 failed / 13 passed** 变为 **39 passed**；脏环境全量 **656 passed**。

### M8 主代理误删运行数据（操作事故）【已记录，非代码修复】
- 账本已由运行中的 bot 自动重建并持续累计；`data/logs/` 需重启 bot 才能恢复落盘（handler 仍持已删 inode 的 fd）。
- 教训已写入 `review/REVIEW-WORKFLOW.md`（见"归档约定"一节）：清理 `data/` 下路径前先确认无运行进程。

### M9 @bot 路径唤醒词残留【已修】
- `wakewords.strip_wake_word` 先 `text.lstrip()` 再匹配，未命中时仍返回**原文**（不引入额外裁剪）。
- 回归：`tests/test_matcher.py::TestWakeWordEdgeCases::test_strip_wake_word_handles_leading_space`。

### M10 `or event.is_tome()` 零覆盖【已修】
- 新增 `test_to_me_without_at_segment_triggers`（`to_me=True` 且无 at 段，仅靠 `is_tome()`）；新增 `test_no_at_no_to_me_still_rejected` 作反向对照；**移除** `test_matcher.py` 里那行冗余的 `monkeypatch.setattr(event, "is_tome", …)` 打桩，使 `test_group_matches_at_me` 真正验证 at 段扫描。

### M11 唤醒词受前导 `reply`/`@别人` 段影响而漏答【已修】
- `matcher._plain_text(event)`：只拼接 `text` 段（忽略 reply/at/image），`trigger_rule` 用它做唤醒词/旧前缀匹配；段结构异常时回退旧行为。
- 回归：`TestWakeWordEdgeCases` 的 `test_wake_word_after_reply_segment_triggers`、`test_wake_word_after_at_other_segment_triggers`、`test_prefix_after_reply_segment_triggers`。

### M12 日志初始化可致 bot 完全起不来【已修】
- 抽出 `agentcore/logging_setup.py::setup_file_logging()`：脏值→告警禁用落盘；`mkdir`/handler 失败→告警并降级为仅控制台；`bot.py` 只调用一次，并移除不再使用的 `pathlib`/`logging.handlers` 导入。
- 回归：`tests/test_logging_setup.py`（6 例：脏值/负数/0/成功写入/不可写目录降级/默认 backupCount=14）。

### L 级（本轮一并修）
| 条目 | 修复 |
|---|---|
| L4 两个 DIR 未文档化 | `.env.example` 补 `AGENT_BUDGET_DIR`/`AGENT_LOG_DIR` 与"相对路径按 CWD、部署用绝对路径"提示 |
| L5 embedding 用量不展示 | `/status` 追加 embedding 用量行（次数 + token，标注不计入对话预算） |
| L7 `AGENT_KB_ENABLED=0` 不拦写入 | `KnowledgeBase.add_text/add_file` 在 disabled 时 `RuntimeError`；`/kb add|file|samples` 给出明确提示。回归：`TestKbDisabledL7` |
| L11 价格 nan/inf/负数 | `_env_float` 校验 `math.isfinite` 与非负，否则告警忽略。回归：`TestDisplayAndPriceHardening::test_nan_inf_negative_price_ignored` |
| L12 阻断文案泄漏 token 数 | `chat_blocked()` 对用户只回「今日 LLM 预算已用完，服务明日自动恢复。」，数字只进日志与 `/status`。回归：`…::test_block_reason_has_no_internal_numbers` |
| L13 预算信息读取失败静默 | `_build_status_lines` 的 `except Exception: pass` 改为 `logger.warning(..., exc_info=True)` |
| L15 `.env.example` 段落错位 | `AGENT_MIGRATE_VECTOR` 移回 embedding 段，日志小节补分隔与说明 |
| L16 BACKLOG 与实现矛盾 | 基线/测试数（700 收集）/A1 状态/M7 状态更新，标注 A1 已完成与残余项 |
| L17 README M7 口径矛盾 | M7 行改为「调度面 ✅（4 job）/ 成本预算 ✅ / 日志归档 ✅ / **定时内容推送** ⏳」 |
| L18 README 目录树过时 | 补 `data/budget/`、`data/logs/`，修正 `data/kb_samples/` 描述 |

---

## 二、评审产物归档（用户要求的工程变更）

| 项 | 内容 |
|---|---|
| 移动 | 4 份历史 `FIX-*.md` 从仓库根目录 `git mv` 到 `review/`（`FIX-6c57fd9..e86fba0`、`FIX-e86fba0..8cfbf6d`、`FIX-436629d..fad144b`、`FIX-fad144b..bbd8913`） |
| 链接 | 移动后的同目录相对链接修正（`review/REVIEW-x.md` → `REVIEW-x.md`）；更新 `README.md` 目录树、`BACKLOG.md`、`review/REVIEW-8cfbf6d..a604023.md`、本报告中的引用 |
| 规范 | `review/REVIEW-WORKFLOW.md` 顶部新增 **⚠️ 产物布局（硬性约定，重点标注）** 表；§2.5 的"仓库根目录"改为"`review/` 目录"；§6 明确 `review/FIX-*.md`；§7 增硬性约束（附自检命令 `git ls-files \| grep -E '^FIX-\|^REVIEW-'` 应为空）；新增 §9.1 修复记录索引 |

---

## 三、遗留（如实记录，未修）

1. **L2 判重窗口天花板**：脚本与 `/kb samples` 仍用 `list_sources(limit=1000)`；来源超 1000 后窗口外同名判重失效（需按 name 精确查询或唯一约束）。
2. **L3 日志留存语义**：`TimedRotatingFileHandler(backupCount=N)` 实际保留 ≈N+1 天；仅在 `README`/`.env.example` 如实说明，未改语义。
3. **L8 脚本退出码**：新增 `2 = 有待替换项`，但"目录不存在/空目录"仍返回 0，CI 仍无法区分。
4. **L9 空正文 `✓ 0 块`**：未改（仍会每轮重复处理并打印成功）。
5. **L10 脚本与命令的判重仍是两份实现**：`scripts/ingest_kb_samples.py` 与 `admin._plan_samples` 都实现了指纹判重，未抽公共函数，存在再次漂移的风险；脚本本身仍无独立测试。
6. **L13 脚本缺 `sys.path.insert`**：非 editable 安装下从项目根直接跑仍会 `ModuleNotFoundError`。
7. **L14 扩展名/Unicode 归一**：`glob("*.md")` 不匹配 `.MD`；NFC/NFD 同名判重仍失败。
8. **M3 多进程账本**：无文件锁，多进程部署下仍可能丢账（仅修了 `.part` 命名）。
9. **多轮遗留**：`split_message` 边界 `.strip()`（更早一轮 H）、`_reconstruct_content_from_memory` 死代码、`AGENT_PREFIX` 掩蔽测试（`PREFIX` 在 import 期冻结，测试的 `setenv` 仍无效）、`reasoning_content` 400。
10. **未验证面**：本地 embedding（Ollama/TEI）链路的 usage 字段、真机多账号通知送达率、多进程部署——本轮均未验证。

---

## 四、执行记录（存量重灌与清理）

### 4.1 存量语料重灌（H1 的存量修复）

命令：`.venv/bin/python scripts/ingest_kb_samples.py --replace`（先生成 dry-run 确认范围）

| 指标 | 重灌前 | 重灌后 |
|---|---|---|
| `kind='sample'` 来源数 | 37（含 1 条僵尸 `hello.md`） | 36 |
| `kb_chunks` 总数 | 6,847 | **8,579（+1,732，约 +25%）** |
| 单来源最大块数 | **200**（9 条撞上限，尾部内容被丢弃） | **294**（无截断） |
| 丢弃块数 | 静默不可见 | **0**（脚本汇总明确报告） |
| 带内容指纹（`meta.sha256`）的来源 | 0 | 36 / 36 |

- 36 个文件全部重新入库成功，`失败 0`、`超限 0`、`丢弃块数合计 0` —— 在 `max_chunks_per_source=1000` 下不再有尾部丢失。
- 输出里「入库 N 块 < 切出 M 块」（如 013：202 < 252）是 `kb_add_chunks` 的**内容级去重**（重复段落），不是截断；脚本把 `dropped`（截断）与 `written`（去重后写入）分开报告，避免再次混淆。
- 重灌后立即复跑一次：36 个全部命中「内容未变，跳过」→ 验证 M5 的内容指纹判重生效（此前只按文件名，永远无法发现这种变化）。

### 4.2 僵尸来源清理（M6）

命令：`.venv/bin/python scripts/ingest_kb_samples.py --prune`（先 dry-run）

- 删除 `hello.md`（id=3）：`location` 在 `data/kb_samples/` 前缀内、文件已删除、无指纹、1 块。该来源自早前删除样例文件后一直残留。
- `--prune` 只处理 `kind='sample'` **且** 路径前缀匹配、文件已不存在的条目，`manual`/`distill` 不受影响；复跑结果为「已删除 0 条」（幂等）。

### 4.3 未受影响项

- 运行中的 bot 未重启：账本继续累计（`data/budget/`），`/kb` 命令与检索即时可用（知识库读的是 PG）。
- `data/logs/` 仍待重启恢复（见 M8）。

---

## 五、验收

按规范 §6，修复完成后以「复现脚本由可复现变不可复现」验收：

| 评审项 | 修复前复现结果 | 修复后 |
|---|---|---|
| H1 截断 | 34/36 文件超 200 块、生产库 9 条撞顶 | 单来源最大 294 块、丢弃 0（§4.1） |
| M1 中途超预算 | 循环内不复查（可再打 7 次） | 第 2 步即中止，LLM 调用数 = 1（测试断言） |
| M2 脏值 | `1,000,000` → 0 且无告警 | 0 + WARNING（测试断言） |
| M4 损坏账本 | 5 种结构全抛异常 | 全部不抛，按空账本处理（参数化测试） |
| M7 脏环境 | `test_engine.py` 24 failed | 39 passed（脏 env 实跑） |
| M9/M11 | `" 小助手 …"` 不剥；reply 后唤醒词不触发 | 均正确（测试断言） |
| M12 脏值日志 | `python bot.py` 直接 traceback | 告警并降级（测试断言） |

- 全量：`pytest` **673 passed / 32 skipped / 0 failed**；`ruff check agentcore plugins tests bot.py scripts` **All checks passed!**
- 依赖安全：本轮修复未新增/升级任何依赖（`pyproject.toml` 未改）。
