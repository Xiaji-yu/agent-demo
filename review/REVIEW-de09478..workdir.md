# agent-demo 增量评审报告（de09478..workdir）

**评审范围**：de09478..1e0f5ae（1 commit：上轮双 H 修复 + ssh_run/重启回执/图片贯穿 tool-loop）+ **33 项未提交变更**（dangerous-admin-actions 功能 + P0 权限缺口修复 + AGENT_PERMISSION_LEVEL 权限三级制 + run_shell）。范围名按 `FIX-81a8521..workdir.md` 先例用字面 `workdir`。
**评审日期**：2026-10-05
**评审方式**：5 线并行子代理审查（线1 沙箱与权限级别 / 线2 技能权限体系与 engine / 线3 消息流水线与插件层 / 线4 记忆·RAG·调度·预算·备份 / 线5 测试与文档一致性）+ 主代理逐条实证。**线1 子代理启动失败无产出，由主代理按同一任务书接手**（先例：REVIEW-46c85d1..6ec3f7c）。
**工作区状态**：不干净（33 项未提交：22 modified + 11 untracked），评审全程未修改任何受版本控制文件；主代理复现仅使用 /tmp。

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 最高风险 | **H1** run_shell/run_command(curl)/log_tail/docker_logs 四个超管工具输出**未过围栏**直达 prompt（与上轮 H2 ssh_run 同类注入面）→ §8.3 **阻断未提交变更提交**，修一行 + 回归即放行 |
| M | 11 条（围栏外的权限披露 2、声称与实现不符 3、健壮性 4、文档治理 2），L 21 条 |
| P0 修复复核 | ✅ handler 21 个 superuser 注册点无一遗漏；config.yaml 段删除有测试锁；registry unknown 同文案无探测口——但 /skills 命令（M3）与 env 层 `SUPERUSERS=*`（M5）留有两个旁路 |
| 上轮修复衔接 | H1/H2/M1–M6 全部真实在位（主代理逐条核对，含 ss -K 实测拒绝、围栏查表在位） |
| 测试 | 默认 **1950 passed + 50 skipped**（57 文件/2000 收集）；`TEST_DATABASE_URL`（qqagent_test）**1991 passed + 9 skipped**；ruff 0.9.6 check+format 双绿；CI run #89（1e0f5ae）success，未提交部分尚未进 CI |
| 声称核对 | 1e0f5ae 抽查 13 项全部落地；未提交变更的文档声称逐项对照一致——**唯 run_build_script「没有内容权」不成立**（M1） |

## 1. 已实证的问题

### H1（线2，主代理核对机制）四个超管工具的输出未过围栏直达 prompt
`agentcore/loop/engine.py:41` `_UNTRUSTED_TOOL_RESULTS = frozenset({"search_web", "search_multi", "ssh_run"})`，其余工具结果原样进 messages（engine.py:845-851 注释「其余工具是 bot 自身计算/操作产物」）。但以下四个输出是**外部可控文本**：`run_shell`（bash 输出）、`run_command`（medium+ 白名单含 curl GET，可拉任意网页；输出还包括 cat 读到的任意工作区文件）、`log_tail`（/var/log，Web 日志常含攻击者可控的请求串）、`docker_logs`（用户部署服务的容器日志）。与已定级 H 的 ssh_run（上轮 H2）完全同类：围栏内注入变成**未围栏直达持有 proc_kill/run_shell/fs_delete 的超管工具循环**。对照：fetch_url/summarize_url/db_query/run_build_script 均自带或走引擎围栏，唯独这四个漏网。主代理核对：frozenset 内容与四工具返回路径均为代码级事实【推演】（端到端需活体 LLM，机制与上轮 H2 同构）。
**修复**：四名纳入 `_UNTRUSTED_TOOL_RESULTS`（run_command 含 fs_read 类读面可再议，至少 run_shell/run_command/log_tail/docker_logs 必须围）+ test_engine 围栏回归。**§8.3 门禁：阻断提交。**

### M1（线5，主代理复现关键链节）run_build_script「没有内容权」声称不成立：medium 档可重组「任意代码 + 全量环境」链
README.md:647 与 AGENTS.md 声称脚本「你预先写好的、LLM 没有内容权」；实现上 `scripts/` 对同档 fs_write 可写。直接 fs_write 覆盖会被 fs.py:126-134 的 `chmod(0o600)`+`os.replace` 剥掉 +x 位（EACCES），但全白名单成员链成立：`curl`(GET) 拉攻击者 .tar.gz → `tar -xzf -C scripts` → `run_build_script` 执行 → **继承完整进程环境**（LLM_API_KEY、DATABASE_URL、SUPERUSERS），绕过 run_command 最小环境不变量与 run_shell 的 high 专属门。主代理已复现决定性链节【已复现】：tar 解压 0777 条目在 umask 022 下落盘 **0755 可执行**，且 `runner.permitted("tar",["-x","-f","a.tgz","-C","out"])` 放行。触发前提=超管会话被注入（正是 H1 的暴露面），维持 M；H1 修复后此链失去注入入口，但 medium 档「内容权」声称仍须改口。**修复**：a) `scripts/` 对 fs_write 关闭新建/改写，或 b) run_build_script 改最小环境，或 c) 最低限度文档如实改述 + tar/unzip 条目 exec 位落盘后清理；并同步 README/AGENTS 措辞。

### M2（线3，主代理忠实复现）media.save_image_atomic 的 0600 窗口，同型遗漏共 3 处
`media.py:344-345` `tmp.write_bytes(data); tmp.chmod(0o600)`——写完才收紧。主代理按同构代码复现【已复现】：umask 022 下 chmod 前权限位 **0o644**，对照 `config_write.atomic_write` 修法（os.open 创建即 0600）为 0o600。上轮 M1 修了 .env 这一处，同型的 `media.py`、`workspace/fs.py::_write_sync`、`budget.py::_save` 三处漏网。介质敏感度递减（图片/工作区文件/账本），合记一条 M。**修复**：三处统一 `os.open(..., 0o600)`（可加 O_NOFOLLOW）。

### M3（线2，主代理核对代码路径）/skills 与 /skill info 把隐藏技能名披露给普通用户
`admin.py:222-224` 对 `reg.skills` 全量列举名字+✅/❌，门仅 `is_allowed`——白名单群任意成员可确定性枚举 log_tail/db_query/run_shell/proc_kill 等全部隐藏名，把 P0 在 registry 层堵掉的「存在性探测口」原样奉还；`/skill info`（admin.py:335-361）同门缺失，会向普通用户回显 manifest 的 permission 串。【已复现】（确定性代码路径，无需 LLM）。**修复**：非 superuser 只列 `is_allowed` 为真的名字；`/skill info` 补 superuser 门。

### M4（线2，主代理复现）_PERMISSION_DENIED_RE 扫工具结果全文：外部内容含敏感词即毒化 denied_skills
`engine.py:30-32` 对**任意**工具结果全文匹配 `permission denied|无权限|权限不足`，命中即加入 denied_skills（后续同工具调用被短路）+ 计数超限硬停误报「需要管理员权限」。主代理复现【已复现】：围栏文本内夹带 "permission denied" 的网页正文 → `_is_permission_denied=True`；正常结果 False。白名单群用户发一个含该词的 URL 即可让 bot 同轮拒绝抓取并误报权限错误（可用性攻击，无提权）。**修复**：只匹配固定模板（`Error: permission denied for skill` 前缀 / handler 白名单文案），不扫结果全文。

### M5（线2，主代理复现）PermissionChecker 仍支持 env 层 `SUPERUSERS=*` 通配
`permissions.py:28-29` `"*" in self.superusers` → 任何人过 registry 闸；`bot.py` 会把 `SUPERUSERS=*` 归一化传给 NoneBot config。主代理复现【已复现】：`SUPERUSERS=*` 时 checker 对任意人放行 log_tail，而 handler 层 `is_superuser` 对**所有人**（含真管理员）fail-closed——净效果=全部超管 schema 披露 + 功能全瞎，非提权。与「勿恢复任何形式 superusers 通配」的 P0 决意相悖，且 test_permissions.py:34-36 还在锁定该语义。**修复**：删 `"*"` 语义（或启动告警按空集处理），同步改测试。

### M6（线1 主代理）docker_ctrl 自保护不认「容器名」，容器化部署下按名操作自身容器穿透
`_container_is_self` 只比对 cgroup 容器 id/≥12 位前缀；而 `docker ps` 语境下用户/LLM 引用容器**名字**是主形态（PG_CONTAINER 按名比对不受影响，仅「自身容器」缺失名字通道）。compose 部署 bot 时 `docker_ctrl("stop","<bot容器名>")` 可穿过自保护。当前部署 bot 跑在宿主机（cgroup 无 docker 段）→ 无症状，属**保护承诺与实现不符**的条件性缺陷【推演】。**修复**：补 /proc/self/mountinfo 或 hostname（容器短 id）匹配，或 README 限定该保护仅覆盖 id 形态。

### M7（线3 走查遗漏，主代理证伪其结论后收录）help_cmd 无任何 ACL 门
`admin.py:113-130` `handle_help` 体内 **0 处 is_allowed**（主代理重读核实；线3 报告「12 个 on_command handler 层 is_allowed+deny 统一出口」在该点上不成立）。后果：非白名单群、被拉黑用户发 `/aihelp`/「帮助」均获回复（含 Pillow 渲染），违反「BLOCKED_USERS 所有入口静默拒绝」与「越界明说」两条不变量；帮助文本还向未授权群描述了全部管理指令面。**修复**：handler 首行补 `is_allowed`+`deny`，与其余 11 个命令对齐。

### M8（线5）FIX-dangerous-admin-actions.md 描述已被推翻的旧实现且无 supersession 横幅
该权威修复记录仍宣称四键白名单/pattern 精确匹配/name 白名单——均已在本轮移除，照它回滚即重开 P0 级面。**修复**：头部加取代横幅（指向 FIX-permission-levels-202610.md），分节标注「已保留/已推翻」；顺带勘误其 TestTarHardening「12 例」实为 11 例。

### M9（线5，主代理核实）§9.1 索引断链第三次发生
FIX-audit-remediation-20260918.md、FIX-images-lost-after-tool-call.md（均已入库）与 FIX-dangerous-admin-actions.md、FIX-permission-levels-202610.md（未跟踪）共 4 份不在 §9.1 索引；另 §9.1 表中 `FIX-c472e56..733f57e` 与 `FIX-46c85d1..6ec3f7c` 两行之间有空行把表截断。**处置**：归档本报告时已同步补 4 行索引并修断行（属 §2.5 允许的 review/ 产物更新）；残留下仅 M8 的横幅属修复阶段。

### M10（线4）APScheduler 自身 cron 用宿主机本地时区，与全仓统一时区脱节
`__init__.py:549` `AgentScheduler()` 未传 timezone → `kb_digest/db_backup/archive_prune` 三个 cron 按宿主机 get_localzone() 触发，而提醒/推送/预算日键全走 agentcore.tz（默认 Asia/Shanghai）。当前宿主机 +8 无症状；TZ=UTC 部署下蒸馏/备份偏移 8 小时。**修复**：`AgentScheduler(timezone=str(tz.zoneinfo()))` 一行 + README 补注。【推演】+ 子代理本机验证 apscheduler 缺省行为；UTC 主机触发时刻未实测（未验证面）。

### M11（线4）reminder.parse_when 全程 naive 本地时间，与 cron 路径对「同一句时间」语义分裂
`reminder.py:108` `dt.datetime.now()`（naive）+ `combine` 不带 tzinfo → 「明天8点」（once）在 UTC 宿主机=北京 16:00，而「每天8点」（cron，走 next_cron_time）=北京 08:00。同 skill 两种说法差 8 小时。**修复**：`now = now or tz.now()`；combine 带 `tzinfo=tz.zoneinfo()`（相对偏移「N分钟后」不受影响）。条件性与 M10 相同。【推演】

## 1.x L 级（压缩列出，21 条）

| # | 位置 | 问题 | 来源 |
|---|---|---|---|
| L1 | action_skills.py:278 | proc_kill 超限文案残留「白名单 pattern」措辞（机制已删） | 线5 |
| L2 | media.py:257-265 | own_client 恒假死代码分支 | 线3 |
| L3 | admin.py:69,218 | `hasattr(event,"group_id")` 可得 "None" 字符串会话键，/reset 静默清错会话（extra="allow" 已核对；acl.py 注释本要求 is not None） | 线3 |
| L4 | music_route.py:511-512 | docstring 残留「config.yaml superusers 通配」失效描述（线2/线3 同点） | 线2/3 |
| L5 | test_web.py:564-583 | 日志轮询守护用源码串断言，变异不敏感（`if (logInFlight) return` 删掉仍绿） | 线3 |
| L6 | tests/ | 缺「权限键不可写」负面清单参数化用例（AGENT_PERMISSION_LEVEL/SUPERUSERS/TOKEN 等） | 线3 |
| L7 | reboot.py:266-271 | 回执 JSON 落盘默认 0644（含 QQ/群号） | 线3 |
| L8 | shell_skill.py 审计行 | raw 命令含换行可伪造多行审计记录（runner 对同威胁显式设防，shell 侧未对齐；建议 repr/折行） | 线1 主代理 |
| L9 | engine.py:828+ | registry unknown 文案不计数，幻觉调用空转至 max_iterations=8（「忽略」语义既定代价，建议独立空转计数） | 线2 |
| L10 | registry.py:192 | prompt 型同名 manifest 启动期静默覆盖内置工具（仅管理员级可达，加固项） | 线2 |
| L11 | __init__.py checker 构造 | 仅 config.yaml 单侧测试锁（根因已断，备查） | 线5 |
| L12 | push.py:371-382 | register_jobs 日志 5 占位符传 9 参数 → 该行 logging error 丢失（子代理已复现 logging 行为） | 线4 |
| L13 | test_backup.py | JSONL 备份→恢复端到端零真库覆盖（vector/JSONB str 回灌纯推演可信） | 线4 |
| L14 | push.py:456-465 | tick 统计桶把 uncertain/blocked 计入 disabled，读数失真 | 线4 |
| L15 | push.py:623-634 | 生成正文未包 route_context("push")，/usage 按路由缺维度 | 线4 |
| L16 | archive.py:35-36、distill.py:477 | 归档/蒸馏 `_today()` 用宿主机 localtime，与统一日键不同源 | 线4 |
| L17 | archive.py:74-81 | 归档新文件先落盘后 chmod（短暂 0644），存量不补救 | 线4 |
| L18 | ingest_kb_samples.py:386-393 | 「需 --replace 而放弃」分支仍已物化切块文件（孤儿块滞留，可自愈） | 线4 |
| L19 | db_backup.py:680-705 | _restore_sql PIPE 双管道理论死锁（--inserts 格式 dump 必现；有 pre-restore 快照兜底） | 线4 |
| L20 | scripts/backup_db.py:114-176 | restore 与运行中 bot 无互斥/停机提示 | 线4 |
| L21 | matcher.py:98、music_route.py:131 | 群上下文记录/序号选歌不查 BLOCKED_USERS（无越权通道，边界未文档化——观察项） | 线3 |

## 2. 被证伪的发现（防重走弯路）

1. **「12 个 on_command handler 层 is_allowed 全覆盖」（线3）**——证伪一处：help_cmd（M7）无门；主代理已复核其余 11 处确实在位。
2. **「SUPERUSERS=* 可提权」**——证伪：handler 层实测对所有人（含真管理员）fail-closed（M5 定 M 的依据）。
3. **「push 的 LLM 正文绕过围栏」**——证伪：正文是出站消息不进 prompt；prompt 输入仅部署配置，无用户内容回流。
4. **「README/.env.example 残留已删四键或旧『留空=关闭』说法」**——证伪：全仓四键仅存「已移除」语境；唯一「留空=关闭」命中是点歌 API，与白名单无关。
5. **「BACKLOG『34 个内置工具』是估算数字」**——证伪：@registry.register 调用点逐一去重恰 34。
6. **「system_status 收编漏注册期过滤」「README 与 FIX 级别矩阵不一致」**——证伪（线5）。
7. **「proc_kill 第二人抢确认」**——证伪：token 按 (user_id,pattern,signal) 键控 + compare_digest。
8. **「run_command readonly_only 有绕过」**——证伪：主代理 12 形态攻击实测（git --output / find -delete/-exec / grep -f 绝对路径 / du --files0-from / hostname 位置参数 / systemctl 动作类 / journalctl --rotate / dmesg -C / tar / git -C 前缀等）全部拒绝，仅纯读通过【已复现】。

**存疑（未定级，附缺失证据）**：① acl/env 双源 superusers 在绕过 bot.py 的启动方式（如 `nb run`）下是否漂移——本环境起不了 NoneBot；② 蒸馏语义级投毒与零宽字符绕过 `\s`——属 sanitize.py 已声明边界的具体化，缺真实样例；③ 全角破折号围栏 lookalike 不被打散——缺「模型会误认」证据；④ `save_fact(session_id="")` 双实现分歧——当前调用方恒传非空 sid，不可达；⑤ `bump_chat_count` 非同事务并发双触发——缺并发复现；⑥ 两份新 FIX 文档的变异复核声称——只读子代理不可复跑，由主代理本轮实际执行的 6 组变异（P0 两处 + 级别闸/只读子集/自保护/环境泄漏）直接背书。

## 3. 测试与文档状况

- **实跑（主代理）**：默认 `pytest -q` = **1950 passed + 50 skipped**（57 文件/2000 收集）；`TEST_DATABASE_URL=qqagent_test` = **1991 passed + 9 skipped**；ruff 0.9.6 `check`+`format --check` 双绿；CI API 实核 run #89（1e0f5ae）success——未提交 33 项尚未进 CI。
- **CI 等价性**（线5）：ci.yml = ruff 双门禁 + pytest（pgvector:pg16 service 注入 TEST_DATABASE_URL）+ ffmpeg；本轮未改 pyproject、无新依赖，无已知致红点。
- **声称核对**：1e0f5ae 全量声称抽查 13 项全部落地（H1 ss 白名单/H2 围栏/M1-M6/L7/L12/L13/L15/L17 及抽验 6 条）；未提交变更的 README/AGENTS/BACKLOG/.env.example 声称逐项对照一致，唯一失真=run_build_script「没有内容权」（M1）。
- **测试矩阵缺口**：权限键不可写负面清单（L6）、M2×unknown 交互契约（L4 类似项）、JSONL 真库恢复（L13）。
- **未验证面与门禁（§8.3）**：(a) 活体 LLM 端到端注入（H1/M4 的完整轮次）——机制级已核，**H1 存在 → 阻断未提交变更提交，修复+回归后放行**；(b) UTC 宿主机上 M10/M11 的触发时刻——当前部署 +8 无症状，具备环境时补验；(c) 容器化部署的 M6——当前 host 部署不触发；(d) 生产 OneBot 载荷私聊是否携带 group_id 字段（L3 触发概率）。以上均不属于「带 H 盲区合并」，H1 本体是代码级已核事实。

## 4. 已验证为「无问题」的关键项（抽样）

P0 修复面：21 个 superuser 注册点 handler 门无一遗漏（线2 逐一核对）；P0 回归用例全部 _AllowAll 变异敏感、非恒真；config.yaml 段删除有专测。web：门禁 fail-closed 全套、写面 7 键无权限/凭据键、值注入防御、atomic_write 0600 在位。reboot：C→B→A、观察窗、scheduler 传递、双门+接线锁、回执一次性消费；且**无绕过通道**（run_command systemctl 仅 8 个只读子命令、kill 不放行、插件层无其他 spawn 点）。出站三态单一判据、不降级不重发；防抖/停机顺序/幂等。pipeline：外部内容全围栏、回显清洗、最近图片键控。双实现契约 test_store_contract 真参数化；隐私围栏注入点全量 grep 命中唯一入口；budget 跨月原子写；备份在产实证（data/backups 连续 7 天 pg_dump+sha256）；无界缓存清点无失控点。权限级别制：三层注册矩阵测试锁、readonly_only 12 形态、run_shell 三层闸/最小环境/杀进程组/审计均有变异敏感用例；级别键不进 web 写面（WRITABLE 逐键核对）。

## 5. 与在库报告的衔接复核

| 上轮（REVIEW-3ce6e0a..de09478） | 状态 |
|---|---|
| H1 ss -K 白名单穿透 | ✅ 已修且健在（主代理实测 `-K/--kill` 拒绝） |
| H2 ssh_run 围栏 | ✅ 已修（frozenset 在位 + 用例锁）；**本轮发现同类面扩大为 H1** |
| M1 atomic_write 0600 | ✅ 在位；**同型窗口另存 3 处（本轮 M2）** |
| M2 reboot scheduler | ✅ 在位（reboot.py:244） |
| M3 C 步观察窗 | ✅ 在位（0.5s 非零回退） |
| M4 LogLevel | ✅ 在位 |
| M5 BACKLOG 数字 | ✅ 当时空测属实；本轮已按新实测重写（1950+50 / PG 1991+9） |
| M6 ssh_run 接线锁 | ✅ 在位（test_tools 两名单） |
| 上轮未验证面 (a) PG 套件 | ✅ 本轮销项（1991+9 实跑） |

## 6. 修复优先级建议

1. **放行闸（提交前必须）**：H1 四工具入围栏查表 + test_engine 回归；顺手 M4（M2 正则改固定模板）与 M3（/skills//skill info 收门）——三处都在 engine/admin 的几行内。
2. **同批提交**：M2 三处 0600、M1 文档改口 + tar exec 位处置（选 a/b/c 之一）、M7 help_cmd ACL、M8 横幅（索引 4 行已随本报告归档补齐）、L1/L4 文案与 docstring。
3. **近期（不阻断）**：M5 删 env 通配、M6 容器名自保护、M10/M11 时区两行修、L 级按表清。
4. 全部修复走 FIX-de09478..workdir.md，变异复核照 §2.4。

> 归档记录：本报告归档时已在 REVIEW-WORKFLOW.md §9 追加索引行、§9.1 补 4 条缺失 FIX 索引并修复表断行（M9 的索引部分就地闭环）。
