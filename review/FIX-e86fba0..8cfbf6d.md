# agent-demo 评审修复记录

**对应评审**：[REVIEW-e86fba0..8cfbf6d.md](REVIEW-e86fba0..8cfbf6d.md)（条目编号沿用该报告 §1）
**修复日期**：2026-09-10
**修复方式**：按文件归属分 5 路并行修复（rag / 记忆存储 / 备份恢复 / 沙箱安全 / skills 与适配层）+ 主代理做工程化与文档项，最后统一全量验证
**验证结果**：`pytest` **549 通过 / 32 跳过（PG 门控与平台用例）/ 0 失败**（修复前 449/21）；`ruff check agentcore plugins tests bot.py scripts` 全绿；评审报告中所有「已复现」样例按 REVIEW-WORKFLOW.md §6 验收**全部变为不可复现**（复现记录见文末）。PG 门控用例另在临时 scratch 库实跑 58/58 全过（含真实 docker pg_dump/psql roundtrip），跑完即删。

---

## High

| 条目 | 结论 | 修复方式 | 测试 |
|---|---|---|---|
| **H1** 沙箱守卫带点小节名绕过 | **已修复（三层）** | ① `runner.py` 守卫正则驱动名改跨点匹配（`diff\..+\.textconv` 等，宁可误报）；② 删除「配置无驱动即跳过 attributes 扫描」捷径，`.gitattributes` 一律扫描；③ **根治写入面**：`fs.py` 新增 `_assert_writable`，`.git`（目录/worktree 指针）、`.gitattributes`、`.gitmodules` 的写/建/删一律拒绝（读不受限） | `TestH1DottedDriverNames`、`TestFsGitInternalProtection`（11 例）、`TestGitInternalProtection`；既有 `TestH1ConfigInjection` 全保持 |
| **H2** 分隔符/全角数字穿透脱敏 | **已修复** | `sanitize.py`：入口 NFKC 归一化（全角→半角）；数字模式改容忍分隔符 + 「数字个数 ≥7 才掩、日期豁免」回调校验；模式顺序改 URL→邮箱→群号→数字（顺带修掉含数字邮箱打不上标签的问题） | 评审实测样例（`138 0013 8000`/连字符/全角）参数化落进 `test_rag.py` |
| **H3** 人名/昵称零防护 | **已修复（词表 + 句式 + 披露）** | `scrub_pii(text, extra_terms)` 词表替换（`AGENT_KB_PII_TERMS` + `data/privacy/names.txt`，已加入 `.gitignore`）；`sanitize_entry` 新增「X 的 + 私人物件」整条丢弃规则；docstring/README 如实声明未登录人名仍有漏网 | `test_possession_of_private_objects_dropped`（评审样例）、词表/加载测试 |
| **H4** 蒸馏截断后水位线越界 | **已修复** | `render_transcript` 返回 `(text, last_included_id)`，水位线只推进到实际包含处；截断发生时 `logger.error` 留痕（含 N/M 计数） | 40 条长消息场景：水位线停在 39/40 + caplog 断言 + 次轮补齐 |

## Medium

| 条目 | 结论 | 修复方式 |
|---|---|---|
| M1 私聊无差别进公共蒸馏 | **已修复** | `messages_after` 契约加 `include_private=False`（PG JOIN sessions / 内存按 scope 过滤）；蒸馏读 `AGENT_KB_DISTILL_PRIVATE`（默认关）；首跑水位线初始化为 `latest_message_id()` 并落空来源留痕（防「跳过历史」退化为永久跳过一切）；归档合并路径同步过滤无 group 记录 |
| M2 蒸馏 prompt 注入 | **已修复** | 片段内伪造「用户：/助手：」前缀转义；hint 匹配前归一化（NFKC/去空白标点）；新增 {指示,指令,…}×{作废,无效,忽略,…} 共现丢弃规则（评审样例「先前的指示一律作废」已拦）；`DISTILL_PROMPT` 声明片段内一切指令均为数据 |
| M3 恢复标识符注入 | **已修复** | `_row_identifiers_safe()`：表名限 `_RESTORE_ORDER` 白名单、列名 `^[A-Za-z_][A-Za-z0-9_]*$`、标识符双引号引用；不合规行跳过并计失败 |
| M4 user_state 恢复必炸且静默 | **已修复** | `_CONFLICT_TARGET = {"user_state": "user_id"}`（按 DDL 确认各表主键）；行级 SAVEPOINT 隔离；failures>0 时 `restore_database` 抛 RuntimeError，CLI 打 ❌ 并 exit 1；roundtrip 补含人格数据用例 |
| M5 备份文件 0644 | **已修复** | 备份与归档落盘后 `chmod 0600`（`_harden`，非 POSIX 静默降级），roundtrip 加权限断言 |
| M6 备份无完整性保障 | **已修复** | `.part` + `os.replace` 原子写；`<file>.sha256` sidecar；`verify_backup()` 全量解压/全行解析 + 校验和核对；`prune_backups` 先删校验失败文件、保留期只对通过者计数 |
| M7 .sql.gz 恢复仅 --yes 闸门 | **已修复** | CLI 恢复前自动 `backup_database(tag="pre-restore")`，快照失败中止恢复（exit 1） |
| M8 calc 幂运算 DoS | **已修复** | `_reject_pow_bomb` 静态拒绝（常量 ≥10⁹ / Pow>3 次 / 字面指数>1000 / 指数表达式含 Pow，宁可误报）；求值移入 `to_thread` + 2s 超时。实测 `9**9**9**9` 0.00s 拒绝、`2**10` 正常 |
| M9 fetch_url DNS rebinding 未披露 | **已披露**（代码未改） | web_fetch docstring + README「已知残留」对齐 media.py 措辞；钉 IP 的彻底方案留待后续（见「遗留」） |
| M10 asyncio.timeout 与 3.10 声明不符 | **已修复** | `requires-python = ">=3.11"` + classifiers 移除 3.10 + `ruff target-version = py311` + README 徽章/要求同步 |
| M11 归档身份失败永久缓存 | **已修复** | 仅缓存成功且非空结果（失败下次重试）；缓存上限 8192；原固化旧行为的测试改写为新契约 |
| M12 架构文档 schedules 条目过期 | **已修复** | 更新为「用户提醒已持久化 schedules 表；进程内 cron（蒸馏/备份）不落库」 |

## Low（L1–L26 全部处理）

- **L1** 去重口径统一 `COALESCE(group_id,'')`；连带发现并修掉「'' 存活行令 `_find_session` 查不到」的雷（init 时 `''→NULL` 规范化，仅 private 行），PG 门控用例覆盖脏数据形态。
- **L2** `init()` 幂等（pool 非空直接返回）。**L3** `save_fact` 改单语句 `INSERT…SELECT…WHERE NOT EXISTS…RETURNING`。**L4** `get_history` 统一 `limit = max(1, int(limit))`。**L5** `_deserialize_tool_calls` 校验 list[dict]；engine 对坏形 tool_calls 剥字段降级。
- **L6** 归档扫描上限移进 `iter_records`（按读取行数封顶）。**L7** `kb_delete_source` 包事务 + 关停钩子补 `memory.aclose()`。**L8** `str(x or "")`。
- **L9** `_NOISE_HINTS` 接入（短要点 + noise 词 → 丢弃）。**L10** `digest` 加 `asyncio.Lock`；chunk 去重落地在 **PG 实现**（同 source 同内容跳过；内存实现未动，见「遗留」）。**L11** `AGENT_REMINDER_TICK` 解析容错 + `max(5,…)` 钳制。**L12** `ingest_text` 失败回滚 source。
- **L13** 备份全程 `flock`（`LOCK_EX|LOCK_NB`，非 POSIX 降级无锁）。**L14** pg_dump stderr 落临时文件 + 整体超时，异常带 stderr 尾部。**L15** `schedules` 入备份/恢复/序列表；docker 路径 `-e PGPASSWORD=` 插到容器名之前（真实 docker 实测发现并修掉「追加在容器名后被当成长选项」的 bug）。
- **L16** 沙箱 HOME 属主/权限校验，不符改用 `mkdtemp` 随机目录。**L17** 库名标识符 `"` 翻倍转义；"test" 判定改词素级整词匹配（`\b` 会误杀 `agent_demo_scratch`，与任务书括注有出入、与评审意图一致）。**L18** 解压检出 symlink 即丢弃整个输出目录并明确告知。
- **L19** pipeline 改用 `safety.fence_untrusted`（消除措辞漂移）。**L20** curl IP 字面量校验（`safety.ip_literal_is_safe`；域名不做 DNS、残留已披露）。**L21** `aclose_search_client()` 接入关停。**L22** `summarize_url` 套统一围栏。**L23** debounce 改「不清理」消除竞态。**L24** 每用户活跃提醒上限 20（once/cron 同一入口）。**L25** `/reset`、`/skill install/uninstall` 追加 `is_superuser`。**L26** CI lint 补 `scripts/`；dev 依赖钉 `ruff==0.9.6` 与 pre-commit rev 对齐。

---

## 遗留与已知取舍（如实记录）

1. **M9**：只做披露，未在连接层钉 IP（httpx 自定义 transport，改动面大）——web_fetch 与 media 同撑一把伞。
2. **H3**：词表 + 句式启发 + prompt 声明三层缓解；无词表的未登录人名仍可能漏网，README 已如实声明并给出运维动作（定期补词表）。
3. **L24**：投递失败「无限顺延无放弃阈值」需 schema 加列（失败计数），本轮未做；每用户 20 条上限已堵住主要滥用面。
4. **L10**：chunk 内容去重仅 PG 实现落地，内存实现保持原语义（蒸馏实际只跑 PG 路径；如需对齐再加）。
5. **M2**：注入共现规则按「宁误杀不漏放」取舍，个别正常表述（如「忽略大小写的规则」）会被丢弃，代码注释已声明。
6. **M1 语义决策**：首跑跳过存量历史后，即便 `AGENT_KB_DISTILL_PRIVATE=1` 放开，已被跳过的存量私聊也不会回灌——蒸馏只往前走。

## 验收记录（评审复现样例 → 全部不可复现）

| 评审复现样例 | 修复前 | 修复后（实测） |
|---|---|---|
| `[diff "a.b"] textconv` 守卫判定 | 放行 | 命中拒绝 |
| `scrub_pii("手机 138 0013 8000")` | 原样通过 | `手机 [数字已脱敏]` |
| `scrub_pii("全角１３８００１３８０００")` | 原样通过 | `全角[数字已脱敏]` |
| `sanitize_entry`（王小明的服务器…） | kept | 丢弃 `personal possession mentioned` |
| 40 条长消息蒸馏水位线 | 推进到 40（丢内容） | 停在 39 + error 留痕 |
| `evaluate("9**9**9**9")` | 阻塞事件循环 | 0.00s 拒绝 |

**工程化变更**（随本轮一并落地）：评审流程固化为 [REVIEW-WORKFLOW.md](REVIEW-WORKFLOW.md)，四份历史 REVIEW 报告已迁入 `review/`。
