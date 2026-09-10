# agent-demo 功能评审报告：长回复分层投递 + 出站节流

**评审范围**：`8482eb3..19aff9f`（新增 `plugins/qq_agent_adapter/outbound.py`，改 `matcher.py` / `sink.py` / `README.md` / `.env.example` / `tests/`）
**评审日期**：2026-09-11
**评审方式**：4 条并行只读子代理分线（分层投递逻辑 / 节流并发与资源 / 接线与文档一致性 / 测试能否失败）+ 主代理逐条实证与证伪
**基线**：`19aff9f`，工作区干净；`580 passed, 32 skipped`，ruff 全绿
**修复提交**：`53d0f44`（本报告 §5 给出逐条状态）

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | **节流器越过窗口上限后连最小间隔一起失效**（评审 H1，我已实测复现）：默认配置同群连发 40 条时，第 21~40 条**同一时刻全部放行**，之后节流完全解除——与该模块「防 QQ 风控」的目标正好相反 |
| 次高 | **合并转发"重试阶梯"在超时场景重复投递**：同一份内容最多发 2 张转发卡片 + N 条逐条文本（已复现） |
| 其他实质缺陷 | 降级/关闭开关时**附发 md 文件**导致内容发两遍；**切分压平代码缩进与空行**；文件发送**绕过节流**且多账号**走错账号**；matcher 异常路径**丢弃剩余分块** |
| 测试质量 | 变异测试 31 个变异仅 14 个被抓；**3 条测试是假阳性**（删掉私聊护栏、把节点内容发空、旁路共享限流器，测试全绿） |
| 被证伪 | 子代理报的「文件发送失败会外发 2000 字未节流文本」「降级日志在两条路径缺失」**均不成立** |
| 修复 | 上述全部 H/M 已修（`53d0f44`），测试从 50 增至 105（`test_outbound.py` + `test_matcher.py`），全量 **603 passed / 32 skipped** |
| 遗留 | `send_forward_msg` 兼容分支的字段形状无法离线验证；逐个 `[reply]` 日志；`data/cache/report.md` 固定文件名；提醒跨群 N≥76 会超过 tick 间隔 |

---

## 1. 已实证的问题（H）

### H1 越过窗口上限后，最小间隔被一并丢弃 → 节流完全失效【已复现，已修】

- **位置**：`plugins/qq_agent_adapter/outbound.py` `OutboundThrottle.acquire`（`19aff9f` 版 257-282 行）
- **根因**：`wait = max(最小间隔分量, 窗口分量)`，随后 `if waited + wait > max_wait: 直接放行`——软上限作用在**合并后**的 wait 上，一旦窗口分量超限，连最小间隔与全局间隔一起放弃；且放行路径仍然 `record`，后续每条继续零等待。
- **证据**（主代理复现，假时钟；子代理另用真实 `time.monotonic` + 真实 `asyncio.sleep` 佐证）：

```
默认参数 min_interval=1.0 / global=0.4 / per_window=20 / window=60 / max_wait=10，同群连发 40 条：
  第1条 t=1000.0  第20条 t=1019.0  第21条 t=1019.0  第40条 t=1019.0
  同一时刻放行的最大条数 = 21
```

- **影响**：`AGENT_OUTBOUND_PER_MIN=20` 几乎不生效；一旦越过它反而解除全部节流，形成瞬时突发——正是本次提交想防的场景。
- **修法**：把两个分量拆开——**最小间隔是硬约束**（任何情况完整执行），**窗口条数是软约束**（最多再等 `max_wait` 余额）。修复后同一实验：同一时刻最大放行 1 条、最小相邻间隔 1.0s，40 条耗时 79s（窗口上限切实提供背压）。

### H2 合并转发"重试阶梯"在"已送达但抛异常"时重复投递【已复现，已修】

- **位置**：`outbound._try_forward`（两跳 API 都 `except Exception` 后重试 / 降级）
- **证据**（主代理与子代理各自复现）：让第一个 API「实际已送达但抛 `TimeoutError`」，实测发送动作 = `send_group_forward_msg` + `send_forward_msg` + 2 条逐条文本，同一内容最多发 3 次。
- **佐证**：`nonebot/adapters/onebot/v11/utils.py` 的 `handle_api_result` 在 `status=="failed"` 时抛 `ActionFailed`——异常发生在请求**已抵达实现之后**，「抛异常 ⇒ 没发出去」不成立。
- **修法**：区分「确定没发出去」与「结果未知」。超时/连接断开（`TimeoutError` / `NetworkError` / `WebSocketClosed`）一律**不重发**，返回新投递模式 `forward-unconfirmed` 并打 ERROR；只有确定失败才回落逐条。

### H3 切分会压平代码缩进与空行【已复现，已修】

- **位置**：`split_message`（旧 `matcher._split_qq_message`）逐段 `strip()` + `\s*` 吞噬换行后空白
- **证据**：真实链路 `_qq_plain → split_message → deliver_reply`，长回复里的代码块：

```
原文含 '\n    if x:' = True  →  切分后 = False
原文含空行 '\n\n'  = True  →  切分后 = False
```

- **影响**：coding agent 的长回复（>1500 字）在群聊/中长私聊里代码语义被改坏；只有私聊 >`FORWARD_MAX` 的 md 附件是完整原文。
- **修法**：改为只在边界断开、不吞后续空白、分段按原文拼接，只裁每个分块**首尾**空白。修复后缩进与空行均保留（非空白字符零丢失，仍由测试 fuzz 保证）。

### H4 降级/关闭开关时附发 md 文件 → 同一内容发两遍【已复现，已修】

- **位置**：`deliver_reply` 末尾的附发文件条件只判断 `kind == "private" and len(text) > forward_max()`，**不看转发是否成功**
- **证据**（子代理矩阵，`SINGLE_MAX=100 / FORWARD_MAX=200`，660 字）：

| 场景 | 修复前 mode | 出站 |
|---|---|---|
| 转发成功 | `forward+file` | 1 条 + 1 文件 |
| 转发被拒 | `chunked+file` | **N 条逐条文本 + 1 文件（双份正文）** |
| `AGENT_REPLY_FORWARD=0` | `chunked+file` | **N 条 + 1 文件** |
| 节点数超上限 | `chunked+file` | **N 条 + 1 文件** |

- **修法**：附发文件条件改为 `mode == MODE_FORWARD`；`chunked+file` 这个组合不再存在。

### H5 三条测试是假阳性（改坏代码也不会失败）【已复现，已修】

变异测试（31 个定向变异，14 caught / 17 escaped）暴露：

| 用例 | 变异 | 结果 |
|---|---|---|
| `test_very_long_group_has_no_file` | 删掉 `kind == "private"` 护栏（= 往群里发文件） | **仍然通过**——因为真实文件发送在测试环境必然失败，group 也进不了 `+file`；通过是环境巧合 |
| 合并转发节点内容 | 把 `content=c` 改成 `content=""`（整条回复发空） | **50 passed** |
| 共享限流器 | 让 `_resolve_throttle` 不用共享实例（节流被旁路） | **50 passed** |

- **修法**：文件发送统一 stub 成「会成功」再断言未被调用/被调用次数；断言节点内容拼接等于原文；断言 `throttle is default_throttle()`；并把自证式断言（`bot.apis() == [...] * len(bot.calls)`）换成精确条数。

---

## 2. 已实证的问题（M）

| 编号 | 问题 | 证据 | 状态 |
|---|---|---|---|
| M1 | `Sink.send` 在 bot 循环内 `acquire`：换 bot 重试重复记账 | 2 个 bot、第 1 个失败 → acquire 2 次、实际送达 1 条、白等 1×min_interval | 已修（移出循环） |
| M2 | `_try_forward` 每次 API 尝试都 `acquire`，失败尝试也记账 | 降级路径 acquire **5** 次送达 3 条；compat 成功路径多等 1s | 已修（一次逻辑投递一次额度） |
| M3 | `_locks` / `_targets` 按 target 无界增长，`reset()` 无生产调用 | tracemalloc：200k target ≈ **190 MiB**（≈997 B/target） | 已修（上限 4096，淘汰空闲桶） |
| M4 | `acquire` 的 while 循环无进度保证：注入时钟下残差被浮点吸收 → 死循环 | 默认 `global=0.4` 即可复现「迭代 21 次不返回」 | 已修（迭代上界 + eps + 单调钳制） |
| M5 | 私聊附发文件完全绕过节流器 | 提交版 `deliver_reply(private, 长文)` 出站 2 条、acquire 仅 1 次 | 已修 |
| M6 | 多账号下 md 附件由 `driver.bots` 第一个账号发出（串号） | 触发 bot 是 20002，文件却由 10001 发出 | 已修（`send_markdown_file` 新增可选 `bot=`） |
| M7 | matcher 异常路径：已发一半后再叠一条错误提示，剩余分块静默丢失 | 4 块回复第 2 块失败 → 实际送达 `[chunk1, "出错啦：..."]` | 已修（单块失败继续发，最后汇总告警） |
| M8 | `AGENT_REPLY_FORWARD_MAX` 不参与模式判定，实际门限是 `SINGLE_MAX × MAX_NODES`；README 表格与实现不符 | 默认 15001 字 → 11 段 → 逐条；`SINGLE=400/FORWARD=4500` 时 4500 字也逐条 | 已修（文档与代码注释澄清真实上限） |
| M9 | 逐条降级时每条 `[reply]` 日志丢失（只剩一条 200 字截断的 mode 日志） | `matcher.py` 调用点对比 | **未修**（mode + chunks 日志已可定位，接受） |
| M10 | compat 分支 `send_forward_msg` 的 `message_type` 字段形状未经真机验证 | 子代理认为 onebot 标准无此字段；go-cqhttp 风格实现则有 | **未修**（无法离线验证，已加注释说明；失败即降级） |
| M11 | `acquire` 内两次读时钟，记账时间戳偏早（实际间隔略短于配置） | 构造 5ms/读的时钟测得 Δ=0.005s | 已修（放行后按当前读数记账） |
| M12 | matcher「无 bot」用例不约束显式 raise（删掉 raise 也通过） | MA3 变异 → 3 passed | 已修（断言错误文案含 `no bot connected`） |

---

## 3. 被证伪的发现

| 子代理结论 | 主代理复核 | 结论 |
|---|---|---|
| 「文件发送失败会**直接发出**未节流的 2000 字符私聊文本」 | `file_sender.py` 的 preview 只是**返回值**，从不发送；`matcher` 侧只在 `FILE_SEND_OK_PREFIX` 命中时改写回复，`outbound._send_file` 只记日志 | **假阳性**（真实缺陷只是"该次发送不经节流"，已修） |
| 「降级日志在 `AGENT_REPLY_FORWARD=0` 与节点数超限两条路径**缺失**」 | 我实跑两条路径，`长回复降级为逐条发送：group:1 chunks=13` 各打印一次 | **假阳性** |
| 「`set` 式断言 `not in bot.apis()` 在零调用时也成立」 | 成立，但同文件另有 `len(sent) > 1` 与精确条数断言兜底 | 部分成立（已一并替换为精确断言） |

---

## 4. 测试与文档状况

- **基线**：`19aff9f` → `580 passed, 32 skipped`；修复后 `53d0f44` → **`603 passed, 32 skipped`**，`ruff check` 全绿。
- **变异测试**（子代理，31 个定向变异）：14 caught / **17 escaped**。修复后新增的用例覆盖了其中的 H 级假阳性与主要盲区；`_try_forward` compat 第二跳、私聊单条分支、sink 多 bot 重试/失败返回、软上限记账等均已补测。
- **测试文件规模**：`tests/test_outbound.py` 51 用例（新增），`tests/test_matcher.py` 新增接线断言。
- **文档一致性**：9 个 `AGENT_OUTBOUND_*` / `AGENT_REPLY_*` 变量与 `.env.example`、README 一一对应，无「写了不生效」；修复后更正了分层语义（真实转发上限 = `SINGLE_MAX × MAX_NODES`）、附发文件的触发条件、`forward-unconfirmed` 模式、节流覆盖范围（`admin.py` 的 `finish()` 未纳管，已如实披露）。
- **CI 盲区（沿用上一轮结论）**：32 个跳过仍全部是 `TEST_DATABASE_URL` 门控，本次改动不涉及 PG。

---

## 5. 已验证为「无问题」的关键项

- **模式返回值与实际发送行为自洽**：`single`=恰好 1 条文本；`forward`=恰好 1 次转发 API 且 0 条逐条；`chunked`=0 次转发 + 精确 N 条；`+file`=恰好 1 次文件调用。
- **非空白字符零丢失**：1500/1501/2999/3000/3001 及含空格换行样本，分块拼接后与原文一致（仅裁分块首尾空白）；硬切分支逐字符无损。
- **越界与降级安全**：`bot is None` → 显式 `RuntimeError` → 错误提示，不会误发到别的会话；`user_id` 非法 → sink 拒绝。
- **身份正确**：node 段 `user_id` / `nickname` 固定为 bot 自己；`self_id` 非数字时直接降级，不伪造他人身份。
- **接线无回归**：群/私聊 `ident` 取值与原 `_send_reply` 一致；`Sink(` 无位置参数调用被破坏；节流键在 matcher/sink/reminder 三处拼法一致。
- **`_split_qq_message` → `split_message` 迁移**：除刻意的空白保留改动外，全仓无残留引用。
- **已有出站路径全景**：`outbound._send_text` / `_try_forward` / `matcher._send_reply` / `sink.send` 全部经节流；`admin.py` 的短命令回复未纳管（低频、已披露）。

---

## 6. 遗留与建议

1. **真机验证合并转发**（唯一未闭环项）：NapCat 在 `192.168.1.2`，通过**反向 WS** 连到本机 8080；其 HTTP API（`192.168.1.2:1315`）从本机 **connection refused**（本机无 1315 监听、非本机地址），因此无法离线确认 `send_group_forward_msg` 是否被该实现支持。代码已按「不支持即降级逐条」设计，但**需要一次真机发送验证**。建议在 NapCat 所在机器上确认 HTTP 服务监听 `0.0.0.0`，或直接在群里发一条 >1500 字回复观察 `[reply] ... mode=forward`。
2. **`send_forward_msg` 兼容分支**：字段形状未经真机验证（M10），已注释说明；若实测两跳都不认，可考虑删除该分支以减少一次无效 API 往返。
3. **逐个 `[reply]` 日志**（M9）：降级路径只留汇总日志，若需要逐条可观测可再补。
4. **`data/cache/report.md` 固定文件名**：并发长私聊会互相覆盖缓存文件（不影响送达内容，上传走 base64）。建议按 `user_id` 分文件名。
5. **提醒投递与节流的相互作用**：`ReminderService.tick` 串行 `await sink.send`，同群 N 条提醒会按最小间隔排开（默认 10 条 ≈ 9s）；跨群约 `0.4 × (N-1)` 秒，**N ≥ 76 时单轮 tick 会超过 `AGENT_REMINDER_TICK=30s`**（调度器 `max_instances=1`，后果是提醒推迟而非堆积）。若提醒量级会上来，建议 tick 内并发投递或分批。
6. **排队时间不受 `max_wait` 约束**：同一 target 并发 N 次发送时，最后一个的总阻塞可达 `(N-1) × 单次持锁时间`。已在 docstring 写明；如需硬上界可在 acquire 外层再加超时。
