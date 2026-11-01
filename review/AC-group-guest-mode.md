# AC — 群聊访客模式 + cron/人格出沙箱（信任模型重铸）

> 状态：**已定夺并部分实施（2026-11-01）**。
> **定夺记录**：① 写通道 = 「LLM 零通道」不变 + 三条人通道（SSH / web / **QQ 私聊命令**（新增 /cron 命令组与 /persona new|edit，两段确认+审计+备份））——比 D2 原案（仅 SSH/web）多一条 QQ 通道，均为人直接下命令、不经 LLM；② 批次顺序调整为 **P2（出沙箱）→ P3（agent_turn）→ P1（技能轴）→ P4（守卫）**（先拔对 owner 私聊也生效的 H1）；③ 互拷 = 单向导入 + `/openclaw import` 命令分拣（不搞镜像目录）；④ 目录按功能 `data/cron/` + `data/personas/`。
> **已实施**：cron 全链出沙箱（`data/cron/` + jobs.json 纯配置/state sidecar + 模板不入库 + USB 冷备必含）+ agent_turn 三修复（M1 投递白名单 / M2 会话隔离 `cron:<job_id>` / L3 崩溃窗口不重投）+ 人格落点 `data/personas/`（容器持久化 + 首启播种）+ QQ 命令通道 + 删除守卫同源判定（f76cadf②）。commit：`1b40383`/`323ca67`/`02fe6a3`/`a763165`（main）。
> **剩余**：P1 技能 where 轴（`sessions` 参数 + AGENT_GROUP_EXTRA_SKILLS）、P4 守卫文档收尾、memory 导入格式（另议）。原草稿正文保留如下（定夺差异以上述为准）。
> 原状态：草案（待评估）。来源：PR #6 评审（[REVIEW-pr6-3c00145.md](REVIEW-pr6-3c00145.md)）3H 的架构定夺
> + 2026-10-29 产品定位决策——**个人助理兼容群聊：owner 全功能，群友=访客（对话 + 有限共用 skill）**。
> 参照系：OpenClaw 的"单操作员 + 入口配对/白名单"外圈模型（其官方明确"共享 gateway 不是多租户边界"），
> 在 QQ 群聊形态下的等价物 = **who（superuser）× where（私聊）双因素**。
>
> 本文是"怎么算改好了"的验收规格。每条验收附**可执行断言**（测试名/命令）；
> 实施时逐条补变异复核结论（改坏实现 → 用例必红 → 还原）。

## 0. 信任模型（本 AC 的宪法）

| 会话 | 身份 | 技能面 | cron | 人格 |
|---|---|---|---|---|
| 私聊 | superuser（owner） | **全量**（含 fs/run/db/ssh/ops/archive） | 可用（文件/web 通道修改） | 可改（web 2FA / SSH） |
| 私聊 | 非 superuser | 访客面（同群访客） | 无 | 无 |
| 群聊 | superuser | **访客面 + `AGENT_GROUP_EXTRA_SKILLS` 白名单** | 无（修改只能回私聊/SSH） | 无 |
| 群聊 | 非 superuser | 访客面 | 无 | 无 |

**访客面** = 对话（含图片/引用/检索）+ `public` 且群可见的 skill（现状即满足者：reminder 三件套、
builtin 只读组、点歌、统计查询——准入标准见 D5）。

**威胁模型不变的部分（重要）**：本模型**不宣称免疫提示注入**——superuser 私聊会话里照样混着
不可信内容（引用/网页/检索，历轮 H 的来源）。因此 H1 的修复（配置出沙箱）**独立于**本 AC 的
技能收窄，两者是纵深关系：收窄挡"群内直接触发高危面"，出沙箱挡"任何会话被诱导改配置"。

## 1. 分层与变更面

| 文件 | 层 | 变更 |
|---|---|---|
| `agentcore/skills/registry.py` | 纯 Python | 注册参数 `sessions: "all"|"private"`；`execute` 前按 `current_route` 过滤 |
| `agentcore/loop/engine.py` | 纯 Python | 无改动（`current_route` 已在 :1798 set，技能执行点在其后） |
| `agentcore/scheduler/jobs.py` / `agent_turn.py` | 纯 Python | 目录改 `data/cron/`；投递白名单（M1）；会话隔离键 `cron:<job_id>`（M2）；state 拆 sidecar（M4） |
| `agentcore/personas/manager.py` | 纯 Python | 默认目录 `data/workspace` → `data/personas/`；web 面落点同步 |
| `plugins/qq_agent_adapter/__init__.py` | NoneBot | 接线路径常量；旧位置一次性迁移探测 |
| `.gitignore` | — | `data/cron/`、`data/personas/` 整目录忽略；回收 `data/workspace/{SOUL.md,personas/,cron/}` 放行 |
| `tests/` | — | A–D 组新用例；改 `TestRepoJobsFile`（入库锁→模板锁）；群白名单漂移守卫 |
| `AGENTS.md` / `docs/features-skills.md` | 文档 | §4 信任模型行 + 准入标准；技能按受众（owner / 访客）标注 |

**不碰**：DDL、reminder/push/nudge 既有链路、matcher 直连命令的 ACL（本就 superuser 门，与 where 轴互为纵深）。
**零新增依赖。**

## 2. 设计决策（评估重点：每条带取舍）

### D1 技能 where 轴：superuser-only 技能**默认群聊不可见**
- 注册参数 `sessions`，默认 `"all"`；但 `permission="superuser"` 的注册**自动视为 `"private"`**（31 处
  现有注册零标注即收窄），显式加开走 `AGENT_GROUP_EXTRA_SKILLS`（env，默认空）。
- 判据：`current_route` contextvar 前缀（`group:` vs `private:`）——engine 路径全覆盖；matcher 直连
  命令不经 engine，不依赖此轴（已有 ACL）。
- 拒绝文案与 unknown 同款（无探测口）+ WARNING 留痕（含会话键，对齐 2026-10-12 定夺）。
- **备选（已否）**：逐技能标 `group_ok=False`——31 处人工标注必漏（名单制在该仓库的历史表现：git 保护
  名单、M21 守卫漂移、M1 的 `_target_allowed` 缺失，全是同型复发）。
- **代价**：owner 在群里跑运维须回私聊（定位决策接受）；群聊中模型对高危请求会答"去私聊找我"
  （prompt 工作流补一句口径）。

### D2 cron 出沙箱 + owner-only：`data/cron/`，LLM 全通道不可达
- `jobs.json` **不入库**（含真实投递目标，H2 教训）；仓库带 `docs/examples/cron-jobs.example.json`
  模板 + compose 首启自模板复制。**推翻 PR 的 `test_repo_jobs_file_is_tracked`**（其"换机器任务全丢"
  诉求由模板+备份承载：jobs.json 纳入 USB 冷备必含清单）。
- state 拆 `data/cron/state.json`（gitignore，0600），`jobs.json` 回归纯配置（M4 收口）。
- 修改通道 = SSH 手改 / 将来 web 面（2FA+审计）。**无任何 LLM 技能可写**——与 OpenClaw 把 `cron`
  列入可 deny 高危工具同构，比其更严（直接零通道）。

### D3 人格出沙箱：`data/personas/`（0600，gitignore）
- 根级 SOUL.md 与 `personas/<name>/` 落 `data/personas/`；模板入库 `agentcore/personas/templates/`
  （PR 已建）。web 人格面（2FA+审计+备份）读写新落点，契约不动。
- `WORKSPACE_DIR`（`data/workspace`）回归**纯 LLM 工作产物**——沙箱语义单一化。

### D4 agent_turn 三修复（随出沙箱一并）
- **M1**：`run_job` 前置 `_target_allowed` 同款白名单判定（不在 `ALLOWED_GROUPS` → `failed` + WARNING，不投递）。
- **M2**：会话隔离键 `cron:<job_id>`（新增 `engine.run_isolated` 或 context `session_override`）——
  不读不写真实用户历史、growth 不触发、预算记 `cron` 路由。
- **L3 顺修**：sent 后先记 pending 再投递，崩溃窗口不重投。

### D5 访客面准入标准（写进 AGENTS.md §4）
一个 skill 对访客可见，须**全部满足**：① 确定性或资源有界；② 按 user 隔离（无跨用户信息流）；
③ 无持久化副作用（reminder 除外——它按 user 隔离+条数上限，是样板）；④ 输出过围栏/脱敏既有不变量。
现集：reminder 三件套 / builtin 只读组（now、date_calc、kb_search、wiki 等）/ 点歌 / 统计查询。

### D6 守卫（名单漂移的机械锁）
- 新测试：`test_group_skill_drift`——`AGENT_GROUP_EXTRA_SKILLS` 展开后的实际群可见集与
  `docs/features-skills.md` 访客清单 diff 为零（双向）。
- 新测试：`test_workspace_purity`——`data/workspace` 下不存在 SOUL.md/personas/cron 路径常量引用
  （防回迁）；`TestSecretBearingPaths` 清单扩 `data/cron/`、`data/personas/`（H3 一并收）。

## 3. 验收项（可执行）

### A 组：技能 where 轴
- **A1** 群聊 superuser 调 `fs_write` 被拒，文案与 unknown 技能一致（无探测口）
  `tests/test_group_guest.py::test_group_superuser_denied_fs_write`（FakeRegistry + `current_route` 设 `group:1`）
- **A2** 群聊访客 `reminder_add` 正常（public 不受影响）→ 同文件 `test_guest_public_skill_still_works`
- **A3** 私聊 superuser `fs_write` 正常 → `test_private_superuser_full_access`
- **A4** `AGENT_GROUP_EXTRA_SKILLS=system_status` 后群聊可见 system_status、其余仍拒 → `test_extra_skills_env`
- **A5** 拒绝留 WARNING 含会话键 → caplog 断言
- **A6** 变异复核：把"`superuser→private`自动收窄"删掉，A1 必红

### B 组：cron/人格出沙箱
- **B1** `fs.write("SOUL.md")` / `fs.write("cron/jobs.json")` 失败——目标已不在沙箱内（resolve 越界）
  → `tests/test_scheduler_jobs.py::test_config_out_of_sandbox`
- **B2** `extract` 解压含 `../../cron/jobs.json` 条目被拒（zip-slip 既有防线 + 新路径不在 root）→ 同上
- **B3** 手改 `data/cron/jobs.json` 下轮 tick 生效（保留"手改即生效"运维体验）→ `test_hot_reload`
- **B4** web 人格面读写 `data/personas/` 正常、0600、备份滚动在位 → `tests/test_web.py` 既有面 + 路径断言
- **B5** 旧位置迁移：启动探测 `data/workspace/{SOUL.md,personas,cron}` 存在 → 一次性搬至新位
  （先备份）+ 告警一条；二次启动幂等零告警 → `test_legacy_layout_migrated_once`
- **B6** state 拆分后 `jobs.json` 内容不含 `state`/`updatedAtMs` 字段 → `test_jobs_file_pure_config`

### C 组：agent_turn 修复
- **C1** 白名单外群目标 → `failed` 状态 + WARNING + **零投递**（FakeSink 计数 0）
  → `test_target_acl_enforced`
- **C2** 执行后 store 里**不存在** `p:<目标QQ>`/`g:<gid>:<gid>` 会话，只有 `cron:<job_id>`
  → `test_isolated_session_key`
- **C3** 预算记账 route 前缀为 `cron:` → budget 断言
- **C4** 变异复核：去掉 C1/C2 的实现，C1/C2 必红

### D 组：守卫与文档
- **D1** `test_group_skill_drift` 双向 diff 为零（见 D6）
- **D2** `TestSecretBearingPaths` 扩容后 `data/cron|personas` 缺忽略即红
- **D3** AGENTS.md §4 新增信任模型行 + 访客准入标准；features-skills.md 每技能标注受众（owner/访客）
- **D4** `check_review_docs.py` 全绿；BACKLOG 计数块 `--write-stats` 重生成

### R 组：回归与兼容
- **R1** 默认套件 + PG 全新空库双套件全绿（计数变化走 `--write-stats`）
- **R2** `AGENT_GROUP_EXTRA_SKILLS` 脏值 → boolenv 式告警一次取默认（不 crash，M5 教训）
- **R3** 回滚方案：目录常量集中单点（`agentcore/paths.py` 或等效），出问题 revert 一个 commit 即回旧布局

## 4. 非目标（明确不做）

1. **多用户 RBAC / 按群差异化技能面**——OpenClaw issue #8081 同款诉求，官方也未做；一个 env 白名单足够。
2. **提示词注入免疫**——围栏/脱敏既有不变量继续承载，本 AC 只收窄触发面与可写面。
3. **群友 cron / 群友人格修改**——定位决策：零通道。
4. **llm-providers/agent.json 配置链重构**——评审 M8（三层漂移）另立 AC。

## 5. 风险与代价

| 风险 | 缓解 |
|---|---|
| owner 群内运维体验变化（须回私聊） | `AGENT_GROUP_EXTRA_SKILLS` 显式加开只读类（system_status） |
| 群白名单是名单，会漂移 | D1 守卫双向 diff + CI 门禁 |
| 旧布局迁移搬坏贡献者 NAS 现网配置 | B5 先备份后搬 + 幂等 + 告警；迁移失败不阻塞启动（降级读旧位+ERROR） |
| `current_route` 依赖 engine 路径 | matcher 直连命令本有 ACL；新增直连调用点须显式过 where 判定（写进 AGENTS） |
| 贡献者 PR 返工量 | 本 AC 即评审 §6 架构定夺的成文；D2 保留其 cron/agent_turn 骨架与文件格式，只换落点与通道 |

## 6. 工作量估计

| 批 | 内容 | 估计 |
|---|---|---|
| P1 | D1 where 轴 + A 组 | ~1 天 |
| P2 | D2/D3 出沙箱 + 迁移 + B 组 | 1–1.5 天 |
| P3 | D4 agent_turn 修复 + C 组 | 0.5–1 天 |
| P4 | D5/D6 守卫文档 + R 组 | 0.5 天 |

合计 **3–4 天**（含测试与变异复核），可分四批独立合入（每批自带回归）。
