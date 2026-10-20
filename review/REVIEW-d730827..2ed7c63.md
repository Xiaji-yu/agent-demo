# agent-demo 近期 Commit 评审报告

**评审范围**：d730827..2ed7c63（5 个 commit：af2ed2f 沙箱解压退役 → a4b38dd 视觉回捞/统计白名单 → 9ded5e1 多 provider+运行时切模型 → 484e6ce 17 设置簇 web 面板 → 2ed7c63 docs 同步）+ 未提交预审（`[trigger-miss]` 诊断：matcher.py / test_matcher.py / .env.example / docs/features-messaging.md）
**评审日期**：2026-10-20
**评审方式**：5 个并行子代理分线审查（线1 沙箱归档 / 线2 视觉回捞与群统计 / 线3 多 LLM Provider / 线4 设置簇写入面 / 线5 测试与文档一致性）+ 主代理逐条实证（实跑测试与 lint、2 个 H 亲自复现、4 个存疑 M 复现、声称逐项核对）
**工作区状态**：**不干净**——4 个文件未提交（`[trigger-miss]` 诊断功能及其测试/文档，已并入线2/线5 预审）；评审过程未修改任何受版本控制文件

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | **双 H，阻断发布/部署**：H1 Anthropic 协议把多模态消息整体 `str()` 化（切 provider 即触发：图片静默丢失 + base64 全文按文本计费）；H2 provider「编辑继承 key + test 连接无确认无审计」组成 key 外带链（持 web token 者可窃取全部 LLM 凭据） |
| 线1 沙箱归档（af2ed2f） | 无 H。M×2（tar 扫描期全流解压 CPU 无上限【已复现】、pack_zip 全量读内存）+ L×6。上轮 M×5/L×15 全修核对属实，方案 B 消洞干净 |
| 线2 视觉回捞/群统计（a4b38dd + 双周 + 未提交） | 无 H。M×1（`_IMAGE_REF_RE` 误触发面过宽【已复现】）+ L×4。跨用户隔离、SSRF 链、fail-closed 白名单全部验证通过 |
| 线3 多 Provider（9ded5e1） | **H×1** + M×3 + L×7。key 存储主链路（0600 创建即设/O_NOFOLLOW/不回显/迁移幂等）验证安全；「不回显」契约被 `public().endpoint` 打破（M5） |
| 线4 设置簇写入面（484e6ce） | **H×1** + M×2 + L×5。写面门禁 fail-closed 验证真强制；165 键/17 簇/可写 108+29 数字声称**逐项实测吻合**；两段确认 TOCTOU 防住 |
| 线5 测试与文档（横切 + 2ed7c63） | M×4（AC 虚报回归测试名且 `legacy_model_key` 零覆盖、文档数字失真、环境依赖缺口、名实不符用例）+ L×6 |
| 实跑数据 | 默认套件 **2691 passed / 58 skipped**（55.23s）；ruff check + format 双绿（0.9.6 与 pin 一致）；RUN_PERF 9 passed、基线无劣化；PG 套件未跑（本机无容器，见未验证面） |
| 依赖安全 | **零新增/升级**（pyproject.toml / uv.lock 在 d730827..HEAD diff 为空），无 CVE 面、无 `^`/`latest` |
| 声称核对 | af2ed2f 基本属实（`.git*` 措辞夸大、死代码 2 处残留）；a4b38dd「明确指向图片」与实现不符（→M3）；9ded5e1 声称5 在本 commit 不成立、由 484e6ce 兑现（→M12）；484e6ce 属实；2ed7c63 docs 有虚报（→M8/M9） |

---

## 1. 已实证的问题

### H1｜Anthropic 适配把多模态 content 数组整体 `str()` 化：图片静默丢失 + token/成本爆炸【已复现】

**位置**：`agentcore/llm/protocols.py:156`（`_anthropic_messages`）；引擎侧多模态构造 `agentcore/loop/engine.py:918-925`

```python
_append("user", [{"type": "text", "text": str(content or "")}])
```

**证据（主代理复现）**：构造引擎同款数组 content（text + `image_url` data URI）走 `_anthropic_messages`：输出只有 1 个 text block（`['text']`），无任何 image block；base64 载荷一字不落进入文本块。OpenAI 路径 messages 原样透传不受影响，问题只在 Anthropic 协议路径。

**影响**：用户把主 provider 切到 Anthropic（本增量主打能力）后，vision 默认开启、`AGENT_VISION_KEEP_IN_LOOP` 让图片在 tool-loop 全程重发——①模型完全看不到图（功能静默损坏，回执仍像在识图）；②一张几百 KB 的图 base64 全文按文本计费且每步重发，可击穿日预算硬闸或被上游 400 拒绝；③无需任何攻击者，正常使用即触发。

**修复建议**：`content` 为 list 时按块映射（data URI → `{"type":"image","source":{"type":"base64",...}}`，https → url source）；至少丢弃 image 块只保留 text 块并告警。补「数组 content build」回归（`test_llm_protocols.py` 现无任何数组用例）。

### H2｜provider「编辑继承 key」+「test 连接无确认码无审计」组成凭据外带链【合并逻辑已复现，外发请求为读码确认】

**位置**：`plugins/qq_agent_adapter/web.py:2567-2576`（save 合并）、`:2550-2560`（test 动作）、`agentcore/llm/protocols.py:83,213`（请求头带 key）、`agentcore/llm/providers.py:105`（endpoint 仅校验 http(s) 前缀，无 IP 判定）

```python
elif not str(raw.get("api_key") or "").strip():
    merged["api_key"] = existing.api_key   # endpoint 任意改、key 原样继承
...
if action == "test":   # 不需要 confirm_token/nonce，也不落 _record 审计
    detail = await provider_probe_detail(provider)
```

**证据（主代理复现合并逻辑）**：对既有 provider（key=`sk-REAL-SECRET`）提交 `endpoint=https://evil.example/v1`、api_key 留空 → 保存成功，`p.api_key == 'sk-REAL-SECRET'` 为 True。随后 `{"action":"test","id":"main"}` 即向该端点发出携带真实 key 的 POST（OpenAI `Bearer` / Anthropic `x-api-key`）。`test` 是写面唯一不需要两段确认、也唯一无 `_record` 审计的动作。

**影响**：绕过 AGENTS.md「凭据/URL 刻意不可写、key 只写不回显、凭据走 SSH」刻意构筑的最后防线——web token 泄漏（写面本已用 CIDR+token 控制，威胁模型正是「token 被打穿时限制损害」）即可把 0600 凭据文件里的全部 LLM key 外带。两段确认不构成障碍（持 token 者自己发两段即可）。

**修复建议**：save 时 `endpoint`/`protocol` 相对 existing 变更且未显式提供新 key/`clear_key` → 422/409 要求重新提供 key；`test` 动作补 `_record` 审计（不含 key）；可一并考虑 probe 端点与 provider 现值一致性校验。`tests/test_edit_without_key_keeps_it` 目前把「留空继承」当功能锁定，未意识到与 endpoint 变更的组合，需同步收窄。

### M1｜tar 系包在扫描期就全流解压，CPU 无上限（炸弹防护只护住 zip 侧）【已复现】

`agentcore/skills/archive_skills.py:139-141,207-208`：`tarfile.open(...).getmembers()` 对 gz/xz 流必须顺序解压到底，而条目数上限与累计 size 上限检查都发生在 `getmembers()` 返回之后；第二遍写盘还要再解压一次。主代理复现：20.2 KB gz → 20MB（1016x），`getmembers()` 41.9ms，线性外推大包可占满 CPU 数分钟且 `to_thread` 无超时。仅 superuser+medium 可触发，无逃逸，故 M。**建议**：`_scan_tar` 改 `tf.next()` 惰性迭代、循环体内提前 raise；第二遍复用第一遍成员列表。

### M2｜pack_zip 全量 `f.read()` 入内存，默认上限（1024MB）下 GB 级内存峰值【推演】

`agentcore/skills/archive_skills.py:400-401`。extract 两侧都是 1MB 分块，唯独 pack 全量读。**建议**：`zf.open(zi,"w")` + `shutil.copyfileobj` 分块，或单文件独立上限。

### M3｜`_IMAGE_REF_RE` 误触发面过宽，与「明确指向图片」声称不符【已复现】

`plugins/qq_agent_adapter/pipeline.py:58-66`。主代理复现正则原样：`查看一下地图上的位置`（「图上」子串）、`刚才那个很好笑`、`我发的照片能删吗` 全部命中回捞。命中后：一次 get_msg + 最多 3 张图下载 + 挤占 vision 预算；该用户 900s 内没发过图时更会向 prompt 注入「请让用户重新发送图片」，把无关请求误导向重发图片。回捞严格限同发送者，无跨用户泄漏，故 M。**建议**：收紧「刚才那个」为必须后随图词；「图里/上/中」加前字排除（地图/版图/图书…）；「无带图记录」时静默维持旧行为，仅「有记录但取不到」才提示重发。

### M4｜providers.json 迁移/保存用固定 tmp 名，双进程并发首启可互删凭据文件【推演】

`agentcore/llm/providers.py:264`（`p.name + ".tmp.write"` 无 pid/uuid）+ `:276-283`（校验失败且无 backup 时 `p.unlink`）。最坏时序下两进程先后校验失败、文件最终不存在；`.env` 已清理的部署不自愈（凭据文件静默消失）。窗口小、双进程属部署侧错误，但后果是无声丢失。**建议**：tmp 掺 pid/uuid；无 backup 时校验失败先比对现内容再决定 unlink。

### M5｜base_url/endpoint 回显面超出 AC 记录的例外范围；`model_status()` 自身契约被破坏【已确认】

`agentcore/llm/providers.py:58`（`public()` 含 `endpoint`）+ `client.py:762`（providers 分支整包返回）vs `client.py:738` docstring「不含 api_key / base_url——返回值会经模型转述给用户」直接矛盾；`web.py:176` overview API 把 endpoint 送出响应体；legacy 模式 `LLM_MODEL` 值为 `model@base_url` 且非 secret（`settings_spec.py:248-259`），值中的端点随 `_env_settings_payload`/preview/write 响应出体，`test_credentials_and_urls_never_returned` 断言的子串是键名，对值内嵌 URL 是假阴性；`tests/test_llm.py:969` 甚至断言 `"endpoint" in p` 把反方向锁进测试。AC-settings-clusters-llm.md B2 记录的例外是「仅 `llm.providers` 面、仅 web 管理员」，实际面更宽，AGENTS.md「唯一例外：日志查看面」未同步。**建议**：`public()` 不回完整 endpoint（回 netloc 或布尔）；`model_status` 拆出 providers；例外范围在 AGENTS.md 显式改写；`test_llm.py:969` 断言方向跟随修订。

### M6｜「测试连接」SSRF：无 IP 段判定 + 错误回显上游响应体 + 无审计【推演（链路读码确认）】

`model_switch.py:73-75`（`body = e.response.text[:120]` 回显进 409 detail）+ `client.py:365-378`（对任意 http(s) 端点发 POST，无 runner.py 式私网/回环/链路本地/保留段/`100.64.0.0/10` 判定）。与 `.env` 时代配内网 LLM 的能力等价故不算新增强逃逸，但组合「无需确认 + 无审计 + 响应体回显」后，持 token 者获得对 bot 进程网络位置的半盲内网读取（如云元数据端点）。已核实 httpx 默认不跟随重定向，无重定向护栏问题。**建议**：`test` 补审计（与 H2 同批）；错误回显只留状态码+域名；如需保留内网 LLM 合法用例，做 provider 级显式开关而非不设防。

### M7｜gate 判定与实际生效值两种口径分歧【解析已复现，dotenv/systemd 侧为推演】

`web.py:561-575`（`_gate_value`）+ `config_write.py:194-205`（`parse_env_value` 不剥行内注释）。主代理复现：`.env` 写 `AGENT_GROUP_STATS=0  # 暂停周报`（docs/features-community.md 的示例就是这种形态）→ `parse_env_value` 返回整段 `'0  # 暂停周报'` → gate 判「开」，而 dotenv 实际生效 `0`——簇 409 保护与页面显示双双失真。另 `_gate_value` 读文件优先于 os.environ，systemd `EnvironmentFile` 部署形态下会得出相反结论（本仓库 README 主推裸 nohup/docker，影响面有限）。**建议**：`parse_env_value` 按 dotenv 口径剥 ` # ` 行内注释；文档写明 gate 以文件为准的边界。

### M8｜AC 文档虚报回归测试名；`legacy_model_key` 修复零测试覆盖【已复现】

`review/AC-settings-clusters-llm.md:169` 以「已执行回归」口吻点名 `test_llm_model_is_legacy_and_readonly_when_providers_exist`——主代理 grep + `git log --all -S` 证实该测试全仓不存在；`grep -rn legacy_model_key tests/` 零命中，即 web.py:648 的拒写校验（9ded5e1 声称的修复）删掉后全量套件照样绿。**建议**：补测试（providers 存在 → POST LLM_MODEL → 409 `legacy_model_key`；不存在 → 仍可写），或 AC 改口承认无回归。

### M9｜features-interaction.md「可写 105 个 .env 键」与实测 108 不符，同 commit 文档自相矛盾【已复现】

`docs/features-interaction.md:304` 写 105；主代理实测 `ENV_SPECS` 165、可写 **108**（与同 commit 的 AGENTS.md「108 env」、AC §8「108」矛盾）。105 是 484e6ce 时点的过期数字，2ed7c63 漏更。**建议**：改 108 或改「由 schema 守卫锁死」口径。

### M10｜新增用例隐式依赖 `AGENT_GROUP_CONTEXT` 本机 .env（本地红 CI 绿）【已复现（无显式 setenv）+ 推演（gate 路径）】

`tests/test_matcher.py` TestGroupRecorderImageContext、`tests/test_pipeline.py` TestGroupHistoryImageFollowup 等约 7 个新用例未 `monkeypatch.setenv("AGENT_GROUP_CONTEXT", "1")`，而记录/回捞都 gate 在 `context_enabled()`（读该键）；conftest 只剥 DATABASE_URL 与 `AGENT_GROUP_STATS*`。本地 `.env` 关闭群上下文（真实部署合理选择）时这些用例红而 CI 绿——#91「测试环境必须用例内显式给」纪律的残留。**建议**：用例内显式 setenv；更根本的是 conftest 改默认剥离 `.env` 注入（需一并设计 `_normalize_superusers_env`）。

### M11｜`test_switch_by_unique_prefix` 名实不符：唯一前缀分支零覆盖【已复现】

`tests/test_model_switch.py:404`。主代理读用例：实参 `"m-b"` 命中 exact model 分支、`"a"` 命中 id 精确分支，`_find_provider` 的唯一前缀分支（传 `"m-"` 应唯一命中）与歧义分支（传 `"m"` 应报错列全表）都无测试。commit 与 AC 都声称「provider 匹配扩展到唯一前缀」并点名此测试为回归——回归并未钉住声称的分支。**建议**：改传真前缀与歧义实参。

### M12｜9ded5e1 声称5「迁移后 LLM_MODEL 标 legacy 并拒写」在该 commit 内不成立（由 484e6ce 兑现）【已确认】

`git show 9ded5e1:plugins/qq_agent_adapter/web.py` 无 legacy；9ded5e1 时点 web 仍可写 `LLM_MODEL` 且写入静默无效。484e6ce（3 秒后提交）兑现，HEAD 终态已符（主代理读 web.py:416-424 确认）。按「夸大即记 M」立案；考虑到三连提交同批落地、终态兑现、无中间消费窗口，建议裁定为拆分提交的时序瑕疵，但后续 commit message 应如实反映当 commit 状态。

### L 级（28 条，压缩列出；>10 触发 §3 收敛规则，批量关闭计划见 §6-4）

| # | 位置 | 一句话 |
|---|---|---|
| L1 | archive_skills.py:51 + commit/FIX/docs 措辞 | 「拒 `.git*`」实为三个精确段名，`.gitignore`/`.github` 放行（`.git` 段已拒故无实际逃逸，纯声称夸大） |
| L2 | archive_skills.py:191-204,218-220 | 产物 0600 非「创建即」，存在 0644 窗口（无执行位，危害限同机其它用户可读） |
| L3 | runner.py:213,186-213,385-388 | 死常量 `_MAX_TAR_MEMBERS` + 描述已删白名单的悬空注释（「死代码删净」轻微夸大） |
| L4 | engine.py:42-49 vs registry.py:266 | `_REGISTRY_ERROR_RE` 漏 `Error: prompt skill 执行失败` 模板，该类失败被耗时视图计为成功（纯观测回归） |
| L5 | archive_skills.py:383-401 | `pack_zip(source=".")` 把输出 zip 卷进自身（自引用/损坏）；测试只覆盖子目录 |
| L6 | archive_skills.py:191-203 | 「防谎报 file_size」的写盘期复核防线无用例锁定（变异删掉它现有用例不红） |
| L7 | groupstats/config.py:100-101 + stats_route.py:232 | 「采集仍按 ALLOWED_GROUPS」注释漂移（新收窄键后不成立）；空集 WARNING 文案未覆盖新情形 |
| L8 | pipeline.py:880-898 | 回捞拿到元数据但图片全取不到时缺「请重发」兜底（对比引用路径有占位提示） |
| L9 | matcher.py:106-117 + README/operations | `[trigger-miss]` 落正文 160 字，「勿分享截图」提示未覆盖该行（`%r` 转义已防日志伪造） |
| L10 | web.py:314-351,404-407 | 双周取数每群 5 查询串行 ×2 窗口；上周数据双份序列化（可 gather 并行） |
| L11 | client.py:365-378 | `probe_provider` 不上报 usage，违反 legacy probe「探测是真花钱的请求，记账不该漏」口径 |
| L12 | .env.example:50-52 | `LLM_FALLBACK_*` 注释未提「provider 文件存在后仅作迁移源、运行期忽略」，照配即静默失效 |
| L13 | model_switch.py:117 + admin.py 异常日志 | `/model` 错误回执/日志原样回显用户输入，手滑 `provider|sk-real-key` 可把 key 带进群聊 |
| L14 | providers.py:261 + config_write.py:254 | 备份 `shutil.copy2` 继承源权限，无 0600 收紧（源被手工 chmod 0644 后备份持续 0644 且含 key） |
| L15 | client.py:656-658,743-765 | 全部 provider 停用时 `chat()` 静默回落 env 旧路径，`model_status` 仍报 provider 模式（状态与路由撕裂） |
| L16 | model_switch.py:254-256 | provider 模式 reset 不探测就落盘，与 legacy 分支不对称（回执显示已恢复但目标可能不可用） |
| L17 | config_write.py:272-278 | `atomic_write` 写失败不清理 tmp，残留含完整新 `.env`（含 key）的半成品（对照 providers.save 有清理） |
| L18 | config_write.py:177-181,281-283 + providers.py:280-288 | `restore_file` 非原子（崩溃留半个文件）；patch 失败分支尚未写盘也 restore，可回退第三方改动 |
| L19 | config_write.py:201-218 | `export KEY=` 前缀行不识别 → append 重复键 → 后续写入永远 409（duplicate_key_lines），只能 SSH 清理 |
| L20 | web.py:2654-2659 + client.py:429 | delete 自动换主兜底不过滤 enabled；全停用后 `provider_chain()` 为空 → `assert last_err` 每轮聊天 AssertionError |
| L21 | providers.py:151 | `bool(raw.get("enabled", True))`：直调 API 传字符串 `"false"` → True（前端不发字符串，不触发） |
| L22 | settings_spec.py（GROUP_STATS_RETENTION_DAYS / IMAGE_HOSTS / MUSIC_AUDIO_HOSTS 可写） | 与 AGENTS.md「数据保留期 / SSRF 白名单不可写」承诺字面冲突，需拍板：收窄承诺措辞或补 `writable=False` |
| L23 | AC-settings-clusters-llm.md:123 | 「16 簇」vs 实际 17（同文档 §1/§8/测试断言均为 17，笔误） |
| L24 | AC §「交互增强（后续）」+ BACKLOG 遗留段 | 与 HEAD 实现脱节（模态表单/页内确认已实现、prompt() 已移除），误导后续排期 |
| L25 | BACKLOG.md 设置簇条目 | 「161 键/105 env/38 只读」为落地时点数字，无时点标注，与头部 DOCS-STATS 并存易误读 |
| L26 | tests/test_archive_skills.py:366 | `or reason` 使文案断言退化为「非空」，锁不住文案（核心断言 `not ok` 有效） |
| L27 | test_llm_providers.py:158 等 4 处 | symlink 用例直接 `os.symlink`，未用现成 `symlinks_supported` fixture（平台假失败，非假通过） |
| L28 | tests/test_matcher.py:437 | 计时断言依赖真实 sleep 差值（≥0.15s），高负载机器 flaky 风险 |

---

## 2. 被证伪的发现（含存疑）

**线1（7 项）**：zip 非常规条目（FIFO/设备）逃逸——zipfile 不重建特殊文件、chmod 固定 0600；scan→extract TOCTOU——工作区写入通道都在引擎串行 loop 内；dest 覆盖越权——resolve 后必须严格在工作区内；URL 编码文件名逃逸——条目名不解码、无路径语义；嵌套 archive 重入——产物 0600 普通文件、重入须完整重过防线；pack_zip 打包工作区外文件——resolve 越界拒 + symlink 跳过（hardlink 需管理员手工介入）；`mode=ping` 兜底误标——failed 行已在，total 无丢失。

**线2（5 项）**：回捞可取他人图片——`latest_image_message` 硬性 uid 匹配 + 跨用户反例用例锁定；followup 测试被前用例污染——autouse `_fresh_buffer` 每用例重建（子代理先复现后自行撤回）；raw_message 换行伪造日志——`%r` 转义；本周窗口漏当天——闭区间 `<= end`；新键漏注册红 schema 锁——`settings_spec.py:1663` 已注册。

**线3（7 项）**：孤儿 tool_result → Anthropic 400——`_sanitize_history` 上游已消毒；错误路径带 key 进异常/日志——逐一核对 `_post_raw`/load/save/回执均不内插 key；切换 in-flight 撕裂——快照语义保证旧请求旧配置（仅存极苛的理论取消窗口）；迁移后 `.env` 明文残留——刻意设计（损坏回落路径依赖）且相关键 `secret=True, writable=False`；fallback 链与多 provider 断裂——迁移把主/池/fallback 全迁成条目、语义保留；链长拖垮时延——与 legacy 同纪律非新增；声称 1-4 全部兑现无夸大。

**线4（6 项）**：provider CRUD 是未记录的「变相写凭据」旁路——AC B2 已显式承认（真正问题收敛为 H2/M5）；endpoint 回显未记录——对 providers 面证伪、对 overview/LLM_MODEL 两处成立（并入 M5）；yaml load RCE / 整文件重写抹注释——`safe_load` + 文本级补丁有专测；非 ASCII 入参裸 500 回归——token/nonce/id 均 ASCII 预检或白名单；新增开关静默关停功能——gate 关闭时 skill 返回明确文案；探针重定向护栏问题——httpx 默认不跟随。

**线5（6 项）**：test_llm `_pool()` 依赖本地 env——`_LLMCfg` 有默认回落且涉值用例显式 setenv；TestClusters 先写 envf 被 `_mount_write` 覆盖——两者内容相同；conftest `_isolate_llm_providers` 不足致真实 key 进测试——function 级 autouse 全部落 tmp_path；AC 实跑数字（2683+58 等）虚报——时序与量级自洽、未见内部矛盾，唯一可判虚报的回归测试名已列 M8；`[trigger-miss]` INFO 生产不可见——与既有 `[msg]`/`[raw-dump]` 同级同 logger；`test_rejects_malformed` 恒真——逐参数核对各有独立校验行。

**存疑（unknown，主代理裁定不立案但记录）**：线2 SnowLuma 协议端「群历史可 get_msg 回捞」的真实可用性（取不到时代码走「请重发」分支，fail-safe 无害）；M3 误触发的线上真实频率（仅字符级仿真佐证）；M7 systemd EnvironmentFile 形态的实际部署占比。

---

## 3. 测试与文档状况

**实跑数据（主代理，本工作区含未提交变更）**：

| 项 | 结果 |
|---|---|
| 默认套件 | `pytest -q` → **2691 passed / 58 skipped**（55.23s） |
| ruff check | All checks passed（0.9.6，与 pyproject pin 一致） |
| ruff format --check | 197 files already formatted |
| RUN_PERF | 9 passed；`perf_baseline.py` 无劣化（qq_plain 1.11x、split 1.01x，阈值 1.5x） |
| PG 套件 | **未跑**——本机无 postgres/pgvector 容器（见未验证面 (a)） |
| CI 状态 | **未核实**——GitHub API 403 限流（同上轮 (a) 现象）（见未验证面 (b)） |

**文档一致性**：AGENTS.md 数字声称（165 键/17 簇/可写 108 env + 29 yaml）经主代理实测**逐项吻合**；`YAML_SPECS` 35/29 吻合；新增 9 个 env 键全部双侧登记（schema 等式守卫在位）；零新增依赖。失真项：M9（105）、L23（16 簇）、L1（`.git*` 措辞）、L24/L25（后续/遗留脱节与无时点数字）。

**未验证面清单（§8.3 门禁口径）**：
- (a) **PG 套件**：本轮 5 commit 未触碰 memory/store、备份恢复路径与 DDL（风险低），但按清单惯例显式声明：具备环境时补跑 `TEST_DATABASE_URL` 全套件。
- (b) **CI 状态**：API 限流未核实；HEAD 2ed7c63 的 CI 结论待查（本地全绿 + lint 双绿 + 零依赖变化，规则性失败风险低）。
- (c) **H2 的真实外发请求**：评审不执行凭据外带；合并逻辑已内存复现、probe 请求构造（URL/头）读码确认，链路完整。
- (d) **真机 QQ 通道**：图片回捞依赖协议端 get_msg 对群历史消息的支持（线2 存疑）；`[trigger-miss]` 的线上日志量级（每条未触发群消息一行 INFO，默认 0 关闭）。
- (e) **M7 的 systemd 形态**：gate 读文件 vs env 层分歧的实际影响取决于部署形态。

**上轮未验证面销项**：(a) H1 端到端——af2ed2f 方案 B 整体退役外部解压工具，「消洞而非堵洞」，线1 复核解压入口唯一、白名单同源、被删符号全仓无引用，**销项**；(b) npm 真实 registry 行为——本轮未触碰该路径，继续降级记录；(c) commit 自称变异复核台账——信 af2ed5f「5/5 被抓」台账，但线1 L6 发现新防线（file_size 复核）无用例锁定，提示该类覆盖仍有缺口；(d) 真机通道——延续本轮 (d)。

**门禁结论**：**存在 H → 阻断发布/部署直至 H1、H2 修复并复验**；未声明盲区无 H；M1-M12 不涉及无法本地复现的环境面（M4/M6/M7 为推演级但链路读码确认，具备环境时优先补验 M6 内网读取面）。

---

## 4. 已验证为「无问题」的关键项

1. **key 存储主链路（线3）**：`save` 创建即 0600（`os.open` 显式 mode + O_NOFOLLOW + os.replace）、symlink fail-closed、写后重读校验失败回滚、`public()`/`model_status` 事件/审计/回执均无 key；`git check-ignore` 实证凭据文件与备份被 .gitignore 覆盖；`ls -la data/` 实测 `-rw-------`。
2. **迁移逻辑（线3）**：幂等（文件存在不覆盖）、失败 fail-open 回落 env + warning 留痕、`ensure_migrated` 先于 `get_shared_llm_client`、conftest 把 provider 文件关进 tmp 防真实 key 进测试。
3. **协议适配（线3，除 H1 外）**：system 提升、tool_calls↔tool_use、连续 tool_result 合并、stop_reason/usage 映射——11 例断言具体非恒真。
4. **写面门禁（线4）**：provider CRUD/test/设置写/reboot 全部在 `_write_enabled()` 块内注册，无路由绕过；未配 token 整面不挂载（负例测试断言路由不存在）；token 仅 Bearer + `compare_digest` + ASCII 预检；CIDR 复用既有 `_ip_allowed`。
5. **两段确认强度（线4）**：HMAC 绑定 nonce|key|最终值|30s 窗口、nonce 单次消费（跨窗重放回归在位）、provider 确认绑定全量 `value_text`（TOCTOU 防住）、页内 modal 无原生 confirm/prompt（有测试）。
6. **审计+备份全覆盖（线4）**：设置写/provider 写/reboot 均有 `_record`（含 source_ip+nonce，值经 masked/preview 口径）；三介质 symlink fail-closed → 纳秒后缀滚动备份 → 原子写 → 写后验证失败还原。
7. **.env 补丁正确性（线4）**：CRLF 全文件字节保留、末尾补齐、值含 `=`/空格保留、引号/#/换行注入拒绝、同键重复 409（export 前缀缺口见 L19）。
8. **回捞安全（线2）**：跨用户隔离（反例用例锁定）、SSRF 链不降级（https+域名白名单+IP 禁段+30s deadline+20MB 上限）、无新注入通道（get_msg 文本丢弃、notes 固定文案）、缓冲有界（10 行环形 + TTL 900s）。
9. **群统计收窄（线2）**：空集回落 ALLOWED_GROUPS（空集=不采集 fail-closed）、越界 `wanted & chat_allowed` + WARNING、非 ASCII `isascii() and isdigit()` 剔除、采集侧硬闸同一集合、双周窗口经 `agentcore.tz.today_date()` 统一时区、前端全部 `esc()` 转义。
10. **extract 防线主体（线1）**：两遍法零落盘拒绝、穿越/绝对/UNC/盘符/反斜杠归一全拒、链接与设备条目 tar 白名单拒绝、zip 声明 size+实际字节双重上限、权限双层（注册期+handler）、输出恒过围栏、user_id 双层防伪造。
11. **registry positional-only 修复（线1）**：主代理确认修复前 `execute(..., name=...)` 必在绑定期 TypeError、回归走 registry 端到端修复前必红；线上受害面确为 run_build_script。
12. **上轮 M×5/L×15 全修落码（线1）**：逐条 diff 核对 FIX 第二批表，均有代码与测试落点；L8 随防线退役销项成立。
13. **未提交 `[trigger-miss]`（线2/线5 预审）**：开关默认 0 fail-closed、`%r` 转义防日志伪造、实现/.env.example/docs/测试四方一致、`at_targets` 重构与旧 `any()` 数学恒等——预审放行（隐私提示补一句见 L9）。

---

## 5. 与在库报告的衔接复核

| 上轮项（REVIEW-6745556..d730827） | 本轮状态 |
|---|---|
| H1（unzip 通配符旁路 → 0777 落盘） | af2ed2f 方案 B 整体退役外部 zip/unzip/tar，标准库实现独立复核无同型面 → **真修（消洞）** |
| M×5（symlink 守卫/错误态判定/total 前移/npm 登记/MAX_WAIT WARNING） | 线1 逐条核对全部落码且有回归 → 真修 |
| L×15（L8 随防线退役销项） | 全部处置属实；新发现 L1（`.git*` 措辞）为该批文档同步的残余夸大 |
| 上轮未验证面 (a)(b)(c)(d) | (a) 销项；(b)(c) 降级延续（L6 提示台账覆盖仍有缺口）；(d) 延续为本轮 (d) |

**与 AC-settings-clusters-llm.md 的衔接**：AC 的能力声称主体兑现（17 簇/多 provider/门禁/两段确认均有对应实现与测试）；虚报项 M8（回归测试名）、失真项 M9/L23、脱节项 L24 ——AC 文档需按本轮勘误修订。

---

## 6. 修复优先级建议

1. **P0（阻断发布，修复+回归后复验）**：
   - H1 Anthropic 多模态映射 + 数组 content 回归；
   - H2 key 继承与 endpoint 变更互斥（或要求重新提供 key）+ `test` 动作审计 + `test_edit_without_key_keeps_it` 收窄。
2. **P1（与 P0 同批）**：M5（`public()` 去 endpoint + model_status 拆分 + AGENTS.md 例外改写 + test_llm.py:969 反转）；M6（SSRF 判定/错误回显收窄，审计随 H2）；M7（parse_env_value 剥行内注释）；M8（legacy_model_key 回归测试 + AC 勘误）；M12 随 P0 修复顺带在 commit message 层面勘误。
3. **P2**：M1（tar 惰性扫描）、M3（正则收紧+静默策略）、M4（tmp 掺随机）、M2（pack 分块）、M9/M10/M11（文档数字、setenv、前缀用例——均低成本）。
4. **L 批量关闭计划（28 条 > 10 触发收敛规则）**：建议本轮修 17 条低成本项——L1（文档措辞）、L2（`open(..., opener=...)` 一处）、L4（RE 加一备选）、L6（补谎报 size 用例）、L9（README/operations 一句）、L11（probe 记账）、L12（注释一句）、L13（回执打码）、L14（备份后 chmod）、L17（try/except unlink）、L19（locate 兼容 export 前缀）、L20（换主过滤 enabled 一行）、L21（归一化一行）、L23（笔误）、L26（删 `or reason`）、L27（换 fixture）、L12 并 L25（BACKLOG 时点标注）；**显式遗留候选 11 条**：L3（死代码清理挂BACKLOG）、L5（source="." 自卷，触发面窄）、L7（文案同步随 M 类批量）、L8（折中提示随 M3）、L10（并行化）、L15（随 M5 拆分一并）、L16（reset 探测对称性）、L18（restore 原子性）、L22（需维护者对「保留期/白名单可写」拍板）、L24（随 AC 勘误）、L28（计时写法）。
5. 修复阶段照例产出 `review/FIX-d730827..2ed7c63.md`，每条带回归测试与变异复核；归档前跑 `scripts/check_review_docs.py`。
