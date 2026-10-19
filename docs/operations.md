# 运维与质量

> 本文自 `README.md` 拆分而来（2026-10-16）；返回 [README](../README.md)。

## 配置一览

> **必配** = 不设置则该功能不可用（示例值即占位符）；**选配** = 有可用默认值或仅用于调优。全局必配见 [configuration.md](configuration.md)。

| 配置项 | 标注 | 默认 | 说明 |
|---|---|---|---|
| `AGENT_LOG_DIR` / `AGENT_LOG_KEEP_DAYS` | 选配 | `data/logs` / `14` | 按天落盘轮转（含聊天明文，勿分享） |
| `AGENT_ARCHIVE_ENABLED` / `_DIR` / `_KEEP_DAYS` | 选配（默认开） | `1` / `data/archive` / `7` | JSONL 归档（数据库之外的副本） |
| `AGENT_BACKUP_ENABLED` / `_DIR` / `_KEEP` / `_CRON` | 选配（默认开） | `1` / `data/backups` / `7` / `30 3 * * *` | 每日备份 |
| `AGENT_BACKUP_MIRROR_DIR` | 选配（**推荐**） | 空 = 无异地副本 | 异地镜像目录 |
| `PG_CONTAINER` | 选配 | `agent-demo-db-1` | 容器内 `pg_dump` 回退路径 |
| `AGENT_BUDGET_DAILY_TOKENS` / `AGENT_BUDGET_ENFORCE` / `AGENT_PRICE_PROMPT_PER_M` / `_COMPLETION_PER_M` / `AGENT_BUDGET_DIR` | 选配 | `0` / `0` / 空 / 空 / `data/budget` | 成本预算与账本 |
| `AGENT_SHUTDOWN_FLUSH_TIMEOUT` | 选配 | `30` | 停机 flush deadline（`0` = 不限） |
| `AGENT_MAX_CONCURRENT_TURNS` / `AGENT_TURN_TIMEOUT` | 选配 | `4` / `180` | 全局并发闸门 / 单轮超时 |

## 日志归档（M7）

控制台日志之外，按天落盘到 `data/logs/agent.log`（午夜轮转，保留 `AGENT_LOG_KEEP_DAYS`
份历史文件，默认 14 ⇒ 实际最多约 15 天；设 `0` 关闭落盘），目录可用 `AGENT_LOG_DIR`
覆盖（相对路径按 CWD，部署建议用绝对路径）。**日志含聊天内容明文**，`data/logs/` 已
gitignore 绝不入库，请勿把该目录放进任何公开同步盘——把 `AGENT_LOG_DIR` 指到仓库内
其它路径会让这条 gitignore 保护失效。初始化失败（脏值/目录不可写）只降级为「仅控制台」，
不会阻止 bot 启动。

### 耗时日志（排查「这一轮为什么慢」）

`LLM step` 与 `[reply]` 行带耗时（`time.monotonic()` 差值，分档显示 `0.42s` /
`12.35s` / `1m05s`）；`skill call`+`skill done` 成对出现，耗时记在 `done` 行
（起始行只有名称与参数）。按**从粗到细**三层看：

| 层 | 行样例 | 说明 |
|---|---|---|
| 回合 | `[reply] group:123 \| mode=forward \| engine=1.23s \| total=4.72s \| text=…` | `engine` = 产出这条回复（LLM + 工具）；`total` = 从**收到消息**算起（含 payload 组装/图片下载），含防抖窗口、并发闸门排队与出站投递；出站失败打 `mode=failed`，裸 ping 短路打 `mode=ping`（无 engine 分量） |
| 步骤 | `LLM step 0: 1.83s \| content=None \| tool_calls=['fetch_url', 'search_web']` | 单次 LLM 往返；无工具调用时 `tool_calls=none`；`content` 为模型回复**前 200 字**（与 `[msg]`/`[reply]` 同口径——本行同样含聊天明文，勿分享截图） |
| 工具 | `skill done: fetch_url (0.42s) -> ok, 1234 chars` | 单次工具的执行耗时与结果规模（只记字符数，不落返回正文）；`-> error: …` 仅表示**调用层错误**（技能不存在/缺参数/权限拒绝/handler 异常——registry 固定模板），技能自身返回的 `Error: …` 文案与 `run_shell` 透传的命令输出按成功计、不落正文；并行批另有 `parallel batch: [a, b] (0.55s)` 一行 |

排查套路：`total` 远大于 `engine` ⇒ 等待在防抖/排队/投递；`engine` 大 ⇒ 逐个看
`LLM step`，某步特别贵通常是 prompt 太长；某步里的 `skill done` 贵 ⇒ 是外部依赖
（网页 / 内网设备 / 数据库）慢。**工具卡住**只有开始行 `skill call:` 而没有
`skill done:`——这一对行的缺失本身就是信号。日志查看面（`/api/logs*`）按行解析，
这些行都保持单行。


## 记录保全：归档 + 备份（防误删）

数据库只有一个副本时，一条误执行的 `TRUNCATE`/`DROP` 就能让全部历史消失。这里做了三层：

| 层 | 内容 | 作用 |
|---|---|---|
| **A. 聊天记录归档** | 每条消息实时追加到 `data/archive/messages-YYYY-MM-DD.jsonl`（**数据库之外的文件**），滚动保留 `AGENT_ARCHIVE_KEEP_DAYS`（默认 7 天） | 任何针对数据库的误操作都碰不到它；明文可 grep；可直接回灌 |
| **B. 每日数据库备份** | 每天 `backup.cron`（默认 03:30）备份整库到 `data/backups/`，保留最近 `AGENT_BACKUP_KEEP` 份 | 连 facts / 人格 / 知识库 / 会话一起保；最坏只丢一天 |
| **C. 蒸馏读归档** | 每日蒸馏的输入是 **数据库 ∪ 归档**（按消息 id 去重） | 即使库被清空，知识库仍能继续从归档沉淀，成长不断流 |

备份实现优先用 **pg_dump**（宿主机没有 pg 客户端时自动改用 PG 容器里的 `pg_dump`，
容器名由 `PG_CONTAINER` 指定），失败则降级为 **asyncpg 全表 JSONL 导出**，无外部依赖。

```bash
python scripts/backup_db.py backup                 # 立即备份一次（自动选 pg_dump / JSONL）
python scripts/backup_db.py list                   # 列出已有备份
python scripts/backup_db.py verify <file>          # 只读校验：能否解析、各表多少行
python scripts/backup_db.py restore <file> --yes   # 从备份恢复（会写入目标库，需显式确认）

# 最后手段：连备份都没有时，仅凭归档把消息灌回去（保留原 id，幂等可重跑）
python scripts/backup_db.py restore-archive --dry-run            # 先看会灌多少条
python scripts/backup_db.py restore-archive --yes                # 真回灌
python scripts/backup_db.py restore-archive --since 2026-09-08 --yes   # 只恢复某天之后
```

**异地镜像（推荐开启）**：备份与原库在同一块盘时，挡得住误删、挡不住盘坏。设置
`AGENT_BACKUP_MIRROR_DIR`（挂载的第二块盘 / NAS / 同步盘）后，每次备份会自动再复制一份到该
目录，并按同样的 `keep` 轮转。镜像失败**不会**让本地备份失败，但会在日志里 error 告警并在结果
中标记 `mirrored=false`——避免你以为有异地副本而实际没有。

> 恢复前先用 `verify`，并优先在一个独立库里演练一遍（`python scripts/scratch_db.py create`
> 可开临时库）。真正的异地恢复演练：`python scripts/backup_db.py verify <镜像目录里的文件>`。

两个目录都已加入 `.gitignore`（**含隐私内容，绝不入库**）。归档从启用时刻开始记录，
更早的库内历史不在归档里（由数据库备份覆盖）。


## 性能测试规范

`tests/test_perf.py`（marker `perf`）覆盖**延迟**与**有界性/泄漏**两类，全部用真实测量值：

| 维度 | 覆盖对象 |
|---|---|
| 延迟 | 长回复切分 `split_message`、`_qq_plain`、`merge_parts`、本地 embedding、`fs.resolve`/`fs.list` |
| 有界性 | 群上下文缓冲、最近图片缓冲（写入量远超容量后驻留必须收敛） |
| 泄漏 | 防抖器高频 push/cancel 后活跃 asyncio 任务数不增长 |

约定：

- **默认跳过**（`RUN_PERF=1` 才跑），避免日常套件变慢与机器抖动误报
- **阈值刻意宽松**（数倍于实测），只用于抓 O(n²) 回归与无界增长，**不作为基准数字**
- 测试会打印实测值与机器可读的 `[metric] key=value` 行（`-s` 可见）；线上耗时仍以日志中的 `LLM step` / `skill call` 时间线为准
- 新增热路径函数时应补一条延迟用例；新增缓存/缓冲应补一条有界性用例

**基准存档与劣化对比**（抓"缓慢劣化"，不只是阈值）：

```bash
python scripts/perf_baseline.py            # 跑 perf 测试并与 perf/baseline.json 对比；劣化则退出码 1
python scripts/perf_baseline.py --update   # 换机器/有意优化后重新存档基线
python scripts/perf_baseline.py --threshold 2.0 --floor-ms 5   # 调阈值与噪声下限
```

- 基线：`perf/baseline.json`（记录实测值 + Python/平台/CPU 数；**不同机器数字不可横向比较**）
- 判定：仅当"本次 > 基线 × 阈值"**且基线与本次都高于噪声下限**才算劣化（毫秒默认 5ms、MB 类 1.0、计数类 0.5），避免抖动假警
- 新增指标在基线中缺失时不判定（不会因新增用例报警）

当前本机实测（供对照，非门槛）：`split_message` 240k 字符 11 ms；`_qq_plain` 300k 字符 **32 ms**；`merge_parts` 500 part/1 万图 **<1 ms**（两者原有 O(n²) 已修为线性，见 BACKLOG §6）；`fs.resolve` 2 万次 1.5 s；群上下文与图片缓冲 2 万次写入后驻留 ≈0 MB 且有界；防抖器无任务泄漏。



> **调试日志隐私**：`AGENT_DEBUG_RAW_DUMP=1` 时，未触发的群消息会打一行
> `[trigger-miss]`（at 目标/wake/to_me/reply + **截断后的原文**）。该行含
> 消息正文，排障结束请置回 0，**不要分享日志截图**。
