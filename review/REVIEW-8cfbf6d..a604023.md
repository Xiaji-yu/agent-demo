# agent-demo 近期 Commit 评审报告

**评审范围**：`8cfbf6d..a604023`（8 个 commit）
**评审日期**：2026-09-11
**评审方式**：5 个并行子代理分线审查（记忆存储 / RAG 脱敏 / 备份恢复 / 沙箱工具 / 工程与声称核对）+ 主代理逐条实证（实跑测试、对抗复现、证伪）
**工作区状态**：`git status --porcelain` 为空（干净），HEAD = `a604023`

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | **H-A 公共库数字脱敏仍可被分隔符绕过**：`/ _ , ， 、 \| \ + ( ) ‧ ZWSP` 等 18/22 种写法原样入库（我已复现）。公共库是全局共享且会注入他人 prompt，手机号/QQ 号以自然写法进入即隐私泄漏 |
| 修复复核 | 上一轮 H1（git 配置注入）**已封死**；H2/H3/H4 **部分修复**（各留一条穿透路径，见 §5）；M3–M11 大部分成立 |
| 新增回归 | 无明显新增 H；本范围引入 3 条**静默路径**（per-message 截断丢尾、batch 窗口停更、restore 不校验校验和） |
| 被证伪 | 子代理报的 **H NEW-1（save_fact 丢作用域条件）为假阳性**（HEAD 第 919 行该谓词存在，且引入时即有）；「私人物件启发误杀 9/12 常规表达」为假阳性（我实测 0/12 误杀） |
| 测试 | 实跑 **581 收集 / 549 通过 / 32 跳过**，ruff 全绿；**32 个跳过全部是 PG 门控**，CI 无 PG 服务 → 本范围 900+ 行 PG 侧回归在 CI 零覆盖 |
| 声称核对 | 三处**未落地/夸大**：①「所有注入点共用围栏」漏了检索结果 ②CI 声明下限 3.11 却只测 3.12 ③`BACKLOG.md` 仍写「schedules 表空置」（M12 漏项） |
| 工程一致性 | py3.11 四处一致 ✓、ruff pin 三处一致 ✓、CI 已纳入 `scripts/` ✓ |

---

## 1. 已实证的问题

### H-A 公共库数字脱敏的分隔符类不完整【已复现】

- **位置**：`agentcore/rag/sanitize.py`（`_DIGIT_SEQ_RE`，编号沿用上轮 H2）
- **证据**：我对 22 种分隔符各构造 `联系 138<sep>0013<sep>8000 找我` 调 `sanitize_point` → **18 种穿透原样入库**：

```
sep='/'  → 联系号码 138/0013/8000 找我
sep='_'  → 联系号码 138_0013_8000 找我
sep='，' → 联系号码 138,0013,8000 找我   （NFKC 已归一为 ASCII 逗号，仍不在类里）
sep='、' '|' '\' '+' '(' ')' '‧' U+200B U+200D U+00AD U+2060 U+FEFF … 同样穿透
被拦下仅 4 种：无分隔、'-'、'.'、空格类
```

- **影响**：确定性第二层实际只覆盖「紧凑/点线/空格」写法；`138/0013/8000` 这类自然书写进入全局公共库后会被注入到其他人的 prompt，与「模型不可全信、确定性兜底」的设计前提相悖。
- **建议**：把分隔符类改为取反式（`[^0-9A-Za-z\u4e00-\u9fff]`），或先 `re.sub(r'(?<=\d)[^\w\u4e00-\u9fff]+(?=\d)', '', s)` 去掉数字间噪声再掩；零宽字符一并纳入。

### M-1 `fetch_url` 之外的 SSRF 面：沙箱 curl 的数字型 IP 字面量绕过内网判定【已复现】

- **位置**：`agentcore/workspace/runner.py`（curl 的 host 判定，编号沿用上轮 L20）
- **证据**：我起了一个只绑 127.0.0.1 的本地 HTTP 服务，用不同写法访问：

```
curl http://127.0.0.1:8899/   → 200 127.0.0.1
curl http://2130706433:8899/  → 200 127.0.0.1    ← 十进制
curl http://0177.0.0.1:8899/  → 200 127.0.0.1    ← 八进制
curl http://0x7f000001:8899/  → 200 127.0.0.1    ← 十六进制
```

而 `permitted()` 对 `https://2130706433/`、`0177.0.0.1`、`0x7f000001`、`127.1`、`2852039166`（= 169.254.169.254）**全部放行**，仅标准点分/冒号写法被拒。
- **影响**：`run_command`（仅管理员）可被模型用来访问本机服务与云元数据；35853ec 声称「与 web_fetch 的出网防护对称」，实际 web_fetch 走 DNS 解析故能拦住，两者并不对称（声称不符）。
- **建议**：对「类 IP 但非标准字面量」的 host（`^\d+$`、`^0[xX][0-9a-fA-F]+$`、`^[0-9.]+$`、`^0[0-7.]+$`）fail-closed 拒绝，或复用 `web_fetch` 的解析后判定。

### M-2 蒸馏的两条静默路径：per-message 截断丢尾 + 窗口停更【均已复现】

- **位置**：`agentcore/rag/distill.py`（`render_transcript` 的 `per_message_cap` 与水位线推进；`min_chars` 跳过分支）
- **证据**（两阶段：先建立水位线，再投新消息）：

```
② 1513 字的消息 → LLM 收到的 prompt 含开头、不含结尾「结尾的关键结论」；
   new_watermark=2（= 该消息 id）→ 尾部永久不再处理，无任何日志
③ batch=3、min_chars=200：3 条「嗯」占满窗口 → 连续 3 轮
   status=skipped / not enough content / watermark 恒为 1，LLM 零调用
   → 其后的长知识永不处理（永久停更）
```

- **影响**：上轮 H4 只修了 `total_cap` 截断，`per_message_cap` 与被极短消息占满的窗口这两条同类路径仍在：前者静默丢内容，后者让知识库**永久停止生长**（默认 `min_chars=200`、`batch=200`，群里 200 条寒暄即可触发）。
- **建议**：截断时把该消息的水位线退回到「已完整处理的位置」并记日志；`min_chars` 跳过改为「推进到本批最后一条**完整渲染**的消息」或按比例推进，避免窗口被永久占住。

### M-3 检索结果未纳入统一围栏（声称「所有注入点共用」不完整）【已复现】

- **位置**：`agentcore/skills/search.py`（`_fmt_item` / `_clip_results`）
- **证据**：源码中无 `fence_untrusted`/「不可信」标记（我按函数源码检索确认）；而 35853ec 的说明是「统一 fence」。
- **影响**：`search_web`/`search_multi` 的 title/snippet 来自第三方搜索结果，可被 SEO 内容写入指向模型的指令，绕过本轮刚统一的围栏体系。
- **建议**：对检索结果整体套 `fence_untrusted(..., source_desc="第三方搜索引擎返回、未经核实")`。

### M-4 备份完整性链路断在恢复端【已复现】

- **位置**：`agentcore/backup/db_backup.py`（`restore_database` / `_restore_jsonl`）与 `_mirror_backup`
- **证据**：`_harden`（0600）存在且被调用 ✓、`_write_checksum` 写 sidecar ✓、`verify_backup` 会读 sidecar ✓；但 `restore_database` 与 `_restore_jsonl` 源码中**没有**任何 `verify`/`sha256`/`sidecar` 调用 → 被篡改的备份照样灌库；`_mirror_backup` 只有一行 `shutil.copy2(src, dst)` → **异地副本不带 .sha256**，镜像侧篡改不可检测。
- **建议**：恢复前强制 `verify_backup`（校验失败即中止）；镜像时一并复制 sidecar。

### M-5 双实现契约分歧（内存 vs PG）【已复现】

- `kb_add_chunks`：PG 有同 source 去重，**内存实现没有**（源码比对）→ 同一输入返回 `len(chunks)` vs 去重数；用内存写的回归用例测不到 PG 行为，反之亦然。
- `list_facts`：内存按插入序 ASC，PG 按 `id DESC`（最新在前）→ `limit` 生效时返回不同集合。
- 私聊判定：内存靠解析会话键 `key.partition(":")[2] == "private"`，PG 用 `scope` 字段 → `user_id` 含冒号即漏判（当前平台 id 为数字，实际触发面小）。
- **建议**：内存 `kb_add_chunks` 补去重；统一 `list_facts` 排序；内存用显式 `session_scopes` 而非解析字符串。

### M-6 门控与声称（CI 盲区）【已复现】

- **32 个跳过全部来自 `TEST_DATABASE_URL` 门控**（`test_pg_store.py` 21 + `test_persistence.py` 11），本范围新增的 PG 侧回归（store 加固、restore/replay、pg_dump 往返）在 CI **零覆盖**；`FIX-e86fba0..8cfbf6d.md` 声称「PG 门控 58/58 在 scratch 库实跑」在本环境无法复核。
- **CI 只测 3.12**，而本轮刚把 `requires-python` 抬到 `>=3.11` → 声明的下限从不验证（本机也只有 3.12）。
- **建议**：CI 加 `postgres:16`+pgvector service 并导出 `TEST_DATABASE_URL`（conftest 已有防误连生产库断言）；python-version 用 `["3.11","3.12"]` 矩阵。

### M-7 `BASE 人名/昵称防护在默认配置下几乎不生效`【已复现】

- **位置**：`agentcore/rag/distill.py`（词表加载，路径按 CWD 解析）+ `agentcore/rag/sanitize.py`
- **证据**：默认词表大小 **0**；实测 `@张三 提到的方案`、`张三说这个方案不行`、`李四住在上海浦东新区`、`她昨天说的思路` **全部原样入库**；只有「X 的+私人物件」句式命中（`王小明的服务器` 正确丢弃）。
- **缓解**：`sanitize.py` 开头已**如实声明**该边界（「不能声称保证无 PII」），故定为 M 而非 H；但 `data/privacy/names.txt` 相对 CWD 意味着按 systemd/docker 启动时运维放进去的词表会静默失效。
- **建议**：词表路径按模块根解析；对 `@\S+` 与 `(?:[\u4e00-\u9fff]{2,4})(?:说|觉得|住在|要求|提到)` 增加确定性掩码。

### L 级（压缩列出，均带证据）

| 编号 | 问题 | 证据 |
|---|---|---|
| L-1 | `fence_untrusted` 不转义围栏标记，content 可自带「XX结束」伪造边界 | 已复现 |
| L-2 | 沙箱 HOME 预检失败时每次调用 `mkdtemp` 且不回收（/tmp 目录无界增长） | 已复现 |
| L-3 | cron 提醒投递失败把 `next_run` 改成 `now+retry_delay`，丢周期语义、可无限重试 | 已复现 |
| L-4 | `proc_detail(top="abc")` 抛 ValueError，落到通用错误提示 | 已复现 |
| L-5 | 归档 `latest_message_id` 复用带提前 break 的迭代器 → 单文件读满即停，最大 id 被截断 | 已复现 |
| L-6 | 归档读取是同步 I/O 且直接在协程内调用（最多 20 万行）→ 阻塞事件循环 | 推演 |
| L-7 | 首跑水位线用「含私聊的全局 MAX(id)」，比可见口径多跳过一段公共历史（设计已在 README:167 与代码注释披露） | 已复现 |
| L-8 | `restore_database` 的 `restored` 字段类型漂移（`.sql.gz` 是 bool、`.jsonl.gz` 是 dict） | 源码 |
| L-9 | 空身份（`('',None)`）不缓存 → 该 session 每条消息多一次 DB 往返 | 已复现 |
| L-10 | `.env.example` 缺 `AGENT_SKILLS_DIR`、代码读取的 `LLM_EMBEDDING_MODEL`；README 工具表漏 3 个 prompt 技能 | 已复现 |
| L-11 | 本地 venv 的 ruff 0.16.6 与 pin 的 0.9.6 漂移（pin 只对新装生效，不会自愈既有环境） | 已复现 |
| L-12 | `BACKLOG.md:70` 仍写「schedules 表空置」，与提醒已落库矛盾（M12 漏项） | 已复现 |
| L-13 | 本仓库评审报告摘要把「忽略之前的指令」的拦截归因于 `scrub_pii`，实际在 `rejection_reason` | 已复现 |

---

## 2. 被证伪的发现

| 子代理结论 | 我的复核 | 结论 |
|---|---|---|
| **H NEW-1**：`save_fact` 单语句把 `AND session_id IS NOT DISTINCT FROM $2::int` 写丢，跨会话同内容事实被误丢 | HEAD `agentcore/memory/store.py:919` **存在**该谓词；`git show bd3a0f5:...` 显示引入时即存在（父提交是另一种写法 `$3::int ... LIMIT 1`） | **假阳性**（读错版本/误读 diff） |
| 「X 的+私人物件」启发**误杀 9/12 常规技术表达** | 我实测 12 条常规表达（服务器的 CPU、Python 的 GIL、Docker 的镜像层、GPT 的上下文窗口…）**0 误杀**，且 `王小明的服务器` 正确丢弃 | **假阳性** |
| M 级「facts 去重原子」 | 单语句确实缩短了竞态窗口，但 `facts` 表无唯一索引，Read Committed 下并发仍可双插（§1 M-5 关联） | 降为推演级 M（非 H） |
| 「首跑水位线导致公共历史永久跳过」 | 首跑跳过存量历史是**有意设计**并在 README:167 与 `distill.py` 注释/log 中披露；用全局 MAX(id) 只是多跳一点 | 降为 L-7 |

---

## 3. 测试与文档状况

- **实跑**：`.venv/bin/python -m pytest tests/ -q` → **549 passed, 32 skipped**（581 收集）；`ruff check agentcore plugins tests bot.py scripts` → All checks passed。
- **CI 盲区**：32 个跳过全部因 `TEST_DATABASE_URL` 未设（`test_pg_store.py` 21 + `test_persistence.py` 11）；`.github/workflows/ci.yml` 无 PG service，故本范围改动最大的 PG 侧（store 加固、restore/replay、pg_dump）在 CI 无回归保护。CI 仅 3.12 单版本。
- **文档一致性**：py3.11 四处一致（pyproject/README/ruff target/CI）✓；ruff 0.9.6 三处一致 ✓；CI lint 已含 `scripts/` ✓；`docs/agent-demo-memory-architecture.md` 的已知边界已更新 ✓；**`BACKLOG.md` 漏改**（L-12）。
- **正常行为确认**：私聊默认不进公共蒸馏已生效（我最初用私聊会话测时 `digest` 直接返回 `no new messages`，说明可见性过滤工作正常，且有 README 说明与 `AGENT_KB_DISTILL_PRIVATE` 开关）。

---

## 4. 已验证为「无问题」的关键项

- **上轮 H1（git 配置注入）**：`-c key=a.b.c`、带点小节名、`--config-env`、别名、`--exec-path`、`-C`、`--git-dir`、`--work-tree`、白名单外子命令等，子代理穷举后结论为已封死；我复核了 `permitted()` 的分层与 `_GIT_HARDENING` 存在性，未发现反例。
- **calc 幂炸弹上界**（M8）：静态上限（常量≥1e9、字面指数>1000、嵌套幂、Pow>3）生效且不误伤 `sqrt(16)+2**3` 等常规表达式（我在真实启动路径下逐个调用工具时验证）。
- **M9 披露**：`web_fetch` docstring 与 README 已如实写明 DNS rebinding 残留，措辞与 `media.py` 一致。
- **M5 权限**：`_harden` 确实存在并被调用（0600），Windows 下静默跳过有注释说明。
- **M4（user_state 恢复）**：`ON CONFLICT ("user_id")` 已落地（子代理用假驱动实跑验证）。
- **工程一致性**：ruff pin、py3.11 声明、CI 路径集合、`review/` 目录规范执行均与声称相符。

---

## 5. 与在库报告的衔接复核（上一轮 H/M 修复状态）

| 上轮 | 状态 | 依据 |
|---|---|---|
| H1 git 配置注入 | ✅ 已封死 | §4 |
| H2 脱敏穿透 | ⚠️ **部分** | H-A：分隔符类仍缺 12+ 种写法 |
| H3 人名防护 | ⚠️ **部分** | M-7：词表为空 + 句式覆盖窄（已如实声明边界） |
| H4 水位线越过被截消息 | ⚠️ **部分** | M-2：`total_cap` 已修，`per_message_cap` 与窗口停更仍在 |
| M1 私聊进公共库 | ✅ 已修（默认排除 + 开关 + 文档） | §3 正常行为确认 |
| M2 注入可同义改写绕过 | ⚠️ 部分 | 归一化+共现规则已加；英文同义改写/伪造说话人大小写变体仍可穿透（子代理复现，我未重复） |
| M3 SQL 标识符注入 | ✅ 已修（白名单） | 子代理假驱动复现拦截 |
| M4 user_state 恢复失败 | ✅ 已修 | §4 |
| M5 备份权限 0644 | ⚠️ 部分 | `_harden` 覆盖产物；`.part`/目录与镜像 sidecar 见 M-4 |
| M6 完整性/原子性 | ⚠️ 部分 | 原子写+sidecar+全量 verify 已有；镜像与恢复端未接（M-4） |
| M7 覆盖写无退路 | ✅ 已修（pre-restore 快照 + 目标库校验） | 子代理复现；同日二次恢复覆盖快照为 L（子代理） |
| M8 calc 幂炸弹 | ✅ 已修 | §4 |
| M9 TOCTOU 未披露 | ✅ 已修（如实披露） | §4 |
| M10 py3.11 | ✅ 已修（但 CI 未验证 3.11，M-6） | §3 |
| M11 归档身份缓存 | ✅ 已修（只缓存成功、可自愈、有上限） | 子代理复现；空身份不缓存为 L-9 |
| M12 文档与实现矛盾 | ⚠️ 部分 | 架构文档已改，`BACKLOG.md` 漏改（L-12） |

---

## 6. 修复优先级建议

1. **P0 — H-A 分隔符脱敏**：一行正则取反式即可封住 18 种写法；顺手把零宽字符纳入。加对抗回归（把本报告的 22 种分隔符表搬进测试）。
2. **P1 — 沙箱 curl 数字型 IP（M-1）**：fail-closed 拒绝类 IP 非标准字面量；或直接改用 `web_fetch` 的解析后判定，让「对称」的声称成立。
3. **P1 — 蒸馏两条静默路径（M-2）**：截断处退水位线并记日志；`min_chars` 跳过改为有限推进，消除永久停更。
4. **P2 — 完整性链路收口（M-4）**：`restore_database` 前强制 `verify_backup`；镜像复制 sidecar。
5. **P2 — 统一围栏补检索（M-3）**：`search_web`/`search_multi` 输出套 `fence_untrusted`，让「所有注入点共用」的声称成立。
6. **P3 — 双实现契约与 CI（M-5/M-6）**：内存 `kb_add_chunks` 去重、`list_facts` 排序统一、私聊判定不解析字符串；CI 加 PG service + 3.11/3.12 矩阵。
7. **P4 — 文档与低危清理（L-10/L-12/L-13）**：`BACKLOG.md` schedules 条目、`.env.example` 环境变量、报告摘要措辞。

> 修复阶段按规范 §2.5 另出 `review/FIX-8cfbf6d..a604023.md`，逐条记录改法、回归测试与遗留项；每条修复必须带回归测试，对抗样例直接从本报告复制。
