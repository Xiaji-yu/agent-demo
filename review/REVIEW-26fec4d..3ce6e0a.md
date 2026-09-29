# agent-demo 近期 Commit 评审报告

**评审范围**：26fec4d..3ce6e0a（22 个 commit）**+ 未提交工作区**（D2-1 web 受控写入：新 `config_write.py` + `web.py` 写入/预览/设置增量 + 2 个测试文件）
**评审日期**：2026-09-29
**评审方式**：4 个并行子代理分线审查（线1 web 安全 / 线2 流水线正确性 / 线3 ACL+渲染+戳一戳 / 线4 测试文档声称依赖）+ 主代理逐条实证（实跑全量测试与 ruff、复现、证伪）
**工作区状态**：**不干净**——D2-1 未提交（6 modified + 2 untracked，见下）；本报告覆盖该工作区（预审）

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 最高风险 | **无 H 级**。上轮 H1「表格渲染同步阻塞事件循环」未回归（4 个渲染调用点均在 `asyncio.to_thread`，读码核验） |
| 最突出 M 级 | **7 条集中在未提交的 D2-1/D4 工作区**：确认码跨窗重放、`atomic_write` 丢 0600 权限、symlink `.env` 被替换、非 ASCII 入参裸 500、前端 editKey 漏传 `confirm_nonce`（写入面板浏览器内整体不可用）、日志下载 `location.href` 无 Bearer 恒 401、审计缺 `source_ip`；另有 LINES/TTL 谎报"保存即生效"、BACKLOG D 段文档与实现自相矛盾、at 占位使他人属性可能入库（推演） |
| 测试 | 全量 **1723 passed / 48 skipped**（主代理实跑）；ruff check + format（pin 0.9.6）全绿 |
| 声称核对 | 3ce6e0a「1691 passed/48 skipped」✓（`git archive` 实跑复核）；bc664d3「P1×10 + 1630/48 + 141 files ruff」✓；a894a85 三项 ✓；f782812 黑名单语义 ✓；**D2-1 的「7 键全部保存即生效」与 frontend 端到端可用性两项，经实证不成立（见 M2/M6）** |
| 门禁结论（§8.3） | 无 H → 不阻断。M 级问题全部可本地复现（除 M8/M10 为推演）。**未验证面**：PG 契约套件本轮未跑（本轮改动不触存储层）、真机 QQ 通道（戳一戳/通知）、浏览器真机 UI（M6/M7 以服务端行为+读码实证） |
| 依赖 | 本轮 `pyproject.toml` 无依赖变化 |

## 1. 已实证的问题

### M1 确认码「单次有效」跨 30s 窗口边界可重放一次
- **位置**：`plugins/qq_agent_adapter/web.py`（`_confirm_token`/`_consume_confirm`，约 :100-118）
- **证据**【已复现】：`_consume_confirm` 试算窗口 `(now_w, now_w-1)`，但消费记账键是 `(nonce, now_w)`。主代理脚本复现：同窗口第 2 次 → `confirm_reused`；把时钟推到 `w+1` 后第 3 次同 token → **`None`（放行，第二次真实写盘）**。子代理线1(W-4)/线2(F5)/线4(D2-1-F1) 三线独立命中同一缺陷。
- **影响**：README/代码注释/测试三处宣称「单次有效」，实际一次确认码可驱动两次写入（各带一次备份+审计）。HMAC 仍绑定 key+值，无法挪去写别的键值；需持有 bearer token。
- **建议**：`used_key` 记录**实际命中的窗口** w（或按 nonce 单集合判重）；补跨窗口重放回归。

### M2 `AGENT_GROUP_CONTEXT_LINES`/`TTL` 谎报「保存即生效」，实为 import 期快照
- **位置**：`plugins/qq_agent_adapter/group_context.py:139-142`（单例构造取 import 期值）+ `config_write.WRITABLE`（两键无 boot 标注，`apply_runtime` 恒返回 `live`）+ README/BACKLOG「7 键全部保存即生效」
- **证据**【已复现】：先 import 固化单例（10/900.0），再改 env 为 3/42 —— `context_lines()` 返回 3，但 `group_context.max_lines` 仍 10、`ttl` 仍 900.0；`record` 按 `self.max_lines` 裁剪、`snapshot` 按 `self.ttl` 过滤。三线独立命中（线2 F1 / 线3 F2 / 线4 D2-1-F2）。
- **影响**：管理员在确认弹窗「保存后立即生效」提示下改大保留量/TTL，缓冲静默不生效（仅注入切片变化），运维被文档误导沿错误方向排查。
- **建议**：`apply_runtime` 对两键回写单例属性 `max_lines`/`ttl`，或 `KeySpec` 增 boot 标注 + UI 标注需重启。
  （`recent_images` 单例同型但键未入白名单，见 L13）

### M3 `atomic_write` 静默丢失 `.env` 文件权限（0600 → 0644）
- **位置**：`plugins/qq_agent_adapter/config_write.py`（`atomic_write`，约 :198-205）
- **证据**【已复现】：`.env` chmod 0600 → 完整两段写入 → 复测 0644（`open(tmp,"w")` 按 umask 建新文件 + `os.replace` 换 inode）；`restore_file` 恢复内容也恢复不了模式。
- **影响**：`.env` 承载 `LLM_API_KEY`/`AGENT_WEB_TOKEN`；多用户主机上原本硬化的 0600 经一次 web 写入被放宽到本地其他用户可读。
- **建议**：`os.replace` 前 `os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))`；补权限保持回归。

### M4 `AGENT_ENV_FILE` 为 symlink 时：替换链接本体、真实目标不更新，接口仍返回 ok
- **位置**：`plugins/qq_agent_adapter/config_write.py`（`backup_file`/`atomic_write` 无 `is_symlink` 检查）
- **证据**【已复现】：探针：`AGENT_ENV_FILE=link.env → real.env`，走完写入后 `link.is_symlink()==False`、`real.env` 仍是旧值，接口 `ok/effective=live`（`verify_env` 走同一路径故发现不了）。
- **影响**：symlink/受管 secrets 部署下 web 改动不落在目标文件，重启后读旧值，静默配置漂移。
- **建议**：写前 `path.is_symlink()` 即 409 拒写并提示改用真实路径。

### M5 非 ASCII 入参直达 `compare_digest` → 未认证 500（两处）
- **位置**：`plugins/qq_agent_adapter/web.py`（`_auth` 约 :1265 + 确认码比较约 :111）
- **证据**【已复现】：`secrets.compare_digest("非ASCII","token")` 抛 `TypeError: comparing strings with non-ASCII characters is not supported`（Python 层根因）；子代理探针：`Authorization` 头带非 ASCII 字节 → 读面全路由 500（应为 401）；JSON `confirm_token` 非 ASCII → 写面 500（应为 400，文件未动）。挂载期的 `token.isascii()` 只覆盖服务端 token，未覆盖入参。
- **影响**：未认证攻击者可稳定制造 500，traceback 刷日志/触发误告警，掩盖真实 401/400 语义。
- **建议**：两处比较前 `isascii()` 预检，非 ASCII 直接 401/400；补回归。

### M6 前端 `editKey` 二段漏传 `confirm_nonce`，受控写入在浏览器里永远失败
- **位置**：`plugins/qq_agent_adapter/web.py` 页面 JS（`editKey`，约 :1081）
- **证据**【已复现（读码+按 shipping JS 序列推演）】：二段 body 为 `{key, value, confirm_token}`，**无 `confirm_nonce`**；服务端 `if not token or not nonce` 判真 → 重发挑战返回 `need_confirm` → 前端落入 else 分支 `alert("失败")`，`.env` 从未被改。测试只直接调 API 且总是带 nonce，故全绿掩盖。
- **影响**：D2-1 受控写入面板**整体不可用**（fail-safe 无错误写入，但宣称功能全废且无测试覆盖）。
- **建议**：二段 body 加 `confirm_nonce: res.data.confirm_nonce`；补一条两段端到端回归（走 JS 同构序列）。

### M7 日志下载用 `location.href` 导航，带不上 Bearer → 恒 401
- **位置**：`plugins/qq_agent_adapter/web.py`（`logDownload`，约 :1184）
- **证据**【已复现】：`location.href = "api/logs/download?file=..."` 是浏览器导航，无法自定义 `Authorization`；`_log_download` 挂 `Depends(_auth)` 只认 Bearer 头（token 仅存 sessionStorage），无头 GET 即 401（现有测试用手动带头的 TestClient，掩盖缺陷）。
- **影响**：README 宣称的「单文件下载」对使用者不可用（fail-closed 但不报原因）。
- **建议**：改 `fetch`（带 Bearer）→ `Blob` → 临时 `a[download]` 触发。

### M8 at 占位使他人属性可被抽取为该用户的长期事实
- **位置**：`agentcore/memory/facts.py:48`（过滤规则）+ `plugins/qq_agent_adapter/media.py:519`（占位进入 `user_text`）
- **证据**【推演】：`user_text` 现含 `[@QQ:n]`；探针 `is_transient_fact("用户 @ 的群友住在北京") == False`，该句可入库；`_META_PATTERNS` 与抽取 prompt 规则 6 均未覆盖第三人称归属句。
- **影响**：他人在 @/引用语境里的属性被记成本用户事实并递归污染后续对话的身份/归属判断——正是 a894a85 想堵的投毒面的新变体。
- **建议**：抽取输入剥离 at 占位，或 `_META_PATTERNS` 增加「@ 的?(群友|成员|其他人)」等归属形态；补样例。

### M9 BACKLOG D 段残留旧写入语义与 D2-1 实现自相矛盾
- **位置**：`BACKLOG.md`（D2 组，约 :90-94）
- **证据**【已复现】：`:90`「原定稿要点（仍有效）：写入语义 = 改文件 + 运行态尽力同步，**不落盘**到 config.yaml」与自身「改文件」矛盾；`:94` 仍是旧定稿「改运行态（os.environ/模块状态），**不落盘**——重启回 .env/config.yaml」；且该段列的键（`extract_facts`/`summary_enabled` 等）不在 7 键白名单内。
- **影响**：后续维护者据旧条目误解写入语义与可写范围，可能做出错误扩容决策。
- **建议**：将旧条目标记「已被 D2-1 取代」并同步 7 键清单与真实语义。

### M10 写入审计缺来源信息（定稿承诺 who）
- **位置**：`plugins/qq_agent_adapter/web.py`（`config_write` 审计 `_record` 调用）
- **证据**【推演】：`_record("config_write", key, old, new, backup, effective)` 无 `source_ip`/`confirm_nonce`；定稿原话「审计（who/when/key/old→new）」的 who 未落实（CIDR 是门禁不是记录）。
- **影响**：同一 token 多人共持或事后追查时无法区分操作者/来源 IP。
- **建议**：`record` 增加 `source_ip`（`request.client.host`）与 `confirm_nonce`，并在事件表展示。

### L 级（压缩列出，均已定位，处置见 §6）
| # | 位置 | 问题 |
|---|---|---|
| L1 | `media.py:519` | at 占位与用户手写 `[@QQ:n]` 逐字节相同，可伪造 @ 归属（无区分标记）【已复现】 |
| L2 | `facts.py:49` | `_META_PATTERNS` 过宽：句首「刚刚/本次」即杀，「用户刚刚搬到上海」等正当事实被误丢【推演】 |
| L3 | `llm/client.py:164` | 主备通知 `await` 在用户回复路径上同步执行，无超时；故障期可给回复追加 N×30s 延迟【推演】 |
| L4 | `sink.py:92` / `media.py:257` | `send()` return 后 12 条语句不可达；`own_client` 恒 False 死分支【已复现】 |
| L5 | `music_route.py:134` | 序号选歌群分支只查 `is_group_allowed` 不查黑名单（当前因选择列表可达性而不可达）【推演】 |
| L6 | `poke.py:92` | `_cooling` docstring「否则刷新时间戳」与实际不符——失败路径不记冷却是 docstring 明示的有意设计（防故障期叠加等待），但注释漂移 |
| L7 | `web.py:82`（`_env_value_or_none`） | 只捕 `OSError`；非 UTF-8 `.env` 抛 `UnicodeDecodeError`（非 OSError）穿透成无 detail 的 500【已复现】 |
| L8 | `web.py` 写入段 | 写面多个失败路径裸 500（备份目录不可写 / body 非 dict）；且 `_consume_confirm` 在锁前执行，锁内失败即烧掉确认码【已复现】 |
| L9 | `web.py` 下载段 | 日志目录内 symlink 可被读取/下载跟随（`is_file` 与 `FileResponse` 均不拒链接）【已复现】 |
| L10 | `config_write.py` `patch_env_text` append 分支 | 键不存在时追加到文件尾，多键追加后 `.env` 出现尾部分散行（可读性退化）【推演】 |
| L11 | `web.py` | 模块级 `asyncio.Lock` 的前瞻契约：锁内仅同步段，未来若加真实 IO await，多事件循环部署下不互斥【推演】 |
| L12 | `web.py` 长行处理 | 单行 ≥256KB（窗口上限）时游标暂停推进（`last_nl==-1` 原地等待），新完整行到达后恢复——非永久死锁，兜底缺失【推演】 |
| L13 | `pipeline.py:197` | `recent_images` 单例同 M2 的 import 期固化反模式；键未入白名单，D2-2 扩容时须同步改【已复现】 |
| L14 | `web.py` | 静态 key 回显潜在面：非 `WRITABLE` 键不产 `file_text`，当前表内无此类键，无实际泄漏；记为契约提示【推演】 |

## 2. 被证伪的发现

| 怀疑 | 证伪理由 |
|---|---|
| 上轮 H1「表格渲染同步阻塞事件循环」在本轮渲染改版（612afa3/7006822）后回归 | 四个渲染调用点（admin usage/outbound 表格/help 图/poke 概览图）均在 `asyncio.to_thread`，读码核验无回归 |
| `_used_confirms` 无界增长 | 每窗口清理 `t[1] < now_w-2`，集合有界 |
| dotenv 多行引号值可破坏写入安全 | 写入值禁止引号/换行/`#`；`verify_env` 与读取同一行模型，一致 |
| poke 失败重试无冷却是缺陷 | `poke_mark_replied` docstring 明示「发送成功后才记冷却」是有意设计（防故障期叠加等待）；仅 `_cooling` 注释漂移（L6） |
| `asyncio.Lock` 跨事件循环绑定失败 | 3.12 无竞争快路径不绑定 loop；三个独立 TestClient 并发写均 200 |
| os.environ 泄漏导致写路由在别处意外挂载 | test_web 有 autouse `_clean_env` delenv 两键；无裸 `os.environ` 写入 |
| 确认码可被改值/改键/换 token 重放 | HMAC 绑定 `nonce\|key\|file_text\|窗口` 与 web token；改值即 400（跨窗同值重放成立，见 M1） |
| bc664d3「P1×10」声称夸大 | `git archive bc664d3` 实跑 1630 passed/48 skipped；ruff check `-v` 恰 141 files；两声称属实 |
| BACKLOG 头部 1699 数字过期是笔误 | 1699 为 bc664d3 时点（baseline=ba0b33f）实测数，按该 commit 档案复跑一致；3ce6e0a 时为 1691（其 commit message 亦如实） |

## 3. 测试与文档状况

- **实跑**（主代理）：全量 `pytest -q` → **1723 passed / 48 skipped / 49 warnings**（69s）；`ruff check` 144 files 全过；`ruff format --check` 全过；ruff 0.9.6 = pyproject pin。
- **本轮新增/修改测试的真实性**：抽查 test_web.py（含新 TestSettingsWrite/TestLogsView）、test_config_write.py、test_pipeline.py、test_facts.py、test_engine.py、test_acl.py 的断言均为具体值/结构断言；上轮缺陷「恒真断言」在本轮范围**未发现新实例**（审查中曾在 test_web.py 抓到过一条 `or True`，已在评审前由实现方自查修复）。
- **变异覆盖**（实现期记录，非本轮重新执行）：D1 设置护栏 5/5、D4 日志护栏 6/6、D2-1 写入护栏 6/6 曾被逐一改坏验证。
- **CI 盲区**：PG 契约套件（`TEST_DATABASE_URL`）本轮未跑——本轮改动不触 `memory/store.py` 与 DDL，登记为未验证面；TZ=UTC 下 CI 全量按 04f8d74/2820b04 钉时区用例处理。
- **未验证面与门禁结论**（§8.3）：(a) PG 契约套件未跑（改动不触存储层，降级记录）；(b) 真机 QQ 通道（戳一戳触达/私聊通知/下载在真浏览器的行为——M6/M7 以服务端行为+读码实证，真机 UI 待具备环境时补验）；(c) 无 H 级 → **不阻断**。

## 4. 已验证为「无问题」的关键项

1. **写入门禁 fail-closed**：缺 `AGENT_WEB_WRITE` 或缺 CIDR → 写/preview 路由整体不注册（404），读面零影响（测试+探针）。
2. **白名单锁死与凭据键保护**：`LLM_API_KEY` 等凭据/AOL 边界键一律 403；7 键全部非敏感，审计值经 `masked`（sk- 前缀掩码、120 截断）。
3. **`.env` 值注入防御**：换行/`#`/引号在 `validate_value` 落盘前拒绝；CRLF 文件全路径逐字节保留（HTTP 级回归）。
4. **写后验证失败自动还原**：`verify_env` 失败 → 从预生成备份还原 + `config_write_rollback` 审计（变异已验证）。
5. **读面无密钥/正文泄漏**：`model_status` 只含模型名与线路；settings/日志响应与诊断事件字段无 api_key/base_url。
6. **日志文件名穿越防御**：URL 编码/绝对路径/NUL/错格式轮转名均拒绝；参数上限（bytes 钳制、grep>100 拒、after 负数归零）齐备。
7. **引用围栏头部注入防护**（a894a85）：昵称空白/控制字符转 `_`、去括号尖括号、截断 24 字，伪造围栏行探针被中和。
8. **图片链路 SSRF 逐跳校验**（184a7b9）：http→https 升级仅命中白名单主机，每跳 `host_ips_are_safe` + 域名白名单照跑。
9. **BLOCKED_USERS 语义**（f782812）：superuser 豁免优先、命中经 `acl.deny` 静默仅记 WARNING、非 ASCII 数字条目启动 WARNING；admin 命令未误伤。
10. **合并转发二级解析 / get_msg 图片占位兜底**（82cdae2/eb020f7/ca57e1b）：两级兜底链完整，占位不可还原时显式引导重发。
11. **点歌 skill 硬闸**（e634063）：群白名单/私聊 superuser/歌名校验/账号级冷却四道闸收口在 handler；序号选歌为确定性兜底。
12. **主备通知无凭据泄漏**（824165e）：事件 dict 与推送文案仅模型名+异常类型名。

## 5. 与在库报告的衔接复核

上轮 [REVIEW-a36ea1d..26fec4d.md](REVIEW-a36ea1d..26fec4d.md) 的 M1–M4 由 `1bb9290` 修复落地（commit message 声称），本轮全量绿、未发现其回归。更早轮次（REVIEW-6ec3f7c..a36ea1d 的 H1/M3、REVIEW-6ec3fd9..8cfbf6d 系列）的修复项在本轮分线中抽查无回归。上轮「未验证面」中 (b)(d) 类（真机/部署面）保持未验证，本轮无新增可销项。

## 6. 修复优先级建议

| 优先级 | 项 | 理由 |
|---|---|---|
| P0（未提交代码入库前必修） | M1/M3/M4/M5/M6/M7 + M2 | 七条均在未提交的 D2-1/D4 工作区：两条使宣称功能在浏览器里整体不可用（M6/M7），两条削弱部署安全姿态（M3/M4），一条契约破防（M1），一条未认证 500（M5），一条运维误导（M2）。**修复前不要提交 D2-1** |
| P1（随下一提交） | M8、M9、M10 | 记忆投毒新变体（推演，需样本复核）、文档自相矛盾、审计缺 who |
| P2（择机） | L1–L14 | 按 §6 纪律逐条带回归；L4/L6/L9 优先（死代码/注释漂移/symlink 面） |

修复按 §6 要求产出 `review/FIX-26fec4d..3ce6e0a.md`；主代理复现实证（§2.4）作为验收：M1–M7 的复现脚本必须由「可复现」变「不可复现」。

---

## 证据附录（主代理复现脚本摘要，2026-09-29 实跑）

```text
V1  secrets.compare_digest("非ASCII","token") → TypeError: comparing strings with
    non-ASCII characters is not supported            （M5 根因，Python 层实锤）
V2  时钟推进复现（web._confirm_token/_consume_confirm）：
    同窗口第2次 → confirm_reused；跨到 w+1 第3次 → None（放行）        （M1）
V3  .env chmod 0600 → atomic_write → stat 0644                            （M3）
V4  AGENT_ENV_FILE 为 symlink → 写后 is_symlink()=False、
    real.env 仍是旧值、verify_env=True、接口 ok                            （M4）
V5  先 import 固化 group_context(10, 900.0)，再 setenv LINES=3/TTL=42：
    context_lines()=3 但 buffer.max_lines=10 / ttl=900.0                  （M2）
V6  read_env_text(非 UTF-8 .env) → UnicodeDecodeError（非 OSError，
    _env_value_or_none 不捕获 → 裸 500）                                  （L7）
读码核实：editKey 二段 body 无 confirm_nonce（M6）；
    logDownload 用 location.href（M7）；
    sink.send return 后 12 条不可达（L4）；
    music_route 群分支无 is_allowed（L5）；
    BACKLOG D 段旧写入语义（M9）。
```
