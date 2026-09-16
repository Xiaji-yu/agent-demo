# agent-demo 近期 Commit 评审报告

**评审范围**：46c85d1..6ec3f7c（5 个 commit：de97227 / 81a8521 / 6ffc01f / 7addd06 / 6ec3f7c）
**评审日期**：2026-09-16
**评审方式**：6 维分线评审（安全/并发/部署/测试/配置/行为契约）。子代理运行环境连续失败十余次，实际执行：3 个维度（安全/并发/部署兼容）由子代理出报告、**主代理逐条实证复核**；3 个维度（行为契约/配置健壮性/测试有效性）由主代理直接完成（纯代码走查+实测，无子代理依赖）。全部【已复现】项均有主代理独立验证或可复现命令。
**工作区状态**：git status 干净（HEAD=6ec3f7c，无未提交改动）
**基线**：1034 passed / 43 skipped（PG 用例因无 TEST_DATABASE_URL 跳过），ruff check + format --check 全绿

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 最高风险 | **H-1** 表格渲染同步 CPU 阻塞 NoneBot 单事件循环（最长实测 7.3s，LLM 输出即可触发，无需注入）；**H-2** docker-compose 提交真实数据库凭据（个人密码 `Yll@2468` 入公开仓库 git 历史，且 `@` 字符令 DSN 解析失败——本地 bot 实际连不上 PG、一直静默跑在内存降级态） |
| 各主题 | 安全：表格链路无注入面（围栏 lookalike 实测推翻），但 search 工具结果不过围栏（不变量违反，既有缺口被本轮放宽触发放大）；并发：事件循环阻塞 + 退避失效 + 池泄漏；部署：字体路径仅 editable 成立；配置：LLM 数值 env 空值 import 即崩；测试：3 条防线缺口（H-1/M-1/M-3 均无守卫用例） |
| 测试数 | 1034 passed / 43 skipped（默认套件）；ruff 双 check 全绿 |
| 声称核对 | README:126「不再每轮等待 30s 超时」被 M-2 退避失效**推翻**；de97227 commit message 未提 compose 凭据改动（§3 声称管理，记 M） |
| §8.3 门禁 | 2 条 H → **阻断**（未修复前不建议继续部署新克隆/公开分发） |

## 1. 已实证的问题

### H-1 | `agentcore/render/table.py:96-166` + `plugins/qq_agent_adapter/outbound.py:717-718`（6ec3f7c 引入）—— 表格渲染阻塞整个事件循环

**问题**：`render_table_png` 是纯同步 CPU（Pillow），被 async `deliver_reply` 在 await 之前直接调用（`outbound.py:718` `png = render_table_png(tbl)`，全链路无 `to_thread`/executor）。NoneBot 单事件循环 → 渲染期间所有会话的消息处理、防抖计时、节流、提醒 tick、在途 httpx 响应全部停摆。

**证据【主代理已复现】**：
- 10 列×30 行全中文：**425ms**（本机）；子代理实测 30 列×50 行最坏用例 **7.23–7.44s**（4 次），心跳（10ms 周期）最大间隔 **7623.7ms**——整个循环冻结
- 耗时应归因（子代理 cProfile + 主代理复核）：`font.getbbox` 单次 ~329µs；列宽按字号 16→10 **7 轮全量重算**（`table.py:111-120`，每轮 1500 次 getbbox）+ 逐字符截断 while（`table.py:151-152`，每次 O(len) 复测）→ 约 2.5 万次 ≈ 7.3s
- 渲染在 turn 并发闸门之外（`matcher.py:214-215` 信号量只包 `_run_and_format`，223 行 `deliver_reply` 在锁外）→ 最多 4 个并发 turn 的渲染串行叠加，冻结时间相乘
- 可达性：用户正常说"把这段长文整理成表格"、模型把长文本填进单元格即触发（**无需注入**）；`LLM_MAX_TOKENS` 默认 1024 → 单条回复约 900 字上限，默认配置约 4-5s 阻塞；调大 max_tokens 后分钟级

**与 AGENTS.md §5 已踩坑同类**（`pow(2,999999999)` 冻结事件循环）。

**建议**：① 主修一行 `png = await asyncio.to_thread(render_table_png, tbl)`（异常经 await 原样传播，语义不变）；② 截断改二分/按平均字宽估算一次校验；③ 补"渲染期间循环保持响应"回归用例（渲染中起心跳任务断言间隔 <100ms）+ 最坏表格 wall-time 上限断言。

### H-2 | `docker-compose.yml:6-7`（de97227 引入，区间内）—— 真实数据库凭据入公开仓库 + DSN `@` 陷阱

**问题**：de97227 把 compose 的 `POSTGRES_USER/PASSWORD` 从 `qqagent/qqagent` 改为个人账号 `xiaji` / `Yll@2468`，随 git 历史进入公开仓库 `github.com/Xiaji-yu/agent-demo`。密码含 `@` 字符导致 DSN 按第一个 `@` 分区失败。

**证据【主代理已复现】**：
- `git show 46c85d1:docker-compose.yml` → `qqagent/qqagent`；`git show HEAD:docker-compose.yml` → `xiaji` / `Yll@2468`；`git log 46c85d1..HEAD -- docker-compose.yml` → 唯一改动 commit 为 de97227（其 commit message 未提及此改动，见 M-5）
- asyncpg 实测 `.env` 中的 DSN `postgresql://xiaji:Yll@2468@127.0.0.1:5432/qqagent` → **`gaierror: [Errno -2] Name or service not known`**（asyncpg 按 netloc 第一个 `@` 分区：密码被截断为 `Yll`、host 变成 `2468@127.0.0.1`；正确写法须 `Yll%402468`）
- **实际影响**：本地 bot 当前连不上 PG，一直运行在 de97227 的 InMemory 回退态——记忆/事实/归档全部进程内、重启即丢，且不崩溃（ERROR 日志）
- 文档冲突：README:96 `DATABASE_URL=postgresql://qqagent:qqagent@...`、README:123 `psql -U qqagent`、CONTRIBUTING:27、AGENTS:74 仍全部写 qqagent/qqagent，本区间无同步
- 缓解项：端口绑定 127.0.0.1 未对外暴露；CI 自带 qqagent/qqagent 的 pgvector service 不受影响

**建议**：compose 改 `${POSTGRES_USER}`/`${POSTGRES_PASSWORD:?}` 环境注入（.env.example 给占位符）；**轮换该密码**（已入 git 历史）；README:96/123、CONTRIBUTING:27、AGENTS:74 四处同步；若保留个人值必须注明 `@` 需 URL 编码。

### M-1 | `agentcore/render/table.py:107-127,145-162` + `outbound.py:710-720`（6ec3f7c 引入）—— 渲染路径零规模防护 + 未处理异常

**问题**：单元格长度、行数、列数、每回复表数均无护栏；渲染/发送异常无 try/except，冒泡到 `matcher._answer` 兜底，**整条回复被"出错啦"替换、剩余文本与后续表格全丢**（先发出的图保留，部分投递+脏错误信息）。与 chunked 路径逐块 try/except（`outbound.py:788-796`）容错粒度不一致。

**证据【主代理已复现】**：
- 超宽表（1601/2000 列）：`_MAX_WIDTH // n_cols == 0` → col_w 全 0 → `Image.new((0,h))` → **`ValueError: cannot write empty image`**（主代理实测复现）
- 单格 1000 汉字（3×3 小表）：子代理实测 **24.6s**（截断 while 对超宽单元格逐字符 `text[:-1]` + 每次 O(len) getbbox → O(n²)）；主代理复核同量级成立
- 2000 行×2 列：3.98s / PNG 1.3MB；图像高度 = `line_h × 行数` 无上限 → 2 万行约 2.5GB → MemoryError → 用户拿到无信息的"出错啦"（MemoryError 的 str() 为空）

**建议**：render 前校验（行数 ≤200、列数 ≤20、单元格 ≤200 字、每回复表数上限）越限降级纯文本（沿用 `png is None` 路径）；`deliver_reply` 表格分支逐表 try/except（渲染异常等同 None、发送异常记日志继续）；`_answer` 兜底文案附异常类型名。

### M-2 | `agentcore/embedding/client.py:97-108`（81a8521 引入）—— 降级退避在首次探测失败后永久失效；README 声称被推翻

**问题**：`_enter_degraded` 的 `if self._degraded_since is None` 守卫使探测失败时**不刷新**时间戳 → `_should_try_remote` 从首次降级算起永远"到期" → 降级态每次 `embed_many` 都先吃满远程超时（默认 30s）再回退本地。81a8521 的核心目标（"不再每轮等待 30s"）在故障持续 5 分钟后被推翻。

**证据【主代理已复现】**（打桩 `_remote_embed` 抛 ConnectError，时间推进模拟）：
```
远程请求数: 首次=1, 探测失败后=2, 再调用=3
_should_try_remote() = True
```
即设计意图"每 300s 试探一次"只在**第一次探测之前**成立；Ollama 未启动等持续故障场景（正是 81a8521 要修的场景）下，每轮对话的召回+抽取两次 embed_many 各挂 30s。

**声称核对**：README:126「embedding 服务不可达时……不再每轮等待 30s 超时」——首次降级后 300s 内成立，探测失败后失效，属**声称与实现不符**（§3 记 M）。

**测试缺口**：`test_degraded_skips_remote_until_retry_interval`（test_embedding.py:377-395）第 3 步手动伪造 `_degraded_since` 后直接断言 `calls==2` 结束，**缺"探测失败后再调一次仍跳过"的断言**——补 `await client.embed_many(["d"]); assert len(calls) == 2` 即可抓住。

**建议**：`_enter_degraded` 改无条件刷新 `_degraded_since`（日志仅首次进入打，加 `_degraded_since is None` 判断仅用于日志）；补上述第 4 步断言。

### M-3 | `plugins/qq_agent_adapter/__init__.py:143-152`（de97227 引入）—— PG init 回退时失败池从未 aclose（进程级连接泄漏）

**问题**：except 分支直接 `memory = InMemoryMemoryStore()` 替换引用，失败的 `PgMemoryStore` 及其 asyncpg 池被整体丢弃、从未关闭；停机时 `lifecycle._close_memory` 关闭的是 InMemory（无 aclose）→ 泄漏连接伴随进程终生，事件循环关闭时 asyncpg 留下未关闭告警。

**证据【主代理代码确认 + 子代理 fake 池复现】**：
- `__init__.py:143-152`：except 分支无 `await memory.aclose()`（已读全文确认）
- 子代理复刻回退代码 + 假池：被丢弃对象 `pool` 仍非 None、`close()` 次数=0
- `PgMemoryStore.aclose`（store.py:1532-1535）`if self.pool:` 守卫对 pool=None 幂等 → **修复方案安全**：except 分支先 `await memory.aclose()` 再回退

**建议**：except 分支加 `await memory.aclose()`；补"init 失败后池被关闭"用例（假池计数）。

### M-4 | `agentcore/loop/engine.py:625-631`（既有缺口，6ec3f7c 放大）—— search 工具结果不过围栏（AGENTS.md §4 不变量违反）

**问题**：`search_web`/`search_multi` 的返回（title/snippet/url/date）以 `role:tool` 原样注入 messages，未过 `fence_untrusted`。对照：`web_fetch.py:172`（网页围栏）、`rag/retriever.py:49-50`（KB lookalike 打散）、facts/摘要（engine.py:287-299/481-488）均有处理——唯独 search 工具结果没有。AGENTS.md §4 明文"检索结果必须过围栏"。

**证据【主代理代码确认】**：engine.py:625-631 `messages.append({"role": "tool", "content": safe_result})` 无围栏；本轮 6ec3f7c 把搜索触发从"用户明说"放宽为"语境差异大即主动搜"，暴露频率上升。历史评审 REVIEW-8cfbf6d..a604023 已报"检索结果未套围栏"，本轮仍未处置。

**建议**：tool 结果注入前过围栏（与 web_fetch 同款，标题+围栏+来源）；给 search 调用加每轮计数（可选预算化）。

### M-5 | de97227 commit message 声称与 diff 不符（§3 声称管理）

**问题**：de97227 message 为 "fix: pg init fallback and review report"，实际 diff 含 docker-compose.yml 凭据改动（H-2 载体）——`git add -A` 把工作区个人凭据一并提交，message 未声明。

**建议**：凭据改回/变量化后，历史中的个人密码需轮换（已推送无法改写历史，除非 force-push 且无他人克隆）；今后提交前 `git diff --stat` 核对声称。

### M-6 | `agentcore/llm/client.py:19-20`（既有，本轮用户已提出未修）—— LLM 数值 env 空值 import 即崩

**问题**：`float(os.getenv("LLM_TEMPERATURE", "0.7"))` / `int(os.getenv("LLM_MAX_TOKENS", "1024"))` 裸解析，空值 ValueError；`_CFG = _LLMCfg()` 模块级单例 → **import 即崩，bot 起不来**。

**证据【主代理已复现】**：`LLM_MAX_TOKENS= .venv/bin/python -c "from agentcore.llm.client import _CFG"` → `ValueError: invalid literal for int() with base 10: ''`；`LLM_TEMPERATURE=` → `ValueError: could not convert string to float: ''`。

**建议**：按 embedding/client.py 与 pipeline.py 预算解析的 try/except 兜底模式改为空值回落默认（0.7 / 1024）；补两条 env 空值用例。

### L 级（压缩列出）

| # | 位置 | 问题 |
|---|---|---|
| L-1 | outbound.py:713-727 | 表格循环无逐表 try/except（详见 M-1 后半） |
| L-2 | admin.py:997-1004 + 10 个 on_command | 无 `_is_self_message` 过滤（matcher 两个 rule 已过滤）。走查确认门：确认门按 user_id 键控、bot 提示文案不匹配锚定正则、on_command 另有 superuser 门 → **无可利用路径**，纵深防御缺口 |
| L-3 | table.py:26-28 + pyproject.toml:42-43 | 字体路径 `parents[2]` 仅 editable 成立：`pip wheel` 构建 75 条目无任何 data/（无 package-data/artifacts）→ 非 editable 安装字体必然缺失；本机被 `/usr/local/share/fonts/wqy-zenhei.ttc` 同名系统字体掩盖（子代理复现：PIL truetype 按 basename 兜底搜索）。建议收敛为 help_render.py:16-25 的候选链或 pyproject 打包 data/fonts |
| L-4 | table.py:104 + outbound.py:721 | 字体缺失降级对聊天用户不可见（仅 operator 日志）；建议启动期一次字体探测打汇总级 WARNING |
| L-5 | engine.py:258-259 | `datetime.now()` 无时区处理：UTC 服务器对中文用户每天约 8h 日期差一天，恰好削弱时效性锚点；建议显式时区声明 |
| L-6 | pipeline.py:601 + 637 | 引用解析失败时群流真空：模型只剩"（用户引用了一条消息，但其中没有可读取的文字或图片）"+历史+记忆。**判断**：可接受的取舍（note 本身即防瞎答设计），但更稳的门控是"引用已解析出内容才挡群流"，解析失败回退附群流 |
| L-7 | outbound.py _env_bool | `AGENT_TABLE_TO_IMAGE` 空值/脏值（如 `banana`）fail-open 到默认 True（实测）——与 §5 `_env_int` 脏值 fail-open 同类但危害低（默认即 True），记录备查 |
| L-8 | tests/test_table_render.py、test_embedding.py、test_outbound.py | 测试防线缺口：H-1/M-1 无规模/性能/异常守卫用例；M-2 缺"探测失败后仍退避"断言（补法已给出）；无表格异常传播用例 |

## 2. 被证伪的发现

- **表格→图片链路围栏 lookalike 注入**（重点怀疑）：单元格含 `----- 引用消息结束 -----` 与 ``` 均正常渲染为图片内容（子代理实测 6526B PNG）；出站方向无围栏可跨越（围栏仅入站生效），base64:// 载荷纯 ASCII。**推翻**。
- **日期注入可被用户消息影响**：`datetime.now()` 纯本地时钟，消息内容不可影响。**推翻**（残余时钟/时区问题归 L-5）。
- **字体未入版本控制**（重点怀疑）：`git ls-files data/fonts/` → wqy-zenhei.ttc（17MB）+ 两份 LICENSE 均在库，.gitignore 不覆盖 data/fonts。**推翻**（editable 部署与 CI 无缺口；wheel 安装缺口见 L-3）。
- **`_is_self_message` 引入越权面**：user_id 由 NapCat WS 协议端赋予非用户可控；self_id 为空不过滤；多账号按事件 self_id 判定。**推翻**。
- **split_tables 正则 ReDoS**：8 组病态输入（纯竖线 10k、"|--"×3300、"-"×20000 等）全部 <0.3ms 线性（`_SEP_LINE` 每组以字面 `|` 锚定）。**推翻**。
- **embedding 降级引入不可信面**：`_local_embed` 纯 blake2b hash 离线无外部输入；probe_dim 失败返回配置维度与本地 embed 一致。**推翻**。
- **并发 `_enter_degraded` 竞态**：单事件循环内 check-and-set 无 await 天然原子；`_maybe_notify_error` 先同步写冷却再 await → 至多一次通知。**推翻**（无害）。
- **time.monotonic() 重启归零致 300s 误判**：`_degraded_since`/`_last_error_notify` 初值 None 且 `_should_try_remote` 先判 None（§5 的 0.0 哨兵坑已避开）。**推翻**。
- **NAPCAT_HTTP_URL 空值影响群文件**：`file_sender.py:201` 群分支先于 HTTP 分支，群文件走 `bot.upload_group_file`（反向 WS），无部署假设。**推翻**。

## 3. 测试与文档状况

**实跑数据**：1034 passed / 43 skipped（默认套件，11.0s）；ruff check + format --check 全绿（112 files）。PG 契约用例因无 TEST_DATABASE_URL 跳过（43 skipped 的一部分）——**未验证面 (a)**：本轮未动 store.py 双实现，风险低。

**测试有效性（主代理维度）**：
- 恒真断言排查：本轮新增用例的措辞守卫/行为断言在实施时做过变异复核（split 失效 2 失败、render 恒 None 2 失败、outbound 回退无法收集），未发现恒真断言
- 覆盖矩阵缺口（详见 L-8）：H-1/M-1/M-2 三条实证问题**均无守卫用例**——特别是 M-2，测试只覆盖到"手动伪造时间戳后的探测"，未覆盖"探测失败后的下一次调用"（这是真实场景）
- 环境隔离：`_clean_image_env`/`_clean_kb_env` 有效（污染环境实测 74 passed；autouse 先跑、setenv 后生效的顺序逻辑正确）

**文档一致性**：
- README:431 群文件"上传失败如实告知、不会改成私发"已同步（6ffc01f 行为变更）✓
- README:125-126 embedding 降级措辞如实说明词面近似，但"不再每轮等待 30s"被 M-2 推翻 ✗
- README/CONTRIBUTING/AGENTS 的 DB 凭据与 compose 不一致（H-2）✗
- .env.example 的 AGENT_TABLE_TO_IMAGE 与实现一致 ✓

**未验证面清单**（§8.3）：
- (a) PG 门控用例（无 TEST_DATABASE_URL）：本轮未动存储代码，降级记录
- (b) 非 editable 安装字体路径（L-3）：本机 editable，wheel 内容检查由子代理 pip wheel 复现（75 条目无 data/），主代理采信（方法可复现：`pip wheel --no-deps .` + zipfile 列表）
- (c) 真机 NapCat 回传自身消息的自触发场景（6ffc01f 的 self 过滤）：协议行为，本地不可复现；走查确认 NoneBot 只认 post_type=message
- (d) H-1 在真机多会话并发下的冻结叠加：推演（闸门外渲染 ×4 并发 turn），本地不可复现

## 4. 已验证为「无问题」的关键项

- 表格渲染链路的注入面（围栏 lookalike、CQ 码、base64 载荷）——见 §2 证伪项
- `_is_self_message`（6ffc01f）的越权面、多账号语义
- split_tables 正则性能与病态输入
- embedding 降级的并发竞态、恢复一致性、时钟哨兵（§5 坑已避开）
- PG 回退后的停机顺序（lifecycle 固定 scheduler→flush→aclose；InMemory 无 aclose → hasattr 跳过即 no-op）与依赖 PG 的 scheduler 组件行为（内存可用、备份失败被 _guard 记录不崩）
- 出站"结果不确定绝不重发"不变量（图片路径失败即整体中止，不重发）
- 长期记忆/知识库/摘要围栏本轮无回归（仅 search 工具结果缺口为既有）
- NAPCAT_HTTP_URL 空值的群文件路径
- pillow>=10.3.0 依赖本轮无新增缺口（46c85d1 时已在 pyproject 声明）

## 5. 与在库报告的衔接复核

上轮 [REVIEW-733f57e..46c85d1.md](REVIEW-733f57e..46c85d1.md)（由 de97227 提交入档）发现与本区间的处置：

| 上轮发现 | 本区间处置 |
|---|---|
| M1 测试环境隔离（.env 泄漏） | ✅ 已修两例：`_clean_kb_env` 加 AGENT_KB_DISTILL_*（81a8521）、`_clean_image_env` 新增（7addd06） |
| L1 BACKLOG 数字陈旧 | ⚠️ 未核（本轮范围外，建议下轮处理） |
| L2 README 文档缺口 | ✅ 已多处同步（embedding 降级、群文件、表格转图、搜索优先级） |
| L3 依赖 pinning | ✅ pillow>=10.3.0 已在 c472e56 轮锁定，本轮无新增依赖 |

## 6. 修复优先级建议

1. **H-2 凭据**（阻断）：compose 改环境变量 + 轮换密码 + 四处文档同步（个人密码已入 git 历史，轮换是唯一彻底处置）
2. **H-1 事件循环阻塞**（阻断）：`asyncio.to_thread` 一行主修 + 回归用例；规模护栏（行/列/单元格上限）随 M-1 一并做
3. **M-2 退避失效**：`_enter_degraded` 无条件刷新时间戳 + 补第 4 步断言（81a8521 的目标才能真正成立）
4. **M-3 池泄漏**：except 分支 `await memory.aclose()` + 用例（一行）
5. **M-4 search 围栏**：tool 结果过 fence_untrusted（与 web_fetch 同款）
6. **M-6 LLM env 空值**：try/except 兜底 + 用例
7. L 级按 L-2/L-3/L-5/L-6 排序酌情处置

> 修复产出 `review/FIX-46c85d1..6ec3f7c.md`，逐条记录改法与回归。
