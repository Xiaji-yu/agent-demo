# agent-demo 近期 Commit 评审报告

**评审范围**：63f9444..858afcc（39 个 commit；其中 2cbe32c/874584b/5751f73 为上轮修复验收已覆盖的提交，本轮只核销项不重审）
**评审日期**：2026-11-02
**评审方式**：5 个并行子代理分线审查（线1 安全/权限/访客面、线2 cron/调度/persona、线3 web 登录门/2FA/modal、线4 容器化/runtime/reboot、线5 功能面/渲染/搜索/文档）+ 主代理逐条实证（默认/PG 双套件、ruff/pyright/RUN_PERF 实跑、H 与关键 M 亲自复现、CI API 核对）
**工作区状态**：评审开始时 git status 干净（分支 release/1.0.4 与 gh/release/1.0.4 同步；gh/main 顶端即 858afcc）

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 最高风险 | **H1**：`backup_usb.sh` 的 cron jobs.json 只进必含清单、没有复制语句——配置过 cron 任务的部署每周 U 盘冷备必失败（CWD=SRC）或静默缺件（CWD≠SRC），**主代理沙箱双形态复现** |
| 本轮判定 | H×1、M×12、L×29（含批量关闭计划）；被证伪 20+ 项 |
| 声称核对 | 大体诚实；3 失真立案：AC-group-guest-mode「jobs.json 纳入 USB 冷备必含（已实施）」、FIX-openclaw/QQ 人格通道「改前备份」（M7）、`_fmt_wall` docstring「与引擎口径一致」（M11）；「无人值守任务看不到运维技能」仅群目标成立（M2 口径收窄） |
| 测试 | 默认 **3461 passed / 73 skipped**（89.57s）；PG 全新空库契约套件见 §3；ruff 0.17.1 双绿；pyright 0 errors；RUN_PERF 9 |
| CI | main 与 release/1.0.4 的 858afcc 均 success；`d129404` 曾因 `--locked` 红、`94d01cd` 修复闭环；dependabot 分支 91770ee 被测试门禁拦下（区间外） |
| 门禁 | **H1 阻断发布**：1.0.4 发布物含 backup_usb.sh，修复+回归前不打 tag/不发布；M 批建议同批修复后发布 |

## 1. 已实证的问题

### H1 backup_usb.sh:82 jobs.json 只进必含清单、没有复制语句——cron 用户每周冷备必败或静默缺件【已复现】

`scripts/backup_usb.sh:82`（1b40383 引入）：

```bash
# cron 任务配置：存在则必须带走（投递目标只在里面）；state sidecar 不必
[ -f data/cron/jobs.json ] && REQUIRED_FILES+=(data/cron/jobs.json)
```

全脚本 rsync 清单（72/73/84/95/98/101 行）没有任何一行复制 `data/cron/`；而 M15 的必含断言（106-111 行）对 `REQUIRED_FILES` 逐项硬校验 `$TMP/$req`，缺失即 `exit 1`。主代理沙箱复现（/tmp 复刻 + 仅替换 mountpoint 判定）：

- 形态 A（CWD=`$SRC`，即 systemd unit 配 `WorkingDirectory` 的标准部署）：`ERROR: 冷备必含文件缺失：data/cron/jobs.json（放弃本次，保留上一份不动）`，**exit 1——凡配置过 cron 任务，每周冷备永久失败**，旧备份滚动耗尽后无新备份；
- 形态 B（CWD≠`$SRC`，如手动从别处运行）：相对路径探测不到 → 不进清单 → 备份「成功」（exit 0）但 **jobs.json 静默缺失**（实测 `find jobs.json` 计数 0），cron 投递目标全部丢失，与行内注释「存在则必须带走」相反。

**影响**：备份可用性/完整性双形态破坏；`review/AC-group-guest-mode.md:5` 声称「jobs.json 纳入 USB 冷备必含」已实施——失真立案。测试缺口：`tests/test_backup.py` 对 backup_usb.sh 只覆盖 M10/M15/L12（958 行起），cron 块零用例，H1 因此漏网。
**补充核实（归档后问询）**：出货的 `agent-demo-usb-backup.service` 配 `WorkingDirectory=$SRC`——**形态 A 就是生产形态**，非假设；且冷备失败无任何主动跟踪面：systemd `OnFailure=` 只拉起 `agent-demo-backup-failure.service` 写一条 error 级 journal 标记（该 unit 注释自认「无人主动看等同静默」），web「备份」页只跟踪本地备份与异地镜像、不含 U 盘冷备状态。无人看 journal 即等同冷备静默停摆——「保留上一份不动」保证最后一份好备份冻结不丢、轮转不执行，但新鲜度归零且无人感知。
**修复**：判定与复制都用 `$SRC` 前缀并补复制动作（`mkdir -p "$TMP/data/cron"` + rsync `$SRC/data/cron/jobs.json` → `$TMP/data/cron/`）；同步修正 AC 文档；补「配置 cron 后冷备必含 jobs.json」的行为级回归（临时目录夹具跑脚本断言产物含该文件、无 jobs.json 时备份仍成功）。

### M1 reminder 空 `user_id` 静默落入 store「管理员语义」——权限判定层与数据层语义错位【已复现】

`agentcore/skills/reminder_skills.py:139-152`（106-111 同型）：`uid = user_id or None`、`admin = _is_admin(uid)`（None → False），随后 `store.schedule_cancel(str(schedule_id), None if admin else uid)`——非管理员分支传出的 `uid` 恰是 `None`，而 `None` 是 store 契约的管理员哨兵（`store.py`：「除非 user_id 为 None 表示管理员操作」）。主代理内存仿真：`schedule_list(None)` 返回全量（含他人）、`schedule_cancel(id, None)` 跨用户取消成功。当前生产链路 uid 恒非空，但 `registry.execute` 是公开 API，任何新调用方漏传即静默获得全量+跨用户取消。
**修复**：两个技能入口加 `if not uid: return "错误：拿不到会话信息"`（与 reminder_add 同款），补空串负例。

### M2 cron 私聊目标轮次 where 轴不收窄——无人值守轮次可见全部 superuser 技能【推演·代码链完整】

`agentcore/scheduler/agent_turn.py:154-158` 私聊目标时 `group_id=""`；`registry.py:203` `if not group_id: return True`——`_sessions_allowed` 视作私聊全放行。若 jobs.json 私聊目标配的是超管 QQ（最自然形态），无人值守轮次的工具 schema 含 run_shell/ssh_run/db_query 等全部 superuser 技能，且轮内无人在环（真人私聊尚有 ACL 的人在环语义）。`_sessions_allowed` docstring 与 AGENTS.md「cron 的 agent_turn 同样带目标群→看不到运维技能」仅对**群目标**成立，私聊侧偏差未声明。
**修复**：cron 上下文 `user_id` 改 `cron:<job_id>` 命名空间（目标身份只在 Sink 投递用），或对私聊目标强制 `ident ∈ SUPERUSERS` 并向 registry 传显式无人值守收窄标志；文档同步口径。

### M3 cron 私聊目标的成长计数记到真实 QQ 名下——persona_growth 污染真实用户【推演·代码链完整】

`agent_turn.py:10-12` docstring 声称 growth 以伪 user 记录、「真实用户会话永不以该 id 对话，条目成为不可召回的孤儿」——群目标成立，**私聊目标不成立**：`ident` 就是真实 QQ，engine `growth.maybe_trigger(user_id, session_id)`（engine.py:1689-1691）达阈值后把 cron 会话内容经 `_propose` 写进 `user_state(user_id)`，本人正常私聊即召回。无人值守产物进真实用户成长层，且消耗一次回顾 LLM 成本。
**修复**：cron 场景跳过 growth（`context["skip_growth"]=True`），或 growth user 改 `cron:<job_id>` 命名空间。

### M4 /openclaw import 对中文人格名抛未捕获 ValueError——命令崩溃、无回复、无审计【已复现】

`cron_admin.py:273-274` `pm.save(name, …)` 的 name 取自 SOUL.md frontmatter；`PersonaManager._NAME_RE = ^[A-Za-z0-9_-]+$`（manager.py:36,55）对 `夏祭的助手` 抛 ValueError；`import_openclaw` 与 `cron_cmd.py:66` 均无 try/except。主代理复现：`RAISED: ValueError invalid persona name: '夏祭的助手'`。中文 name 是 OpenClaw 迁移最常见形态——导入对主场景不可用且无诊断；`_record("openclaw_imported")` 在 import 成功后才调，崩溃时审计也缺失。
**修复**：捕获 ValueError 转 slug 或逐条报告跳过；`_record` 移入 finally 或失败分支也记。

### M5 纯中文 name 无显式 id 的 job 一律解析失败——`_slug` 与 `_ID_RE` 互相矛盾【已复现】

`jobs.py:135-140` `_slug` 刻意保留 unicode（docstring 自举「每3小时用电用水报告」为例），`:64` `_ID_RE = ^[A-Za-z0-9_-]{1,64}$` 只认 ASCII。主代理复现：`_validate_job({"name": "每3小时用电用水报告", …})` → `REJECTED: id 非法（64 位以内 A-Za-z0-9_-）：'每3小时用电用水报告'`。中文用户手写 jobs.json 不写 id 即全被跳过，与自家 docstring 直接矛盾。
**修复**：`_slug` 产 ASCII 口径（或 slug 后过 `_ID_RE` 校验回退 `"job"`），docstring 同步。

### M6 /persona new|edit 把多行正文压成单行——markdown 人格结构损坏后落盘【已复现】

`persona_utils.py:19` `s.split()` 吃掉换行、`:35` `" ".join(tokens[1:])` 换行→单空格；`admin.py:1217-1225` 再 `rest.split(None, 1)`。主代理复现：`/persona new mp 你是运维助手：\n- 简洁\n- 危险操作先确认` → body=`'mp 你是运维助手： - 简洁 - 危险操作先确认'`。人格正文即 system prompt 内容（docstring 自述），多行人格经 QQ 通道必然格式破坏，两段确认钉住的正是损坏文本；web 面无此问题，双通道不一致。
**修复**：new/edit/confirm 分支不走 token 化，用正则在原始文本上切分保留原文。

### M7 /persona edit 无改前备份，成功回执却谎称有备份——声称与实现不符【已复现·静态】

`admin.py:1264-1278`：confirm 分支直接 `manager.save(...)`（os.replace 覆盖，旧文件不可恢复），审计只有 `body_len`；成功文案却写「（改前备份见 data/personas 同名旧文件被覆盖前的审计记录）」——该备份不存在。FIX-openclaw-20261101 与 AC-group-guest-mode 声称 QQ 通道「审计+备份」。web 面有 `_persona_bak_path`（web.py:517-527）而 QQ 通道没有，同厂双标。
**修复**：confirm 落盘前对已存在文件复制到 `data/personas` 备份名（0600），或复用 web 备份函数；文案与文档同步。

### M8 /openclaw import 原样保留 `enabled: true`——导入后 60s 内无人审核立即执行【已复现·代码链】

`cron_admin.py:292-298` 对 incoming 逐条 append（enabled 原样、无 state 时 next=0）；对照 `_apply_add` 强制 `new["enabled"] = False  # 新任务先停用：/cron run 试跑后再 on`；`agent_turn.py:216` `if job.next_run_at_ms and …` 使 next=0 立即执行。导入即向目标群/私聊投递 LLM 产物，与自家「先停用试跑再启用」安全姿态不一致。
**修复**：导入统一 `j.enabled = False`，报告注明「均已停用，请 /cron run 试跑后逐条 on」。

### M9 /cron run 试跑无锁：可与 tick 并发双跑同一 job，且试跑推进 next_run【推演】

`cron_admin.py:240-243` `create_task(runner.run_job(hit))` 无锁；`run_job` 内部先 `update_state(status="running", next_run_ms=下个周期)`（agent_turn.py:162-167）——试跑与 tick 的 ≤60s 竞争窗口内双跑即同日两次投递；对已启用任务 run 试跑还把 next_run 从「下个整点」改写成「试跑时刻+周期」。仓库在 nudge 有现成解法（nudge.py:802-842、1979「与 tick 共用同一把锁」）未复用。
**修复**：Runner 加 `asyncio.Lock` 全程持锁（含 run 路径），或试跑走 dry-run 跳过 update_state。

### M10 web_login 审计缺 `source_ip`，且登录审计路径零测试覆盖【已复现·grep】

`web.py:4135/4151/4163` `_record("web_login", ok=False, reason=...)` 均不带 IP（`diagnostics.record(kind, **fields)` 支持任意字段；同文件 `config_write` 审计按既定决策带 `source_ip`，web.py:4314/4535）。按源 IP 锁定（`_login_fails[ip]`）后取证断链：管理员只见「有爆破」无法定位来源。`grep -rn web_login tests/` 零命中——「失败/锁定记审计、成功不记」这条不变量无回归保护，删掉 `_record` 全量照绿。
**修复**：三处补 `source_ip=ip`；TestLoginSession 补审计正反断言。

### M11 群上下文行时间与引擎历史前缀跨年口径不一致——docstring 声称一致已失真【已复现·代码对照】

`group_context.py:46-58` `_fmt_wall` 只有两档（同日 `HH:MM`/跨日 `MM-DD HH:MM`）；engine 同函数已改三档且注释写明跨年必须带年份（engine.py:382-390，L7：跨年 `12-31 23:59` 会被当成今年）。`_fmt_wall` docstring 仍声称「与引擎历史前缀的跨日约定一致」。跨年时段（或调大 `AGENT_GROUP_CONTEXT_TTL`）注入的群上下文片段会把去年 12-31 呈现为无年份的 `MM-DD`，模型读成未来。
**修复**：`_fmt_wall` 补跨年档（`%Y-%m-%d %H:%M`），docstring 与用例同步。

### M12 屏显臻宋 3.35MB 随公开仓库/GHCR 镜像分发但无许可与署名文件【文件事实核实】

`data/fonts/` 下 LICENSE 仅覆盖 wqy（GPL-2.0.txt、LICENSE-WQY-ZenHei.txt）；`review/FIX-default-font-20261031.md` §4 自认屏显臻宋无独立授权文件。本轮 `docker-publish.yml` 使镜像分发面从 git clone 扩大到 GHCR pull，第三方再分发链无任何凭据——上轮 M10（GPL 字体零署名）同型。
**修复**：补 LICENSE/README（来源 URI、授权记录、分发条款），或在 docs 给出书面凭据存放位置。

### L（29 条，压缩列出；批量关闭计划见 §6）

- **线1**：L1 drift 守卫只识别 keyword 字面量 `permission`，位置参数/常量形态逃检（当前仓库 0 处盲区，实测）；L2 超管跨用户取消提醒无审计留痕；L3 boolenv 存量三处旧写法未迁移（web.py:1031、rag/service.py:43、rag/distill.py:70——`disabled` 仍静默按开启）；L4 drift 文档侧 strict 提取跳过混合权限行 + AC D6 声明口径大于实现；L5 TestSecretPathGuards 清单式依赖人工同步。
- **线2**：L1 两处恒真/短路断言（`test_cron_cmd.py:87` `or True`、`test_web.py:3546` 后件已被前一行保证）——**必修**；L2 `deleteAfterRun` 死字段全链支持但执行体不消费（若文档声称兼容则升 M）；L3 cron 私聊投递目标零准入（测试锁定=有意设计，建议收紧至 SUPERUSERS∪历史会话）；L4 预算闸 reason 文案被 cron 当产出投递到群（nudge 同场景是静默跳过）；L5 `cron:<job>` 会话历史无界累积；L6 `_next_run_ms` 失败返 0 与「立即执行」语义耦合（当前缓解充分）；L7 `run_cron_action` jobs_path 传递链断裂——测试若补 add→confirm 全链会写真实 `data/cron/jobs.json`（1b40383 的 jobs.py 事故近亲）；L8 杂项：群聊 `/cron` 拒绝文案与 `/persona` 口径不一、tick 串行致触发时刻漂移、手改 expr 首跑按旧 sidecar、`create_task` 未存引用 + `_pending` 不清扫、无人值守文档口径（同 M2）。
- **线3**：L1 modal DOM shim 不触发 `onclick=` 属性形态（变异实测 18/18 仍绿）——顺手回归逃检；L2 invalid_token 分支无限速且逐笔进 50 槽 recent 面板（可刷屏挤掉真实事件）；L3 `_login_fails` 失败 IP 键无上界无过期清理（对照 `_used_confirms` 有 600s 清理）；L4 杂项：`_login` 内重复 import×4、`AGENT_WEB_SESSION_TTL_MIN` 无上限、多 worker 假设已留痕。
- **线4**：L1 entrypoint root 分支内联 `check_writable` 丢 FILES 复检、两份实现漂移（该脚本注释自己反对两份实现）；L2 `test_root_branch_rechecks_after_dropping_privileges` 弱断言——删内联调用仍绿，**必修**；L3 docker-arm64.yml 缺 `permissions:` 块；L4 `.env.example`「compose 层变量不注入容器」前提错误（`env_file` 全量注入）+ `AGENT_CRON_DIR` 归类错误；L5 `deploy.py _docker_data_root` 不复刻 compose「shell env > .env 文件」插值优先级；L6 禁写死架构守卫漏相对路径与部分 tuple 形态；L7 Dockerfile mkdir 预建清单是第三份目录清单、无守卫。
- **线5**：L1 uv.lock revision 5→3（`uv lock --check` exit 0 属实，但高版本 uv 会再升回，两版本互踩）；L2 超管群聊 reminder_list 全量视图可广播他人提醒正文；L3 features-messaging「群聊命令无需 @」与 AGENTS 路由行表述张力；L4 search 8 格式 URL/headers/payload 双实现重复（漂移面）；L5 sink.py `send()` return 后不可达代码（存量）。

## 2. 被证伪的发现（防误导后续评审）

1. 「858afcc 无 CI 运行」——证伪：main 与 release/1.0.4 双分支 head_sha=858afcc 均 success（API 实查）；`d129404` 的 failure 正是 `--locked` 闸门抓到 uv.lock 版本未同步、`94d01cd` 修复后转绿——闭环符合设计。
2. 「backup_usb.sh:82 的 `[ -f ] &&` 在 set -e 下会退出」——证伪：沙箱实测文件缺失时脚本继续执行（`&&` 列表豁免），bug 只在缺复制。
3. 「X-Forwarded-For 伪造源 IP 绕过登录锁定/CIDR」——证伪：`_client_ip` 只读 TCP 对端（web.py:154-155），应用层从不读 XFF。
4. 「写面开启但 TOTP 未配置会退化为纯 token」——证伪：totp.py 三条路径都返回可用 secret，空码 401 不发会话（线3 实跑 30 用例）。
5. 「de8c76f 误删可能不止 7 个用例」——证伪：63f9444 的 165 例与 HEAD 183 例逐条 diff，删除 8 例全部有语义替换。
6. 「execute 拒绝路径的 WARNING 是探测口」——证伪：WARNING 只进运维日志，返回模型/用户的与 unknown 同文案（测试锁定）。
7. 「占位号 123456789 白名单可被利用夹带真实号」——排除：正则匹配单个数字串，无拼接机制。
8. 「groupstats 黑名单复刻白名单空集 fail-open 的 H」——排除：空集回落聊天白名单、仍空则不采集（fail-closed 有锁）；blocked 是减法语义，方向正确。
9. 「boolenv 告警哨兵重蹈 0.0 覆辙」——排除：`_warned: set[tuple[str,str]]`，按 (name,value) 去重、新错值仍报。
10. 「cron 表达式畸形输入 DoS」——排除：解析期与执行侧同源预校验，坏表达式在装载期跳过并报告。
11. 「/openclaw import 可覆盖/删除现网配置」——排除：人格与 job 均按 name/id 查重跳过（测试锁定），问题只在 M8 的「导入即执行」。
12. 「cron 输出绕过出站三态/节流」——排除：send_once 同出口同口径（throttle + uncertain）。
13. 「搜索 8 源 base_url 缺内网判定是新 SSRF 洞」——排除：AGENTS §4 配置行既定设计（web 可写凭据例外），写面有会话+CIDR+两段确认+审计，probe 已禁回显响应体。
14. 「六键回落 boot 误降级热读键」——证伪：六键载体全部 import/首调期固化（outbound.py:437-468、pipeline.py:220-222），回落正确。
15. 「ed41610 chown 挪位引入运行期窗口」——证伪：chown 不存在用户构建期 hard error，顺序现正确。
16. 「deploy.py docker 覆盖已有 .env」——证伪：双层存在性检查 + 测试。
17. 「mask-click 回归完全无防护」——部分证伪：addEventListener 形态会被抓，仅 `onclick=` 属性形态逃逸（L 线3-L1）。
18. 「备份票据双侧 IP 同空放行是本轮放宽」——证伪：行为未变，仅注释纠偏。
19. 「test_docker_deploy 静态守卫恒真」——主体排除：负例与具体行为断言齐全，仅 L 线4-L2 一处弱断言、L 线4-L6 逃检形态。
20. 「群统计黑名单是自动探测 bot」——声称核对为「显式点名」（`AGENT_GROUP_STATS_BLOCKED_USERS` 部署者配置），.env.example/docs 如实。

## 3. 测试与文档状况

**实跑数据**（主代理，本次）：默认套件 `pytest -q` → **3461 passed / 73 skipped**（89.57s）；`ruff check` + `ruff format --check`（0.17.1，与 pyproject pin 一致）双绿；`pyright agentcore` 0 errors；`RUN_PERF=1 tests/test_perf.py` → 9 passed 无劣化；PG 全新空库（`qqagent_test` DROP/CREATE 重建，CI 等价起点）`test_pg_store + test_store_contract` → **57 passed**；`uv lock --check` exit 0（Resolved 72 packages）。

**CI**（GitHub API 实查）：`858afcc` 在 main（12:48 与 15:47 两批）与 release/1.0.4 均 success；`d129404` failure（uv.lock 未随版本更新，`--locked` 抓住）→ `94d01cd` 修复 → 绿，commit message 声称属实；dependabot 分支 `91770ee`（pytest 9.2.0/pytest-asyncio 1.4.1/ruff 0.17.3 bump）CI failure 于 lint-and-test 的 Test 步——区间外，属门禁拦下不兼容升级的预期闭环（§8.1 观测，非本轮规则性失败）；其 Docker arm64 workflow success 顺带佐证 arm64 构建路径。

**测试矩阵缺口**（本轮新增行为的回归盲区）：backup_usb.sh 的 cron 块零覆盖（H1 漏网根因）；web_login 审计零覆盖（M10）；modal shim 对 `onclick=` 形态不敏感（线3-L1）；drift 守卫位置参数形态（线1-L1）；`test_root_branch_rechecks` 弱断言（线4-L2）；恒真断言两处（线2-L1）。

**文档一致性**：.env.example 新增键（SESSION_TTL/BLOCKED_USERS/GROUP_EXTRA_SKILLS 等）在 docs/configuration.md、features-community.md、features-skills.md 均有条目且与 ENV_SPECS 同步（schema 守卫用例绿）；CHANGELOG 1.0.4 段与 commits 逐项对应（38624fe/9aa62c7 归 1.0.3-beta 段时序正确）；README 字体/体积数字自洽；pyproject/uv.lock 仅版本与 revision 变化、依赖零新增（无新增 CVE 面）。失真已立案：AC-group-guest-mode 冷备声称（H1）、QQ 人格通道「备份」声称（M7）、`_fmt_wall` docstring（M11）、「无人值守看不到运维技能」仅群目标（M2 口径）、.env.example compose 注释（线4-L4）、features-messaging 与 AGENTS 路由行（线5-L3）。

**未验证面**：(a) 真机 QQ 通道端到端（cron 投递、/persona、/cron、/openclaw 命令）；(b) GHCR publish workflow 实跑（镜像推送与 latest tag；workflow 文件已静态核对）；(c) OpenClaw 8 搜索源真实 API 字段映射（无 key，仅与测试内嵌官方文档注释互证）；(d) M2/M3/M9 的长跑行为复现（代码链已完整核实，需 stub engine 连跑）。**门禁结论**：H1 属已提交代码 → **阻断发布**，修复+回归前不打 1.0.4 tag；M 批无环境盲区型 M（皆本地可修可验），建议同批修复后发布；上轮未验证面 CI 与 perf_baseline 本轮双销项（API success + RUN_PERF 9）。

## 4. 已验证为「无问题」的关键项

1. **访客 where 轴核心**（线1，代码走读+断言对照+AST 实测）：群聊 schema 不可见、execute 拒绝与 unknown 同文案、缺省推导 superuser→private、`AGENT_GROUP_EXTRA_SKILLS` 只对 private 生效且不入 web 写面（fail-closed）、当前 28 处 superuser 注册零盲区形态。
2. **cron 安全骨架**（线2）：投递白名单注册期+执行期双层且缺注入 fail-closed（白名单外不执行 agent）；`cron:` 会话隔离与预算路由短路在 resolve_session 之前；jobs.json 原子写 0600 + O_NOFOLLOW + 写后重读校验回滚 + 备份滚动；人格文件名注入不可能（`_NAME_RE` + O_NOFOLLOW + os.replace）；停机顺序不变量保持（scheduler 仍第一位，cron 无独立连接池）；cron_cmd 经 `_load_plugin_modules` import（reboot 漏接线教训已避开）。
3. **tz 传递修复（323ca67）彻底**：校验/执行/创建/导入四路全带 tz，跨时区触发时刻实测正确（线2 仿真）。
4. **web 登录门**（线3，实跑 TestRouteInventory/TestAuth/TestLoginSession/TestModalJs 30 passed）：原始 token 只认 /api/login（无 `_auth` 路由钉死为例外三件套）；URL query token 拒收；会话生命周期/登出作废/恒时比较/锁定边界（第 6 次拒）全部有回归钉子；12 个写端点 challenge 两段确认逐一在场，「编辑免重复验证」只免 TOTP 不免 challenge；前端 token 只进 sessionStorage 且 `#tok=` 预填后抹地址栏；真 modal 18 断言 + 变异可抓。
5. **容器化四条硬边界**（线4，静态+测试守卫）：data/ 只挂运行期子目录且三份清单两份有双向锁；root→gosu 降权时序正确；PG 客户端 16-bookworm + psql 依赖实测；libpq multiarch glob + arm64 CI 绿；runtime_env 容器判定在 `which` 之前、cgroup 逐形态覆盖（上上轮 M3 三形态全锁定）；reboot_plan/perform_reboot 同一判据、env_snapshot prime 时序正确。
6. **时间前缀体系**（线5）：`_HISTORY_PREFIX_TOKENS` 津贴落实（上轮 M10 销项）、inf/nan/1e300 防线在位、剥离正则双括号双冒号、主动消息同剥、剥离日志走 `log_text`；tz 告警去重（上轮 M11 销项）。
7. **font_for 体系**（线5）：八个渲染文件动态文本绘制点逐个核对全走 `font_for`，回落链与逐字形墨迹探测实测正确，渲染规模护栏（`_MAX_COLS/_MAX_ROWS/max_words`）未回退。
8. **搜索/provider**（线5）：fallback 串行+25s 总墙钟、8 源响应校验防「200 错误体当成功」、key 全链不回显不落日志、providers 漂移告警仅启动期一次且不含值。
9. **群统计黑名单**（线1+线5）：采集层切词前整条跳过零残留、与白名单正交、全角号告警剔除、接线有锁。

## 5. 与在库报告的衔接复核

上轮 REVIEW-4d5c1b8..63f9444 的修复验收（874584b）在本轮区间头部，抽核销项：M10 前缀津贴、M11 tz 告警去重、M8 reboot 判据同源、M12 午夜 flake（本轮全量绿含该用例）、M2 compose `$$` 口径（线4 证伪项复核一致）——全部在位；上轮未验证面 CI、perf_baseline 双销项（§3）。本轮无上轮遗留 H/M 悬空。

## 6. 修复优先级建议

1. **阻断项（发布前必修）**：H1（一行复制 + `$SRC` 前缀 + AC 文档纠正 + 行为级回归）。
2. **同批修复（无人值守安全姿态与声称一致性）**：M7、M8、M9、M2、M3——cron/导入是本轮最大变更面，五个 M 同源（无人值守场景的边界），建议一并收口并同步 AGENTS「信任模型 where 轴」行的私聊目标口径。
3. **功能可用性**：M4、M5、M6（OpenClaw 中文迁移三连，同源于「中文/多行现实输入」）。
4. **审计与一致性**：M1、M10、M11、M12。
5. **L 批量关闭计划**（29 条 > 10，按 §3 收敛规则）：随修复顺带 5 条（线2-L1 两处恒真断言、线4-L2 弱断言、线3-L1 shim 补 `onclick` 派发、线2-L4 预算文案识别）；独立小修 8 条（线1-L1/L2、线3-L2/L3、线4-L1/L3/L4、线5-L3）；显式遗留 16 条及理由：线1-L3（boolenv 存量迁移，独立批次 + BACKLOG 登记）、线1-L4/L5（守卫完备性，随下次触及）、线2-L2（deleteAfterRun 先文档标注「暂不支持」再议实现）、线2-L3（私聊准入收紧需产品定夺）、线2-L5/L6/L7/L8、线4-L5/L6/L7、线5-L1/L2/L4/L5（存量/低危/需决策，均记录在案供下轮销项）。
