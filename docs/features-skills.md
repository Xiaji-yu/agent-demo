# 工具、沙箱与权限

> 本文自 `README.md` 拆分而来（2026-10-16）；返回 [README](../README.md)。

## 配置一览

> **必配** = 不设置则该功能不可用（示例值即占位符）；**选配** = 有可用默认值或仅用于调优。全局必配见 [configuration.md](configuration.md)。

| 配置项 | 标注 | 默认 | 说明 |
|---|---|---|---|
| `SUPERUSERS` | **必配**（管理能力） | 空 = 无管理员 | 所有动作类 skill 的前置门（schema + handler 双层） |
| `AGENT_PERMISSION_LEVEL` | 选配 | `medium` | `low` / `medium` / `high` 能力上限 |
| `WORKSPACE_DIR` | 选配 | `data/workspace` | `fs_*` / `run_command` 根目录 |
| `AGENT_KILL_CONFIRM_TTL` / `AGENT_BUILD_TIMEOUT` | 选配 | `120` / `300` | 两段确认 TTL / 构建脚本超时 |
| `AGENT_DB_CONNECT_TIMEOUT` / `AGENT_DB_TIMEOUT_MS` / `AGENT_DB_MAX_ROWS` | 选配 | `10` / `10000` / `200` | `db_query` 限制 |
| `AGENT_EXTRACT_MAX_ENTRIES` / `AGENT_EXTRACT_MAX_MB` | 选配 | `20000` / `1024` | `extract`/`pack_zip` 解压炸弹上限（条目数 / 总大小 MB） |
| `AGENT_FETCH_ALLOW_RANGES` | 选配 | `198.18.0.0/15` | `fetch_url` SSRF 白名单 |
| `AGENT_LOG_ALLOWLIST` | 选配 | `/var/log` | `log_tail` 允许目录 |
| `AGENT_SSH_HOSTS` + `AGENT_SSH_KEY_<名>` 或 `AGENT_SSH_PASSWORD_<名>` | **必配**（启用 `ssh_run`） | 空 = 整个功能关闭 | 内网主机清单与凭据（密码不进 argv/日志） |
| `AGENT_SSH_KNOWN_HOSTS` | 选配 | 默认路径 | 不得落在 fs 可写工作区内 |
| `AGENT_HA_BASE_URL` + `AGENT_HA_TOKEN` + `AGENT_HA_ENTITIES` | **必配**（启用 `ha_*`） | 空 = 整个功能关闭 | HA 地址 / 长期令牌 / 实体登记表；令牌不进 Web 写面 |
| `AGENT_HELP_IMAGE` | 选配 | `1` | `/aihelp` 图片菜单（缺 Pillow/字体自动退回文本） |
| `AGENT_SKILLS_DIR` | 选配 | `data/skills` | prompt 型（`*.yaml`）与自定义代码型（`*.py`）技能共用目录 |

## 工具集（skills）

模型可调用的工具按用途分组（`/skills` 可查当前可见性；`superuser` 类仅管理员可见）：

| 分类 | 工具 | 权限 | 说明 |
|---|---|---|---|
| 信息 | `search_web` | public | 联网搜索（需 `SEARCH_API_KEY`；结果带发布日期，query 不写死历史年份） |
| 信息 | `search_multi` | public | 多查询并行搜索、去重合并（一次问多个方面） |
| 信息 | `fetch_url` | public | 抓网页正文，**含 SSRF 防护**，结果按不可信数据围栏 |
| 信息 | `xiaoheihe_post` | public | 抓小黑盒（xiaoheihe.cn）帖子：标题/作者/时间/正文/图片（分享链接 302 到 JS 空壳，`fetch_url` 抓不了，走官方匿名接口 + 本地 hkey 签名） |
| 信息 | `summarize_url` | public | 抓取 + 中文摘要（先一句话概括，再列要点） |
| 信息 | `current_model` | public | 回答"你当前用哪个模型"：生效型号 + 主/备线路（降级中会说明主模型当前不可用） |
| 信息 | `translator` / `summarizer` / `polisher` / `coder_reviewer`（prompt 技能，`data/skills/*.yaml`） | public | 互译 / 摘要 / 润色 / 代码评审 prompt 技能，即装即生效 |
| 信息 | `wiki_prts` / `wiki_blhx` | public | MediaWiki 在线直查（`AGENT_WIKI_SITES` 选启用站点）。见 [features-memory-and-kb.md](features-memory-and-kb.md) |
| 实用 | `now` / `date_calc` | public | 当前时间、星期几、日期加减、天数差 |
| 实用 | `unit_convert` | public | 长度/重量/数据/时间/速度/面积/温度换算（中英文单位） |
| 实用 | `random` | public | 抽签、随机数、骰子（2d6+3）、抛硬币 |
| 实用 | `calc` | public | 安全算术（四则/幂/取模 + sqrt/round/log 等函数白名单） |
| 实用 | `get_weather` | public | 天气查询（wttr.in），可带未来几天预报 |
| 文件 | `send_markdown_file` / `send_workspace_file` | public / **superuser** | `content` = 把文本存成文件发出去；`path` = 把工作区里的文件**原样**发出去（服务端直读，不经模型、无 6000 字截断；`send_workspace_file` 是它的 superuser 别名）。群聊自动上传群文件；上传失败如实告知，**不会改成私发** |
| 图片 | `render_text_image` | public | 把文字/歌词/长文渲染成项目风格长图（纸色底、墨线面板）直接发到当前会话；6000 字软上限（超出截断并在图内标注）、12000 字硬上限（拒绝并提示摘要/分段）、标题 100 字软截断、画布高 ≤4000px、渲染 10s 超时 + 并发闸门 2；无中文字体或渲染失败降级为文本 |
| 音乐 | `play_music` | **restricted** | 群内语音放歌（需 `AGENT_MUSIC_API_URL` + `NAPCAT_HTTP_URL` + pysilk/ffmpeg；**群白名单硬闸**在 handler 内，未开放群调用即拒）。见 [features-interaction.md](features-interaction.md) 的「点歌 / 放歌」一节 |
| 阶段 | `reminder_add` / `reminder_list` / `reminder_cancel` | public | 定时提醒（见 [features-community.md](features-community.md) 的「定时提醒」一节） |
| 社区 | `my_group_review` | public | 群发言周点评（人格化锐评），per-user 冷却 + 同周缓存。见 [features-community.md](features-community.md) |
| 运维 | `system_status` | **superuser** | 主机概览：负载/内存/磁盘/进程/GPU/Docker（含本机信息，仅管理员） |
| 运维 | `proc_detail` / `disk_usage` / `port_check` / `service_status` / `log_tail` | **superuser** | 进程、磁盘、端口监听、systemd 服务、日志尾部（全只读） |
| 动作 | `service_ctrl` / `docker_ps` / `docker_logs` / `docker_stats` / `docker_ctrl` / `proc_kill` / `run_build_script` / `npm_update` | **superuser** | 管理员动作类：服务/容器控制（自保护：拒 bot 自身与 DB 容器）、两段确认杀进程、构建脚本、npm 全局包更新（登记表制）；按 `AGENT_PERMISSION_LEVEL` 分档（见「权限级别与管理员动作类能力」一节） |
| 数据 | `db_query` | **superuser** | 在 `.env` 的 PostgreSQL 上执行**一条只读** SELECT/WITH，结果表格过围栏（未配 `DATABASE_URL` 即关闭；medium+ 级别） |
| 工作区 | `fs_list/read/write/mkdir/delete`、`send_workspace_file`、`run_command` | **superuser** | 沙箱工作区（low 档只有读与只读命令子集，见「权限级别」一节） |
| 远程 | `ssh_run` | **superuser** | SSH 登录 `.env` 登记的内网主机执行写死的只读诊断（见「SSH 远程只读诊断」一节） |
| 家居 | `ha_state` / `ha_control` | **superuser** | Home Assistant 状态查询与设备控制：实体白名单登记、动作按 domain 枚举、敏感域两段确认（见「Home Assistant 智能家居」一节） |
| Shell | `run_shell` | **superuser** | `bash -c` 任意命令，**high 级专属**（见「run_shell」一节） |

**`fetch_url` 的安全边界**：只允许 http/https；解析后的所有 IP 必须是公网地址，
内网/回环/链路本地/云元数据（`169.254.169.254`）一律拒绝；不自动跟随重定向，
逐跳重新校验；限 2MB / 15s。抓回的正文按「不可信数据」围栏后再交给模型
（网页是典型的间接 prompt 注入载体）。

**`xiaoheihe_post` 的安全边界**：小黑盒分享链接（`api.xiaoheihe.cn/v3/bbs/app/api/web/share?...`）
实测 302 到 JS 渲染的空壳页面，`trafilatura` 抽不到正文，故本技能不走 HTML 路线，
而是调小黑盒 web/H5 端**自用的匿名 JSON 接口**（`/bbs/app/link/tree`），帖子本体在
`result.link`。边界：① link_id 用正则约束（`[0-9a-zA-Z]`，6~32 位），API 主机与路径
**硬编码**，LLM 给出的任何文本都进不了 URL 结构；② 连接前对 API 主机复用
`fetch_url` 的 SSRF 校验（内网/回环/保留段一律拒绝）；③ 匿名接口**无凭据**，
请求不带 cookie；④ hkey 签名按官方 bundle 算法**本地复算**（无浏览器/JS 依赖），
请求带官方 web 端同款公共参数；⑤ 正文过统一不可信围栏；⑥ 限 15s / 2MB / 输出
6000 字。**频次风控**：请求过于频繁时接口返回 `show_captcha`，技能会转告
「稍后再试」——按需抓取，不要拿它做轮询/监控。

> **`/reboot` 部署前提**：执行链末端是「退出进程等 supervisor 拉起」——用 systemd
> 需 `Restart=always`（`Restart=on-failure` 不会因 exit 0 重启），用 compose 需
> `restart: unless-stopped`。**裸 `nohup python bot.py &` 部署在 re-exec 也不可用时
> 重启即死亡**；那种部署请配置 `AGENT_REBOOT_CMD` 走外部命令。外部命令 spawn 后
> 有 0.5s 观察窗：子命令秒退非零（unit 名拼错、docker 未起等配置错误）会**自动
> 回退进程内 re-exec**，不再退出干等一个起不来的 supervisor。

> **`/reboot` 时序注意**：命令响应器是模块级注册，只有 bot **进程启动时**才装好。
> 所以「更新了代码后的第一次重启」只能手动做（`systemctl restart agent-demo` /
> `supervisorctl restart ...`），之后 `/reboot` 和 web 重启按钮才可用、也才带上新代码。
> 症状对得上就是忘了这步：私聊 `/reboot` 没重启，反而由云崽当普通消息答了。

> **`.env` 改动何时生效**：re-exec 自替换只带进程**出生时**的真实环境变量，`.env`
> 由新进程从文件重新读（2026-10-12 修复；此前继承当前 environ，上一轮 `load_dotenv`
> 注入的旧值会熬过重启，「改 `.env` + `/reboot`」静默不生效）。**例外**：deploy 脚本
> 生成的 unit 带 `EnvironmentFile=.env`——`.env` 内容由 systemd 在 unit 启动时读入
> 环境层，改 `.env` 后要 `systemctl restart agent-demo`（让它 re-read EnvironmentFile），
> `/reboot` 带过去的仍是 unit 启动时的旧值。web「全部设置」的可写键只改配置文件（`.env` / `config.yaml`）
> 本身，同样按上述规则在重启后生效。

> **重启完毕回执**：`/reboot` 重启完成后，云崽会往**同一条会话**补发一条
> 「☁️ 重启完毕，耗时 X.Xs，我回来了。」——耗时从指令生效算到新进程连上协议端
> （含宽限、停机 flush、exec、启动）。实现上是在 perform_reboot 前把「回执目标 +
> 起始时间」原子落盘到 `data/reboot-notice.json`，新进程由 bot 连接钩子一次性
> 消费（web 重启没有会话上下文，不发回执）。文件位置可用 `AGENT_REBOOT_NOTICE_FILE` 覆盖。

> **已知残留（如实披露）**：IP 校验与实际连接是两次独立的 DNS 解析，存在 DNS rebinding
> 的 TOCTOU 窗口（短 TTL 域名在校验后切到内网地址可绕过）。彻底方案是把已校验的 IP
> 钉进连接层，`media.py` 的图片抓取有同样的残留——当前均以「白名单 + 代理场景放行段
> 可配置」缓解。

> 透明代理（Clash 等 fake-IP）会把外网域名解析到 `198.18.0.0/15`，该段默认放行；
> 置空 `AGENT_FETCH_ALLOW_RANGES` 可切到严格模式。

**`log_tail` 只能读 `AGENT_LOG_ALLOWLIST` 指定目录下的文件（默认 `/var/log`）**，
端口检查读 `/proc/net/tcp`，服务查询只允许 `systemctl is-active/status` —— 全部只读。

## LLM 沙箱工作区（个人服务器）

`data/workspace/`（可用 `WORKSPACE_DIR` 覆盖，已 gitignore）是 LLM 的临时/缓存/产物目录。相关技能**仅管理员（SUPERUSERS）可用**：

- `fs_list / fs_read / fs_write / fs_mkdir`：读写工作区，路径锁定（`..` / 绝对路径 / 越界符号链接拒绝）。`fs_read` 单次最多返回 6000 字符（内容要进 prompt）——**要原样发整个文件用 `send_markdown_file` 的 `path` 参数（或 `send_workspace_file`）**：它由服务端直读后投递、不经过模型，因此没有这个截断；二进制与读不到的文件会如实拒绝而不是发个损坏文件
- `fs_delete`：需在聊天中回复「确认删除 XXXX」二次确认（确认码 8 位、10 分钟有效、连续输错作废）
- `run_command`：白名单命令执行（**不经 shell、逐参数校验**、20s 超时、输出流式截断、审计日志带操作者）
  - 允许：`git`(只读子命令 + 安全选项)、`grep/cat/ls/head/tail/wc/pwd`、`find`(仅搜索动作)、`curl`(GET-only https)。压缩/解压不在白名单内：解压用 `extract` 技能、打包用 `pack_zip` 技能（Python 标准库实现，2026-10-18 H1 方案 B——外部 zip/unzip/tar 退役，见下文「压缩与解压」节）
  - 只读运维命令：`ps` / `top`(须 `-b` 批量模式) / `free` / `df` / `du` / `uptime` / `uname` / `nproc` / `whoami` / `id` / `netstat` / `lscpu` 免参数；`ss` 仅展示/过滤选项（**拒 `-K/--kill`** 强断 socket、`-D` dump 文件）；`systemctl` 仅 `is-active/is-enabled/is-failed/status/show/list-units/list-unit-files/list-jobs`（拒 `--host/-M` 跨机）；`journalctl`/`dmesg`/`hostname` 仅只读选项（拒 `--file/--root/--directory/--rotate/--vacuum-*`、`dmesg -C/-c/-w`、位置参数改主机名）
  - **已禁用**：`python3` / `node` / `npm`（任意脚本 ≈ 任意代码）、shell 组合与命令替换、`find -exec/-delete`、`git --ext-diff/-c/--output`、`curl -o/-T/-d/-H` 等一切可写文件/上传/执行外部程序的参数
  - **没有 kill 与 systemctl 动作类子命令**（restart/stop/mask/edit…）：重启本服务请发 `/reboot`（superuser 直执行，含 C→B→A 三段式退路）；杀掉 LLM 沙箱进程的执行权不可回退，刻意不放行；服务/容器控制走动作类 skill（medium+，见「权限级别与管理员动作类能力」），任意命令仅在 high 档的 `run_shell`
  - 含路径分隔符（`/` 与 `\`）的参数 resolve 后必须仍在工作区内；子进程使用最小化环境变量（不继承 API key）
  - **配置注入防护**：仅校验「命令 + 参数」不足以防住「命令读取配置文件」这条路径，额外做了三层封堵——
    (1) `HOME`/`USERPROFILE`/`CURL_HOME` 指向工作区之外的专用沙箱目录，且 `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` 指向空设备、`GIT_CONFIG_NOSYSTEM=1`；
    (2) 所有 `git` 调用前缀注入 `-c` 覆盖（`core.fsmonitor`/`core.pager`/`diff.external` 等），`git diff` 追加 `--no-ext-diff`；
    (3) 若工作区仓库声明了**可执行外部命令的驱动**（`.gitattributes` 的 `filter=`、`filter.*.clean/smudge/process`、`diff.*.command/textconv`、`include.path`），直接拒绝在该仓库执行 `git` 并说明原因（fail-closed）
  - > 安全声明：以上是纵深防御而非硬隔离。git 的配置驱动执行面较宽，第 (3) 层是「拒绝已知形态」而非完备证明。**根治方案是容器/独立低权用户运行**，部署时建议配合 Docker 使用。

## 压缩与解压（extract / pack_zip，个人服务器）

外部 `zip` / `unzip` / `tar` 已于 2026-10-18（REVIEW-6745556..d730827 H1，方案 B）整体退出
`run_command` 白名单：历轮 4 个 H（`zip -T/-TT` 命令注入、tar 扫描超限告警放行、
`--strip-components`/`unzip -j` 展平、通配符 archive 名）全部出生在「Python 侧预扫描 vs
外部工具真实解压行为」的语义鸿沟里。现改由标准库实现的两个技能承担（仅管理员、medium+）：

| 技能 | 行为 |
|---|---|
| `extract` | 解压工作区内的 zip / tar(.gz/.bz2/.xz) 到工作区子目录（默认 `<包名>_extracted`）。**两遍法**：先全量校验条目（穿越 / 绝对路径 / 盘符 / `.git*` 配置段 / 符号链接·硬链接·设备条目 / 条目数与总大小上限），零落盘；全部通过才写盘 |
| `pack_zip` | 打包工作区目录为 zip（`name` 必须是工作区根下的单纯文件名）。不跟随符号链接；包内条目权限位固定 0600 |

**执行位从根上消失**：解压产物一律文件 0600 / 目录 0700（不看包内权限位）——「LLM 内容 →
可执行文件」的转化器不再存在，需要执行请让管理员手工 `chmod`。上限防解压炸弹：
`AGENT_EXTRACT_MAX_ENTRIES`（默认 20000 条）、`AGENT_EXTRACT_MAX_MB`（默认 1024 MB）。
条目名回显过 safety 围栏（压缩包来源不可信）。


## SSH 远程只读诊断（ssh_run，个人内网设备）

不在家里也想让云崽看一眼内网机器（OpenWrt/iStoreOS 小主机、NAS）时，用 `ssh_run`：

```bash
# .env（未配置 = 整个功能关闭，fail-closed）
AGENT_SSH_HOSTS=myserver=root@10.0.0.2:22
AGENT_SSH_KEY_MYSERVER=/home/you/.ssh/id_ed25519   # 私钥路径（推荐）
# 或 AGENT_SSH_PASSWORD_MYSERVER=——  密码经环境变量注入 sshpass，不进 argv/日志
```

- **host 只能来自 `.env`**：LLM 无法指定主机，也没有 SSRF/内网漫游面；未登记的 alias 一律拒绝
- **远程命令写死**：action 仅限 `free / uptime / hostname / uname / ps / ps_mem / top / meminfo / cpuinfo / disks / mounts / dmesg / logread / netstat / ifconfig / services / users`——全部只读。ssh 会把命令交给**远端用户 shell** 执行，因此这里不容许任何字符串插值（自由拼命令 = 把远端 RCE 权交给 LLM）
- **没有 shell 语义**：远端管道（`dmesg | tail`）写死在代码里，LLM 不能自己拼 `|`、`;`、`>`
- 连接参数：`BatchMode=yes`（禁交互）、`ConnectTimeout=5`、`StrictHostKeyChecking=accept-new` + 专用 `data/ssh/known_hosts`（TOFU：首次连接自动登记并在输出里留 Warning，之后严格校验——刻意**不设** `LogLevel=ERROR`，那会把这条首连 Warning 一起吞掉）、`-F /dev/null`（不读 bot 自己的 ssh config）、`IdentitiesOnly=yes`；输出 20s 超时 + 8KB 截断
- 私钥权限过宽（group/world 可读）会按未配置处理并记 WARNING；`AGENT_SSH_KNOWN_HOSTS` 可改 known_hosts 位置（不得落在 fs 可写工作区内——那里可被预埋 host key，配置了会拒绝执行）
- **输出按不可信内容处理**：远端主机的回显（dmesg/logread 等）进 prompt 前过 `safety.py` 围栏——失陷设备构造的提示注入无法直接驱使后续工具调用
- > 如实披露：`accept-new` 的**首次**连接可被同网段攻击者 MITM（换来一次远程只读命令执行权）；要收紧就预置 known_hosts 指纹。**不要把密码贴进聊天**——密码应待在 `.env` 里

## Home Assistant 智能家居（ha_state / ha_control，仅管理员）

让管理员在 QQ 里查家里设备状态、开关灯/插座，不需要暴露 HA 到公网：

```bash
# .env（三项都配才启用；缺任一 = 整个功能关闭，fail-closed）
AGENT_HA_BASE_URL=https://ha.example.com   # 最终地址；不跟随重定向
AGENT_HA_TOKEN=eyJhbGciOi...               # HA「长期访问令牌」
AGENT_HA_ENTITIES=light.living_room,switch.desk_plug,sensor.living_temp
```

- **连接参数只来自 `.env`**：LLM 无法指定地址（没有 SSRF/内网漫游面）；token 不进日志、不入返回值、不走 Web 写面。`AGENT_HA_BASE_URL` 必须是 `http/https` 且不带凭据/查询串，非法配置记 WARNING 并整体禁用；技能不跟随重定向（3xx 直接报错，请把地址配成最终地址）
- **实体登记表制**：`ha_state` 与 `ha_control` 都只认 `AGENT_HA_ENTITIES` 里的 entity_id，未登记一律拒绝（读也只读登记过的）；实体 ID 形态按 `domain.object_id` 校验
- **动作按 domain 枚举**：`light/switch/input_boolean/fan`（turn_on/off/toggle）、`climate`（turn_on/off/set_temperature）、`cover`（open/close/stop）、`lock`（lock/unlock）、`scene`（activate）。数值参数只有两个：`light.turn_on` 的亮度 1~100、`climate.set_temperature` 的 5~35°C；没有任意 service 调用面
- **敏感域两段确认**：`lock` / `cover` / `alarm_control_panel` / `climate` 首次调用只返回确认码（120s 有效，与参数绑定），带同一确认码且参数完全一致地二次调用才执行；一次确认只消费一次
- **结果过围栏**：设备上报的状态、friendly_name 与属性属于设备侧可控文本，技能内用 `safety.fence_untrusted` 包裹后再返回（写入历史的 tool 结果同样带围栏）
- **权限**：`permission="superuser"`（schema 过滤）+ handler `is_superuser` 双层；`ha_control` 另需 `AGENT_PERMISSION_LEVEL` ≥ `medium`（`low` 档不注册，handler 内复查）

## 权限级别与管理员动作类能力（service_ctrl / docker_* / proc_kill / run_build_script）

服务器操作类技能（运维查询、工作区、动作类、ssh、db）**只对 `SUPERUSERS` 开放**：
普通用户的工具 schema 里根本没有这些技能，幻觉调用也只会得到「unknown skill」——
不存在「权限不足」提示去确认能力存在。公共技能（搜索/网页/计算/天气/提醒/点歌等）
不受此约束。级别只调节**管理员**的能力上限：

```env
AGENT_PERMISSION_LEVEL=medium   # low / medium / high，缺省 medium，改后重启生效
```

| 级别 | 管理员可用能力 |
|---|---|
| **low** | 纯只读：ops 查询（proc/disk/port/service_status/log_tail）、`docker_ps/logs/stats`、`fs_list/read`、`run_command` **只读子集**（curl 拒绝）、`ssh_run`、`system_status` |
| **medium**（缺省） | low 全部 + `fs_write/mkdir/delete`（删除保留聊天确认码）+ `run_command` 全白名单 + `service_ctrl`/`docker_ctrl`（restart/start/stop）+ `proc_kill`（两段确认）+ `run_build_script` + `npm_update`（登记表制全局包更新，见下）+ `db_query` |
| **high** | medium 全部 + `run_shell`（`bash -c` 任意命令，见下） |

动作类细节（medium+）：

| 能力 | 行为 |
|---|---|
| `service_ctrl` | `restart/start/stop` 任意 systemd 服务，执行后回报 `is-active` 状态；**自保护**：拒绝 bot 自身的 unit（从 `/proc/self/cgroup` 识别；裸 nohup/容器部署识别不到则无此保护） |
| `docker_ctrl` | `restart/start/stop` 任意容器；**自保护**：拒绝 `PG_CONTAINER`（bot 的数据库容器）与 bot 自身容器（id / ≥12 位前缀 / docker ps 映射的**名字**三种形态都认——REVIEW-de09478..workdir M6） |
| `docker_ps` / `docker_logs` / `docker_stats` | 容器列表 / 容器日志尾部（最多 200 行）/ 容器资源用量快照（CPU%/内存/网络/磁盘 IO，`--no-stream` 一次性快照非实时曲线），low+ 即可用，命令与参数由代码写死 |
| `proc_kill` | **两段确认**杀进程：首次调用只返回确认码与目标 pid 清单，带确认码二次调用才发信号；拒绝 pid 1 与机器人自身；一次最多 10 个进程；确认码 `AGENT_KILL_CONFIRM_TTL` 秒内有效（默认 120） |
| `run_build_script` | 执行工作区 `scripts/<name>.sh` 下**你预先写好的**脚本；参数禁 shell 元字符、路径限工作区内；超时 `AGENT_BUILD_TIMEOUT` 秒（默认 300）；输出过围栏。**内容权边界**（REVIEW-de09478..workdir M1）：同档 `fs_write` 可写 `scripts/`，因此脚本内容对 LLM 并非完全封锁——但 `fs.write` 的 0600 原子写会剥掉执行位，解压产物也一律 0600（extract 技能，2026-10-18 起），LLM 侧不存在「内容→可执行文件」的转化通路；真实防线是 medium 档起步 + superuser 双层闸 + 围栏。需要完整构建环境故继承进程环境变量（与 `run_command` 的最小环境不同） |
| `npm_update` | 更新**登记在册**的 npm 全局包（`AGENT_NPM_GLOBAL_PACKAGES`，逗号分隔；**空 = 技能不注册**）。包名只能精确选、不能拼（含 GitHub shorthand 如 `deepseek-ai/dsh`）；执行 `npm update -g <包>`，`dry_run=true` 只预演。**真实 HOME**（尊重 `~/.npmrc` 的 prefix/registry——与 `run_command` 的沙箱 HOME 相反：沙箱化会让 npm 回落默认 `/usr/local`，装错位置或 EACCES）且**不强制 `--ignore-scripts`**（登记即信任上游账号；全局 CLI 常靠 postinstall 装平台二进制）；90s 超时杀整个进程组、输出过围栏、最小 env（不泄漏 LLM key）。写入全局 prefix **不保留旧版本，不可回退** |

- **为什么从「目标白名单」改成级别制**（2026-10 管理员决策）：目标白名单在单人
  自用部署里维护成本高、收益低；级别制 + 自保护（bot 自身/DB 容器不可被硬停）
  + 管理员专属（普通用户不可见）覆盖了原 fail-closed 的真实意图。原
  `AGENT_SYSTEMCTL_UNITS` / `AGENT_DOCKER_CONTAINERS` / `AGENT_KILL_PATTERNS` /
  `AGENT_BUILD_SCRIPTS` 四键已移除
- **机器人自身的重启仍走 `/reboot`**（三次校验 + 退路 + 完成回执），`service_ctrl`
  对自身 unit 直接拒绝
- 输出/日志均按不可信数据过 `safety.py` 围栏后才进 prompt（服务输出与构建日志是
  间接注入载体）；`run_build_script` 为让构建拿到完整环境**继承**进程环境变量
  （脚本内容可信 + 参数受控，两者叠加后不引入新的 LLM 可控风险面）

## npm 全局包更新（npm_update，登记表制）

```env
AGENT_NPM_GLOBAL_PACKAGES=deepseek-ai/dsh   # 逗号分隔；空 = 整个技能不注册
```

模型可调用 `npm_update(package, dry_run)`：把登记在册的 npm 全局包更新到最新
（`npm update -g <包>`；`dry_run=true` 只预演）。典型场景：更新自己装的 CLI 工具。

- **登记即授权**：包名只能从 `.env` 登记表精确选，LLM 不能拼——与 `ssh_run` 的
  host 白名单同信任模型。GitHub shorthand（`user/repo`）同样支持，但它**不走
  registry**：供应链信任 = 上游 GitHub 账号，且版本不被 lockfile 钉死
- **刻意与 `run_command` 相反的两个取舍**：① **真实 HOME**——`~/.npmrc` 里的
  prefix（如 `~/.npm-global`）、registry、auth token 必须被尊重，沙箱化 HOME 会
  让 npm 回落默认 `/usr/local`（装错位置 / 非 root EACCES）；② **不强制
  `--ignore-scripts`**——登记动作即信任该包及其上游账号，且全局 CLI 常靠
  postinstall 装平台二进制，强制忽略会把它搞坏。对应地，项目级 `.npmrc` 注入面
  不存在（global 安装不读 cwd 的项目配置）
- **不可回退**：写入 npm 全局 prefix 且不保留旧版本；要回退得自己记住版本号重装
- **其余底线**：90s 超时（到点杀整个进程组）、输出 4000 字符截断 + 过围栏、
  最小 env（PATH/HOME/locale，不泄漏 LLM key）、审计日志带 uid 与命令全文
- **分工**：项目内依赖更新走 `run_build_script`（管理员预写脚本）；任意 npm 操作
  仍只在 high 档 `run_shell`

## run_shell（high 级专属）

`AGENT_PERMISSION_LEVEL=high` 时注册 `run_shell`：用 `bash -c` 执行任意命令行
（仅管理员）。这是「沙箱无任意命令」不变量的**唯一显式例外**，底线只剩三道：
120s 超时（到点杀整个进程组）、输出截断（8KB/4000 字符）、审计日志（uid + 命令
全文）。**最小环境执行**——不继承 bot 进程的完整环境，`echo $LLM_API_KEY` 拿不到
密钥（需要完整环境的构建场景走 `run_build_script`）。管道/重定向由 bash 解释，
白名单与路径遏制不适用——误操作不可回退，只建议可信管理员在 high 档使用。
工作区目录（`WORKSPACE_DIR`，缺省 `data/workspace`）不存在时会按需创建——它是
命令的 cwd，全新部署漏建目录不再让每次执行都 spawn 失败。

## 只读数据库查询（db_query）

在 `.env` 的 `DATABASE_URL` 指向的 PostgreSQL 上执行**一条只读** SELECT/WITH 查询并
返回结果表格（仅管理员）。单条 SELECT/WITH + 只读事务（`SET TRANSACTION READ ONLY`
级别的最终闸）、危险函数黑名单纵深、行数与输出长度上限、结果按不可信数据过围栏；
`AGENT_DB_CONNECT_TIMEOUT`/`AGENT_DB_TIMEOUT_MS`/`AGENT_DB_MAX_ROWS` 可调，
未配置 `DATABASE_URL` 时功能关闭（绝不连接默认库）。

## 自定义工具型技能（data/skills/*.py）

除 prompt 型（`*.yaml`，见「工具集」表，`/skills` 管理）外，还支持**代码型**用户
技能：在 `AGENT_SKILLS_DIR`（默认 `data/skills`）放 `*.py`，暴露模块级
`register_skills(registry)` 即生效——用来接自己的 API（典型：Home Assistant
控制、自建站管理后台）。**管理界面就是文件系统**（`skillinstall` 不管 `.py`），
**改文件重启生效**（无热重载，与 `.env` 心智一致）。启动结束日志一行汇总
`local skills: N loaded (…), M skipped (…)`——「技能没生效」的第一诊断入口
（同名冲突另有冲突点 WARNING，直接给出被拒的技能名）。

```python
# data/skills/ha_light.py
import json
import os
import urllib.request

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")  # 凭据只走 .env（已 gitignore）


def register_skills(registry):
    @registry.register(
        "ha_light",
        "开关指定灯。entity_id 形如 light.living_room；on=true 开、false 关。",
        {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "如 light.living_room"},
                "on": {"type": "boolean", "description": "true=开，false=关"},
            },
            "required": ["entity_id", "on"],
        },
        permission="public",
        read_only=False,
    )
    async def ha_light(args):
        if not HA_TOKEN:
            return "未配置 HA_TOKEN（.env），功能关闭。"
        body = json.dumps({"entity_id": args["entity_id"]}).encode()
        req = urllib.request.Request(
            f"{HA_URL}/api/services/light/turn_{'on' if args['on'] else 'off'}",
            data=body,
            headers={
                "Authorization": f"Bearer {HA_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return f"HA 响应 {resp.status}"
```

**凭据纪律（与内置技能同一套规矩）**：API key / 令牌只经 `os.environ.get` 从
`.env` 读——技能文件在 git 里，tool call 参数全量落 `agent.log`，两者都不是
key 该待的地方（未配置时像 `search_web` 那样返回友好文案，别抛栈）。固定 base
URL 不是凭据，写文件无妨；内网设备（HA / 自建后台多在 `192.168.x.x`）**不能**
走 `fetch_url`（内网地址一律拒绝），必须像上面这样在自有 skill 里直连。控制类
技能照 `ssh_run` 先例：`permission="superuser"` + handler 内 `is_superuser`
复查；外部 API 返回值进 prompt 前过 `safety.fence_untrusted` 围栏。

**安全边界**（实现 `agentcore/skills/local.py`）：① 拒绝覆盖已占用技能名——
内置或先加载的自定义技能同名即拒，**冲突点直接 WARNING 明示技能名**（加载器
再报所在文件；防本地 `ssh_run.py` 静默顶掉内置提权），两个自定义文件同名则
字典序先者生效；② 技能目录（解析符号链接后）落在 fs 可写工作区
（`WORKSPACE_DIR`）内 → 拒绝加载全部 `.py`——否则聊天内容可经 `fs_write` 预埋
代码，下次启动即执行；③ 坏文件（import 失败 / 缺 `register_skills` / 注册抛异常）
WARNING 跳过，不阻断启动；④ 默认 `read_only=False`（引擎据此决定一步内多工具
调用能否并行，副作用类绝不能并行），真无副作用者模块内显式
`registry.mark_read_only("名字")`。

> **信任模型**：`data/skills/*.py` 与「改本仓库源码 / 装 NoneBot 插件」同信任级，
> 由部署者完全自担——加载器**不做沙箱、不管依赖**（import 失败即跳过）。
> 想分享给别人的「能力」可以跟踪入库（opt-in 形态与内置技能一致），
> 自家设备表 / 后台 action 表这类**数据**留在本地 `.py` + `.env`。


## 技能管理命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/skills` | 白名单用户 | 模型当前可见的工具；非 superuser 只列自己可用的名字（隐藏技能名不枚举） |
| `/skill catalog` | 白名单用户 | 可安装目录（固定内置预设，当前 4 个 prompt 型） |
| `/skill install <name>` | superuser | 从目录安装 prompt 型技能（tool 型拒绝：工具必须由代码注册） |
| `/skill list` | superuser | **已安装清单**：按介质分两组——prompt 型（`*.yaml`，含名称/描述，即时生效）与代码型（`*.py`，仅文件名，重启后生效） |
| `/skill uninstall <name>` | superuser | 卸载 prompt 型技能（`.py` 请直接删文件 + 重启） |
| `/skill info <name>` | superuser | 已安装 prompt 型技能的详情（含权限串） |

**没有「链接安装」**：聊天里发一个 skill 链接让机器人自己装上——这条链刻意不做。
LLM 的输入含群聊不可信内容，让它「决定装什么」等于让不可信内容驱动持久配置；
远端 yaml 的 `prompt` 字段还会原样进 system prompt（`agentcore/skills/registry.py`
的 `_run_prompt_skill` 不过围栏），远端服务器改一次内容就等于改写机器人行为；
`.py` 链接更直接等于启动即任意代码执行。安装入口保持为确定性命令（可审计、
superuser 门禁），代码型技能的「管理界面就是文件系统」。
