# agent-demo 近期 Commit 评审报告
**评审范围**：c472e56..733f57e（13 个 commit）
**评审日期**：2026-09-14
**评审方式**：4 个并行只读子代理分线审查（线1 记忆与存储 / 线2 RAG 与 KB 摄取 / 线3 QQ 适配层与沙箱 / 线4 工程化资产与测试归并）+ 主代理逐条实证（实跑双套件与 ruff、复现 6 项、证伪与声称核对、全新空 scratch 库验证）
**工作区状态**：评审全程 `git status` 干净，除本报告与索引行外未修改任何文件

## 0. 结论摘要

| 维度 | 结论 |
|---|---|
| 最高风险 | **H1** A2 滚动摘要水位超前于实际喂给摘要器的内容 → 超长会话的早期上下文**静默永久丢失**（A2 默认开启，已亲自复现） |
| 记忆与存储 | A2 主体设计合理（双实现契约、围栏注入、失败隔离均在位）；但裁剪-摘要链有三处缺陷：H1、M1、M2 |
| RAG / KB 摄取 | 大文件自动切块主语义正确（先写新再删旧、32MB 响亮拒绝、sanitize 管道未绕过）；收敛性/配置面问题集中：M4–M9 |
| 适配层与沙箱 | 帮助图片、debounce 守卫、zip `=` 收窄均未发现安全回退；**M3** 停机 flush deadline 被真实 `flush_all` 的 shield 设计完全击败（声称失真 + 回归测试假通过） |
| 工程化 | 测试归并完整性经机器逐字比对**通过**（48 条全找回、唯一差异是加严）；资产许可（M10）、依赖（M11）、记账失真（M13/M14） |
| 测试 | HEAD 实跑：默认 **910 passed / 42 skipped**；全新空 scratch 库 **943 passed / 9 skipped**（等价复现 CI #43 场景，DDL 修复确认有效）；`ruff check` 全绿 |
| 声称核对 | 13 个 commit 中 4 处失真（M3 / M8 / M13 / M14 关联），其余（d303a5c、0e49904、d60c3b2、9e93472、ba0b33f 主体、733f57e 主体）相符 |
| 门禁（§8.3） | 无环境盲区 H，**不阻断**；未验证面 (a)–(e) 见 §3，上一轮未验证面 1 项闭环、1 项恶化（M3）、1 项仍开放 |

## 1. 已实证的问题

### H1【已复现·主代理】A2 摘要只喂 `lines[-200:]`，水位却推进覆盖全部 backlog——超出的最旧消息静默永久丢失
- **位置**：`agentcore/loop/engine.py:349`（`"\n".join(lines[-200:])`）、`engine.py:390-391`（`save_session_summary(..., int(to_summarize[-1]["id"]))`）
- **证据**（真实 engine + InMemoryMemoryStore 端到端）：预置 220 条用户消息、`history_token_budget=40` 触发裁剪，摘要器 prompt 中「【新对话】」实收 200 行，落库水位 `upto_id=218` 覆盖全部已取消息；**18 条最旧消息从未进入摘要器却被标记为已摘要**。水位之下永不再取（`get_session_messages_between` 以 `after_id=wm` 续取），缺口无任何日志与自愈路径。
- **影响**：`summary_enabled` 默认开启（config.yaml）。单会话积压超过过滤后 200 行（长会话/离线积压）即触发；与 commit ba0b33f 声称的 `get_session_messages_between`「开区间补漏，防离线积压……摘要缺口」直接矛盾——补漏取回了数据，在喂 LLM 前又丢掉。原始消息仍在 sessions/归档中（可重建），属**静默损坏长期上下文**而非不可恢复的数据销毁；按 §3「正确性错误导致静默损坏」及本仓 H4（蒸馏水位线丢失）先例记 H。
- **建议**：水位只推进到实际入参最后一条 id；或按批摘要（每批 ≤200 行、逐批推进）；或删掉 `[-200:]` 依赖 `get_session_messages_between` 的 limit=400 分批续推（契约测试已锁该语义）。

### M1【已复现·主代理】`_trim_history_to_budget` 切点可落在 assistant(tool_calls) 与 tool 响应之间，主请求以孤儿 `tool` 消息开头 → 上游 400
- **位置**：`agentcore/loop/engine.py:423-429`（先 `_sanitize_history` 后 trim）、`engine.py:136-155`（trim 无工具对感知）、`engine.py:451`（trim 结果直接 `extend`，无二次清洗）
- **证据**：构造 8 组填充 + 1 组工具调用对 + 2 条新消息，`history_token_budget=32` 时主请求历史首条为 `role='tool', tool_call_id='c1'`（其 assistant 消息已被裁掉）。`_sanitize_history` docstring 自述该形状会被 DeepSeek 等接口 400，但 sanitize 在 trim **之前**执行。既有测试 `test_orphan_tool_history_does_not_break_request` 只覆盖「窗口起点在工具对中间」，未覆盖「trim 之后」。
- **影响**：工具密集 + 超预算会话中该轮回复失败（烧一个用户轮次），随窗口滑动自愈；与 H1 同路径（A2 默认开启）。
- **建议**：trim 后对 kept 再跑一次 `_sanitize_history`（幂等），或切点回退跳过 assistant.tool_calls 消息。

### M2【已复现·主代理】`id`/`tool_calls: None`/`tool_call_id: None` 泄漏进发给 LLM 的 messages，打破存储层自家文档契约
- **位置**：`agentcore/memory/store.py:392-393`（ABC docstring「id 不得进入最终发给 LLM 的 messages」）vs `store.py:651-666`（`get_history_window` 无条件带 `id`/None 键）；`engine.py:451` 无投影
- **证据**：实跑捕获的 user 历史消息键集为 `['content', 'id', 'role', 'tool_call_id', 'tool_calls']`；旧路径 `get_history` 刻意剥掉，契约测试 `test_history_contract` 锁定 `{"role","content"}`——A2 路径使该不变量回退。
- **影响**：多数供应商忽略多余字段；严格网关（部分 Azure/自建 strict 网关）可能整请求 400；至少是文档化契约被自己打破。
- **建议**：`extend` 前投影（只留 role/content + 非空 tool_calls/tool_call_id），或在 trim 返回前统一清洗。

### M3【已复现·主代理】停机 flush deadline 被真实 `flush_all` 的 `asyncio.shield` 击败——`AGENT_SHUTDOWN_FLUSH_TIMEOUT` 完全无效，4e6e657/FIX 声称失真，回归测试假通过
- **位置**：`plugins/qq_agent_adapter/lifecycle.py:79-90`（`wait_for(flush_all(), timeout)`）vs `plugins/qq_agent_adapter/debounce.py:138-148`（每窗口 `asyncio.shield` + `except CancelledError: continue`，M5 特意设计的防取消）
- **证据**：用**真实 Debouncer** 复现——6 个会话窗口 × 0.4s 串行 runner，`wait_for(..., timeout=0.5)` 实际耗时 2.10s、6/6 窗口全部执行完、`TimeoutError` 从未抛出（取消被 flush_all 吞掉后继续跑完剩余窗口，`wait_for` 等其自然结束）。lifecycle 的 `except TimeoutError`（含「放弃剩余窗口」ERROR 日志）实际不可达。
- **影响**：L3 要解决的 systemd SIGKILL 全丢场景原样存在；`review/FIX-679c9b3..c472e56.md`「到点放弃剩余窗口（记 ERROR）」与 `.env.example`「确定性 deadline」的声称不成立。回归测试（`tests/test_lifecycle.py`）用裸 `asyncio.sleep` 的 FakeDebouncer——可被 cancel，与真实 shield 实现行为相悖，**测试假通过**（变异只复核了 lifecycle 侧，未验集成）。
- **建议**：给 `Debouncer.flush_all(deadline)` 原生传参（循环内逐窗口查剩余时间，超时停止排新窗并返回已处理/放弃计数）；修后用真实 Debouncer 补集成回归。

### M4【推演·机制经主代理读码核实】源文件「变小」后切块替换留下永久孤儿块来源，`--prune` 也清不掉
- **位置**：`scripts/ingest_kb_samples.py:170-184`（superseded 只按**新计划**的 unit 名匹配）、`scripts/ingest_kb_samples.py:48-69`（`_prune` 以 `Path(loc).is_file()` 判僵尸）、`agentcore/rag/ingest.py:96-126`（`_write_groups` 不清理目录中超出新份数的旧块）
- **影响**：文件编辑后块数 N→N-1（内容略微缩短即常见）时，旧 `big.md/00N.md` 来源不在新计划名里、也不是 parent 名，替换循环看不见 → 库中永久残留一份旧内容参与检索；磁盘旧块文件未删 → `_prune` 判活通过、连 `--prune` 都清不掉。README 宣称 `--prune` 可清理「含切块产物」，实际前提是手动删 `<stem>/` 子目录，无提示。
- **建议**：按 `f"{parent.name}/"` 前缀枚举全部旧块来源纳入 superseded；`_write_groups` 写新前清理超份数旧块；或 prune 对切块目录内且父源已变的来源判僵尸。

### M5【已复现·主代理（摊平+glob）；覆盖分支读码】切块目录两处边角：`..md` 摊平写进顶层 + 同 stem 目录静默覆盖
- **位置**：`agentcore/rag/ingest.py:62-65`（`split_dir = p.parent / p.stem`）、`ingest.py:101-106`（`mkdir(exist_ok=True)` + `write_text`）
- **证据**：`Path('..md').stem == '.'` → `split_dir('/srv/data/kb_samples/..md') == '/srv/data/kb_samples'`（pathlib 折叠 `.`）；且 `glob('*.md')` 命中 `..md`（实测）。即名为 `..md` 的大文件会把 `001.md…` 直接摊平写进 kb_samples 顶层，打破 `scan_samples_units` 文档声明的不变量（产物只在 `<stem>/` 子目录）→ 下轮扫描把块当新顶层源再次入库（内容翻倍、可循环放大）。另一面：源 `notes.md` 与既有用户目录 `notes/` 同 stem 时，`exist_ok=True` 静默覆盖用户文件。
- **建议**：stem 为空/`.`/`..` 或目标目录已存在且含非 `NNN.md` 文件时拒绝并报错；写块前检测冲突。

### M6【已核实·读码】`/kb file` 切块路径（add_file_smart）无判重、无替换、无失败回滚——重跑即成倍复制来源
- **位置**：`agentcore/rag/ingest.py:491-504`（逐 unit 直接 `ingest_file`，无 by_name 对比）；`plugins/qq_agent_adapter/admin.py:684-693`
- **影响**：32MB 文件可产生 ~275 个独立来源；`/kb file` 重跑（或中途第 k 份 embedding 失败后重试——最可能的重跑场景）把已入库的份再整批复制，与脚本/`/kb samples` 路径的指纹判重形成双标。成功文案也不提示重复风险。
- **建议**：切块路径复用 `_process_split_source` 的指纹判重/先写新后删旧语义，或至少在文案中警示。

### M7【推演·机制经主代理读码核实】切块部分失败后的恢复死路：重跑 `--replace` 谎报「内容未变，跳过」，被遮蔽的旧来源永不清除
- **位置**：`scripts/ingest_kb_samples.py:72-83`（`_latest_by_name` setdefault 只留最新）、`scripts/ingest_kb_samples.py:186-188`（skip 分支）；`agentcore/memory/store.py:831-836`（同名按时间倒序）
- **影响**：替换运行中第 k 份失败 → 按设计不删旧（正确），但库里已有一条**新的**同名成功来源；重跑时 `by_name` 只看到最新那条（指纹相同）→ pending/superseded 全空 → 打印「⏭ 切块内容未变，跳过」，被遮蔽的旧同名来源（含旧 parent）任何模式都不再触碰也不提示。
- **建议**：同名多条时对**所有**同名来源做指纹比对（任一匹配即视为已写入，其余纳入 superseded），或跳过时检测同名多条并提示。

### M8【已核实】蒸馏两个新旋钮用裸 `int()`——脏 env/配置即启动崩溃，违背本仓 M2 纪律；commit 声称的默认值与代码不符
- **位置**：`agentcore/rag/service.py:113-121`
- **证据**：`int(os.getenv("AGENT_KB_DISTILL_PER_MESSAGE_CAP", cfg.get(..., 500)))`——同文件 `_resolve_max_chunks` 的 docstring 明写同类教训（「脏值不抛异常……此前 `int("abc")` 会让 bot 启动即失败」）。且代码回退默认实为 **500/12000**（service、`distill.py:124-125` 签名、`render_transcript` 三处一致），8774ede 声称 "default 1000 / 20000"——那只是 config.yaml 的值，配置缺 key 的部署与声称不符。
- **建议**：复用 `_coerce_positive`（告警+回退）；签名/DEFAULTS 与 config.yaml 对齐。

### M9【已核实·grep 0 命中】`.env.example` 未补 8774ede 的两个新 env
- **位置**：`.env.example` KB 节（178-193 行）无 `AGENT_KB_DISTILL_PER_MESSAGE_CAP` / `AGENT_KB_DISTILL_TOTAL_CAP`；同范围的 `AGENT_KB_MAX_CHUNKS_PER_SOURCE`、`AGENT_SHUTDOWN_FLUSH_TIMEOUT`、`AGENT_HELP_IMAGE` 均在档。183 行「超出部分会被丢弃」的注释在自动切块后也已过时。
- **建议**：KB 节补两行（注明字符单位与截断语义），顺带修正过时注释。

### M10【已核实】16.7MB GPL 字体零署名入库：MIT 仓库的许可合规缺口 + 仓库永久膨胀
- **位置**：`data/fonts/wqy-zenhei.ttc`（16,791,251 字节，0e49904 引入；blob 占 `.git` 约 88%）；`plugins/qq_agent_adapter/help_render.py:18`；项目 `LICENSE` 为 MIT
- **证据**：PIL 读出字体身份 WenQuanYi Zen Hei；`data/fonts/` 无 LICENSE/NOTICE；全仓 grep 无 wqy/文泉驿/GPL 署名；无 `.gitattributes`/LFS。上游许可为 **GPL-2.0 + 字体嵌入例外**（多个发行版元数据一致）。GPL 字体以无署名方式并入 MIT 仓库，任何再分发即产生合规风险；16.7MB blob 永久进入 git 历史。
- **建议**：`data/fonts/` 补 LICENSE 与来源 URL，README 资产节声明；评估改安装期获取（help_render 本就有 4 个系统字体候选兜底）或 Git LFS。

### M11【已核实】`pillow>=10.0` 允许解析到带已知 CVE 的版本，且项目无锁文件
- **位置**：`pyproject.toml:31`
- **证据**：`>=10.0` 允许 10.0.0–10.2.x（CVE-2023-50447 `ImageMath.eval` 任意代码执行 <10.2.0；CVE-2024-28219 栈溢出 <10.3.0）。缓解（核实存在）：本项目仅用 `ImageFont/ImageDraw` 绘制自产字符串，不解析不可信图像，CVE 面不可达；约束风格与既有依赖一致（全部 `>=` 无上界）。
- **建议**：改为 `pillow>=10.3.0`（零成本）；长期考虑最低版本 pin。

### M12【已核实】`scratch_db` 固定库名 + `WITH (FORCE)` 在共享 PG 实例上互踩，create/drop 无确认门控
- **位置**：`scripts/scratch_db.py:47-48,57`
- **影响**：两个开发者共用同一 `DATABASE_URL` 实例时，一方的 create/drop 会 FORCE 杀掉另一方正在跑的 PG 套件连接。缓解：惯例为各自本地实例；连到 scratch 库本身时 `DROP` 当前连接库被 PG 拒绝（fail-safe）；CI 不经此脚本。
- **建议**：create/drop 前打印目标 host 并要求 `--yes`；或支持显式后缀 env。

### M13【已复现·主代理逐文件计数】b688317 声称「53 条用例迁入」，实为 48 条
- **证据**：4 个退役文件 `def test_` 实数 = 5_fixes 9（4e6e657 新建）+ concurrency 8 + h 19 + m 12 = **48**；b688317 diff 新增 def 49（48 迁移 + 1 新增单 `-T` 断言）。53 无法由任何口径复现。归并**实质完整性**经逐字比对无问题（见 §4），纯记账失真。
- **建议**：更正 FIX/索引中的口径（与上上轮 M 级计数失真同类）。

### M14【已核实】BACKLOG 基线数字与自身声明的基线不符
- **位置**：`BACKLOG.md:5-6`（ba0b33f 写入）
- **证据**：称「基线 `main @ ba0b33f`……925 收集：默认 886+39」——但 925/886/39 是父提交 b688317 的数字（A2 在 ba0b33f 又加了测试）。违反 BACKLOG 自己「数字按本行基线实测」的纪律（历史上同类记 M14「BACKLOG 数字失真」）。附带：b688317 提交信息「915 passed + 9 skipped」合计 924 ≠ 默认口径 925，疑 typo。
- **建议**：按实测重写该行。

### L 级（压缩列出）
| # | 位置 | 内容 |
|---|---|---|
| L1 | `agentcore/loop/engine.py:130-133` | `_message_tokens` 不计 `tool_calls[].function.arguments` 载荷（send_markdown_file 场景单条实际数千 token 只计 4），「绝不会顶爆上下文」声称对该类消息不成立 |
| L2 | `agentcore/memory/store.py:1122-1129` | `save_session_summary` 盲覆盖写、无水位单调守卫；并发轮次可把水位拉回（重复摘要、多耗 LLM，无丢失） |
| L3 | `agentcore/loop/engine.py:377` | `get_session_summary` 是 `_maybe_roll_summary` 中唯一无 try/except 的存储调用（与邻居不对称，非回归）；摘要 LLM 复用 60s 超时，「绝不影响当轮回复」不含延迟语义 |
| L4 | `engine.py:429` | summary（全 CJK ≈2000 token）/facts/persona 均不计入 `history_token_budget`，README 措辞易读成整体 prompt 预算 |
| L5 | `plugins/qq_agent_adapter/help_render.py:95-96` | docstring 虚称「任何渲染异常都在本函数内吞掉」，实际兜底在 admin.py:92-98；行为上回退链完整 |
| L6 | `lifecycle.py:63-68` | `AGENT_SHUTDOWN_FLUSH_TIMEOUT=nan` 静默退化为 0（=不限）且无 WARNING（`inf` 同） |
| L7 | `help_render.py:77` | `AGENT_HELP_IMAGE=`（空值）经 `or "1"` 视为开启；其余脏值 fail-safe 方向正确 |
| L8 | `admin.py:400` | `/kb samples` 预检即 `materialize=True` 落盘，不启动导入的分支也留孤儿块文件，无清理路径 |
| L9 | `agentcore/rag/distill.py:154-165` | per_message 截断「留痕」仅 WARNING 日志（水位照推进），日志轮转后痕迹即失 |
| L10 | `agentcore/rag/distill.py:172-180` | `total_cap` 配得比单行还小时 transcript 恒空 → min_chars 跳过且水位不推进 → 同批永久重试（仅 error 日志可见，默认值不可达） |
| L11 | `admin.py:544-545` 等 | 无新文档分支 `_oversized_note` 漏 `skipped=True`；`ingest_file_smart` 切块路径静默忽略 `name` 参数；`/kb file` 成功文案不提示重跑重复（关联 M6） |
| L12 | `tests/test_store_contract.py:11` | docstring 仍指向已退役的 `test_review_m_fixes.py`（实际在 `test_memory.py:299`） |
| L13 | `BACKLOG.md:168` | 仍引用已更名的 `tests/test_backup_jsonl.py` |
| L14 | `review/REVIEW-679c9b3..c472e56.md` | 两处引用不存在的 `uv.lock`（仓库从无此文件） |
| L15 | `tests/test_persistence.py:1276-1290` | scratch_db 守卫退化为「源码字符串包含」断言——Mimosa 门禁下可接受的脆弱断言，记录即可 |

## 2. 被证伪的发现（含存疑）

| 怀疑 | 结论 |
|---|---|
| 线3-M3 次级声称：被取消的窗口「脱出 flush 串行、detached 后台运行，与 aclose 竞争」 | **存疑（未复现）**：主代理复现中 6 窗口全部在 flush_all 内串行完成，未出现脱出。M3 的主声称（deadline 无效、声称失真、测试假通过）独立成立 |
| 「>400 条 backlog 造成永久摘要缺口」 | **证伪**：fetch limit=400 截最旧、水位只推进到已取末条（契约测试锁定）；真缺口在 `lines[-200:]`（H1） |
| 「摘要注入可被 ``` 围栏逃逸」 | **证伪**：注入用 `-----` 分隔线围栏（neutralize 按行打散），`summary_max_chars` 截断发生在 fence_untrusted **之前**，截断不可能破坏围栏 |
| 「L5 zip `=` 收窄 reopen 逃逸面（`--opt=val` 注入等）」 | **证伪**：`-` 开头参数精确白名单 `{-r,-q,-9,-j}` 且拒 `=`，`-T`/`-TT`/`-T=x`/`-rq`/`--unzip-command=x` 全拒；单 `-T` 拒绝断言在册；操作数只当文件名 |
| 「切块文件名路径穿越」 | **证伪**：stem 不含分隔符；`..md` 的 stem 是 `.`（本级非上级），后果降为 M5 的摊平/覆盖 |
| 「total_cap 停止收集丢消息或反复重蒸」 | **证伪**：break-before-append + `last_included_id` 只在 append 后推进，未收集消息下轮重收（H4 未回退）；「重蒸」仅 total_cap 配置过小时可能（L10） |
| 「测试归并中用例被静默弱化」 | **证伪**：48 条函数体机器逐字比对，唯一差异 `test_rejects_destructive_flags` 是**加严**（黑名单补 `-T`） |
| 「debounce `task=None` 还有其他 cancel 路径」 | **证伪**：entry 注册与 task 赋值同锁临界区、所有 pop 均持锁 → `_pending` 中 entry 恒有 task |
| 「16.7MB 字体逐条帮助消息重复加载是性能问题」 | **证伪**：实测 4 字号加载共 0.2ms（FreeType 惰性读表） |
| 「resolve_session 键型不一致致新原语错乱」 | **证伪**：两实现均返回 str |
| 线1 存疑：trim 后 sanitize 可能把窗口头部孤儿 tool 摘掉使摘要提前纳入近期消息 | 判定无害：被摘的只有 tool/空 content 行，摘要器本就过滤 |

## 3. 测试与文档状况

**实跑数据（主代理）**
- 默认套件：**910 passed / 42 skipped**（13 warnings）。声称链吻合：ba0b33f 声称 898 + 733f57e 新增 12 = 910。
- 全新空 scratch 库（`scripts/scratch_db.py create` → 全套件 → drop）：**943 passed / 9 skipped**（931 + 12 = 943，吻合）。等价复现了 CI #43 的「全新库建表」场景，**9e93472 的 DDL 修复确认有效**；AGENTS.md §5 的教训流程（动 DDL 必须跑全新空库）本次实际执行通过。
- `ruff check`：全绿（venv ruff 0.16.6）。`ruff format --check`：仅 2 个 `review/*.md` 被标记（0.16.x 新增的 markdown 代码块处理；`review/` 文档里嵌入的代码片段），**无 .py 文件被标记**。

**CI**：本会话 `gh` 未认证，无法经 API 核实 HEAD 的 run 状态（上轮曾用 API 核实 #35–#38）。ci.yml 配置核实：pgvector:pg16 service 每次全新空库 → ruff==0.9.6 check + format --check → pytest（注入 TEST_DATABASE_URL），与 pyproject/pre-commit 三处 pin 一致。

**文档一致性**：README（历史裁剪节、帮助图片、KB 自动切块）、AGENTS.md、BACKLOG A2 ✅ 主体与实现相符；缺口见 M9（.env.example 缺 2 env + 过时注释）、M13/M14（记账失真）、L12–L14（悬空指针/更名/不存在的文件）。

**未验证面（§8.3 门禁声明）**
- (a) HEAD 的 CI run 结论（无凭据）；本地已用「全新空库全套件」等价复核，风险低。
- (b) ruff **0.9.6**（CI pin）的 format 口径（离线无法安装）：所有 .py 文件在 0.16.6 下双 check 全绿，0.9.6 不处理 .md，风险很低。
- (c) 真机 QQ 通道的帮助图片发送（`MessageSegment.image` base64）仅读码+本地渲染验证（141,981 字节 PNG）。
- (d) da764b4/0d9f0be 声称「清 Mimosa 3+6 高危」：代码形态已核对一致（动态标识符确实清零），扫描器结论本地不可复核。
- (e) DeepSeek thinking fallback（上上轮遗留，不在本范围）：**仍开放**。
- 结论：涉及面均无 H 级问题，**不阻断**；上一轮未验证面逐项复核见 §5。

## 4. 已验证为「无问题」的关键项

| 要点 | 验证方式 |
|---|---|
| A2 双实现契约：`get_history_window`/`get_session_messages_between`（严格开区间、limit 截最旧、`before_id=None` 等价）/`get_session_summary`/`save_session_summary`（空摘要 ("",0) 口径）、ABC 签名一致、`resolve_session` 双实现均 str | 线1 读双实现 + 契约测试逐条核对 |
| `_trim_history_to_budget` 边界（空/恰好/单条超预算/budget≤0）与 `_estimate_tokens` 确定性 | 主代理纯内存实跑 |
| DDL：`summary_upto_id` 幂等 ALTER 先建表后补列、TIMESTZ 笔误全仓无残留、da764b4 白名单表（table 来自硬编码元组、dim int 强转、`{dim}` 恰两处）真正消除注入面 | 线1/线4 读码 + 全新空库实跑 |
| 摘要失败隔离：`_summarize_messages` 吞异常水位不动、save 失败回退旧摘要、`summary_enabled=false` 等价旧行为、fence 不可提前闭合 | 线1 读码 + 测试核对 |
| 大文件切块主语义：32MB 硬上限在读取前按 stat 判定且三路径响亮拒绝；「先写新再删旧」属实（failures>0 零 delete，测试锁定顺序）；分组切块「只合并不新增」不变量（300 组随机压演）；samples/脚本路径按份级 sha256 幂等；delete_source 仍受 AGENT_KB_ENABLED 门控 | 线2 读码+测试核对+内存压演 |
| 蒸馏：4096 预算只改 max_tokens 透传；伪造说话人前缀中和先于截断；sanitize/围栏管道未被新代码绕过 | 线2 读码 |
| 帮助图片：回退链完整（无 Pillow/无字体/异常→None→文本）；尺寸有界（880×≈1002，PNG ≈142KB）；字体路径 `__file__` 定位正确；帮助命令 ACL 无变化 | 线3 读码+本地实测 |
| debounce M1 守卫完整（`_pending` 恒有 task 不变量）、`_spawn` 强引用+命名无新竞态、停机顺序仍固定幂等 | 线3 读码+测试核对 |
| zip `=` 收窄对抗复核无逃逸；curl/SSRF、`pow` 守卫等历史 H 回归用例逐字保留 | 线3+线4 |
| 测试归并完整性：48 条全部找回、逐字一致、映射表 §9.1 与实际吻合、13 条 pg_store 删除逐条有契约对应、`def test_` 915→903 与「937→925」Δ 自洽、文件 40→37 | 线4 机器逐字比对 |
| flock-on-fd（db_backup）、scratch_db DDL 全字面量且无调用残留、产物位置硬性约束（`git ls-files` 根目录 0 命中）、.mimosa gitignore | 线4 读码+自检 |

## 5. 与在库报告的衔接复核

上轮 [REVIEW-679c9b3..c472e56.md](REVIEW-679c9b3..c472e56.md) 的 M1–M4 + L1–L11，对应 [FIX-679c9b3..c472e56.md](FIX-679c9b3..c472e56.md)（4e6e657）：

| 上轮项 | 本轮状态 |
|---|---|
| M1 debounce `max_parts=1` 崩溃 | ✅ 真修（守卫+2 回归，线3 核对不变量） |
| M2 默认唤醒词含 `ai` | ✅ 真修（`.env.example` 现为 `小助手,助手` + 风险注释） |
| M3 两条假通过用例 | ✅ 真修（get_bot 三态 / zero_delay 立即性，线3 核对） |
| M4 §9 索引缺失 | ✅ 已补（在册） |
| L3 停机 flush deadline | ⚠️ **形式上修了，实际无效 → 本轮 M3**：旋钮/文档/测试俱在，但真实 `flush_all` 的 shield 使其完全失效 |
| L4 后台任务强引用 | ✅ 真修（`_spawn`+burst 命名） |
| L5 zip `=` 收窄 | ✅ 真修（对抗复核未发现逃逸，安全用例原样通过） |
| L1/L2/L6–L11（docstring/台账/README/计数） | ✅ 抽查一致（L14 发现上轮报告自身引用了不存在的 uv.lock） |

上上轮及更早的「未验证面」销项：
- `max_parts=1` 真实使用面 → **闭环**：默认 20、仅 env 可配 1、守卫在位。
- 停机 flush 经全局信号量的阻塞时长 → **未闭环且恶化**：deadline 无效（本轮 M3），阻塞上界仍未约束。
- DeepSeek thinking fallback → **仍开放**（本轮 (e)）。

## 6. 修复优先级建议

1. **H1**：水位推进语义（只推进到实际喂给 LLM 的末条 / 分批摘要）。与 M1、M2 同在「trim→摘要→组装」链上，建议同批修复并互为回归（tool 对切点 + 键投影 + 水位口径），补「trim 后孤儿 tool」「水位 ≤ 实际喂入末条」「messages 键集」三条真实验证。
2. **M3**：`flush_all(deadline)` 原生传参 + **真实 Debouncer** 集成回归（现有 Fake 假通过必须换掉）。
3. **M5 / M8 / M9**：小块级——切块目录边角拒绝、`_coerce_positive` 复用与默认值对齐、.env.example 补档。
4. **M4 / M6 / M7**：摄取收敛性同批设计（前缀枚举 superseded、/kb file 判重、同名多条指纹比对）——三者同模块同语义，分头修容易再留角。
5. **M10–M14 + L12–L14**：文档/资产/记账批处理（字体 LICENSE + README 声明、pillow>=10.3.0、scratch_db `--yes`、53→48 更正、BACKLOG 基线实测）。
6. L 项按 FIX 惯例逐条标注「修/降级记录」。

> 修复阶段请按规范 §6 产出 `review/FIX-c472e56..733f57e.md` 并引用本报告编号。
