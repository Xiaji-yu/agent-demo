# 运维与质量

> 本文自 `README.md` 拆分而来（2026-10-16）；返回 [README](../README.md)。

## 配置一览

> **必配** = 不设置则该功能不可用（示例值即占位符）；**选配** = 有可用默认值或仅用于调优。全局必配见 [configuration.md](configuration.md)。

| 配置项 | 标注 | 默认 | 说明 |
|---|---|---|---|
| `AGENT_LOG_DIR` / `AGENT_LOG_KEEP_DAYS` | 选配 | `data/logs` / `14` | 按天落盘轮转（含聊天明文，勿分享） |
| `AGENT_LOG_CONTENT` | 选配 | `1` | 置 `0` 后日志只记长度/段类型，不落消息正文（不可经 web 改） |
| `AGENT_ARCHIVE_ENABLED` / `_DIR` / `_KEEP_DAYS` | 选配（默认开） | `1` / `data/archive` / `7` | JSONL 归档（数据库之外的副本） |
| `AGENT_BACKUP_ENABLED` / `_DIR` / `_KEEP` / `_CRON` | 选配（默认开） | `1` / `data/backups` / `7` / `30 3 * * *` | 每日备份 |
| `AGENT_BACKUP_MIRROR_DIR` | 选配（**推荐**） | 空 = 无异地副本 | 异地镜像目录 |
| `AGENT_BACKUP_ENCRYPT` / `AGENT_BACKUP_AGE_RECIPIENT` | 选配 | 空 / 空 | age 公钥加密备份（产物 `.sql.gz.age`；开启后不回退明文 JSONL） |
| `PG_CONTAINER` | 选配 | `agent-demo-db-1` | 容器内 `pg_dump` 回退路径 |
| `AGENT_BUDGET_DAILY_TOKENS` / `AGENT_BUDGET_ENFORCE` / `AGENT_PRICE_PROMPT_PER_M` / `_COMPLETION_PER_M` / `AGENT_BUDGET_DIR` | 选配 | `0` / `0` / 空 / 空 / `data/budget` | 成本预算与账本 |
| `AGENT_SHUTDOWN_FLUSH_TIMEOUT` | 选配 | `30` | 停机 flush deadline（`0` = 不限） |
| `AGENT_MAX_CONCURRENT_TURNS` / `AGENT_TURN_TIMEOUT` | 选配 | `4` / `180` | 全局并发闸门 / 单轮超时 |

## 日志归档（M7）

控制台日志之外，按天落盘到 `data/logs/agent.log`（午夜轮转，保留 `AGENT_LOG_KEEP_DAYS`
份历史文件，默认 14 ⇒ 实际最多约 15 天；设 `0` 关闭落盘），目录可用 `AGENT_LOG_DIR`
覆盖（相对路径按 CWD，部署建议用绝对路径）。**日志含聊天内容明文**，`data/logs/` 已
gitignore 绝不入库，请勿把该目录放进任何公开同步盘——把 `AGENT_LOG_DIR` 指到仓库内
其它路径会让这条 gitignore 保护失效。隐私敏感部署可设 `AGENT_LOG_CONTENT=0`：日志只留
长度与段类型，不落正文（排障时看不到原文）。初始化失败（脏值/目录不可写）只降级为
「仅控制台」，不会阻止 bot 启动。

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

**加密备份（可选，age recipient-only）**：隐私敏感部署可在备份离开 `pg_dump` 时就加密
（`pg_dump → gzip → age` 全流式，**明文 SQL 不落盘**）：

```bash
age-keygen -o key.txt            # 本地生成密钥；输出里的 public key 即 recipient
# .env:
# AGENT_BACKUP_ENCRYPT=age
# AGENT_BACKUP_AGE_RECIPIENT=age1...
python scripts/backup_db.py backup          # 产物：agent-demo-YYYY-MM-DD.sql.gz.age
python scripts/backup_db.py verify <file>   # 只能校验 age 头 + sha256（无私钥不解密）
age -d -i key.txt <file> > <file>.sql.gz    # 恢复前先解密（私钥离线保管）
python scripts/backup_db.py restore <file>.sql.gz --yes
```

- **私钥不要放服务器**：bot 只需要公钥；`restore` 不接受 `.age` 直读（会给出解密提示）
- 开启加密后 **pg_dump 不可用/加密失败 → 备份直接失败**，不会回退 JSONL 明文
- web「备份」页下载到的也是密文（`application/octet-stream`），可安全外传后再解密
- 内置异地镜像、`backup_mirror.sh`（NAS/VPS）与 `backup_usb.sh` 均识别 `.sql.gz.age`
  （U 盘冷备内保留 `db.sql.gz.age`，不会误命名为明文 dump）

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

**NAS 只开 SSH、不挂 NFS/SMB 时**，用 `scripts/backup_mirror.sh` + systemd timer 推送
（`rsync` over SSH，断线可续，NAS 需有 `rsync`）：

```bash
sudo cp scripts/systemd/agent-demo-mirror.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-demo-mirror.timer
sudo systemctl start agent-demo-mirror.service   # 手动跑一次验收
```

脚本默认 `NAS_HOST=nas-backup`、`NAS_ROOT=/vol1/1000/backup/agent-demo`（按实际改），
同步 `backups/`、`pgwal/`、`pgbase/`（后两者不存在时自动跳过），并对**远端最新一份**做
SHA256 校验。它与内置 mirror 的区别：**不删除远端历史**，远端按 `KEEP_DAYS`（默认 30 天）
独立清理，所以本地 `keep` 轮转不会连带删掉异地副本。NAS 侧建议再开文件系统快照，
防"备份被同步删除/勒索加密"。

**VPS 异地镜像（WireGuard 内网，推荐与 NAS 并存）**：同一脚本用通用目标
`MIRROR_HOST/MIRROR_ROOT`，VPS 版 unit 走 WG 内网 `10.10.0.1`，不暴露公网 SSH：

```bash
sudo cp scripts/systemd/agent-demo-vps-mirror.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-demo-vps-mirror.timer
```

默认 `KEEP_DAYS=30`（约 5.5 GB）、`MAX_GB=9` 目录级闸门（40 GB 盘只规划 10 GB）；
同步前先删过期、再查 `du`，超限直接失败而不是塞满 VPS。VPS unit 另设
`REQUIRE_PRIVATE_IP=1`：解析 `MIRROR_HOST` 必须落在私网段（WG 内网），防配置漂移
把 9GB 明文 dump 推向公网主机。WG 断开时 timer 失败并在 journal 留痕，不会静默跳过；
三个备份 unit 都挂 `OnFailure=agent-demo-backup-failure.service`（失败时一条
`agent-demo-backup` tag 的 error 日志）。`backups` 目录缺失同样失败（bot 没产出
dump 时不允许"成功镜像空气"）。

**U 盘离线冷备（可选的第三份）**：`scripts/backup_usb.sh` 把最新 dump、`.env`、
`config.yaml`、自定义人格 md、provider/2FA 配置和 `data/{archive,workspace,budget,skills}`
打成一个 `agent-demo-YYYY-MM-DD/` 目录写到 U 盘（带 `MANIFEST.sha256`），按 mtime
保留最近 `KEEP` 份（默认 8）。U 盘未挂载（`mountpoint -q` 判定，**不是**目录存在性——
挂载点目录拔盘后常驻）时跳过（exit 0，journal 留一条）；本地 dump 超过 30h 拒绝写入；
先写临时目录、必含清单（dump/`.env`/`config.yaml`）硬断言 + 校验通过才替换当天目录，
失败不会毁掉上一份。

```bash
sudo cp scripts/systemd/agent-demo-usb-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-demo-usb-backup.timer
```

U 盘建议 ext4；service 统一 `UMask=0077`（rsync `--no-perms` 下 ext4 目标按 umask
落盘），FAT32 没有权限位、重挂载时必须带 `umask=077`（或用 fstab 固定），否则 `.env`、
`2fa-secret.json` 和聊天记录会变成所有人可读。冷备含明文密钥与聊天内容，会带出门时
请用 LUKS 加密。

**Web「备份」标签**：`/agent-web` 可看本地备份列表（最新年龄、大小、磁盘剩余、镜像配置），
并显式触发「立即备份」「校验」「删除非最新一份」「下载」——写操作与下载都要 2FA + 审计
（「校验」是只读动作：审计 + 并发互斥，无 2FA）。下载返回的是 pg_dump 明文（聊天记录/
知识库/人格），页面会二次提示；恢复生产库仍只走 CLI（`scripts/backup_db.py restore --yes`），
web 不提供一键恢复。

> 恢复前先用 `verify`，并优先在一个独立库里演练一遍（`python scripts/scratch_db.py create`
> 可开临时库）。真正的异地恢复演练：`python scripts/backup_db.py verify <镜像目录里的文件>`。

两个目录都已加入 `.gitignore`（**含隐私内容，绝不入库**）。归档从启用时刻开始记录，
更早的库内历史不在归档里（由数据库备份覆盖）。


## Schema 迁移（migrations）

数据库结构由**版本化 SQL + 账本**管理，不再依赖 `CREATE TABLE IF NOT EXISTS` 随手补：

```
agentcore/memory/migrations/0001_baseline.sql   # 基线（等价旧 DDL）
agentcore/memory/migrations/__init__.py         # MIGRATIONS 注册表（导入时校验）
agentcore/memory/migrate.py                     # runner：锁 / 校验和 / 事务 / 版本守卫
agentcore/memory/schema_repair.py               # 修复步骤（去重/索引/维度/HNSW）
```

**启动即迁移**：`bot.py` 装配 PG 存储时自动 `migrate()` 到代码声明的最高版本，
随后跑修复步骤。多实例同时启动或与 CLI 并发时，靠 **session 级 advisory lock**
（`pg_try_advisory_lock`，最多等 30s）串行化，不会两边同时改表。

```bash
python scripts/migrate.py status    # 只读：已应用/待应用/缺口/校验和
python scripts/migrate.py up        # 应用待执行迁移 + 修复步骤
python scripts/migrate.py repair    # 只跑修复步骤（可重复执行）
python scripts/migrate.py create 0002_add_something   # 生成模板（记得注册）
```

Runner 的五条硬保证：

| 保证 | 行为 |
|---|---|
| **校验和** | 已应用迁移的 SQL 被改动 → 启动/CLI 直接拒绝（`ChecksumMismatchError`）。**已应用的迁移永远不许改**，要改结构就新增一个 |
| **版本守卫** | 数据库版本高于代码（回滚了代码）、或账本出现空洞/非前缀历史 → 拒绝运行（`SchemaTooNewError` / `SchemaStateError`），避免旧代码写新 schema |
| **每迁移一事务** | SQL 与账本行同事务提交；失败只回滚当前迁移，`schema_migrations` 不会出现"半应用" |
| **破坏性迁移默认关闭** | `Migration.destructive=True` 的迁移只在 `allow_destructive=True` 时执行；CLI 必须同时给 `--allow-destructive --yes`，且应先 `python scripts/backup_db.py backup` |
| **失败 fail-closed** | 启动路径遇 `MigrationError` **不会**降级到内存存储，而是记录 CRITICAL 并退出：在不匹配的 schema 上继续运行会静默丢数据，比起不来更危险 |

**修复步骤与版本分离**：合并历史重复会话、补唯一索引、vector 维度迁移、HNSW 索引
这些操作每次都幂等重试（`schema_repair.py`），不占版本号；其中会清空维度不匹配表的分支
同样受 `allow_destructive` 门控。

**和备份/恢复的关系**：

- pg_dump 与 JSONL 都**排除 `schema_migrations`**（`--exclude-table=schema_migrations`）：
  账本由恢复后的首次启动按基线幂等重建，避免把旧账本带回来导致校验和/版本判断错乱。
- 每次 pg_dump 会写同名 `.schema.json` sidecar（记录代码的迁移版本），镜像一起复制。
- `restore` 读到备份的 schema 版本**高于当前代码**时默认拒绝，防止用旧代码打开新库；
  确认要降级演练时显式加 `--allow-newer-schema`（`scripts/backup_db.py restore ... --yes --allow-newer-schema`）。

> 改动 DDL 的流程：`create` 生成文件 → 写 SQL → 在 `MIGRATIONS` 注册 → 先在空库
> （`scripts/scratch_db.py create --yes`）跑 `up` 验证 → 跑全套件 → 再动生产库。
> 老库上永远测不出 DDL 错误（`CREATE TABLE IF NOT EXISTS` 是 no-op），这条是历史教训。


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
>
> **日志正文开关**：`AGENT_LOG_CONTENT`（默认 `1`）控制 `[msg]`/`[reply]`/
> `[trigger-miss]`/`[raw-dump]` 是否落消息正文。置 `0`/`off` 后这些行只保留
> `text_len`、段类型、用户/群 id 与耗时，被引用原文同样被隐去——适合隐私敏感
> 部署；代价是排障时看不到原文。该键不可经 web 改写，编辑 `.env` 后重启生效。
