# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)与 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 格式。
版本号以 `pyproject.toml` 为准（PEP 440：`1.0.3b1` = 1.0.3-beta）。

## [1.0.6] - 2026-11-04

自 `1.0.5` 以来三条线：线上私聊全静默事故的根因修复与派发埋点、`REVIEW-a9a28cf..6867995`
评审 P0 七项修复、以及 1.0.5 周期漏登记的评审产物归档。

### 修复

- **私聊全静默根因（线上事故）**：`1301348` 把 `_get_debouncer` 改成 `async def` 却漏改
  调用点，`await _get_debouncer().push(...)` 解析成「对 coroutine 取 `.push`」抛
  AttributeError，NoneBot 记一条 ERROR 后把整条消息静默丢弃。`AGENT_DEBOUNCE=0` 能
  缓解只因那条分支压根不调用它。回归补 2 条**真防抖派发路径**用例（此前用例全部把
  窗口打成 0.0 走 direct 分支，对该路径零覆盖）。
- **框架日志进文件**：`bridge_loguru()` 把 nonebot/loguru 的框架日志并入
  `data/logs/agent.log`——排障不再必须同时翻 journald（本次事故的 ERROR 只在 journald
  里，白隐身两个多小时）。
- **单回合总期限覆盖等闸门**：等并发配额原为无超时等待，卡在闸门之前即全 bot 静默
  锁死且零日志；改为「等闸门 + 跑引擎」同一 `wait_for`，取消即释放配额。
- **评审 P0 七项**（逐项改法与变异复核见
  [`review/FIX-a9a28cf..6867995.md`](review/FIX-a9a28cf..6867995.md)）：
  - cron 一次性语义：单次时间拒绝转年度 cron（指路 `reminder_add`）、`trusted` 缺省
    False、新任务落盘停用、web 待决定区改读 `state.json` 的 `lastStatus`；
  - outbox 首建 0600；悬空账目两阶段先读后删（通知全失败则保留，下次启动重试）；
  - reboot 收尾补 stats/archive/growth 三槽位（此前每次 `/reboot` 丢 ≤1 个 flush
    周期的群统计与原文归档、蒸发在飞的成长提议）；
  - 慢更新回执：stale 判据改读 `finished_at`（fetch+merge+uv sync+冒烟合法 >600s，
    旧判据让回执永远丢失）、watcher 预算 180→1500s；
  - `/reboot` 与 `/update` 互锁（判据看 `update.lock` 的 flock，僵尸态不把 reboot
    这个恢复手段挡死）；
  - 五个主机状态技能补围栏（上轮声称落码实则从未进 main）。

### 新增

- **派发路径逐格埋点**（`AGENT_TURN_TRACE`，默认 1）：每条触发消息打一行
  `[turn-trace]`，依次记录 `nudge_done → … → answer_out`。「消息进了但没回复」时看
  最后一条停在哪一格即卡点；0 = 关闭。

### 文档

- 归档 `review/REVIEW-a9a28cf..6867995.md`（1.0.5 周期评审报告）与
  `review/FIX-a9a28cf..6867995.md`（P0 修复记录）；BACKLOG §6.18 登记本轮 P0。

## [1.0.5] - 2026-11-03

自 `1.0.4` 以来的改动。三条线合入：出站记账与打扰预算（参考 superagent 的运维面优化）、
记忆星云移动端双指缩放、开发库合成种子脚本，以及两轮评审修复。

> **补记（2026-11-04，评审 M16）**：本段初版漏记 6 组用户可见变更（同型第三次复发），
> 下列条目为事后补齐，事件顺序以 git 历史为准。

### 新增

- **在线更新 `/update`（全链，7 个 commit）**：`/update` 拉 `main` → `uv sync --locked`
  → `import bot` 冒烟 → 重启 → 校验 boot marker；失败自动 `reset --hard` 回滚 + 重建
  依赖 + 再重启。脚本跑在 `systemd-run --user` 的独立 cgroup 里（服务重启杀不到它，
  回滚才有执行者）；前置检查全部 fail-closed（`AGENT_UPDATE_OK=1`、分支必须 `main`、
  磁盘 > 2G、`uv` 存在、工作树只放行运行态目录）。回执两段：更新前失败由旧进程 watcher
  发，重启后的结果由新进程 `on_bot_connect` 发。配套 `/update status`（只读）。
- **cron 任务自然语言创建 + trusted 轮次**：`/cron add 明天早上9点 做日报` 可直接建
  agent_turn 任务（自然语言→cron 由确定性解析器完成）；job 可标 `trusted: true` +
  `owner`，到点轮次以 owner 的**权限身份**执行（可见可调 superuser 技能），数据身份仍是
  伪用户 `cron:<job_id>`——会话键/预算/growth 隔离不变，每次执行记 `cron_trusted_run`
  审计。**注意**：该版 `trusted` 缺省为 True 且新任务立即启用，1.0.6 已按评审 M6/M7 收窄
  （缺省 False + 落盘停用）。
- **superuser 私聊 / trusted cron 为完全可信通道**（**信任边界变化，部署者须知**）：
  超级管理员私聊与 trusted 轮次免除指令防御性围栏，引用/转发/长期事实/相处成长/历史摘要
  以及本地运维命令（`run_shell`/`run_command`/`log_tail`/`docker_*`）回显直接作为完全
  可信上下文直通。非 trusted 路径的防御不变。
- **记忆星云视图主体**：`/agent-web` 「记忆」tab 的三层分簇星云（事实按用户 / 知识块按
  来源 / 会话带时间轨道）+ 分层抽样 3000 + 拖动旋转/滚轮缩放/悬停概览/点击详情。
  **只回计数与布局**——不含 messages/facts/chunk 正文、embedding 与群号；用户簇标签为
  4 位哈希。本段初版只记了尾部的「移动端双指缩放」。
- **长期事实召回加入时间衰减**（**排序行为变化**）：冷区事实的相关度按时间衰减加权，
  只改排序、不改集合；衰减曲线可在 web「主动」页调参。

- **出站投递记账（outbox）**：提醒/推送/定时任务的投递结果（sent/failed/uncertain）逐行落
  JSONL（落点 `AGENT_OUTBOX_FILE`，默认 `data/state/outbox.jsonl`），只存字段不存正文；
  重启后私聊通知 SUPERUSERS 一份「悬空投递」清单——
  **只通知、绝不重发**（结果不确定时重发等于让用户收两遍）。
- **web 总览「待决定区」**：汇总三类「发生了但没有结论」的事项——出站悬空投递、被全局预算
  拦下的主动投递、还没跑成的一次性定时任务；只出字段不出正文。
- **全局打扰预算**：push / nudge / agent_turn 三类无人值守主动投递共用一个按日计数器与全局
  上限 `AGENT_NOTIFY_DAILY_LIMIT`（默认 8；计数落点 `AGENT_NOTIFY_BUDGET_FILE`）。此前
  agent_turn 完全没有上限——一条 `* * * * *` 的任务就能把一个群每分钟刷一遍；用户显式预约的
  提醒只记账不拦。
- **记忆星云支持移动端双指缩放**。
- **合成种子脚本 `scripts/seed_dev_data.py`**：开发库与生产彻底分离，幂等、规模可指定。

### 修复

- **cron 装配接线两处断裂（启动事故）**：`agent_turn` runner 与 `/cron` 命令面在部分
  启动路径下未接线，表现为「jobs.json 里有任务但从不触发、`/cron` 无响应」。已修并补
  装配冒烟测试（`tests/test_scheduler.py` 文件尾的 wiring 用例）。
- **群统计（PG 侧）**：① `upsert_daily` 的 hours 是整列覆写而非累加，同批次两行同
  (群,人,日) 时第一行的小时增量被永久覆盖（内存实现不受影响，双实现一致性缺陷）；
  ② flush 时 daily 已提交而 words 失败，旧代码把两批一起合并回内存，重试时 daily 被二次
  upsert 导致双计；③ retention 少算一天、多留一天；④ 原文缓冲内 message_id 重复被
  `ON CONFLICT` 静默吞掉。
- **人格成长层并发**：confirm 的 read-merge-write 与 maybe_trigger 的 bump/reset 各加
  per-user 锁，消掉「两个并发确认后写覆盖前一条」与「双 reset 产生两个提议」。
- **cron 永触发表达式**（如 `0 0 30 2 *`）：此前每 60s 烧一次完整 LLM 回合且永不标记
  failed，现在装载期拒载 + 执行期第二道闸。
- **提醒权限**：`reminder_list/cancel` 拿不到会话信息时显式拒绝，不再落到跨用户的管理员语义。
- **备份脚本**：`KEEP=0` 假成功（轮转把当天份也删掉还 exit 0）、`find` 失败被管道末端掩盖、
  config.yaml 位置二选一、人格目录改 `data/personas`、容器形态的状态文件路径探测。
- **技能沙箱**：只读/动作命令的子进程改最小环境（不再继承 `LLM_API_KEY`/`DATABASE_URL`），
  超时杀整个进程组（SSH 的 ControlMaster 不再成孤儿）。
- **识图预算**：embedding 交互墙钟预算现在夹住 POST 本身（实测最坏 135.2s / 30s 预算）。
- **设置并发写**：web 设置项加 per-key 乐观锁（落盘值摘要 + 两处校验），两个管理员同时改
  同一个键时后写者不再静默覆盖前写。
- **prompt 缓存**：system prompt 改「静态段前置、动态段置尾」，可缓存前缀不再被逐分钟变的
  时效锚点打穿；`LLM_PROMPT_CACHE` 开关默认关（部分自建网关会拒绝 cache 字段）。

### 文档

- NAS 部署指南按 main 真实容器路线重写——原稿描述的 JSON 配置路线在 main 上并不存在。
- 新增记忆架构速查表 `docs/memory-architecture-table.md`（七路数据→存储→注入位置、表结构、
  每轮时序、衰减、配置）。

## [1.0.4] - 2026-11-01

自 `1.0.3-beta` 以来的改动，按主题归类。

### Web 后台

- **登录门重构（2FA 前移）**：登录页一次输入 token + 2FA 码换**会话凭证**（默认 12h，`AGENT_WEB_SESSION_TTL_MIN` 可调），会话内编辑不再逐个收码——管理员反馈「每次 web 更新都要私聊取码太麻烦」。原始 token 只能调登录接口、数据面只认会话（防绕过 2FA）；失败按源 IP 15 分钟 5 次锁定；登出作废服务端记录。
- **弹窗改为真 modal**：点蒙层不再关闭、ESC 对有未保存修改的编辑框只提示、取消有修改时需两次点击；保存前的二次确认会**暂存编辑表单**，取消确认/写入失败带已填值原样恢复（管理员实测反馈「误触外面直接关闭还得从头填」）。
- 登录页版式对齐（顶栏与表单同缘）；页面 JS 增加 `node --check` + DOM shim 行为测试（pytest 抓不到 JS 语法错误）。
- **GHCR 多架构镜像发布**：合并 main 自动构建 `linux/amd64+arm64` 推 `ghcr.io/<owner>/agent-demo`（`latest` + `:sha-<短哈希>`），NAS 等部署机直接 `docker pull` 免本机构建。

### 群聊与定时任务（OpenClaw 兼容）

- **群聊上下文带发送时间**：注入格式 `群名片（HH:MM）：消息`（跨日 `MM-DD HH:MM`），围栏点破「括号内是历史时间」——窗口调大到小时级后模型能分清片段里谁在几分钟前说的。
- **cron 定时任务（OpenClaw 兼容）**：`data/cron/jobs.json`（格式可与 OpenClaw 互拷，手改即生效），运行态拆 `state.json` sidecar；**出 LLM 沙箱**（安全修复：配置不再落可写工作区）；投递白名单（群目标必须在 `ALLOWED_GROUPS`，fail-closed）、会话隔离 `cron:<job_id>`（不污染真实用户历史）、崩溃窗口不重投。模板见 `docs/examples/cron-jobs.example.json`，jobs.json 纳入 USB 冷备必含清单。
- **人格落点迁移**：`agentcore/personas/` 改为出厂模板，运行时权威目录 `data/personas/`（0600、容器挂载持久化——修 recreate 丢用户人格）；首启播种。
- **QQ 私聊命令通道**（superuser+私聊，两段确认+审计+改前备份）：`/cron`（list/add/del/on/off/run/confirm，新任务先停用试跑再启用）、`/persona new|edit`、`/openclaw import <目录>`（OpenClaw 工作区一键分拣导入，只增不改）。
- **技能 where 轴（群聊访客面）**：superuser-only 技能注册即自动「仅私聊」（群聊不可见，拒绝无探测口），群内加开走 `AGENT_GROUP_EXTRA_SKILLS`。

### 容器与部署

- 容器内 **web 受控写面可用**（`.env`/`config.yaml` 单文件挂载 + `/app` 属主 + entrypoint 修属权）；`data/state`/`mirror`/`ssh` 挂载——recreate 不再丢 2FA secret/provider 配置；db `shm_size 256mb`；psql 补 libedit2/libicu72（「备份能跑、恢复不能跑」防线）；compose 容器侧按 `PORT` 插值。

### 渲染

- 默认字体换**屏显臻宋（瘦金风格）**，动态文本统一走 `font_for` 逐段回落（Pillow 无逐字形回退，缺字会成零墨迹空白）。

### 其他修复

- **隐私止血**：仓库内真实群号/QQ 号全部打码（前 4 位 + ****）+ `TestQQNumberRedaction` 守卫。
- 设置页六键谎报「即时生效」修正（出站节流五键 + 图片预算是 import 期单例，改后需重启）；`AGENT_NUDGE_HOTSPOT_FILE` 补只读标志。
- 人格删除守卫改用 `PersonaManager.default()` 同源判定（`PERSONAS_DEFAULT` 指定的默认人格不再可被误删）。
- 测试策略分级：套件 3400+ 条后，「每步全量」退役——迭代中按改动面跑子集，提交前全量一次 + CI 兜底（详见 AGENTS §3）。

### 文档与流程

- PR #6 评审报告（3H/22M/24L）与架构定夺 AC 入库；外部贡献处置说明与合并方案（`review/PR6-disposition.md`）；OpenClaw 化整体方案（`review/FIX-openclaw-20261101.md`）。


### 修复（1.0.4 评审批次，随 1.0.4 发布）

1.0.4 评审（[`review/REVIEW-63f9444..858afcc.md`](review/REVIEW-63f9444..858afcc.md)）的修复批次，
明细见 [`review/FIX-63f9444..858afcc.md`](review/FIX-63f9444..858afcc.md)。

### 修复

- **备份**：U 盘冷备的 cron `jobs.json` 此前只进必含校验清单、没有复制语句——配置过 cron 任务的部署每周冷备必失败或静默缺件，现已真实复制（存在判定与复制都用 `$SRC` 前缀）。
- **cron 无人值守安全姿态**：定时任务的 agent 轮次改以 `cron:<任务id>` 伪用户执行（非超管）——无人值守轮次一概看不到 superuser 技能（私聊目标也不例外）、不写任何用户的成长层；`/cron run` 试跑与到点执行串行化（不再双投递）；`/openclaw import` 导入的条目一律先停用（试跑后再启用）。
- **OpenClaw 迁移可用性**：纯中文任务名不再被 id 校验拒绝；中文人格名导入不再崩溃（折叠为合法名并注明）；`/persona new|edit` 保留多行正文；QQ 通道编辑人格现在真的会留改前备份（此前回执谎称有）。
- **提醒权限**：`reminder_list/cancel` 收到空会话信息时显式拒绝（此前会静默落到跨用户的管理员语义）；超管跨用户取消提醒补审计留痕。
- **web 后台**：登录失败审计补来源 IP；无效 token 计入限速（15 分钟 5 次，锁定后静默 429 不再刷诊断面板）；登录失败键过期清理；群上下文片段的跨年时间戳补年份。
- **文档/字体**：`data/fonts` 补许可状态说明（屏显臻宋未随附授权文本，第三方再分发需自行核实）；`.env.example` 修正「compose 层变量不注入容器」的错误注释并归位 `AGENT_CRON_DIR`。


## [1.0.3-beta] - 2026-10-29

自 `1.0.1-beta` 以来的改动，按主题归类。

### 主动联系（nudge）

- **沉默检测器 P1–P3**：按沉默时长择机搭话、热点监测与爆点抢占、记忆事件回访；Web 新增「主动」标签（沉默检测器状态 + 全参数编辑，两段确认 + 2FA + 审计 + 改前备份）。
- **加固**：事件当日结果回访；未回复只数普通搭话（避免自问自答刷屏）；同群并发锁；配置容错。
- 验收标准归档 [`review/AC-proactive-nudge.md`](review/AC-proactive-nudge.md)。

### 部署

- **容器化部署**（新增）：`docker compose up -d db bot` 一条命令起全栈。镜像含 ffmpeg / PG 16 客户端 / tzdata / 文泉驿字体四件易漏能力（缺任何一件都是静默降级）；`data/` 只挂运行期子目录（整卷挂 `/app/data` 会遮掉字体，文字卡片静默失败）；运行身份由 `scripts/docker-entrypoint.sh` 降权到 `app`。详见 [docs/operations.md](docs/operations.md) 的「容器化部署」。
- **`SUPERUSERS` 空值不再启动即崩**（修复）：照 `.env.example` 模板生成的 `.env` 未填超管时，模板默认是空行、注释承诺「留空 = 没有超管」，而 `bot.py` 只把**非空**值转成 JSON 数组，空值原样穿透给 NoneBot → pydantic-settings 对「存在但为空」的 complex 字段照样 `json.loads("")` → `SettingsError`，宿主机与容器都起不来（容器表现为 restart 循环）。现在空值/纯空白规范成 `"[]"`。
- **LLM 配置双份不再靠猜**（修复，2026-10-28）：`data/llm-providers.json` 生成后即为唯一权威，`.env` 的 `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` 从此不参与运行（只有文件缺失/损坏才回落），但 .env.example 仍把这组摆在最显眼处——用户改完重启发现没生效、只能靠「删文件重启」摸索（用户反馈原话：两份配置「兜一大圈还多余」）。现在启动期做一次漂移检测：env 侧**确实填了**（非空、非 `...` 占位）且与文件不一致 → 一条 WARNING，只报字段名（不含 key 值），并给出两条正路（web「模型」标签 / `/model`；或用 .env 覆盖时备份并删文件后重启）。`.env.example` 的 LLM 段改成「一次性种子」措辞，docs/configuration.md 与 docs/features-interaction.md 各补一张「想改什么 → 改哪儿」的表。迁移与文件权威语义**未变**（不破坏多 provider / 双协议 / 降级链）。
- **容器首启自举**（新增，2026-10-28）：`docker compose` 解析期就要求 `env_file` 指向的 `.env` 存在（缺了连 `db` 都起不来：`env file …/.env: no such file or directory`），而模板是隐藏文件、`data/` 运行期子目录也不在版本控制里——新用户很容易卡在「手动拷 .env / 手动 mkdir data」这三步。新增 `python scripts/deploy.py docker [--up]`：从模板生成 `.env`(0600) + 建 `data/` 运行期子目录 + `docker compose config -q` 预检，幂等（已有 `.env` 绝不覆盖），尊重 `AGENT_DATA_DIR`（相对仓库根解析，与 compose 同口径），LLM 三件套仍占位时只 warn 不阻断（严格放行门仍是 `deploy.py check`）。目录清单与 compose 挂载点由 `tests/test_deploy.py::test_data_dirs_match_compose_mounts` 锁死。README「Docker 部署」与 docs/operations.md「首次部署」同步。
- **ARM 构建修复**（修复，2026-10-27）：`Dockerfile` 取 libpq 时写死了 amd64 的 multiarch 目录 `/lib/x86_64-linux-gnu`，导致 `linux/arm64` 构建（绿联 ARM NAS 实测）在 COPY 阶段以 `lstat /usr/lib/x86_64-linux-gnu: no such file or directory` 中止，后面一行都跑不到。改为 `/lib/*-linux-gnu/libpq.so.5*` 由构建期按目标架构匹配，amd64/arm64 同一行覆盖；用纯 `COPY` 而非 `RUN cp`，因此在 amd64 上交叉构建 arm64 也不需要 qemu/binfmt。回归双层：`tests/test_docker_deploy.py` 新增静态断言禁止写死 multiarch 路径，另加 `.github/workflows/docker-arm64.yml`（qemu 全量 arm64 构建 + 实跑 `pg_dump`/`psql` 验证 libpq 能加载）。文档同步 README「Docker 部署」、docs/operations.md「构建」、AGENTS.md「容器化」不变量。
- **国内构建补 PyPI 镜像参数**（修复，2026-10-26 起，10-27 定稿）：新增 `--build-arg PIP_INDEX_URL=<国内镜像>`。此前 `APT_MIRROR` 只换 apt 源，builder 的 `pip install uv` 与两处 `uv sync` 仍走 PyPI 官方源（腾讯云实测 uv 的 20MB wheel 下载 0 进度直至 pip 超时）；新参数覆盖这三处下载，腾讯云内网可用 `http://mirrors.tencentyun.com/pypi/simple`。默认空 = 官方源，不影响境外构建。四个坑逐一修：① pip 25 对 http 源要求 `PIP_TRUSTED_HOST` 否则整源拒绝（http 形态自动从 URL 提取）；② `UV_DEFAULT_INDEX_URL` 不重写 uv.lock 记录的 wheel URL，下载仍全打官方源（实测 120 请求全 `files.pythonhosted.org`）；③ `UV_INDEX_URL` 触发 re-resolve 与 `--locked` 冲突——最终方案是构建层内 sed 改 **lock 副本**的 wheel URL 指向镜像（镜像以官方同构路径 `/pypi/packages/` 提供文件，腾讯/阿里实测）+ `--frozen` 安装：版本与 wheel hash 仍按 lock 强制（镜像文件与官方不符会被 hash 拒绝），lock 新鲜度由 CI 的 `--locked` 承担，默认路径保持 `--locked` 原语义；④ uv 对显式 http index 直接放行无需 allow-insecure-host（实测 0.12）。
- **容器化部署**（新增）：`docker compose up -d db bot` 一条命令起全栈。镜像含 ffmpeg / PG 16 客户端 / tzdata / 文泉驿字体四件易漏能力（缺任何一件都是静默降级）；`data/` 只挂运行期子目录（整卷挂 `/app/data` 会遮掉字体，文字卡片静默失败）；运行身份由 `scripts/docker-entrypoint.sh` 降权到 `app`。详见 [docs/operations.md](docs/operations.md) 的「容器化部署」。
- **容器内沙箱收敛**（新增）：容器部署刻意不依赖 systemd、不碰宿主机——`systemctl`/`journalctl`/`dmesg` 一律拒并返回说明文案（旧文案「命令不存在」会让模型换参数重试、白烧 token），`/reboot` 跳过 C 步外部命令直接 `os.execve` 自替换（外部命令白名单全是宿主机设施，容器里 spawn 即透出沙箱），备份用镜像内 PG 16 客户端。判定统一走 `agentcore/runtime_env.py` 的 `in_container()`。
- **部署文档补齐**（新增）：README 环境要求补 ffmpeg（宿主机部署不装则点歌技能静默不注册）、快速开始补安装命令、Docker 部署段落补大陆 VPS 构建参数（`--network host --build-arg APT_MIRROR=mirrors.aliyun.com`，原因是 docker 默认 DNS 8.8.8.8 被污染导致 apt 源解析到假 IP）。顺带修掉 `container_id()` 在 cgroup v2 混合形态下漏剥 `.scope` 导致 `docker_ctrl` 自保护静默失效的缺陷。
- 私聊仅放行 superuser（`acl.py` 私聊分支）的行为不变；群聊唤醒词/@ 不变。

### 对话与引擎

- **命令面统一 `/` 前缀，`/aihelp` 改名 `/help`**（2026-10-27）：帮助命令主名从 `/aihelp` 改为 `/help`，`/aihelp` 与「帮助」保留为别名（非破坏）。帮助菜单改为**单一数据源**（`plugins/qq_agent_adapter/help_menu.py`，文本与图片共用同一份 `SECTIONS`），并补全了此前漏登的命令面：`/词云`、`/发言榜`、`/memory`、`/2fa`、`/model`、`/growth`、`/reboot`、`/reset`、`/status`、`/usage`、`/push`、`/nudge`、点歌/放歌、定时提醒、`/skill catalog|install|list|uninstall|info`。AST 守卫锁死「已注册命令必须都在菜单里」。
- **群统计免前缀触发已撤除**：`9dc74b3` 曾给 `/词云`、`/发言榜` 挂过免前缀 `on_message` 精确匹配入口，与「群聊只认唤醒词或 @机器人」的成文路由不变量冲突，`8519210` 已完整撤除，命令面统一带 `/` 前缀。
- **时间感知三件套**（2026-10-26 线上：凌晨 3:24 对「每天 14:30 推送」回复「还有十几分钟」，实际差 11 小时；当日二次修正：历史时间前缀第一版用裸 `（HH:MM）`，被模型当成当前时间用并照搬进回复格式，改为自解释的 `（此消息发送于 …）`；三次修正：首版「必须先查表」规则过严，按用户反馈放宽为「时间背景」——给参考、不强制查表）：prompt 时效锚点从「只有日期」扩为「日期 + 星期 + 时刻」；工作流新增「时间背景」说明（当前日期/星期/时刻 + 历史消息各自的发送时间，需要时间感时可参考，要报精确时刻/倒计时再调 `now`/`date_calc`，拿不准不给具体时间）；历史 user/assistant 消息进 prompt 时带自解释的发送时间前缀（同日 `HH:MM`/跨日 `MM-DD HH:MM`）；回复侧禁止主动加时间戳/模仿该前缀格式，并有 `_strip_imitated_time_prefix` 兜底剥离（线上两次复现：回复以「（08:43）」「（11:20）」开头）。
- **历史 token 预算计入时间前缀**（修正）：前缀在投影阶段才拼上，而预算估算跑在之前——200 条短消息场景实测低估 162% 且超出 `agent.history_token_budget`，「保守估算」不变量被破坏。现在每条带 `ts` 的 user/assistant 历史加一份前缀津贴。
- **回复长度归人格管**：私聊不再默认「适当详细」，群聊不再要求「有条理」。
- **at 段带群名片**：@ 到谁不再只有裸 QQ 号；被 @ 的人是第三方，不是发言者。
- **工具循环熔断**：同一工具调用反复失败即中止本轮，中止轮次不再毒化历史。
- 私聊空消息不进引擎；历史里的空轮次在读路径剔除。

### Skills 与工具

- **搜索格式预装扩到 8 种**（2026-10-29）：新增 `baidu`（百度 AI 搜索，千帆 `/v2/ai_search/web_search`，Key 形如 `bce-v3/ALTAK-…` 整串）、`zhipu`（智谱 `/api/paas/v4/web_search`）、`serper`（Serper.dev Google 代理）、`exa`（`x-api-key` 头鉴权）、`firecrawl`（Firecrawl v2 `/v2/search`，Bearer，响应取 `data.web` 组、`success:false` 判失败）五种请求/响应格式——此前 web 模型页搜索源的格式下拉只有 bocha/tavily/stepfun，想配百度无从下手（选别的格式连百度端点必失败）。格式注册表收敛为 `search_providers.FORMATS`（有序 dict，单一事实源），web 下发 `model_config.search.formats`、前端下拉动态渲染——以后加格式只改后端一处；`SEARCH_PROVIDER` 的设置页预选项与 env 回落校验同源同步。
- `send_workspace_file`：按路径原样发送工作区文件。
- **Home Assistant**：`ha_state`（只读，low+）/ `ha_control`（有副作用，medium+），实体白名单 + 凭据只来自 `.env`。

### 记忆与隐私

- `/memory` 自助：查看/删除当前会话事实、清空当前会话；群聊不能跨会话删。
- **`AGENT_LOG_CONTENT`**：置 `0` 后适配层 `[msg]`/`[reply]` 与引擎侧内容日志（LLM 输出、技能参数、事实/蒸馏原始输出、提醒与主动消息正文）统一只记 `<len=N>` 与段类型。
- **age recipient-only 加密备份**：`AGENT_BACKUP_ENCRYPT=age` + `AGENT_BACKUP_AGE_RECIPIENT`（bot 只持公钥，私钥离线保管）。

### 存储与迁移

- **版本化迁移器**：`agentcore/memory/migrations/*.sql` 为记忆库 DDL 唯一来源，`migrate.py` 以 advisory lock + 校验和 + 版本/空洞守卫 + 每迁移一事务应用；启动即迁移，`scripts/migrate.py status|up|repair|create`。
- **fail-closed 收口**：迁移/修复执行期的语法、权限、SQL 运行期异常统一包成 `MigrationError`，启动路径不再降级内存（否则记忆看似持久化、重启即丢）。
- 恢复前先清空目标库旧账本（恢复进空库/非空库都成立）；加密备份的 `.schema.json` sidecar 按解密后名字命名，旧名仍兼容读取；维度探测与运行时同源、TRUNCATE 与 ALTER 同事务、修复步骤与迁移共用 advisory lock。
- 群统计四表仍由 `init()` 自建，作为登记例外接入 BACKLOG §2.8 并加表清单守卫。

### 群统计

- **词云只收内容词**：分词后按词性过滤——保留名词/动词/英文技术词/形容词/拟声词，丢掉副词/连词/代词/数词/介词与英文虚词（`by/many/really`），并叠加「东西/感觉/还有/喜欢」等通用高频词黑名单；读取旧数据时同口径过滤，当前保留窗口立即变干净。

### 工程与 CI

- **uv 锁 + pyright 门禁**：CI 以 `uv sync --locked` 校验 lock 新鲜度、`uv run --no-sync` 执行，测试加覆盖率阈值（≥80%）；`scripts/deploy.py install` 走 uv，缺 uv 明确报错（不回退 pip）。
- **可维护性重构（H1–H4）**：拆分 Web/引擎热点、双存储收敛共享辅助函数。
- **评审整改**：REVIEW-ee39c8e..e55cab5 的 1H/9M/14L 全部落地，详见 [`review/FIX-ee39c8e..e55cab5.md`](review/FIX-ee39c8e..e55cab5.md)。

### 修复

- **Web 2FA 确认码过期 / 备份下载无反应**：写面 challenge 时效由 30s 对齐到 TOTP 的 5 分钟——原窗口短于「私聊 bot 取码再回来填写」的往返，备份下载等操作提交时报 `confirm_invalid_or_expired`；下载改为一次性 HttpOnly 票据 + 浏览器原生流式 GET，不再把 183MB 备份读成 JS Blob（手机/内嵌浏览器上会静默不下载）。详见 BACKLOG §6.16。
- **工具循环收尾与抓取熔断**：步数用尽先追加一次无工具收尾调用（基于已有工具结果作答，空内容才回「想得太久」）；一轮内 `summarize_url` / `fetch_url` 连续失败 3 次即停用抓取工具并提示用 `search_web` 已有结果回答；`agent.max_iterations` 移到 Web「模型与 LLM」簇（「思考步数」），写入后热更新运行中的 engine。详见 BACKLOG §6.17。
- **评审修复（REVIEW-e55cab5..4d5c1b8）**：词云轮询用例解除 ISO 周奇偶依赖（本周 CI 连红根因）；tool-loop 收尾调用补当日预算硬闸并标注 length 截断；设置总览「思考步数」读运行值并移入即时生效区；词云大写英文缩写不再被停用词误杀；posseg 词性表重建窗口降级为纯分词；备份下载票据 IP 严格相等；usb 备份脚本补 KEEP/MAX_AGE_H 校验；ruff pin 三方一致性守卫测试；文档数字/口径同步（详见 `review/FIX-e55cab5..4d5c1b8.md`）。

## [1.0.1-beta] - 2026-10-21

自 `1.0.0`（tag `v1.0.0b1`）以来的改动，按主题归类。

### LLM 与模型

- **多 LLM Provider**：`data/llm-providers.json`（0600、原子写、备份、写后校验）支持 OpenAI / Anthropic 双协议，web 模型页 CRUD / 设主 / 排序 / 测试连接，key 只写不回显；运行时 `/model` 切模型（探测通过才切）。
- **模型页 Embedding / 搜索配置（M3/M4）**：embedding 端点/key/模型/维度与搜索配置入口；`SEARCH_API_KEY` / `SEARCH_BASE_URL` / `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` 四个凭据从「走 SSH 不可写」放开为 web 可写（只写不回显、改后重启生效）。
- **联网搜索多源 + fallback 容错**：`data/search-providers.json`（0600，同 llm-providers）支持多搜索源，按列表顺序 fallback——一个源失败（超时/断连/非 2xx）自动切下一个；env 单配置回落兼容。
- **stepfun（阶跃星辰）搜索格式**：新增第三种搜索格式（Bearer 鉴权、请求 `{query, n}`、解析 `results:[{url,title,time,snippet,content}]`）。

### Web 与设置

- **17 个设置簇**：设置页折叠 + 锚点 + 跨簇搜索，gate 关闭时同簇后端拒写（409）；多 provider 面板 + 群统计双周视图。
- **顶层「模型」「人格」标签**：provider 面板与人格导入/编辑/删除从设置页搬入顶层标签，防双入口漂移。
- **写面 2FA（TOTP 双因素）**：两段确认的第二段从时间窗口升级为 5 分钟滚动码（RFC 4226 HOTP，secret 0600）；superuser 私聊 bot 发 `/2fa` 取码；challenge HMAC 绑定操作 + 30s 时效 + nonce 防重放 + 同源 IP 失败锁定。
- **「备份」标签**：本地备份列表（最新年龄/大小/磁盘/镜像状态）+ 显式「立即备份 / 校验 / 删除非最新一份 / 下载」；写操作与下载均 2FA + 审计，下载文件名白名单防穿越。
- 手机端适配（640px 单列）、退出登录、header 挤压修复。
- 修复人格标签表单被 5s 轮询清空的问题。

### 人格

- **群人格**：每群独立、群内共享；assistant 默认位让出。
- `PERSONAS_DEFAULT`：本机用某人格当默认位（与仓库 md 的 default 解耦），私有人格不必改 assistant.md。

### 群统计

- 修复含 NUL 群名片毒化全部统计 flush（落库前剥控制字符）。
- 群历史图片可按上下文回捞 + 统计白名单可收窄。

### Skills 与工具

- `xiaoheihe_post`：小黑盒帖子抓取（分享链接 → 标题/作者/正文）。
- `npm_update`：登记表制 npm 全局包更新（medium+）。
- `render_text_image`：文字/歌词/长文 → 项目风格长图（纸色底、墨线面板）；
  门禁：6000 字软上限截断 / 12000 字硬上限拒绝 / 画布高 ≤4000px / 渲染 10s 超时；
  无中文字体、渲染失败或图片过大均降级为文本，发送结果未知时不重发。
- `/skill list`：已安装技能查询命令。
- 支持 `data/skills/*.py` 自定义工具型技能。

### 诊断与日志

- 未触发群消息补 `[trigger-miss]` 诊断行。
- 工具调用与回合三层耗时日志（LLM step / skill done / reply）。

### 沙箱与安全

- 解压线退役外部 `zip`/`unzip`/`tar`，改用标准库实现（0600 落盘）。

### 备份与恢复

- **异地镜像与完整冷备**：`scripts/backup_mirror.sh`（NAS/VPS 通用目标、`MAX_GB` 目录级闸门、先 prune 再同步、远端 SHA256 校验、VPS unit `REQUIRE_PRIVATE_IP=1` 强制 WG 内网）与 `scripts/backup_usb.sh`（dump/配置/人格/运行态整包 + `MANIFEST.sha256` + 8 份轮转 + `mountpoint` 挂载判定）；6 个 systemd unit + `OnFailure` 失败标记。
- restore 过滤 pg17 dump 的 `SET transaction_timeout`（旧版 psql 不认识的会话参数；只滤 COPY 块外的顶格 SET 行）。

### 工程与文档

- CI 升级 `actions/checkout@v5` 与 `setup-python@v6`。
- 全量文档体检修复漂移 + 清理过期继任文档。
- `data/search-providers.json` 加入 `.gitignore`（含 key 运行时产物，防误提交泄漏）。
- **消息管线**：删除「自动发文件」关键词兜底（线上两次把正文吞成「文件已发送」），发文件完全由 LLM 自主调 `send_markdown_file`。
