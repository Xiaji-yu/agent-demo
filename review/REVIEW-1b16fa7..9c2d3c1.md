# agent-demo 近期 Commit 评审报告

**评审范围**：1b16fa7..9c2d3c1（20 commit，+4151/−202，47 文件；分支 release/1.0.1-beta，已合回 main=df1e4e3）
**评审日期**：2026-10-23
**评审方式**：5 个并行子代理分线审查（线1 Web 写面/2FA/备份标签、线2 搜索多源/stepfun、线3 备份镜像/恢复、线4 流水线/render_text_image、线5 测试/文档/声称横切）+ 主代理逐条实证（双套件实跑、4 个 H 全部亲自复现或读码证实、变异抽查、CI API 核对）
**工作区状态**：git status 干净、无 stash（评审全程未改受版本控制文件；变异常规在独立 worktree 做后已删）

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 最高风险 | **4×H，全部阻断**：H1 恢复过滤误删数据行（已复现）、H2 preview 明文回显四个凭据键（已复现）、H3 LLM_MODEL 预探测免 2FA 外带主 key（存量）、H4 2FA challenge 无时效+失败无限速（读码+算术） |
| 主题结论 | Web 写面 2FA 落地方向正确但门前有两条免检通道（H2/H3）+门禁本体可爆破（H4）；备份链脚本结构良好但 restore 过滤损坏数据（H1）、挂载检测失效（M10）；搜索多源 fallback 骨架正确、残留单源时代检查（M4）；render_text_image 守住了事件循环纪律但 title 路径绕过全部门禁（M16）；测试质量总体良好（无恒真断言家族新增）、用例数声称全部数平 |
| 测试 | 默认 2858 passed / 60 skipped；PG 全新空库 2931 passed / 9 skipped；RUN_PERF 9 无劣化；ruff 双绿（0.9.6=pin） |
| CI | #149（HEAD）success；#145/#146/#148（区间中段三点）success；#147（main 合并 df1e4e3）success |
| 声称核对 | 用例数/路由数全部数平；**AGENTS.md+README「校验均 2FA」失真**（M2）；「改后重启生效」失真（M6）；AGENTS.md「165 键」过期实为 168（L）；CHANGELOG 缺后半批大项（M19） |
| 上轮衔接 | 上轮 M4/M5/M6 销项；**M1/M2（persona 备份 0644/互覆、save tmp 窗口）未修遗留**；L 批量计划（14 修 11 遗留）未见执行 |
| 门禁（§8.3） | **阻断发布**：4 个 H 全部在可达路径，修复+回归复验后放行 |

## 1. 已实证的问题

### H1 restore 过滤按子串匹配，静默误删 COPY 块数据行 → 恢复损坏【已复现·主代理】

`agentcore/backup/db_backup.py:805`：

```python
kept = [ln for ln in parts if b"transaction_timeout" not in ln]
```

pg_dump 走 plain COPY 格式（`db_backup.py:347-365` 参数无 `--inserts`），数据行是原文，字段内容含 `transaction_timeout` 子串即被删；COPY 块少行而 `\.` 完好，psql 不报错——恢复「成功」但数据静默丢失。573873a 引入。主代理复现（真实函数 + BytesIO）：

```text
被丢弃的行：
  b'SET transaction_timeout = 0;'                ← 预期
  b'1\tp:100\tPG17 新增 SET transaction_timeout = 0 session param'  ← 数据行误删
  b'SET transaction_timeout = 90000;'            ← 预期
COPY 数据行保留数: 1 （应为 2）
```

语料含 PG 文档知识块/技术讨论，命中现实存在。修复：只在 COPY 块外滤 `^SET transaction_timeout = ` 顶格行；补「数据行含子串必须保留」反向用例（现用例恰好锁死错误行为，见 L 线3-L1'）。

### H2 settings/preview 与写第一段明文回显四个凭据键，「只写不回显」失守【已复现·主代理】

4756bfa 把 `SEARCH_API_KEY`/`SEARCH_BASE_URL`/`EMBEDDING_API_KEY`/`EMBEDDING_BASE_URL` 放开为 writable=True & secret=True，但 `web.py:3592-3603` preview 对 env 键直接返回 `current_file`/`current_runtime` 原值，无 secret 处理；写第一段的 `old/new` 走 `cw.masked()`，而 `config_write.py` 掩码只认 `sk-` 前缀——非 sk- key 与 URL 原样进响应与确认框 DOM。主代理 TestClient 复现（临时 env 文件，未触真实 `.env`）：

```text
SEARCH_API_KEY:   http=200 current_runtime='sk-fake-abc123'
EMBEDDING_BASE_URL: http=200 current_runtime='http://10.0.0.5:9999/v1'
```

preview 是只读路由，**零 2FA**——持 web token 即可读走存量搜索/embedding 凭据，直接违反 §4「响应体不得含 api_key/base_url」与 settings_spec 自述「只写不回显」。与上轮 H2 同向的外带通道。修复：secret=True 键一律不回 `current_*`（返回 `is_set` 布尔），`masked()` 接收 spec。测试盲区佐证：test_web.py 的 PREVIEW 无「secret 键不回显」断言。

### H3（存量，484e6ce 引入）settings/write 的 LLM_MODEL 预探测在 2FA 之前：`model@http://attacker` 让 bot 携主 LLM key POST 任意 URL【读码证实·主代理】

`web.py:3661-3677` 探测先于 `_consume_confirm`；`parse_model_spec(file_text, allow_key=False)` 接受 `model@http://evil`；`client.py:164-172` `_apply_entry`「空 key 回落主 key」+ `_post`（:594-610）`Authorization: Bearer {主key}` → 主 LLM key 主动送攻击者服务器；`model_switch.py:56-59` 把上游响应体前 120 字放进 409 detail（半盲读）。前提：providers 未激活（否则 `legacy_model_key` 409 先挡）+ 持 web token。该段 1b16fa7 已存在（`git log -L` 证实），但本轮 a0ce202 立 2FA 门后它成为**门前免检通道**，上轮 H2 的修复决策（provider 面已封）未覆盖此兄弟路径。修复：探测移到 `_consume_confirm` 之后，或空 key 时不带 Authorization 探测。评审未真实外发（不执行攻击）。

### H4 2FA challenge 无时效 + 失败不限速 + 失败不消费 nonce → 6 位码可在线爆破，2FA 增量保护数十分钟量级失效【读码证实+算术推演·主代理】

`web.py:96-128`：`_confirm_token` 的 HMAC 不含任何时间成分（docstring 自认「时间由 TOTP 管」）；`_consume_confirm` 的 `totp.verify` 失败在 nonce 记账**之前** return「totp_invalid」——nonce 无限复用；全文件无任何失败计数/锁定/限速（grep 证实）。攻击链：第一段零成本拿 `(nonce, token)` 后固定，无限换码重试。±1 窗 = 3 有效码/10⁶，500 rps 持续打期望 ~11 分钟命中；每次尝试仅 3 次 HMAC-SHA1，服务端无成本差异。admin.py:96 声称的「web token 泄漏后还得能私聊 bot」这一第二因素被击穿。修复：失败计数（5 次锁 15 分钟）+ challenge HMAC 加时间戳并验窗 + 失败也消费 nonce。评审未对生产 secret 实际压测。

### M 级（19 条，按线归组；位置/证据见各条）

**Web/2FA（线1）**
- **M1** `/2fa` 未限私聊（`admin.py:97-118` 只查 superuser）：superuser 在群聊发 `/2fa` 把滚动码发进群（admin 响应器 priority=5 独立于群聊唤醒词纪律）。叠加 H4 使爆破成本骤降。【推演·读码证实】
- **M2** 备份「校验」无 2FA（`web.py:3181-3203` 仅 `_auth`+锁+审计），而 AGENTS.md:142 与 README:177 声称「校验…均 2FA + 审计」——声称与实现不符（docs/operations.md 与 features-interaction.md 的口径反而正确）。验证路由不接 2FA 本身可辩护，失真在文档。【已复核·主代理】
- **M3** `SEARCH_BASE_URL`/`EMBEDDING_BASE_URL` 可单独改而存量 key 不动，重启后旧 key 发往新端点——llm providers 面同场景有「改 endpoint 必换 key」409 守卫（`web.py:3813-3837`），settings 单键写无同等防护。【推演】

**搜索多源（线2）**
- **M4** `search_multi` 残留单源时代检查（`search.py:349-351` 判 `get_search_client().cfg.api_key`）：纯 JSON 配置下 `search_web` 正常而 `search_multi` 误报「搜索未配置：请在 .env 中设置 SEARCH_API_KEY」——工具循环里该技能必失败且文案误导。【已复现（子代理仿真）+读码证实·主代理】
- **M5** 搜索 test 连接回显上游响应体前 200 字（`web.py:665-671`），且兜底 `except Exception` 回显 `str(e)`（含 URL）——上轮已定修复决策「probe 错误只回状态码」（llm 侧 `model_switch.py:74-75` 注释明示）未同步到新路由；test 动作零测试覆盖。【读码证实·主代理】
- **M6** 「启动期装配、改后重启生效」声称失实：`_load_providers()` 每次调用热读 JSON（`search.py:64-73`），providers 保存即时生效；四处声称（features-interaction.md:267-268、BACKLOG、web.py 注释与 `"effective": "boot"` 标注、AGENTS.md:142）与现实不符。方向上热生效更好，但管理员基于错误模型操作。【读码证实·主代理】
- **M7** fallback 不覆盖「200 + 错误体」：`_search_bocha` 对 dict 响应不校验 schema 即返回（`search.py:163-164`），网关错误体被当「成功空结果」，不切下一源，LLM 收到「未找到相关结果」。【已复现（子代理仿真）】
- **M8** 串行 fallback 无全局墙钟：每源 15s × N 源全挂 = N×15s，跑在工具循环热路径（旧单源 15s 是既有基线，多源放大）。【推演】
- **M9** `sp.load()` 任一条目坏 → 整文件失败 → 静默回落 env 单源（`search.py:71-73`）：用户「收紧/换源」意图被反转继续出网；GET 面板损坏时返回空列表不提示降级态。【推演】

**备份镜像（线3）**
- **M10**（线3-H2 主代理降级）U 盘挂载检测只查 `-d/-w`（`backup_usb.sh:19-22`）：挂载点目录常驻，拔盘后全真 → 完整冷备（含全部凭据+聊天归档）写进根盘，KEEP=8 轮转在根盘累积，可写满根盘致多服务中断；journal 记「done」假成功，与 operations.md「未挂载时跳过」声称不符。裁定 M（高端）：仓库先例中备份管线静默失败记 M，但根盘写满后果建议按高优先级修（`mountpoint -q` 一行）。【读码证实·主代理】
- **M11** `MIRROR_ROOT` 单引号拼进远端命令串（mirror.sh:39/46/93-98）、`KEEP_DAYS` 无数字校验（对比 MAX_GB 有 case 校验）拼进 find 谓词、`ssh` 目标未用 `--`——配置坏值=对备份服务器任意命令（unit 以 xiaji 跑，信任边界内不评 H）。【读码证实】
- **M12** VPS unit 声称「WireGuard 内网」无机制强制：`MIRROR_HOST=vps-backup` 改公网地址即把 9GB 明文 dump（含全部凭据）推公网主机；`After=wg-quick@` 只排序不约束。SSH 加密+known_hosts 缓解（非交互下 ask 会失败，但用户 ssh_config 配 accept-new 即失效）。【读码证实】
- **M13** 冷备在 ext4 上落 0644：rsync `--no-perms`（vfat 刻意）+ service 未设 `UMask=`（默认 0022）→ `.env`/`llm-providers.json`/`2fa-secret.json`/聊天归档同机其他本地用户可读；operations.md 只提示了 FAT32 场景。修复：unit 加 `UMask=0077` 或 rsync `--chmod=D0700,F0600`。【推演】
- **M14** mirror 对 `backups` 目录缺失静默 exit 0（`backup_mirror.sh:80-82`），与同脚本「目录存在但无 dump 则必须失败」的新鲜度闸门自我矛盾：换路径部署后每晚「成功」但从未镜像任何 dump。【读码证实】
- **M15** USB 冷备 MANIFEST 只校验「已写文件」不自检清单完整性（dump/.env/config.yaml 缺一仍全绿）；personas glob 的 `2>/dev/null || true` 吞 U 盘 IO 错误。【推演】

**流水线/长图（线4）**
- **M16** `render_text_image` 入口门禁只查 `content`（`text_image.py:71-79`），`title` 直通渲染（schema 亦无 maxLength），`_fit` 逐字符收缩 O(n²)（`text_card.py:143-149`）。主代理实测：**title=12000 → 27s**（子代理 35s），**title=6000 → 6.9s 已近 10s 超时**；wait_for 到点后僵尸线程继续满核跑完。public 技能+群聊普通用户一句话稳定触发；workflow 重试 2 次 × turn 信号量 4 ≈ 最坏 12 线程 × 30s+。事件循环不受阻（to_thread ✓）故不评 H。修复：title 限长（>100 字截断）或 `_fit` O(n) 化。【已复现·主代理】
- **M17** 软上限截断标注声称失实：`text_card.py:235` 标注条件是 `cut_by_height`（高度门禁）而非 `truncated`（软上限）——半角 7000 字截到 6000（96 行 < 119 行上限）时图内**无任何「已截断」标注**，页脚还显示截断后的 6000 字。commit 5a7dab8 与 features-skills.md:44 声称「超出截断并在图内标注」。主代理复现：半角 7000 → truncated=True 且无高度截断。【已复现·主代理】

**测试/配置（线5）**
- **M18** `.env.example:93,96` 空值键 + `os.getenv(ENV, _DEFAULT_PATH)` 空串不回落：README 快速开始（`cp .env.example .env`）的新部署，`AGENT_2FA_SECRET_FILE=` 空串 → `Path("")`=`.` → totp 写分支 `with_name` 抛 **ValueError 逃逸**（`load_or_create_secret` 只接 OSError）→ `/2fa` 无响应（NoneBot 吞异常）、web 全部写路由 500，2FA 全链断裂；注释「留空 = data/2fa-secret.json」失真。search 同病但被 `SearchProviderConfigError` 回落救回；`AGENT_LLM_PROVIDERS_FILE=`（存量）同病。测试抓不到：conftest 隔离用 tmp **绝对路径**恰好绕开空串分支。主代理复现：`AGENT_2FA_SECRET_FILE= python -c ...` → `ValueError: PosixPath('.') has an empty name`。【已复现·主代理】
- **M19** CHANGELOG「记录 1.0.0 至今改动」缺后半批大项：写面 2FA（a0ce202）、PERSONAS_DEFAULT（ebf16bc）、自动发文件先收紧后删除（8aeb664+edb984f，线上事故修复）、web 备份标签（1b4519a）、NAS/USB/VPS 异地镜像+6 units（7e2e6f1）均未入账（`git log --follow` 证实仅 4fbd1b0/5a7dab8 触碰）。【读码证实·主代理】

### L 级（18 条，压缩列出；批量关闭计划见 §6）

- **L1**（线1）challenge「expires_in: 30」是假提示，实际无时效；nonce 记账 600s 清理后同一 `(nonce, token)` 可二次消费（写幂等危害≈0，download 可多下一次，需新 TOTP 码）——随 H4 一并修。
- **L2**（线1）`totp` secret 文件 JSON 合法但值非 base32 时不重生成 → `/2fa` 与全部写路由 500 死锁（原子写使概率很低；子代理复现 binascii.Error）。
- **L3**（线1=线5 同项）AGENTS.md「ENV_SPECS 165 键」过期，实测 168（可写 109、17 簇均正确——同段改一半）。裁定 L：单数字漂移、无能力夸大。
- **L4**（线1）`cw.masked()` 只认 sk- 前缀与 `spec.secret` 脱节，注释停留在「本期白名单键全非敏感」——随 H2 一并修。
- **L5**（线2）stepfun 在 env 层三处行为分裂：回落路径静默改 bocha（`search.py:52-53`）、迁移认 stepfun、写面 choices 只 bocha/tavily。
- **L6**（线2）`search_web` 的 max_results 无夹紧（0/负数透传 API）；`search_items` 把 0 吞成默认——与「非正 limit → 空结果」不变量精神不符；`test_max_results_zero` 把透传固化。
- **L7**（线2）全挂报错把异常原文（含自配端点 URL/内网 host）送进对话（`search.py:131`），对齐 M5 口径只回类型名。
- **L8**（线2）搜索 key 备份落 `data/backups/`（与 llm 侧同源决策）：web 列表 glob 不含 .bak 不可下载，记录在案。
- **L9**（线3）mirror 远端校验硬依赖最新份有 .sha256 sidecar，缺 sidecar（合法场景）与校验失败不可区分，每晚红造成告警疲劳。
- **L10**（线3）mirror 新鲜度闸门与远端校验只认 `*.sql.gz`，纯 JSONL 部署每晚 exit 1 误报（prune 反而认 jsonl）。
- **L11**（线3）三个 backup service 均无 `OnFailure=`，备份失败只进 journal。
- **L12**（线3）USB 轮转按目录名 sort：时钟回拨时当天目录排队尾优先被删（KEEP=8 内影响极低）。
- **L13**（线3）远端 `pgwal`/`pgbase` 无清理路径（当前无产出者，启用 PITR 前需补）。
- **L14**（线4）渲染无专用信号量：排队时间计入 10s 超时，池满载时任务未跑即「超时」白渲染一张（~14MB，短暂）。
- **L15**（线4）发送「确定失败」降级分支（`text_image.py:139-140`）无用例（uncertain/no_bot/超时/过大均有）。
- **L16**（线4）features-messaging.md:144「并把当轮回复替换为『文件已发送』」残留系统替换语义，实现已是 LLM 自主转述。
- **L17**（线4）`_CONTROL_RE` 漏 `\x0b/\x0c`——与 engine/fs/groupstats 三处既有口径一致，要修四处一起修。
- **L18**（线5）`test_totp.py:47` 首断言 `not verify(...) or current==...` 有 1/10⁶ 恒真漏抓窗（后两条已兜底，恒真变异仍必红）；建议改固定非当前码。

## 2. 被证伪的发现（防止重走弯路）

1. **「渲染阻塞事件循环」**——证伪：`render_text_card_png` 全程 `asyncio.to_thread`，content 门禁绘制前生效（6000 CJK 最坏 2.3s 实测），历史 H1/H2 纪律守住；放大队景点只剩 title 路径（M16）。
2. **「6000 字软上限击穿 4000px」**——证伪：6000 CJK=146 行 > 119 行上限，被高度门禁二次截到 h=3988；软上限与高度门禁串联自洽（缺的只是 M17 的标注）。
3. **「is_uncertain_send_error 接不全/重发」**——证伪：TimeoutError/httpx.TimeoutException/ReadError/WriteError/RemoteProtocolError/ProxyError/NetworkError → True，ConnectError/UnsupportedProtocol（未发出）→ False；UNCERTAIN 分支不附文本降级（测试锁 bot.calls==1）。
4. **「搜索多源丢 usage/预算账」**——不成立：搜索无 LLM token，budget 无此计费维度。
5. **「迁移会用 env 覆盖 web 编辑」**——不成立：仅 `FileNotFoundError` 才迁移，幂等有测试。
6. **「搜索结果绕过围栏」**——不成立：search_web/search_multi 在 `engine._UNTRUSTED_TOOL_RESULTS`，fence 于引擎层应用，截断发生在包装前。
7. **「search-providers.json 0644 窗口/tmp 固定名互删」**——不成立：`os.open(0o600|O_NOFOLLOW)` 一步到位、tmp 唯一名（pid+uuid4），与 llm 侧逐行同构。
8. **「搜索面板组成 H2 凭据外带链」**——不成立：改 endpoint/format 不提供新 key → 409；test 只用已存配置；删最后一源 409 堵空列表回落。
9. **「备份下载可借道取 .env/2fa-secret」**——不成立：`Path(name).name == name` + `glob("agent-demo-*")` + `.gz` 后缀真白名单。
10. **「删除最新一份有竞态」**——不成立：op 锁内重 list + 第二段 404 复查，测试在。
11. **「TOTP 实现错误/熵弱/0600 窗口」**——不成立：RFC 4226 附录 D 十向量全对（子代理仿真）、`os.urandom(20)`、`O_NOFOLLOW|0o600` 一步到位（逐步核对 syscall 序）。
12. **「preview 可回显 LLM_API_KEY 等不可写键」**——不成立：那些键 writable=False，`_validate_write` 即 403；H2 面仅限本轮放开四键。
13. **「llm test 动作与 delete 挑战互换」**——不成立：test 免两段是设计（有 `llm:test` 审计），不进 challenge 体系。
14. **「多 worker 部署使 nonce 防重放失效」**——当前不成立（单进程挂载）；记录为部署约束。
15. **「mirror 的 `[ ] &&` 在 set -e 下意外退出」**——证伪（bash 实测循环内短路列表失败不触发 errexit）。
16. **「prune 后同步失败 → 远端零备份」**——作为 H 排除：KEEP_DAYS=30 多份留存，仅新部署首推为空属正常初始态。
17. **「USB 冷备漏打包 PERSONAS_DEFAULT 指向的本地人格」**——证伪：本地自定义人格同在 `agentcore/personas/*.md` glob 内。
18. **「新用例隐式依赖本地 .env/data（CI #91 复发）」**——不成立：新用例全显式 monkeypatch/tmp 路径，无网络；TestBackupAPI 显式 setenv DATABASE_URL。
19. **「BACKLOG DOCS-STATS 2918 失真」**（上轮线5 存疑）——销项：`--check-stats` 实跑一致（2858+60=2918）。

## 3. 测试与文档状况

**实跑（主代理）**：
- 默认套件：`pytest -q` → **2858 passed / 60 skipped**（67s，0 failed）
- PG 套件：重建全新空 `qqagent_test`（等价 CI 起点）→ **2931 passed / 9 skipped**（首轮 13F/58E 经查为评审者连接串缺密码所致，已排除，非代码问题）
- `ruff check` + `ruff format --check`：双绿（本地 0.9.6 = pyproject pin = pre-commit rev）
- `RUN_PERF=1 pytest tests/test_perf.py -q` → 9 passed，基线无劣化
- `check_review_docs.py --check-stats`：索引完整/产物位置/文档地图/BACKLOG 计数全 OK
- CI（GitHub API）：#149（HEAD 9c2d3c1）success；#145/#146/#148（716d61f/5a7dab8/edb984f）success；#147（main 合并 df1e4e3）success；区间内其余 commit 无独立 run（分支推送头+合并点覆盖，HEAD run 覆盖最终树）

**声称核对（抽全）**：用例数声称全部数平（totp 7、TestBackupAPI 10、TestSearchProvidersWeb 5、text_card 11+发送 10=21、search 组 7=5+2、providers 17=16+1、4756bfa +3、ebf16bc 3）；「7 个写入路由接 2FA」为首批口径，HEAD 10 个状态变更/外带路由全部经 `_consume_confirm` 无漏网；「删除对应测试、保留 M5 边界」属实（残留断言仍绑定实现）。**变异复核声称**：a0ce202「verify 恒真→2 红」经主代理 worktree 变异抽查吻合（恰 `test_verify_rejects_wrong`+`test_window_tolerance` 红）；其余 5 处变异声称静态旁证吻合、未独立重放（见未验证面 (b)）。

**文档一致性**：AGENTS.md+README「校验均 2FA」失真（M2）；四处「改后重启生效」失真（M6）；AGENTS.md 165→168（L3）；CHANGELOG 缺后半批（M19）；`.env.example` ↔ ENV_SPECS 168=168 同步 ✓；features-skills/messaging/operations 主体与实现一致（L16 一句措辞漂移）。

**未验证面（§8.3）**：
- (a) H3 真实外发、H4 真实压测——评审不执行攻击，读码链+算术推演定性；
- (b) 5 处 commit 变异声称中 4 处未独立重放（a0ce202 已抽查吻合，其余静态旁证）；
- (c) 真机 QQ 通道：M1 `/2fa` 群聊路径为读码推演（admin 响应器 priority=5 独立路由），未真机复现；
- (d) NAS/USB/VPS 真实硬件与远端链路（M10-M15 均推演级，脚本未在真实盘/远端跑）；
- (e) 搜索真实上游（bocha/tavily/stepfun）未打真 API。

**门禁结论**：4 个 H 全部在可达路径（H1 恢复命令、H2/H3/H4 web 写面）→ **阻断发布**；M10 建议随 H 批同修。上轮未验证面 (a) PG 套件本轮已销项（全新空库实跑）。

## 4. 已验证为「无问题」的关键项

1. **RFC 4226 实现正确**（官方向量全对）+ secret 熵 160bit + 0600 一步到位（O_NOFOLLOW，无历史 atomic_write 式窗口）+ 全角数字/非 6 位拒收。
2. **challenge 绑定与成功路径防重放**：HMAC 覆盖 nonce|key|值，跨路由/换值/换键不互换；nonce 单次消费（成功路径）位于无 await 同步段。
3. **写面覆盖完整**：10 个状态变更/外带路由全部 2FA；审计齐备（含 source_ip/nonce，old/new 过 masked）。
4. **备份标签**：文件名真白名单、删除保护最新、下载 no-store+两段 2FA+白名单内二次复查。
5. **搜索多源骨架**：原子写与 llm-providers 逐行同构（tmp 唯一名/0600/fsync/写后重读回滚）、迁移幂等、H2 守卫在位、围栏未被绕过、停机 CancelledError 穿透。
6. **兜底删除净度**：`_user_asked_for_file`/`_maybe_file_request`/auto_file 全仓零残留；send_markdown_file 链路（注册/public 权限/prompt 引导/1500 字门禁独立）完好；M5 边界断言保留且仍绑定实现。
7. **text_image 越权/隐私面收口**：registry 覆盖 LLM 传入的 user/group（无法伪造收件人）、PNG 全程内存不落盘、发往当前会话、过出站节流。
8. **shell 脚本基线**：`set -euo pipefail`、全插值加引号、bash -n 通过；MAX_GB 数字校验+超限拒绝；上轮 M7 修复未破坏（PGPASSWORD 不进 argv）。
9. **测试质量**：无恒真断言家族新增（断言均绑定「改坏必红」）；conftest 三新夹具隔离闭环；无隐式 .env/data/网络依赖。
10. **依赖零变化**：pyproject 仅版本号 1.0.0b1→1.0.1b1（PEP 440 合规）；Pillow>=10.3.0 为主依赖非新增。

## 5. 与在库报告的衔接复核（上轮 REVIEW-2ed7c63..1b16fa7：无 H / M×6 / L×25）

| 上轮项 | 本轮状态 |
|---|---|
| M1 persona 覆盖备份三重不一致（0644/同秒互覆/无滚动） | **未修遗留**：web.py:4203-4207 仍 `write_bytes`（0644）+ 秒级同名 + 无滚动（主代理读码证实） |
| M2 PersonaManager.save tmp 先 0644 后 0600 窗口 | **未修遗留**：manager.py:195-197 仍 open→chmod 序（主代理读码证实） |
| M3 8af2cd9「e2e 用例」声称失真 | 台账已立案（上轮定谳），无代码动作 |
| M4 未提交 DeepSeek.md 引用不存在的 send_sticker | **按门禁建议未入库**（e4811ed gitignore 本地人格）→ 销项；本机私有文件仍含 send_sticker×2，属用户本地选择 |
| M5 docs 人格清单与本机 yun.md 耦合 | **已修**：现清单只列仓库内置人格（memory-and-persona.md:104）→ 销项 |
| M6 BACKLOG DOCS-STATS 漂移 21 例 | **已修**：`--check-stats` 实跑一致 → 销项 |
| L×25（上轮计划 14 修 11 遗留） | **未见执行**：本区间全部为 feature commit，无 L 批量修复落码；按 L 收敛规则需在下一 FIX 周期显式处置（见 §6） |

## 6. 修复优先级建议

1. **H1** restore 过滤（一行级：仅 COPY 块外滤顶格 `^SET transaction_timeout = ` + 反向用例）——恢复路径数据损坏，最优先。
2. **H2** preview/写第一段对 secret=True 键不回 `current_*`（`is_set` 布尔），`masked()` 接收 spec。
3. **H3** LLM_MODEL 预探测移到 `_consume_confirm` 后（或空 key 不回落主 key、不带 Authorization）。
4. **H4** challenge HMAC 加时间戳并验窗 + 按 (ip,nonce) 失败计数锁定 + 失败也消费 nonce。
5. **M18** 空值 env 键 `or`-回落默认（新部署 2FA 全断，一行级×2 + 空串单测）。
6. **M16** title 入口限长 + （顺带 L14）渲染 Semaphore。
7. **M10** `mountpoint -q` 挂载检测（+M13 service `UMask=0077` 顺带）。
8. 其余 M 按线批修：web（M1/M2/M3）、搜索（M4/M5/M6/M7/M8/M9）、备份（M11/M12/M14/M15）、长图（M17）、文档（M19）；**同批处置上轮遗留 M1/M2（persona 备份 0644/互覆、save tmp 窗口）**。
9. **L 批量关闭计划（18 条）**：随 H/M 顺带修 6 条（L1 随 H4、L4 随 H2、L7 随 M5、L14 随 M16、L18 随变异加固、L9 随线3 批）；独立小修 8 条（L3/L5/L6/L10/L11/L15/L16/线5-L2 gitignore 提示）；显式遗留 4 条入档（L8 同源决策、L12 时钟回拨影响极低、L13 无产出者、L17 全仓四处统一口径需专项）。
