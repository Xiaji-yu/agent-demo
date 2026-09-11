# agent-demo 近期 Commit 评审报告
**评审范围**：`19aff9f..436629d`（2 个 commit：修复提交 `53d0f44` + 文档提交 `436629d`）
**评审日期**：2026-09-11
**评审方式**：4 条并行只读子代理分线（消息流水线/节流 / 技能边界与多账号安全 / 测试有效性 / 文档一致性）+ 主代理逐条实证（实跑 pytest/ruff、复现脚本、证伪）
**工作区状态**：干净；`603 passed, 32 skipped`，ruff 全绿

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| **最高风险** | **`split_message` chunk 边界仍用 `.strip()`，极端换行/缩进场景仍会压平代码缩进**（子代理 L3 / 文档子代理 H1 共同指出；主代理【推演】确认边界条件存在） |
| 次高 | **`_reconstruct_content_from_memory` 死代码**：无论 `_agent_memory` 是否存在都返回 `""`，导致缺省 `content` 时 skill 必然报 `missing content`（技能边界子代理 H1） |
| 其他实质缺陷 | `AGENT_OUTBOUND_MAX_TARGETS` 未在 README/.env.example 文档化（M1）；`AGENT_OUTBOUND_MAX_WAIT` 的约束范围（只约束窗口分量）未在文档中显式说明（M2）；文件发送经过节流器未在 README 覆盖范围中列出（M3） |
| 被证伪 | 子代理提出的「文件发送失败会外发未节流文本」「降级日志缺失」「sink acquire 重复」等怀疑**均不成立** |
| 测试与文档 | 全量 **603 passed / 32 skipped**；ruff 全绿；README/.env.example 与代码基本一致，但存在上述文档缺口 |
| 遗留 | `send_forward_msg` 兼容分支字段形状未真机验证；`_reconstruct_content_from_memory` 死代码需设计决策（删除或实现）；`_evict_idle` 命名与 FIFO 实现不完全一致 |

---

## 1. 已实证的问题

### H1（边界）`split_message` 在 chunk 边界仍会压平前导缩进
- **位置**：`plugins/qq_agent_adapter/outbound.py:158,181,186`
- **根因**：正则按 `\n` 切段后，下一段首部空白即下一行缩进。若该段被 flush 为独立 chunk 或新 buf 起始段，`.strip()` 会把这些空白当成"首部空白"裁掉。
- **证据**（主代理【推演】+ 子代理 L3）：
  ```python
  # outbound.py:158,181,186
  chunks.append(buf.strip())
  chunks.append(seg[start:cut].strip())
  chunks.append(buf.strip())
  ```
  默认 `SINGLE_MAX=1500` 下触发概率低，但较小 chunk 尺寸或超长代码跨段场景可复现。
- **影响**：长回复里的代码块在 chunk 边界仍可能被压平。
- **修复建议**：将三处 `strip()` 改为 `rstrip()`，仅裁掉尾部空白（分隔换行/尾随空格），保留行首缩进。

### H2 `_reconstruct_content_from_memory` 存在死代码，memory 回退完全失效
- **位置**：`agentcore/skills/file_sender.py:49-60`
- **根因**：获取到 `memory` 后直接返回 `""`，无论 memory 是否存在。
- **证据**（技能边界子代理【已复现】）：
  ```python
  def _reconstruct_content_from_memory() -> str:
      ...
      memory = getattr(driver, "_agent_memory", None)
      if memory is None:
          return ""
      return ""  # ← 死代码
  ```
- **影响**：`send_markdown_file_skill` 在缺省 `content` 时必然报 `Error: missing content`。
- **修复建议**：如果 `memory` 有 `get_recent` 或类似 API，应在此处调用并返回实际内容；若该回退路径当前无需实现，应删除此函数并在 skill 中移除对它的调用。

### M1 `AGENT_OUTBOUND_MAX_TARGETS` 未在 README/.env.example 中说明
- **位置**：`outbound.py:53,257,344-361`；`README.md:282-292`；`.env.example:185-192`
- **影响**：长期运行的机器人若见过大量历史会话（>4096 个 distinct target），旧桶会被静默淘汰并丢失窗口记录。运维人员无法从文档中了解该上限的存在或调整方式。
- **修复建议**：在 README「出站节流」节补充 `AGENT_OUTBOUND_MAX_TARGETS` 的作用；在 `.env.example` 中添加该配置项及说明。

### M2 `AGENT_OUTBOUND_MAX_WAIT` 的「软上限」约束范围未明确
- **位置**：`README.md:288-289`；`outbound.py:243-244,322-326`
- **影响**：用户可能误以为将 `max_wait` 设为 0 即可跳过所有等待，但实际上 `min_interval`（per-target 和 global）是硬约束。
- **修复建议**：在 README 补充说明：「注意：该软上限仅约束「窗口条数」分量，最小间隔（per-target / 全局）始终完整执行，不受 `max_wait` 限制。」

### M3 文件发送经过节流器未在 README 覆盖范围中列出
- **位置**：`README.md:290`；`outbound.py:520`
- **影响**：低。文件发送事实上受节流保护，但 README 的覆盖范围列表未列出。
- **修复建议**：将 README 改为「覆盖范围：回复（单条/合并转发/逐条降级）、附发文件与主动推送（提醒）全部走它」。

---

## 2. 被证伪的发现

| 怀疑 | 结论 | 原因 |
|---|---|---|
| 文件发送失败会外发未节流文本 | **被证伪** | 主代理复现：文件发送失败仅记日志，文本路径已受节流保护 |
| 降级日志在两条路径缺失 | **被证伪** | 代码阅读确认 `_try_forward` 和 `deliver_reply` 均有完整日志 |
| sink acquire 在 bot 循环内重复 | **被证伪** | 主代理复现：`acquired == ["group:1"]`，仅发生一次 |
| H1 最小间隔仍会被窗口软上限压制 | **被证伪** | 主代理复现 + 子代理推演：`wait = max(wait_min, min(wait_win, grace))` 保证 `wait >= wait_min` |
| H2 uncertain failure 会重复投递 | **被证伪** | 主代理复现：超时返回 `forward-unconfirmed`，不重发 |
| M3 target 表仍会无界增长 | **被证伪** | 主代理复现：`max_targets=3` 时 `len(th._targets) <= 3` |

---

## 3. 测试与文档状况

### 实证数据
- **全量测试**：`.venv/bin/pytest tests/ -q` → **603 passed, 32 skipped**
- **lint**：`.venv/bin/ruff check .` → **All checks passed!**
- **主代理复现脚本**：`review_verify.py` 覆盖 H1/H2/H3/H4/M1/M3/clock，全部 PASS

### 测试覆盖评估
- 对 H1/H2/H3/H4/M1/M2/M3 均有对应测试用例
- 新增测试从 50 增至 105，覆盖了 min_interval 硬约束、window 软约束、frozen clock、clock 回拨、target 表有界、UNCERTAIN 场景、多账号 bot、sink acquire 单次等
- 上一轮报告的 3 条假阳性测试已修复

### 文档一致性
- README 与 `.env.example` 已同步更新，与代码基本一致
- 遗漏项：`AGENT_OUTBOUND_MAX_TARGETS`、`AGENT_OUTBOUND_MAX_WAIT` 约束范围、文件发送节流覆盖

---

## 4. 已验证为「无问题」的关键项

| 要点 | 验证方式 |
|---|---|
| H1 最小间隔硬约束 + 窗口软约束 | 主代理复现：假时钟 6 连发，gaps = `[1.0, 10.0, 10.0, 10.0, 10.0]`，第 21~40 条不再瞬时放行 |
| H2 三态返回正确区分确定失败/结果未知 | 主代理复现：`TimeoutError` 返回 `forward-unconfirmed`，不重发 |
| H3 split_message 保留空白（主体路径） | 主代理复现：代码块缩进与空行保留，chunks=4，`"\n\n" in joined=True` |
| H4 降级时不发文件 | 主代理复现：两个 API 都失败时 `mode=chunked`，`sent_files=[]` |
| M1 sink acquire 只发生一次 | 主代理复现：`acquired == ["group:1"]` |
| M2 备用 API 不重复取额度 | 代码阅读：`acquire` 在 attempts 循环外 |
| M3 target 表有界 | 主代理复现：`max_targets=3` 时 `len(th._targets) <= 3` |
| `_now()` 时钟回拨钳制 | 主代理复现：100 次回拨后 `hist == sorted(hist)`，`len(hist) <= 61` |
| `_safe_filename` 路径穿越防护 | 代码阅读：`Path(name).name` 只保留文件名 |
| `_safe_user_id` 注入防护 | 代码阅读：全数字校验 + `int()` 转换 |

---

## 5. 与在库报告的衔接复核

上轮报告 `REVIEW-8482eb3..19aff9f.md` 的 H/M 项修复状态：

| 上轮问题 | 修复状态 | 证据 |
|---|---|---|
| H1 越限后节流失效 | **已修复** | 主代理复现 + 测试 `test_min_interval_survives_window_overflow` |
| H2 超时重复投递 | **已修复** | 主代理复现 + 测试覆盖 UNCERTAIN 场景 |
| H3 切分压平缩进 | **部分修复** | 主体路径已修复，但 chunk 边界 `.strip()` 仍有残余 corner case（本轮 H1） |
| H4 降级双份文件 | **已修复** | 主代理复现 + 测试覆盖 |
| H5 三条测试假阳性 | **已修复** | 测试从 50 增至 105，变异测试覆盖改善 |
| M1 sink 重复取额度 | **已修复** | 主代理复现 + 测试覆盖 |
| M2 备用 API 重复取额度 | **已修复** | 代码阅读 + 测试覆盖 |
| M3 target 表无界增长 | **已修复** | 主代理复现 + 测试覆盖，但文档未说明（本轮 M1） |

---

## 6. 修复优先级建议

1. **P0**：修复 `split_message` 三处 `.strip()` → `.rstrip()`，彻底解决 H3 残余 corner case
2. **P1**：修复 `_reconstruct_content_from_memory` 死代码（实现 memory 回退或删除函数+skill 调用）
3. **P1**：补充 `AGENT_OUTBOUND_MAX_TARGETS` 和 `AGENT_OUTBOUND_MAX_WAIT` 约束范围的文档
4. **P2**：明确 `_evict_idle` 命名或实现真正 idle-based 淘汰
5. **P2**：考虑为 `send_markdown_file_skill` 增加 bot 参数，多账号场景闭环
