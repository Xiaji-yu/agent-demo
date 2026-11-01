# PR #6 处置结果与合并方案（给 Daiyimo）

> 2026-11-01 · 面向 PR #6（`5751f73..3c00145`，14 commits）作者的处理说明。
> 评审报告：[REVIEW-pr6-3c00145.md](REVIEW-pr6-3c00145.md)（3H/22M/24L）；
> 架构定夺与验收规格：[AC-group-guest-mode.md](AC-group-guest-mode.md)。

## 一、你的 PR 与后续工作的处置结果

### ✅ 已进 main（直接采纳/移植）

| 项 | 说明 | main commit |
|---|---|---|
| jobs.json / SOUL.md / agent_turn **骨架与文件格式** | OpenClaw 互拷格式完整保留（未识别字段忽略不报错），只换落点与通道——这正是评审「合并决议专项」要求的「保留 PR 重构」 | `1b40383` |
| **H2 隐私止血** | 全仓真实群号/QQ 号打码（含 main 存量 8 处）+ `TestQQNumberRedaction` 守卫 | `2528232` |
| f76cadf 的 CI 修复经验 | ffmpeg/WSL 等价环境方法论记档；main 的 CI 此前已自装 ffmpeg | — |
| **GHCR 多架构镜像发布**（cd81c84 思路） | `docker-publish.yml`：合并 main 自动构建 amd64+arm64 推 GHCR，NAS 直接 `docker pull`——比原版更进一步：原版只 amd64（受 P1-1 限制），main 已修 P1-1 故双架构全量发布 | `04b0499` |
| **NAS 容器适配批 A/B**（028ed19 的可移植修复） | P1-5 六键谎报 live 回落 boot（+守卫）；P2-1 psql 依赖 libedit2/libicu72；P2-5 db shm 256mb；**P1-2 容器内 web 写面可用**（.env/config.yaml 单文件挂载 + /app 属主 + entrypoint 修属权）；**P1-3 data/state 状态持久化**（recreate 不丢 2FA secret/provider）；P2-6 端口插值；P2-8 writable 标志 | `977da5b`/`ed41610` |
| **f76cadf② 人格删除守卫**（思路） | 同源判定修复在 main 实锤并采纳（`PERSONAS_DEFAULT` 指定的默认人格被 target.default 漏判） | `323ca67` |
| 评审报告 | `REVIEW-pr6-3c00145.md` 已归档进 main（49 项可跟踪） | `1584f9f` |

### 🔁 按架构定夺重做（你的目标达成了，但路径不同）

**OpenClaw 化**（a7e221b/e85bb1e 的诉求：工作区互拷）已落地，但配置落点走的是安全化路线而非
agent.json 路线——评审 H1（人格/cron 落 LLM 沙箱可写区，绕过 web 2FA/审计/备份）是阻断项，
agent.json 单文件配置链的 M8 三层漂移问题也未解。最终形态：

```
data/cron/jobs.json      ← 你的 jobs.json 格式原样可用（互拷直接生效，手改即生效）
data/cron/state.json     ← 运行态拆 sidecar（M4：配置与状态分离）
data/personas/SOUL.md    ← SOUL.md 落这里（OpenClaw 导入入口）
data/personas/<name>.md  ← 用户人格（0600，web 面/私聊命令同契约管理）
```

- 修改通道三条（都是**人直接下命令**，LLM 零通道不变）：**QQ 私聊命令**（新增 `/cron` 命令组、
  `/persona new|edit`——手机随时改，两段确认+审计+改前备份）/ web 面（token+2FA）/ SSH。
- `/openclaw import <目录>`：一条命令把 OpenClaw 工作区（SOUL.md、cron/jobs.json）分拣导入，
  已存在的一律跳过不覆盖。memory 格式转换另议（暂不支持，导入时会明示）。
- agent_turn 三修复随出沙箱一并完成：M1 投递白名单（群目标必须在 ALLOWED_GROUPS）、
  M2 会话隔离（`cron:<job_id>`，不污染真实用户历史）、L3 崩溃窗口不重投。

### ❌ 不采纳（及原因）

| 项 | 原因 |
|---|---|
| `config/agent.json` 单文件配置路线（e85bb1e） | 与 main 的 `.env`（176 键 schema）+ config.yaml 双轨冲突；M8 三层漂移未解。配置的**挂载/持久化问题**已由 P1-3 的 data/state 方案解决 |
| `compose_init.py` 与 deploy.py 并存（d9e084d） | M15 两套自举漂移；main 保留 `deploy.py docker`，其中状态重定向（STATE_REDIRECTS）的理念已吸收进 `.env.example` 注释块 |
| `test_repo_jobs_file_is_tracked`（jobs.json 入库） | 被推翻：jobs.json 含真实投递目标（H2 教训），改为「模板入库 + jobs.json 纳入 USB 冷备必含清单」——你的「换机器任务全丢」诉求由这两者承载 |
| PR 版 Dockerfile 写死 x86_64（P1-1） | main 的 multiarch glob（2df3641）为准，GHCR 双架构发布已验证 |

## 二、给 main 的合并建议（UGREEN ↔ main）

当前 main（`9cc2c41`+）已包含：GHCR 发布、评审报告、H2 打码、六键 live 修复、容器写面/状态持久化、
cron/人格出沙箱全链。**UGREEN 剩余未进 main 的价值点**只剩：

1. NAS 专用 compose 三文件与部署文档三文件（8847ca3/d44ca24）——与 main 的 compose 变量插值
   （AGENT_BOT_PORT 等）有重叠，建议按 main 的变量体系重写一份 `docker-compose.nas.yml`
2. README 拆简易说明书（5cf7c37）——与 main 的 README 255 行正式版冲突，建议摘成 `docs/quick-nas.md`
3. OpenClaw 化旧实现（a7e221b/e85bb1e 的代码本体）——**不再合并**，其用户价值已由新路线承载

**建议操作**：UGREEN 不做整分支合并；把上面 1/2 按新基线重做两个小 PR（内容已验证过，工作量小）。
PR #6 本体无需任何动作（已合并，价值已全部或以替代形式进入 main）。

## 三、验证基线

- 全量默认套件 3445 passed + 73 skipped（3488+ 收集，2026-11-01 实测）
- ruff check/format 双绿；pyright agentcore 0 errors
- 变异复核覆盖：M1/M2/L3/守卫/六键/P1-5（各 ≥1 用例红后还原）
- CI：main 与 release/1.0.3-beta 双分支全绿（CI + Docker arm64 + Docker Publish）

—— 有异议或想认领 1/2 两个小 PR，随时说。
