# FIX — 群聊访客模式 + cron/人格出沙箱（P0–P3，2026-10-29）

> 来源：`review/AC-group-guest-mode.md`（v2）。分支 `fix/guest-mode-p0`，**未提交**。
> P0 是两条既有缺陷的直接修复；P1–P3 按 AC 的 D1–D6 逐项落地（见 §8–§11）。

## 1. 行为变更（从 X 改为 Y）

| # | 变更 | 之前 | 之后 |
|---|---|---|---|
| 1 | cron `agent_turn` 群目标 | **完全不看白名单**：文档承诺"不在 `ALLOWED_GROUPS` 不会投递"，代码零校验；探针实测向白名单外群投递成功、状态 `ok` | 群目标必须命中白名单，否则**不跑引擎、不投递**，job 记 `failed` + WARNING（按 job 去重），仍推进下次执行时刻。空集 fail-closed。私聊目标不受群白名单约束 |
| 2 | 工作区根下 `SOUL.md` / `personas` / `cron` | `fs_write`/`fs_mkdir`/`fs_delete` 可随意改写（探针实测 `fs.write("cron/jobs.json")`、`fs.write("SOUL.md")` 成功）→ 提示注入可直接改默认人格 / 给自己加定时任务 | 三个名字为**保留名**（大小写不敏感，仅根级）：写/建目录/删除一律 `ValueError`；读取仍允许（旧布局迁移与排障） |

## 2. 实现

- `agentcore/scheduler/agent_turn.py`
  - `AgentTurnRunner(..., allowed_groups=...)`：缺省空集 = 拒绝所有群目标（漏注入必须是"不发"
    而不是"全发"）。`_target_allowed()` 私聊放行、群目标查集合。
  - ACL 判定在 `engine.run` **之前**；拒绝时 `update_state(status="failed",
    next_run_ms=...)` —— 不推进下次时刻会让 tick 每 60s 重试一次（日志与 `.bak` 轮转刷屏）。
  - `_acl_warned: set[(job_id, target)]` 独立去重，不复用 `_last_error`（两者恢复条件不同）。
- `plugins/qq_agent_adapter/__init__.py`：`_build_cron_jobs` 注入 `acl.ALLOWED_GROUPS`
  （`agentcore` 不 import `plugins` 的分层铁律保持）。
- `agentcore/workspace/fs.py`：`_RESERVED_ROOT_NAMES` + `MSG_RESERVED_NAME`，检查落在
  `_assert_writable()`（write / mkdir / delete / delete_abs 共用同一入口），先于 git 保护判定。

## 3. 用例

- `tests/test_scheduler_jobs.py::TestTargetACL`（5 条）：白名单外 → failed/零引擎/零投递/
  WARNING 含 job id/下次时刻推进；白名单内 → ok；空集 → 拒绝；私聊 → 放行且
  `group_id == ""`；ACL 告警按 job 去重（两次执行只 1 条）。
- `tests/test_workspace.py::TestFsReservedNames`（7 条）：写 `SOUL.md`、写 `cron/jobs.json`、
  大小写变体、`mkdir personas`、`delete`/`delete_abs`、嵌套同名允许、读取允许。
  正向对照：失败用例断言**规范位置字节不变**（只断言"抛异常"会漏掉"先写后抛"的实现）。

## 4. 变异复核

把两处判定分别改成 `if False`：**8 条红灯**（`TestFsReservedNames` 5 条 +
`TestTargetACL` 3 条），4 条正向对照仍绿；还原后全绿。与"改坏实现 → 用例必红"的纪律一致。

## 5. 验证

- 目标文件：`pytest tests/test_workspace.py tests/test_scheduler_jobs.py -q` → 99 passed /
  1 failed（`test_write_created_0600`，Windows 无 POSIX 权限位，属存量失败）/ 1 skipped。
- 全量默认套件：`179 failed / 3349 passed / 84 skipped`（本机 Windows）。把新判定改回
  `if False` 后失败数**仍是 179** ⇒ 本次改动零新增失败；179 条均为存量 POSIX-only 类
  （`os.O_NOFOLLOW`、权限位、`age` 等），真门禁以 CI 为准。
- `ruff check` + `ruff format --check` 对改动文件全绿。

## 6. 残留 / 未验证（P0 时期）

- **CI 未跑**（分支未推送；按纪律不自动 push）。
- 访客白名单（D1）、cron/人格出沙箱（D2/D3）、会话隔离（M2）在 P0 时未做；P1–P3 已补（§8–§11）。
- 保留名目前只覆盖"历史落点"三个名字；`data/personas` 迁出后本项继续保留（防回迁、
  防"写了假的还以为成功"）。

## 7. 回滚

两处改动互相独立，revert 任一文件的改动即恢复旧行为（不涉及数据迁移）：
`agentcore/scheduler/agent_turn.py` + `plugins/qq_agent_adapter/__init__.py`（ACL）；
`agentcore/workspace/fs.py`（保留名）。

## 8. P1（D1）：技能 where 轴 + 访客白名单

- `Skill.guest`（默认 `False`，fail-closed）→ `register(..., guest=...)`、manifest `guest:`、
  `local.py` 转发；`PermissionChecker.is_allowed` 增加 `group_id` 维度：
  群内 superuser 只见非 superuser 技能 + `AGENT_GROUP_EXTRA_SKILLS` 的**只读**项；
  群内普通成员只见 `guest=True`。私聊行为不变。
- 判定轴是 `group_id` **实参**而不是 `current_route` contextvar：cron 回合的 route 是 `cron:<job>`，
  用 route 做安全轴会在"既不是群也不是私聊"的上下文里 fail-open。
- `guest=True` 站点：`calc`/`get_weather`/`now`/`date_calc`/`unit_convert`/`random`/`wiki_*`/
  `xiaoheihe_post`/`reminder_add|list|cancel`/`play_music`/`my_group_review`。
- 用例：`test_permissions.py::TestGroupTrustModel`（8）、`test_registry.py::TestGuestSurface`（5）、
  `TestGuestDocDrift`（3）、`test_manifest.py`（2）、`test_admin_import.py::TestGroupExtraSkills`（5）；
  `test_workspace_skills.py::test_group_context_denied_private_allowed` 取代旧"群内可发工作区文件"断言。
- 变异：删 `or not guest` → 3 红；`guest=skill.guest` 改 `guest=False` → 1 红；均还原后全绿。

## 9. P2（D2/D3）：cron 与人格出沙箱

- cron：`data/cron/jobs.json` + 同目录 `state.json`（0600 原子写）。`jobs.json` 不再回写
  `state`/`updatedAtMs`（配置与运行态分离：手改配置不再被运行态覆盖、备份不再每次 churn）；
  旧文件里的内嵌 `state` 读入后迁移到 sidecar（旧字段保留不删，回滚版本仍能读）。
- 人格：`data/personas/`（`SOUL.md` 默认 + `<name>/SOUL.md`）。workspace 是 LLM 沙箱可写面，
  提示注入本可改人格；迁移用 `agentcore/migrate_layout.py` **复制不搬**（旧位仍可读，回滚不丢数据），
  每次启动幂等跑、失败只 WARNING。
- 部署面（v1 漏项）：Dockerfile `mkdir data/cron data/personas` + 拷入模板；两份 compose 加逐目录挂载；
  `docker-entrypoint.sh` 的 `MOUNTS` 加 `cron personas`；`compose_init.DATA_DIRS` 加两目录并自举
  `data/personas/SOUL.md` 与 `data/cron/jobs.json`（模板 `docs/examples/cron-jobs.example.json`）；
  `backup_usb.sh` 冷备改拷 `data/personas`/`data/cron`（原来的 `agentcore/personas/*.md` glob 早已失效）；
  `.gitignore` 整目录忽略三处运行期数据；`git rm --cached` 三个曾入库的运行时文件。
- 隐私门禁：模板移到 `docs/examples/` 后必须扩 `_SHAPE_SCOPES`，否则模板里的号不再被规则 B 扫；
  已加 `docs/examples/` 并补 `test_workspace_still_scanned` 防"扩了范围把原范围丢了"。
- **写面语义（第二轮自查找回的真回归）**：第一版 `_write_base()` 写成「`dir/personas` 存在才写它」，
  显式 `PersonaManager(dir)` 会落到 `dir/<name>`——而升级前是 `dir/personas/<name>`，
  web 导入人格的既有用例（`TestPersonaWeb`，断言 `pm.dir/"personas"/"tester"/SOUL.md`）在 Linux 会红。
  本机之所以没暴露，是 2FA 的 `agentcore/totp.py:74` 先撞 Windows 缺失的 `os.O_NOFOLLOW`。
  现规则二分：默认 `data/personas`（`DEFAULT_PERSONAS_DIR`）平铺 `<名字>/SOUL.md`；显式 `PERSONAS_DIR`
  沿用 `personas/<名字>/`，老用户升级前后写入位置一致。`migrate_layout` 相应地在
  「写面就在旧位」时整段跳过人格复制（否则造出 `ws/personas/SOUL.md` 这种没人读的副本）。
  变异：默认分支改 `if False:` → 平铺用例红；迁移守卫改 `if True:` → 跳过用例红（返回了那个副本路径）。
- 用例：`test_scheduler_jobs.py::TestStateSidecar`（7）+ `TestCronTemplateExample`、
  `test_migrate_layout.py`（7：6 + 写面在旧位时跳过）、
  `test_personas.py::TestPersonaOutOfWorkspace`（5：含默认平铺 + 显式旧布局）、
  `test_compose_init.py`（模板自举 + 两份 compose 挂载 + Dockerfile 守卫）、
  `test_repo_guards.py::TestRuntimeDataNotTracked`（3）。

## 10. P3（D4/D6）：cron owner 上下文、会话隔离、pending-before-send、守卫

- owner 轴：`is_allowed(..., owner=True)` 直接放行。owner 上下文 = 私聊 private ∪ cron `agent_turn`
  （jobs.json 是 owner 写的主机配置）；群聊回合**永不带** owner。D1 与 D4 的冲突由此消解：
  收紧的是"群里"，不是"cron"。
- 隔离：cron 回合 `user_id="cron:<job>"`、`group_id=None` → 会话键落在 `p:` 命名空间，
  不借目标群历史，也不产生 `g:<gid>:<gid>` 自我对话键（facts.session_id 是整数，不能发明裸 `cron:` 键）。
- 显式抑制：`_remember_facts` 与 `growth.maybe_trigger` 在 `isolated=True` 时跳过——
  换会话键**不会**自动关掉这两个无条件调用，必须显式判。
- L3：投递前先落 `pending`（`nextRunAtMs` 同时推进），崩在投递中间不会重投；出站三态语义不变。
- 预算：`current_route` 取 `context["route"]`（`cron:<job>`），成本账能按任务归集。
- 用例：`test_engine.py::TestCronOwnerTurn`（4：facts/growth 抑制含正向对照、会话键隔离、route、
  owner 见 superuser 技能）、`test_scheduler_jobs.py::TestPendingBeforeSend`（2：投递瞬间 pending、
  崩溃不重投）。
- 变异：facts 抑制、growth 抑制、owner 透传、route、owner 旁路、pending 写入、pending 落点 —— 7 项
  逐一改坏均有用例转红，还原后全绿。

## 11. 门禁与残留

- 全量默认套件（本机 Windows，人格写面语义修正后）：`167 failed / 3423 passed / 84 skipped`（收集 3674）；失败逐条核对为存量
  POSIX-only（`os.O_NOFOLLOW` 残留、权限位语义、`age`/`df`/`zip` 的 msys 缺失）与 Windows 路径
  分隔符类，**无本次改动引入的失败**（改动前本机为 179 failed / 3349 passed；`extract` 的
  `os.O_NOFOLLOW` 收敛让 `test_archive_skills` 从 9 红降到 7 红）。CI 才是门禁，未推送故未跑。
- 剩余一条：`test_repo_guards.py::TestEnvDocs::test_real_tree_env_vars_documented` 只差
  `AGENT_GROUP_EXTRA_SKILLS=`（实测报错仅此一个名字）。该文件在本机被 DSH 凭据围栏挡住
  （`CREDENTIAL_DENIED`，`.env*` 被视为凭据位置），**不绕围栏**，改由用户手工加一行；
  同处建议把已有的 `AGENT_CRON_DIR` 值从 `data/workspace/cron` 改成 `data/cron`（不是测试门禁项，
  但不改就是文档漂移）。**2026-10-29 18:13 复核**：`.env.example` 仍只有 `AGENT_CRON_DIR=
  data/workspace/cron`（:531），无 `AGENT_GROUP_EXTRA_SKILLS`，故该用例仍红——等用户补。
- BACKLOG 计数块按「本机 167 条平台专属失败在 Linux 全绿」推断为 `94 文件 / 3674 收集：
  3601 通过 + 73 跳过`（收集数 `--collect-only` 实测 3674，跳过数沿用上次 CI 记录 73；
  本机 84 含 11 条 Windows 专属跳过）。
  该数字标 `unknown` 直到 CI 实跑；若漂移，在 CI 等价环境跑一次
  `scripts/check_review_docs.py --write-stats` 即可（勿手改）。
- **D2 守点换位**：AC v1 要求「扩 `TestSecretBearingPaths`」，但它的 `unignored_secrets()` 只认
  字面量/通配，读不懂 `.gitignore` 里 `data/*` + `!docs/examples/...` 的顺序语义，硬扩只能写成
  恒真断言。改为复用 `TestRuntimeDataNotTracked`（`git ls-files` 三目录为空 + `git check-ignore`
  认下四条运行期路径 + 模板仍被跟踪），直接问 git 本体。AC §3 D2 已从 ⚠ 改 ✅。
- **文档数字漂移（本轮顺带校准）**：`AGENTS.md` 配置行原写 `ENV_SPECS` 176 键 / 可写 114 env，
  实测为 **179 键 / 可写 113 env / 18 簇**（`len(ENV_SPECS)`、`sum(s.writable ...)`、`len(CLUSTERS)`）；
  其中 `HEAD` 已是 178 键，176 属更早的失真（本轮只新增 1 个 `AGENT_GROUP_EXTRA_SKILLS`，`writable=False`）。
  已改回实测值。这类「数字写在文档里」的漂移只能靠实测复核，别按上一次报告的结论照抄。
- `tests/test_backup.py::TestUsbScriptGuards`（5 条）在本机 spawn bash 失败（UTF-16
  `Bash/E_ACCESSDENIED`），存量环境问题，冷备脚本改动**未在本机实跑**，标 `unknown`。
- **补修（评审自查发现）**：`extract` 技能自己 `os.open` 落盘、不经 `WorkspaceFS._assert_writable`，
  于是 `dest="cron"` / `"personas"` 仍能在工作区造出「永不生效的假配置」，P0 的保留名守卫被它绕过。
  修法：`workspace/fs.py` 抽出公开 `reserved_root(rel)`（`_assert_writable` 复用），
  `archive_skills._resolve_dest` 解析目标后判 `reserved_root(p.relative_to(root))` → 返回
  「解压目标不能是保留名目录」。用例 `tests/test_archive_skills.py::TestExtractReservedDest` 5 条（4 拒 + 正向对照）
  （`cron`/`personas`/`SOUL.md`/`Cron/jobs` 拒绝且零落盘 + 正常 `cron_notes` 成功）。变异：
  `reserved_root` 改 `return False` → 4 红、撤回后 5 绿（1 条正向对照不受影响）。
  顺带把该模块两处 `os.O_NOFOLLOW` 收敛为 `getattr(os, "O_NOFOLLOW", 0)`（本机原先 9 条
  `test_archive_skills` 因 Windows 无该常量而红，属**早于本轮**的存量问题）。
- **补修（A5/B3）**：① `SkillRegistry` 拒绝分支加 `_warn_denied_once(name, group_id)`——按 `(技能, 群)`
  去重、私聊不告警、真正的 unknown 不告警，响应文案不变（无探测口）；用例
  `tests/test_registry.py::TestGuestSurface::test_group_denial_warns_once_per_skill_and_group`，
  变异（去掉调用）→ 红。② `AgentTurnRunner.tick()` 原先 `jobs_mod.load()` 读**默认路径**，而
  `update_state` 写 `self.jobs_file`，两者不同源：测试里传 `jobs_file` 后 tick 根本没读那份文件，
  `TestPendingBeforeSend::test_crash_during_send_is_not_redelivered` 的「tick 不重投」断言恒真。
  改为 `jobs_mod.load(self.jobs_file)`，并加 `TestHandEditReload::test_tick_reads_edited_file_without_restart`
  （手改文案 + 删状态侧车 → 下轮 tick 用新文案、投递 2 次）；变异（回退 `load()`）→ 红。
