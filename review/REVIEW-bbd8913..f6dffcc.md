# agent-demo 近期 Commit 评审报告
**评审范围**：`bbd8913..f6dffcc`（5 个 commit：`43607b9` 上轮报告归档、`be13897` M7 成本预算+日志归档、`00eaa52` @bot 任意位置触发、`bef83a8` 脚本判重与超限跳过、`f6dffcc` FIX 记录）
**评审日期**：2026-09-11
**评审方式**：4 个并行只读子代理分线审查（线1 预算核心与接线 / 线2 消息适配层 / 线3 脚本与数据治理 / 线4 文档声称与门禁）+ 主代理逐条实证（实跑 pytest/ruff、PG 只读查询、纯内存与 tmp 仿真、证伪、声称核对、未验证面销项）
**工作区状态**：评审开始时 `git status` **干净**；评审产物仅为 `review/` 下本报告与索引更新（符合 §2.5）。为保证复现，主代理曾临时改动 `tests/test_matcher.py` 一行（复核线2-M10 的打桩冗余）并**立即还原**，`git diff` 确认与 HEAD 一致。
**⚠️ 主代理操作记录（必须声明）**：实证期间主代理**误删**了运行中 bot 产生的 `data/budget/usage-2026-09.json`（当日用量账本）与 `data/logs/agent.log`（运行日志），两者均在 `.gitignore` 内、非版本控制文件，但属运行数据。详见 §1-M8。

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | **H1 知识库摄取 200 块上限静默砍尾，且已在实际生产库发生**：`ingest.py:13,42` 硬编码 `MAX_CHUNKS_PER_SOURCE=200`，超出部分直接 `[:200]` 丢弃、运行时无告警。主代理复算当前语料：**36 文件中 34 个超限，全语料仅入库 80.3% 字符**（最差文件 64.9%）；PG 只读查询证实 **37 条 `kind=sample` 来源中 9 条 `chunks` 恰好=200（截断已发生）** |
| 次高 | **M1 硬闸门只在 `engine.run()` 入口判一次**：`for step in range(max_iterations=8)` 循环内不复查，一次工具循环最多再发 8 次 LLM 调用，与代码注释/README「当日不再发起任何 LLM 调用」不符 |
| 其他实质缺陷 | **M2** `_env_int` 脏值静默 fail-open（`1,000,000`→0、`1e6`→0、`-5`→-5），预算被无声关闭且 `/status` 不显示；**M3** 账本无进程级互斥 + 整月文件覆盖写 → 静默丢账（仿真 30+20 → 只剩 20）；**M4** 账本结构异常时 `chat_blocked()` 抛错、`engine.run` 顶部未捕获 → 对话静默降级为 `[echo]` 且闸门失效；**M5** 脚本判重键为纯文件名、无内容指纹，同名语料更新后**永久不生效**且无 `--force`；**M6** 改名留僵尸来源；**M7** 测试未隔离 `AGENT_BUDGET_*`，脏环境下 `test_engine.py` **24 failed / 13 passed**（干净环境 37 passed）；**M9** @bot 路径唤醒词残留（本轮改动使其首次可达）；**M10** `or event.is_tome()` 生产主路径零测试覆盖（删掉该分支测试仍全绿）；**M11** 打唤醒词 + 前导 `reply`/`@别人` 段仍漏触发（同动机只修了 @bot 一侧）；**M12** `bot.py` 日志初始化在模块级无保护，`AGENT_LOG_KEEP_DAYS` 脏值或 `mkdir` 失败 → **bot 完全起不来** |
| 上一轮 M1–M5 | **全部真修**（逐条主代理实证，含「回归测试在旧实现下必红」的反向验证） |
| 测试与文档 | **636 passed / 32 skipped**；ruff 全绿；**声称核对：本轮 5 个 commit 声称全部属实**（与上两轮形成对比）；**依赖安全：范围内 `pyproject.toml` 零变化** |
| 质量门禁（§8.3） | **H1 存在 → 按规范阻断该路径发布**（归因见 §1-H1：根因为既有代码，本轮判重未引入亦未修复）。上轮 4 项未验证面：DashScope 错误体**已销项**；embedding 成功响应 usage 字段、真机多账号通知、systemd CWD **仍未验证** |

---

## 1. 已实证的问题

### H1 知识库摄取 200 块上限静默砍尾，生产库已被截断【已复现，按 §8.3 阻断】
- **位置**：`agentcore/rag/ingest.py:13,40-46`；脚本打印处 `scripts/ingest_kb_samples.py:74-75`；`/kb samples` 同路径 `admin.py:385`
  ```python
  MAX_CHUNKS_PER_SOURCE = 200         # ingest.py:13（硬编码，不可配置）
  chunks = chunk_text(body, max_chars=max_chars)[:MAX_CHUNKS_PER_SOURCE]   # [:200] 静默丢弃
  ```
- **证据 A（主代理复算当前语料）**：真实 `chunk_text` + `scrub_pii` 跑 `data/kb_samples/*.md`（`chunk_chars=600`）——
  **超 200 块的文件 = 34/36**；全语料字符 **4,140,405 → 3,323,302，保留率 80.3%**；最差 `030_剧情合集_此际我独行…md`（296 块）仅保留 **64.9%**。
- **证据 B（PG 生产库只读查询）**：`kb_sources` 中 37 条 `kind=sample` 来源，**9 条 `chunks` 恰好 = 200**（撞上限），`kb_chunks` 合计 6847。
  → **截断不是理论风险，已经发生在用户当前的知识库里。**
- **影响**：每个超限文档的**尾部内容整段缺失**（`[:200]` 取前 200 块）；脚本只打印存活块数（`✓ N 块`），用户无法从输出察觉；`/kb samples` 同样只报存活数。
- **归因（重要，防止误判为本轮引入）**：`MAX_CHUNKS_PER_SOURCE` 是**既有**防灌库保护（非本轮引入）；本轮 `bef83a8` 的判重**既未引入也未修复**该截断——判重前重跑只会新增一条同名残缺来源（重复而非修复），判重后重跑跳过。脚本 docstring 第 7-8 行已披露「单来源最多 200 块（超出部分丢弃）」，属**已披露但运行时无告警**的静默截断。
- **修复建议**：
  1. `ingest_text` 返回值补 `chunks_total` / `dropped`，脚本与 `/kb samples` 打印「⚠ 超限丢弃 N 块」；
  2. `MAX_CHUNKS_PER_SOURCE` 提为可配置（env `AGENT_KB_MAX_CHUNKS_PER_SOURCE`），或超限时按来源分片多行写入而不是丢弃；
  3. 已入库的 37 条 sample 需**重新摄取**（删旧来源 + 提高上限后重导）才能补回丢失内容。

### M1 硬闸门只在 `run()` 入口校验，tool-loop 内每步仍可继续调用 LLM【已复现】
- **位置**：`agentcore/loop/engine.py:254`（唯一闸门）与 `:301`（循环）
  ```python
  blocked, reason = get_budget().chat_blocked()   # :254 注释写「不再发起任何 LLM 调用」
  if blocked: return reason
  ...
  for step in range(self.max_iterations):         # :301  max_iterations 默认 8
      response = await self.llm.chat(messages, tools=...)   # 循环内无任何复查
  ```
  主代理 grep 确认：`run()` 循环体内**零次** `chat_blocked()` 调用。
- **影响**：额度语义是「每次 run 开始时校验」，而非「当日不再发起调用」。一次工具循环最多 8 次 LLM 调用全部发生在闸门放行之后，超支幅度与单轮步数成正比（有界于 `max_iterations`，非无限）。
- **声称不符**：`budget.py:9-10` 与 `README.md`（成本预算节）均称「当日不再发起任何 LLM 调用」。
- **修复建议**：循环体内 `if step > 0 and get_budget().chat_blocked()[0]: break`（返回现有提示）；或把文档改为「超限后新的对话轮次被拦截，进行中的轮次允许跑完」。

### M2 `_env_int` 脏值静默 fail-open：预算被无声关闭【已复现】
- **位置**：`agentcore/budget.py:26-30`（`_env_int` 无 warning）对照 `:33-41`（`_env_float` 有 warning）
- **证据**（主代理逐值实测）：
  | 配置值 | 解析结果 | 告警 |
  |---|---|---|
  | `1000000` | 1000000 | — |
  | `1,000,000` | **0** | **无** |
  | `1e6` | **0** | **无** |
  | `十万` | **0** | **无** |
  | `abc` | 0 | 无 |
  | `-5` | **-5** | 无 |
- **影响**：`daily_tokens=0` 或负数 → `chat_blocked()` 恒为 False → **硬闸门等于没开**；且 `/status` 因 `daily_tokens > 0` 不成立而**不显示预算行**，运维无法察觉配置未生效。安全机制 fail-open 且静默。
- **修复建议**：`_env_int` 解析失败/非法值 → `logger.warning` + 回退；对 `daily_tokens <= 0` 且显式配置过的情形也提示；`enforce` 非空且非真值时同样告警。

### M3 账本无进程级互斥 + 整月覆盖写 → 静默丢账【已复现】
- **位置**：`agentcore/budget.py:85-93`（`_ensure_loaded` 命中月份即 return，此后不重读磁盘）、`:95-100`（`_save` 整月覆盖写 + 固定 `.part` 名）
- **证据**（主代理 tmp 仿真，同进程双实例等价于多进程）：A、B 各自缓存后 `A.record(30)`、`B.record(20)` → 磁盘最终只剩 `prompt=20`，**A 的 30 静默丢失**；变体：外部写入 999 后本实例 `record(1)` → 磁盘被重写为 `prompt=1`。
- **影响**：(a) 用量/成本统计系统性偏低，而成本控制以它为唯一依据；(b) 多进程部署下每个 worker 各持 `_default`，实际日上限 ≈ 进程数 × 配置值；(c) `.part` 固定名多进程互踩（上轮 L10 未修）。
- **现状**：`bot.py` 当前是单进程 `nonebot.run()`，**单进程下不触发**；属多进程/多实例的潜在风险。
- **修复建议**：README 明确单进程假设；`record` 前重读合并或改 append-only；`.part` 加 pid/uuid 后缀。

### M4 账本结构异常时 `chat_blocked()` 抛错 → 对话静默降级且闸门失效【已复现】
- **位置**：`agentcore/budget.py:85-93`（`json.loads(...).get("days", {})` 不做结构校验）、`:104`（`self._days.setdefault`）
- **证据**（主代理 5 组损坏结构实测，全部抛错）：
  | 损坏形态 | 结果 |
  |---|---|
  | `{"days": null}` / `[]` / `"x"` | `AttributeError: 'NoneType'/'list'/'str' object has no attribute 'setdefault'` |
  | 日条目为 `int` | `TypeError: 'int' object is not subscriptable` |
  | 日条目缺 `completion` | `KeyError: 'completion'` |
- **影响链**：`chat_blocked()` 抛错 → `engine.run()` 顶部**未包 try** → `matcher._run_and_format` 的 except 捕获后 `reply=None` → 用户收到 **`[echo] 原文`**（静默降级，用户以为 bot 只会复读）；同时**闸门实际不生效**（fail-open）。`record()` 抛错则被 `record_chat_usage` 吞掉 → 之后用量静默不记。
- **触发条件**：手改 / 旧版本格式 / 半截 `usage-*.json`（`.part`+`os.replace` 保证不会半写，但外部编辑可造成），低概率高影响。
- **修复建议**：`days = data.get("days") or {}` 且 `isinstance(days, dict)` 校验，否则重置并 warn；日条目用 `_blank_day()` 补齐缺失键；`admin.py:128-129` 的 `except Exception: pass` 收窄为打日志。

### M5 判重键为纯文件名、无内容指纹：同名语料更新后永久不生效【已复现】
- **位置**：`scripts/ingest_kb_samples.py:63-69`；`/kb samples` 同病 `admin.py:342`
  ```python
  known = {s.get("name") for s in await kb.list_sources(limit=1000)}
  if path.name in known:
      print(f"⏭ {path.name}: 同名来源已入库，跳过"); continue
  ```
- **证据**（主代理实测）：`x.md` 首版入库 → 覆盖写为全新内容 → 重跑打印「同名已入库，跳过」，**新内容永不入库**，且无 `--force`，只能手工 `/kb delete`。
- **影响**：语料迭代（修订/补充）静默失效，检索到的仍是旧正文。与 H1 叠加时更严重：用户以为"已导入完成"，实际是**旧且残缺**的版本。
- **修复建议**：`meta` 存 `sha256 + size + chunks_total`，指纹不同则提示或 replace；补 `--force`；`/kb samples` 同步修（否则又是一次"声称对齐"）。

### M6 改名留僵尸来源，无清理路径【已复现】
- **位置**：同上判重逻辑
- **证据**（主代理实测）：`x.md` 入库后改名 `x2.md` 再导 → 来源 `[('x2.md',…), ('x.md',…)]` 共存，旧 `location` 已不存在。
- **影响**：重复内容污染 `top_k` 检索与 `/kb stats`；无 CLI 清理路径。
- **修复建议**：加 `--prune`，仅删 `kind='sample'` 且 `location` 前缀在 `data/kb_samples/` 内、文件已不存在的来源（避免误删 `manual`/`distill`）。

### M7 预算 env 未做测试隔离：脏环境下 engine 测试大面积挂【已复现】
- **位置**：`agentcore/budget.py:177`（`_default = CostBudget()` 导入期读 env）；`tests/conftest.py` 只隔离 `PERSONAS_DIR`，无 `AGENT_BUDGET_*`
- **证据**（主代理实测）：`AGENT_BUDGET_DIR=<含超限账本的 tmp> AGENT_BUDGET_DAILY_TOKENS=1 AGENT_BUDGET_ENFORCE=1 pytest tests/test_engine.py` → **24 failed / 13 passed**；同一命令不设脏 env → **37 passed**。
- **影响**：开发者本地若开启 `AGENT_BUDGET_ENFORCE=1` 且 `data/budget/`（就在仓库 CWD 下、已 gitignore）超限，CI 之外的一切引擎测试整片红。上轮 L12 只点名 `test_budget.py`，**实际面更大**。
- **修复建议**：conftest 加 autouse fixture：`monkeypatch.delenv` 五个 `AGENT_BUDGET_*`/`AGENT_PRICE_*` + `monkeypatch.setattr(budget_mod, "_default", CostBudget(root=tmp_path))`。

### M8 主代理误删运行数据（评审操作事故，不是代码缺陷）
- **事实**：主代理在排查"测试是否污染仓库"时，执行 `rm -rf data/budget data/logs`，删除了**正在运行的 bot 进程**（PID 1580245，21:29:42 启动，父进程 bash）产生的当日用量账本与运行日志。随后清理重跑测试证明**测试并不会写这两个目录**——原先的文件来自运行中的 bot，误判已纠正。
- **影响与现状（21:39 复核）**：
  - **账本已自愈**：`record()` 会 `mkdir(parents=True, exist_ok=True)` 并重写文件，当前 `data/budget/usage-2026-09.json` = `{prompt 69201, completion 5323, embedding_tokens 1378, chat 15 / embedding 18}`（数值在持续增长）→ 丢失的仅是删除前的累计值，写入链路未受影响；因默认 `AGENT_BUDGET_ENFORCE=0`，闸门判定不受影响。
  - **日志未自愈**：`TimedRotatingFileHandler` 持有已删除 inode 的 fd，`data/logs/` 目录不存在但写入仍"隐形"进行；`/proc/1580245/fd` 因权限（EACCES）无法读取，未能取得 deleted-inode 的直接证据，但删除前 `data/logs/agent.log`（16261 字节）确实存在（主代理删除前 `ls` 记录）。
- **处置建议**：重启 bot 即可恢复日志落盘（账本已自行恢复）。删除前的当日用量与日志已不可恢复。
- **对子代理结论的影响（重要）**：线4 因观测到"运行实例没有 `data/logs/`"而提出存疑「进程环境里 `AGENT_LOG_KEEP_DAYS=0`」。该推断**被主代理证伪**——文件是被主代理删除的，与配置无关（线4 不可能知道该操作）。唯一的残余不确定性是"agent.log 由当前进程还是更早进程创建"（mtime 仅到分钟），但这不影响"删除导致缺失"的结论。
- **流程教训（建议纳入规范 §7）**：`data/budget/`、`data/logs/` 这类"运行数据"与"测试产物"外观相同且被 gitignore（`git status` 干净），极易在评审中被误清理。建议补一条：**清理任何 `data/` 下路径前必须先确认无运行进程**（`ps aux | grep bot.py`），或改为只读观察、不删。

### M9 @bot 路径唤醒词残留，且本轮改动新增一条可达路径【已复现】
- **位置**：`plugins/qq_agent_adapter/wakewords.py:24-27`、`plugins/qq_agent_adapter/pipeline.py:144-151`
  ```python
  def strip_wake_word(text: str) -> str:
      hit = match_wake_word(text)        # startswith，不先 strip
      return text[len(hit):].lstrip() if hit else text
  # pipeline._strip_trigger_prefix: 群聊先剥唤醒词，最后才 re.sub(PREFIX,"",...).strip()
  ```
- **证据**（线2 经真实预处理 `message.reduce()` + `_check_at_me` 仿真；主代理复核 `strip_wake_word(" 小助手 …")` 原样返回）：
  | 消息形状 | 触发 | 进入 prompt 的文本 |
  |---|---|---|
  | `[reply][at(别人)][at(bot)][text(" 小助手 帮我查天气")]` | True | **`小助手 帮我查天气`（残留）** |
  | `[at(bot)][image][text(" 小助手 …")]` | True | **残留**（适配器只在 at 紧跟 text 时才 lstrip） |
  | `[at(bot)][text(" 小助手 …")]` | True | `帮我查天气`（适配器已 lstrip，正常） |
- **本轮相关性**：上轮 L1 已发现该残留但当时**不可达**（`str(msg)` 以 `[CQ:reply…` 开头，唤醒词与旧前缀都不命中、`to_me=False`）；本轮 `00eaa52` 的 @bot 全段扫描把这条路径**变成可达**，于是残留被暴露到生产路径上。
- **影响**：与 `README.md`「命中后唤醒词本身会被剥掉、不进入对话内容」不符；多耗 token、污染对话内容。
- **修复建议**：`strip_wake_word` 内先 `text = text.lstrip()`（一行）；并覆盖「at 与 text 之间有其它段」的用例。

### M10 `or event.is_tome()` 生产主路径零测试覆盖，且现有用例的打桩已冗余【已复现】
- **位置**：`plugins/qq_agent_adapter/matcher.py:47`、`tests/test_matcher.py:257`
- **证据**（主代理实测）：
  1. 适配器 `Bot.handle_event` 在规则前调用 `_check_at_me`，会把**首/尾的 @bot 段从 `event.message` 删除** → 生产高频路径「@bot 在开头/结尾」下新扫描**必然 False**，触发完全依赖 `or event.is_tome()`；
  2. 删掉 `test_matcher.py:257` 的 `monkeypatch.setattr(event, "is_tome", lambda: True)` 后，该用例**仍然通过**（新扫描在 at 段上已命中）→ 打桩不产生区分度；
  3. `grep -rn '"to_me": True' tests/` **为空** → 「`to_me` 为真 ⇒ 触发」这条主路径**没有任何测试**。
- **影响**：最危险的假阴性——将来若有人把 `or event.is_tome()` 当成与新扫描重复的死代码删除，**测试全绿**，而线上首/尾 @bot 会静默失效。
- **修复建议**：新增用例「`to_me=True` 且 `event.message` 中已无 at 段 + 无唤醒词 → 触发」（可直接调 `_check_at_me(None, event)` 制造真实状态），并移除 257 行打桩或改为断言反向区分。

### M11 唤醒词触发仍受前导 `reply`/`@别人` 段影响而漏答，本轮只修了 @bot 一侧【已复现】
- **位置**：`plugins/qq_agent_adapter/matcher.py:34,47`
- **证据**（主代理真值表实测）：
  | 消息 | trigger |
  |---|---|
  | `[text("小助手 帮我查")]` | **True** |
  | `[reply][text("小助手 帮我查")]` | **False** |
  | `[at(别人)][text("小助手 帮我查")]` | **False** |
  | `[reply][at(别人)][at(bot)][text(" 你好")]` | True |
- **根因**：触发判定用 `str(event.get_message())`（含 `[CQ:reply…][CQ:at,…]` 前缀）做 `startswith`，任何前导非 text 段都会让唤醒词失配。
- **影响**：本轮 commit 的动机正是「用户在 reply/@别人 之后呼叫 bot」，却只补了 @bot 一侧 → 线上表现为「@bot 能唤醒、打唤醒词不能」的不一致；README 声称「消息以任一唤醒词开头即触发」不成立。
- **修复建议**：触发判定改用与 `pipeline._build_user_text` 一致的口径（只取 text 段 join 并 `strip()` 后再匹配）；**必须与 M9 的 `lstrip` 一起修**，否则统一口径后仍会因前导空格失配。

### M12 日志归档配置脏值/目录不可写会让 bot **完全起不来**（模块级未捕获）【已复现】
- **位置**：`bot.py:20-30`（模块顶层）
  ```python
  _log_keep = int(os.getenv("AGENT_LOG_KEEP_DAYS", "14") or "0")   # :20 模块级
  if _log_keep > 0:
      _log_dir = Path(os.getenv("AGENT_LOG_DIR", "data/logs"))
      _log_dir.mkdir(parents=True, exist_ok=True)                   # :23 无 try/except
  ```
- **证据**：`AGENT_LOG_KEEP_DAYS=abc`（或 `1.5`）→ `ValueError` 在 import 期抛出、无 warning 无回退 → `python bot.py` 直接 traceback，**NoneBot 完全不启动**（主代理实测）。`.env.example` 明文给出 `AGENT_LOG_KEEP_DAYS=14`，用户改错一个字符即可触发。`mkdir` 同理：systemd 默认 `WorkingDirectory=/` 或只读 CWD → `PermissionError` 同样崩启动。
- **定级理由**：上轮记 L8；本轮升为 **M**——正常运行路径可触发、失败模式是**整体不可启动**（可观测性功能反而让机器人起不来），且同一 commit 的 `budget.py:26-41` 已有正确的"脏值 warning + 回退"写法却未被 `bot.py` 复用，自相矛盾。
- **修复建议**：`try/except ValueError` → WARNING + 回退 14/0；`mkdir` 失败降级为"仅控制台 + warning"；或直接复用 `budget._env_int` 风格的解析。

### L 级（压缩列出）
- **L1**（上轮 L1 的独立成因）`match_wake_word` 用 `startswith` 且不先 `lstrip`：已升级为 M9/M11，此处仅保留成因索引。
- **L2**（上轮 L3 未修）判重 `list_sources(limit=1000)` 天花板：主代理仿真验证来源 >1000 后旧同名落出窗口 → 判重失效、重复入库；PG 侧 `LIMIT $1` 静默截断（`store.py:1071`）。
- **L3** 日志留存口径差 1：README 称「保留 `AGENT_LOG_KEEP_DAYS` 天，默认 14」，而 `TimedRotatingFileHandler(backupCount=N)` 实际保留 **N 个备份 + 当前文件** ⇒ 默认最多约 15 天（线4 用 `when='S'` 等价仿真验证）。
- **L4**（上轮 L12/L8 未修）`AGENT_BUDGET_DIR` 仍未进 `.env.example` / README；本轮新增的 `AGENT_LOG_DIR` 也未进 `.env.example`（仅 README 提）。两者都是相对 CWD 的路径且无绝对路径建议。**附带风险**：若把 `AGENT_LOG_DIR` 指到仓库内其它未忽略路径，`data/logs/` 的 gitignore 保护即失效（聊天明文可能被 git 跟踪）。
- **L5**（上轮 L13 未修）`embedding_tokens`/`embedding_requests` 有记账但 `/status` 与成本估算都不展示 → RAG 的 embedding 花费全盲。
- **L6** 字节预检 vs 字符截断（线3 报 M）：**主代理证伪**——预检 `st_size > 2MB`，正常 UTF-8 文本 `len(body.encode()) ≤ st_size`，故预检通过者永不触发 `body[:524288]`；仅 `errors="replace"` 膨胀的乱码/二进制文件可能，降为 L/存疑。
- **L7** `AGENT_KB_ENABLED=0` 不拦 `add_file`（主代理实测：`kb.enabled=False` 时 `add_file` 仍写入来源），与 `README.md:243`「`AGENT_KB_ENABLED=0` 可整体关闭」不符（`enabled` 只门控 `retrieve`/`digest`）。
- **L8** 脚本退出码无区分度：目录不存在 = 0、无 `.md` = 0、全 dup = 0、全超限 = 0、全失败/部分失败 = 1（主代理实测目录缺失 rc=0）→ CI 无法发现"挂载点写错、什么都没导"。
- **L9** 空/纯空白正文 → 不建来源行 → 每轮重跑重复处理并打印 `✓ 0 块`。
- **L10** `scripts/ingest_kb_samples.py` 零测试（`tests/` 无用例）；本轮三个脚本缺陷正是"两条路径靠人眼对齐"的产物。
- **L11** 价格项 `nan`/`inf` 通过解析 → `/status` 显示 `≈ nan 元`；负预算被接受。
- **L12** 闸门阻断文案把内部 token 数字（`{used}/{daily_tokens}`）发给**普通用户**（`budget.py:159-161`）；对比 `/status` 仅管理员可见。
- **L13** 脚本缺 `sys.path.insert`（同目录 `backup_db.py:22` 有）→ 非 editable 安装下从项目根直接跑会 `ModuleNotFoundError`。
- **L14** `glob("*.md")` 不匹配 `.MD`（Linux 大小写敏感）与点文件；NFC/NFD 同名文件判重失败。
- **L15** `.env.example:75-77` 结构错位（本轮 diff 引入）：新插入的「日志归档」小节把 `AGENT_MIGRATE_VECTOR`（向量维度迁移）的注释与变量割走，读起来像日志配置；该小节也缺结束分隔线。
- **L16** `BACKLOG.md` 与本轮实现直接矛盾：仍写「M7 只剩成本预算」「`client.py` 完全无 usage 解析」「581 收集」，而 HEAD 已有 `budget.py` + usage 钩子、实测 668 收集 → 下一个实现者会照其 §4 迭代计划重做已完成的 A1。
- **L17** `README.md` M7 行「定时推送 ⏳（蒸馏调度已落地）」与 `BACKLOG.md`（4 个 job 已接线 + `ReminderService` 已接 sink）、README 自身「定时提醒 / 主动推送」节互相矛盾；需澄清「定时内容推送」与「已落地的提醒推送」的区别。
- **L18** `README.md` 目录树仍写 `data/kb_samples/ # 示例知识库语料`（fresh clone 无此目录），且未列本轮新增的 `data/budget`、`data/logs` 以及 `review/` 下的评审产物（`FIX-*.md` 已随本轮归档约定迁入）。

---

## 2. 被证伪的发现

| 怀疑 | 结论 | 原因 |
|---|---|---|
| 线3-M1「字节预检 vs 字符截断：1536KB–2048KB 区间静默切尾」 | **被证伪** | 主代理实测：纯 CJK 600k 字（1.8MB）预检通过 → `len(body.encode())=1.8MB ≤ 2MB` → 不触发内部截断。预检通过者必不截断（正常文本）；仅乱码文件可能，降 L6 |
| 「测试会污染仓库 `data/budget`/`data/logs`」 | **被证伪** | 清理后单独与全量重跑测试均**未生成**这两个目录；原先文件来自运行中的 bot 进程（§1-M8 的操作事故根因） |
| 月切换时旧月内存数据未落盘即被替换 | **被证伪** | 任何内存变更后紧跟 `_save(同月)`；仿真确认 10 月文件只含 10-01 条目 |
| `today()` 读路径会产生幽灵日条目 | **被证伪** | 跨月重载丢弃；同月内即"今天"，`record` 本来也建；不落盘无痕 |
| 同一响应被记两次（chat 主备/embedding 双路径） | **被证伪** | `_post` 单点记录；fallback 只记一次；4xx/`json()` 抛错在记录之前；`LLMClient.embeddings` 全仓零调用方 |
| `record_embedding_usage` 把 embedding token 计入 chat 预算 | **被证伪** | `record("embedding")` 只加 `embedding_tokens`；测试断言 `day["total"] == 0` |
| 存在绕过闸门的第二 LLM 入口 | **被证伪** | 所有 `.chat(` 调用点（engine tool-loop / facts / distill / prompt-skill / info-skill）均在 `run()` 或 `digest()` 之内；闸门拦截时 `run` 直接 return，下游不执行 |
| `kind=file/sample` 造成双份入库或检索污染 | **被证伪** | `kind` 不参与检索/统计，唯一逻辑用途是 `kind=='distill'` 水位线 |
| `name=None` 导致判重误判 | **证据不足** | `kb_sources.name` 为 NOT NULL；`Path("None.md").name == "None"` 不与 `None` 相等 |
| `.gitignore` 的 `logs/` 与 `data/logs/` 规则冲突 | **被证伪** | `git check-ignore -v` 命中 `.gitignore:75` 显式行 |
| 线3「M2 未修」 | **部分证伪** | 判重生效、失败重跑幂等（主代理用持久化 FakeKB 实测 DB 2→2）；但**只覆盖"失败→重跑"**，不含内容更新/改名/>1000 来源（→ M5/M6/L2） |
| 线3「M3 未修（超限跳过失实）」 | **证伪** | 主代理实测 3MB 文件打印「超过 2MB 上限，跳过」且不计 failed、不翻退出码 |
| 新 @bot 扫描与适配器 `_check_at_me` 重复/冲突 | **被证伪** | 二者判定同构且**互补**：适配器删掉首/尾 at 段并置 `to_me`，扫描补中间，`is_tome()` 补被删的 |
| `to_me` 语义漂移会污染下游 | **被证伪** | `grep -rn "to_me" plugins/ agentcore/ bot.py` 除 `matcher.py:41` 注释外零命中；下游用的是 `event.self_id` |
| `seg.data` 非 dict（`None`/`list`）会让新增 `.get` 崩 | **被证伪** | `matcher.py:34`（既有行）先崩：`str(Message)` 对非 text 段调 `self.data.items()`；新增 `.get` 不引入新失败面，且需上游非协议负载 |
| `self_id=""` 会匹配 `qq=""` 的段（误触发） | **被证伪** | `self_id: int` 由 pydantic 校验，传 `""`/`None` 直接 `ValidationError`，事件构造不出 |
| 线4 存疑「运行实例 `AGENT_LOG_KEEP_DAYS=0`，日志归档实际未落盘」 | **被证伪（主代理自证）** | `data/logs/` 缺失是主代理误删所致（§1-M8）——删除前该文件确实存在（16261 字节）；与配置无关。残余不确定：`agent.log` 由当前进程还是更早进程创建（mtime 仅到分钟），但不影响"删除导致缺失"的结论 |
| 线3「H1 是本轮新引入」 | **部分证伪（归因修正）** | `MAX_CHUNKS_PER_SOURCE=200` 是既有常量，本轮 `bef83a8` 未改 `ingest.py`；本轮的判重既未引入也未修复截断（判重前重跑只会新增同名残缺来源）。**但 H 级结论仍成立**：静默数据丢失是事实，且已发生在生产库 |

---

## 3. 测试与文档状况

### 实证数据
- **全量测试**：`.venv/bin/pytest tests/ -q` → **636 passed / 32 skipped**（与 `FIX-fad144b..bbd8913.md` 自述一致）
- **lint**：`.venv/bin/ruff check agentcore plugins tests bot.py scripts` → **All checks passed!**
- **依赖安全（§4 必查）**：`git diff bbd8913..f6dffcc -- pyproject.toml` **为空**；仓库无 requirements/lock 文件，零新增/升级依赖
- **PG 只读核查**：`kb_sources` 39 条（`sample` 37 / `distill` 2）、`kb_chunks` 6847；**9 条 sample 撞 200 上限**（H1 证据 B）
- **CI**：`.github/workflows/ci.yml` 用 py3.12、`ruff check agentcore plugins tests bot.py scripts`（覆盖 `scripts/`）+ `pytest -q`；无 PG service → 32 skipped 为既有 PG 门控

### 未验证面（§8.3 门禁声明）
| # | 未验证面 | 来源 | 本轮状态 | 门禁结论 |
|---|---|---|---|---|
| 1 | 本地 embedding 服务（Ollama/TEI）成功响应的 `usage` 字段与分批行为 | 上轮 | **部分销项**：本机无任何本地 embedding 服务（11434/8081/8000/9997 全不可达），部署实际用 DashScope 兼容端点；**真实链路的 usage 采集已被生产账本证实**（`embedding_tokens=1378` / `embedding_requests=18`，分批=10 正常无 400）。Ollama/TEI 仍不可验证，当前部署不使用 | 不阻断 |
| 2 | DashScope 错误体实际内容 | 上轮 | **部分销项**：线1 真实 POST（假 key）得到 401 + 结构化 JSON（`error.message`/`code` + `request_id`），`resp.text[:300]` 透出有效；线4 拒绝代持用户 key 发业务失败请求 → 400/限流等**业务错误体仍未验证** | 不阻断 |
| 3 | 真机多账号下完成通知送达率 | 上轮 | **仍不可验证**（无真机、日志未启用） | 不阻断（上轮 L4 已记录为遗留） |
| 4 | systemd 部署 CWD 对相对路径账本/日志的影响 | 上轮 | **销项**：实测本部署为手工启动（父进程 `/bin/bash`，非 PID 1），唯一账本落在仓库内 ⇒ CWD=仓库根，相对默认路径当前安全。**残余风险已并入 M12**：systemd 默认 `WorkingDirectory=/` 会让 `bot.py:23` 的 `mkdir` 抛 `PermissionError` → 启动失败 | 不阻断；建议随 M12 一起修 |
| 5 | `AGENT_BUDGET_ENFORCE=1` 端到端真实链路 | 本轮新增 | 未验证（生产 `=0`）；已用脏账本仿真触发 24 failed 证明闸门确实生效 | 不阻断 |
| 6 | 多进程账本并发丢账 | 本轮新增 | 仅同进程双实例仿真 | 不阻断（当前单进程） |
| 7 | `TimedRotatingFileHandler` 午夜轮转真实行为 | 本轮新增 | 未验证（需跨午夜） | 不阻断 |

**门禁结论**：**H1 存在 → 按 §8.3 阻断知识库摄取路径的发布/合并**，直到截断被修复（可配置上限 + 告警 + 存量重灌）。其余 M1–M12 均可本地复现（M8 为评审操作事故），不属"未验证面"降级范围；M3 的多进程变体、M12 的 systemd 变体已声明为未验证面。

### 文档一致性
- `README.md` 成本预算节 / 日志归档节与 `budget.py`、`engine.py`、`rag/service.py`、`bot.py` 逐项吻合（除 M1 的"当日不再发起任何 LLM 调用"口径）
- **声称核对（§3 声称管理）**：`be13897`（usage 钩子覆盖主备、embedding 分账、日志轮转、M4/M5 修复、gitignore 例外清理）、`00eaa52`（扫描全部 at 段 + 回归）、`bef83a8`（同名跳过 + kind=sample + 预检真跳过）**逐条与 diff 相符，无夸大**
- 缺口：`AGENT_BUDGET_DIR` 未见于 `.env.example`/README；`AGENT_LOG_DIR` 未见于 `.env.example`；README:243「`AGENT_KB_ENABLED=0` 可整体关闭」与 `add_file` 不受门控不符（L7）

---

## 4. 已验证为「无问题」的关键项

| 要点 | 验证方式 |
|---|---|
| M4 跨月竞态已修（单次 `date.today()` 贯穿日键与月文件） | 主代理计数仿真：`record()` 仅调 `today()` **1 次**；用重构的旧实现跑同一测试场景产出 `usage-2026-10.json`（旧实现必红）、新实现产出 `usage-2026-09.json` |
| M5 恒真断言已修 | 断言改为 `glob("usage-*.json")`；主代理注入 `usage-2026-09.json` 后断言会失败（非恒真） |
| M1/M2/M3（上轮）已修 | `git check-ignore` 确认 kb_samples 全忽略、无 hello.md 例外；持久化 FakeKB 实测重跑幂等（DB 2→2，`kind=sample`）；3MB 文件真跳过且 rc 不变 |
| `@bot` 任意位置触发鲁棒 | 主代理 9 种形态实测：int/str `self_id`×int/str qq 全命中；@别人、匿名@(80000000)、@全体(all)、无@ 全不触发；`to_me=False` 时仍正确。线2 另用 12 行真值表复核，与 `README.md:147-150` 的 OR 语义一致 |
| 不可信内容无法唤醒 bot | 线2 读码 + 仿真：`_check_reply` 把被引用消息存到 `event.reply`（走 API），**不注入 `event.message`** → 引用/转发里的 `@bot` 不参与扫描 |
| 新增扫描的性能开销可接受 | 线2 实测 `trigger_rule`：1 段文本 5.9µs（扫描 0.52µs）、1000 个 at 段 1.45ms（扫描占 18%，既有 `str(get_message())` 同阶 O(n)） |
| 新触发面不抢管理命令 | 读码：`chat_matcher` `priority=10`（最低），admin 命令 `priority=5`、确认删除 `priority=8`，不会被截断 |
| 测试有效性 | `test_budget.py` 8 例逐条可在旧实现下变红（M4/M5 反向验证）；未发现新恒真断言 |
| usage 上报无双计/漏计 | `_post` 单点；fallback 只记一次；4xx 与 `json()` 异常在记录前；无 `usage` 字段早退 |
| 账本 embedding 与 chat 分账 | `record("embedding")` 只加 `embedding_tokens`；`chat_blocked`/`estimate_cost` 只读 `prompt+completion` |
| 数据治理干净 | `git ls-files data/` 仅 5 个 skills 文件；kb_samples/budget/logs 零跟踪；`git count-objects -vH` = 1071 对象 / 4.88 MiB，最大 blob 51KB（无大文件入库） |
| 依赖安全 | 范围内 `pyproject.toml` 零变化；无未锁定版本引入 |
| **M7 成本预算在真实链路生效**（生产证据） | 运行中的 bot（PID 1580245）账本 `data/budget/usage-2026-09.json`：`prompt 69201 / completion 5323 / embedding_tokens 1378 / chat 15 次 / embedding 18 次` → 真实 LLM 与真实 DashScope 兼容 embedding 的 `usage` 均被采集落账；embedding 与 chat **分账正确**（embedding 未计入 `total`） |
| `v11._check_at_me` 确实只认首/尾 @bot（本轮修复前提） | 线4 直接读 `.venv` 内 nonebot 源码核实：`bot.py:64-` 只检查首段与尾段；中间 `at(bot)` 时 `to_me=False` → 本轮补扫描确有必要，且保留 `is_tome()` 兜底无回归 |

---

## 5. 与在库报告的衔接复核

上轮 `REVIEW-fad144b..bbd8913.md` §1 的 H/M 修复状态：

| 上轮问题 | 状态 | 证据（主代理实证） |
|---|---|---|
| M1 bbd8913 声称失真（gitignore 例外未删） | **已修复** | `.gitignore` 无 `!data/kb_samples/hello.md`；`git check-ignore -v` 命中 `data/kb_samples/*`；全仓无 hello.md 跟踪 |
| M2 脚本非幂等、无判重 | **已修复（范围有限）** | 持久化 FakeKB 实测 run2 全部跳过、DB 2→2、embedding 每文件 1 次；但内容更新/改名/超 1000 来源不覆盖（本轮 M5/M6/L2） |
| M3 docstring「超限跳过」失实 | **已修复** | 预检 `stat > MAX_INGEST_BYTES` → `continue`，不计 failed、rc 不变（实测） |
| M4 预算跨月午夜竞态 | **已修复** | 单次 `date.today()`；旧实现反向验证必红（本轮 §4） |
| M5 恒真断言 | **已修复** | 断言改 glob；注入落盘即失败 |
| L1 @路径唤醒词残留 | **仍未修，且本轮升级为 M9** | `strip_wake_word(" 小助手 …")` 原样返回；本轮 `00eaa52` 使该路径首次可达 |
| L3 判重 1000 天花板 | **仍未修** | 仿真 1001 条时旧同名落出窗口 |
| L8 日志脏值崩启动 | **仍未修** | `int("abc")` → ValueError 顶层未捕获（实测） |
| L12 `AGENT_BUDGET_DIR` 未进 `.env.example` | **仍未修**（且 `AGENT_LOG_DIR` 同缺） | grep `.env.example` 确认 |
| L13 embedding 用量不展示 | **仍未修** | `_build_status_lines` 只输出 chat total |
| 上上轮 H1 `split_message` 边界 strip / H2 `_reconstruct_content_from_memory` 死代码 | **仍未修** | 本轮未触及，继续遗留 |

**本轮新增待修**：H1（截断）/M1（闸门范围）/M2（脏值 fail-open）/M3（账本互斥）/M4（结构容错）/M5（内容指纹）/M6（僵尸来源）/M7（测试隔离）/M8（操作事故记录）。

---

## 6. 修复优先级建议

1. **P0（阻断项）**：修 H1——`MAX_CHUNKS_PER_SOURCE` 提为可配置 + `ingest_text` 返回 `dropped` 并告警；**并重灌已截断的 37 条 sample 来源**（删旧 + 高上限重导）。修复前不建议继续扩充知识库语料。
2. **P1**：M2（`_env_int` 脏值 warning + 非法值回退）——安全机制 fail-open；M4（账本结构校验 + `_blank_day`）；M1（闸门循环内复查，或把代码注释/README 改为"新轮次被拦截、进行中的轮次跑完"）。
3. **P1**：M5/M6（内容指纹判重 + `--force` + `--prune`，脚本与 `/kb samples` 同步）。
4. **P1**：M7（conftest 隔离 `AGENT_BUDGET_*`）——避免本地脏环境打挂 24 个引擎测试。
5. **P1**：M9（`strip_wake_word` 先 `lstrip`，一行）+ M10（补「`to_me=True` 且无 at 段」用例、去掉 `test_matcher.py:257` 冗余打桩）——一个是行为修复，一个是防止 `or event.is_tome()` 被误删的护栏。
6. **P1**：M12（`bot.py` 日志初始化加 try/except + 脏值回退，`mkdir` 失败降级为仅控制台）——唯一会导致**服务完全不可启动**的问题，systemd 场景必现。
7. **P2**：M11（唤醒词触发判定与 `pipeline._build_user_text` 统一口径，必须与 M9 同批修）；L7（`AGENT_KB_ENABLED` 门控 `add_file` 或改 README 措辞）；L12（闸门阻断文案去掉内部 token 数字）。
8. **P2**：M3（`.part` 加 pid 后缀、README 声明单进程假设）。
9. **P3**：L2（判重分页/唯一约束）、L4（补 `AGENT_BUDGET_DIR`/`AGENT_LOG_DIR` 到 `.env.example` + 绝对路径提示）、L5（`/status` 补 embedding 用量）、L8（脚本退出码区分）、L10（脚本抽纯函数 + 5 条测试）、L11/L13/L14（价格校验、`sys.path`、扩展名/Unicode 归一）、L15–L18（`.env.example` 段落错位、BACKLOG/目录树/M7 口径）。
10. **重申（多轮遗留）**：`split_message` 边界 `.strip()`（上上轮 H1）、`_reconstruct_content_from_memory` 死代码（上轮 H2）、`AGENT_PREFIX` 掩蔽测试（上轮 M4）、判重 1000 天花板、`_remote_embed` 条数校验、`reasoning_content` 400。
11. **规范建议**：把 M8 的教训写入 §7——清理 `data/` 下任何路径前先确认无运行进程；或明确"只读观察，不删运行数据"。
