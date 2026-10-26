# 运维与质量

> 本文自 `README.md` 拆分而来（2026-10-16）；返回 [README](../README.md)。

## 配置一览

> **必配** = 不设置则该功能不可用（示例值即占位符）；**选配** = 有可用默认值或仅用于调优。全局必配见 [configuration.md](configuration.md)。

| 配置项 | 标注 | 默认 | 说明 |
|---|---|---|---|
| `AGENT_LOG_DIR` / `AGENT_LOG_KEEP_DAYS` | 选配 | `data/logs` / `14` | 按天落盘轮转（含聊天明文，勿分享） |
| `AGENT_LOG_CONTENT` | 选配 | `1` | 置 `0` 后日志只记 `<len=N>`/段类型，不落消息正文（适配层 + agentcore 内容日志；不可经 web 改） |
| `AGENT_ARCHIVE_ENABLED` / `_DIR` / `_KEEP_DAYS` | 选配（默认开） | `1` / `data/archive` / `7` | JSONL 归档（数据库之外的副本） |
| `AGENT_BACKUP_ENABLED` / `_DIR` / `_KEEP` / `_CRON` | 选配（默认开） | `1` / `data/backups` / `7` / `30 3 * * *` | 每日备份 |
| `AGENT_BACKUP_MIRROR_DIR` | 选配（**推荐**） | 空 = 无异地副本 | 异地镜像目录 |
| `AGENT_BACKUP_ENCRYPT` / `AGENT_BACKUP_AGE_RECIPIENT` | 选配 | 空 / 空 | age 公钥加密备份（产物 `.sql.gz.age`；开启后不回退明文 JSONL） |
| `PG_CONTAINER` | 选配 | `agent-demo-db-1` | 容器内 `pg_dump` 回退路径 |
| `AGENT_BUDGET_DAILY_TOKENS` / `AGENT_BUDGET_ENFORCE` / `AGENT_PRICE_PROMPT_PER_M` / `_COMPLETION_PER_M` / `AGENT_BUDGET_DIR` | 选配 | `0` / `0` / 空 / 空 / `data/budget` | 成本预算与账本 |
| `AGENT_SHUTDOWN_FLUSH_TIMEOUT` | 选配 | `30` | 停机 flush deadline（`0` = 不限） |
| `AGENT_MAX_CONCURRENT_TURNS` / `AGENT_TURN_TIMEOUT` | 选配 | `4` / `180` | 全局并发闸门 / 单轮超时 |

## 容器化部署

`docker compose up -d db bot` 一条命令起全栈（bot 也在容器里跑）。与宿主机部署
（`scripts/deploy.py` + systemd）的差别**只在「谁当 supervisor」**：

| 维度 | 宿主机部署 | 容器部署 |
|---|---|---|
| 进程托管 | systemd unit（`Restart=always`） | `restart: unless-stopped` |
| 信号转发 | systemd | `init: true`（tini） |
| `/reboot` 的 A 步 | 退出等 systemd 拉起 | 退出后容器重启，等价替代 |
| 优雅停机 | systemd SIGTERM → 停机钩子 | `docker stop` → tini → 同一套钩子 |

`stop_grace_period: 60s` 是按 `AGENT_SHUTDOWN_FLUSH_TIMEOUT`（默认 30s）留的余量：
flush 要过全局 LLM 闸门，无界等待会被 SIGKILL 反而全丢。

### 挂载边界（最容易踩的一条）

`data/` **只挂运行期子目录**（`logs`/`archive`/`backups`/`budget`/`workspace`/
`cache`/`sessions`/`privacy`/`skills`/`kb_samples`），绝不能把空卷挂到 `/app/data`
本身——镜像里的 `data/fonts/wqy-zenhei.ttc`（`agentcore/render/fonts.py` 按
`parents[2]/data/fonts` 解析）会被遮掉，症状是**文字卡片/表格渲染静默失败**。

`data/` 根下的单文件（`llm-providers.json` / `2fa-secret.json` / `nudge.json` /
`search-providers.json`）刻意不挂：容器内自管理，首次启动由 `.env` 的 `LLM_*` 迁移
生成。从宿主机部署迁过来要带走这些文件时，手动复制即可。

### 运行身份与属权

镜像**不设 `USER app`**：compose 的 bind mount 指向不存在的目录时，docker 以
`root:root` 创建它，此时进程若已降权就改不了属主。由 `scripts/docker-entrypoint.sh`
在 root 下修挂载点属权，再 `gosu` 降到 `app`（uid/gid 1000）运行。宿主用户 uid 不是
1000 时，在 compose 里加 `user: "UID:GID"` 直接以该身份启动（不走降权分支）。

为什么必须修：日志落盘、原文归档、预算账本三处对不可写目录都是**容错降级**
（`logging_setup` 退化为「仅控制台」、archive append 失败只 ERROR、budget 记账失败
只 WARNING）。不修属权的故障表现是「日志只剩控制台、账本数字不动、归档文件不生成」
——全程不报错，极难排查（本次容器化实测复现过）。

### 镜像里刻意做的四件事

缺任何一件都是**静默降级**（不报错，只是功能消失）：

| 组件 | 用途 | 缺了会怎样 |
|---|---|---|
| ffmpeg | 点歌 silk 转码 | `silk_available()` 恒 False，点歌整体不注册 |
| PG 16 的 `pg_dump`/`psql` | 备份 / 恢复 | 见下 |
| tzdata + `TZ` | 时效锚点 | 默认 UTC 对中文用户日期差一天 |
| 文泉驿字体 | 文字卡片渲染 | 渲染静默失败 |

PG 客户端版本是个坑：Debian bookworm 的 `postgresql-client` 是 **PG 15**，而服务端
是 `pgvector:pg16`（PG 16），pg_dump 会以 `aborting because of server version
mismatch` 直接拒绝导出、备份静默降级成 JSONL（实测复现）。故从官方
`postgres:16-bookworm` 镜像取 `pg_dump`/`psql`/`libpq.so.5`——tag **必须带
`-bookworm`**：无后缀的 `postgres:16` 基于更新的 Debian（glibc 2.38），它的库在
bookworm（2.36）上直接 `GLIBC_2.38 not found`。

### 构建

```bash
docker build -t agent-demo:local .                                                  # 常规
docker build --network host \
  --build-arg APT_MIRROR=mirrors.aliyun.com \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  -t agent-demo:local .                                                             # 国内
```

- 构建期即跑 `python -c "import bot"` 冒烟（与 `deploy.py smoke` 同款），缺件在 build 就炸
- `--network host`：docker 默认 DNS（8.8.8.8）在大陆被污染，apt 源会解析到假 IP（`198.18.x.x`）
- `APT_MIRROR`：deb.debian.org 慢时换国内源（默认空 = 官方源，不影响境外构建）
- `PIP_INDEX_URL`：PyPI 官方源慢/超时时换国内源（默认空 = 官方源），覆盖 `pip install uv`
  与两处 `uv sync`（腾讯云机器可用更快的内网源 `http://mirrors.tencentyun.com/pypi/simple`）
- 依赖安装走 `uv sync --locked`，与 CI 同纪律（`--locked` 校验 lock 与 pyproject 一致）

### 协议端（NapCat）

反向 WS 是**协议端连 bot**，bot 是服务端。NapCat 可用官方镜像的 `MODE=nonebot` 模板
（连 `ws://nonebot:8080/onebot/v11/ws`）；compose 里服务名不叫 `nonebot` 时，给 bot
加个网络别名即可：

```yaml
services:
  bot:
    networks:
      default:
        aliases: [nonebot]
  napcat:
    image: mlikiowa/napcat-docker:latest
    environment: { MODE: nonebot, WEBUI_TOKEN: "<自己填>", NAPCAT_UID: "1000" }
    ports: ["127.0.0.1:6099:6099"]
```

握手顺序：**先校验 `X-Self-ID` header，再校验 `Authorization: Bearer <access_token>`**。
两者任一缺失都在 accept 前 close，uvicorn 统一呈现为**空响应体的 403**——看到 403
先查这两个 header，别只怀疑 token。

### 容器内的能力边界（沙箱收敛）

容器部署刻意**不依赖 systemd、不碰宿主机**，全部操作收敛在容器内。为此有几处行为与宿主机部署不同：

| 能力 | 宿主机部署 | 容器部署 |
|---|---|---|
| `systemctl` / `journalctl` / `dmesg` | 只读查询可用 | **一律拒**，返回说明文案（无 systemd/journald；日志看 `data/logs`，用 `log_tail`） |
| `service_status` 技能 | 查 systemd 单元状态 | 返回同类说明文案 |
| `/reboot` 执行链 | C 外部命令 → B 自替换 → A 退出 | **跳过 C 步**，直接 B 步 `os.execve` 自替换（A 步由 `restart: unless-stopped` 等价替代） |
| 数据库备份 | 宿主机 `pg_dump`，否则 `docker exec` | 镜像内 PG 16 客户端（`docker exec` 回退在容器内不成立，也不需要） |

三处判定都走 `agentcore/runtime_env.py` 的 `in_container()`（读 `/proc/self/cgroup`
+ `/.dockerenv`）。

**为什么把「命令不存在」改成明确文案**：容器里 `systemctl` 要么没装、要么返回
`System has not been booted with systemd`——模型看到会以为环境缺了东西、换个参数
再试，白烧一轮 token 还答不到点上。明确文案一次说清「容器里没有这个、该看哪里」。

**`AGENT_REBOOT_CMD` 在容器里不生效**（打了也是跳过）：它的可执行体白名单是
`systemctl`/`docker`/`supervisorctl`/`service`，全是宿主机设施，容器里 spawn 就是
把重启动作透出沙箱（`docker` 那支还要挂 docker.sock 才成立）。容器内重启的正解是
B 步自替换。若确实需要外部重启，别用容器部署。

### 测试隔离（别拿生产库试）

```bash
AGENT_BOT_PORT=18080 AGENT_BOT_BIND=127.0.0.1 \
AGENT_DB_PORT=55432 AGENT_DB_NAME=qqagent_ctest \
AGENT_DATA_DIR=/tmp/agent-demo-test/data \
docker compose -p agentdemo-test up -d
```

独立 project（容器/卷/网络全隔离）+ 独立端口 + **全新空库** + 独立 data 目录，与宿主机上
正在跑的生产实例（端口、库、`data/`）零交集。

## 日志归档（M7）

控制台日志之外，按天落盘到 `data/logs/agent.log`（午夜轮转，保留 `AGENT_LOG_KEEP_DAYS`
份历史文件，默认 14 ⇒ 实际最多约 15 天；设 `0` 关闭落盘），目录可用 `AGENT_LOG_DIR`
覆盖（相对路径按 CWD，部署建议用绝对路径）。**日志含聊天内容明文**，`data/logs/` 已
gitignore 绝不入库，请勿把该目录放进任何公开同步盘——把 `AGENT_LOG_DIR` 指到仓库内
其它路径会让这条 gitignore 保护失效。隐私敏感部署可设 `AGENT_LOG_CONTENT=0`：日志只留
`<len=N>` 与段类型，不落正文（排障时看不到原文）。开关在 `agentcore/logging_setup.py`
单点判定，适配层 `[msg]`/`[reply]` 与引擎侧 `LLM step` 输出、`skill call` 参数、长期事实/
蒸馏原始输出、提醒与主动消息正文都走同一出口。初始化失败（脏值/目录不可写）只降级为
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
（下载由一次性 HttpOnly 票据换原生流式 GET，避免 183MB 进浏览器内存）
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
（`pg_try_advisory_lock`，最多等 30s）串行化，不会两边同时改表——**迁移与修复步骤
共用同一把锁**，修复拿不到锁时本次跳过（另一个实例正在修，结果里 `skipped=True`）。

```bash
python scripts/migrate.py status    # 只读：已应用/待应用/缺口/校验和（异常时退出码 1）
python scripts/migrate.py up        # 应用待执行迁移 + 修复步骤
python scripts/migrate.py repair    # 只跑修复步骤（可重复执行）
python scripts/migrate.py create 0002_add_something   # 生成模板（纯文件操作，不需要 DATABASE_URL）
```

**embedding 维度只有一个来源**：`up` / `repair` 与运行时一样按 `.env` 构建
embedding client 并 `probe_dim()`（远程模型以真实输出为准，探测失败/超时回落
`EMBEDDING_DIM`）。CLI 不再直接读 `EMBEDDING_DIM`——否则远程实际维度与配置不一致时，
`--allow-destructive` 会把表 TRUNCATE 到错误维度。`status` 不联网，只用于展示/校验。

Runner 的五条硬保证：

| 保证 | 行为 |
|---|---|
| **校验和** | 已应用迁移的 SQL 被改动 → 启动/CLI 直接拒绝（`ChecksumMismatchError`）。**已应用的迁移永远不许改**，要改结构就新增一个 |
| **版本守卫** | 数据库版本高于代码（回滚了代码）、或账本出现空洞/非前缀历史 → 拒绝运行（`SchemaTooNewError` / `SchemaStateError`），避免旧代码写新 schema |
| **每迁移一事务** | SQL 与账本行同事务提交；失败只回滚当前迁移，`schema_migrations` 不会出现"半应用" |
| **破坏性迁移默认关闭** | `Migration.destructive=True` 的迁移只在 `allow_destructive=True` 时执行；CLI 必须同时给 `--allow-destructive --yes`，且应先 `python scripts/backup_db.py backup` |
| **失败 fail-closed** | 启动路径遇 `MigrationError` **不会**降级到内存存储，而是记录 CRITICAL 并退出：在不匹配的 schema 上继续运行会静默丢数据，比起不来更危险。迁移/修复执行期的语法、权限、SQL 运行期异常也统一包成 `MigrationError`（保留 `__cause__`），不再以裸异常触发"普通故障降级" |

**修复步骤与版本分离**：合并历史重复会话、补唯一索引、vector 维度迁移、HNSW 索引
这些操作每次都幂等重试（`schema_repair.py`），不占版本号；其中会清空维度不匹配表的分支
同样受 `allow_destructive` 门控。**TRUNCATE 与 ALTER 在同一事务内**（ALTER 失败时
表不会被清空），DDL 均带 `lock_timeout=30s`，不会无限期排在别人的长事务后面。

**和备份/恢复的关系**：

- pg_dump 与 JSONL 都**排除 `schema_migrations`**（`--exclude-table=schema_migrations`）：
  `restore` 在灌数据**之前**先清空目标库的旧账本（`DO $$ … DELETE FROM schema_migrations`），
  恢复后的首次启动再按基线幂等重建——**恢复进空库和非空库都成立**；不清的话，
  恢复到已有库时旧账本通常已是当前版本，启动期 migrate 会误判"已到最新"而跳过，
  业务表却仍是备份时的旧结构。
- 每次 pg_dump 会写 `.schema.json` sidecar（记录代码的迁移版本），镜像、U 盘冷备一起复制。
  加密备份的 sidecar 按**解密后**的名字命名（`a.sql.gz.age` → `a.sql.gz.schema.json`），
  这样 `age -d` 解出 `a.sql.gz` 后版本守卫才读得到；旧命名 `a.sql.gz.age.schema.json`
  仍兼容读取。sidecar 存在但损坏时 restore **直接拒绝**（读不到版本就不放行），
  只有完全缺失（早期备份）才按"无版本信息"降级并告警。
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
> `[trigger-miss]`/`[raw-dump]` 以及引擎侧内容日志（`LLM step` 输出、
> `skill call` 参数、长期事实/蒸馏原始输出、提醒与主动消息正文）是否落正文。
> 置 `0`/`off` 后这些行只保留 `<len=N>`、段类型、用户/群 id 与耗时，被引用
> 原文同样被隐去——适合隐私敏感部署；代价是排障时看不到原文。该键不可经 web
> 改写，编辑 `.env` 后重启生效。
