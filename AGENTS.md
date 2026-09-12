# AGENTS.md —— 交接文档（给下一位接手的人 / Agent）

本文件是**开工前必读**：项目是什么、边界在哪、改代码的硬性纪律、以及历史上踩过的坑。
面向"要动手改这个仓库"的人（或 Agent），不替代 README（用户视角）与 CONTRIBUTING（提交规范）。

---

## 1. 一句话与分层

QQ 机器人 Agent：**NapCat（QQ 协议端）↔ NoneBot2（反向 WS）↔ 本仓库**。LLM 走 OpenAI 兼容接口，
从 `.env` 读 base_url / model / key，**不绑死任何供应商**。

```
┌ NapCat (私有协议端，局域网)  ── 反向 WebSocket ──┐
│                                                  ▼
│  plugins/qq_agent_adapter/   ← 薄适配层（唯一依赖 NoneBot 的地方）
│    matcher.py     路由/ACL/防抖/并发闸门/发送
│    pipeline.py    payload 组装、引用+转发解析、图片管线、最近图片缓冲
│    outbound.py    长回复分层投递（单条/合并转发/文件）+ 出站节流
│    group_context.py  群聊上下文环形缓冲（可选）
│    debounce.py    按 chat+user 防抖合并
│    lifecycle.py   停机顺序编排（scheduler → flush → aclose）
│    wakewords.py   唤醒词加载/匹配/剥离（触发与剥前缀共用）
│    acl.py / sink.py / admin.py / scheduler 接线
│                    │
│                    ▼
│  agentcore/        ← 纯 Python，**不得 import nonebot**
│    llm/ embedding/ loop/ personas/ skills/ memory/ rag/
│    workspace/ backup/ scheduler/ budget/ safety.py multiagent/(空壳)
└─ tests/  review/  scripts/  data/(运行时产物，gitignore)
```

**分层铁律**：`agentcore` 保持 NoneBot 无关（可单测、可复用）；`plugins/` 只做平台适配。
平台相关的东西下沉到 `agentcore` 会让全部单测失效。

---

## 2. 工作纪律（硬性）

1. **先分级再动手**：Low（笔误/文案）→ 直接改；Medium（局部逻辑/契约/边界）→ 说明方案再改；
   High（安全边界、数据丢失、并发/生命周期、schema 迁移）→ 先给出**可复现证据**与方案，再改。
2. **AC 先行**：改之前写清"怎么算改好了"（一条可执行的验收命令或断言），改完必须真的跑过。
3. **只报可验证的结论**，并标注等级：
   - `verified` — 本次实际运行/复现得到（附命令与输出摘要）
   - `high` / `low` — 静态阅读代码得出的高/低置信推断
   - `unknown` — 无法在当前环境验证（说明为什么）
   **绝不编造测试输出、日志、行号或"已修复"结论**；没跑过就说没跑过。
4. **不要自动 commit / push**。本仓库的推送**只在用户明确要求时**执行；改完把 diff 与验证结果摊开即可。
5. **最小改动**：不顺手重构、不批量改风格、不动与本任务无关的文件。历史行为要有意识保留
   （若确实要改语义，必须在 `review/FIX-*.md` 或 BACKLOG 里写明"从 X 改为 Y"与理由）。
6. **文档随代码同步**：改了行为就改 README / BACKLOG 里的对应描述与数字；注释只写"为什么"，
   不写"做了什么"（代码本身已经说了）。
7. **测试要能抓 bug**：新增/修改用例后，把实现**故意改坏**跑一遍，确认用例**真的失败**再还原
   （本仓库大量历史用例是"恒真断言"，详见 §5）。单文件变异可能假阴性，判定覆盖时以**全量套件**为准。

---

## 3. 环境与常用命令

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
cp .env.example .env      # LLM_BASE_URL/API_KEY/MODEL、DATABASE_URL、NapCat 鉴权等全部 env 驱动
docker compose up -d db   # PostgreSQL + pgvector（M1+ 需要）

# 默认套件（秒级；PG 用例跳过）
.venv/bin/python -m pytest -q

# 带 PG 的完整套件（PG 用例是真的建表/TRUNCATE，绝不能用生产库）
TEST_DATABASE_URL="postgresql://qqagent:qqagent@127.0.0.1:5432/qqagent_test" \
  .venv/bin/python -m pytest -q

# 仅 PG 契约测试
TEST_DATABASE_URL=... .venv/bin/python -m pytest tests/test_pg_store.py tests/test_store_contract.py -q

# 性能/泄漏基线（默认跳过）
RUN_PERF=1 .venv/bin/python -m pytest tests/test_perf.py -q
.venv/bin/python scripts/perf_baseline.py          # 与 perf/baseline.json 比对，劣化退出码 1

# Lint / 格式（CI 两者都门禁）
.venv/bin/python -m ruff check agentcore plugins tests bot.py scripts
.venv/bin/python -m ruff format --check agentcore plugins tests bot.py scripts
```

- **`ruff` 版本必须与 `pyproject.toml` 的 pin 一致**：本地版本漂移会导致"本地绿、CI 红"或反之。
- **验收以 CI 为准**（`.github/workflows/ci.yml`：ruff check + ruff format --check + pytest，
  并起 `pgvector/pgvector:pg16` service 导出 `TEST_DATABASE_URL`）。本地绿 ≠ CI 绿。
- CI 运行查询（无需登录）：
  `curl -s https://api.github.com/repos/Xiaji-yu/agent-demo/actions/runs?per_page=3`。
  查具体日志需要 `gh` 登录（匿名拿不到）。

---

## 4. 必须守住的不变量（改代码前先对照）

| 领域 | 不变量 |
|---|---|
| 配置 | 全部通过 `.env`：`LLM_BASE_URL/API_KEY/MODEL/…`、`LLM_FALLBACK_*`（可选）、`EMBEDDING_*`。**禁止**在代码里加供应商预设分支 |
| 路由 | 私聊直接响应；群聊**只认唤醒词（`AGENT_WAKE_WORDS`）或 @机器人**。旧 `ai/!ai//ai` 前缀已移除，不要恢复；触发判定与剥前缀共用 `wakewords.py` |
| 存储双实现 | `InMemoryMemoryStore` 与 `PgMemoryStore` 必须**语义一致**，由 `tests/test_store_contract.py` 参数化锁死。改任一侧必须两侧都改并跑带 `TEST_DATABASE_URL` 的套件 |
| 非正 limit/top_k | 所有 `limit` / `top_k` 参数：非正值 → **空结果**（不是"去掉最后 N 条"，也不是 DB 报错） |
| 记忆作用域 | 会话键命名空间化（`p:<uid>` / `g:<gid>:<uid>`）；facts 按 `(user, session)` 作用域隔离 + 同作用域内容去重；`list_facts` **最新优先** |
| KB | `kb_add_chunks` 同来源内容去重（含批内去重），返回**实际写入条数**；蒸馏水位线 = **最新一条** distill 来源记录的进度 |
| 不可信内容 | 外部文本（引用/转发/网页/检索结果/知识块/长期事实）进 prompt 前必须过 `agentcore/safety.py` 的围栏与 `rag/sanitize.py` 的脱敏；围栏行本身不得被内容闭合 |
| 沙箱 | `agentcore/workspace/runner.py` 命令白名单：**无 shell**、禁 `|;&><`；`zip`/`curl` 等分支对参数逐项校验（曾经有 `-T -TT` RCE 与裸内网地址 SSRF）；IP 判定含私网/回环/链路本地/保留段/`100.64.0.0/10` |
| 出站投递 | 长回复分层（单条 → 合并转发 → 文件）；**结果不确定时绝不重发**（超时/断连 → `FILE_UNCERTAIN`，见 `is_uncertain_send_error`）；合并转发段数超上限先"重打包"而不是逐条刷屏 |
| 停机 | 顺序固定 `scheduler → debounce flush → store.aclose`，且幂等（反了会在连接池关闭后 flush，重启必丢消息） |
| 成本 | LLM/embedding 调用上报 usage；预算软上限到点直接返回未发送结果（该分支**不打 WARNING**，是设计而非 bug） |

---

## 5. 已踩过的坑（别重犯）

| 现象 | 根因 / 教训 |
|---|---|
| CI 红了 7 个提交才发现 | `_last_error_notify` 用 `0.0` 当"未通知过"哨兵，而 `time.monotonic()` 是**开机时长** → 刚启动时第一次告警被当成冷却期内。**"未初始化"的哨兵必须用 `None`**；告警类逻辑必须有"首次必发"用例 |
| 一批用例是"恒真断言" | 典型：`assert "某字符串" not in caplog.text`，而该字符串**全仓不存在**；`assert p["text"].strip()`（换成任意常量都通过）；只断言默认值（把参数改成忽略默认值也通过）。加固方式：断言"改坏实现后必然失败" |
| 单文件变异得出"无覆盖"的错误结论 | `permissions.is_allowed` 在单文件变异下看似没覆盖，实际由 `test_workspace_skills` 覆盖。**判定覆盖要看全量套件** |
| `zip -T -TT` / `curl 2130706433` | 白名单式参数校验必须**穷举**（禁 `-T/--unzip-command`、禁含 `=` 的参数；所有非选项参数都当 URL 校验），黑名单必然漏 |
| `pow(2, 999999999)` 冻结事件循环 | 纯 Python 的"静态安全计算"必须同时限制**输入规模**（指数上限、运算计数） |
| `httpx` 超时被判"未送达" | 超时/断连属于**结果不确定**：既不能重发也不能当失败降级，否则用户收到两遍 |
| asyncpg JSONB 的 `tool_calls` 变成字符串 | 出库必须反序列化（`_deserialize_tool_calls`），否则下一轮 LLM 收到 `invalid type: string` |
| 全角数字 `"１２"` 通过 `isdigit()` | 需要 ASCII 的标识（QQ 号等）必须 `text.isascii() and text.isdigit()` |
| 文档数字过期 | BACKLOG 头部的测试/行数统计必须用命令实测后写入（`pytest -q`、`wc -l`），不要手写估算 |
| `FinishedException` 被 `except Exception` 吞 | NoneBot 的流程控制异常必须在最前面 `raise` |

---

## 6. 文档地图

| 文件 | 内容 |
|---|---|
| `README.md` | 用户视角：配置项、功能、运维命令、里程碑 |
| `CONTRIBUTING.md` | 开发环境、风格、PR 规范 |
| `AGENTS.md`（本文件） | 给接手者/Agent 的边界、纪律、不变量、坑 |
| `BACKLOG.md` | 还没做的事（§1–§5）+ 已实证缺陷（§6，含 §6.1 规范审查、§6.2 小项） |
| `docs/agent-demo-memory-architecture.md` | 记忆/存储设计 |
| `review/REVIEW-*.md` | 各轮评审报告（按 commit 区间命名，问题都带复现证据） |
| `review/FIX-*.md` | 对应修复记录：改法、验证命令、剩余项 |
| `review/REVIEW-WORKFLOW.md` | 多子代理评审 → 主代理复核的流程约定 |

---

## 7. 交付前检查清单

- [ ] 默认套件绿：`.venv/bin/python -m pytest -q`
- [ ] 动过 `memory/store.py`、备份/恢复、归档 → 带 `TEST_DATABASE_URL` 再跑一遍
- [ ] 动过热路径/缓冲 → `RUN_PERF=1 ... tests/test_perf.py -q`（必要时更新 `perf/baseline.json`）
- [ ] `ruff check` + `ruff format --check` 全绿（版本与 pin 一致）
- [ ] 新增/修改用例都做过"改坏实现 → 必须失败"的复核
- [ ] 行为变更已同步 README / BACKLOG / `review/FIX-*.md` 的描述与数字
- [ ] 没有顺手重构、没有自动 commit/push
