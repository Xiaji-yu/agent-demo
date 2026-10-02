# 修复记录：带图提问在 tool-loop 终稿步丢图（识图降级）

**现象**：线上实测 2026-10-02 14:52–14:56，群 667575205 用户（超管 2224513919）
发送两张截图 +「你怎么看」，bot 两轮均回复「看不到图片具体内容」；同一用户把同样的图
转到私聊后，bot 却能逐段读出截图内容（「你截图里那位仁兄聊着微信直接黑屏重启」）。

**证据链**（`data/logs/agent.log`）：

| 时间 | 会话 | step 0 | 终稿出自 | 结果 |
|---|---|---|---|---|
| 14:52:53 | 群 667575205 | `tool_calls=True`（search_web，query 精确命中截图机型/黑屏） | step 1 | 看不到图 |
| 14:54:32 | 群 667575205 | `tool_calls=True`（search_web） | step 1 | 看不到图 |
| 14:56:02+04 | 私聊 | `tool_calls=False` | step 0 | 正常读出图 |
| 14:56:08 | 私聊 | `tool_calls=False` | step 0 | 正常读出图 |

同日志中 03:41:28（群 1076073471）、14:25:24（群 1051425116）均为「step 0 直接终稿 →
正常识图」。规律：**终稿步只要不在第 1 步，图必丢**，与群/私聊路由无关（pipeline 侧
两处 note 均为 `[图片N 已保存到工作区 media/…]`，证明图已 data-URI 化并进入
`payload["images"]`，step 0 请求确实带图——搜索 query 的精确度也佐证了这一点）。

**根因**：`agentcore/loop/engine.py` 的 tool-loop 在 `step > 0` 时把多模态 user 消息
替换为纯文本（REVIEW-be1fb07..6c57fd9.md 为压 token/请求体提出的「首次调用后降级为
文本占位再续跑 tool-loop」）。当模型第一步按 system prompt 工作流调了工具，出终稿的
第二步起就再也没有图片——LLM 无跨步视觉记忆，模型只能如实回「看不到图片」。
群聊消息文本薄（「你怎么看」+ 引用无文字）恰好命中工作流第 1 条「缺乏背景应主动
search_web 补全」，所以先在群聊暴露；私聊同样可复现，只是当天没碰上。

**修法**：默认让图片载荷贯穿整个 tool-loop（终稿步仍带图），旧降级行为改为显式开关
`AGENT_VISION_KEEP_IN_LOOP=0`（README / `.env.example` 已同步）。

- 成本上界：pipeline 单图 `AGENT_VISION_MAX_IMAGE_KB` + 单条消息 `AGENT_VISION_TOTAL_KB`
  已钉死单步请求体；仅带图轮次受影响，且步数 ≤ `max_iterations`（默认 8）。
- 历史/记忆路径不变：图片仍不落 memory（`run()` 写库的是纯文本 user_message）。

**从 X 改为 Y**（AGENTS.md §2.5 语义变更声明）：tool-loop 后续步骤「不再重发图片载荷」
（M16 旧契约，`tests/test_engine.py` 原 `test_images_not_resent_in_tool_loop`）→
「默认重发、保留至终稿步；仅在 `AGENT_VISION_KEEP_IN_LOOP=0` 时降级」。

**验证**：

- `.venv/bin/python -m pytest tests/test_engine.py -q -k images` → 6 passed
  （新增 `test_images_kept_through_tool_loop` 默认保留 / `test_images_not_resent_in_tool_loop_when_disabled` 显式关闭）
- 变异复核（改坏实现必须失败）：
  - 把 strip 条件改回无条件降级 → `test_images_kept_through_tool_loop` FAILED ✅
  - 把开关改为恒真 → `test_images_not_resent_in_tool_loop_when_disabled` FAILED ✅
- 全量默认套件 + ruff 见交付说明。

**残留**：keep 模式下带图轮次的 token 消耗随步数线性放大（最坏 max_iterations × 单条
消息预算）；如上游按请求体计费且群内带图高频，可将 `AGENT_VISION_KEEP_IN_LOOP` 置 0
换回旧行为，代价是带图 + 首步工具的提问会退回「看不到图片」。
