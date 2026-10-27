# agent-demo 近期 Commit 评审报告

**评审范围**：`4d5c1b8..63f9444`（33 个 commit，48 文件 +1901/-166）
**评审日期**：2026-10-27
**评审方式**：5 个并行子代理分线审查（线1 容器化部署 / 线2 沙箱收敛+runtime_env+reboot / 线3 engine 时间感知 / 线4 群统计与帮助菜单 / 线5 测试与文档）+ 主代理逐条实证（双套件实跑、14 个现场复现/变异实测、docker 实测、CI API 全 step 明细核对）
**工作区状态**：干净（`git status` 无输出；HEAD=`63f9444` == `gh/main`）

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | 无 H。两个「静默失效」类 M 需优先修：**M1 入口脚本无 shebang**（非 `init` 运行方式镜像起不来，compose 只因 `init: true` 的 tini 兜底才工作）、**M9 兜底剥离日志绕过 `AGENT_LOG_CONTENT`**（隐私开关对模型输出失效） |
| 容器化部署 | 主体成立（属权修复/gosu/PG16 客户端/镜像无密钥/compose 不变量逐条实测通过）；**5 个 M**：shebang、compose `$` 吞密码、无日志上限、`user:` 注释失实、动作技能无容器分支 |
| 沙箱收敛 | `in_container()` 先于 `which`、reboot 同判据、白名单未收窄——三条不变量实测守住；**`container_id()` 嵌套形态回归 + cgroupns=private 形态自保护整体失效且测试未覆盖**（2 个 M） |
| engine 时间感知 | 前缀/stale/摘要/裁剪链路读码正确；**3 个 M**：日志绕过隐私开关、前缀不计 token 预算（实测超预算 162%）、tz 回落告警放大 |
| 群统计与帮助 | **路由不变量在最终态成立**（`9dc74b3` 引入的免前缀 `on_message` 入口已被 `8519210` 完整撤除，HEAD 无残留）；帮助菜单确为单一数据源、命令面 21 条全覆盖；**2 个 M**：票据 IP 修复无回归钉子、CHANGELOG 漏记 |
| 测试 | 恒真断言专项排查通过（变异点逐一辨认）；**1 个 M**：`test_same_day_uses_clock_only` 午夜边界 flake——与上轮挡过发布的 M-1 **同型复发** |
| 测试数 | 默认 **3272 passed / 73 skipped**；PG 全新空库 **3358 passed / 9 skipped**（`qqagent_test`，验前确认与生产库 `qqagent` 不同） |
| Lint/类型 | ruff **0.17.1** check + format 双绿（230 文件）；pyright agentcore **0 errors**；RUN_PERF 9 passed |
| CI | HEAD `63f9444` 双分支 **success**；`c7b651c` 死于 **Format check**（本地复现 1 文件未格式化）、`062fe1f` 死于 **Review docs audit**（BACKLOG 计数漂移）——两次均为**规则性失败且已修复**，非环境抖动 |
| 声称核对 | 5 个 docker commit message 与 diff 基本相符；`794a3f1` 主题行「18 条全部做过变异复核」言过其实（body 如实只记 4 处）；`FIX-e55cab5..4d5c1b8` 表缺 L4 行 |
| 门禁（§8.3） | **无 H → 不阻断**。但 M12（午夜 flake）是上轮挡发布的同型问题复发，CI 当前绿有时运成分，**建议修复后再切 release** |

---

## 1. 已实证的问题

### M1. `scripts/docker-entrypoint.sh:1` 无 shebang——非 `init` 运行方式镜像起不来【已复现】

文件首行是注释，全文件无 `#!`；`Dockerfile:186` 用 exec-form `ENTRYPOINT ["docker-entrypoint.sh"]`（`Dockerfile:166` 的 `chmod +x` 只解决执行位）。

```dockerfile
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "bot.py"]
```

**证据**（最小镜像现场实测，`/tmp/opencode/shebang-probe`）：

```
$ docker run --rm shebang-probe:latest
exec /usr/local/bin/docker-entrypoint.sh: exec format error   # exit 255
$ docker run --rm --init shebang-probe:latest
entrypoint ran ok / CMD-ARG-OK                                 # exit 0
```

**影响**：`docker run`、k8s、swarm 等不设 `init: true` 的编排下容器 CrashLoopBackOff，交付物不可用。compose 能跑**只因为** `init: true`——tini 用 glibc `execvp` 派生子进程，ENOEXEC 时静默回退 `/bin/sh`；runc 原生 `execve` 无此兜底。README/AGENTS.md 把 `init: true` 的作用只归结为「信号转发」，其真实负载还包括「让 ENTRYPOINT 可被执行」——声称与实现不符。构建期冒烟在 builder 阶段跑 `python -c "import bot"`，**从不执行 ENTRYPOINT**，抓不到这类问题（本次漏网原因）。

**定 M 不定 H 的理由**：故障是响的（CrashLoopBackOff），不涉数据丢失/静默损坏；但它是交付物主路径的正确性错误、当前仅靠意外机制工作。

**建议**：首行加 `#!/bin/sh`（脚本仅用 `[ ]`/`for`/`stat -c`，dash 可跑）并 `git update-index --chmod=+x`；补一条秒级守卫测试断言首行 `#!`。

### M2. `docker-compose.yml:49` compose `$` 插值静默吞掉含 `$` 的密码字符【已复现】

```yaml
DATABASE_URL: postgresql://${POSTGRES_USER:-qqagent}:${POSTGRES_PASSWORD:-qqagent}@db:5432/${AGENT_DB_NAME:-qqagent}
```

注释只提醒「`@` → `%40`」，没提 `$`。实测（/tmp 最小 compose + `.env` 写 `POSTGRES_PASSWORD=p$ss@word`）：`docker compose config` 输出 `POSTGRES_PASSWORD: p@word`——`$ss` 被当成未定义变量替换为空，**无任何警告**。db 与 bot 两处被同样改写，功能自洽，但库密码与运维以为的不同，之后从宿主用原密码连会被拒。

**建议**：注释补 `$$` 转义说明（`p$$ss@word`），或 bot 侧也只从 env_file 读 db 密码、单点插值。

### M3. `agentcore/runtime_env.py:70-81` `container_id()` 解析退化 + cgroupns=private 形态自保护整体失效，测试未覆盖【已复现】

```python
for entry in cgroup_text().split():
    last = entry.rsplit("/", 1)[-1]      # 取「末段路径」
    candidate = last
    ...
    if last.startswith("docker-") or "/docker/" in entry:
        return candidate
```

**证据一（相对旧实现是回归）**——与 `4d5c1b8` 的 `action_skills._self_container_id`（用 `rsplit("/docker/",1)[1].split("/")[0]`，取 `/docker/` 后第一段）逐形态对照：

| cgroup | 旧实现 | 新实现 |
|---|---|---|
| `4:cpu:/docker/<id>` | `<id>` ✓ | `<id>` ✓ |
| `4:cpu:/docker/<id>/init` | `<id>` ✓ | **`init`** ✗ |
| `0::/docker/<id>/system.slice/agent-demo.service` | `<id>` ✓ | **`agent-demo.service`** ✗ |
| `0::/system.slice/docker-<id>.scope` | `<id>` ✓ | `<id>` ✓ |
| `0::/docker/<id>.scope` | `<id>.scope` ✗ | `<id>` ✓（本轮修对的） |

**证据二（更要紧，存量但被本轮「已锁死」的声称掩盖）**——本机 Docker 29.8.2 / cgroup v2 / systemd 驱动 / `cgroupns=private` 实测，容器内 `/proc/self/cgroup` 只有 `0::/`：

```
container_id()                                    = ''
in_container()                                    = True   （靠 /.dockerenv）
_container_is_self('agent-demo-bot-1')            = False
_container_is_self('<64 位 id 前 12 位>')          = False
```

即 `docker_ctrl`「拒操作自身容器」自保护的 **id 与名字两个通道同时失效**。`_self_container_names()` 的 `cid.startswith(parts[0])` 也因 `cid` 为空而永不命中。旧实现在 `0::/` 下同样返回 `""`，故非本轮回归；但 `794a3f1`/AGENTS.md §5 坑表声称该类失效已被测试锁死，而 `tests/test_runtime_env.py` 只覆盖 `4:cpu:/docker/<id>`、`0::/docker/<id>.scope`、`0::/user.slice/...`、空串四种形态，**没有 `0::/`**——Docker 在 cgroup v2 上的默认形态恰好不在测试集里，构成虚假信心。

**影响**：superuser + medium+ 下 LLM 可停掉 bot 自身容器，跳过优雅停机（防抖队列未 flush → 消息丢失）。**实际不可达**：当前镜像未装 docker CLI、compose 未挂 docker.sock，`docker_*` 一律返回「(系统没有 docker)」——与 M4 同一前提，故不计 H。

**建议**：`/docker/` 形态取 `/docker/` 之后第一段（恢复旧语义）再统一剥 `.scope`/`docker-`；为 `0::/` 形态补回归（喂该字符串，当前实现下用例即红）；若要在 cgroupns=private 下也认自身容器，需改走 `/.dockerenv` + `hostname`/`HOSTNAME` 映射通道。

### M4. `agentcore/skills/action_skills.py:211/220/238/262/294` 动作技能无容器分支，违反自订不变量

```python
if not which("systemctl"):
    return "(系统没有 systemctl)"
```

`ops_skills.service_status:110-111` 已按不变量修好（`in_container()` 先于 `which`），但 `service_ctrl` 与 `docker_ps/docker_logs/docker_stats/docker_ctrl` 没有。且 `run_action`/`run_readonly`（`agentcore/skills/command_util.py:20/57`）走 `asyncio.create_subprocess_exec` 直起子进程，**不经 `runner.permitted()`**，runner 层的容器检查（`runner.py:836`）覆盖不到这条路径。容器内返回的是本次要消灭的误导文案「(系统没有 systemctl)」；哪天镜像装了 systemd/docker CLI，`systemctl restart` / `docker stop` 会在容器内真实执行。`tests/test_action_skills.py` 无任何容器用例（对比 `test_runner_security.py` 有 5 条且含 which 顺序观测）。

**建议**：这些 handler 的 `which` 检查前加 `if in_container(): return host_only_hint(...)`（docker 需新 hint 文案），并补测试。

### M5. `docker-compose.yml` 两服务均无 `logging:` 限制，容器 stdout 无上限

实测 `/etc/docker/daemon.json` 无 log-opts、`docker info` 的 json-file 默认不轮转。bot 的 INFO 日志含 `[msg]`/`[reply]` 聊天正文前 200 字（AGENTS.md 已注明），容器日志会在根盘无限增长——既是磁盘风险也是隐私面扩大。`data/logs` 的 `AGENT_LOG_KEEP_DAYS` 只管挂载卷那份，管不到 stdout。

**建议**：两个服务加 `logging: {driver: json-file, options: {max-size: "10m", max-file: "5"}}`。

### M6. `scripts/docker-entrypoint.sh:10-11` 注释「文件属主本就匹配」不成立【已复现】

```sh
# 宿主机 uid 非 1000 时，compose 里加 user: "UID:GID"
# 直接以该身份启动（不走这个分支），文件属主本就匹配。
```

实测 `docker run --user 1001:1001 -v <不存在的目录>:/app/data/budget` → 宿主机侧目录被 docker 以 **root:root** 创建（`stat` uid=0 gid=0），非 root 分支跳过 chown，容器内 `touch` 被拒。即日志/账本/归档三处**静默降级**——正是本机制要消灭的故障形态。「属主本就匹配」只在宿主目录**已存在且属主正确**时成立。

**建议**：注释与 `docs/operations.md:52-53` 改为「先 `mkdir -p` 并 `chown` 到该 uid」；或在非 root 分支加挂载点可写性自检，不可写时打 ERROR（把静默降级变响）。

### M7. `README.md:165-166` 与代码矛盾：「`/reboot` 的 C/B 两步照常可用」

README 写「`/reboot` 的 C/B 两步照常可用、A 步由容器重启等价替代」，但 `7b30d06` 已把容器内 C 步**整体跳过**（`reboot.py:141-144` 与 `:189-191`），同一 docs commit 的 `docs/operations.md:128` 也正确写着「**跳过 C 步**，直接 B 步」。运维照 README 在容器里配 `AGENT_REBOOT_CMD` 会静默不生效（见 M8）。违反 AGENTS.md §2.6。

**建议**：改为「C 步（外部命令）在容器内跳过，直接走 B 步自替换；A 步由容器 `restart` 等价替代」。

### M8. `plugins/qq_agent_adapter/reboot.py:139` docstring 引用不存在的函数，承诺的「启动 WARNING」未实现

```python
（启动时打 WARNING，见 :func:`_warn_external_cmd_in_container`）。
```

全仓 grep 确认该函数**不存在**（唯一定位就在这句 docstring 里），`bot.py`/`reboot_plan`/`perform_reboot` 均无该告警。容器内配了外部重启命令时：启动零痕迹、执行时也只静默跳过 C 步——正是 AGENTS.md §5 判定为「诊断成本极高」的静默配置失效模式。部分缓解：`/reboot` 回复与 web 首段会显示 `strategy=execv`，运行时可见，但配置期不可见。

**建议**：二者之一——① 实现该告警（`reboot_plan()` 或模块加载时 `in_container() and os.getenv(CMD_ENV)` → `logger.warning`，并补用例锁「容器内配了 CMD 必有一条 WARNING」）；② 删掉文档里的承诺。

### M9. `agentcore/loop/engine.py:370` 兜底剥离的日志绕过 `AGENT_LOG_CONTENT`，隐私开关对模型输出失效【已复现】

```python
logger.info("stripped imitated time prefix from reply: %r", original[:40])
```

engine.py 内其余内容日志（`:780/:1367/:1448/:1594/:1701`）全部走 `log_text(...)`，这是唯一一条直接 `%r` 打正文的。`AGENT_LOG_CONTENT=0` 部署下回复前 40 字仍落盘：

```
$ AGENT_LOG_CONTENT=0 ...
REPLY AFTER STRIP: 用户私密内容甲甲甲甲
LOG LINE: stripped imitated time prefix from reply: '（11:20）用户私密内容甲甲甲甲'
（对照：log_text(reply, 40) → '<len=17>'）
```

且只在模型真模仿了前缀时触发——正是运维盯梢的场景。

**建议**：`logger.info("stripped imitated time prefix from reply: %s", log_text(original, 40))`。

### M10. `agentcore/loop/engine.py:1143/1168` 时间前缀不计入 token 预算，「保守估算」不变量被破坏【已复现】

`_trim_history_to_budget`（L1143）在不含前缀的消息上估算，`_project_history`（L1168）之后才拼前缀。`summary_fetch_limit: 200` 允许多达 200 条，短消息场景偏差最大：

```
200 条「在吗」级短消息、budget=3000：
kept=200 dropped=0
trim 时估算(不含前缀)=1200 tokens
投影后实际(含前缀)=3142 tokens   → 低估 1942 (162%)，实际超预算
```

**建议**：`_message_tokens` 对 user/assistant 消息加一条固定前缀津贴，或改为「先 project 到副本、再按副本 trim」。

### M11. `agentcore/tz.py:45-52` 回落分支每次调用都告警，docstring 却声称「一次性」【已复现】

无 once-guard；`_history_time_prefix`（`engine.py:352-353`）每条历史消息触发 3 次 `zoneinfo()`。`AGENT_SCHEDULER_TZ` 写错或镜像缺 tzdata 时，一轮对话刷出数百条同文 WARNING，把真正的告警埋掉。根因在 `4d5c1b8` 之前已存在，但**放大效应是本 diff 新引入的**（从 ~2 条/轮 → 3×历史条数/轮）：

```
AGENT_SCHEDULER_TZ=Not/AZone + 20 条历史 → WARNING 条数 = 60
（按 summary_fetch_limit=200 上限推算约 600 条/轮）
```

**建议**：模块级 `_warned: set[str]`，同名只告警一次。

### M12. `tests/test_engine.py:2101-2109` `test_same_day_uses_clock_only` 午夜边界 flake（00:00–01:00 本地时间必红）【已复现】

```python
ts = time.time() - 3600  # 今天一小时前
...
assert "-" not in head, "同日只给 HH:MM"
```

用真实时钟。`_history_time_prefix` 按 `tz.day_key(ts) == tz.day_key(now)` 判同日（`engine.py:352`）。套件运行在本地 00:00–01:00 时 `ts` 落前一天 → 走跨日格式 → 断言红：

```
本地 2026-10-27 00:30，ts = now-3600
day_key(now)=2026-10-27  day_key(ts)=2026-10-26
head: （此消息发送于 10-26 23:30   → has '-': True → 断言 FAIL
```

**这是上轮 M-1（词云 ISO 周奇偶，连红一整周、挡住发布）的同型复发**，且藏在本批新用例里。约 1/24 时段必红。

**建议**：从本地当天正午/午夜推导 `ts`（如 `datetime.now(sh).replace(hour=12).timestamp()`），或 monkeypatch 时间源；与 `test_older_day_uses_date_and_clock`（3 天偏移，无条件跨日，安全）保持同口径。

### M13. 票据 IP fail-closed 修复无回归钉子，FIX 声称的「既有负例仍绿」不成立【已复现】

`plugins/qq_agent_adapter/web.py:393` 改为严格相等，但注释言过其实：

```python
if item.get("ip") != source_ip:
    # 严格相等：任一侧 IP 为空也视为不匹配（fail-closed，REVIEW L2）
    return None
```

实测双侧同空仍相等**放行**（`_issue_download_ticket(name,"")` + `_consume_download_ticket(t,"")` 返回文件名）；真正收窄的只是「一空一非空」混合态。更关键的是该分支**零测试**——把这一行改回旧实现（`item.get("ip") and source_ip and item["ip"] != source_ip`），`test_download_ticket_expires_and_binds_ip` 依然全绿：

```
变异后：tests/test_web.py::TestBackupAPI::test_download_ticket_expires_and_binds_ip PASSED
```

即 `FIX-e55cab5..4d5c1b8.md` L2 行写的「既有负例仍绿」不构成对该修复的保护。违反 §6「每条修复必须带回归测试」。

**建议**：注释改为「任一侧为空而另一侧非空即拒；双侧同空仍相等」；补一条钉子「空 IP 票据从非空 IP 消费必须 None」（以及反向）。

### M14. `CHANGELOG.md` 漏记两项用户可见变更（上轮同型 M 复发）【已复现】

`1.0.3-beta` 段没有：① `/aihelp` → `/help` 改名 + 命令面统一 `/` 前缀 + 帮助菜单单一数据源补全（`8519210`、`6e5910b`）；② SUPERUSERS 空值启动即崩修复（`c7b651c`——照 `.env.example` 模板生成 `.env` 不填超管时宿主机与容器**都**启动即崩，容器表现为 restart 循环，每个新部署必踩）。README/docs/BACKLOG 均已同步，CHANGELOG 是发版产物，1.0.3-beta 的发版说明会缺这两条。上轮（REVIEW-1b16fa7..9c2d3c1）已把「CHANGELOG 缺后半批」立过 M。

**建议**：部署节补 SUPERUSERS 空值修复；命令面相关节补 `/help` 改名（注明 `aihelp`/`帮助` 别名保留、非破坏）。

---

## L 批（压缩列出；单轮 19 条 > 10，按 §3 给批量关闭计划）

| # | 位置 | 问题 | 等级 |
|---|---|---|---|
| L1 | `.env.example:545` | 帮助命令改名后漏改 env 注释，仍写「帮助/aihelp 指令」（`aihelp` 仍可用，仅口径漂移） | L |
| L2 | `tests/test_tools.py:345-353` | `test_service_status_on_host_unchanged` 走 `_UNIT_RE` 校验路径，**变异实测摘掉容器分支它仍 PASSED**——不判别其声称的「宿主机文案不变」，且与既有非法字符用例重复 | L |
| L3 | `tests/test_group_stats_route.py:309-312` | 免前缀守卫只认 `plain_stats_cmd`/`_plain_kind`/`_plain_stats_rule` 三个字面名（这三个名字确实存在过，非恒真），换个名字重挂 `on_message` 即逃逸 | L |
| L4 | `tests/test_help_menu.py:105-112` | `covered()` 在 `COMPACT_FAMILIES` 循环内 `return`，将来加入前缀互相包含的第二族会误判；当前单族无实害 | L |
| L5 | `engine.py:348-354` | `_history_time_prefix` 的 try 只包 `float()`，`inf`/`nan`/`1e300` 炸 `OverflowError`/`ValueError` 逃出整轮（两 store 当前不产此类值） | L |
| L6 | `engine.py:360-362` | 剥离正则不覆盖开头 emoji、半角括号 `(11:20)`、全角冒号（混合全角/半角数字反而会剥） | L |
| L7 | `engine.py:353` vs `:871` | 跨日前缀用 `%m-%d`（缺年份）、时间线警示用 `%Y-%m-%d`，跨年对话口径不一 | L |
| L8 | `engine.py:1114` | `generate_proactive` 的 `return content` 不过 `_strip_imitated_time_prefix`（chat 路径 1619/1690 都过），主动消息是次生风险 | L |
| L9 | `engine.py`（设计） | 用户消息里写「（此消息发送于 08:43）」会与系统前缀形成「双前缀」歧义；伪造的始终是用户自己消息的时间，不涉提权 | L |
| L10 | `Dockerfile`/entrypoint/compose | 零守卫测试——M1 这类「镜像根本起不来」的缺陷能合入，正因没有秒级静态断言 | L |
| L11 | `docker-entrypoint.sh:18-20` | 只 chown 挂载点顶层不递归；早年以 root 跑过或从宿主拷入 root 属主文件时，app 改不动 → 静默降级 | L |
| L12 | `Dockerfile:183-184` | HEALTHCHECK 只探端口 listen，`restart` 不按健康态重启 → 进程假死时显示 unhealthy 但不自愈（注释已自陈，知情取舍） | L |
| L13 | `docs/operations.md` | 容器化文档三缺口：① 容器内 `/reboot` 后改宿主 `.env` 不生效须 `compose restart`（AGENTS 为 systemd 记了同款例外）；② `AGENT_SSH_KEY_*` 宿主机路径在容器内不存在、`data/ssh/known_hosts` 未挂载；③ `stop_grace_period: 60s` 与 flush 超时的依赖是注释级约定 | L |
| L14 | `794a3f1` 主题行 | 「18 条，全部做过变异复核」言过其实——条数属实（8+5+3+2），但 body 只记 4 处变异，其余 14 条是行为锁无变异记录 | L |
| L15 | `FIX-e55cab5..4d5c1b8.md:17` | L 编号从 L3 跳到 L6，**L4 无任何处置行**（REVIEW 原文把它列为「记录在案」）。注意：`x-forwarded-proto` 分支早在 `77e21ca`（本范围之前）就有，L4 是「未记录」而非「未实现」 | L |
| L16 | `tests/test_scheduler.py:973-977` | `today_anchor()` 与 `tz.now()` 两次调用跨分钟边界时断言失败（窗口微秒级，~1e-6） | L |
| L17 | `docs/features-messaging.md:44` | 父注「群聊中同样需先 @机器人 或带唤醒词」与实现相反：`on_command` 无 `to_me`（`admin.py:163-165` 只挂 `_not_self_message`），白名单群里任何成员裸发 `/status` 即触发（ACL 仍校验群白名单，安全边界不受影响）。**既有错误**，本轮改过该 bullet 首行而未修父注 | L |
| L18 | `tests/test_bot_startup.py:62` | docstring「strip 后为空 → pop」，实现已改为置 `"[]"`，措辞漂移 | L |
| L19 | `tests/test_engine.py:737` | `assert "现在" in prompt and "周" in prompt`——「现在」由「**不是现在**」提供，把 `today_anchor` 变异成只有日期该断言仍 True；真正锁时刻的是 `test_scheduler.py:968` | L |

**批量关闭计划（§3，单轮 L > 10）**：
- **随下批顺手修（建议 10 条）**：L1、L2、L3、L10、L14、L15、L17、L18、L19、L13①——都是分钟级改动，且 L2/L3/L19 属「测试抓不住 bug」类，越早修越省后续评审成本。
- **独立成项（建议 3 条）**：L5、L8、L6——健壮性/覆盖面收窄，需各自补用例，适合并进 engine 时间感知专项。
- **显式遗留（6 条）**：L4（当前单族无实害，加第二个族时连带修）、L7 与 L9（AGENTS.md/CHANGELOG 已把格式写成不变量，改需同步文档）、L11（递归 chown 代价真实存在，不建议改）、L12（知情取舍）、L16（概率可忽略）。
- 单主题最大 5 条（容器化/engine 各 5），**无一超过 15，不触发专项整改**。

---

## 2. 被证伪的发现（防止后续评审重走弯路）

1. **「免前缀触发会让群友普通消息触发统计命令」——否（净状态）**。`9dc74b3` 确实引入过 `on_message(rule=_plain_stats_rule, priority=5, block=False)`（`git log -S` 实证三个名字的引入与删除），但 `8519210` 已完整撤除，HEAD 的 `stats_route.py` 无 `on_message`。中间态的触发判据是整条消息去语气标点后**精确相等**、私聊显式排除、`block=False` + 前置不满足静默放行给聊天响应器、`is_allowed` 在 handler 首行 + `_respond` 双查——即便瞬时存在也未漏 ACL、未扩大误触发面。
2. **「容器内 `/reboot` 后 C/B 两步照常可用」——否，README 错、代码对**（已立 M7）。`reboot_plan`（`:141-144`）与 `perform_reboot`（`:189-191`）逐字比对同以 `in_container()` 门控 C 步、`_execv_target_ok()` 门控 B/A，同判据成立；web 与 QQ 两个入口都经同一对函数，无第二判据。
3. **「整卷挂 `/app/data` 会遮掉字体」——否**。compose 逐行核对只挂 10 个运行期子目录；`.dockerignore` 的 `data/*` + `!data/fonts` negate 组合有效（镜像内 `wqy-zenhei.ttc` 在）。
4. **「Debian 自带 PG 15 会被服务端 16 拒」——否**。镜像根本不用 apt 的 postgresql-client，实测 `pg_dump/psql` 均为 16.15（PGDG bookworm 构建）。
5. **「`--frozen` 会因 lock 被 sed 而校验失败/回退公网」——否**。`uv.lock` 全部 1908 个 URL 均为 `files.pythonhosted.org`（无其它 host 形态、无 `--find-links`）；sed 只改 URL 不改 version/hash，镜像缺文件/hash 不符 → uv 直接报错 build 失败，无 fail-open。
6. **「gosu 失败会 fail-open 成 root」——否**。`exec gosu app "$@"` + `set -e`，失败即容器退出。
7. **「entrypoint 缺 shebang 是测试假象」——否**。用真实最小镜像复现同样报错，且加 `--init` 即恢复，机制清楚（tini `execvp` 的 glibc ENOEXEC→`/bin/sh` 兜底 vs runc 原生 `execve`）。
8. **「compose 的 bot 其实也起不来」——否**。`init: true` 兜底，已实测完整启动 + healthcheck healthy，不要把 M1 夸大成「compose 部署全废」。
9. **「`test_no_prefix_matcher_registered` 是恒真（断言全仓不存在的字符串）」——否**。三个名字在 `9dc74b3` 真实存在、`8519210` 删除；残留问题是防不住改名重挂，已降格 L3。
10. **「`test_container_rejection_precedes_which` 是恒真断言」——否**。`which` 返回 None 时两种顺序产出不同 reason，变异实测可抓（代码注释明确写了这个坑）。
11. **「58fa305 让 3260613 的『时间问题先查表』名存实亡 = 声称漂移」——部分成立但非缺陷**。该规则确已被「时间背景」整体替换，但这是 `58fa305` 明示的用户反馈驱动，CHANGELOG 以「三次修正」完整记录演化链、AGENTS.md 只描述终态。git 历史保留被取代的 commit 属正常。
12. **「历史前缀可闭合安全围栏」——否**。围栏只包 system prompt 里的工具结果/KB/facts/摘要；历史在 conversation messages 内、按设计不加围栏；本 diff 未改 `fence_untrusted`/`neutralize_fence_lookalikes` 的任何输入。
13. **「剥离正则会误杀正常内容 / ReDoS」——否**。无嵌套量词、`[^）]*` 线性、`^` 锚定；实测不误伤 `现在 14:30，周一。`、`（14:30-15:00）`、`（x）`、文中时间戳；剥空则原样返回。
14. **「`test_bot_startup.py` 在 CI（无 `.env`）会红」——否**。`env -i` 只留 PATH/HOME 实测 `import bot` 成功并打印 `SUPERUSERS_PARSED: []`；子进程把 `SUPERUSERS` 放**环境层**，本机 `.env`（`SUPERUSERS=2224513919`）存在时仍解析为 `[]`。
15. **「帮助图片会溢出/截断」——否**。实测最宽行右缘 717px < 844px 可用边界，6 节高度预算正确（PNG 880×1626 / 228KB，仅为观感与流量提示）。
16. **「`/aihelp` 改名打断旧习惯」——否**。`aihelp` 保留为别名，Trie 中 `/aihelp`、`/帮助`、`/help` 均在，import 期无重复前缀告警。
17. **「帮助菜单泄漏隐藏技能名/内部路径」——否**。SECTIONS 只列用户可输入命令与公开 env 名；「管理（管理员）」节对非超管可见是既有设计且有 FOOTER 标注 SUPERUSERS；隐藏技能名枚举由 `/skills` 的 superuser 分支收口。
18. **「群统计白名单与 acl 同键语义相反」——否**。`gs_config.allowed_groups` 与 `acl._load_list` 同解析、∩ ALLOWED_GROUPS fail-closed、空集=不采集，与 acl 空集语义一致。
19. **子代理结论更正（主代理实证推翻）**：线5 原报「L4 对应实现仍是 `secure=request.url.scheme == "https"`」——**错**。`x-forwarded-proto` 分支由 `77e21ca` 引入、`4d5c1b8` 时已在位，L4 是记录缺失而非实现缺失（已降格 L15）。
20. **CI 失败归因更正**：初次查询把 `062fe1f` 的失败 step 误判为 Format check；重拉全 step 明细实为 **Review docs audit**（BACKLOG 计数漂移），Format check 在该 commit 是 success。

---

## 3. 测试与文档状况

- **默认套件**（ruff 0.17.1 + pydantic 2.14 + websockets 17.3，本机实测）：**3272 passed / 73 skipped**（71.57s）。
- **PG 全新空库**（`qqagent_test`，跑前确认与 `.env` 的 `DATABASE_URL`→`qqagent` 不同）：**3358 passed / 9 skipped**（84.28s）。上轮未验证面 (a) 销项。
- **ruff 0.17.1**：`check` + `format --check` 双绿（230 文件）；**pyright agentcore**：0 errors。
- **RUN_PERF**：9 passed（`tests/test_perf.py`）。`perf_baseline.py` 未单独跑——本批触发热路径（engine 历史投影/前缀拼接），但 perf 套件本身已含合并/切分/图片缓冲基线且全绿。
- **CI**（GitHub API 双时点核对）：`63f9444` 在 `main` 与 `release/1.0.3-beta` **双 success**；`c7b651c` 失败 step = **Format check**（本地在干净树上复现「1 file would be reformatted」）；`062fe1f` 失败 step = **Review docs audit**（BACKLOG DOCS-STATS 漂移，由 `63f9444` 的 `--write-stats` 修复）。两次均**规则性失败且已修复**，按 §8.1 登记闭环。
- **恒真断言专项**：14 个测试文件 diff 逐份读完，变异点均已辨认。本次实测 3 处变异：`test_service_status_on_host_unchanged`（摘容器分支仍过，L2）、票据 IP 改回旧实现（既有负例仍过，M13）、`assert "现在" in prompt`（锚点丢时刻仍过，L19）。
- **测试环境隐式依赖（§5 / CI #91 专项）**：新测试不隐式依赖本机 `.env`（`test_bot_startup` env 层覆盖；`test_help_menu`/`test_group_stats_route` 用 `nonebot.init(_env_file=None)`）；不依赖 gitignore 的运行时产物；不依赖本机二进制（ffmpeg/docker/systemctl 全经注入点或 monkeypatch）。
- **文档一致性**：AGENTS.md「176 键 / 18 簇」实测吻合（`len(ENV_SPECS)=176`、`len(CLUSTERS)=18`、可写 114 env + 29 yaml）；BACKLOG DOCS-STATS 计数块与 `pytest --collect-only` 实测一致（3345 collected / 87 文件）；`docs/memory-and-persona.md` prompt 组装 13 项顺序与 `engine.py:522-660` 逐项吻合；README Docker 章节与 compose 逐项一致（10 个挂载子目录、5 个变量默认值）。**不一致项已全部立档**（M7/M14/L1/L13/L17）。
- **未验证面**（§8.3 门禁输入）：① 真机群内 `/memory` 全流程（无法本地复现，延续上上轮）；② `websockets 17.3`/`pydantic 2.14` 下 bot 进程长稳（运行中进程仍是旧版本模块，重启后需观察）；③ `062fe1f` 的 Review docs audit 失败细节（匿名 API 拿不到 job 日志，需 `gh` 登录；已由 `63f9444` 转绿佐证根因）；④ `docker run`（无 init）在仓库自有镜像上的现场复现——本次用等价最小镜像复现，未跑全量 build。
- **销项记录（2026-10-27 修复验收后补）**：⑤ `RUN_PERF` 的 `perf_baseline.py` 比对已跑，**无劣化**（阈值 1.5x，最大 merge_parts 1.47x）；⑥ 修复以 `fix/review-4d5c1b8-63f9444` 分支合入 main（merge `874584b`）并推送，**CI success**——此前声明的「CI 未推送」未验证面随之销项。
- **门禁结论**：该路径**无 H**，不阻断。M1–M14 均非环境盲区（全部本地复现或读码证实），按 §8.3「仅 L 级或纯观测项才可降级」不适用；其中 **M12 属上轮挡过发布的同型复发，建议修复复验后再切 release**。

---

## 4. 已验证为「无问题」的关键项

| 项 | 验证方式 |
|---|---|
| `in_container()` 先于 `shutil.which`（不变量） | 读码 `runner.py:836`；变异实测（对调二者、`which→None`）→ `test_container_rejection_precedes_which` 失败 |
| `reboot_plan` 与 `perform_reboot` 同判据 | 逐字比对 `reboot.py:141-150` vs `:189-191`；变异实测（只摘 perform 分支）→ `test_container_perform_reboot_never_spawns` 失败 |
| `.scope` 剥离 / dockerenv 兜底 / 文案自解释 | 19 种 cgroup 形态脚本实测；变异实测（去 `.scope` 剥离 → 2 条红；去 dockerenv 兜底 → 红） |
| 沙箱白名单未被收窄 | `systemctl` 只读子命令/拒 `--host/-M`、`ss` 拒 `-K/--kill/-D`、`journalctl` 拒 `--file/--vacuum-*`、`dmesg` 拒 `-C/-c/-w`、`top` 必须 `-b`——逐条未动；`test_runner_security.py` 107 条全绿 |
| `ts` 来源与时区口径（双实现一致） | InMemory 写 `time.time()`；PG 取 `created_at TIMESTAMPTZ → .timestamp()`；`fmt_ts`/`day_key` 统一过 `tz.zoneinfo()` |
| `ts` 不进最终 messages | `_project_history` 重建 dict 只留 role/content(+tool_calls/tool_call_id)；store 契约测试参数化锁死 |
| 裁剪后前缀与消息不错位 | `_trim_history_to_budget` 返回 `history[cut:]` 切片；`_drop_empty_turns` 保序；两个调用点顺序均正确 |
| `history_stale_after=0` 关闭语义 / `summary_enabled:false` 旧路径两者皆无 | `_cfg_int(minimum=0)` + `<=0` 早退；实测旧路径历史无前缀、无时间线 |
| 摘要不被前缀污染、仍过围栏 | `_maybe_roll_summary` 在 project 之前取数（`:1148`）；`summary_block` 仍走 `fence_untrusted`（`:987`） |
| 帮助菜单确为单一数据源 | 全仓仅 `help_menu.py` 定义 `SECTIONS/FOOTER`；`help_render.py:14-15` import 同一对象（`is` 断言锁定）；AST 复算适配层 21 个 `on_command` 全部被菜单覆盖（0 缺失） |
| 新增帮助守卫「改坏必红」 | 副本目录变异：删 `/词云` 行 → 2 条红；主名改回 `aihelp` → 2 条红；`help_render` 另写一份 `_SECTIONS` → identity 断言红 |
| tokenize M-4/M-5 修复有效 | `cut("IT US CAN 运维")` → `['IT','US','CAN','运维']`；清空 `word_tag_tab` 后有守卫降级 `jieba.lcut` 保留内容词 |
| 属权修复 + gosu 降权端到端 | 真实镜像实测 root 启动 + root 属主挂载点 → chown 1000 → app 写入成功；`docker top` 确认降权后进程；gosu 缺失时 `set -e` 退出（fail-closed） |
| 镜像无密钥 / web 默认最小暴露 | `ls -a /app` 无 `.env`/`.git`；`.dockerignore` negate 生效；`AGENT_WEB_TOKEN` 空时整个写面不挂载（fail-closed） |
| bot.py SUPERUSERS 归一化 | `""`/`" "`/`"[]"` 三态读码正确，另有 3 条子进程测试锁死；`"null"` 按字面超管名处理（既有语义，不崩） |
| 分层铁律 | `runtime_env.py` 纯 stdlib 无 nonebot；`agentcore` 内部引用，`plugins/reboot.py` 引用符合分层 |
| 思考步数热更新链路 | `config_yaml.apply_runtime` 写 `engine.max_iterations` 并回落 `boot`；`engine.py:472` 用 `_cfg_int` 保证 int；新用例还原 call site 即 8≠20 变红 |

---

## 5. 与在库报告的衔接复核

| 上轮项 | 状态 |
|---|---|
| REVIEW-e55cab5..4d5c1b8 **M-1**（词云 ISO 周奇偶，main CI 红、挡发布） | **真修**。今日 2026-10-27 是 ISO 周 44（偶数 = general 周），`test_nudge.py` 193 passed 证明桩生效 |
| **M-2**（收尾 LLM 调用绕过当日预算硬闸） | **真修**。`budget.calls == 9` / `len(llm.calls) == 8` 是精确计数锁；变异（去掉收尾前判闸）红 |
| **M-3**（设置总览「思考步数」显示启动期旧值且归 boot 区） | **真修**。`build_settings` 读 `engine.max_iterations` 运行值 + `max_iterations_effective="live"`；JS 移入「即时生效」区 |
| **M-4**（英文大写缩写被停用词误杀） | **真修**。`_acceptable` 全大写跳过小写匹配；变异（去掉 `isupper()` 豁免）红 |
| **M-5**（posseg 词性表重建窗口丢词） | **真修**。`_pos_table_ready()` 降级纯分词；实测清空 `word_tag_tab` 后仍保留内容词 |
| **M-6**（ruff pin 三方无守卫） | **真修**。`tests/test_toolchain_pins.py` 三条秒级断言 |
| **L 批** | 17/19 条有对应行；**L4 无处置行**（L15）；L5 实为随 M-2 顺带修了但只记在 M-2 行内 |
| 未验证面 ① 真机群内 `/memory` | 延续，无法本地复现 |
| 未验证面 ② websockets/pydantic 长稳 | 延续（运行中进程仍是旧版本模块） |
| 未验证面 ③ ruff 0.17.1 CI 最终结论 | **销项**——HEAD 双分支 success |
| 未验证面 (a) PG 套件 | **销项**——本轮实测 3358 passed / 9 skipped |
| AGENTS.md §4「容器化」行三条（which 之前 / 跳过 C 步 / 同判据） | 与代码逐条相符；README:166 才是矛盾点（M7） |
| AGENTS.md §4「历史预算」行 | 描述终态准确；但 **M10 表明「保守估算」在含前缀后已不成立**——不变量本身被本轮 diff 破坏，需修代码或改文档 |
| AGENTS.md §5「cgroup 解析漏剥 `.scope`」坑表 | `.scope` 形态确已修；但该坑表声称的自保护失效类在 **cgroupns=private 下仍存在**（M3 证据二），坑表的「回归用例必须逐个形态喂字符串」要求本身未被满足 |

---

## 6. 修复优先级建议

**合并/发布前必修（3 条）**：

1. **M12** 午夜 flake——上轮同型问题挡过发布，当前 CI 绿有时运成分；改法一行（从本地当天正午推导 `ts`）。
2. **M9** 日志绕过 `AGENT_LOG_CONTENT`——隐私开关失效，改法一行（`log_text`）；这是 AGENTS.md §4「日志正文开关」不变量的直接违反。
3. **M1** entrypoint 加 shebang——交付物主路径靠意外机制工作；改法一行 + 一条守卫测试。

**本版本内修（6 条）**：M2（compose `$` 注释）、M3（`container_id()` 嵌套形态 + `0::/` 回归用例）、M4（动作技能容器分支）、M6（`user:` 注释/自检）、M7+M8（README 改正 + 实现启动告警或删承诺）、M13（票据 IP 补钉子）。

**随下批（5 条）**：M5（logging 上限）、M10（前缀计入预算）、M11（告警 once-guard）、M14（CHANGELOG 补两条）、L 批「顺手修」10 条。

**修复回归要求**：每条必须带回归测试，对抗样例直接从本报告复现脚本搬进测试；M1/M3/M12/M13 的复现脚本已在 `/tmp/opencode`（shebang-probe / verify_old_vs_new.py / probe_ticket.py），可原样搬入。修复后按 §2.4 做验收（复现脚本应全部由「可复现」变「不可复现」），并全量跑 pytest + ruff + pyright。
