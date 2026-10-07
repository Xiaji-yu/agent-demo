# 修复记录：管理员动作类能力（服务/容器控制、两段确认杀进程、构建脚本、只读库查询）

> **⚠ 取代横幅（M8，REVIEW-de09478..workdir）**：本文件描述的**四键白名单机制**
> （`AGENT_SYSTEMCTL_UNITS`/`AGENT_DOCKER_CONTAINERS`/`AGENT_KILL_PATTERNS`/
> `AGENT_BUILD_SCRIPTS`）、proc_kill 的「pattern 精确匹配」、run_build_script 的
> 「name 白名单 + 内容可信」**均已被后续修复推翻**，现行实现见
> [`FIX-permission-levels-202610.md`](FIX-permission-levels-202610.md)
> （P0 权限缺口 + `AGENT_PERMISSION_LEVEL` 三级制）。照本文回滚 = 重开 P0 级面。
> 各节标注：**【已保留】** = 机制仍如本文所述；**【已推翻】** = 以取代文档为准。
> 另勘误：§一的 `TestTarHardening` 实为 **11 例**（原写 12 例；本轮按
> `pytest --collect-only` 实测复数）。

**背景**：2026-10-02 管理员决策——agent 需要「更高级（危险）的命令」辅助日常工作。
经评估（威胁模型：超管专属入口 + 群聊注入可达 + LLM 自身出错 + 配置驱动执行四类风险），
确定分四条线落地，全部遵循仓库既有安全模式（ops_skills 的固定语义 / ssh_run 的
env-only 凭据 / reboot.py 的两段确认 / workspace runner 的白名单穷举）。

**设计决策（与原评估方案的差异，附理由）**：

1. `systemctl` 动作与 `docker` 控制**不进 `run_command` 通用白名单**，改为独立
   `action_skills.py` 的固定语义 skill（枚举动作 + 白名单目标）——比逐参数穷举
   systemctl/docker 的 flag 空间更安全，且与既有 `service_status`（ops_skills）同构。
   `run_command` 白名单只加了 **tar 解压**（通用文件操作，参数可穷举）。
2. `proc_kill` 的确认态放**模块内存**（TTL + 惰性清理），不走 reboot 的文件落盘：
   确认是一次性短时状态，进程重启即失效本就合理；文件反而引入清理与竞态。

## 一、runner：tar 安全解包（`agentcore/workspace/runner.py`）【已保留】

复刻 unzip 分支全套并加强：纯解压开关白名单（拒 `-P`/`--to-command`/
`--checkpoint-action`/`-O` 等；连写短选项逐字过表，粘连取值形态有意拒绝）、
必须且各只能一个 `-C`/`-f`（多 `-f` 会让条目预扫描扫错包）、`-C` 必须指向工作区
子目录且 spawn 前先建（GNU tar 不像 unzip 自动创建）、解压前扫条目（`..`/绝对路径/
`.git*`/**符号与硬链接条目**/设备条目——比 unzip 多拒链接，解压前即拦掉）、解压后仍
strip + 检出即废弃输出目录（纵深）。

**验证**：`tests/test_runner_security.py::TestTarHardening`（11 例，含真实 tar.gz
解压行为级用例）。变异复核：① 关链接条目拒绝 → 2 例失败；② 允许多 `-C` → 1 例失败。
均还原后复绿。

## 二、`agentcore/skills/action_skills.py`（新，仅 superuser）【已推翻】**：本节的「白名单 env（空=关闭）」四键（`AGENT_SYSTEMCTL_UNITS`/`AGENT_DOCKER_CONTAINERS`/`AGENT_KILL_PATTERNS`/`AGENT_BUILD_SCRIPTS`）已随 `FIX-permission-levels-202610.md` 整体移除，改为 `AGENT_PERMISSION_LEVEL` 三级制 + 自保护（`PG_CONTAINER`/自身 unit/自身容器）；`proc_kill` 的 pattern 从「白名单精确成员」改为调用参数（pgrep -f 语义，两段确认 + pid1/自身排除 + 10 个上限不变）；`run_build_script` 的「name 白名单」与「内容可信、没有内容权」不再成立（见 M1 边界说明）。两段确认、审计、输出围栏、pid 1/自身拒绝等仍原样保留

| skill | 白名单 env（空=关闭） | 关键边界 |
|---|---|---|
| `service_ctrl` | `AGENT_SYSTEMCTL_UNITS` | 动作枚举 restart/start/stop；unit 形状正则 + 白名单成员；执行后回报 is-active |
| `docker_ps`/`docker_logs` | 无（纯只读，命令代码写死） | 容器名形状正则；lines 封顶 200；进只读并行名单 |
| `docker_ctrl` | `AGENT_DOCKER_CONTAINERS` | 同上枚举 + 容器白名单 |
| `proc_kill` | `AGENT_KILL_PATTERNS` | **两段确认**（token=secrets.token_hex，TTL 默认 120s，`compare_digest` 比对）；拒 pid 1 与自身；一次上限 10 个进程；signal 仅 TERM/KILL；二次调用重新解析 pid；执行记 WARNING 审计 |
| `run_build_script` | `AGENT_BUILD_SCRIPTS` | 仅跑工作区 `scripts/<name>.sh`（name 白名单+形状）；参数禁 shell 元字符/`..`/路径逃逸（shlex 拆分）；超时 env（默认 300s）；输出过 `fence_untrusted` 围栏；环境变量继承（脚本内容可信+参数受控） |

**验证**：`tests/test_action_skills.py`（33 例）。变异复核：① 去 token 门 → 4 例失败；
② 去 service_ctrl 白名单 → 1 例失败；③ 去输出围栏 → 1 例失败。均还原后复绿。

## 三、`agentcore/skills/db_skills.py`（新，仅 superuser）【已保留】（readonly 事务/schema 校验/围栏不变；级别门为 medium+ 起步由取代文档补充）

`db_query(sql)`：单条 SELECT/WITH（去注释→拒残留 `;`→首关键字白名单→危险函数
黑名单纵深）；**只读事务** `conn.transaction(readonly=True)` 作最终闸；statement_timeout
先 `int()` 强转拼纯数字（防 SET 注入）；`fetch(limit+1)` 探行数上限；单元格/总输出
截断；结果过 `fence_untrusted` 围栏；异常不外泄连接串/密码；`DATABASE_URL` 未配置即
关闭（fail-closed，测试经 `dsn=` 注入绝不回落生产库）。

**验证**：`tests/test_db_skills.py`。**本地 scratch 测试库实跑通过**
（`CREATE DATABASE qqagent_test`，非生产库；`TEST_DATABASE_URL=postgresql://xiaji:xiaji@127.0.0.1:5432/qqagent_test`
→ **35 passed**，含 2 个真库端到端用例）。变异复核（子代理执行，sha256 级还原）：关写
关键字白名单 → 9 例失败；关多语句判定 → 2 例失败；关危险函数黑名单 → 2 例失败；去围栏
→ 1 例失败。

**实现期发现并修复的真 bug（子代理假连接照错 API 形状所致，值得记录）**：
初版用 `prepare(sql)` + `stmt.fetch(limit + 1)` 做行数上限——asyncpg 的
`PreparedStatement.fetch(*args)` 的坑是**查询绑定参数**不是行数限量，真实 PG 直接
`InterfaceError: the server expects 0 arguments for this query, 1 was passed`；假
连接把 `fetch(n)` 当成行数限量才让单测全绿。已改为 `conn.cursor(sql)` +
`cur.fetch(n)`（正确的"最多 N 行"API），并把假连接改成照真实契约：`_FakePrepared.fetch`
传位置参数即抛 `InterfaceError`（`test_prepare_fetch_row_limit_tripwire` 回归护栏），
另加"退回 prepare().fetch(n) → 7 例失败"的变异复核锁定。**教训：镜像第三方 API 的
假连接必须复刻"会抛错"的形状，否则只是恒真断言**（AGENTS.md §5 同款坑）。

**注册契约调整**：只读标记按 REVIEW C3 约定集中在 `builtin.py` 尾部审计点；db_query
按其结果要进 prompt 的口径 **fail-closed 不进**只读并行名单（`test_builtin_does_not_mark_db_query_readonly`
锁定，并以 docker_ps/docker_logs 作对照组验证审计点有效）。

## 从 X 改为 Y 的语义声明

- `run_command` 白名单：**新增 tar 解压分支**（此前 tar 整体不在白名单）
- skill 集合：新增 `service_ctrl`/`docker_ps`/`docker_logs`/`docker_ctrl`/
  `proc_kill`/`run_build_script`/`db_query` 七个 superuser 技能；只读并行名单新增
  `docker_ps`/`docker_logs`
- **刻意仍不做**：kill/systemctl 随意动作不进 run_command（reboot.py 注释的不变式
  保持）；make/pip/npm/docker exec 等图灵完备命令不放行（配置/内容驱动执行，
  参数穷举无意义）

## 残留风险

> 【已推翻】proc_kill 的 pattern 白名单机制已移除（见 §二横幅），现行防线是
> 两段确认 + pid1/自身排除 + 10 个上限；旧条目存档备查：
> - `proc_kill` 的 pattern 白名单是**精确成员匹配**：配宽了（如 `node`）仍可能一次匹配
  多进程（有 10 个上限 + 两段确认兜底）
> - `run_build_script` 继承进程环境变量：脚本自身 echo 密钥会把密钥带进 LLM 上下文
  （旧「内容可信」框述已推翻：同档 fs_write 可写 scripts/、curl+tar 可落盘攻击者包，
  REVIEW-de09478..workdir M1；现行防线=medium 起步+双层闸+解压产物清执行位+围栏）
- db_query 已在本地 scratch 测试库（`qqagent_test`，非生产）跑通真库端到端；CI 环境
  以 `TEST_DATABASE_URL` 为准

**验证汇总**：默认套件全量 + ruff check/format 全绿（见交付说明）；所有新增用例均
做过「改坏实现 → 必须失败」复核；未 commit/push（按仓库纪律等管理员明确指示）。
