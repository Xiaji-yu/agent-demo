# FIX-4d5c1b8..63f9444

对应 [REVIEW-4d5c1b8..63f9444.md](REVIEW-4d5c1b8..63f9444.md)（**M1–M14 全修 + L 批 17/19 + 历史遗留 5 条**）。

**工作区基线**：修复前 `git status` 干净（HEAD=`63f9444` == `gh/main`）；本文件与新增测试是唯一的评审/修复产物，
其余改动按 §6 属工程变更，照常走测试与 lint。**未 commit / 未 push**。

---

## 修复映射

| 项 | 修法 | 回归 / 变异 |
|---|---|---|
| **M1** 入口脚本无 shebang，非 `init` 运行方式镜像起不来 | `scripts/docker-entrypoint.sh` 首行加 `#!/bin/sh`（脚本仅用 `[ ]`/`for`/`stat -c`，dash 可跑）；新增 `tests/test_docker_deploy.py` 把「镜像/编排形态不变量」纳入秒级静态断言 | 现场对照实测：**修复前** `docker run`（无 `--init`）→ `exec /usr/local/bin/docker-entrypoint.sh: exec format error` exit 255；**修复后** 脚本被 `/bin/sh` 解释、走到 `exec gosu app "$@"`（残留失败仅因最小镜像无 `app` 用户）。变异（删 shebang）→ 3 条红；变异（compose 去 logging）→ 1 条红 |
| **M2** compose `$` 插值静默吞密码字符 | `docker-compose.yml` 注释补 `$$` 转义说明（`p$ss@word` 实测变 `p@word`，无警告） | 文档性修复；`test_no_plaintext_credentials` 顺带锁住凭据不得硬编码 |
| **M3** `container_id()` 嵌套形态回归 + cgroupns=private 自保护整体失效 | ① `/docker/` 形态改回取「`/docker/` 之后第一段」（恢复旧语义），`.scope`/`docker-` 前缀统一剥；② v2 scope 改为扫路径任意一段（补 `/system.slice/docker-<id>.scope/init` 嵌套）；③ 新增 `_hostname_container_id()`：cgroup 无信息且 `/.dockerenv` 存在时采信 Docker 默认主机名（12 位短 id，正则 `[0-9a-f]{12}`），**只在已判定为容器时启用**，宿主机上 12 位 hex 主机名不误判；④ `_self_container_names` 的 id 匹配改双向前缀（docker ps 的 64 位全 id 与 12 位短 id 都要中） | `tests/test_runtime_env.py` 参数化 9 种 cgroup 形态（含嵌套 ×2、`0::/`、非容器）+ 3 条主机名兜底用例；`tests/test_action_skills.py` 补嵌套/短 id 自保护 + 名字通道短 id 映射。变异 3/3 被抓：取末段 → 3 红；去主机名兜底 → 2 红；单向前缀 → 1 红 |
| **M4** `service_ctrl`/`docker_*` 无容器分支 | 四处 handler 统一走新闸门 `_docker_unavailable()`（`in_container()` → 自解释文案；否则 `which` → 原文案），`service_ctrl` 补 `host_only_hint("systemctl")`；新增 `_DOCKER_IN_CONTAINER` 文案（说清「容器里没有、该看哪里」） | 新增 `TestContainerEnvBoundary` 5 条（含 `which→None` 的顺序观测、宿主机路径不变、缺 CLI 不起子进程）。变异 2/2 被抓 |
| **M5** compose 两服务无 logging 上限 | `db`/`bot` 均加 `json-file` + `max-size: 10m` + `max-file: 5` | `docker compose config` 实测两个服务都生效；`test_both_services_have_log_caps` 锁死 |
| **M6** entrypoint「属主本就匹配」注释失实 + 非 root 无可写性自检 | 注释改为「只在宿主目录已存在且属主正确时成立，否则先 mkdir -p 并 chown」；非 root 分支加 `[ -w ]` 自检，不可写即向 stderr 打 ERROR（把静默降级变响） | `test_non_root_branch_checks_writability` 锁 `-w` 与 ERROR；现场实测 `--user 1001:1001` + 缺失目录 → 宿主机 root:root + `touch: Permission denied`（复现 REVIEW 的 M6） |
| **M7** README 与代码矛盾（「C/B 两步照常可用」） | 改为「C 步（外部命令）在容器内跳过，直接走 B 步自替换；A 步由容器 restart 等价替代」，并补「容器内改 `.env` 后必须 `docker compose restart`」（execve 只带出生环境快照，同 systemd EnvironmentFile 限制） | 与 `docs/operations.md:128`、`reboot.py:141-144`/`:189-191` 三方一致 |
| **M8** reboot docstring 引用不存在的函数，容器内 `AGENT_REBOOT_CMD` 静默失效 | 实现 `_warn_external_cmd_in_container()` 并在模块装载时调用：`in_container() and AGENT_REBOOT_CMD 非空` → 一条 WARNING（含解析结果），docstring 的 `:func:` 引用随之成立 | `tests/test_reboot.py` 3 条（配了必警、没配不警、宿主机不警）。变异（告警恒不触发）→ 1 红 |
| **M9** 兜底剥离日志绕过 `AGENT_LOG_CONTENT` | `engine.py` 该行改走 `log_text(original, 40)`——engine 内其余内容日志（`:780/:1367/:1448/:1594/:1701`）本来就都走它，这是唯一漏网的 | `test_strip_log_respects_log_content_switch`：`AGENT_LOG_CONTENT=0` 下断言正文不落盘、只留 `<len=`。变异（改回裸 `%r`）→ 1 红 |
| **M10** 时间前缀不计入 token 预算 | 新增 `_HISTORY_PREFIX_TOKENS`（跨日最长形态一次性算清），`_message_tokens` 对「role∈{user,assistant} 且 content 非空且带 ts」的消息加这笔津贴；判据刻意比 `_project_history` 宽（不校验 ts 可否解析）——多算不会误裁，少算才破坏不变量 | `test_budget_accounts_for_time_prefix`：200 条短消息、budget=3000，断言投影后实际 ≤ 预算。变异（津贴=0）→ 1 红。**连带修正** `test_no_resummarize_when_watermark_current`：原 budget=80 的前提（窗口罩住未摘要尾巴）在津贴下不再成立，按可行带中点重设为 220 + 120 字消息 + `extract_facts=False`（可行带宽度 35→95） |
| **M11** tz 回落告警放大 | `agentcore/tz.py` 加模块级 `_warned_bad_tz` 去重集合，docstring 承诺的「一次性告警」至此才真成立 | `TestBadTzWarnsOnce` 2 条（同名只警 1 次；20 条历史投影后 ≤1 条）。变异（去 once-guard）→ 1 红 |
| **M12** `test_same_day_uses_clock_only` 午夜 flake | 从「本地当天 12:00」推导 `ts`（`dt.datetime.now(tz.zoneinfo()).replace(hour=12) - 1h`），不再用 `time.time()-3600` | 仿真 24 小时 × 3 个分钟位全部同日；对照旧写法在 00:00–00:59 必跨日 |
| **M13** 票据 IP fail-closed 修复无回归钉子 | 注释改正（双侧同空仍相等，那属「双方都无 IP 信息」而非绕过）；补 `test_download_ticket_ip_binding_has_negative_cases` 逐形态钉死（空→非空拒、非空→空拒、双同空放行、双同非空放行、一次性） | 变异（改回旧条件 `and source_ip and`）→ 1 红。此前该分支**零测试**，改回旧实现既有负例仍绿 |
| **M14** CHANGELOG 漏记两项用户可见变更 | 部署节补 SUPERUSERS 空值修复；对话与引擎节补 `/aihelp`→`/help` 改名 + 帮助菜单单一数据源与命令面补全、群统计免前缀触发已撤除、历史 token 预算计入前缀 | `grep` 核对四条均在 |

---

## L 批修复映射（17/19；L4 误报、L14 无法修复，见文末）

| 项 | 修法 | 回归 / 变异 |
|---|---|---|
| **L1** `.env.example` 帮助命令注释仍是旧名 | 改为 `/help 指令，「帮助」/ /aihelp 同效` | —（注释） |
| **L2** `test_service_status_on_host_unchanged` 近似恒真 | 改为**观测 argv**：桩 `which→路径` + 记录 `run_readonly`，断言真起了 `systemctl is-active`（原用例走 `_UNIT_RE` 校验路径，摘掉容器分支它照样绿，实测过） | 变异（摘容器分支）→ `test_service_status_in_container` 红 |
| **L3** 免前缀守卫只认三个字面名 | 改 AST 扫描：`stats_route` 内不得出现 `on_message(` 注册调用；另加一条断言模块内全部 matcher 注册清一色 `on_command` | 变异（换名 `stats_msg_cmd` 挂 `on_message`）→ 2 条红 |
| **L4** `covered()` 族循环内 `return` | **穷举证伪为误报**：`squashed` 是 substring 包含、同 primary 的多前缀必然嵌套，3840 组组合两种写法零分歧。提取为模块级 `_covered` 并把证明写进 docstring，**不设判别用例**（造不出分歧输入） | 穷举脚本实测 0 分歧 |
| **L5** 非有限/超范围 ts 炸整轮 | `_history_time_prefix` 的整段换算包进 `try (OverflowError, OSError, ValueError)`，失败返空串。`isfinite` 首版加过又移除——try 已完全覆盖它，留两处是冗余防线 | 变异（去 try）→ `test_non_finite_ts_does_not_break_turn` 红 |
| **L6** 剥离正则漏半角括号/全角冒号 | 抽出 `_TIME = r"\d{1,2}[:：]\d{2}"`，两种括号 × 两种冒号 + `MM-DD`/`YYYY-MM-DD` 形态全列；混合形态（全角数字+半角冒号）自然覆盖 | 9 应剥 + 6 不应剥矩阵；不误伤 `（14:30-15:00）`/`（x）`/`（11:2）`/`（11:20:33）`/文中时间戳 |
| **L7** 跨年前缀缺年份 | 同年跨日 `MM-DD HH:MM`、**跨年 `YYYY-MM-DD HH:MM`**（与时间线警示段口径一致）；AGENTS.md §4「历史预算」行与 docs 同步 | 变异（跨年改回 `%m-%d`）→ `test_cross_year_prefix_carries_year` 红 |
| **L8** `generate_proactive` 不过剥离 | 返回前过 `_strip_imitated_time_prefix`（剥空则原样返回） | `TestProactiveGeneration::test_strips_imitated_time_prefix`；变异（还原 `return content`）→ 红 |
| **L9** 用户可伪造同形前缀造成双前缀 | `docs/features-memory-and-kb.md` 补说明：系统那条恒在 content 开头、伪造的只能在它之后，且只能描述用户自己那条消息的时间，不涉提权 | —（文档，设计层无可改动点） |
| **L10** Dockerfile/entrypoint/compose 零守卫测试 | **已由 M1 的 `tests/test_docker_deploy.py` 关闭**（13→16 条静态断言） | 变异（删 shebang）→ 3 红 |
| **L11** entrypoint 只 chown 顶层不递归 | 提取 `check_writable()`，**root 分支降权到 app 后再自检一次**（root 的 `[ -w ]` 恒真，顶层 chown 覆盖不到异属主文件），不可写打 ERROR | `dash -n` 通过；实测 555 目录打 ERROR、可写时无噪音；4 条静态守卫 |
| **L12** HEALTHCHECK 只探端口 | Dockerfile 注释写清已知局限（探 listen 不探业务可用；`restart` 不按健康态重启故不自愈；要自愈需外加 watchdog），并指向 operations.md | —（注释 + 文档，知情取舍） |
| **L13** 容器化文档三缺口 | `docs/operations.md` 新增「容器部署的三条运维注意」：① 改 `.env` 后必须 `compose restart`（execve 只带出生环境快照）；② `AGENT_SSH_KEY_*`/`known_hosts` 容器内默认不可用；③ `stop_grace_period` 与 flush 超时耦合 | —（文档） |
| **L14** `794a3f1` 主题行「全部做过变异复核」 | **无法修复**：commit 已推送，不改写历史。body 已如实记 4 处变异，仅主题行压缩过度。按 §3 显式入档为有意保留 | — |
| **L15** `FIX-e55cab5..4d5c1b8.md` L 编号跳号 | 补录 L4/L5 两行（原表从 L3 跳到 L6；L5 原只写在 M-2 行内）。L4 核实为「未记录」而非「未实现」——`x-forwarded-proto` 分支早在 `77e21ca` 就有 | —（review 产物勘误） |
| **L16** `today_anchor` 分钟跳变竞态 | 把 `tz.now` 桩成固定 `datetime`，两边同源、彻底消除两次调用跨分钟的窗口 | `test_today_anchor_has_date_weekday_and_clock` 改确定性断言 |
| **L17** features-messaging 父注与实现相反 | 改为「群聊中带 `/` 前缀即可触发，**无需**先 @机器人 或带唤醒词（`on_command` 只做命令名匹配）；ACL 仍校验群白名单与黑名单」 | —（文档） |
| **L18** `test_bot_startup` docstring 漂移 | 「strip 后为空 → pop」改为「→ 规范成 `"[]"`」 | —（注释） |
| **L19** `assert "现在" in prompt` 弱断言 | 改 `re.search(r"现在 \d{1,2}:\d{2}", prompt)`（「现在」在工作流第 4 条与「不是现在」里也出现，写宽了测不出锚点丢时刻） | 变异（`today_anchor` 丢时刻）→ 2 红 |

### 历史遗留 L 项（5 条）

| 来源 | 修法 | 回归 / 变异 |
|---|---|---|
| `2ed7c63-L10` 未闭合 `<script>` 静默吞掉其后全部正文 | `_HtmlTextExtractor` 跳过期间分批缓冲，`close()` 时仍未闭合就把缓冲当正文吐出；正常闭合的脚本内容仍在 endtag 处丢弃 | 3 条回归（未闭合 script/style 恢复、闭合仍剥、嵌套不误恢复）；变异（去 EOF 恢复）→ 红 |
| `2ed7c63-L18` 坏人格文件告警每 2 秒刷一次 | `PersonaManager._warn_once` 按 (原因, 文件) 去重；`refresh()` **先清去重集再扫描**（显式重读应重新报告）。同条「list 回退显示失效名」部分已不成立——`list()` 只返回解析成功的人格 | 4 条回归（重复扫描只 1 条、refresh 后再报、新故障仍报、PERSONAS_DEFAULT 错名只 1 条） |
| `e55cab5-L12` `_EN_STOPWORDS` 注释未点名豁免通道 | 注释点名 `AGENT_GROUP_STATS_STOPWORDS`，并按原处置「词表不预置」维持不改词表 | —（注释） |
| `e55cab5-L3` 写入确认框的 live 承诺是 schema 静态值 | `docs/features-interaction.md` 注明第一段响应据此提示、**不探测运行态**，真实时机以第二段响应与审计为准 | —（文档） |
| `ee39c8e-L7` 布尔 env 脏值静默按开启 | 新增 `agentcore/boolenv.py` 单一实现（`env_flag`/`env_bool`/`parse_bool`），迁移 8 处内联 `raw not in {...}`；**语义与旧写法逐值等价**（契约测试锁死），只多「认不出的值告警一次后取默认」 | `tests/test_boolenv.py` 28 条；变异 2/2（告警恒不触发 → 4 红；脏值 fail-open → 1 红） |


---

## 文档同步（§2.6）

- `AGENTS.md` §4「历史预算」行：补「前缀计入预算」与 `_HISTORY_PREFIX_TOKENS`。
- `AGENTS.md` §4「容器化」行：补 action_skills 的容器分支（`run_action` 不经 `runner.permitted()`）、容器内配 `AGENT_REBOOT_CMD` 启动即告警、非 root 自检可写性。
- `AGENTS.md` §4「配置」行：补布尔 env 统一走 `agentcore/boolenv.py` 及脏值告警口径。
- `AGENTS.md` §5「cgroup 解析漏剥 `.scope`」坑表行：扩写为同族两个形态（嵌套取末段 / cgroupns=private）+ 主机名兜底通道。
- `CHANGELOG.md`：四条（见 M14）。
- `README.md`：容器与宿主机差异段改正（M7）。
- `docs/operations.md`：新增「容器部署的三条运维注意」三条（L13）。
- `docs/features-memory-and-kb.md`：跨年前缀格式、前缀计入预算、双前缀说明（L7/L9）。
- `docs/features-messaging.md`：群聊管理指令父注改正（L17）。
- `docs/features-interaction.md`：live 静态承诺的口径注明（e55cab5-L3）。
- `.env.example`：帮助命令注释改名（L1）。
- `BACKLOG.md`：DOCS-STATS 计数块随测试数重生成（89 文件 / 3427 收集：3354 通过 + 73 跳过，`--write-stats` 实测）。
- `review/REVIEW-WORKFLOW.md` §9：补本轮 REVIEW 索引行；§9.1 补本轮 FIX 索引行。

---

## 验收

- **默认套件**：`3354 passed / 73 skipped`（M 阶段 3311，L 阶段 +43）。
- **PG 全新空库**（`qqagent_test`）：`3440 passed / 9 skipped`。
- **ruff 0.17.1**：`check` + `format --check` 双绿（233 文件）。
- **pyright agentcore**：0 errors。
- **RUN_PERF**：9 passed（L 阶段补跑）。
- **变异复核**（M+L 合计 20 处「改坏必红」全部实测被抓）：M1（shebang/logging）、M3×3、M4×2、M8、M9、M10、M11、M13、L5、L7、L8、L10、L18（personas 去重）、L19、ee39c8e-L7×2。L2/L3/L4/L6/L12/L15/L16 为文档/配置/测试自身修正，变异对象即原缺陷本身，已在对应行注明实测证据。
- **现场实测**：`docker run` 无 `--init` 从 `exec format error`(exit 255) → 脚本被 `/bin/sh` 正常解释执行；entrypoint 非 root 分支对 555 目录打 ERROR；9 种 cgroup 形态 + cgroupns=private 主机名兜底自保护命中；compose 两服务 logging 上限生效。
- **未验证面**：CI 未推送无法核实（与上轮同样原因）；`perf_baseline.py` 比对未跑（RUN_PERF 9 条本身全绿）。

---

## 遗留 / 未修

- **L 批 19 条：17 条已修**（L1/L2/L3/L5/L6/L7/L8/L9/L10/L11/L12/L13/L15/L16/L17/L18/L19）。
  - **L4 分析后为误报**：`covered()` 在紧凑族循环内的 `return <布尔>` 看着像「首个命中的族判 False 就短路」，但 `squashed` 判定是 substring 包含、而同一 primary 的多个前缀必然互相嵌套——**穷举 3840 组前缀/子命令/文本组合，两种写法结论零分歧**。故不设判别用例（造不出分歧输入），只在 `tests/test_help_menu.py::_covered` 的 docstring 里写下这个证明，防止后人误改。
  - **L14 无法修复**：`794a3f1` 的 commit message 已推送，改写历史会影响他人 checkout；其 body 已如实记录 4 处变异，仅主题行的「全部」压缩过度。按 §3「不得连续两轮遗留」显式入档为**有意保留**，理由即此。
- **历史遗留 L 项：4 条已修**
  - `2ed7c63-L10`（未闭合 `<script>` 静默吞掉其后全部正文）：跳过期间改分批缓冲，EOF 仍未闭合就把缓冲当正文吐出；正常闭合的脚本内容仍在 endtag 处丢弃。3 条回归 + 变异 1/1。
  - `2ed7c63-L18`（坏人格文件告警每 2 秒刷一次）：`_scan` 按 TTL 重跑导致同一条 WARNING 无限重复。新增 `_warn_once` 按故障 key 去重，`refresh()` 先清去重集再扫描（显式重读应重新报告）。4 条回归。同条的「list 回退显示失效名」部分**已不成立**——`list()` 只返回解析成功的人格。
  - `e55cab5-L12`（`_EN_STOPWORDS` 注释未点名豁免通道）：注释点名 `AGENT_GROUP_STATS_STOPWORDS`，并按原处置「词表不预置」维持不改词表。
  - `e55cab5-L3`（写入确认框的 live 承诺是 schema 静态值）：`docs/features-interaction.md` 注明第一段响应据此提示、不探测运行态，真实时机以第二段响应与审计为准。
  - `ee39c8e-L7`（布尔 env 脏值静默按开启）：新增 `agentcore/boolenv.py` 单一实现，迁移 8 处内联 `raw not in {...}`；**语义与旧写法逐值等价**（有契约测试），只多「认不出的值告警一次后取默认」。`tests/test_boolenv.py` 28 条，变异 2/2。
  - `e55cab5-L4`（票据 `Secure` 依赖反代传 `X-Forwarded-Proto`）：补录进 `FIX-e55cab5..4d5c1b8.md` 的 L4/L5 两行（原表跳号）；并写入 `docs/operations.md` 的三条运维注意。
- **仍未动的历史 L**：`ee39c8e-L0`（补录 `REVIEW-9c2d3c1..ee39c8e.md`）、`ee39c8e-L10`（age 子进程 `CancelledError` 清理，仅 POSIX 可复现）、`1b16fa7` 的 L8/L12/L13/L17、`2ed7c63` 的 L28——均为需专项或需真机验证的项，本轮不动。
- **M3 的 `_self_container_names` 短 id 通道**只在「docker ps 的 ID 列与 cid 互为前缀」时生效；若某天 docker 改动 `--format {{.ID}}` 的输出长度语义，需复核该处。
- **M6/L11 的非 root 可写性自检**只打 ERROR 不退出（`set -e` 下 `[ ! -w ]` 为假不终止）——刻意如此：不可写是降级而非致命，退出会让容器起不来。
