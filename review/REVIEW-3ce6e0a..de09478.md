# agent-demo 近期 Commit 评审报告

**评审范围**：`3ce6e0a..de09478`（4 个 commit）+ 未提交变更预审（13 个修改 + 3 个未跟踪：ssh_run 技能、reboot 回执接线、图片贯穿 tool-loop、文档同步）
**评审日期**：2026-10-02
**评审方式**：4 线并行只读子代理分线审查（线1 Web 受控写入面 / 线2 reboot 执行链 / 线3 沙箱+ssh_run / 线4 engine·facts·文档）+ 主代理逐条实证（实跑三套测试、复现、证伪、变异测试）
**工作区状态**：dirty（`git status`：13 个已跟踪文件修改 + 3 个未跟踪，均为本次待预审的未提交变更；**评审全程未修改任何受版本控制文件**——变异测试在 `/tmp` 的独立 git worktree 内完成，结束后已 `worktree remove`，主工作区零改动）

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | **2 个 H，全部位于未提交变更**：H1 `ss -K/--kill` 穿透 A 组「免参数只读」白名单（破坏性动作放行）；H2 ssh_run 远端输出不过围栏直达 prompt（§4 不可信内容不变量违反，注入落在超管权限工具循环内）→ **未提交部分不应当前形态提交（§8.3 阻断）** |
| 提交部分（3ce6e0a..de09478） | 无 H；M×2（atomic_write 窗口期权限、reboot C 步无命令级退路）+ L×7；M1–M10 修复复核**全部真修** |
| 未提交部分其余 | M×4（reboot 停机漏传 scheduler、TOFU 警告被静默、BACKLOG 数字失实、ssh_run 接线无测试锁）+ L×12 |
| 测试（主代理实跑） | 默认套件 **1805 passed / 48 skipped**（73.8s）；带 PG（`qqagent_test`）**1844 passed / 9 skipped**（82.5s）；`--collect-only` 实测 **1853** collected；ruff check + ruff format --check（0.9.6 = pin）全绿（148 files） |
| 变异测试（主代理，git worktree） | 3/3 被抓（images 保留 / ssh `--` 分隔 / config_write 禁字符），无存活 |
| 声称核对 | 1aef144「7 键白名单 + M1–M10 修复」逐条属实；8575d6a「C→B→A 每步失败有退路」对 C 步**命令级失败**不成立（M3）；de09478 行为属实但回归测试仅为源码字符串护栏（L）；未提交 FIX-images-lost-after-tool-call.md 逐条属实（含变异声称独立复验）；BACKLOG 头部数字失实（M5） |
| 依赖安全（§4 首查） | 本轮 commit 与工作区对 `pyproject.toml` **零变化** |
| 门禁结论（§8.3） | 未提交部分存在 H → **阻断提交**，H1/H2 修复并带回归后放行；已提交部分无 H 不阻断 |

## 1. 已实证的问题

### H1｜`ss -K/--kill` 穿透 A 组「免参数只读」白名单（未提交 runner.py）【已复现】

- **位置**：`agentcore/workspace/runner.py:204-217`（`_READONLY_OPS` 含 `ss`）+ `:768-770`（该分支除全局四道闸外零参数校验直接放行）。
- **证据**：
  - 本机 `ss --version` = iproute2 6.1.0，`ss -h` 实证存在 `-K, --kill  forcibly close sockets`；
  - 主代理内存仿真（不执行命令）：`CommandRunner.check("ss", ["-K"])` → `(True, "")`；`["--kill"]`、`["-K","dst",":8080"]`、粘合 `["-Kx"]` 全部 `(True, "")`；
  - `tests/test_runner_security.py:1160` 仅 `ss -tulnp` 正例，无 `-K` 负例。
- **影响**：代码注释、AGENTS.md:109、README.md:588 均断言 A 组「没有任何写文件或执行代码的选项」——`ss -K` 可按过滤器批量强断 socket（含 bot **自己**的 WS 连接：同 uid 无需特权），与本变更刻意排除的 `kill` 同类且不可回退。「只读」白名单保证被打破，LLM 可经 `run_command` 触发破坏性动作。对他人 socket 需 root/CAP_NET_ADMIN 属部署面前置条件，但白名单不变量不应依赖进程特权。
- **建议**：`ss` 移出 A 组改逐 flag 白名单（或显式拒 `-K/--kill` 及其粘合/`=` 变体），补 `ss -K` / `--kill` / `-Kx` 负例测试；文档三处「ss 只读」表述随修。

### H2｜ssh_run 远端输出未过 safety 围栏直达 prompt（未提交）【已复现·读码断定】

- **位置**：`agentcore/loop/engine.py:39`（`_UNTRUSTED_TOOL_RESULTS = frozenset({"search_web","search_multi"})`）+ `:843-848`（查表不中 → `model_result = safe_result` 原样进 messages 并持久化为 tool 消息）；`agentcore/skills/ssh_skill.py:281-289`（返回值无自围栏）。
- **证据**：`grep ssh_run agentcore/loop/engine.py` 零命中（不在查表）；engine.py:839-842 注释「其余工具是 bot 自身计算/操作产物」对 ssh_run 不成立；对照先例：web_fetch 自带围栏（`web_fetch.py:160` `fence_untrusted("网页内容", …)`）。
- **影响**：违反 AGENTS.md §4 不可信内容不变量（外部文本进 prompt 前必须过围栏）。ssh_run 的 dmesg/logread/netstat/who 输出是**远端主机控制的文本**（目标常为暴露公网的 OpenWrt/NAS）；注入文本落在**超管权限的工具循环内**，可诱导同循环后续 `run_command`/`fs_write` 以超管身份执行——跨机注入→本机横向移动路径。
- **建议**：`ssh_run` 加入 `_UNTRUSTED_TOOL_RESULTS`（一行），或在 ssh_skill 内自围栏（照 web_fetch 先例）；回归测试断言 tool 消息含围栏头。

### M1｜atomic_write 窗口期以 0644 承载含凭据的完整 .env（1aef144，已提交）【已复现】

- **位置**：`plugins/qq_agent_adapter/config_write.py:217-222`：`open(tmp,"w")`（0666 & ~umask）→ write → fsync → **之后**才 `os.chmod(tmp, mode)` → replace。
- **证据**：主代理按同序仿真：窗口期实测 **0o644**，chmod 后 0o600。`patch_env_text` 是整文件替换——窗口内 tmp 含 `LLM_API_KEY`/`AGENT_WEB_TOKEN` 等全部密钥；进程在 chmod 前被杀/断电则 0644 的 `.env.tmp.write` 持久残留。上轮 M3 只修对了「最终态」。
- **次要**：tmp 文件名固定可预测，具目录写权限的本地用户可预置同名 symlink 使 `open("w")` 截断目标。
- **建议**：`os.open(tmp, O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW, 0o600)`，或创建后立即 chmod 再写内容。

### M2｜reboot 路径停机编排漏传 scheduler，「scheduler→flush→aclose」在 reboot 路径不成立（未提交）【已复现·读码链条】

- **位置**：`plugins/qq_agent_adapter/reboot.py:191,206`（`_shutdown_everything` 只取 memory，`shutdown_agent` 不带 scheduler）；对照 `__init__.py:697-700`（优雅停机钩子有传）。
- **证据链**：reboot 经 `os.execv`/`os._exit` 退出 → NoneBot `on_shutdown` 钩子（唯一停 scheduler 处，`__init__.py:680-702`）永不执行；`lifecycle.py` `scheduler=None` 缺省 → `_stop_scheduler` no-op；宽限（默认 2s）+ flush deadline（默认 30s）窗口内 reminder/push tick（最小间隔 5s）大概率再 firing：aclose 后触发→任务报错，tick 在发送/落库中途被截断→重启后提醒**重发或丢失**。reboot.py 模块 docstring 与 AGENTS.md:113 的停机不变量在 reboot 路径落空。
- **建议**：`_shutdown_everything` 补 `scheduler=getattr(get_driver(), "_agent_scheduler", None)`；`tests/test_reboot.py:250` 的断言补 scheduler kwarg（现只查 debouncer/memory，本条回归抓不住）。

### M3｜reboot C 步只回退「spawn 失败」，不观察子命令自身失败（8575d6a，已提交）【读码断定】

- **位置**：`reboot.py:148-155`：spawn 成功即 `do_exit(0)`，子进程退出码无人观察。
- **影响**：`AGENT_REBOOT_CMD` 配错（unit 名拼错、docker 未起、supervisorctl 连不上）时子命令非零退出，本进程已退出——docstring「每一步失败都有明确退路」对 C 步只覆盖 exec 级失败，后果是 bot 彻底下线等人工（B 步 execv 本可兜住）。
- **建议**：spawn 后 `Popen.poll()` 观察 0.2–0.5s，已退出即回退 execv；至少 README 部署前提处明示该边界。

### M4｜`LogLevel=ERROR` 抑制 TOFU 首连警告，文档承诺的检测信号不存在（未提交）【已复现·实机】

- **位置**：`agentcore/skills/ssh_skill.py:166`（`"-o", "LogLevel=ERROR"` + 注释「首次连接的 Warning 仍会输出（TOFU 可见）」）；docstring:13-14、README.md:612-614、.env.example 同口径披露。
- **证据**：主代理对本机 sshd 实测——`LogLevel=ERROR` 下首次连接**无**「Warning: Permanently added」输出；默认级别（INFO）下正常出现。该警告属 INFO 级，`LogLevel=ERROR` 恰好抑制。
- **影响**：ssh_skill.py:19-21 与 README 披露的「首次连接 MITM 可经 Warning 发现」残余风险检测机制实际失效，TOFU 完全静默（叠加 L-线3-7 的 CWD 相对 known_hosts 路径，换目录启动还会静默重新 TOFU）。
- **建议**：去掉 `LogLevel=ERROR`（子进程输出本有 8KB/20s 帽），或实测后改文档如实声明无首连信号。

### M5｜BACKLOG 头部规模数字与勘误行自相矛盾、实测偏差（未提交）【已复现】

- **位置**：`BACKLOG.md:8-12`：「1852 收集：1804 通过 + 48 跳过」+ 勘误行「+1 文件 +12 收集」。
- **证据**（主代理实测）：`pytest --collect-only` = **1853**；默认套件实跑 **1805 passed**（非 1804）；带 PG 实跑 **1844 passed / 9 skipped**（文中换算口径 1743/9，虽诚实标注 `unknown` 但数字失实）；测试文件实测 **53** 个，勘误行「50+1=51」算术不成立（区间内新增 test_config_write.py、test_reboot.py 两个 + 未跟踪 test_ssh_skill.py = +3）。违反 AGENTS.md §5「文档数字必须实测后写入」。
- **建议**：按实测重写本行（1853 / 1805+48 / PG 1844+9），勘误行改按实际新增文件枚举。

### M6｜ssh_run 的 builtin 接线点零覆盖（未提交）【已复现】

- **位置**：`agentcore/skills/builtin.py:101`（`register_ssh_skills(registry)`）vs `tests/test_tools.py` 注册名单与 superuser 名单均无 `"ssh_run"`（grep 零命中）。
- **影响**：`tests/test_ssh_skill.py` 只直接调 `register_ssh_skills`，删掉 builtin.py:101 全量套件不会红——与本仓刚发生的 reboot 漏接线事故同类（BACKLOG §6.4「必须锁注册与路由」的技能版）。
- **建议**：两张名单各加 `"ssh_run"`（两行改动）。

### L 级（压缩列出）

**已提交部分**（线1）：
- L1 web.py:1257-1268 日志文件轮转（`reset=true`）时客户端仍按增量追加，新旧两文件内容在 DOM 无缝混排，仅 meta 一行 ⚠；建议 `if (incremental && !d.reset)`。
- L2 web.py:1293-1294 5s 轮询无在途互斥/序号：响应 >5s 时两个增量请求共用同一游标 → 同批行追加两次。
- L3 config_write.py:153,161,241 `splitlines()` 行模型与 dotenv 不一致（U+2028/FF/NEL 等）：主代理仿真证实此类字符 splitlines 切 2 行 vs 文件迭代 1 行 → 落盘后 `verify_env` 恒失败 → 回滚 + 500（fail-safe 无损坏，但「合法输入必 500」+ docstring :100 承诺对该类字符不成立）。
- L4 BACKLOG.md:87 D2-1 回归计数过期：实测 21/20 条（文中 17/12；1aef144 自己的 commit message 都写 21）。
- L5 web.py:515 docstring 指向 README 的「不追补」口径在 README 两版均不存在（grep 零命中）。
- L6 tests/test_web.py:564-576 de09478 回归测试为源码字符串护栏（`"appendChild" in src` 等），追加/reset 分工错、重复追加等逻辑回归抓不住。

**未提交部分**（线2/线3/线4）：
- L7 reboot.py:59-69 `reboot_delay()` 不防非有限值：`float("1e400")=inf`、`max(0.0,inf)=inf`（主代理仿真）→ `asyncio.sleep(inf)` 永不重启且无告警（lifecycle.py L6 同型教训在此复发）。
- L8 web.py:1566-1573 web /reboot 第一段响应回显完整 argv（审计只记 argv[:1]，口径不一致；Bearer 门内不触红线，防运维把凭据写进 argv 的回显）。
- L9 reboot.py:46,51,54 死配置键 `AGENT_REBOOT_UNIT`（全仓无读取点）+ 注释声称 `shutil.which` 校验但 shutil 未导入（行为仍 fail-closed，注释失真）。
- L10 .env.example:44-45 缺 `AGENT_REBOOT_NOTICE_FILE`（README/AGENTS.md 都称可覆盖）与既有 `AGENT_REBOOT_ENTRY` 的登记。
- L11 tests/test_reboot.py 三缺口：shutdown 断言无 scheduler kwarg（M2 抓不住）；`_mount` 吞参致「web 重启不回执」无用例锁；`on_bot_connect(_send_reboot_done)` 注册无测试锁（删掉该行 TestRebootDoneNotice 仍全绿——漏接线事故的回执版）。
- L12 tests/test_ssh_skill.py:175-195 「密码不进日志/异常」半边无 caplog 断言（docstring:11-12 声称的四不进只锁了 argv/返回值）。
- L13 ssh_skill.py:238 `--` 置于 destination 之后跨平台行为不定（musl/BSD getopt 非置换模式可能把它留给远端 shell；两分支均无注入面，仅可用性）；tests:185 注释「防选项注入」定性不准确。
- L14 ssh_skill.py:209-213 拒绝消息回显全部已配置 alias（内网别名枚举；三层超管门内可达，可接受，记录备查）。
- L15 ssh_skill.py:69,115-121 known_hosts 默认 CWD 相对路径；若 `WORKSPACE_DIR` 配成仓根，`data/ssh` 落入 fs_write 遏制根，注入可预埋 pinned key（默认配置安全）。
- L16 runner.py:327-344 `_HOSTNAME_SAFE_FLAGS` 含 `-n/--node`，现行 util-linux hostname 无此选项（疑 dead entry，无安全面）。
- L17 runner.py:518-527 参数夹带换行不拒（继承既有全局闸口径，无执行面；可向审计日志伪造多行，建议 `_denied_for_shell` 加 `\n/\r`）。
- L18 facts.py:57 新增兜底模式 `被\s*@|有人\s*@` 加宽既有误杀面（「用户讨厌被 @」类稳定偏好被丢；保守方向，与上轮已挂账 L2 同类，建议并入权衡）。

## 2. 被证伪的发现

**线1 证伪**：csv 唤醒词回写注入新键（换行/#/引号落盘前全拒）；append 分支 `# via agent-web` 被 dotenv 当值（注释行且在键行前）；`_used_confirms` 无界（每窗口清理）；`esc()` 不转义单引号致 XSS（动态值均进双引号属性/文本位，onclick 参数仅静态键名）；`#token=` fragment 泄漏（不发服务器 + replaceState 抹除）；未提交变更越界 web.py（git status 证伪）。

**线2 证伪**：`deny()` 不抛异常致黑名单漏到明拒（acl.py:120-128 恒 finish）；回执文件新旧进程并发消费（execv 原位替换、unlink 先于解析）；删除失败无限重复回执（最多多一次，读失败「保留待下次」系有意取舍）；双触发并发写坏 tmp 拖垮重启本体（只吞 OSError + os.replace 原子）。

**线3 证伪**：`top` 的 `^-b` 前缀通配放行交互写向（:636-644 粘合参数二次拆解重校验，`-bW` 拆成 `-W` 被拒）；glued 规则粘合多开关（`-kr`）绕过（仅首字符命中 keys 才按取值放行，fail-closed）；`systemctl status foo restart` 动作词滑入（后续 token 全按 unit 名解析）；hostname `-y foo` 设 NIS 域（位置参数前置全拒）。**主代理自证伪**：ssh_skill `except TimeoutError` 对 asyncio 超时无效（requires-python >=3.11，`asyncio.TimeoutError` 即内建 `TimeoutError`，捕获成立）；「worktree + PYTHONPATH 遮蔽 editable 安装」首查误判（真因是 `-c` 模式 sys.path[0]=CWD，改在 worktree 内运行后解决）。

**线4 证伪**：README:598 行内注释污染 dotenv 值（M7 陷阱）——dotenv 1.2.3 parser 对未加引号值剥离 `#` 后注释；`register_ssh_skills` 未预检 AGENT_SSH_HOSTS 与「未配置=关闭」措辞矛盾——效果层面成立（调用全拒绝 + 非管理员 schema 不可见）。

**存疑（不判级，交后续/部署面）**：web 下载 `revokeObjectURL` 同步回收时机（无头无法验证）；after=0 尾窗含半行（窗口极小仅显示层）；C 步子进程在 `KillMode=control-group` 下是否被 systemd 收割（取决于部署）；QQ+web 同窗双触发无「重启中」闸（后果有界）；`ss -K` 对他人 socket 的真实可达面取决于 bot 进程特权（不影响 H1 的白名单不变量违反判定）；util-linux hostname 历史版本是否有过 `-n`。

## 3. 测试与文档状况

- **实跑**（主代理）：默认 `pytest -q` → **1805 passed / 48 skipped**；带 PG（`TEST_DATABASE_URL` → `qqagent_test`，与生产库 `qqagent` 隔离已核实）→ **1844 passed / 9 skipped**；`ruff check` + `ruff format --check`（0.9.6 = pyproject pin）全绿。存储双实现契约（`test_store_contract.py` 参数化）随带库套件通过——本轮范围不触 `memory/store.py`/DDL，上轮未验证面 (a) 销项。
- **变异测试**（主代理，/tmp git worktree + 未提交变更搬入后 3 处变异）：images 保留（改回无条件降级→`test_images_kept_through_tool_loop` 红）；ssh argv `--` 分隔（删除→`test_password_mode_never_in_argv` 红）；config_write 禁字符（去校验→`test_forbidden_chars_rejected` 红）。**3/3 被抓，无存活**。FIX-images 文档声称的两条变异与主代理实测一致。
- **测试真实性**：四线 + 主代理通读本轮新增用例（test_config_write 21、test_web 63、test_reboot 40+、test_ssh_skill 14、engine/facts 新回归），未发现「恒真断言」新实例；关键护栏（fail-closed/白名单 403/跨窗重放/回滚/0600 继承/CRLF 保留/漏接线三层锁）均有具体值断言。缺口见 L6/L11/L12。
- **CI 盲区**：真机 QQ 通道（回执端到端、戳一戳类）；浏览器真机 UI（线1 各 JS L 项以服务端契约+读码实证）；`ss -K` 跨用户可达面、C 步 cgroup 收割、musl 远端 `--` 行为属部署/远端面。
- **文档一致性**：README/AGENTS.md 与代码逐项对照基本一致（线2/线3/线4 各自核对通过）；例外——M4（TOFU 可见性声称失实）、M5（BACKLOG 数字）、L4（BACKLOG D2-1 计数）、L5（README 缺「不追补」口径）、L9（reboot.py 注释失真）、H1/L1 涉及的「ss 只读」表述三处需随 H1 修复同步。
- **未验证面清单与门禁结论（§8.3）**：(a) 真机 QQ 通道与浏览器真机 UI——保持未验证，无 H 挂其上，降级记录；(b) 部署/远端面存疑项（见 §2 存疑）——非 H，具备环境时补验；(c) **H1/H2 均已本地复现判定，不存在环境盲区 → 未提交部分不应当前形态提交（阻断），修复带回归后放行；已提交部分无 H，不阻断**。
- **上轮未验证面复核**：(a) PG 契约套件 → 本轮已跑全绿，**销项**；(b) 真机 QQ/浏览器 → 保持，本轮无新增可销项。

## 4. 已验证为「无问题」的关键项

- **Web 写入面**（线1 逐路由核对）：7 键白名单恰 7 键、键外 403/422 分明；值注入防御（\r\n#"' 全拒 + 归一化生成落盘文本）；fail-closed（缺 token 整面不挂、写面需 WRITE=1∧CIDR，3 个写路由整体不注册）；token 只认 Bearer + compare_digest + ASCII 预检；8 个 api 路由鉴权全覆盖；日志文件名 fullmatch 防穿越；响应体无 api_key/base_url/正文（唯一例外 /api/logs* 与 README 声明一致）；写三件套（审计含 source_ip+nonce + 两段确认 + 备份滚动）与写后验证失败还原；「7 键保存即生效」运行态同步目标逐一与 budget.py/group_context.py 实配核对；优先级 env>config.yaml>默认未破坏。
- **M1–M10 修复全部真修**（线1 核验 + 主代理抽查）：nonce 单集合判重 / group_context 单例回写 / 0600 最终态继承 / symlink 409 / ASCII 双预检 / editKey nonce / fetch+Blob / at 占位双保险 / BACKLOG D 段改写 / 审计字段（M3 留 M1 窗口期新发现，见 §1-M1）。
- **reboot 执行链**（线2）：C→B→A 逐步转移有精确断言；QQ 门禁 is_allowed→deny（恒 finish）→superuser 三态齐全；web 门禁真实强制（`_write_enabled()` 块内注册，主代理独立核实）+ 两段确认 reason 绑定/重放拒绝（上轮 M5「任意成员可确认」面不存在）；回执原子写/一次性消费/坏 JSON 丢弃/目标校验（全角数字已防）；C 步 argv 白名单裸名 + 控制字符拒绝 + 无 shell；`AGENT_REBOOT_*` 六键均不在 web 可写 7 键内（web 打穿改不了重启命令）。
- **沙箱既有不变量**（线3）：分层铁律守住（agentcore 无 nonebot import）；无 shell（两处 create_subprocess_exec）；全局四道闸先于新分支；kill/pkill/bash/python3/ssh 兜底拒；systemctl 动作类子命令与跨机三形态逐项堵死并有测试锁；journalctl/dmesg 写向/读任意文件 flag 全拒；top `-b` 粘合拆解重校验；`_GIT_NUM_RE`→`_NUM_SHORT_RE` 仅改名行为不变；curl 分支零改动。
- **ssh_run 三道白名单**（线3）：host/user/port 仅 `AGENT_SSH_HOSTS`（LLM 无字段可指定主机）；action 查表常量 + schema enum 双层；凭据仅 env、宽权限私钥按缺失处理；`sshpass -e` + SSHPASS env → 密码不进 argv/审计/异常/返回值；20s 超时 + 8KB 流式帽 + 4KB 返回帽；权限三层 + user_id 不可伪造；未进 mark_read_only（不并行，fail-closed）。
- **engine/facts**（线4）：M8 双保险完整（占位正则与 media.py 逐字节一致、只剥抽取输入）；会话作用域不变量保留；facts/摘要/tool 结果围栏调用点未被触碰；M1 tool_calls trim 契约与 `_project_history` 键卫生未动；图片贯穿 tool-loop 的成本上界（单图/单条字节预算 × max_iterations≤8）成立、图片仍不落 memory。

## 5. 与在库报告的衔接复核

| 上轮（REVIEW-26fec4d..3ce6e0a） | 本轮状态 |
|---|---|
| M1 确认码跨窗重放 | **真修**（nonce 单集合判重 + 跨窗回归测试） |
| M2 LINES/TTL 谎报 live | **真修**（单例属性回写 + 实配核对） |
| M3 atomic_write 丢 0600 | **真修（最终态）**；窗口期新发现 → 本轮 M1 |
| M4 symlink .env 被替换 | **真修**（锁内 409 + 测试验链接保留） |
| M5 非 ASCII 裸 500 | **真修**（两处 ASCII 预检 401/400） |
| M6 editKey 漏 nonce | **真修**（二段 body 补 nonce + 护栏） |
| M7 日志下载恒 401 | **真修**（fetch+Blob） |
| M8 at 占位他人属性入库 | **真修**（facts 模式 + engine 剥占位双保险） |
| M9 BACKLOG 矛盾 | **真修**（D 段改写）；新数字问题 → 本轮 M5 |
| M10 审计缺 source_ip/nonce | **真修**（字段 + 具体值断言） |
| L×14 遗留 | 未纳入本轮范围，仍在 BACKLOG 挂账；其中 L9（日志 symlink 跟随）经线1 读码复核确认仍为声明内遗留 |
| 上轮未验证面 (a) PG 套件 | **销项**（本轮实跑 1844/9 全绿） |
| 上轮未验证面 (b) 真机通道/UI | 保持未验证（无 H 挂其上） |

## 6. 修复优先级建议

- **P0（未提交部分提交前必须）**：H1 `ss` 移出 A 组/显式拒 `-K` 系 + 负例测试 + 三处文档表述同步；H2 `ssh_run` 入围栏查表（或自围栏）+ 围栏回归断言。
- **P1（随下批修，建议与 P0 同一修复周期）**：M1 atomic_write 窗口期权限（O_NOFOLLOW+0600）；M2 reboot 停机补传 scheduler + 测试断言补 kwarg；M3 C 步观察子命令退出码（或 README 明示边界）；M4 去 `LogLevel=ERROR`（或改文档）；M5 BACKLOG 数字按实测重写；M6 test_tools 两张名单补 `ssh_run`。
- **P2（挂账 BACKLOG，随相关模块顺手修）**：§1-L1–L18（其中 L1/L2/L3 同属 web 日志与回写面可一并修；L7 与 lifecycle L6 同型护栏统一处理；L11/L12 测试缺口可与 P0/P1 回归同批补）。
- 修复完成后按 §2.4 实证验收：H1/H2 复现脚本（内存仿真 + 查表断言）应翻转为「不可复现」，并全量跑默认 + PG 双套件与 ruff 双门禁。

---

*评审产物归档：本报告位于 `review/`，已录入 `review/REVIEW-WORKFLOW.md` §9 索引。修复阶段请产出 `review/FIX-3ce6e0a..de09478.md` 引用本报告编号（H1/H2/M1–M6/L1–L18）。*
