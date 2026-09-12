# agent-demo 近期 Commit 评审报告（规范审查）

**评审范围**：`a604023..679c9b3`（32 个 commit；按**当前代码实况**审查，非仅看 diff）
**评审日期**：2026-09-12
**评审方式**：4 条并行子代理分线取证（安全边界 / 并发资源与生命周期 / 正确性与契约 / 测试有效性与工程规范）+ 主代理逐条复核
**工作区状态**：`git status --porcelain` 为空，HEAD = `679c9b3`
**基线实况**：`794 passed / 41 skipped`（跳过 = 32 PG 门控 + 9 perf 门控）、`ruff check`（CI 范围）全绿

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | **沙箱边界被两条白名单命令直接架空**：`zip -T -TT/--unzip-command` 任意命令执行；`curl` 无 scheme 参数绕过内网判定实读 loopback。沙箱存在的意义就是约束"LLM 即攻击者"，这两条使其失效 |
| **可用性/数据** | `calc` 的 `pow()` 算力炸弹可阻塞整个事件循环 7.19s / 473MB（**public 权限**，任何用户可打）；停机钩子逆序导致**每次重启丢一批消息**并把回复降级为 `[echo] 用户原话` |
| **契约漂移** | 内存实现 vs PG 实现：`list_facts` 排序相反、`kb_add_chunks` 内存完全不去重；两套实现无共享契约测试且 PG 侧在 CI **永久跳过** |
| **CI 盲区** | PG 契约测试 21 条 + `test_pg_store` 全文在 CI 零执行（无 postgres service）；`ruff format` 完全未门禁；`perf_baseline.py` 未接入且默认阈值在未改动 HEAD 上有实测假阳（3 次中 1 次 exit 1） |
| **测试有效性** | 安全规则存在**零覆盖**用例（`acl` 私聊拒绝分支：变异后 794 passed）；另有 10 余条假通过/名不副实用例（`pytest.raises(ValueError)` 被 `int()` 自身满足、恒真断言、参数被忽略仍通过） |
| **工程一致性** | `CONTRIBUTING.md` 声称 Python 3.10+（实为 `>=3.11`）；`BACKLOG.md` 行号系统性过期（行为描述为真）；`AGENTS.md` **不存在** |

---

## 1. High（5 条，全部经主代理独立复现）

| # | 位置 | 问题 | 主代理复核证据 |
|---|---|---|---|
| **H1** | `agentcore/workspace/runner.py` `permitted()` zip 分支 | `zip` 参数零校验；Info-ZIP 的 `-T` 会执行测试命令，`-TT cmd` / `--unzip-command=cmd` 可替换 unzip 执行任意命令。`_denied_for_shell` 不拦 `touch x`/`sh -c ...`，路径检查也只拦含分隔符参数 | 变体 A/B 打出标记文件成功（`--unzip-command=touch <marker>`、`-T -TT "touch <marker>"` 均 `标记存在=True`）；仅"分离参数+绝对路径"形态被拦 |
| **H2** | `runner.py` `permitted()` curl 分支 | 只把含 `://` 的参数当 URL 校验；裸 `host:port/path` 被跳过，curl 视其为 `http://`。https 诱饵 + 裸内网地址即可 SSRF（明文 http） | 起本地 http.server：`permitted(...)= (True,'')` 且沙箱输出含 `SECRET-LOOPBACK-CONTENT`；对照纯 `https://127.0.0.1/` 被拒 |
| **H3** | `agentcore/skills/basic_tools.py` `_reject_pow_bomb` | 静态守卫只查 `ast.BinOp(Pow)`，漏 `pow(...)` 调用（`_FUNCS` 放行 `pow`）；`asyncio.wait_for(to_thread(...))` **无法取消线程**，GIL 被 `long_pow` 占住 | `2**999999999` 被拦 / `pow(2,999999999)` 通过；实跑 `evaluate()` 返回"超时"但耗时 **7.19s**、峰值 RSS **473MB** |
| **H4** | `agentcore/skills/file_sender.py` `is_uncertain_send_error` | 判据只认 `TimeoutError` 与异常文本含 "timeout"；`httpx.ReadTimeout` 既非 `TimeoutError` 子类、`str(exc)` 又为空 → 返回 False → NapCat 超时被当"未送达" → 降级重发 → 用户收到两遍 | `isinstance(httpx.ReadTimeout(""), TimeoutException)=True`、`isinstance(..., TimeoutError)=False`、`str==""`、函数返回 **False** |
| **H5** | `plugins/qq_agent_adapter/__init__.py` `_shutdown_agent` + `bot.py` 停机钩子 | NoneBot 停机钩子 **reversed** 执行 → 插件先 `memory.aclose()`（`pool=None`），`bot.py` 的 `flush_all()` 后跑 → 首次 DB 调用 `AttributeError` → 被 `matcher` 兜底吞成 `[echo] 用户原话`，消息与回复都不落库/归档（每次重启必现） | 真实 `Lifespan` 实验：注册序 `bot.flush` → `plugin.aclose`，实际执行序 **`plugin:aclose -> bot.py:flush`**；`_lifespan.py:80` = `reversed(...)`；`matcher.py:212` 有 `[echo]` 降级 |

---

## 2. Medium（17 条，均有子代理实证；标注为需与修复同批验证）

**安全/注入**

| 位置 | 问题 | 证据 |
|---|---|---|
| `agentcore/rag/retriever.py` `format_block()` | 自带第二套围栏且不打散 lookalike（`safety.py` 已修，这里漏修）→ KB 内容可提前闭合围栏，注入落到 system prompt 层；公共库全局共享 = 跨用户注入 | `format_block` 输出围栏尾 **2 次**、恶意句落在围栏外；`fence_untrusted` 对照为 1 次（主代理复现） |
| `agentcore/loop/engine.py` `_build_system_prompt()` | 用户来源 `long_term_facts` 未围栏直接进 system prompt（持久化、跨轮次） | 注入句 `in system prompt: True`，`'-----' in prompt: False` |
| `agentcore/safety.py` + `skills/web_fetch.py` | 缺 `100.64.0.0/10`（RFC6598，Tailscale 默认段）→ 判为公网，可直连 tailnet | `ip_literal_is_safe('100.64.0.1')=True`（主代理复现）；`192.168.1.1`/`169.254.169.254` 均 False |

**存储契约**

| 位置 | 问题 | 证据 |
|---|---|---|
| `store.py` `list_facts` 内存 vs PG | 内存取最旧 `limit` 条，PG `ORDER BY id DESC` 取最新 → `engine` 用它做去重预筛，内存后端看不到最近事实 | 内存 `['fact-A-first','fact-B']` vs PG `['newest','middle']`；两侧测试各自固化，漂移零覆盖 |
| `store.py` `kb_add_chunks` 内存 | 完全不去重（违背 ABC 文档与 PG 语义、返回写入条数失真） | 内存 `["dup","dup","uniq"] -> wrote 3`（PG 为 2）；去重用例只在 `test_pg_store` |
| `store.py` `messages_after` / `resolve_session` | `group_id="private"` 与私聊同键；未注册会话的消息被内存当群消息（PG JOIN 排除） | `resolve_session('u2','private')==resolve_session('u2',None)` |
| `store.py` 负 `limit`/`top_k` | 内存按切片返回"除最后 N 条外全部"，PG 直接报错 | 内存 `limit=-1` 返回 2 条；PG `LIMIT must not be negative` |

**备份/归档/蒸馏**

| 位置 | 问题 | 证据 |
|---|---|---|
| `db_backup.py` `_mirror_backup` | 镜像只 `copy2` 数据文件、不复制 `.sha256` → 异地副本永久 `checksum="missing"`，无法识别"gzip 合法但内容被改" | 同款篡改下 src `mismatch` / mirror `ok` |
| `db_backup.py` `find_pg_dump` + `backup_database` | `docker exec` 探测的 `TimeoutExpired` 在 try 之外 → `strategy="auto"` 不回退 JSONL | 桩化后直接抛 `TimeoutExpired` |
| `rag/distill.py` 单条截断 | 超 500 字被静默截断且水位线照推进 → 该消息剩余内容永久不再蒸馏（与"截断不静默丢失"冲突） | `last_included_id=2` 但 `chars=511`，无日志 |
| `rag/retriever.py` `format_block` 循环 | 用 `break` 而非 `continue`：首个超 2400 字命中 → 返回空串，**整批检索结果被丢弃** | `format_block([{'chunk':'x'*3000}]) -> ''` |
| `archive_restore.py` + `memory/archive.py` | 恢复走 `iter_records` 默认 20 万**读行**上限且无参数关闭 → 超限静默截断，dry-run 统计同样偏小 | `iter_records(limit=4)` 读满 4 行即停 |
| `skills/utility_skills.py` `"bit": "B"` | 位/字节映射成 1:1，换算错 8 倍 | `convert(1,'bit','B') -> '1 bit = 1 B'` |

**并发/资源**

| 位置 | 问题 | 证据 |
|---|---|---|
| `db_backup.py` `prune_backups` | async 里同步对每份备份全量 gunzip + 逐行 `json.loads` + 整份 SHA256 → 事件循环停摆 2.16s/41MB（≈53ms/MB） | `wall=2.16s, loop max stall=2.17s` |
| `db_backup.py` `_backup_jsonl` | `SELECT *` 无 LIMIT 整表进内存 + dumps/gzip 同步 → 10 万行停摆 4.27s（`to_thread` 后 stall 0.03s） | `stall=4.28s` vs `0.03s` |
| `pipeline.py` `RecentImageBuffer` | 缓存 base64 data URI：默认配置下单会话可达 ~8MB 原始 → 32 会话 ≈ **350MB 常驻**（TTL 180s），小内存 VPS 有 OOM 风险；perf 基线用玩具串故不报警 | 32×1×5MB → RSS +230MB；理论最坏 447MB |
| `matcher` → `debounce` → `merge_parts` | 合并顺序 = payload **构建完成**顺序（含最长 30s 的下载/get_msg）→ 同会话两条消息可倒序 | 到达序 msg1,msg2 → 合并文本 `'msg2\nmsg1'` |
| `debounce.py` `parts` | 单窗口 parts 无条数/字数上限 → 刷屏时整批拼成一次请求（20k 条 → 400 万字符 ≈ 100 万 token） | 实测 `parts=20000, chars=4019999` |
| `debounce.py` + `matcher` | 全进程无并发闸门（无 Semaphore）→ 200 个 key 同时到期即 200 路并发 LLM | `peak concurrent runners = 200` |

---

## 3. Low（摘要，19 条）

- 校验承诺削弱：`ops_skills` 服务名允许首字符 `-`（`--version` 通过）；`git branch <name>` 实际写 `.git`（"只读"不成立）
- 健壮性：`web_fetch.url_rejection_reason` 对畸形 URL 抛未捕获 `ValueError`；`_sandbox_home()` 预检失败目录从不清理（`/tmp/agent-demo-sandbox-home-*` 已 1286 个）
- 一致性差一：`archive.prune` 实留 `keep_days+1` 天；`sanitize` 对 ≥25 位数字漏尾；`persona_utils` 的中文别名 `\b` 不生效（`/人格yun` 不被剥离）；`utility_skills` 单位大小写给误导错误；`matcher.trigger_rule` 缺返回注解
- 死代码/未跑路径：`_reconstruct_content_from_memory` 恒返回 `""`；`LLMClient.embeddings` 无调用方；`_napcat_upload_private_file`、`_download_for_su`、`_display_key` 在测试中从未执行
- 文档漂移：`CONTRIBUTING` 写 3.10+（实为 3.11+）；`README` 称软上限超时"放行并打 WARNING"（实际无 warning）；`BACKLOG` 行号系统性过期；`.env.example` 缺 `AGENT_SKILLS_DIR`
- 性能/资源小项：`budget.record` 每次响应同步写整月账本（p50 0.169ms）；`file_sender` 同步 `write_text` 且所有会话共用固定 `data/cache/reply.md`；`InMemoryMemoryStore` 永不裁剪（生产用 PG）；`debounce._key_locks` 不淘汰（5000 key 实测）；`flush_all` 顺序 await 拖长停机

---

## 4. 测试有效性专项（H 级零覆盖 + 假通过）

**H：`acl` 私聊拒绝分支零覆盖**——`tests/test_acl.py` 的 `FakePrivateEvent` 带 `group_id=None`，`hasattr(event,"group_id")` 为真 → 走群分支；把私聊 `return False` 改成 `return True` 后**全量 794 passed**。

**假通过/名不副实（变异后仍全绿）**：

| 用例 | 问题 |
|---|---|
| `test_file_sender.py` `_safe_user_id` | 只断言 `pytest.raises(ValueError)`，而 `int("abc")` 自身即抛 → 删掉 `isdigit` 守卫仍通过 |
| `test_debounce.py::test_runner_exception_does_not_break` | 两条断言恒真（`pending_keys()` 执行前已 pop；`calls` 在 raise 前未 append） |
| `test_pipeline.py` placeholder/fallback 3 条 | 只断言 `text` 非空，兜底降级对任何输入都成立 → `_build` 首行 `raise` 仍通过 |
| `test_outbound.py:743` | `assert "超过软上限" not in caplog.text` 恒真（该字符串全仓只存在于这一行） |
| `test_outbound.py::test_too_many_nodes_falls_back` | 名称声称验证重打包，禁用 `_repack_chunks` 仍通过 |
| `test_outbound.py:277` nickname | 测试值与默认值同为 `"助手"`，参数被忽略无从发现 |
| `test_pipeline.py::test_get_bot_prefers_self_id` | 函数体只有 `assert callable(...)`（恒真）；`matcher._send_reply` 丢掉 `self_id` 后全量仍绿 → 多账号防串号未验证 |
| `test_debounce.py::test_zero_delay_runs_immediately` | 删掉 `delay<=0` 快路径仍通过（不验证"立即"） |

**方法学纠正（重要）**：对 `permissions.is_allowed` 做单文件变异时看似"零覆盖"（12 passed），跑全量则 2 failed —— **单文件变异会误报，只有全量变异可信**；上表中标注"需全量复核"的项已按此口径处理。

**CI 结构性盲区**：`tests/test_pg_store.py` 21/21 在 CI 永久跳过（无 postgres service、不导出 `TEST_DATABASE_URL`）→ `PgMemoryStore`（约 470 行）契约在 CI 从不执行，且两套实现无共享契约测试；`ruff format` 无门禁；`perf_baseline.py` 未接入任何流水线且默认阈值有实测假阳（同指标 run-to-run 波动可达 ~2x）。

**工具版本漂移**：`pyproject` pin `ruff==0.9.6`（与 pre-commit 对齐），实际 `.venv` 为 **0.16.6** —— 本地 `ruff format --check` 判定与 CI 不一致（0.9.6 下 103 files formatted；0.16.6 下 1 file 待格式化，且 `ruff format .` 会改写 `review/*.md`）。

---

## 5. 子代理证伪/排除项（不按这些改码）

- `finish()` 抛 `FinishedException` 被 `except Exception` 吞：AST 全量扫描 `plugins/**` 仅 2 处 finish 在 try 内，**均有显式 re-raise**；`FinishedException` MRO 确为 `Exception` 子类，处理正确
- `unzip` 的 `../` 条目：Info-ZIP 自身 skip，落点仍在 `-d` 目录内，**无 zip-slip**
- `is_allowed_image_url` 的 11 种构造（`qpic.cn.evil.com`、`qpic.cn@evil.com`、`%2e`、尾点、制表符…）：与 `httpx.URL().host` 判定一致，**无白名单绕过**
- 权限面 fail-closed：`registry.execute` 二次校验权限；engine 会 `pop` 掉 LLM 传入的 `user_id/group_id`
- `OutboundThrottle._evict_idle` 锁淘汰竞态：复现后两次同 target 发送仍相隔 0.300s（`_record` 重建 bucket），属 docstring 已披露取舍
- `_decode_base64_file` 5MB 解码仅 1.0ms，不构成阻塞
- budget 跨日/跨月重置、`warned` 持久化：桩化 `date` 实测正确

---

## 6. unknown（缺证据，不下结论）

1. `PgMemoryStore` 的 embedding 为空/短列表时 `'[]'::vector` 抛 `DataError`（cast 必失败已实测），但生产中 `embed_many` 是否会返回短列表未验证
2. `kb_last_digest_watermark` 内存取 max / PG 取 id 最大：合成数据已复现差异，但需"水位线非单调"才有真实影响，未找到线上触发路径
3. 线上实际工作目录决定 `data/privacy/names.txt`、`data/skills`、`data/cache`、`AGENT_BUDGET_DIR` 等相对路径是否生效——未验证
4. `_backup_jsonl` 整表 `fetch` 的峰值内存（需真实大表）；`archive.append` 用默认线程池与图片下载混用的排队延迟
5. 提醒投递失败 120s 无限重试、无最大次数的长期离线行为
6. `perf_baseline` 假阳是否纯属本机 4 核噪声（未在多核/不同负载机器复测）
7. 运行中（非停机）DB 短抖时同样走 `[echo]` 降级，影响面未评估

**合规披露**：并发线子代理为验证 `aclose()` 后失败路径，曾向线上 `sessions` 插入一条 `user_id='__audit_probe__'` 探针行并立即删除；主代理复查残留 **0 行**（净零成立），但属对"只读"要求的偏离，如实记录。

---

## 7. 修复优先级建议（按 ROI 排序）

1. **H1/H2 沙箱边界**：zip 拒绝 `-T/-TT/--unzip-command` 及含 `=` 的测试类参数；curl 改为"所有非 flag 参数都当 URL 校验"并禁 `-T/--upload-file`、`-x/--proxy`、`-k`
2. **H3 算力炸弹**：`pow()` 调用纳入静态守卫（限指数/位数），或直接移除 `pow` 支持
3. **H5 停机顺序**：让 flush 在 `aclose` 之前执行（调整注册顺序或把池关闭移到 flush 之后）
4. **H4 重复投递**：判据加 `isinstance(err, httpx.TimeoutException)`
5. **M 安全**：`retriever.format_block` 改用 `safety.fence_untrusted`；facts 进 system prompt 前围栏/清洗；补 `100.64.0.0/10`
6. **M 契约**：`list_facts` 统一语义（更新 ABC 文档 + 双实现 + 共享契约测试）；内存 `kb_add_chunks` 补去重；`format_block` `break`→`continue`
7. **M 资源/并发**：`prune`/`_backup_jsonl` 移入 `to_thread`；`recent_images` 加字节预算或改存文件路径；`debounce.parts` 加上限；补全局并发闸门；合并顺序改按消息 `message_id` 排序
8. **测试补强**：`acl` 私聊拒绝真实覆盖；把上述假通过用例改成有牙断言；`test_pg_store` 接入 CI（加 postgres service）
9. **文档/工程**：`CONTRIBUTING` 版本改 3.11+、`README` 软上限描述、`BACKLOG` 数字与行号刷新、`.env.example` 补 `AGENT_SKILLS_DIR`、`ruff` 版本对齐、`ruff format` 纳入 CI、补 `AGENTS.md` 交接文档
