# FIX-openclaw-20261101 — 第三步（OpenClaw 化）整体合并方案

> 2026-11-01 · 对应 [AC-group-guest-mode.md](AC-group-guest-mode.md) P1–P4 全部完成。
> 本文是**合并方案/交付说明**：做了什么、改了哪些不变量、如何验证与回滚、与
> UGREEN 分支的最终对应关系。发 PR 作者看 [PR6-disposition.md](PR6-disposition.md)。

## 1. 总览：四批已全部合入 main（逐个增量，无整分支合并）

| 批 | 内容 | commit（main） | 验证 |
|---|---|---|---|
| P2a（含 P3） | cron 全链出沙箱 + agent_turn 三修复（M1/M2/L3）+ 模板/冷备/容器 | `1b40383` `323ca67` | 变异 4/4 |
| P2b | 人格落点 `data/personas/` + QQ 私聊命令通道 + `/openclaw import` | `02fe6a3` `a763165` | 全量 3445 |
| P1 | 技能 where 轴（superuser 自动 private + 白名单加开） | `c7c6ab0` | 变异 A6 |
| P4 | 访客 guard 与文档（drift/secret-path/信任模型行） | `c7c6ab0` | 12 例 |
| 文档 | AC 定案入库 + 处置说明 | `46fc234` | 索引完整 |

AC 定夺（2026-11-01）：① 写通道 = LLM 零通道 + 三条人通道（SSH / web / **QQ 私聊命令**）；
② 批次 P2→P3→P1→P4；③ 单向导入 + `/openclaw import`；④ 按功能目录。

## 2. 数据/配置落点（合并后的最终形态）

```
data/cron/jobs.json        OpenClaw 兼容配置（模板入库 docs/examples/；本文件 gitignore+冷备）
data/cron/state.json       运行态 sidecar（M4，执行体只写它）
data/personas/SOUL.md      OpenClaw 人格入口（导入目标）
data/personas/<name>.md    用户人格（0600/gitignore/容器挂载持久化）
agentcore/personas/*.md    出厂模板（首启播种到 data/personas，之后只读语义）
data/workspace/            纯 LLM 工作产物（workspace_purity 守卫防回迁）
```

- **修改通道**：QQ 私聊 `/cron`（list/add/del/on/off/run/confirm）、`/persona new|edit`、
  `/openclaw import <目录>`——superuser+私聊 ACL、两段确认、审计、改前备份；web 面沿用
  既有契约；SSH 手改 jobs.json 即生效（60s 内被扫描）。
- **互拷**：jobs.json/SOUL.md 格式与 OpenClaw 一致，直接拷入即用；`/openclaw import`
  可整目录分拣（只增不改，已存在一律跳过）；memory 导入另议（明示不支持）。

## 3. 不变量变化（写进 AGENTS §4）

| 新增/强化 | 内容 |
|---|---|
| 信任模型（where 轴） | superuser 技能注册即 private（群聊不可见）、白名单 `AGENT_GROUP_EXTRA_SKILLS`、拒绝无探测口+WARNING |
| 配置落点 | cron/人格**不得**落在 `data/workspace`（cron 目录守卫 + personas 默认目录断言） |
| cron 执行安全 | M1 群目标白名单（fail-closed 不注入）、M2 会话隔离 `cron:<job_id>`、L3 崩溃窗口不重投 |
| 敏感路径 | `data/{2fa-secret,llm-providers,search-providers,nudge}*`、`data/{cron,personas,state,mirror,ssh}/` 全部 gitignore（TestSecretPathGuards 锁） |
| 文档一致性 | features-skills.md 的 superuser 标注与源码 AST 双向 diff 为零（TestGuestSkillDrift 锁） |

## 4. 验证基线（2026-11-01 实测）

- 全量默认套件 **3461 passed / 73 skipped（3534 收集，~80-90s）**
- ruff check/format 双绿；pyright agentcore 0 errors；check_review_docs --check-stats 绿
- CI：main 与 release/1.0.3-beta 全绿（CI + Docker arm64 + Docker Publish 三 workflow）
- 变异复核：M1/M2/L3/M4、六键 live、守卫同源、where 轴收窄（7 组，各 ≥1 用例红后还原）

## 5. 回滚方案

单点回滚（任何一批）：目录常量集中（`jobs.cron_dir()`/`manager.DEFAULT_PERSONAS_DIR` 单点），
revert 对应 commit 即回到旧落点；`data/` 下的运行时文件两者互不覆盖（旧位置残留不影响）。
命令通道（cron_cmd/persona new|edit）独立模块，回滚即删 matcher 注册。

## 6. 与 UGREEN 分支的最终对应

| UGREEN 内容 | 最终处置 |
|---|---|
| PR #6 骨架（jobs.json/SOUL.md/agent_turn 格式） | ✅ 已保留（落点/通道安全化） |
| H2 打码 | ✅ 已移植（`2528232`） |
| GHCR 发布（cd81c84 思路） | ✅ 已实现且升级为双架构（`04b0499`） |
| NAS 容器适配（028ed19 可移植项） | ✅ 批 A/B 已移植（`977da5b`/`ed41610`） |
| config/agent.json 路线、compose_init、jobs.json 入库 | ❌ 不采纳（理由见 PR6-disposition） |
| NAS compose 三文件、README 拆分 | ⏳ 建议按新基线重做两个小 PR（见 PR6-disposition） |

## 7. 剩余事项

1. **memory 导入格式**：OpenClaw memory → facts 的映射（需实物样例，导入侧已占位提示）
2. 群内 owner 运维体验：需要群内只读运维时用 `AGENT_GROUP_EXTRA_SKILLS` 加开（典型 system_status）
3. P1 的 prompt 配套：群聊中模型对高危请求应引导「去私聊找我」（工作流小改，可选）
