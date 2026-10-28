# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)与 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 格式。
版本号以 `pyproject.toml` 为准（PEP 440：`1.0.3b1` = 1.0.3-beta）。

## [1.0.3-beta] - 2026-10-25

自 `1.0.1-beta` 以来的改动，按主题归类。

### 主动联系（nudge）

- **沉默检测器 P1–P3**：按沉默时长择机搭话、热点监测与爆点抢占、记忆事件回访；Web 新增「主动」标签（沉默检测器状态 + 全参数编辑，两段确认 + 2FA + 审计 + 改前备份）。
- **加固**：事件当日结果回访；未回复只数普通搭话（避免自问自答刷屏）；同群并发锁；配置容错。
- 验收标准归档 [`review/AC-proactive-nudge.md`](review/AC-proactive-nudge.md)。

### 部署

- **容器化部署**（新增）：`docker compose up -d db bot` 一条命令起全栈。镜像含 ffmpeg / PG 16 客户端 / tzdata / 文泉驿字体四件易漏能力（缺任何一件都是静默降级）；`data/` 只挂运行期子目录（整卷挂 `/app/data` 会遮掉字体，文字卡片静默失败）；运行身份由 `scripts/docker-entrypoint.sh` 降权到 `app`。详见 [docs/operations.md](docs/operations.md) 的「容器化部署」。
- **`SUPERUSERS` 空值不再启动即崩**（修复）：照 `.env.example` 模板生成的 `.env` 未填超管时，模板默认是空行、注释承诺「留空 = 没有超管」，而 `bot.py` 只把**非空**值转成 JSON 数组，空值原样穿透给 NoneBot → pydantic-settings 对「存在但为空」的 complex 字段照样 `json.loads("")` → `SettingsError`，宿主机与容器都起不来（容器表现为 restart 循环）。现在空值/纯空白规范成 `"[]"`。
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
