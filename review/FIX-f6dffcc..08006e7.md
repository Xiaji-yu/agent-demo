# agent-demo 评审修复记录

**对应评审**：[REVIEW-f6dffcc..08006e7.md](REVIEW-f6dffcc..08006e7.md)（条目编号沿用该报告 §1）
**修复范围**：该报告 §1 的 **H1、H2 + M1–M14**；另含一条评审未列出的同类注入面（见 §三）
**验证结果**：`pytest` **765 通过 / 32 跳过 / 0 失败**（修复前 715/32；收集数 797）；`ruff check agentcore plugins tests bot.py scripts` **全绿**
**L 级**：本轮**不修**，仅在 §四 记录可低成本闭环的项，留给下一轮评审决定优先级

---

## 一、按条修复

### H1 `--replace` 先删后写 → 重灌失败即永久丢知识【已修】

| 项 | 内容 |
|---|---|
| 修复方式 | `_process_file()` 抽出单文件流程，顺序改为 **先 `add_file` 写入成功、再 `delete_source` 删旧**；写入抛异常时直接返回并打印「旧来源 #id 未删除，原有内容仍在库中」；删除失败只告警（库中留重复，不丢数据），提示重跑或 `/kb forget #id` |
| 附带 | `--replace` 的同名判重从 `{name: s}` 改为 `_latest_by_name()`（`setdefault` 保留最新一条）：历史同名多条时旧记录会覆盖新记录、判重依据错位 |
| 改动文件 | `scripts/ingest_kb_samples.py`（+ docstring/`--help` 文案）、`plugins/qq_agent_adapter/admin.py`（同一判重写法） |
| 回归测试 | `tests/test_ingest_script.py`（8 例，其中 3 例为顺序断言：`add` 失败 ⇒ **不得**出现 `delete`；成功 ⇒ 调用序列必须是 `['add','delete']`；`delete` 失败 ⇒ 只告警且仍计为导入） |
| 变异验证 | 把删除移回 `add_file` 之前 → 3 例失败（`test_add_failure_keeps_old_source` / `test_replace_writes_new_before_deleting_old` / `test_delete_failure_only_warns_and_counts_as_imported`） |

### H2 `file` 段文件名经 notes 回显到围栏**外**【已修】

先答复了「会不会影响此前调好的引用回复」：**不会**——两个通道分离。

| 通道 | 位置 | 处理 |
|---|---|---|
| 引用内容主体（含 `[文件：x.jpg]` 占位，来自 `text_from_segments`） | `fence_untrusted` **围栏内** | **原样保留**（模型已知其为不可信数据） |
| 图片出处备注 `[图片N 来自引用/转发消息（文件名）]` | 所有围栏**之外**（系统提示位置） | 清洗：`_display_filename()` 只留 basename + `\w`/`.`/`-`，长度 ≤40 |
| 图片下载/识图（vision） | 独立通道 | 不受影响 |

- 同时加固 `_display_url()`：去掉空白/控制字符与 `[]<>`` `（URL 路径同样是用户可控的）。
- 回归：`tests/test_pipeline.py::TestH2UntrustedEchoSanitizing`（5 例，含端到端「恶意文件名不出现在围栏之后」与「引用主体仍在围栏内」的双向断言）。

### M1 `AGENT_KB_MAX_CHUNKS_PER_SOURCE` 在默认部署下完全无效【已修】

- `service.py` 新增 `_resolve_max_chunks()`：优先级 **env > `config.yaml` > ingest 默认 200**，与本文件已有的 `AGENT_KB_ENABLED` / `AGENT_KB_DIGEST_CRON` 一致；env 非法时告警并回退 config 值。
- 回归：`tests/test_rag.py::TestMaxChunksWiringM1M2`（6 例，含 env 经 `KnowledgeBase` 一路传到 `ingest` 实际 limit 的端到端断言）。

### M2 `max_chunks_per_source` 未校验：脏值崩启动、负值静默丢整篇【已修】

- `ingest.py` 新增 `_coerce_positive()`；`max_chunks_per_source()` 与 `ingest_text(max_chunks=...)` 共用，负值/脏值 → 告警 + 回退默认（此前 `-5` 会 `all_chunks[:-5]` 切出 0 块）。
- `if not chunks:` 分支改为回报**真实** `chunks_total` / `dropped`（此前写死 0，与上一行 WARNING 自相矛盾）。
- 回归：同上类目（`"abc"` / `-5` / `0` / `1.5` 构造期不抛异常；`max_chunks=-5` 仍能入库）。

### M3 `KB.delete_source()` 未受 `AGENT_KB_ENABLED` 门控【已修】

- `service.py::delete_source` 加 `_require_enabled()`；`admin.py` 的关闭态守卫集合补 `"forget"`（提示文案改为「写入/删除类操作不可用」）；脚本 `_prune` 逐条兜异常，避免关闭态下整个脚本带栈退出。
- 回归：`tests/test_rag.py::TestKbDisabledL7::test_disabled_kb_rejects_delete`、`tests/test_admin_import.py::TestKbDisabledGuardsDeleteM3`（变异验证：守卫集合去掉 `"forget"` → 用例失败）。

### M4 段数超过节点上限回落逐条：默认配置下 1456 字 → 42 条【已修】

- 新增 `_repack_chunks()`：段数 > `AGENT_REPLY_FORWARD_MAX_NODES` 时先**均匀重打包**到至多节点上限段（只拼接，不丢字符不重排），随后照常走卡片/逐条分支，并打 WARNING 给出配置建议。
- **一条回复最多产生「节点上限」条消息**，与是否开启转发无关。
- 关于「人格里加限制能否明显改善」：能**降低触发频率**（要求简短 ⇒ 段数变少），但**不能替代代码修复**——模型不保证遵守、长回答是合理需求、且根因在投递层。已在回复中说明，本轮按代码修复。
- 回归：`tests/test_outbound.py::TestChunkCountOverflowM4`（3 例：重打包成卡片、关转发时条数仍有界、重打包保字符）。
- 变异验证：去掉重打包分支 → 2 例失败。

### M5 陈旧图片跨消息复用【已修】

- `RecentImageBuffer.clear()` 新增；`_build` 的复用段改为 `if had_image_segments: put(...) / else: clear(...)`——本条带图段却一张都没取到时**清空缓存**，下一条纯文本消息不再复用更早的图。
- 回归：`tests/test_pipeline.py::TestM5StaleImageReuse`（2 例，复现 P0→P1(失败)→P2 三连）。

### M6 超时/断连（结果未知）后仍降级重发全文【已修】

- `_send_file()` 改**三态**（`FORWARD_OK/FAILED/UNCERTAIN`）：群聊 `upload_group_file` 超时/断连 → `UNCERTAIN`；私聊 `send_markdown_file` 新增 `FILE_SEND_UNCERTAIN_PREFIX`，内部把超时压成不确定标记而不是普通失败串（`file_sender` 的 NapCat HTTP 分支同样处理，不再降级到 OneBot 路径重发一遍）。
- `deliver_reply()` 见 `UNCERTAIN` 立即返回新模式 **`file-unconfirmed`**，不再走文本分层。
- 判据统一：`_is_uncertain_failure()` 改为转发 `file_sender.is_uncertain_send_error()`（全仓唯一实现，避免两处规则漂移）。
- 回归：`tests/test_outbound.py::TestFileUncertainM6`（4 例）、`tests/test_file_sender.py`（2 例）。
- 变异验证：群文件分支去掉不确定判定 → 用例失败。

### M7 群聊改发文件无开关【已修】

- 新增 `AGENT_REPLY_FILE_IN_GROUP`（默认 **1**，保持既有行为，避免无声改变部署表现）：设 `0` 时群聊超长回复跳过群文件、回落合并转发卡片；私聊不受影响。README 与 `.env.example` 写明取舍（群文件长期留存、可能只有管理员能上传）。
- 回归：`tests/test_outbound.py::TestGroupFileSwitchM7`（3 例）。

### M8 私聊文件名是死变量【已修】

- `send_markdown_file(str(ident), text, filename=filename, bot=bot)` 真正传参；回归断言 `filename == "reply.md"`（`TestFileUncertainM6::test_file_send_uses_declared_filename`）。

### M9 `extract_forward_id` 对非 dict 的 `data` 直接 `.get`【已修】

- `forward` 分支加 `isinstance(data, dict)` 护栏并告警跳过。回归：`tests/test_media.py::TestM9M11MediaRobustness`。

### M10 WARNING 把 json 卡片原始正文写进日志【已修】

- 只记 `view`/`app` 与正文**键名**，不落正文；`_looks_like_forward_card()` 改为按解析后的 `view`/`app` 判定（新增 `_forward_card_markers()`），与 `extract_forward_id` 判据一致——普通分享卡片（标题含 "Forward"）不再误记。

### M11 `_coerce_segments(dict)` 返回 key 列表【已修】

- 增 `if isinstance(body, dict): return [body]`。回归：node 的「单段 dict content」不再变成空文本。

### M12 get_msg 兜底：`raw_message` 从未使用、取 bot 不带 self_id【已修】

- `_resolve_reply()` 在 `event.reply` 解析为空时**先试 `reply.raw_message`**（CQ 串，`_coerce_segments` 已支持），能取到就不再多打一次 `get_msg`；`_try_get_bot(self_id)` 接收并传递 `self_id`（多账号下不再用别的账号拉取）。
- 回归：`tests/test_pipeline.py::TestM12QuoteFallback`（2 例，含 `seen == ["botA"]` 的取 bot 断言）。

### M13 测试边界缺口（变异存活）【已修】

| 此前逃逸的变异 | 现在 |
|---|---|
| 「≤3 段逐条」的 `>` 改成 `>=` | 被 `test_three_chunks_stay_sequential_and_four_become_card` 抓住 |
| 文件阈值 `>` 改成 `>=` | 被 `test_file_threshold_is_strictly_greater` 抓住 |
| 群文件载荷改成 `base64://AAAA` / `name="WRONG.md"` | 被 `test_group_file_payload_is_complete_and_named` 抓住 |

新增 `tests/test_outbound.py::TestLayerBoundariesM13`（3 例，含 base64 解码回原文与文件名断言）。

### M14 `BACKLOG.md` 测试数字失真【已修】

- 按基线 `f6dffcc` 实测（`git worktree` + `python -m pytest --collect-only -q`）：**668 收集 = 636 通过 + 32 跳过**；测试文件 **28 个 / 9.3k 行**；核心代码 `wc -l` 实测 **11159 行**。原「700 收集：668 通过」「26 个文件 7.7k 行」「~10.0k 行」均订正，并加注「数字按基线 commit 实测，勿手写估算」。

---

## 二、验证与回归

- 全量：`765 passed / 32 skipped / 0 failed`（收集 797）；`ruff check agentcore plugins tests bot.py scripts` 全绿。
- 本轮共 **4 次变异验证**（H1 顺序、M3 守卫集合、M4 重打包、M6 不确定判定、M13 三处边界），全部被新测试捕获。
- 测试新增：`tests/test_ingest_script.py`（新文件）+ 6 个既有文件的用例；未改动既有用例的断言语义，仅 `tests/test_pipeline.py` 中 6 处 `_try_get_bot` 桩随签名变更改为 `lambda self_id=None: ...`。
- **未验证面（诚实声明）**：
  - (a) 群文件真实链路上限/权限（NapCat 是否允许 `base64://` + 大文件）仍未经真实环境冒烟——本轮只覆盖到「调用形状与不重发」；
  - (b) `AGENT_REPLY_FILE_IN_GROUP=0` 后超长群回复改走卡片，卡片的**真实**节点上限由协议端决定，未做线上验证；
  - (c) `is_uncertain_send_error` 对非 nonebot 封装层的连接断开异常（消息文本不含 timeout 且类名不匹配）仍可能判为「确定失败」→ 极端情况下重发一次，属已知残余。

---

## 三、评审未列出、本轮一并修复的同类问题

### N1 围栏可被内容提前闭合（fence escape）

- **发现**：实现 H2 时核对 `fence_untrusted()`，发现围栏只靠「`----- X开始…-----` / `----- X结束 -----`」两行划分，**内容里自带同形状的一行即可提前闭合围栏**，其后文字落到围栏之外（与 H2 同一注入位置）。
- **修复**：`agentcore/safety.py` 新增 `_neutralize_fence_lookalikes()`，把内容中形如分隔线的整行打散（`- - - - -`），只动这一种形状，不影响 markdown 水平线等正常写法。
- **回归**：`tests/test_pipeline.py::TestH2UntrustedEchoSanitizing::test_fence_itself_cannot_be_closed_early`（断言真正的结束行只出现一次）。

---

## 四、本轮未修（留待下轮决定优先级）

- **L1–L30**：均为可读性/文案/测试覆盖类问题，本轮按要求只修 M 级及以上。
- 其中与安全/数据相邻、建议优先看的几条：L4（`test_rag.py` 的 `dropped == chunks_total - chunks` 去重口径不普遍成立）、L6（超 2MB 分类两条路径不一致 + admin 提示 `--replace` 对超限文件是死胡同）、L10（脚本此前零测试，本轮已补 `tests/test_ingest_script.py`，可视为部分闭环）。
