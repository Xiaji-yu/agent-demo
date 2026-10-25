# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)与 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 格式。
版本号以 `pyproject.toml` 为准（PEP 440：`1.0.3b1` = 1.0.3-beta）。

## [1.0.3-beta] - 2026-10-25

自 `1.0.1-beta` 以来的改动，按主题归类。

### 主动联系（nudge）

- **沉默检测器 P1–P3**：按沉默时长择机搭话、热点监测与爆点抢占、记忆事件回访；Web 新增「主动」标签（沉默检测器状态 + 全参数编辑，两段确认 + 2FA + 审计 + 改前备份）。
- **加固**：事件当日结果回访；未回复只数普通搭话（避免自问自答刷屏）；同群并发锁；配置容错。
- 验收标准归档 [`review/AC-proactive-nudge.md`](review/AC-proactive-nudge.md)。

### 对话与引擎

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
