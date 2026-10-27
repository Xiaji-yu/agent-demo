# FIX-e55cab5..4d5c1b8

对应 [REVIEW-e55cab5..4d5c1b8.md](REVIEW-e55cab5..4d5c1b8.md)（M1–M6 + L 批）。

## 修复映射

| 项 | 修法 | 回归 / 变异 |
|---|---|---|
| M-1 词云轮询用例依赖 ISO 周奇偶（偶数周必红，main CI 红） | 纯测试修复：用例内把 `week_kind` 桩成 `tech`（类别与用例数据同源），专注断言轮询间隔/候选池本身 | 当日实测红（周 44）→ 修复后绿；移除桩即复现 |
| M-2 收尾 LLM 调用绕过当日预算硬闸 | 收尾前判 `chat_blocked()`，blocked 直接落既有兜底（不再发调用）；顺带 **L5**：`finish_reason=length` 时标注「回答因长度上限被截断」 | `test_final_answer_skipped_when_budget_blocked` / `test_final_answer_marks_truncation`；变异（去掉收尾前判闸）红 |
| M-3 设置总览「思考步数」显示启动期旧值且归 boot 区 | `build_settings` 读 `engine.max_iterations` 运行值 + `max_iterations_effective="live"`；JS 该行移入「即时生效」区 | `test_memory_summary_reflects_live_max_iterations`；变异（还原 call site）红 |
| M-4 英文大写缩写被停用词小写匹配误杀 | `_acceptable`：全大写词（`w.isupper()`）跳过小写停用词匹配 | `test_uppercase_acronyms_survive_stopwords`；变异（去掉 isupper 豁免）红 |
| M-5 posseg 词性表重建窗口静默丢词 | 新增 `_pos_table_ready()`：`word_tag_tab` 为空（重建窗口/未初始化）即降级纯分词（与缺 posseg 同口径），告警一次；`cut`/`_word_pos_ok` 双侧接入 | `test_pos_rebuild_window_degrades_instead_of_dropping`；变异（去掉守卫）红 |
| M-6 ruff pin 三方无守卫 | 新增 `tests/test_toolchain_pins.py`：pyproject pin == pre-commit rev == uv.lock version 三条秒级断言 | 变异（篡改 pyproject pin）2 用例全红 |
| L1 断点续传空头承诺 | 文档注明「不支持断点续传，中断需重走下载确认」（票据单次为设计，未改代码） | —（文档） |
| L2 票据 IP 绑定 fail-open 分支 | 改严格相等：任一侧 IP 为空即不匹配（fail-closed） | 既有 `test_download_ticket_expires_and_binds_ip` 负例仍绿 |
| L3 preview/第一段 live 承诺 | 记录在案（有意保留）：第二段响应与审计始终如实；engine 未装配属启动降级瞬态 | — |
| L4 票据 cookie 的 `Secure` 依赖反代传 `X-Forwarded-Proto` | **补录（L15，REVIEW-4d5c1b8..63f9444）**：原表从 L3 跳到 L6，漏了本行。实现侧 `web.py` 早已同时接受 `request.url.scheme == "https"` 与 `x-forwarded-proto`（`77e21ca` 引入，早于本评审范围），故非「未实现」而是「未记录」；仍是部署侧要求——反代必须在 HTTPS 终结处传该头，否则票不带 `Secure`（票 90s/单次/256-bit 限级） | —（部署要求，见 docs/operations.md「容器化部署」的三条运维注意） |
| L5 收尾调用不检查 `finish_reason==length` | 随 M-2 一并修复（收尾前判 `chat_blocked()` 时顺带标注截断）；原表只写在 M-2 行内，此处补独立行以便按号销项 | `test_final_answer_marks_truncation` |
| L6 熔断后 schema 未收窄 | 触发熔断时重建 `active_schemas`（模型不再看见抓取工具） | fetch budget 用例补 tools 收窄断言 |
| L7 blocked 回包口径 | 改走 `_done`（`skill done` 成对日志）+ sig 统计显式跳过 | fetch budget 用例（停用回包不计入失败/重复） |
| L8 「摘要失败」前缀缺文档/文案误导 | `_FETCH_BUDGET_NUDGE` 中性化（覆盖抓取与摘要两类失败）；`docs/features-messaging.md` ⑤ 补全四前缀 | fetch budget 用例（「不要再调用」断言） |
| L9 收尾 memory 零断言（变异存活） | `test_max_steps_forces_final_answer` 补历史末条断言 | 同用例（删除 memory 写入即红） |
| L10 词云口径变更未落 BACKLOG | 本文档即记录（全词→内容词，理由与口径见 REVIEW 线3；旧数据读取同口径过滤） | — |
| L11 web 词云 Top30 口径分裂未文档化 | features-community（.env.example 注释同源）/features-interaction 补口径 | —（文档） |
| L12 eng 技术噪声（org/src/com） | 记录在案：豁免通道为 `AGENT_GROUP_STATS_STOPWORDS`（tokenize 注释已点名）；词表不预置 | — |
| L13 AGENTS「17 个设置簇」 | 修正为 18（实测 `len(CLUSTERS)`） | —（文档） |
| L14 features-interaction「165 个键」 | 修正为 176（实测 `len(ENV_SPECS)`） | —（文档） |
| L15 `log_text`「唯一出口」注释口径 | 注释改为「对话/用户内容类日志的唯一出口」（运维/配置回显类不在此列） | —（注释） |
| L16 HA alarm 不可达条目 | 有意保留（防御纵深；将来把 alarm 域加入动作表时两段确认已就位） | — |
| L17 usb 脚本 KEEP/MAX_AGE_H 无数字校验 | 照 backup_mirror.sh M11 同款 case 校验（KEEP 拼进 `tail -n +$((KEEP+1))` 算术上下文） | —（脚本无测试基建，与 mirror 同款为先例） |
| L18 feature 在 release/main 双 merge、dependabot 叠 release 线 | 流程提示（feature/dependabot 分支直对 main；release 只收版本收尾），无代码改动 | — |
| L19 `migrate()._validate` 在包装 try 外 | 备查：`_validate` 内部已把 ValueError 转 `MigrationRegistryError`（MigrationError 子类）；仅文件 stat 的 TOCTOU 理论漏网 | — |

## 验收

- 默认套件（ruff 0.17.1 + pydantic 2.14 + websockets 17.3）：全绿（计数见 BACKLOG DOCS-STATS 实测块，`--check-stats` 通过）。
- ruff 0.17.1 `check` + `format --check` 双绿；pyright agentcore 0 errors。
- 变异复核：M-2/M-3/M-4/M-6 四处「改坏必红」实测（M-1 的红即修复前当日实测；M-5 等价于恢复旧分支的丢词行为）。
- RUN_PERF 未跑：本批不触合并/切分/图片缓冲热路径（收尾与熔断仅在步数耗尽/抓取失败路径生效）；perf 基线由下轮例行复测。
- PG 套件：本批未触碰 store/migrations，由 CI 的 pgvector service 覆盖（声明）。
