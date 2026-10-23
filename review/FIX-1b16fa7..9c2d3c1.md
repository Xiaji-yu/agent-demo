# FIX-1b16fa7..9c2d3c1

对应评审：[REVIEW-1b16fa7..9c2d3c1.md](REVIEW-1b16fa7..9c2d3c1.md)（4H / 19M / 18L，主代理逐条实证后全量修复）

**分支**：`fix/review-1b16fa7-fixes`（从 release/1.0.1-beta 同点切出，未直接堆在旧 release 分支）
**验证**：默认套件 **2901 passed / 60 skipped**；PG 全新空库（`qqagent_test` 重建）**2974 passed / 9 skipped**；`ruff check` + `ruff format --check` 双绿（0.9.6=pin）；`RUN_PERF=1` 9 无劣化；`check_review_docs.py --check-stats` 通过；CI 待推送后由 #150+ 复核。

---

## H（4 条，全部修复 + 回归 + 变异复核）

| # | 修复方式 | 改动 |
|---|---|---|
| H1 | `_feed_restore_stdin` 改 **COPY 块状态机**：`COPY … FROM stdin;`→`\.` 之间一行不滤（数据行是原文，字段含 `transaction_timeout` 子串也必须保留）；块外仅删顶格 `^SET transaction_timeout` | `agentcore/backup/db_backup.py`；回归 `tests/test_persistence.py::TestFeedRestoreStdinFilter::test_copy_data_line_containing_substring_survives`（**变异复核**：改回子串过滤→红） |
| H2 | preview 与写第一段对 `secret=True` 键**只回 `is_set` 布尔**；`cw.masked(value, secret=)` secret 一律 `***`（旧口径只认 `sk-` 前缀，URL 类凭据原样漏出；preview 是零 2FA 只读路由） | `plugins/qq_agent_adapter/web.py`、`config_write.py`；回归 `test_preview_never_echoes_secret_values` / `test_write_first_stage_masks_non_sk_secret`（**变异复核**：还原旧回显→红） |
| H3 | LLM_MODEL 预探测移到 `_consume_confirm` **之后**，探测失败**退还 nonce**（`_used_confirms.pop`）——原「失败时码还有效、改名可直接重试」的诉求以退还确认凭证实现；`probe_failure_detail` 对齐 llm 侧 `provider_probe_detail` 口径**只回状态码**（连带收口上轮 L5 的 body 回显） | `plugins/qq_agent_adapter/web.py`、`model_switch.py`；回归 `test_llm_model_probe_not_run_without_valid_totp` / `test_llm_model_probe_failure_refunds_nonce` + `test_http_status_surfaces_code_only`（**变异复核**：探测挪回确认前→红） |
| H4 | challenge HMAC 加入**时间戳段**（30s 时效，ts 随 token 本体 `f"{ts}:{digest}"` 传输，验证端拆回验窗，10 个调用点零签名改动）+ **同源 IP 失败计数**（15 分钟 5 次锁，成功清零，10 个调用点经 `_client_ip(request)` 传入）；nonce 失败路径**不**消费（管理员手滑输错码不必重走两段；爆破面由计数兜底，5 次/15 分钟/IP × 3 有效码/10⁶ ≈ 期望百年量级——取舍记录） | `plugins/qq_agent_adapter/web.py`；回归 `test_h4_challenge_expires_after_window` / `test_h4_failed_totp_locks_after_limit`（**变异复核**：去时效+去限速→2 例全红） |

## M（19 条）

| # | 修复 | 回归/说明 |
|---|---|---|
| M1 | `/2fa` 群聊触发拒绝并提示转私聊（`admin.py` handler 内 session 判定） | `tests/test_admin_import.py::TestTwoFaCommandPrivacy` 3 例（变异：去群聊判定→红） |
| M2 | 文档口径勘误：AGENTS.md / README / operations.md 改「立即备份、删除、下载均 2FA；校验为只读动作（审计+并发互斥）」 | verify 路由不加 2FA 是设计（只读+锁+审计），失真在文档 |
| M3 | **降级为记录项**：`SEARCH_BASE_URL`/`EMBEDDING_BASE_URL` 单独改写不构成凭据外带——settings 单键写不继承 key（key 本就在 .env 独立），换端点是管理员显式授权行为且有 2FA+审计；与 llm providers「编辑继承 key」场景不同。不改代码 | 复审结论记录在此 |
| M4 | `search_multi` 改 `_load_providers()` 判空（删残留 env api_key 检查） | `test_m4_multi_uses_providers_not_env`（变异：回退 env 判空→红） |
| M5 | `_search_probe_detail` 只回状态码 + 类型名（不回显响应体/URL） | `test_probe_detail_hides_upstream_body`（变异：还原 body 回显→红） |
| M6 | 「改后重启生效」勘误：providers 每次调用热读、保存即时生效；`model_config.search.effective="live"` + 面板 degraded 提示；docs/AGENTS 同步 | `test_settings_mark_degraded_on_corrupt_json` 含 effective 断言 |
| M7 | 三源 200+错误体判定：bocha 缺 `data` / tavily·stepfun 缺 `results` 或含 `error` → `raise`（交 fallback），`_search_items_one` 同口径 | `test_m7_http200_error_body_triggers_fallback` / `test_m7_bocha_missing_data_raises`（变异：bocha 回退→红） |
| M8 | 全局墙钟：`asyncio.timeout(_total_timeout_s())` 包住 `search_web` 循环与 `search_multi` gather；新 env `AGENT_SEARCH_TOTAL_TIMEOUT_S`（默认 25，ENV_SPECS + .env.example 同步） | `test_m8_total_timeout_caps_wall_clock` |
| M9 | 面板损坏如实标记：`_search_providers_admin` 返回 `(list, degraded)`，GET 的 model_config.search.degraded，前端显示降级警示条 | 同 M6 用例 |
| M10 | U 盘挂载判定改 `mountpoint -q`（挂载点目录拔盘后常驻，`-d/-w` 全真 → 9GB 写根盘+假成功） | `test_unmounted_skips_without_writing`（变异：回退 `-d/-w`→红） |
| M11 | 远端命令拼接防护：`KEEP_DAYS` 数字 case 校验、`MIRROR_ROOT` 拒单引号、全部 `ssh -- "$HOST"` | `test_rejects_non_numeric_keep_days` / `test_rejects_quote_in_mirror_root` / happy-path 断 `--` |
| M12 | VPS unit `REQUIRE_PRIVATE_IP=1` + 脚本 `getent` 解析私网段断言（10/192.168/127/172.16-31） | `test_require_private_ip_rejects_public` |
| M13 | 三个 backup service `UMask=0077`（rsync `--no-perms` 下 ext4 目标按 umask 落盘） | unit 内容随 `TestConfigAndDocs` 类守卫；operations.md 同步 |
| M14 | mirror `backups` 目录缺失 exit 1（旧静默 exit 0 假成功） | `test_missing_backups_dir_fails_loud`（变异：回退静默→红） |
| M15 | MANIFEST 前必含清单硬断言（dump/.env/config.yaml）+ personas glob 改 `nullglob` 数组（去掉吞 IO 错误的 `2>/dev/null \|\| true`） | `test_missing_required_file_fails` |
| M16 | `title` 100 字软截断 + note 回执 + schema `maxLength`（`_fit` O(n²)：12000 字 title 实测 27s，6000 字已 6.9s 逼近 10s 超时，僵尸线程无法取消） | `test_m16_long_title_is_truncated_not_o_n_squared`（变异：去 title 门禁→红） |
| M17 | 页脚标注抽 `_footer_note(truncated, cut_by_height, chars)`：任一维度截断都显示「已截断」（旧只看高度门禁，半角 7000→6000 场景图内无标注） | `tests/test_text_card.py::TestFooterNote` 4 例（变异：回退单条件→红） |
| M18 | 三处路径 env 空串回落默认：`totp._secret_path` / `search_providers.providers_path` / `llm/providers.providers_path`（`(os.getenv(ENV) or "").strip() or _DEFAULT`）；`AGENT_2FA_SECRET_FILE=` 空串曾致 ValueError 逃逸 → /2fa 无响应 + web 写面全 500 | `test_empty_env_falls_back_to_default` ×3（totp 例含落盘验证） |
| M19 | CHANGELOG 补录后半批：2FA、备份标签、手机端、PERSONAS_DEFAULT、异地镜像/冷备、auto-file 删除 | `git log --follow` 可复核 |

## L（18 条）

- **随 H/M 顺带修（8）**：L4 masked 接 spec（随 H2）；L1 challenge 时效（随 H4，30s 过期后旧 nonce+token 自然失效，「600s 后可二次消费」随之消失）；L7 全挂报错只回类型名（随 M5 同类口径）；L14 渲染 `Semaphore(2)`（随 M16）；L5 stepfun env 层统一（回落/迁移/写面三处都认）；L6 `max_results` 夹紧 [1,20]（**行为变更**：`test_max_results_zero` 改写断言）；L18 test_totp or 逃逸（固定非当前码）；L9 远端校验区分 `NO-SIDECAR`/`CHECKSUM-MISMATCH` 退出码。
- **独立小修（6）**：L3 AGENTS.md 165→168 键；L16 features-messaging 措辞（系统不再替换回复）；L10 mirror 新鲜度闸门与远端校验收 `*.jsonl.gz`（纯 JSONL 部署不误报）；L11 三 unit `OnFailure=agent-demo-backup-failure.service`（新 unit，logger 显式标记）；L15 确定失败降级分支用例 `test_l15_definite_failure_falls_back_to_text`；L2 非 base32 secret 按损坏重生成（`test_non_base32_secret_regenerates`）。
- **显式遗留入档（4）**：L8 搜索 key 备份落 `data/backups/`（与 llm 侧同源决策，web 下载有 2FA+白名单、list_backups 不含 .bak）；L12 时钟回拨轮转（已改 mtime 排序，残留面仅「同时刻批量写入」极端场景，KEEP=8 内影响极低——部分修复）；L13 远端 pgwal/pgbase 无清理路径（当前无产出者，启用 PITR 前需补）；L17 `_CONTROL_RE` 漏 `\x0b/\x0c`（与 engine/fs/groupstats 三处既有口径一致，要修须四处同修，单独专项）。

## 上轮遗留（REVIEW-2ed7c63..1b16fa7 M1/M2）

- **persona 覆盖备份三重不一致**：`write_bytes`（0644）+ 秒级同名互覆 + 无滚动 → `_persona_backup_path`（时间戳+pid+单调序号唯一名）+ `_atomic_copy_0600`（`O_NOFOLLOW` 0600 一步到位）+ `_prune_persona_backups`（每人格保留 5 份）。
- **`PersonaManager.save` tmp 0644 窗口**：`os.open(..., 0o600)` 创建即 0600（chmod 保留为已存在 tmp 的兜底）。
- 回归：`test_save_overwrite_backup_is_0600_unique_and_rotated`（8 次覆盖：唯一名/0600/滚动/正文本体 0600 四断言）。

## 行为变更（有意，需知会）

1. `search_web`/`search_items` 的 `max_results`：透传/0 吞默认 → 统一夹紧 [1,20]（0 夹到 1）。
2. 搜索全挂报错文案只回异常类型名（不再含自配端点 URL/内网 host）。
3. `/2fa` 群聊触发不再返回验证码（转私聊提示）。
4. mirror `backups` 目录缺失由「静默成功」改为失败（部署路径变更会立刻暴露）。
5. U 盘未挂载判定由目录存在性改为 `mountpoint -q`（拔盘后常驻目录不再被当作已挂载）。

## 验收记录

- 默认套件：2901 passed / 60 skipped（修复前 2858/60，**+43** 新回归）
- PG 套件（全新空库 `qqagent_test`，DDL 从零建）：2974 passed / 9 skipped
- ruff check + format --check：双绿（本地 0.9.6 = pyproject pin）
- `RUN_PERF=1`：9 passed，与 perf/baseline.json 无劣化
- `check_review_docs.py --check-stats`：索引完整 / 产物均在 review/ / BACKLOG 计数与实测一致
- 变异复核：H1/H2/H3/H4、M1/M4/M5/M7/M9/M10/M14/M16/M17 共 13 处「改坏必红」全部验证
- 脚本回归：`tests/test_backup.py` 新增 11 例（PATH mock ssh/rsync/getent/mountpoint 跑真实脚本，ssh mock 把远端命令映射进 tmp 目录真实执行——嵌在脚本里的远端 shell 逻辑也被覆盖）
