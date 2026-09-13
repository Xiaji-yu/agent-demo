# agent-demo 近期 Commit 评审报告
**评审范围**：`679c9b3..c472e56`（9 个 commit：REVIEW-a604023..679c9b3 的修复周期 60daae5/971092e + 第二/三/四批修复 44eec62/f4c98c5/c0af639 + routing 语义变更 97e4045 + embedding 时钟修复 a5c5450 + 记录归档 9d2b944/c472e56）
**评审日期**：2026-09-13
**评审方式**：3 个并行只读子代理分线审查（修复批次复核 / routing 语义变更与 embedding 时钟 / 记录工程与依赖）+ 主代理逐条实证（复现、证伪、声称核对）
**工作区状态**：`git status --porcelain` 为空，HEAD = `c472e56`

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | **M1 debounce `max_parts=1` 时会话永久无回复**【已复现】：突发结算分支对新建 entry 的 `task=None` 调 `cancel()` 抛 AttributeError，entry 残留 `_pending`，该会话此后每条消息都再炸（主代理独立复现：runner 执行 0 次）。默认 20 不触发，但配 1 是文档化的合法值 |
| 修复批次质量 | 上轮报告（a604023..679c9b3）的 **5 条 H 与 19 条 M 修复映射全部对上**，测试断言抽查"有牙"；但「假通过用例加固」**漏了上轮点名的 2 条**（M3），台账宣布关闭与实况不符 |
| routing 语义变更（97e4045） | 移除旧前缀执行干净（无死常量、迁移提示如实、README 主体已重写）；但 **`.env.example` 默认唤醒词仍含 `ai`**（M2）——新装环境任何 "ai" 开头的群消息都会触发，变相保留被移除的触发面，且与 README 示例（不含 ai）自相矛盾 |
| 记录与索引 | **REVIEW-a604023..679c9b3 与 FIX 均未追加 §9 索引行**（M4）——增量评审链入口断裂；M 级计数出现 17/19/21 三种口径（L） |
| 测试与 CI | **871 passed / 52 skipped**（923 collected；PG 契约用例已随新 CI service 真跑）；ruff 全绿（含新增 format 门禁）；**CI #35–#38 全绿经 GitHub API 独立核实** |
| 依赖安全（§4） | `pyproject.toml`/`uv.lock` 在本范围 **零变化**，ruff 规则集未动 |
| 质量门禁（§8.3） | 未验证面：`max_parts=1` 是否有真实使用者；DeepSeek thinking fallback（上轮线上 451→400 问题不在本范围且仍未修）；停机 flush 经全局信号量的最长阻塞时长 |

---

## 1. 已实证的问题

### M1（f4c98c5 引入）debounce `max_parts=1`：突发结算对 `task=None` 调 `cancel()`，会话永久无回复
- **位置**：`plugins/qq_agent_adapter/debounce.py:54-59`
- **证据**（子代理仿真 + 主代理独立复现【已复现】）：
  ```python
  entry = {"parts": [part], "runner": runner, "task": None}
  self._pending[key] = entry
  if len(entry["parts"]) >= self.max_parts:
      entry["task"].cancel()   # ← 新建 entry 的 task 是 None
      self._pending.pop(key, None)   # ← 异常抛在 pop 之前，entry 永久残留
  ```
  主代理复现：首条消息 `AttributeError: 'NoneType' object has no attribute 'cancel'`，`_pending` 残留 `['k']`，第二条同样炸，runner 执行 **0 次**。`max(1, int(max_parts))`（:30）与 `.env.example:267` 的 `AGENT_DEBOUNCE_MAX_PARTS` 都允许配 1（语义合理：每条消息独立请求）。
- **影响**：一旦按文档合法值配 1，该会话所有消息静默无回复，只在停机 flush 时才被处理；`_pending` 残留还会让重启前每次 push 继续炸。
- **修复建议**：burst 分支改 `if entry["task"] is not None: entry["task"].cancel()`（两行）；补 `max_parts=1` 回归用例（现有 `TestDebounceBurstCap` 只测 3）。

### M2（97e4045 引入）`.env.example` 默认唤醒词含 `ai`：被移除的旧触发面从默认模板复活
- **位置**：`.env.example:104-107`；对照 `README.md:172`、`wakewords.py` 匹配实现
- **证据**（线2【已复现】+ 主代理核对）：注释写「需要就在此把 ai 列为唤醒词」，但默认值 `AGENT_WAKE_WORDS=小助手,助手,ai` **已经列了**。唤醒词是大小写不敏感、无词界的 `startswith` 前缀匹配——照抄模板的新装环境里，"airpods 值得买吗"、"AI帮我写周报" 全部触发。触发面与刚被 97e4045 移除的旧 `ai ` 前缀相当甚至更宽，与该 commit 自述「配不配唤醒词都一样不再触发」直接冲突；README 示例（`小助手,助手`）与模板不一致；开发者本地 `.env` 用的是 `云崽`，故自测暴露不了。
- **影响**：新部署默认触发面显著过宽（每条 ai 开头群消息 = 一次 LLM 调用）；文档两处默认值互相矛盾。
- **修复建议**：默认值改 `小助手,助手`（与 README 对齐），`ai` 留在注释作可选示例；或保留默认但明示「以 ai 开头的群消息都会触发」。

### M3（f4c98c5/c0af639）「假通过用例加固」漏掉上轮点名的 2 条，台账宣布关闭与实况不符
- **位置**：`tests/test_pipeline.py:401-404`、`tests/test_debounce.py:73-82`；对照 `review/FIX-a604023..679c9b3.md:125,161-170`
- **证据**（线1【已复现（对照 diff）】）：上轮报告 §4 点名 8 条假通过用例，第三/四批收录并加固了 6 条，以下 2 条**未出现在任何仍待修清单**且 HEAD 原样未动：
  1. `test_get_bot_prefers_self_id` 仍只 `assert callable(pl.get_bot)`（docstring 自认"只验证函数存在签名"）——多账号防串号至今零验证；
  2. `test_zero_delay_runs_immediately` 仍靠 `sleep(0.01)` 后计数——删掉 `delay<=0` 快路径照样通过，不验证「立即」。
  第四批自述「假通过用例（原 ~6 条，全部加固并变异复核）」就其自己记账的 6 条属实，但相对上轮报告是漏项。
- **修复建议**：两条补入 BACKLOG 或本轮修复；台账以「上轮报告编号」为锚而不是重新收录。

### M4（971092e 引入）新报告与修复记录均未追加 REVIEW-WORKFLOW §9 索引
- **位置**：`review/REVIEW-WORKFLOW.md`（§9 两张表）；对照 971092e 归档的 `REVIEW-a604023..679c9b3.md` 与 `FIX-a604023..679c9b3.md`
- **证据**（线3【已复现】+ 主代理核对）：`git log 679c9b3..HEAD -- review/REVIEW-WORKFLOW.md` 为空；文件内 `a604023` 仅命中上一轮（第 164 行）。违反规范 §2.5「更新 §9 索引」——增量链「起点=上一份报告终点」的唯一入口断链，且与既有 11 份报告全部在册的惯例断裂。
- **修复建议**：两张表各补一行（本轮归档时只追加本报告自身行，a604023 行留待修复轮）。

### L 级（压缩列出）
- **L1**（线1）新契约文档自相矛盾：`store.py:328` 写「非正 limit 一律返回空结果」，但 `get_history` 两实现均钳到 1、契约测试锁死"钳 1"（`test_store_contract.py:87`）——防漂移文档自带漂移。
- **L2**（线1）`test_review_concurrency_fixes.py:93,102` 裸赋值 `_turn_semaphore = None` 不恢复：测试后进程内单例固定为 `Semaphore(2)`，后续用例在非默认并发度下跑。
- **L3**（线1【推演】）停机 flush 现在要过全局 LLM 信号量：4 个在途回复挂读超时时，`lifecycle.shutdown → flush_all` 排队等待，systemd 短超时下可能被 SIGKILL 反而丢 flush。建议 flush 绕闸或加 wait 上限。
- **L4**（线1）`debounce.py:70` 突发结算任务为无引用 fire-and-forget（asyncio 弱引用，理论风险）。
- **L5**（线1）zip 白名单"含 `=` 即拒"误伤合法文件名参数（`report_v=2.zip`）；perf 套件（`RUN_PERF=1`，9 条）与基线回归仍未接 CI（上轮已点名）。
- **L6**（线2）README:185-187「群聊限制」一句仍残留三处「前缀」措辞，与三行前的「已移除」自相矛盾。
- **L7**（线2）`tests/test_matcher.py:365` 用例与 :309 重复（残留 AGENT_PREFIX 被忽略），命名「prefix both miss」已无对应实现。
- **L8**（线3）M 级计数三种口径互相矛盾：REVIEW 表实数 19 行 vs FIX「17 条」vs BACKLOG「19+2」——实际修复完整，纯属记账不一致。
- **L9**（线3）BACKLOG §6.1 已修项（acl 私聊拒绝、format 门禁、CONTRIBUTING 版本、`AGENT_SKILLS_DIR`）写成无状态标记的"遗留"口吻。
- **L10**（线3）「环境变量核对」宣布关闭但无产物——实际抽审即有漏网（`LLM_EMBEDDING_MODEL` 未入档，属已知死代码路径，应记录结论）。
- **L11**（线3）AGENTS.md:27「agentcore 不得 import nonebot」铁律与 `file_sender.py:17-18` 既有受保护导入冲突且未注明豁免——接手者按铁律"修复"会破坏运行时行为。

---

## 2. 被证伪的发现

| 怀疑 | 结论 | 原因 |
|---|---|---|
| 修复批次「声称修了但 diff 没有」 | **被证伪** | 线1 逐条映射：5H+19M 全部在对应 commit 落地，无虚报 |
| TestFactsFence 断言恒真（`count==0 or 打散串` 疑似永真） | **被证伪** | 逐分支推演：不 neutralize 则 count≥1 且无打散串 → 必红 |
| debounce cancel 会杀掉已在跑的 runner 丢消息 | **被证伪** | 已 pop 的 job 不在 `_pending`；`_key_locks` 串行旧 runner 与新窗口 |
| 两处停机钩子双跑 flush/aclose 有害 | **被证伪** | 空窗口 no-op、`aclose` 二次调用 no-op（store.py:1353） |
| 移除前缀残留死常量/死分支 | **被证伪** | 线2 全仓 grep：仅注释与故意的负向测试；无未用导入 |
| 上轮「@bot 中置扫描」修复被 97e4045 破坏 | **被证伪** | matcher.py:63-67 扫描原样保留，对应测试未改 |
| a5c5450 新测试抓不住旧 bug | **被证伪** | monkeypatch `monotonic=5.0` 时修复前代码首报必被吞 → 断言必红 |
| CI 门禁排除某些测试 / CI 状态声称不实 | **被证伪** | ci.yml 只增不减；线3 经 GitHub API 核实 #35–#38 全绿 |
| 依赖新增/升级 | **被证伪** | pyproject/uv.lock 零 diff |

**存疑**：`max_parts=1` 是否有真实使用者（M1 影响面前提）；内存/PG 水位线在"删源后乱序重建"合成场景下的分叉；plan-job 间被 `/kb file` 抢注同名（沿用上轮存疑）。

---

## 3. 测试与文档状况

### 实证数据
- **全量测试**：871 passed / 52 skipped（923 collected；PG service 进 CI 后，52 条 PG 契约用例在 CI 真跑、本地无 `TEST_DATABASE_URL` 时跳过）
- **lint**：ruff check 全绿；CI 新增 `ruff format --check` 门禁
- **CI**：#35（a5c5450）/ #36（97e4045）/ #37（c0af639）/ #38（c472e56）全绿——线3 经 GitHub API 独立核实，与 c472e56/FIX 记录逐字吻合
- **依赖安全（§4）**：`pyproject.toml`/`uv.lock`/`.pre-commit-config.yaml` 零 diff，ruff 仍 pin 0.9.6
- **CI 覆盖面**：pgvector service + `TEST_DATABASE_URL` 注入为本范围新增（PG 契约不再永久跳过）；仍排除的只有 `RUN_PERF=1` 门控的 9 条 perf 用例（L5）

### 未验证面（§8.3 门禁声明）
`max_parts=1` 的真实使用面；停机 flush 经信号量的实际阻塞时长（L3 需真机 systemd 观测）；DeepSeek thinking fallback（不在本范围，仍未修）。均不阻断本批提交。

---

## 4. 已验证为「无问题」的关键项

| 要点 | 验证方式 |
|---|---|
| 5H 修复：zip 白名单（`--unzip-command=`/`-T`/组合 token 全拒）、curl 非选项参数全当 URL 候选 + SSRF 校验、pow 炸弹静态守卫（`pow(exp=)`/嵌套均拒）、`httpx.TimeoutException` 补全、lifecycle 停机顺序固定且幂等 | 线1 逐条 diff + HEAD 读码 |
| 19M 修复落地：围栏打散/neutralize、CGNAT 双处、`list_facts` 最新优先、`kb_add_chunks` 去重返回实写数、会话键 `p:/g:`、`messages_after` 群白名单、bit=0.125、镜像 sidecar、游标流式（每 500 行 to_thread、失败删 `.part`）、debounce `max_parts`（除 M1 外路径） | 线3 逐条 grep + 线1 核对，`test_store_contract.py` 双实现参数化锁定 |
| 断言质量抽查（`test_review_h/m_fixes.py`、`test_store_contract.py`、c0af639 加固用例） | 线1 各 2-3 例：未 mock 被测逻辑、非恒真 |
| @bot 中置扫描（上上轮 M 修复）在移除前缀后完好 | 线2 读 HEAD + 测试在册 |
| a5c5450 修复无新竞态（check-then-set 均在首个 await 前）；同库无同类哨兵 bug | 线2 全仓 monotonic 哨兵扫描 |
| 路由收敛：私聊路径不受影响、迁移提示如实、`.env.example` 的 AGENT_PREFIX 条目已删 | 线2 grep + 读码 |
| AGENTS.md 交接文档引用的模块/基线/路由不变量与实况相符（除 L11 措辞） | 线3 读码核对 |

---

## 5. 与在库报告的衔接复核

上轮 `REVIEW-a604023..679c9b3.md` 的修复状态：

| 上轮问题 | 状态 | 证据 |
|---|---|---|
| 5 条 H（zip 命令注入 / curl SSRF / pow 炸弹 / 超时误判 / 停机逆序） | **已修复**（60daae5） | 线1 映射表 + `test_review_h_fixes.py` 19 条 |
| 19 条 M（注入面 3 / 存储契约 4 / 备份蒸馏 6 / 并发 6） | **已修复**（44eec62/f4c98c5/c0af639 三批） | 线3 逐条 grep + 三套新测试文件在册 |
| 8 条假通过用例 | **部分修复**：6 条加固，2 条漏项（本轮 M3） | 线1 对照 FIX 台账与 HEAD |
| 工程门禁类 L（format 门禁 / PG service / CONTRIBUTING / `AGENT_SKILLS_DIR`） | **已修复** | 线3 核对（BACKLOG 口吻未更新，L9） |
| perf 套件接 CI | **未修复**（L5） | ci.yml 无 RUN_PERF |

---

## 6. 修复优先级建议

1. **P1**：`debounce.py` burst 分支 `task is not None` 守卫 + `max_parts=1` 回归用例（M1，两行改动，防"配置即瘫"）
2. **P1**：`.env.example` 默认唤醒词去掉 `ai`、与 README 对齐（M2，一行，收敛新装环境触发面）
3. **P2**：REVIEW-WORKFLOW §9 补 `a604023..679c9b3` 索引行（M4）；两条漏项假通过用例补修或入 BACKLOG（M3）
4. **P2**：并发测试单例泄漏改 monkeypatch（L2）；README:185 残留措辞清理（L6）；BACKLOG/计数口径统一（L8/L9）
5. **P3**：停机 flush 信号量上限（L3）、fire-and-forget 持引用（L4）、zip 文件名误伤（L5）、AGENTS.md 豁免注明（L11）、环境变量核对补记录（L10）
6. **重申（多轮遗留）**：DeepSeek thinking fallback `reasoning_content` 400（线上 451→400 链）；AGENT_PREFIX 掩蔽测试类问题（97e4045 后已随代码移除，自然销项）
