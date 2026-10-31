# agent-demo PR #6 评审报告
**评审范围**：PR #6（Daiyimo，14 commits）`5751f73..3c00145`（已按用户决议以覆盖式 merge 合入 `UGREEN` 分支，`d79a467`，树与 PR head 完全一致）
**评审日期**：2026-10-29
**评审方式**：5 线并行子代理分线审查（配置源/人格与调度/LLM provider/Docker 部署与 CI/测试质量与文档声称）+ 主代理逐条实证（双套件实跑、6 组亲自复现、探针与 check-ignore 实测、CI API 核对）
**工作区状态**：主仓 `/home/xiaji/code/agent-demo` 有 4 个用户未提交修改（`agentcore/skills/reminder_skills.py`、`docs/features-community.md`、`plugins/qq_agent_adapter/__init__.py`、`tests/test_tools.py`），本轮评审未触碰任何受版本控制文件；全部审查与实跑在干净 worktree `/home/xiaji/code/agent-demo-ugreen`（HEAD=3c00145）进行。新增产物仅本报告与 §9 索引行。

## 0. 结论摘要

| 维度 | 结论 |
|---|---|
| 最高风险 | **3H**：H1 人格/定时任务配置落 LLM 沙箱可写区（绕过 web 2FA/审计/备份契约，注入可持久化）；H2 真实 QQ 群号/号主入库与文档；H3 `data/state|mirror|ssh` 不被 gitignore（2FA secret/搜索 key/全库 pg_dump 明文 `git add .` 即入库） |
| 主题结论 | 配置源四级优先级链/env_snapshot 防线/容器属权/出站铁律等既有不变量**全部保留且多数有新测试钉住**；问题集中在：**新开的攻击面（配置进沙箱 + 状态文件出忽略范围）**、**升级路径断链（legacy provider 迁移/宿主机 .env 部署）**、**两套自举并存漂移（deploy.py vs compose_init）**、**文档承诺 vs 实现（ACL/不带历史/计数块"实测"）** |
| 测试实跑 | 默认套件 **3513 passed / 73 skipped / 0 failed**（3586 收集，86s）；PG 全量（全新空库 `qqagent_test` 重建后）**3599 passed / 9 skipped / 0 failed**；ruff check + format 双绿（239 文件）；pyright agentcore **0 errors**；CI（UGREEN head `d79a467`）**success**（run 40730758243，匿名 API 核对） |
| 声称核对 | 14 个 commit 声称**绝大多数兑现**（含 README 拆分去向逐段核实、P1-2/P1-3/P1-5 逐项对上）；失真 3 处：BACKLOG 计数块两次把推算值标「实测」（M20）、FIX 文档「pyright 不适用：未触碰 agentcore」与终态不符（M6）、agent_turn「不带历史」与实现不符（M2） |
| 门禁（§8.3） | **3H 阻断合入 main**。H2/H3 修复成本一行级；H1 需架构定夺。另见「合并决议专项」：UGREEN 覆盖式 merge 已丢 main 三修复（arm64/deploy.py/drift 告警），并回 main 时必须重解 |

**合并决议专项（非 PR 缺陷，用户已知）**：UGREEN=`d79a467` 的树完全取 PR 侧，main 在基线后的三个提交内容在树层面丢失——`2df3641`（libpq multiarch glob + 静态守卫 + docker-arm64.yml，PR 版 `Dockerfile:146` 仍写死 `x86_64-linux-gnu`，贡献者在 `docker-publish.yml:48-50` 注释自认 P1-1 并收敛 `platforms: linux/amd64` 规避）、`cf5677c`（`deploy.py docker` 自举，PR 版 deploy.py 为基线旧版，另起 `compose_init.py` JSON 路线，见 M15）、`38624fe`（LLM 配置漂移启动告警 + `test_llm_providers.py` 84 行，见 M8/M9）。`git merge-tree` 实测 `Dockerfile`/`AGENTS.md`/`.env.example`/`agentcore/llm/providers.py` 等 7 文件 changed-in-both：**并回 main 时必须按「保留 main 修复 + 采纳 PR 重构」逐文件解冲突，不能沿用覆盖**。

## 1. 已实证的问题

### H1 人格与定时任务配置落进 LLM 沙箱可写区，web 面 2FA/审计/备份契约被 fs_write 通道绕过【推演（攻击链各环节读码逐一复现）】
- 落点：`agentcore/personas/manager.py:32`（`DEFAULT_PERSONAS_DIR = data/workspace`）、`agentcore/scheduler/jobs.py:59`（`data/workspace/cron`）与 `agentcore/workspace/utils.py:17`（LLM 沙箱根 `WORKSPACE_DIR=data/workspace`）**同根**。
- 写边界：`agentcore/workspace/fs.py:39-52` `_assert_writable` 只挡 `.git/.gitattributes/.gitmodules`，`SOUL.md`、`personas/*/SOUL.md`、`cron/jobs.json` 均可写（主代理已亲验源码）。
- 权限档：`agentcore/skills/workspace_skills.py:161-190` fs_write 为 superuser+medium、**无确认码**（fs_delete 有）。PR 之前沙箱内无任何安全敏感文件；现在 superuser 会话中逃过围栏的注入（引用/网页/检索结果均为不可信内容，历史轮次真实发生过围栏绕过类 H）可：① 覆盖 `SOUL.md` 持久化篡改此后**所有会话**的 system prompt；② 写 `cron/jobs.json` 植入无人值守 agent_turn，叠加 M1（无 ACL）向任意 `group:/private:` 定时投递。人格改写的正式契约（web 面 2FA+审计+改前备份）此路径三者皆无。
- 建议：`SOUL.md`/`personas/`/`cron/` 移出沙箱根（`data/personas/`、`data/cron/`），或 `_assert_writable` 增设配置保护区（同 git 元数据待遇）。
- 次生面（线4-M7 同源）：`.gitignore:96-101` 放行后，LLM 可写内容从此可入库（`git add .` 收进 LLM 生成的人格与 job）。

### H2 真实 QQ 群号、QQ 号与贡献者机器路径入库并在文档示例中【已复现】
- `data/workspace/cron/jobs.json:19-21`：`group:10816466**`、`private:815833**`（真实群号/QQ 号，已在上库时打码，全文形态见 PR 原始提交）、`sh /root/.picoclaw/...`；`docs/features-community.md:212-214` 示例同（主代理 grep 亲验）。仓库公开于 GitHub，代码搜索可直接检索；`/root/.picoclaw/` 暴露部署拓扑。
- 建议：样例全部改占位符（`group:000000` 等）；`tests/test_scheduler_jobs.py::TestRepoJobsFile` 锁了样例形态，替换时同步改断言。

### H3 `.gitignore` 漏 `data/state/`、`data/mirror/`、`data/ssh/`，持密状态文件落未忽略路径【已复现】
- 主代理 `git check-ignore` 实测：`data/state/2fa-secret.json`、`data/state/search-providers.json`、`data/state/nudge-hotspot.json`、`data/mirror/2026.sql`、`data/ssh/known_hosts` **全部 NOT-IGNORED**。
- 链条：`scripts/compose_init.py:64-72` `STATE_REDIRECTS` 把 7 个状态文件（含 **TOTP 2FA secret**、**搜索源 API key**、nudge 目标 QQ）重定向到 `data/state/`，仅 `.gitignore:74` 跟了一条 `data/state/llm-providers.json`；`docs/deploy-nas.md:294` 明文指导 `AGENT_BACKUP_MIRROR_DIR=data/mirror`（每日 pg_dump 明文=全库聊天记录）。NAS 部署目录里 `git add .` 即入库。新守卫 `test_real_tree_secret_paths_ignored` 只锁 `config/agent.json`/`config/agent.env` 两个路径。
- 建议：`.gitignore` 追加三个整目录 + 守卫清单扩展；`data/reboot-notice.json`/`data/push-state.json` 的 data/ 根路径顺手补。

### M 级（22 条，按主题分组）

**升级路径与迁移**
- **M7 legacy provider 迁移对全部存量用户断链**【已复现】：`providers.py:116-122` `legacy_path()` 只认 env `AGENT_LLM_PROVIDERS_FILE` 显式值（`_LEGACY_DEFAULT` 是死常量）；存量裸机用户旧文件在默认 `data/llm-providers.json`、从不设 env → 升级后迁移分支永不可达，web 配的 provider（含 key）静默失效回落 `.env` 种子，无日志线索。主代理复现脚本：场景 A（env 未设）旧文件被无视、`agent.json` 未生成；场景 B（env 显式）迁移正常——分支本身能工作，默认路径断。
- **M8 LLM 配置漂移告警回退且漂移面扩成三层**【推演+merge 事实】：PR 重写 `ensure_migrated/load/save` 无任何 drift 检测；main `38624fe` 的告警在覆盖 merge 中丢失。新形态 `config/agent.env` > `agent.json` 标量键（`config_json.py:141-163` 注入 `LLM_*`）> `agent.json` 的 `LLM_PROVIDERS` 数组三层互不告警；`compose_init` 生成的 MINIMAL_CONFIG 同时含明文 `LLM_API_KEY`（`:113`）与数组占位，双 key 各管各的路径。
- **M14 宿主机 `.env` 老部署升级后 web 写面整体 500**【推演】：`config_write.py:177-178` 对不存在的 `config/agent.env` fail-closed → 114 键/`/model` 全 `env_file_missing`；宿主机升级路径零文档。

**新调度器（agent_turn）**
- **M1 投递无 ACL，文档承诺是假的**【已复现】：`agent_turn.py` 全文无 `is_allowed/allowed_groups`（grep 零命中）；对照 `push.py:498,550` 有 `_target_allowed` 且对白名单外群主动 disable。`docs/features-community.md:236` 与 `jobs.json:5` 白纸黑字承诺「不在白名单不投递」。
- **M2 「不带历史」声称与实现不符**【已复现（会话键构造）】：`agent_turn.py:98-104` 伪造 context → `engine.run` → `store.py:600-607` `resolve_session` 生成 `p:<目标QQ>`（真实用户私聊会话键）/`g:<gid>:<gid>` 孤儿键；cron message 写进该用户历史、成本记 `private:<QQ>` 路由、`growth.maybe_trigger` 按伪造 user_id 触发。
- **M4 jobs.json 既是 git 配置又是运行时状态**【已复现】：`update_state`→`save()` 全量重写（state 字段+全体 `updatedAtMs`），且 `test_repo_jobs_file_is_tracked` 把入库锁成守卫 → 每执行一次 job 被跟踪文件变一次，工作区永脏。
- **M5 `enabled` 脏值=开**【已复现】：`jobs.py:154-158` 内联反向集合，`"disabled"/"nope"`→True，违反「布尔 env 统一走 boolenv（认不出告警取默认）」纪律；测试只测 4 个合法值，变异不红。
- M3（`.gitignore` 白名单过宽：`cron/backups/*.bak`、`personas/` 任意运行内容入库）【已复现 check-ignore】——与 H1/H3 同源，随二者收敛。

**容器与自举**
- **M15 两套自举漂移**【推演（merge-tree+两侧测试逻辑）】：PR 版 compose 默认不喂 `.env`（`env_file` 注释掉），bot 改吃 `config/agent.json`；main 版 `deploy.py docker` 生成 `.env`（`test_deploy.py:296-311` 断言 10 目录=挂载表）。合并后 `test_data_dirs_match_compose_mounts` 必红（13 vs 10）；deploy.py「生成 .env 就能起」承诺失效。需架构定夺：两套收敛为单一事实源。
- **M12 compose_init 生成 `agent.json`/`agent.env` 无 0600**【推演（源码无 chmod 已核实）】：`compose_init.py:205-218` `write_text` 默认权限；该文件是全部凭据承载（LLM key/token/SSH）。对照 `deploy.py:323` 生成 .env 后 `chmod 0600`。`atomic_write` 继承源 mode（0644 源永远 0644）。
- **M16 bootstrap 有 fatal 分支可致容器崩溃循环**【推演】：`docker-entrypoint.sh:41` 在 `set -eu` 下直跑 `compose_init.py --bootstrap`；`compose_init.py:163-167` 非空目录陷阱 `SystemExit`、非 root+root 属主挂载 `PermissionError` 均未捕获——与「不可写是降级不是致命」的自举哲学矛盾（现有测试只覆盖模板缺失兜底）。
- **M17 docs「Docker UI 兼容版」compose 不挂 `./config`**【已复现（静态）】：配置落容器可写层，recreate 即丢（正是 PR 自己修的 P1-3 复发）；且含 `./config.yaml` 单文件挂载——PR 刚在 AGENTS.md 立的禁令④。`TestConfigMountIsDirectoryOnly` 只锁主 compose 锁不到文档内嵌块。
- **M18 backup_usb.sh 根级 config.yaml 硬编码**【推演】：bootstrap-only 部署宿主目录无根级 config.yaml → rsync 缺源 + `set -e` → USB 冷备必失败；3965579 只修了 .env/agent.json 二选一，没修 config.yaml 两个可能位置。
- **M19 个人 GHCR 命名空间写死 + `:latest` 可变 tag**【已复现（静态）】：`docs/deploy-nas/docker-compose.yml:54` `ghcr.io/daiyimo/agent-demo:latest`、`deploy-nas.md:235` curl Daiyimo/main；workflow_dispatch 任意分支手动触发即覆盖 `latest`——供应链信任转移，主仓 CI 不保证该 tag 内容。
- M6（FIX-ugreen-docker.md「pyright 不适用：未触碰 agentcore」与终态失真，后续 commit 大改 agentcore 且无补验记录）【已复现】。

**配置源健壮性（config_json）**
- **M10 GBK 编码 agent.json 炸启动**【已复现探针】：`config_json.py:107-115` 只捕 `OSError`；`UnicodeDecodeError` 是 `ValueError` 子类（`issubclass` 实测 False）→ `load()` 抛 → bot 启动崩溃，违反「读不了不抛但响」核心声称。中文用户 + Windows 记事本 ANSI 另存是高频路径；现有用例只测语法坏没测编码坏。
- **M11 JSON 非法键名炸启动**【已复现探针】：`config_json.py:155` `env[key] = text` 无键名校验；`os.environ["A=B"]`→`ValueError`、`os.environ[""]`→`OSError(22)`（探针实测）。JSON 对象键可任意写（笔误 `"LLM_API_KEY ="`）。测试传自定义 dict 不触发，只有生产 `os.environ` 路径触发——测试全绿掩盖。
- M13 纯 `.env` 部署每次启动 5 键 ERROR 误报【推演（时序读码）】：missing_required 在 load_dotenv 之前判定 + `ONEBOT_ONEV11_ACCESS_TOKEN`/`SUPERUSERS` 合法留空也算缺——「必须响」的信号被狼来了稀释。

**测试与文档**
- **M9 池条目凭据保留断言被删**【已复现 diff】：`test_migrates_main_pool_and_fallback` 的 `providers[1].base_url/api_key` 精确断言改为集合包含；实现未变，删除无理由——改坏 `entry.api_key or ...`（providers.py:236-241）现测试不红，`LLM_MODELS` 池条目凭据静默丢失。叠加 main 84 行测试在覆盖 merge 中丢失。
- **M20 BACKLOG 计数块两次把推算值标「`pytest -q` 实测」**【已复现算术】：`f76cadf2` commit body 自述「21 failed/3486 passed/79 skipped」却写「3507 通过」（3486+21，把失败拼进通过）；`d9e084d`「3377 通过」同为推算且与前一个 commit「本机无法生成」自相矛盾。终值已由 CI 校正（见 §3），但「实测」标注两次承载推算值。
- M21 MOUNTS 守卫漂移【已复现】：entrypoint MOUNTS 扩到 13 目录，`test_mount_list_covers_all_runtime_dirs` 仍断言旧 10 个——`state/mirror/ssh`（恰是 P1-3 修的持久化面）删任何一个测试不红。
- M22 README deploy.py 指路失实【已复现】：指向 operations.md 的「容器化部署」节，但子命令表与三条硬边界（不自动启动/不写仓库外/unit 零密钥）被删后未搬进任何文档。

### L 级（24 条，压缩列出；批量关闭计划见 §6）
线1：L1 `apply_env_file` 全量应用白名单外键（与 .env 同权，不升 H，记档）；L2 missing 报告先于 `apply_env_file`；L3 `ALLOWED_GROUPS` 写 JSON 数组→全群静默无响应零告警（`SUPERUSERS` 有 `[` 特判它没有）；L4 fixture teardown pop 列表缺一项。
线2：L1 personas 目录 CWD 相对（非仓库根启动静默无人格）；L2 `delete()` 读 marker 无 OSError 保护（同文件口径不一）；L3 sent 后 update_state 前崩溃→重启重投（窗口极小）；L4 tick 串行执行到期 job（慢 job 阻塞同轮）；L5 `_next_run_ms` 解析失败=0=立即执行（手改坏即每 60s 一次 LLM）；L6 样例 message 是 picoclaw 语义（`sh` 不在白名单、`message` 工具不存在，照抄白烧 token）；L7 `update_state` 刷新全体 `updatedAtMs`。
线3：L1 `max_tokens` 负例钉子被删（1~10⁶ 边界无测试）；L2 `ensure_migrated` 语义从「一次性迁移落盘」变「env 常驻回落」（自洽但 AGENTS 不变量表述需改写）。
线4：L1 「14 个目录」五处 vs 实测 13；L2 `.dockerignore` `!.env.example` 成矛盾死代码；L3 entrypoint 在 gosu 分支内联复制整个函数体（两份实现必漂移）；L4 deploy-nas.md 写死作者家庭内网拓扑（`192.168.110.185`×6、HA 实体名）；L5 主 compose 挂载完整性无直接守卫（deploy-nas 版罩不住主 compose）。
线5：L1 AGENTS.md「README 156 行」vs 实测 168；L2 README:110 `**` 未闭合；L3 `test_warn_once_dedups` 恒真（变异删去重逻辑仍绿；tick 版用例有效，本条自身无效）；L4 `test_compose_init.py:62` `if False` 死代码；L6 `unignored_secrets` 探测器不支持目录/negate 形态（漏报方向）；L7 `_broken_json_fences` 正则漏检空格变体。

## 2. 被证伪的发现（防误导后续评审）
1. **「PR 删了 arm64 修复/docker-arm64.yml/deploy.py」——证伪**：基线 5751f73 的 Dockerfile 本就写死 x86_64、workflows 只有 ci.yml、deploy.py 原样保留。防线的**全部四件套**（glob/静态守卫/workflow/AGENTS 不变量）都是 main 在基线之后的 `2df3641` 引入，PR 只是分支切点早于它；贡献者还在 `docker-publish.yml` 注释里自认 P1-1 并收敛 amd64 规避。真正风险在**并回 main 时的冲突解法**（见 §0 合并决议专项）。
2. **「fs_write 在 low 档可用（普通用户可改人格）」——证伪**：注册在 `at_least("medium")` 分支内 + `permission="superuser"` 双层，可达者仅 superuser；H1 定性为「注入持久化+契约绕过」而非低权限直改。
3. **「run_command 可写 jobs.json」——证伪**：runner 白名单纯只读集，无解释器。
4. **「key 进冷备=新增泄漏面」——证伪**：基线 `backup_usb.sh:115` 本就明文拷 llm-providers.json，等价搬迁。
5. **「`load()` 抛 FileNotFoundError 是契约破坏」——证伪**：两个调用方均按回落处理，语义与基线一致，有钉子。
6. **「坏 jobs.json 丢任务/崩启动」——证伪**：`JobsError`→tick 告警跳过；save 失败回滚备份不删文件。
7. **「README 重写丢失安全警示」——基本证伪**：逐段比对，「裸 nohup」「容器内改 .env 需 restart」「勿分享截图」「整卷挂遮字体」「测试换库名」全部有去处（development.md/operations.md/主 compose 注释）；唯一实质丢失是 M22。
8. **「AGENTS.md 删了不变量」——证伪**：零删除，反而新增「定时任务」不变量行与容器化第④条。
9. **「8b47e7b annotations 改动有运行时风险」——证伪**：全仓 grep `get_type_hints|__annotations__|ForwardRef` 零命中；`inspect.signature` 两处默认 `eval_str=False`。「安全」声称成立。
10. **「dotenv 引号处理写读分裂」——证伪**：写端拒引号、读端兼容剥引号，双保险同向。

## 3. 测试与文档状况
- **实跑**（干净 worktree，主仓 `.venv`，Linux）：默认 3513+73/0 failed；PG 全量（`qqagent_test` DROP/CREATE 重建为 CI 等价空库后）3599+9/0 failed；ruff check/format 双绿（239 文件，pin 版本）；pyright 0 errors（worktree 需链 `.venv`，首轮 50 errors 为环境解析非代码问题）。
- **CI**：UGREEN head `d79a467` run 40730758243 **success**（匿名 API）。`d79a467^{tree} == 3c00145^{tree}`（`0d344003`），故 CI 的 Review docs audit（`--check-stats` 精确相等）在同内容上通过 ⇒ BACKLOG 计数块 3586/3513/73 与 CI 实测精确相等——主代理本地实跑数字与之一致。
- **BACKLOG 计数块**：终值正确，但过程两次失真（M20）。
- **文档一致性**：配置优先级链三处文档口径一致且有测试双锁；`.env.example`↔`ENV_SPECS` 静态 comm 对比零差异；conftest 改动是**显式** `AGENT_CONFIG_JSON` 隔离（防写真实配置，方向正确），无 CI #91 型隐式依赖。
- **未验证面（§8.3 清单）**：(a) 真机绿联 NAS Docker UI/部署端到端（本环境无法起目标 NAS）；(b) SnowLuma 协议端真机行为（仅文档沉淀，未验证）；(c) 贡献者 Windows/WSL 本地基线自述（85 个预存失败修复、13 条先红后绿等，无法复现；其中 a7e221b 声称修掉的恒真断言在 HEAD 仍以 L3 形态存在，该轮变异复核完整性打折）；(d) UGREEN→main 实际合并冲突解法的最终测试态（M15 推演，未实跑合并）。以上均不涉及 H 级路径。

## 4. 已验证为「无问题」的关键项
- **2026-10-12 /reboot 防线未破**：env_snapshot `before_dotenv` 钩子时序正确，JSON 派生值不进出生快照，`apply_env_file`/SUPERUSERS 规范化均在 capture 之后——改 JSON 重启生效、不改不受旧值污染（读码全路径 + `test_never_overrides_real_env`）。
- **四级优先级链真实实现**且四个关键性质各有测试钉住（不覆盖真实 env/JSON 先于 dotenv/web 后应用/受保护键）。
- **历轮 providers.py 安全防线完整保留并加固**：0600 原子写+备份 0600+symlink fail-closed+写后校验回滚+tmp `.` 前缀 `O_NOFOLLOW`；外带链 409、SSRF 半盲、回显面（public 仅 host+key_set）逐项在位。
- **web 人格面契约完整保留且加强**：两段确认 HMAC 升级绑定 `SOUL.md+ABOUT.txt`（顺带堵了改描述绕确认的口子）、备份 0600 滚动+锁、TOTP、审计；`default` 拒删判据修正为同源。
- **容器属权修复保留并扩展**（chown MOUNTS/TOPDIRS/FILES + 降权后自检 + 测试锁）；`stop_grace_period 60s ≥ flush 30s` 两份 compose 均在；db 端口回环绑定守卫在；compose 无硬编码凭据（`Yll@` 断言仍在）。
- **出站铁律**：`send_once` uncertain 不重发有钉子（`test_uncertain_is_not_failure`）；throttle 与 matcher 共用限流器。
- **GHCR workflow 权限最小化**：contents:read + packages:write、仅内置 GITHUB_TOKEN、push 触发仅 main、无 secret echo。
- **测试面整体质量高**：test_scheduler_jobs（567 行）负例 14 条+仓库真实文件防写+样例默认停用守卫；test_compose_init 负例覆盖三种目标态；test_web 两处真实加强（修了预存的 `.find()>0` 恒假与 mtime 夹具）；`3965579` 的 USB 断言修正为真加强非改弱。

## 5. 与在库报告的衔接复核
- 上轮（REVIEW-4d5c1b8..63f9444 及其 FIX）的全部修复在 PR 基线内且未被回退（抽样：entrypoint 属权、healthcheck、logging 上限、M10 历史前缀津贴、票据负例——由五线各自核对）。
- `review/FIX-ugreen-docker.md` 已按规范入 §9.1 索引并如实标注仓外来源；其内容失真部分见 M6。
- main 在基线后的三个提交（arm64/bootstrap/drift）不在 PR 内：非 PR 缺陷（证伪 §2.1），但**并回 main 时若沿用覆盖解法即倒退**——本报告将其列为合并决议专项（§0）。

## 6. 修复优先级建议
1. **合并前必改（阻断项）**：H2（样例占位符化，分钟级）→ H3（gitignore 三行+守卫清单扩展）→ M1（agent_turn 补 `_target_allowed` 同款白名单，或先删文档承诺）→ M12（compose_init 写后 chmod 0600）。
2. **合并前定夺（架构决议）**：H1（配置移出沙箱 vs fs 写保护名单）；M15（deploy.py 与 compose_init 收敛为单一自举事实源）；M8（drift 告警移植到三层结构，或显式记录「接受回退」）；AGENTS.md 容器化行/Dockerfile 的冲突解法（保 main multiarch ④ + 追加配置目录挂载条目，两侧内容不互斥）。
3. **合并后首批**：M7（legacy 默认路径回落双探测）、M10/M11（异常面统一捕 + 键名校验）、M2（会话隔离 `cron:<job_id>`）、M4（state 拆 sidecar）、M5（boolenv）、M14（升级文档或自动建 0600 空文件）、M9/M21（恢复删掉的断言/补 13 目录）、M16-M19。
4. **L 批量关闭计划（24 条 > 15，触发专项）**：随 H/M 修复同文件随修约 12 条（L3-线1/L5-线2/M12 连带 L1-线4 等）；独立小修 8 条（README 笔误/AGENTS 行数/死代码/恒真用例）；显式遗留 4 条（L1-线1 记档、L6/L7-线5 漏检方向探测器增强、L4-线2 接受）。同一条 L 不得连续两轮遗留。

## 附：主代理复现材料
- M7 复现脚本：`/tmp/opencode/m1_legacy_repro.py`（场景 A/B 对照输出）
- M10/M11 探针：`issubclass(UnicodeDecodeError, OSError)==False`；`os.environ["A=B"]→ValueError`、`os.environ[""]→OSError(22)`
- H3：`git check-ignore` 五路径全 NOT-IGNORED
- H2：grep 真实群号/QQ 号/`/root/` 于两文件命中（号码此处打码，见 PR 原始提交）
- M1/M2：`grep is_allowed agent_turn.py` 零命中；`store.py:603` `p:{user_id}` 会话键
- 测试：默认 3513+73；PG 全新空库 3599+9；ruff/pyright 双绿；CI API success
