# agent-demo 近期 Commit 评审报告
**评审范围**：`436629d..fad144b`（1 个 commit：`fad144b`「feat: add custom wake words; fix ruff UP038; review 19aff9f..436629d」）
**评审日期**：2026-09-11
**评审方式**：3 个并行只读子代理分线审查（唤醒词与消息流水线 / skills 与配置文档一致性 / 工程与未提交变更）+ 主代理逐条实证（实跑 pytest/ruff、复现脚本、证伪、声称核对）
**工作区状态**：**不干净**。已提交部分与未提交部分分别标注。未提交内容：M `plugins/qq_agent_adapter/admin.py`、M `tests/test_rag.py`；未跟踪 `scripts/ingest_kb_samples.py` 及 `data/kb_samples/` 下 9 个 .md（合计约 97.8MB）。以上均纳入本轮评审。

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | 无 H。最高为 **M1 唤醒词命中后不剥前缀**：唤醒词原样进入 LLM prompt，且与旧前缀/@机器人两条触发路径行为不一致（已提交部分）与 **M5 ingest 脚本声称与实现不符**（未提交部分） |
| 唤醒词功能 | 触发判定本身可用、权限门控（`is_allowed`）无缺口、旧前缀用法无回归；但下游不剥唤醒词、文档表述两处失真 |
| UP038 改写 | 6 处 `isinstance` 元组→`X|Y` 全部行为保持（双版本 28 条表达式对照 0 差异）；但 **UP038 在 pinned ruff 0.9.6 下本就不报**（旧代码显式 `--select UP038` 实测全绿），commit message 的动机声称不可复现 |
| 未提交部分 | `parse_kb_cmd` 未知词回退 search 实现正确、权限边界未动；**ingest 脚本 3 个问题需在提交前处理**（声称失真+静默截断+单请求无分批）；97.8MB 数据未 gitignore |
| 测试与 lint | 全量 **610 passed / 32 skipped**（基线 603 + 新增 7 用例）；已跟踪代码 ruff 全绿；未跟踪脚本 1×F401（`import glob`），一进版本库 CI 必红 |
| 声称核对 | commit message 4 条声称：唤醒词✅、报告归档✅、**outbound docs❌（diff 零 outbound 改动）**、**fix ruff UP038❌（pinned ruff 下无此告警）** |
| 上轮遗留 | 上轮 H1/H2/M1/M2/M3 **全部未修复**（本 commit 未触及），其中 M1/M2 反被 commit message 声称已更新文档 |

---

## 1. 已实证的问题

### M1（已提交）唤醒词命中后不剥前缀：唤醒词原样进入 prompt，三条触发路径行为不一致
- **位置**：`plugins/qq_agent_adapter/matcher.py:32-38`（触发）、`plugins/qq_agent_adapter/pipeline.py:174`（剥前缀只认 PREFIX）、`matcher.py:152-154`（`user_text` 喂给 `_user_asked_for_file`）
- **证据**（主代理【已复现】，读码 + 纯内存仿真）：
  ```python
  # pipeline.py:174 —— 下游只剥 PREFIX 正则，不认识唤醒词
  return re.sub(PREFIX, "", "".join(parts), flags=re.IGNORECASE).strip()
  ```
  仿真：wake=`小助手`，消息「小助手 帮我查一下天气」→ `user_text = '小助手 帮我查一下天气'`，唤醒词未剥离。
- **影响**：旧前缀路径（`ai 天气`）由 pipeline 剥前缀、@机器人路径由适配器删 at 段，唯唤醒词路径不剥——LLM 每轮收到「小助手」噪声，多轮对话 prompt 风格不一致；且若唤醒词本身含「文件/文档」等关键词，`_user_asked_for_file` 会误触自动发文件。
- **修复建议**：`_build_user_text` 在 `re.sub(PREFIX, ...)` 前先按最长匹配唤醒词剥离。

### M2（已提交）README 触发规则排他式表述与实现不符，旧前缀用法文档失踪
- **位置**：`README.md:377-381`；`matcher.py:38`
- **证据**（主代理【已复现】）：`_match_wake_words` 的正则分支不在「留空才回退」结构里，是**无条件 OR**：
  ```python
  if wake_words and any(lowered.startswith(w.lower()) for w in wake_words):
      return True
  return bool(re.match(PREFIX, text, re.IGNORECASE))
  ```
  仿真：配置 `AGENT_WAKE_WORDS=小助手,助手` 后 `!ai 你好` 仍触发。README 新文案「**命中自定义唤醒词或 @机器人 才会进入处理**」存在反例；且 commit 前 README 明文的 `ai ` / `!ai ` / `/ai ` 用法在 commit 后全文零命中（仅 `.env.example:84` 还留着 `AGENT_PREFIX`）。
- **影响**：用户按文档配置唤醒词后，`ai` 前缀消息仍会触发，行为与文档预期相悖；全角叹号等仅正则能匹配的用法在文档层面失踪。
- **修复建议**：README 补回旧前缀说明并注明「唤醒词与旧前缀正则取或」；`.env.example` 同步。

### M3（已提交）commit message 两条声称失真（声称管理）
- **位置**：`fad144b` commit message；对照 `git show fad144b --stat`
- **证据**（主代理【已复现】）：
  1. 「Update README and .env.example for wake words **and outbound docs**」：diff 里 README 仅消息路由 +6/-2、.env.example 仅唤醒词 +4，**零 outbound 改动**。上轮 M1（`AGENT_OUTBOUND_MAX_TARGETS` 无文档）实测仍为零命中，M2/M3 表述原样。
  2. 「fix ruff UP038」：用 pinned `ruff==0.9.6` 对 436629d 旧代码显式 `--isolated --select UP038` 实测 **All checks passed**——该规则在本仓库工具链下本就不报（0.16.6 中已移除，0.9.6 中亦不触发）。改写无害，但动机声称不成立。
- **影响**：按 commit message 追踪修复的人会误以为上轮文档缺口已闭环、lint 曾红；违反规范 §3「夸大即记 M」。
- **修复建议**：后续 commit message 如实描述；outbound 文档缺口真正补上（见 §5）。

### M4（已提交）掩蔽性测试：`monkeypatch.setenv("AGENT_PREFIX", ...)` 是死代码
- **位置**：`matcher.py:23`（`PREFIX = os.getenv(...)` 导入时冻结）；`tests/test_matcher.py:286-311、313-338`
- **证据**（线1【已复现】+ 主代理复核）：`_match_wake_words` 用的是模块常量 `PREFIX`，测试在 import 后 setenv 不生效；`test_group_falls_back_to_prefix_when_no_wake_words`（setenv 值恰好等于默认值）与 `test_group_prefix_still_works_when_wake_words_configured`（setenv 了不同正则照样通过）均靠「`ai 你好` 匹配导入时默认正则」碰巧变绿。
- **影响**：两个用例无法守护「自定义 AGENT_PREFIX 生效」——若未来把 PREFIX 改成函数级读取之外的任何破坏方式，测试不会变红（同上轮 H5 假阳性主题）。
- **修复建议**：改用 `monkeypatch.setattr(matcher, "PREFIX", ...)`，并让设置值对测试文本有区分度（如匹配 `qq` 前缀的正则）。

### M5（未提交/未跟踪）ingest 脚本声称「超限自动截断」实为抛错：8/9 文件将失败且退出码 0；唯一可入库文件遭 200 块静默截断
- **位置**：`scripts/ingest_kb_samples.py:6,63-64`；`agentcore/rag/ingest.py:73-74,13,40,43`；`agentcore/embedding/client.py:68-77`
- **证据**（主代理复核文件尺寸与代码路径【已复现】；chunk 比例为【推演】）：
  ```python
  # ingest.py:73-74 —— 文件路径是抛错，不是截断（截断只在 ingest_text 文本路径）
  if p.stat().st_size > MAX_INGEST_BYTES:
      raise ValueError(f"文件过大（>{MAX_INGEST_BYTES // 1024}KB）")
  ```
  实测：9 个新样例中 8 个超 2MB（战双 36.7MB、原神 21.9MB、鸣潮 8.7MB、梗知识 8.5MB、FGO 8.0MB、无限暖暖 5.9MB、崩坏3 4.5MB、终末地 3.1MB），仅绝区零（0.7MB）可入库；脚本逐文件 print 后继续、最终退出码 0。且 `ingest.py:40` `[:MAX_CHUNKS_PER_SOURCE]`（200 块）静默砍尾、无告警（对比 `ingest_text:35` 文本截断有 warning）；`embed_many` 将至多 200 块打进单个 POST（timeout=30，无分批无重试）。
- **影响**：用户以为导入 97.8MB，实际约 0.7MB 且尾部再被砍，全程无感；自动化场景退出码 0 掩盖失败。
- **修复建议**：docstring 改为「超限跳过」；截断时 `logger.warning` 并在返回值带 `truncated`；失败计数写入退出码；`embed_many` 分批（32/64）。

### M6（未跟踪数据）97.8MB 知识库样例未入 .gitignore，一次 `git add -A` 即永久膨胀仓库
- **位置**：`.gitignore`；`git check-ignore -v data/kb_samples/FGO.md` 退出码 1【已复现】
- **影响**：最大单文件 36.7MB，虽未触 GitHub 100MB 硬限，但误提交后历史无法轻易收缩，clone 体积永久 +97.8MB。
- **修复建议**：`.gitignore` 增加 `data/kb_samples/*` + `!data/kb_samples/hello.md`；如需记录来源，提交一个不含正文的 MANIFEST。

### L 级（压缩列出）
- **L1**（已提交）唤醒词 `startswith` 无词边界：`airport 查询`、`AI帮我` 均触发（主代理【已复现】）。对 wake=`ai` 与旧 PREFIX 宽松度持平非回归（见 §2），但自定义短词的误触发面扩大，README 未提示「开头即匹配、无边界、勿配过短词」。
- **L2**（已提交）`AGENT_WAKE_WORDS` 只认半角逗号：全角「，」使整条配置变成一个唤醒词、静默失效（主代理【已复现】：`'小助手，助手'` → `['小助手，助手']`）。同仓库 `basic_tools.py` calc 对全角标点做了归一化，此处没有。
- **L3**（已提交）配置语义契约漂移：`AGENT_WAKE_WORDS` 每条消息动态读 env，`PREFIX` 在 matcher.py:23 与 pipeline.py:45 两处导入时冻结——运行中改 env 一个生效一个不生效。
- **L4**（已提交）`test_no_bot_raises_and_falls_back_to_error_reply` 顺带删除 `assert "no bot connected" in sent[0]`（与唤醒词无关的 scope creep），不再锁定具体异常来源。
- **L5**（已提交）新增用例实为 7 个（非 8），缺大小写、空串/全逗号、无边界误触发、下游剥前缀等负例/行为锁定；新测试用 `parse_obj` 产生 7 条 Pydantic 弃用告警。
- **L6**（未提交）`parse_kb_cmd` 未知词静默转搜索无任何文档：`/kb addx 标题|正文` 手滑变成一次 embedding 检索而非用法报错（线3【已复现】）；README `_KB_USAGE` 未提及。
- **L7**（未跟踪）ingest 脚本工程细节：`import glob` 死代码（ruff F401 实证，提交即 CI 红）；`hello.md`（224B 演示数据）会被一并灌入公共知识库；`PgMemoryStore` 无清理、asyncio 收尾告警噪音；hello.md 权限 600 与其余 664 不一致。
- **L8**（工程）本地 venv 的 ruff 为 0.16.6，与 pyproject 钉住的 `ruff==0.9.6` 漂移（CI 用 0.9.6）：本地 lint 与 CI 规则集不一致（UP038 在 0.16.6 已移除），「本地全绿」不能等价「CI 全绿」。

---

## 2. 被证伪的发现

| 怀疑 | 结论 | 原因 |
|---|---|---|
| wake=`ai` 命中 `airport` 是本次引入的回归 | **被证伪** | 主代理【已复现】：旧 PREFIX `^[!！/]?ai\s*` 经 `re.match` 同样匹配 `airport 查询`（`\s*` 可空）；宽松度持平 |
| UP038 改写改变计算器求值行为（bool/嵌套幂/炸弹防护） | **被证伪** | 线2 双版本并行仿真 28 条表达式（含 `9**9**9**9`、`2**1001`、`99999999999+1`）0 差异【已复现】 |
| `monkeypatch.setattr(event, "is_tome", ...)` 在 pydantic 模型上失效，测试恒真掩蔽 | **被证伪** | 线1【已复现】：`Event` 为 `extra="allow"`，实例字典遮蔽类方法，mock 真实生效且为断言成立的必要条件 |
| 38MB 文件批量导入会对 embedding API 发起巨量请求 | **被证伪** | 线3：`ingest_file` 在读文件前即按 2MB 抛 ValueError，大文件根本触不到 embedding；真实代价是 M5 的批量失败 |
| `parse_kb_cmd` fallback 会把 `/kb` 前缀带进搜索词 | **被证伪** | 线3 15 例仿真：前缀剥离发生在 fallback 之前，`("/kb 未知词")→('search','未知词')`【已复现】 |
| `_KB_ACTIONS` 与 handle 分支存在漏项/死条目 | **被证伪** | 线3 逐行核对 8 个成员与 handle 8 个分支完全一致 |
| 唤醒词会劫持 `/help` `/reset` `/status` 管理指令 | **被证伪** | 管理指令为独立 `on_command(priority=5, block=True)`，不经过 `trigger_rule` |
| 未提交的 admin.py 改动引入权限口子 | **被证伪** | `parse_kb_cmd` 是纯函数无权限语义；`handle_kb` 入口 `is_allowed` 与管理员门控结构未动 |

**存疑**：绝区零.md 被 200 块截断的具体比例（约 392 块估→砍半，未实跑确认）；UP038 在 ruff 0.7–0.9 各小版本间的启用状态差异（不影响 M3 结论：pinned 0.9.6 下新旧代码均全绿）。

---

## 3. 测试与文档状况

### 实证数据
- **全量测试**：`.venv/bin/python -m pytest tests/ -q` → **610 passed, 32 skipped**（基线 603 + 本 commit 新增 7 用例，数目吻合）
- **lint**：venv ruff 0.16.6：已跟踪代码（`agentcore plugins tests bot.py scripts/backup_db.py scripts/scratch_db.py`）**全绿**；含未跟踪脚本全仓跑出 1×F401（`scripts/ingest_kb_samples.py:11 import glob`）。另用 `uvx ruff@0.9.6`（pinned 版）复验关键结论，见 M3/L8
- 32 skipped 为 PG 门控用例（无 `TEST_DATABASE_URL`），CI 同样不跑——既有盲区，本轮改动未触及

### CI 覆盖
- 新增的 7 个触发规则用例与 parse_kb_cmd 用例均在 `pytest -q`（py3.12）路径内，CI 可覆盖
- 盲区：CI 无 PG service、仅 py3.12（3.11 下限不测）；**若未跟踪脚本原样提交，CI lint 立即因 F401 变红**

### 文档一致性
- 失真三处：README 触发规则排他表述（M2）、README/.env.example 的 outbound 文档缺口依旧（M3）、ingest 脚本 docstring「自动截断」不实（M5）
- 增量缺口：唤醒词无边界/大小写不敏感/半角逗号限制均未写（L1/L2）

---

## 4. 已验证为「无问题」的关键项

| 要点 | 验证方式 |
|---|---|
| UP038 六处改写行为保持（basic_tools 4 + utility_skills 1 + media 1） | 线2 双版本 28 条表达式对照 + 主代理新旧 ruff 双验【已复现】 |
| 唤醒词空值/空串/全逗号/正则特殊字符安全回退 | 线1 + 主代理【已复现】：回退 PREFIX 分支，字面匹配无异常 |
| 唤醒词大小写归一（双侧 lower） | 线1【已复现】 |
| 旧前缀 `!ai` `/ai` `ai` 用法无回归 | 线1 + 主代理【已复现】，且有测试固化 |
| 群聊触发权限门控 `is_allowed`（matcher.py:83）未被触发面扩大绕过 | 主代理读码 + 线3 确认 |
| `media.py` `isinstance(v, str \| list)` 行为保持（10 类值对照） | 线1【已复现】 |
| 未提交 `parse_kb_cmd`：aliases 5 项全部映射进 `_KB_ACTIONS`、fallback 无前缀污染、`test_rag.py` 断言同步无漏改 | 线3 15 例仿真 + 全仓 grep【已复现】 |
| ingest 链路：PII `scrub_pii` 仍生效、`kb_add_chunks` 失败回滚来源行、`probe_dim` 失败回退 `dim` | 线3 读码 |
| 上轮节流/切分相关回归测试全绿（610 passed 含全部上轮新增用例） | 主代理实跑 |

---

## 5. 与在库报告的衔接复核

上轮报告 `REVIEW-19aff9f..436629d.md` 各项修复状态（本 commit 未含修复性改动）：

| 上轮问题 | 修复状态 | 证据 |
|---|---|---|
| H1 `split_message` chunk 边界 `.strip()` 压平缩进（P0） | **未修复** | 主代理 grep：`outbound.py:158,181,186` 三处 `.strip()` 原样 |
| H2 `_reconstruct_content_from_memory` 死代码（P1） | **未修复** | 主代理读码：`file_sender.py` 仍无条件 `return ""` |
| M1 `AGENT_OUTBOUND_MAX_TARGETS` 未文档化（P1） | **未修复**，且被 commit message 声称已更新 | 主代理 grep README/.env.example/docs 零命中 |
| M2 `AGENT_OUTBOUND_MAX_WAIT` 约束范围未说明 | **未修复** | `README.md:288` 一字未改 |
| M3 文件发送节流覆盖未列入 README | **未修复** | 覆盖范围列表未更新 |
| L `_evict_idle` 命名 / `send_forward_msg` 真机验证 | **未触及** | 本 commit 无相关改动 |

---

## 6. 修复优先级建议

1. **P1**：`_build_user_text` 剥离唤醒词前缀（M1，随 M2 一并定「唤醒词 vs 旧前缀」的产品语义）
2. **P1**：提交未提交部分前先处理：ingest 脚本 docstring/退出码/删 `import glob`（M5/L7）、`.gitignore` 收编 kb_samples（M6）
3. **P1**：README/.env.example 补 outbound 文档 + 修正触发规则表述（M2/M3，即上轮 M1/M2/M3 的真正闭环）
4. **P2**：掩蔽测试改 `monkeypatch.setattr` 并加区分度（M4）；全角逗号归一化（L2）；`_load_wake_words` 与 PREFIX 读取时机统一（L3）
5. **P2（重申上轮）**：`split_message` 三处 `.strip()`→`.rstrip()`；`_reconstruct_content_from_memory` 死代码处理
6. **P3**：还原 `no bot connected` 断言（L4）；补负例测试与 `model_validate` 迁移（L5）；`parse_kb_cmd` 行为写进文档（L6）；本地 venv 对齐 pinned ruff（L8）
