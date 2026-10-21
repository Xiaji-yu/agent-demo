# agent-demo 近期 Commit 评审报告

**评审范围**：2ed7c63..1b16fa7（10 个 commit：98242dc 上轮评审修复 → c8fd03f 小黑盒技能 → 2cf793f trigger-miss 入库 → d1248c1 merge → 624e61b 顶层「模型/人格」标签 → 5c66590 merge → 3cb1d40 groupstats NUL 修复 → 8af2cd9 人格表单轮询 → 81d20ae merge → 1b16fa7 群人格）+ **未提交预审**（归档时点 13 修改 + 1 untracked：群人格后续 PERSONAS_DEFAULT/DeepSeek.md + **评审期间新增的 embedding/搜索凭据 web 可配**〔settings_spec/web/AGENTS/BACKLOG/README/test_web〕）
**评审日期**：2026-10-21
**评审方式**：5 个并行子代理分线审查（线1 上轮修复复核 / 线2 小黑盒+诊断行 / 线3 web 写面 / 线4 群人格 / 线5 测试与文档）+ 主代理逐条实证（首轮 3 个子代理 provider transport 超时，重试成功；实跑双套件与 lint、M 逐条复核、声称对账、**「可写 105/108」之谜收口为主代理独立实证**）
**工作区状态**：**不干净且评审期间演进**——评审开始时 7+1 个未提交文件，归档时 13+1（维护者并行开发了「模型」标签的 embedding/搜索配置）；报告对两个时点的未提交状态分别预审，评审过程未修改任何受版本控制文件

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | **无 H**。上轮双 H（Anthropic 多模态 / provider key 外带链）经复现翻转为**真修** |
| M×6 | persona 备份三重不一致（0644/同秒互覆/无滚动）、save tmp 0644 窗口、「e2e 用例」声称失真、未提交 DeepSeek.md 引用不存在的技能（入库即埋 unknown 硬停）、docs 现有人格清单与本机状态耦合、BACKLOG 计数漂移 21 例 |
| L×25 | 同型面「兄弟函数」遗漏（/model 群聊回执完整 endpoint、legacy probe 回显 body）、esc 不转义单引号、备份 0600 创建窗口、互知声明措辞张力等；含批量关闭计划 |
| 上轮修复复核 | H1/H2 复现翻转；M×12 全部处置（M10 部分修复、M12 记录不重写——诚实声明）；L 25 条修复 + L10/L18/L28 与「M6 内网 endpoint 开关」显式入档；FIX 对账落码率与声称一致 |
| 新功能验证 | 小黑盒：hkey CPU 面实测 62µs（签名输入与 LLM 无关、无增长循环）、SSRF 闸在请求前且不跟重定向、围栏闭合无缺口；群人格：双实现逐方法一致、DDL 全新空库验证通过、命令双层权限+静默留痕、互知声明无注入面；trigger-miss 入库版 ≡ 预审版（上轮 L9 勿分享提示已落 docs） |
| 实跑数据 | 默认套件 **2787 passed / 60 skipped**（56.41s）；**PG 全新空库（scratch）2863 passed / 9 skipped**（68.92s，DDL `group_persona` 表验证通过——上轮未验证面 (a) 销项）；ruff check + format 双绿（0.9.6）；依赖零变化 |
| 声称核对 | 98242dc「2724+58」与「变异 7/7」台账采信（锚点抽样齐全）；c8fd03f「17 例」实测吻合、「变异 10/10」采信；1b16fa7 声称项测试落点齐全；**8af2cd9「e2e 用例」失真 → M3**；AGENTS.md「可写 105→108」演进与实现同步（见 §3 数字之谜收口） |
| 门禁 | 无 H、无未声明 M 盲区 → **不阻断**；未提交预审**放行**（DeepSeek.md 建议按 M4 处置后再提交） |

---

## 1. 已实证的问题

### M1｜persona 覆盖备份与写面既有备份纪律三重不一致【文件名生成已复现，其余推演】

`plugins/qq_agent_adapter/web.py:3169-3177`（624e61b）对照 `config_write.py:286-301`（`backup_file`）：①`write_bytes` 落盘 0644（persona 正本 0600）——备份比正本宽，违背同文件内 L14 注释明言的教训；②时间戳仅秒级——主代理仿真确认同一秒两次「保存同名」生成同一路径，第二次截断覆盖第一次（连环改稿是两段确认流程的正常节奏），改前备份丢失；③无滚动保留（`backup_file` 保留 `_BACKUP_KEEP` 份）。**建议**：直接复用 `backup_file`（或抽公共函数）。

### M2｜`PersonaManager.save` 的 tmp「先 0644 后 0600」写窗口【读码确认】

`agentcore/personas/manager.py:193-198`：`open(tmp,"w")` 按 umask 以 0644 承载全文后 `os.chmod`——与 `config_write.atomic_write` docstring 记录的已立案教训（REVIEW-3ce6e0a..de09478 M1）完全同型：崩溃/被杀残留 0644 半截正文且 tmp 名可预测。**建议**：`os.open(..., O_NOFOLLOW, 0o600)` + `os.fdopen`。

### M3｜8af2cd9 声称「e2e 新增跨轮询保留用例并做变异复核」，实际为 3 行静态字符串断言，且仓库无 e2e 基建【已复现】

commit message vs `tests/test_web.py:1997-1999`（`assert "el.dataset.built" in web._PAGE` 等 3 行）：不执行 JS、不模拟轮询，验证不了「表单跨轮询保留」行为本身；全仓无 playwright/jsdom 依赖。变异对静态断言成立，但断言强度低于声称——历轮「恒真断言」问题族的变体。**建议**：措辞降级为「页面字符串回归锁（静态），行为靠人工验证」并 BACKLOG 挂账，或补可跑的中间层用例。

### M4｜（未提交预审）DeepSeek.md 引导模型调用仓库中不存在的 `send_sticker` 技能，入库后其他部署高频撞 unknown→硬停【已复现（grep）】

`agentcore/personas/DeepSeek.md:192-193`（§十/§十二多处）引用 `send_sticker`：全仓 grep 在 agentcore/plugins 零命中，本机 `data/skills/`（gitignore 运行时目录）也无。persona 正文受信不过围栏（`manager.py:33-36` 明示定位），无机制拦截；入库分发后其他部署的模型在「别人要表情包」这一高频群聊场景调用不存在的技能 → registry unknown → `engine._MAX_UNKNOWN_CALLS=3` 触发后**硬停本轮**，连正当回复一起被吞。frontmatter 合法（name 过 `_NAME_RE`）、5809 字 < 20000 上限、内容无注入面——问题仅在技能耦合。**建议**：①改入 gitignore 的私有位置（与 yun.md 同待遇）；②或入库但改写为不绑定技能名的通用措辞；③同时补 `.gitignore`（当前 `??` 裸露，`git add -A` 会误入库）。

### M5｜docs/memory-and-persona.md「现有人格」清单与本机状态耦合失真【已复现】

`docs/memory-and-persona.md:103`：「`yun`（默认，云崽本崽…）」——`yun.md` 是本机 gitignore 文件（`.gitignore:70`），`git ls-files` 仓库内无此文件；且 HEAD 上 `assistant.md` 的 `default: false`（1b16fa7）在未提交变更里改回 `true`，「谁是默认」随本机状态摇摆，其他部署检出即与文档矛盾。1b16fa7 message 自己承认「仓库内不含 yun.md」但入库文档未加限定。**建议**：「现有人格」清单加「本机」限定或移出入库文档；默认归属按 PERSONAS_DEFAULT 机制（未提交，方案自洽）统一改写。

### M6｜BACKLOG 头部 DOCS-STATS 计数漂移【已复现】

`BACKLOG.md:5` 写「2826 收集：2768 通过 + 58 跳过」（3cb1d40 时点写入）；主代理实测 HEAD **2847 收集（2787 通过 + 60 跳过）**——1b16fa7 新增 16 个测试函数未刷新，漂移 +21 收集。`check_review_docs.py --check-stats` 仅归档报告前必跑，普通 commit 不触发。**建议**：提交前跑 `--write-stats`；或让 CI 对每次 push 核对计数块。

### L 级（25 条，压缩列出；>10 触发收敛规则，批量关闭计划见 §6-4）

| # | 位置 | 一句话 |
|---|---|---|
| L1 | test_archive_skills.py:335 | L27 修复漏网：archive 测试仍直接 `os.symlink`，未挂 `symlinks_supported` fixture |
| L2 | providers.py:292-303 | M4 修复的核心分支（无备份首启回滚防互删）零回归锁定——删掉全量套件照样绿 |
| L3 | model_switch.py:332 | `/model` 群聊回执打完整 `base_url`（M5 例外清单「仅鉴权 web 设置面」未覆盖此面；限 superuser） |
| L4 | web.py:474 | legacy 模式 `LLM_MODEL` 值（含端点）仍出响应体；`test_credentials_and_urls_never_returned` 断言键名、对值内嵌 URL 假阴性 |
| L5 | model_switch.py:57-60 | legacy `probe_failure_detail` 仍回显上游响应体 120 字（M6 只修了 provider 路径，同型面遗漏） |
| L6 | providers.py:277-279 + config_write.py:294-296 | 备份 `copy2`+`chmod` 存在 0644「创建即」窗口（毫秒级、含 key；与 L2 修复标准不一致） |
| L7 | group_context.py:148-152 | 「（提问者本人）」标注可被群名片伪造同名（既有自取名风险的等价放大；建议换不可能自然出现的形态并对含该子串的名片转义） |
| L8 | xiaoheihe.py:97-99 | query 形态不判 host：`https://evil.com/?link_id=…` 可被误认（仅对象混淆，值受 `[0-9a-zA-Z]{6,32}` 约束，不构成注入/SSRF；与 commit 声称的「路径形态只认真实 host」口径差异未披露） |
| L9 | xiaoheihe.py:394-403 | API 错误 `msg`（第三方响应文本）直通进 prompt 与日志，无截断/换行转义（`%s` 非 `%r`；测试把直通锁成预期） |
| L10 | xiaoheihe.py:247-258 | 未闭合 `<script>` 静默吞掉其后全部正文（内容丢失非泄漏，难排查） |
| L11 | xiaoheihe.py fetch_post | 2MB JSON 解析 + HTMLParser 在 async 路径同步执行（实测最坏 ~40ms，远低于 pow 坑量级；风格与 web_fetch 的 to_thread 不一致） |
| L12 | xiaoheihe.py 解析 | 大写 URL/参数名不识别（官方链接恒小写，仅记录） |
| L13 | web.py:3132,3163 | save 覆盖判定 TOCTOU：挑战时 `existing` 快照锁内复用（30s 窗口内经 SSH 出现的同名文件不备份被覆盖） |
| L14 | web.py:1271-1272,2085 | `esc()` 不转义单引号，onclick 内联 JS 拼接安全性押在后端正则上（当前不可达：name/group_id 白名单不含单引号；建议补 `'`→`&#39;` 或改 data-* 委托） |
| L15 | config_write.py:134 vs web.py:3170 | `AGENT_WEB_BACKUP_DIR` 两处语义不同（最终目录 vs 备份根） |
| L16 | web.py:3220 | PERSONAS_DEFAULT 指向的人格（frontmatter default=false）可被 web 删除 → 本机默认静默降级（「default 拒删」不感知 env 口径；建议 delete 追加 `pm.default()` 复查） |
| L17 | store.py:1576-1582 | `list_known_group_ids` PG 侧无索引全表 DISTINCT，人格标签 5s 轮询放大（加 `(group_id)` 索引或 60s 缓存） |
| L18 | engine.py:653-661 + admin.py:1136 | 失效人格 WARNING 每轮重复；list 回退展示可能显示实际不生效的失效名 |
| L19 | engine.py:432-435 vs 486-488 | 互知声明「长期记忆共享」与 facts 段「其他会话不可见」措辞张力（两句都对但模型易混淆） |
| L20 | AGENTS.md:129 | 「17 个设置簇」括在 ENV_SPECS 名下，实际 ENV 键只落 16 簇、17 是 CLUSTERS 定义表（含纯 yaml 的 history 簇）——归因口径歧义 |
| L21 | tests/test_engine.py:694-698 | 群人格矩阵场景 5 只锁回退未锁告警（静默 fail 路径应留痕断言——戳一戳事故教训） |
| L22 | tests/test_tools.py | link_id 长度边界未测邻界值（6/32 放行、5/31 拒绝未测；实现正确） |
| L23 | tests/test_web.py 多处 | 前端 UI 回归全部为 `_PAGE` 字符串断言，JS 行为零自动化（与仓库既有风格一致，已知边界；与 M3 同根） |
| L24 | tests/conftest.py:29-32 | conftest 共享客户端 reload 吞异常（`except Exception: pass`），可能带旧配置假绿 |
| L25 | xiaoheihe/test_tools | （并入线5 视角）hkey 黄金值测试质量好，但「与线上可用签名一致」无法离线复核——属未验证面非缺陷 |

---

## 2. 被证伪的发现（含存疑）

**线1（8 项）**：`provider_status()` 改 `admin_view()` 泄漏 endpoint 给群聊——全部调用点核对仅鉴权面与 `active_id` 取用；`_image_block` 对 `image/jpg/svg` 透传打挂请求——media_type 来自魔数 sniff 只产四种合法值，正常链路不可达；空数组 content 致 Anthropic 400——`_append` 跳空，与旧行为一致；H2 守卫可被空 endpoint/别名/大小写绕过——逐一排除（`rstrip("/")`/`lower()` 归一口径一致，方向是误 409 保守）；M1 第二遍仍全量解压——tar 分支同为惰性、能到第二遍已过总量校验；清洗后空串双实现不等价——两侧首次插入均落空串；「105 但别处残留 108」——HEAD 时点全仓一致（演进后见 §3）；FIX 实跑数字（2724+58、PG 2839+9、变异 7/7）——不可重放，按可证伪断言采信，最终以本轮实跑为准。

**线2（7 项）**：超长 link_id 冻结事件循环——link_id 不进签名、RE 上限 32；302 跳内网绕 SSRF——`follow_redirects=False`；2MB 靠 Content-Length 欺骗——流式实计数；仿冒链接让 bot 请求任意 URL——URL 结构硬编码；截断破坏围栏——截断在 fence 前、伪装闭合行被打散；变异 10/10——不可重放采信；黄金值线上可用性——离线仅验自洽（unknown）。

**线3（7 项）**：`/api/personas` 返回服务器路径违反禁令——禁运清单是 api_key/base_url/正文三类；`_persona_detail` 返回正文违反「消息正文」禁令——禁令针对 QQ 消息正文，persona 是管理员配置；`finally: tmp.unlink` 误删刚 replace 的目标——replace 后路径已不存在；同秒并发 save tmp 冲突——`_write_lock` 串行；确认码判重竞态——检查与写入间无 await；写面关闭时表单可点是双入口——后端 404 真拒；persona 正文直注 system prompt 违反围栏不变量——管理员显式配置属受信决策（`manager.py:33-36` 注释明示），与成长层（用户派生数据必须围栏）边界划分正确。

**线4（5 项）**：非超管静默 return 漏消息给聊天响应器——persona_cmd priority=5+block 先于 chat；群人格指向空 body 人格会崩——回落通用分支有测试；InMemory known_group_ids 键解析越界——`split(":",2)`+count 防护、键格式封闭；内存/PG known_group_ids 语义不一致——均为「有会话的群、去重、升序」且 contract 参数化；「老测试库跑 contract 因缺表假红」——TRUNCATE 报 undefined table 属 fail-loud，逼重建，与 A2 教训互补。

**线5（5 项）**：「8af2cd9 无任何测试改动」——证伪，实为 +5 行静态断言（问题在声称措辞 = M3）；「17 簇吻合」——维持结论但修正口径（L20）；test_probe lambda 签名不匹配——三处调用均 `model=` 关键字；web 群人格端点缺用户级 superuser 判定与 AGENTS.md 矛盾——web 写面即 token+CIDR 管理员门禁，QQ 命令侧有 is_superuser，行文一致；「merge 冲突丢内容」——d1248c1/81d20ae 为平凡 merge（合并树=后 parent），5c66590 双向核对两侧内容各归其位。

**存疑（unknown）**：DeepSeek.md §四/§九「安静结束本轮」在真实模型的空回复处理路径；test_personas 3 个新用例的变异复核（静态推演锚点成立）；FIX/commit 的变异台账（7/7、10/10）不可重放。

---

## 3. 测试与文档状况

**实跑数据（主代理）**：

| 项 | 结果 |
|---|---|
| 默认套件 | `pytest -q` → **2787 passed / 60 skipped**（56.41s；60=58+2，新增用例的环境门控跳过） |
| PG 全新空库 | `TEST_DATABASE_URL=scratch`（`scripts/scratch_db.py create --yes` 建库，验后已删）→ **2863 passed / 9 skipped**（68.92s）——1b16fa7 的 `group_persona` DDL（`CREATE TABLE IF NOT EXISTS`、TIMESTAMPTZ 拼写正确）对全新空库验证通过，双实现契约 PG 臂全绿 |
| ruff check + format | 双绿（0.9.6 与 pin 一致，198 files） |
| RUN_PERF | 未跑（本轮热路径改动仅 pipeline 回捞标注与 groupstats 清洗，非缓冲/节流热路径；上轮基线佐证）——记录为未验证面 |
| CI 状态 | **未核实**——GitHub API 网络不可达（HTTP 000，连续第二轮） |

**「可写 105/108」之谜收口（主代理独立实证）**：上轮 M9 修正为 108 后，本轮两个子代理实测均报「105 与 AGENTS.md 吻合」，主代理复核却得 **108**（`WRITABLE` 运行时事实），逐层追查发现：**工作区在评审期间新增了未提交变更**——`SEARCH_API_KEY`/`EMBEDDING_BASE_URL`/`EMBEDDING_API_KEY` 三键从 `writable=False` 改为 `writable=True`（web「模型」标签新增 M3 Embedding/M4 搜索配置），HEAD 时点它们仍不可写（子代理测的是 HEAD，主代理测的是演进后的工作区，两代实测都正确）。配套的 AGENTS.md 修订（2026-10-21）已同步：可写 108、凭据不可写行补例外「三个凭据改在 web 模型标签可配，只写不回显、改后重启生效」。主代理逐项验证该例外自洽：secret=True → GET 连键名不出（只写不回显 ✓）、三键 live=False（重启生效 ✓）、web 响应体字段名刻意避开 `base_url/api_key` 子串并有 `test_no_secrets_in_payload` 锁、前端密码框留空=保持不变、写入复用 writeSetting 两段确认+审计+备份全流程。**预审放行**，两条建议：①`test_secret_specs_are_not_writable` 的 6 键清单未含三键——拍板「可写」后应补一条正例锁（三键 `secret=True` 且写入走同流程）；②EMBEDDING_BASE_URL 可写在「token 打穿」威胁模型下允许改道 embedding 流量（与 provider 面板同威胁等价，AGENTS.md 已显式接受）——无需再收窄，但 `test_no_secrets_in_payload` 与例外清单的联动关系建议在测试注释中写明。

**文档一致性**：AGENTS.md 不变量三处修订（105→108、群人格行、Web 例外行）与实现同步；165/17/35/29 数字实测吻合；README/docs/configuration 无 108/105 残留混用；BACKLOG §6.11（NUL 事故）与 3cb1d40 diff 逐项对上；失真项：M3（e2e 声称）、M5（现有人格清单）、M6（计数漂移）、L20（17 簇归因）。

**未验证面清单（§8.3 门禁口径）**：
- (a) **CI 状态**：API 网络不可达（HTTP 000）连续第二轮——本地全绿 + lint 双绿 + 零依赖变化，规则性失败风险低，具备条件时补查。
- (b) **RUN_PERF**：本轮热路径无实质改动，未跑；下轮若动 pipeline/outbound 缓冲需补。
- (c) **真机通道**：小黑盒线上接口签名可用性（黄金值自洽但未对线上实测）；群历史 get_msg 回捞真实可用性（上轮延续）。
- (d) **变异台账**：98242dc「7/7」、c8fd03f「10/10」不可重放，采信锚点抽样（线1/线5 已核对回归用例存在且断言具体）。
- 上轮未验证面销项：(a) **PG 套件本轮以全新空 scratch 库销项**（DDL 验证通过）；(b) npm registry 行为本轮未触碰、延续；(c) 变异台账延续为 (d)；(d) 真机通道延续为本轮 (c)。

**门禁结论**：无 H、无未声明的 M 级环境盲区 → **不阻断**。已提交部分可发布；未提交部分预审放行（M4 建议在 DeepSeek.md 入库前处置）。

---

## 4. 已验证为「无问题」的关键项

1. **上轮 H1 真修（复现翻转）**：数组 content 现产出 `['text','image','image']`，base64 source 结构逐字段精确、https → url source、`QUJD` 不出现在任何 text block（反计费断言在测试中）；tool_result 数组同样映射；`file:///` 丢弃并 WARNING。
2. **上轮 H2 真修**：endpoint/protocol 变更强制重新提供 key 或 `clear_key`（409 `provider_key_required_for_endpoint_change`），归一口径与 `validate_provider` 一致，绕过面逐一排除；`test` 动作审计落码（不含 key、含 source_ip）；probe 错误只回状态码。
3. **3cb1d40 NUL 修复**：`_CONTROL_RE` 与 engine/fs 三处逐字节同款（保留 \t\n\r）；内存「先清洗再判空」与 PG `CASE WHEN excluded.nickname <> ''` 语义等价；word/nickname 两侧都洗；契约 3 条参数化双跑 + 毒丸批次端到端用例；**PG 臂经全新空库实跑验证**。
4. **群人格（1b16fa7）**：双实现 set/get/clear/list 逐方法语义一致且 contract 参数化锁死；解析优先级与 AGENTS.md 声称一致（6 场景矩阵）；互知声明为固定文本不列群号/人格名（无注入面）；群命令 SUPERUSERS 双层 + 静默 WARNING 含会话键（戳一戳事故教训落实）+ use/reset 只作用本群 + `FinishedException` 最前 raise + priority 阻断不漏消息；web 群号 `isascii()+isdigit()` 负例锁定、两段确认绑定 `persona:group:{gid}`、审计 `mode="persona-group"`、live 即时生效。
5. **小黑盒（c8fd03f）**：hkey 单次 62µs、签名输入与 LLM 无关、GF(2) 固定 4×4 小矩阵；link_id ASCII 显式字符类（全角拒）；SSRF 闸（真实 DNS 解析 + 私网/回环/CGNAT 判定）在请求前且负例断言「未发请求」；围栏三序（头 < 注入 < 尾）与伪装闭合行打散实测；2MB 流式实计数、15s 超时、风控不重试；builtin 只读白名单接入且并行安全。
6. **trigger-miss 入库版（2cf793f）**：与上轮预审版要点逐项一致（`%r` 转义、at_targets 恒等、默认 0 fail-closed、命中不打），入库后 matcher.py 零改动；上轮 L9 的勿分享提示已落 docs/operations.md 与 README。
7. **顶层标签（624e61b + 8af2cd9）**：provider 面板搬移无双入口（设置页只剩跳转行，双向测试锁定）；persona 写面两段确认 HMAC 绑定最终 md 文本、nonce 单次、审计不含正文全文、default 拒删、`_NAME_RE` 路径穿越不可达、正文 ≤20000 后端硬校验（50000 字 → 422）；XSS 面干净（value 赋值 + esc）；8af2cd9 表单 build 一次 + 列表局部刷新的成因分析属实（测试形态失真 = M3，行为本身成立）。
8. **merge 卫生**：三个 merge 无内容丢失（两个平凡 merge + 一个冲突 merge 双向核对）；依赖零变化（pyproject/uv.lock diff 为空）。
9. **PERSONAS_DEFAULT（未提交）**：`default()` 即时 getenv + 未知名回落 md 默认 + WARNING；全部 5 处生产调用点有 None 保护（「无默认人格不崩」成立）；与 assistant.md default 回 true 配套闭环；conftest 隔离顺序正确（session 注入 → function delenv → 用例显式 setenv）。

---

## 5. 与在库报告的衔接复核

| 上轮项（REVIEW-d730827..2ed7c63） | 本轮状态 |
|---|---|
| H1（Anthropic 多模态 str() 化） | 98242dc 修复，**复现翻转确认为真修** |
| H2（key 外带链） | 修复确认为真修（守卫+审计+回显收窄），绕过面逐一排除 |
| M×12 | 全部处置落码（M10 部分修复 7 处 setenv+conftest、M12 记录不重写——诚实声明符合纪律）；残留同型面见 L3/L4/L5 |
| L×28 | 25 条修复（抽样对账全部落码）+ L10/L18/L28 显式遗留入档；L27 修复漏网 1 处 → 本轮 L1 |
| 「M6 内网 endpoint 开关」入档决策 | 未提交预审未见对应实现——**延续挂账**（测试连接的 SSRF 判定仍无 IP 段过滤，威胁模型靠门禁兜底） |
| 上轮未验证面 (a) PG / (b) CI / (c) 台账 / (d) 真机 | (a) **销项**（全新空库实跑）；(b) 延续（网络不可达）；(c) 延续为本轮 (d)；(d) 延续为本轮 (c) |

---

## 6. 修复优先级建议

1. **P1（建议尽快，均为低成本）**：M1（备份复用 `backup_file`）、M2（tmp `os.open` 0600）、M4（DeepSeek.md 处置 + gitignore，**入库前**）、M6（`--write-stats` 一键）。
2. **P2**：M3（措辞降级或补行为用例）、M5（文档清单限定）；L3/L4/L5（M5/M6 同型面收口，一并做）、L6（备份 opener 同款）、L16（default 双口径复查）。
3. **P3**：L2（补无备份回滚回归）、L7（标注形态转义）、L8/L9（披露或前置判定、msg 截断转义）、L13/L14/L15、L17（索引或缓存）、L18/L19（文案）、L21（告警断言）。
4. **L 批量关闭计划（25 条 > 10 触发收敛规则）**：建议本轮修 14 条低成本项——L1（fixture）、L6（opener）、L8（前置判定一行）、L9（截断+转义）、L12（记录即可）、L14（esc 补 `'`）、L15（统一语义）、L16（一行复查）、L20（措辞）、L21（caplog 断言）、L22（边界参数化）、L24（日志替代 pass）、L10（`_skip_depth` 日志）、L11（to_thread 对齐）；**显式遗留候选 11 条**：L3（与 AGENTS.md 例外清单拍板一并）、L4/L5（legacy 过渡形态，随 provider 全面切换后自然消亡）、L7（与名片转义方案一并设计）、L13（窗口极窄）、L17（索引随下次 DDL 批次）、L18/L19（文案随 M5）、L23（e2e 基建决策）、L25（线上可用性随真机验证）。
5. 修复阶段照例产出 `review/FIX-2ed7c63..1b16fa7.md`，每条带回归测试与变异复核；归档前跑 `scripts/check_review_docs.py`。
