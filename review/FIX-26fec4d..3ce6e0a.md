# FIX-26fec4d..3ce6e0a —— 评审问题修复记录

对应报告：[REVIEW-26fec4d..3ce6e0a.md](REVIEW-26fec4d..3ce6e0a.md)（22 commit + 未提交 D2-1 预审，M×10 / L×14）。
**修复范围**：用户批准的 P0——M1–M10 全部修复；L 级（L1–L14）本轮不改，建议见摘要表。

| # | 修复方式 | 改动文件 | 回归测试 |
|---|---|---|---|
| M1 确认码跨窗重放 | 判重键从 `(nonce, 当前窗口)` 改为 **nonce 单集合**（`_used_confirms: dict[str,int]`），HMAC 宽限窗不变；`_consume_confirm` 增加可注入 `now`（测试）与非 ASCII 预检（与 M5 合并） | `web.py` | `test_m1_confirm_single_use_across_windows`（同窗拒绝/跨窗拒绝/新 nonce 不受影响） |
| M2 LINES/TTL 谎报 live | `config_write` 增 `_GROUP_CONTEXT_ATTRS`，`apply_runtime` 对两键回写 `group_context` 单例的 `max_lines`/`ttl` | `config_write.py` | `test_apply_runtime_writes_group_context_singleton` |
| M3 atomic_write 丢权限 | replace 前 `os.chmod(tmp, S_IMODE(path.stat().st_mode))`；文件不存在兜底 0600 | `config_write.py` | `test_atomic_write_preserves_mode` / `test_atomic_write_missing_file_defaults_600`；HTTP 全路径复验 0600→0600 |
| M4 symlink `.env` | 写锁内 `env_path.is_symlink()` → **409 `env_file_is_symlink`**；链接本体与真实目标均不被改 | `web.py` | `test_m4_symlink_env_409`（409 + 链接保留 + 目标未动） |
| M5 非 ASCII 入参裸 500 | `_auth` 与 `_consume_confirm` 比较前 `isascii()` 预检 → 401/400 | `web.py` | `test_m5_non_ascii_bearer_401`（bytes 头发 latin-1 字节）/ `test_m5_non_ascii_confirm_400` |
| M6 前端漏传 nonce | `editKey` 二段 body 加 `confirm_nonce` | `web.py`（页面 JS） | `test_m6_m7_page_js_fixed`（源码护栏断言二段含 nonce；end-to-end 两段序列由现有 happy-path 覆盖） |
| M7 下载恒 401 | `logDownload` 改 `fetch`（带 Bearer）+ `Blob` + 临时 `a[download]`，弃用 `location.href` | `web.py`（页面 JS） | `test_m6_m7_page_js_fixed`（断言无 `location.href = "api/logs/download`、含 `URL.createObjectURL`） |
| M8 at 占位第三人称入库 | 双保险：`facts._META_PATTERNS` 增 `@ 的?(群友\|成员\|其他人\|一位\|一个)` 与 `被@`/`有人@` 归属形态；引擎抽取输入 `_AT_PLACEHOLDER_RE.sub("", …)` 剥离占位 | `facts.py` / `engine.py` | `test_third_person_attribution_filtered` / `test_placeholder_stripped_before_extraction` |
| M9 BACKLOG 矛盾 | D2 段旧定稿四条改为“已被 D2-1 取代 + 当前有效口径”（7 键清单/写 .env/双门禁/审计字段） | `BACKLOG.md` | 文档核对（无代码断言） |
| M10 审计缺 who | `config_write` 审计增 `source_ip`（`request.client.host`）与 `confirm_nonce` | `web.py` | `test_m10_audit_records_source_ip` |

## 验收（REVIEW §2.4 + §6）

1. **复现脚本翻转**：M4（symlink→409 且链接/目标未动）、M5（非 ASCII Bearer→401，曾 500）、M3（HTTP 全路径 0600 保持）三项原「可复现」现已**不可复现**；M1 HTTP 同 token 重放→400。
2. **变异复核**：11 处逐一改坏（M1 回退窗口记账 / M2 不回写单例 / M3 去 chmod / M4 去 symlink 检查 / M5 去两处预检 / M6 去 nonce / M7 回退 location / M8 去归属模式与去剥离 / M10 去 source_ip），对应用例**全部失败后还原**。
3. **全量门禁**：`pytest -q` → **1734 passed / 48 skipped**（较评审基线 +11）；`ruff check` + `ruff format --check`（0.9.6 = pin）全绿；页面 JS `node --check` 通过。
4. 页面 JS 二段写入序列与浏览器 fetch 行为只能以源码护栏 + 服务端序列覆盖（无头环境），真机浏览器点验列为**未验证面**（PG 契约套件、真机 UI 同列）。

## 遗留 / 降级

- **L1–L14 未修**（本轮 P0 不含）：at 占位可伪造（L1）、`_META_PATTERNS` 误杀正当事实（L2）、主备通知同步 await（L3）、sink/media 死代码（L4）、music 群分支黑名单（L5）、poke 注释漂移（L6）、非 UTF-8 `.env` 裸 500（L7）、写面失败路径裸 500 + 确认码锁前消费（L8）、日志 symlink 跟随（L9）、append 追加到文件尾（L10）、asyncio.Lock 前瞻契约（L11）、超长行游标暂停（L12）、recent_images 同类固化（L13）、静态 key 回显潜在（L14）。**建议下一轮 FIX-27… 处理，优先 L4/L6/L9**。
- M2 的回写只覆盖白名单 7 键内的两键；L13（recent_images 反模式）在白名单扩容（BACKLOG D2-2）前不会暴露。
- D2-2（drift 表 + env 白名单扩容）与 D3-1（config.yaml 手术式回写）维持待做。
