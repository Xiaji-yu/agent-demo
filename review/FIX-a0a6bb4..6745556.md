# FIX-a0a6bb4..6745556（对应 [REVIEW-a0a6bb4..6745556.md](REVIEW-a0a6bb4..6745556.md)）

**修复日期**：2026-10-16　**验收口径**：默认套件 2454 passed / 58 skipped；PG 全量（qqagent_test）2522 passed / 9 skipped；ruff check + format 双绿（0.9.6）。

## H1（阻断项）白名单展平 flag 击穿条目表执行位清理

- **修法**：`--strip-components` 移出 `_TAR_SAFE_FLAG_KEYS`、`-j` 移出 `_UNZIP_SAFE_FLAGS`（runner.py）；模块 docstring 与注释同步移除原因。能力非必需，解压前拒绝。
- **回归**：`tests/test_runner_security.py::TestH1FlattenFlagsRefused`——4 形态 permitted 负例（`--strip-components=1` 等号/空格两形态、`unzip -j` 两种组合）+ tar/unzip 端到端「拒绝且零落盘」2 条；原白名单正向断言 2 处翻负（:1168/-j、:1266/--strip-components）。
- **变异复核**：把两 flag 加回白名单 → 6/6 红；还原后绿。
- **评审 §1 复现脚本翻转验收**：`tar --strip-components=2` / `unzip -j -o` 实包攻击 → 「拒绝执行：tar 仅允许…」/「拒绝执行：unzip 仅允许 ['-d','-n','-o','-q']…」，工作区零落盘（修复前 0755/0777 存活）。
- **文档同步**：BACKLOG §6.9 新增；README 未枚举解压子 flag（:807 仅 `unzip -d`），无需改。

## L 必修 12 条（全部落地）

| # | 修法 | 回归/验证 |
|---|---|---|
| L1 | `plugins/qq_agent_adapter/__init__.py`：jieba 预热任务改存模块级 `_BACKGROUND_TASKS` 强引用 + done_callback discard（仿 growth._spawn/debounce 纪律） | 静态复核 + 模块导入断言；GC 行为无确定性测试面（诚实注记） |
| L2 | runner.py:232 注释「超限告警放行」→「超限**即拒**，fail-closed」 | 注释类，无测试面 |
| L7 | engine.py stale 判定补跨时钟源注记；`test_store_contract.py` 删「ts 单调不减」断言（部署假设不入契约），保留 float/>0 | 契约测试双跑通过（默认 159 相关 + PG 套件） |
| L8 | builtin.py low 档日志后缀改「（low 档：仅只读三件，控制类未注册）」 | 由 L9 守卫覆盖清单半边；后缀文案无独立断言（诚实注记） |
| L9 | `tests/test_action_skills.py::TestStartupLogListGuard`：日志串字面清单 == low 档无条件注册集 | **变异复核**：日志串删 docker_stats → 1/1 红；还原后绿 |
| L10 | conftest 夹具重排 load_dotenv(:63)→guard(:84)→strip(:114)；guard/strip docstring 按真实机制改写 | `.env` 来源 DATABASE_URL 的比对自此实际生效（定义序即实例化序）；test_db_skills 37 passed |
| L11 | docs/GENERAL-GUIDELINES.md 行数快照按归档后 `wc -l` 实测更新（232 → 见文中数值，含本 FIX 索引行） | 实测更新 |
| L12 | test_shell_skill.py:81-82 双重 `@pytest.mark.asyncio` 删一个 | 用例照常通过 |
| L13 | runner.py 两处 root 保留分支对 `removed<0` 渲染「扫描超限，无法确认是否含符号链接」，不再出现「-1 个」 | 不可达防御分支，文案审查 + 用例全绿 |
| L14 | store.py `_pair_sim` docstring 如实化（仅内存侧锁定；与 PG NaN 口径有意分歧）+ PG score 表达式补注释 | 注释类；契约/内存用例全绿 |
| L15 | store.py kb_search `sources = dict(self.kb_sources)` 快照化 + 注释如实（append 者非重建者） | test_memory/test_store_contract 全绿 |
| L16 | engine.py 预 embed 失败路径补代价注记（3 次尝试 vs 成功 1 次，语义不变） | 注释类，无测试面 |

## 遗留（4，显式入档，非本批修复）

- **L3** `_CONTAINER_RE` 首字符允许 `-`：后果仅 flag 歧义报错，不构成逃逸；随下次 runner/skills 触碰顺带收紧（首字符锚定字母数字）。
- **L4** `GrowthManager.aclose` 无调用方：接线属停机语义决策（取消在飞 _propose 的时机与 scheduler/flush 顺序相关），与下次 lifecycle 触碰同批。
- **L5** self 引用标注在 get_msg/raw_message 兜底路径缺失：需 `resolve_quoted_media` 扩展返回 sender.user_id（跨模块接口），并同步 engine 措辞加「若可识别」。
- **L6** `AGENT_DEBUG_RAW_DUMP` 全文落日志：默认关 + 显式 env + 仅群聊已缓解；限长取舍随下一次日志卫生整理。

## 验收汇总

- 默认 `pytest -q`：**2454 passed / 58 skipped**（+7 新回归）；PG 全量：**2522 passed / 9 skipped**。
- ruff check + format --check：双绿（0.9.6，与 pin 一致）。
- H1 复现脚本：由「可复现（0755/0777 存活）」变「不可复现（解压前拒绝、零落盘）」。
- BACKLOG：§6.9 入档 + DOCS-STATS 计数块经 `--write-stats` 重生成（67 文件 / 2512 收集：2454+58）。
- 未 commit / 未 push（等用户明确指示）。
