# agent-demo 近期 Commit 评审报告
**评审范围**：733f57e..46c85d1（当前 HEAD）
**评审日期**：2026-09-12
**评审方式**：静态审查 + 主代理实证（ruff check、ruff format --check、pytest -q、关键模块抽样复核）
**工作区状态**：`main` 分支，HEAD=46c85d1，`docker-compose.yml` 有未提交修改；`.env` 本地文件存在（gitignore）

## 0. 结论摘要

| 主题 | 结论 |
|---|---|
| 静态检查 | **verified**：ruff check / ruff format --check 全绿，版本 0.9.6 与 pyproject.toml pin 一致 |
| 默认测试套件 | **verified**：993 passed / 43 skipped / 1 failed |
| 失败用例根因 | **verified**：本地 `.env` 含 `AGENT_KB_DISTILL_TOTAL_CAP=20000`，被 `tests/conftest.py` 的 `_normalize_superusers_env`（session fixture，`load_dotenv(.env)`）泄漏进测试会话，污染配置优先级断言；删除该变量后测试通过 |
| 上一轮 H/M 修复回归 | **verified**：REVIEW-c472e56..733f57e 的 5H / 19M 在当前 HEAD 未发现回退 |
| 新增 H 级问题 | **high**：静态抽样未发现新增可利用逃逸/注入/越权/数据丢失 |
| 未验证面 | **unknown**：本地无 PostgreSQL，PG 门控用例（test_store_contract / test_pg_store / backup restore）未在本机复现 |
| 依赖 CVE | **unknown**：无在线搜索能力，httpx / pillow / asyncpg / python-dotenv 的已知 CVE 未查 |

## 1. 已实证的问题

### M1（verified）：测试环境隔离缺陷 — `.env` 通过 conftest 泄漏进测试会话

- **位置**：`tests/conftest.py::_normalize_superusers_env` + 本地 `.env`
- **证据**：
  - `.env` 含 `AGENT_KB_DISTILL_TOTAL_CAP=20000`、`AGENT_KB_DISTILL_PER_MESSAGE_CAP=1000`
  - `_normalize_superusers_env` 在 session 作用域执行 `load_dotenv(Path(".env"), override=False)`，将这些变量注入 `os.environ`
  - `test_total_cap_floored_above_per_message_cap` 传入 `{"distill_total_cap": 100}`，期望被 floor 到 `per_message_cap * 2 = 2000`；实际 `os.getenv("AGENT_KB_DISTILL_TOTAL_CAP")` 返回 `.env` 的 `20000`，绕过 config dict
  - 删除 `.env` 中这两行后，该测试通过
- **影响**：测试对开发者本地 `.env` 有隐式依赖，导致配置优先级/隔离类断言可能在部分环境假失败；CI 若无 `.env` 不受影响
- **建议**：
  1. 在 `_normalize_superusers_env` 加载 `.env` 后，显式清理 `AGENT_KB_*` 等测试相关变量；
  2. 或在 `tests/test_rag.py` 的 `_clean_kb_env` fixture 中补充 `AGENT_KB_DISTILL_TOTAL_CAP` / `AGENT_KB_DISTILL_PER_MESSAGE_CAP` 的 `monkeypatch.delenv`

### L1（high）：BACKLOG.md 基线数字过期

- **位置**：`BACKLOG.md:4-6`
- **证据**：
  - 基线写“测试 37 个文件 14.0k 行”
  - 当前实测：`tests/` 下 36 个 `test_*.py`，15648 行；全仓 `agentcore/plugins/tests` 共 31013 行
  - `pytest -q --collect-only` 收集 1037 个测试（基线写 940）
- **影响**：文档数字失真，影响后续评审的“已覆盖/未覆盖”判断
- **建议**：按 AGENTS.md 纪律，用实测命令刷新数字

### L2（high）：README RAG 节未显式 documented 蒸馏 cap 环境变量

- **位置**：`README.md:295-310` vs `.env.example:199-200`
- **证据**：README 详细描述了蒸馏脱敏流程，但未列出 `AGENT_KB_DISTILL_PER_MESSAGE_CAP` / `AGENT_KB_DISTILL_TOTAL_CAP`；`.env.example` 与 `config.yaml` 已有定义
- **影响**：低——功能不受影响，但配置可发现性下降
- **建议**：在 README RAG 节或 `.env.example` 注释中显式说明这两个 cap 的作用与默认值

### L3（high）：依赖版本未完全锁定

- **位置**：`pyproject.toml`
- **证据**：所有 runtime 依赖使用 `>=`（如 `httpx>=0.27`、`pillow>=10.3.0`），无 `==`/`~=`/`^`；未发现 `latest` 裸版本
- **影响**：小版本漂移可能导致本地与 CI 行为差异（ruff 版本漂移已在历史报告中出现）
- **建议**：考虑引入 `pip-compile` / `uv.lock` 等锁文件机制，或在 CI 中显式安装 pin 版本

## 2. 被证伪的发现

本轮审查未发现需要证伪的子代理结论（无并行子代理，主代理直接实证）。

## 3. 测试与文档状况

### 3.1 实跑数据

| 检查项 | 结果 |
|---|---|
| ruff check | **verified**：All checks passed |
| ruff format --check | **verified**：109 files already formatted |
| pytest -q | **verified**：1 failed, 993 passed, 43 skipped |
| 失败用例 | `tests/test_rag.py::TestReviewC472IngestFixes::test_total_cap_floored_above_per_message_cap` |
| 失败根因 | **verified**：本地 `.env` 泄漏 `AGENT_KB_DISTILL_TOTAL_CAP=20000`（见 M1） |

### 3.2 CI 盲区与未验证面

| 未验证面 | 等级 | 说明 |
|---|---|---|
| PG 门控用例 | unknown | 本地无 PostgreSQL 服务，`test_store_contract.py`、`test_pg_store.py`、backup/restore 集成测试未在本机复现。据 BACKLOG/CI 描述，CI 已配置 pgvector service，但本次审查无法本地验证 |
| 依赖 CVE | unknown | 无在线搜索能力，httpx/pillow/asyncpg/python-dotenv 的已知 CVE 未查 |

### 3.3 文档一致性

| 文档 | 状态 |
|---|---|
| README.md vs config.yaml | 大部分一致；蒸馏 cap 环境变量未显式 documented（L2） |
| .env.example vs config.yaml | `distill_per_message_cap=1000` / `distill_total_cap=20000` 一致 |
| BACKLOG.md 数字 | 过期（L1） |
| AGENTS.md 不变量 | 代码未违反已记录的不变量 |

## 4. 已验证为「无问题」的关键项

- **安全面**：无 `shell=True`、无 `eval/exec`、SQL 全参数化、subprocess 使用 `asyncio.create_subprocess_exec`（无 shell）
- **沙箱**：`workspace/runner.py` 的 zip/curl/git 白名单与上一轮修复一致，无回归
- **隐私**：蒸馏/检索/摘要注入均通过 `fence_untrusted` / `neutralize_fence_lookalikes`；`messages_after` 默认排除私聊
- **正确性**：
  - 存储双实现（内存/PG）的 `limit/top_k` 语义一致：非正值返回空（`get_history` 系列按 1 处理，与 PG `LIMIT 0` 语义对齐）
  - `kb_add_chunks` 同来源内容去重（含批内）双实现一致，返回实际写入数
  - `list_facts` 最新优先双实现一致
  - `kb_last_digest_watermark` 取最新一条 distill 来源双实现一致
- **资源**：停机顺序固定为 `scheduler → flush debouncer → memory aclose → extra_closers`，且幂等；flush deadline 原生支持
- **测试**：`tests/test_store_contract.py` 参数化锁双实现；对抗样例覆盖 H2/H3/H4/M1/M2 等历史缺陷

## 5. 与在库报告的衔接复核

| 上轮报告 | 覆盖范围 | 修复状态 |
|---|---|---|
| REVIEW-c472e56..733f57e | 5H / 19M / 多项 L | **未发现回退**：zip/curl/pow/httpx 超时/lifecycle 顺序、存储契约、备份/蒸馏、并发等修复在当前 HEAD 保持完整 |
| FIX-c472e56..733f57e.md | 对应修复记录 | 修复映射全部对上，无遗漏回归 |

## 6. 修复优先级建议

| 优先级 | 项 | 建议 |
|---|---|---|
| P1 | M1：测试环境隔离 | 在 `_normalize_superusers_env` 或 `_clean_kb_env` 中清理 `AGENT_KB_*` env，保证任意本地 `.env` 下测试可重复 |
| P2 | L1：BACKLOG 数字 | 用 `pytest -q --collect-only` 与 `wc -l` 实测刷新 |
| P2 | L2：README 蒸馏 cap 文档 | 显式列出 `AGENT_KB_DISTILL_PER_MESSAGE_CAP` / `AGENT_KB_DISTILL_TOTAL_CAP` |
| P3 | L3：依赖锁定 | 考虑引入锁文件机制，减少小版本漂移风险 |
| P3 | 依赖 CVE | 有网络条件时扫描 httpx/pillow/asyncpg/python-dotenv 的已知 CVE |
