# FIX-e079b8d..3a3ce4a.md

对应评审：[`review/REVIEW-e079b8d..3a3ce4a.md`](REVIEW-e079b8d..3a3ce4a.md)（H1 + M×10 + L×14）。
分支：`fix/review-e079b8d-3a3ce4a`。修复日期：2026-11-13。

## 修复总览

**H1 + M×10 + L 必修 11 全修**；L 显式遗留 3 条（L3 已随批修、L5/L6 入 BACKLOG §6.20）。
未提交的 drill_restore 功能（原工作区变更）随本批一并入库。

### 批次一：沙箱白名单（H1 + M1 + L9 + L10）

- **H1 pidstat**：`agentcore/workspace/runner.py` 从 `_READONLY_OPS` 移除 `pidstat`，新增 `_pidstat_ok`：显式拒 `-e`（sysstat 的 `-e <程序> <参数>` 会执行任意程序——修前 `pidstat -e touch x 1 1` 经 medium/low 双档放行并真实落盘）、flag 白名单（`-d/-h/-H/-I/-l/-R/-r/-s/-S/-t/-T/-U/-u/-v/-w/-C/-G/-p/--dec/--human/--version`）、位置参数限 ASCII 数字（`数字` 或 `数字,数字`，全角 isdigit 坑按仓库惯例避开）。
- **M1 pmap**：同样移出免参数集，新增 `_pmap_ok`：仅 `-x/-d/-q/-p` + 数字 pid；拒 rc 文件写面 `-n/-N/--create-rc*/-C/--read-rc*`（`pmap -n` 实测在 HOME 落 `.pmaprc`）与 `-X` 内核地址。
- **L9 systemd-cgtop**：`_systemd_cgtop_ok` 显式拒 `-M`/`--machine`/`--machine=`（与 systemctl 拒 `-M` 同口径；修前 `--machine=.host` 以 `=` 形态绕过位置参数检查而 `-M host` 反被拒）。
- **L10**：`_READONLY_OPS` 注释如实补充 `iostat -f/+f`（读 sar 数据目录、路径遏制限工作区内）。
- 文档同步：AGENTS.md 沙箱行（pidstat/pmap 专属校验组 + cgtop machine 拒）、docs/features-skills.md。
- 回归：`tests/test_runner_security.py::TestReadonlyMemoryCommands` +4 用例（`-e` 三形态、pmap rc 三形态 + 非数字 pid、cgtop machine 三形态 + 全角位置参数）。

### 批次二：群统计对账日期敏感（M10，存量非本批引入）

- **M10**：`tests/test_group_archive.py:23` `_DAY` 由固定 `date(2026,10,6)` 改为 `tz.today_date() - timedelta(days=10)`——StatsCollector 默认 `retention_days=35`，固定日期越过保留线（2026-11-10）后 flush 末尾的 `_maybe_retention` 把测试数据当过期清掉，`test_recompute_matches_live_stats` 必红（CI 11-07 绿、11-13 红双时点实锤；基线 worktree 同样红证明非本批回归）。同文件其它用例本就用 `tz.today_date()` 相对推导，此为唯一遗漏的固定常量。

### 批次三：恢复演练功能入库（M6 + L11 + L12 + L13 + L14）

- **M6**：`tests/test_drill_restore.py::test_verify_failure_stops_before_scratch` 补 mock `pick_latest_backup`（修前真调 `list_backups("data/backups")`，CI 全新 checkout 无该 gitignore 产物 → `SystemExit(2)` 必红，AGENTS.md §5「CI #91」同型）。
- **L11**：`scripts/drill_restore.py` 源库连接移入 try，失败与 restore 失败分支同口径（返回 1 + 打印清场指引）；`main()` 的 KeyboardInterrupt 出口补同口径提示。
- **L12**：`tests/test_drill_restore.py` 文件末补齐变异复核台账——7 个变异点**全部本轮实跑**（verify 返回码 / is_file 探测 / 成功不 drop / 失败也 drop / 抽样偏移丢首行 / ok 判断移除 / L11 返回码），改坏→红→还原记录在案。
- **L13/L14**：`docs/operations.md` 演练块补三条限制（.age 加密备份需先解密、共享 PG 实例 FORCE DROP 互踩、目标库固定字面量的安全语义）；`drill_restore.py` 的「没有 pg_dump 备份」文案改为自解释（点名 .age 与 `--file`）。

### 批次四：CHANGELOG（M7）

- **M7**：`## [未发布]` 段重写——清掉陈旧的「REVIEW-6867995 修复批」内容（已随 1.0.9 发布、与 [1.0.9] 段重复），改为 1.0.9 定稿后 5 项用户可见变更（help 分节、内存/swap 归因、web 定时页、/cron 剥前缀、表情/文件+回声过滤）+ 遗留项指向 BACKLOG 一行。

### 批次五：web「定时」页（M3 + M4 + M9 + L1 + L2 + L4）

- **M3**：`web.py` 前端 `cronTrustedChanged(user)`——用户勾选 trusted 时同步取消并禁用 `crEnabled`（`cronCollect` 恒回传 enabled 布尔，后端 `enabled is None → not trusted` 默认分支在前端流程下永不触发，「trusted 默认停用」只能前端联动保证）；仅用户勾选时联动，表单初始化调用不改动既有任务状态（编辑 trusted+enabled 任务时如实显示）。
- **M4**：toggle/delete 补两段确认——`/api/cron/preview` 增 `action` 参数（toggle/delete 走轻量 normalize，challenge 绑定 `{action,kind,id,enabled}`），两个端点消费 `_consume_confirm`；前端 `cronToggle`/`cronDelete` 改「确认框 → preview → 带 challenge 写入」。`/api/cron/run` 不写数据维持现状（会话门 + 审计 + `_run_lock`）。
- **M9**：`cron_web.save_reminder` 改「先建后删」——修前 cancel 成功而 add 失败即静默丢旧提醒；失败残留旧提醒可重试。`_FakeStore.schedule_add` 同步改为返回新 id（旧假 store 恒返回 "r1" 会让守卫跳过 cancel，掩盖该形态）。
- **L1**：`normalize_payload` 对非空 owner 前置 `isascii() and isdigit()` 校验（与 jobs 层 `_validate_job` 同判据，修前非法值裸奔到 JobsError → web 500）；`_cron_save` 兜底 catch 转 409。
- **L2**：`_cron_save` reminder 路径补 `store is None` 预检（与 delete 端点同口径）。
- **L4**：`tests/test_web.py::TestCronAPI` +3 用例（toggle/delete 无 challenge 直达 400、preview(A)+write(B) 换值攻击 400、reminder 无 store 409）。
- DOM shim：`tests/modal_dom_test.js` +5 断言（M3 前端联动：初始化不改 enabled / 勾 trusted 取消+禁用 / owner 展开 / 取消 trusted 恢复可编辑），El shim 补 `style` 属性。

### 批次六：消息流水线（M2 + L7 + L8）

- **M2**：`agentcore/outbound_files.py` name 兜底分支加「双方 file_id 均确定且不等 → continue」——修前「bot 发报告 → 用户把同名同大小文件发回来说帮我看看」第一次即被静默吞（已复现三场景），`test_missing_size_matches_by_name_only` 把误杀窗口当 feature 锁死。补 `test_genuine_user_file_same_name_size_not_echo`（误杀负例 + 真回声正例四形态）。
- **L7**：`pipeline._build_user_text` 的 file 段占位名统一走 `_file_seg_name`（修前只认 image_like→file，`{name:"备忘录.txt", file:"XYZ_abc.txt"}` 时 LLM 看到协议端内部缓存名而匹配用 name）。
- **L8**：`test_registry_is_bounded` 弱断言 `<= 3` 改 `== 3`（对 no-op 退化恒真）。

### 批次七：help 分节（M5）

- `/reset`（handler 已 superuser-only，admin.py:74-75）从「常用」移入 `ADMIN_ONLY` + 管理节；删「定时提醒」行（reminder_* 已收窄 superuser，普通用户照菜单操作不可用）。
- `tests/test_help_menu.py` 新增 `TestSuperuserGatedReverseLock` 反向锁：AST 建「on_command 注册变量 → handler」映射扫 `is_superuser`/`is_superuser_id`（**AsyncFunctionDef 也要扫**——首次实现只查 FunctionDef 扫空，正是恒真陷阱的现场）；两张显式清单（整命令级必须在管理节 / 子命令级允许普通节）+ 未归类新 gated 命令即红。抽核 12 passed，gated 集合 18 条命令全部对上。

### 批次八：上轮遗留（M8 + L3 + L5/L6 入档）

- **M8**：BACKLOG 新增 §6.20（XM-4/XM-5 悬空项 + 本轮 L5/L6 显式遗留 + XM-1/2/3 指向 CHANGELOG 已知待修段）；`docs/operations.md` 与 `CHANGELOG.md` 两处「可 git apply 的 patch」声称补降级说明（`runtime.patch` 经实测有 apply 失败形态，原文副本是最终兜底）。
- **L3**：cron 试跑保存 task 引用——`agentcore/scheduler/cron_admin.py` 定义 `_BG_TASKS`（agentcore 侧定义、plugins 层 import，方向不变），`cron_web.run_agent_turn` 与 `/cron run` 试跑共用；once/trusted 任务提示文案拼接（once+trusted 同任务两种提示不互相短路）。回归 `test_run_agent_turn_saves_task_ref_and_hints`（引用保存 + 完成摘除 + run_job 真触发 + 文案）。
- **L5/L6** 显式遗留入 BACKLOG §6.20（存量 tool 行不回填围栏靠窗口滚出衰减；outbox 500 行截断与 uncertain 保留语义的张力）。

### 追加提交：outbox uncertain 行保底（L6，2026-11-13 用户拍板「值得修」）

- **L6**：`agentcore/outbox.py` `_truncate` 从无脑 `lines[-MAX_LINES:]` 改为 **uncertain 行保底**——M6/M14 的两阶段设计让「通知全失败」的悬空账保留多轮等认领，无脑截最新会把挤到窗口外的 uncertain 行蒸发（修前注释声称「不会被历史行挤掉」与极端场景行为漂移：单次通知失败周期内累计 ~500 条新投递 + 进程不重启即触发）。实现：被截部分解析出 `outcome == uncertain` 的行带进保留集（坏行解析失败即非 uncertain，随旧账走；病态期总行数可超 MAX_LINES，方向宁留勿丢）。
- 回归：`tests/test_outbox.py` +2（保底 + peek 仍认领 / 坏行随旧账截掉）；变异复核（去掉保底 → 保底用例必红）。
- 同批评估**不修 L5**（用户确认）：存量 `role=tool` 原文行的围栏回填需先定语义——存库 tool 消息不带工具身份，`_project_history` 无法区分「该围未围」与「表外本地工具本就不围」，粗判一律补围属过度防御；且存量行在历史窗口内几十轮即滚出或被滚动摘要处理（摘要注入侧本来就过围栏）。已入 BACKLOG §6.20 遗留。

## 验证

- 默认套件：**4009 passed / 83 skipped**（修复前 3994+83；新增 15 条用例）
- PG 全新空库全量：**4107 passed / 9 skipped**（M10 修复生效，修复前该套件 1 failed）
- ruff check + format：**双绿**（269 文件）
- pyright agentcore：**0 errors**
- 变异复核（全部实跑「改坏→红→还原」）：H1 显式 -e 拒（断言对准「执行任意程序」文案——白名单路径的 reason 不同，否则变异存活，首次实现即被此抓出）/ pmap 校验退化为放行（2 红）/ cgtop machine 拒移除 / M10 余量 40 天（PG 环境红）/ M6 相关 verify 返回码 + is_file 探测 + ok 判断移除 / L11 返回码 / M9 换回先删后建 / M5 /reset 挪回常用节（单向锁红）/ L3 去掉引用保存
- 归档闸：`scripts/check_review_docs.py`（含 `--write-stats` 重算计数块）

## 遗留 / 降级

- **L5**：M2 修复只盖新写入；存量库 `role=tool` 原文行的历史重放仍不过围栏（靠预算窗口滚出自然衰减）。回填需在 `_project_history` 补围栏，误伤面与成本权衡后单独一批（BACKLOG §6.20 入档）。
- **L6**：outbox `MAX_LINES=500` 截断与 M6 后「uncertain 行保留多轮」的语义张力（生产单机远达不到 500 条/通知失败周期；随下一次 outbox 触碰时顺手）。
- **XM-4/XM-5**：上轮 cross 轮悬空项，本批入档未修（BACKLOG §6.20：flock 假阳性偶现竞态需复现根因；runtime.patch 的 apply 失败已文档三处如实化，彻底修属 /update 回滚面重构）。
- **未验证面**：真机 QQ 通道的表情/文件形态（判定逻辑已内存仿真 + 单测覆盖）；M9 的故障注入为单测级桩（store 抛异常的路径）；CI 结论见 push 后运行记录。
