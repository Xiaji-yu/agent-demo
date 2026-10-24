# 修复记录：REVIEW-ee39c8e..e55cab5（1H / 9M / 14L）

**对应评审**：[REVIEW-ee39c8e..e55cab5.md](REVIEW-ee39c8e..e55cab5.md)
**修复日期**：2026-10-25
**范围**：H1、M1–M9，L 批 11 条随修（L1–L6、L8、L9、L11–L14）；L7/L10 显式遗留，L0 为流程缺口（见 §4）。

---

## 0. 结论

| 项 | 状态 | 关键验证 |
|---|---|---|
| H1 迁移异常静默降级内存 | **已修** | 失败注入用例 + 变异 2 处（migrate / repair 各一）被抓 |
| M1 `/memory` 跨会话回显/删除 | **已修** | 群会话负例（list/forget 不出私聊事实） |
| M2 `.age` sidecar 命名失配 | **已修** | 规范名 + 旧名兼容 + U 盘必含清单，6 条用例 |
| M3 恢复后账本残留 | **已修** | 恢复前清账本，JSONL/PG 两路用例 |
| M4 CLI 维度与运行时不一致 / 修复非原子无锁 | **已修** | 共用 `probe_dim`；TRUNCATE+ALTER 同事务；repair 取锁 |
| M5 日志正文开关只覆盖适配层 | **已修** | helper 下沉 `agentcore/logging_setup.py`，engine 置 0 断言 |
| M6 deploy 绕过 uv.lock | **已修** | 改 `uv sync --locked`，缺 uv 显式失败（无 pip 回退） |
| M7 stats 四表不在迁移链 | **按评审方案 B 处置** | AGENTS 登记例外 + BACKLOG §2.8 + 表清单漂移用例 |
| M8 CI `--frozen` 不校验新鲜度 | **已修** | `uv sync --locked` + 所有 `uv run --no-sync` |
| M9 5 个未跟踪前提文件 | **已修** | 已入库；工作区无 `??` |
| L1–L6、L8、L9、L11–L14 | **已修** | 见 §2 |

---

## 1. H1：迁移/修复执行期异常统一 `MigrationError`

**改法**

- `agentcore/memory/migrate.py`：`migrate()` 整体包一层 `except Exception → MigrationError("migration failed: …") from exc`；
  `MigrationError` 原样透传。覆盖 DDL 语法/权限、SQL 文件读取、取锁后连接中断等全部执行期失败。
- `agentcore/memory/schema_repair.py`：同样包装为 `MigrationError("schema repair failed: …")`。
  dedupe / 唯一索引 / HNSW 各自的容错分支不变（这三类允许降级），只有 vector 维度 SQL、
  取锁等"schema 不可用"的失败向上抛。
- `plugins/qq_agent_adapter/__init__.py`：`_init_memory` docstring 如实化——建池阶段故障仍降级内存，
  进入迁移/修复后的任何失败都 fail-closed。

**回归**

- `tests/test_migrations.py::test_migration_sql_error_is_wrapped_as_migration_error`
- `tests/test_migrations.py::test_repair_vector_sql_error_is_wrapped_as_migration_error`
- `tests/test_migrations.py::test_pg_store_init_propagates_migration_error_and_closes_pool`
  （fake pool：断言抛出的确实是 `MigrationError`、`__cause__` 保留、池被关闭、`store.pool is None`）
- `tests/test_lifecycle.py::TestInitMemoryFallback::test_migration_error_fails_closed`
  （启动路径不返回 `InMemoryMemoryStore`）

**变异复核**：把两处包装改回 `raise` → 3 条用例红；还原后复绿。

---

## 2. M 批

| # | 改法 | 回归 |
|---|---|---|
| M1 | `memory_route.py` 用 `resolve_session` 得到当前会话键，`list_facts_detailed` / `delete_fact` 显式传 `session_id`；`clear` 保持跨会话（用户行使删除权的出口），帮助文案写明。L5/L11/L13 随修：接线测试、确认码成功后才消费、群会话负例 | `tests/test_memory_route.py`（会话作用域 / 群负例 / 接线） |
| M2 | `db_backup.py` 新增 `_schema_sidecar_path`（`.age` 去后缀命名）+ `_schema_sidecar_candidates`（规范名→旧加密名→旧通用名）；写、读、删、镜像统一走规范名；`backup_usb.sh` 复制 sidecar 进必含清单。L1（prune 漏删 sidecar）、L4（sidecar 损坏改为 fail-loud）随修 | `tests/test_backup.py`（加密/旧名/损坏/U 盘） |
| M3 | restore 载入前执行 `_CLEAR_MIGRATION_LEDGER_SQL`（表存在才 DELETE），恢复后由首次启动按基线重建 | `tests/test_persistence.py::test_jsonl_restore_clears_ledger_first` 等 |
| M4 | `scripts/migrate.py` 新增 `_resolve_dim()`：与运行时同源 `probe_dim()`，失败回落配置并告警；`status` 保持离线只用配置。`repair_schema` 全程持 advisory lock；`TRUNCATE+ALTER` 同事务 + `lock_timeout`（L14 随修） | `tests/test_migrations.py`（dim 同源/回落、事务、锁跳过） |
| M5 | 开关与截断下沉 `agentcore/logging_setup.py`（`log_content_enabled` / `log_text`）；matcher 改为别名，engine / facts / distill / nudge / reminder 全部改走 `log_text`。L12（step/skill 参数无上限且不走开关）随修 | `tests/test_logging_setup.py`、`tests/test_engine.py::test_content_switch_redacts_step_and_skill_args`；变异：还原 engine 两处日志 → 用例红 |
| M6 | `scripts/deploy.py::cmd_install` 改 `uv sync --locked [--extra dev] [--reinstall]`；缺 uv 明确失败并给安装指引，**不回退 pip**；lock 不一致时提示 `uv lock` | `tests/test_deploy.py::TestInstallUsesUvLock`（4 条）；变异：argv 改 pip + 去掉 uv 守卫 → 3 条红 |
| M7 | 评审允许的方案 B：`AGENTS.md` §4 已把 stats 四表列为登记例外；`groupstats/store.py`、`raw.py` 的 `DDL` 上方注明来源与漂移守卫；BACKLOG §2.8 登记"纳入迁移链"待办 | `tests/test_migrations.py::test_stats_tables_match_embedded_ddl`；变异：从 `STATS_TABLES` 删一行 → 用例红 |
| M8 | CI 安装改 `uv sync --locked --extra dev`；5 处 `uv run` 全部 `--no-sync`，防止运行期自动重锁 | 本地 `uv lock --check` exit 0（72 包）；CI 实测见 §3 |
| M9 | 5 个前提文件随功能提交入库（`923c0b8`/`2efa9e7` 等），`git status` 无 `??` | `git ls-files` 核对 |

---

## 3. L 批与验证

**L 批**：L1/L4→M2，L2/L3/L14→M4，L5/L11/L13→M1，L12→M5，L6 parity/web 金标加固，L8 `text_len`→`<len=N>`，L9 `ENV_SPECS` 173→176（17 簇，实测）。L7（bool env 脏值 fail-open）与 L10（age 子进程取消清理）按评审结论显式遗留。

**L6 加固细节**

- `tests/test_store_parity.py`：`>=40` 下限 → 抽象方法集合必须与 `BaseMemoryStore` 声明集合**相等**；
  新增 runtime spy 证明内存侧真的调用共享 `_unique_chunk_indices`（内联复制骗不过）。
- `tests/test_web.py`：路由金标在 `(method, path)` 之外，断言除登录页外每条路由都挂 `_auth`，
  且不允许重复注册。变异：摘掉 overview 的鉴权依赖、重复注册同路径，均被检出。

**验证（实测）**

| 项 | 结果 |
|---|---|
| 默认套件（Windows） | `3013 passed / 187 failed / 89 skipped`；失败清单与 75e75ca 干净 worktree **逐条一致**（187 条均为存量 Windows 环境失败：chmod 0600、无 rsync/psql/age、无 `os.O_NOFOLLOW`），新增 35 条全部通过 |
| CI 基线（Linux，run 40128979023 @75e75ca） | `3254 收集：3181 通过 + 73 跳过`；本树 +35 条平台无关用例 → `3289 收集：3216 通过 + 73 跳过`（BACKLOG 计数块按此更新，由 CI 复核） |
| ruff check / format --check | 全绿（版本与 pin 一致） |
| pyright agentcore | 0 errors |
| `uv lock --check` | exit 0（72 包） |
| PG 集成 / POSIX age | 本机无法运行（**unknown**），由 CI 的 pgvector service 复核 |
| `check_review_docs.py` 结构自检 | 通过；`--check-stats` 因 Windows 存量失败无法本地执行，数字取自 CI 实测 + 本树收集增量 |

**变异复核汇总**：H1×2、M5×1、M6×1、M7×1、L6×3（parity 集合 / spy 内联复制 / web 鉴权与重复注册），全部被抓后还原。

---

## 4. 行为变更（需知会）

1. **H1**：迁移/修复执行期错误从"降级内存继续跑"改为"启动失败"。临时数据库故障出现在建池之后，也会 fail-closed（systemd 重启即可）。
2. **M1**：`/memory` 列表与 `/memory forget` 收窄到当前会话；`/memory clear` 保持跨会话。
3. **M3**：restore 会先清空目标库的 `schema_migrations`（表存在时），再由启动期按基线重建。
4. **M4**：`migrate.py up/repair` 会联网探测 embedding 维度（失败回落配置）；`status` 仍离线。
5. **M6**：一键部署要求 uv，不再支持 pip 回退。
6. **M5**：`AGENT_LOG_CONTENT=0` 现在同时覆盖引擎侧日志（step 内容、技能参数、facts/distill 原文、提醒正文）。

---

## 5. 遗留

- **L0**：`9c2d3c1..ee39c8e` 18 个 commit 尚无独立 REVIEW，建议下轮补 `REVIEW-9c2d3c1..ee39c8e.md`。
- **L7**：bool env 脏值（`disabled`、`False\n`）仍按开启处理；待全仓 bool 解析口径统一时一并处理。
- **L10**：age 子进程在 `CancelledError` 下的清理仅 POSIX 可复现，留待 age 集成用例补充。
- **M7 后续**：stats 四表仍在内嵌 DDL，纳入版本链见 BACKLOG §2.8。
