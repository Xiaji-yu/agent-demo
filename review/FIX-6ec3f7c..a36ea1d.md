# FIX 记录：REVIEW-6ec3f7c..a36ea1d

对应评审：[REVIEW-6ec3f7c..a36ea1d.md](REVIEW-6ec3f7c..a36ea1d.md)（**1H / 17M / 25L**）。
修复范围：`6ec3f7c..a36ea1d` 已提交区间 + 未提交的点歌语音功能预审。

## 验证口径

- **全量套件（有 ffmpeg）**：`1346 passed / 44 skipped`（修复前 1231 / 44；净新增 **115 条**）
- **全量套件（无 ffmpeg，CI 等价）**：`1340 passed / 50 skipped`
  —— 用「PATH 指向一个独缺 ffmpeg 的符号链接目录」复现 GitHub `ubuntu-latest` 的等价环境
- **`ruff check` + `ruff format --check`**：全绿（ruff **0.9.6**，与 `pyproject.toml` 的 pin 一致）
- **CI（推送后实测，销项 M1 的存疑）**：`push origin main` → run **#66**（`8ac1cbd`）
  `completed / success`；作业步骤级核对确认 `Install system deps (ffmpeg)` **存在且成功**
  （此前只能在本地用"独缺 ffmpeg 的 PATH"模拟 GitHub runner，现已由真实 runner 证实）
- **变异复核**：**51 处**「改坏实现 → 对应用例必须失败 → 还原」，**全部被捕获**
  （G1–G2 16 处、G3 13 处、G4 11 处、H-1 心跳 1 处，另 10 处为初版存活后修正用例再复跑）
- 变异全部在 **`/tmp` 副本**上执行（`/tmp/mut.py` 统一harness：`tar` 导出 → `PYTHONPATH` 指向副本
  并断言 `agentcore.__file__` 落在副本内 → 逐条改坏/复跑/还原）
  —— 首版曾在活仓库上跑，脚本被工具超时 SIGTERM 杀掉时**留下过一处未还原的变异**
  （`client.py:80`），当场发现并修复；此后改为副本方案，杜绝该类事故

## 逐项修复

### H1 —— 断连家族被判「确定失败」→ 重复投递

- **修复方式**：`is_uncertain_send_error` 从 `type(err).__name__ == "NetworkError"` 的**精确类名**
  匹配改为 `isinstance`：`httpx.TransportError` 全家族归「不确定」；并**显式排除**
  `ConnectError` / `UnsupportedProtocol`（请求从未离机，判确定失败才允许安全重发）。
- **改动文件**：`agentcore/skills/file_sender.py`
- **回归测试**：`tests/test_file_sender.py::TestUncertainCoversTransportFamily`
  （9 类必须不确定 / 2 类必须确定失败 / 守住根因：这些类型名都不等于 `NetworkError` / 基类本身也不确定）
- **变异**：退回类名匹配 → 2 条用例失败 ✓

### M1 —— 未提交的音乐功能在 CI 必红

- **修复方式**：① `ci.yml` 增加 `apt-get install -y --no-install-recommends ffmpeg`
  （镜像清单实测不含 ffmpeg）；② 把两条**依赖宿主环境**的用例改为自足：
  `test_reports_missing_pysilk` 同时桩掉 `shutil.which`（隔离 ffmpeg 前置分支）；
  注册参数用例把 `silk_available` 桩在**源模块**上（`reload` 会重绑 from-import）。
- **改动文件**：`.github/workflows/ci.yml`、`tests/test_music.py`、`tests/test_music_route.py`
- **回归测试**：新增 `TestFfmpegHasTimeout` 同时反向验证了本项纪律（它初版同样依赖宿主 ffmpeg，
  被"无 ffmpeg 全量"抓出后已解耦）
- **验证**：有/无 ffmpeg 两种环境下音乐套件与全量套件**均全绿**

### M2 —— embedding 退避无差别作用于启动探测与热路径（最坏 1050s）

- **修复方式**：按路径**分离预算**——
  `probe_dim` 单次尝试（`retry_count=0`）；`embed_many(interactive=True)` 用
  `EMBEDDING_INTERACTIVE_RETRY_COUNT`（默认 1）+ `EMBEDDING_INTERACTIVE_BUDGET`（默认 30s 墙钟上限，
  退避会越限时**直接失败而不 sleep**）；批量摄取保持原 `retry_count/retry_delay`（正确性优先）。
  另在 `on_startup` 给 `probe_dim` 套 `asyncio.wait_for(EMBEDDING_PROBE_TIMEOUT, 默认 10s)` 做纵深防御。
- **改动文件**：`agentcore/embedding/client.py`、`plugins/qq_agent_adapter/__init__.py`、`.env.example`
- **回归测试**：`tests/test_embedding.py::TestRetryBudgetSplit`（probe 单次 / 交互独立次数 /
  越限不 sleep / 批量仍长预算 / 退避序列 `[10,20,30,40]`）、
  `tests/test_admin_import.py::TestProbeTimeoutBudget`
- **变异**：probe 退回批量预算、去掉墙钟上限 → 均失败 ✓

### M3 —— `/usage`（及 `/aihelp`）同步渲染阻塞事件循环

- **修复方式**：`/usage` 的渲染改 `await asyncio.to_thread(render_table_png, table)` 并把
  「取账本 → 渲染」整体包异常隔离（失败降级纯文本，不再抛穿 handler）；`/aihelp` 的
  `render_help_image()` 同样 `to_thread`（第三处同步 Pillow）。
- **改动文件**：`plugins/qq_agent_adapter/admin.py`
- **回归测试**：`tests/test_usage.py::TestUsageHandlerOffload`（断言 `to_thread` 被用于
  `render_table_png` / 渲染异常降级为文本 / 账本读取失败有明确回复）、
  `TestHelpRenderOffloaded`（`/aihelp` 必须卸载）
- **变异**：两处 `to_thread` 改回同步 → 均失败 ✓

### M4 —— 人格成长文本不过 `safety.py` 围栏

- **修复方式**：注入侧与 facts 路径对齐 —— `neutralize_fence_lookalikes(growth_text)`；
  写入侧（`confirm` 合并结果落库前）同样打散，保证「审核过的字符串 = 入库的字符串 = 注入的字符串」。
- **改动文件**：`agentcore/loop/engine.py`、`agentcore/personas/growth.py`
- **回归测试**：`tests/test_persona_growth.py::TestGrowthFenceNeutralized`、
  `TestWriteSideNeutralization`（含"入库与注入逐字一致"）
- **变异**：注入侧不打散 / 写入侧不打散 → 均失败 ✓

### M5 —— 「管理员确认」实为白名单群任意成员

- **修复方式**：确认门在 **rule 与 handler 两处**都要求 `is_superuser`；
  `GrowthManager.confirm(code, confirmer_id)` 的 `confirmer_id` 改为**必填**并在内部再判一次
  （省略参数即等于绕过，故不给默认值）；被拒时**不消费**确认码。
- **改动文件**：`plugins/qq_agent_adapter/admin.py`、`agentcore/personas/growth.py`
- **回归测试**：`tests/test_persona_growth.py::TestConfirmRequiresSuperuser`
  （非管理员不得兑换/不得落库/不消费码、管理员可兑换、`confirmer_id` 必填）、
  `tests/test_admin_import.py::TestGrowthConfirmRequiresSuperuser`（rule 级 + self 过滤）
- **变异**：去掉确认者判定、去掉 rule 门 → 均失败 ✓

### M6 —— 成长接线零覆盖（删掉接线全仓不红）

- **修复方式**：补齐**端到端**用例（`engine.run()` 真跑，断言写进 store 的成长文本进入 system prompt），
  取代原先"直接给 `_build_system_prompt` 喂参"的捷径；补 admin 侧权限门用例。
- **改动文件**：`tests/test_persona_growth.py`、`tests/test_admin_import.py`
- **回归测试**：`TestGrowthWiringCoverage`（有用例/无用例两侧）
- **变异**：把 `engine.py` 的取数行改成 `growth_text = ""` → 用例失败 ✓（旧实现下全仓无感）

### M7 —— `.env.example:56` 行内注释被 dotenv 当成值

- **修复方式**：注释移到上一行，值行只留 `EMBEDDING_BASE_URL=`（与其它空值项一致）。
- **改动文件**：`.env.example`
- **验证**：`dotenv_values(".env.example")` 逐键扫描 → **0 处**被注释污染（修复前 `EMBEDDING_BASE_URL`
  的值是一整条注释，配 `EMBEDDING_API_KEY` 后会以 `UnsupportedProtocol` 响亮失败）

### M8 —— 音乐功能文档缺失 + `AGENT_MUSIC_CACHE_MB` 契约漂移

- **修复方式**：① `.env.example` 补全部 **9 个** `AGENT_MUSIC_*`（含默认值/单位/边界语义说明）；
  ② README 新增「点歌 / 放歌（群语音）」章节（触发两层、默认关闭、白名单、冷却、时长上限、
  下载安全、缓存、`base64://` 的 ENAMETOOLONG 坑）；③ 实现 `AGENT_MUSIC_CACHE_MB`（此前 AC 声明过但
  **代码从不读取**），并让磁盘缓存按 mtime 最旧优先压回配额。
- **改动文件**：`.env.example`、`README.md`、`plugins/qq_agent_adapter/music_route.py`
- **回归测试**：`tests/test_music_route.py::TestCacheMbEnv`、`TestDiskPurge`
- **验证**：脚本比对「代码实际读取的 `AGENT_MUSIC_*` 全集」与模板声明 → 9/9 齐备
- **变异**：`_env_cache_mb` 直接返回默认 → 用例失败 ✓

### M9 —— 私聊无 ACL + 先下载编码再拒绝 + 吞账号冷却

- **修复方式**：`_rule` 对**私聊**补 `is_allowed`（与主聊天路径一致：私聊仅 superuser）；
  把"能否发送"提到**冷却/搜索/下载/编码之前**（`sender` 只有 `send_group_msg`，私聊本就发不出）。
- **改动文件**：`plugins/qq_agent_adapter/music_route.py`、`review/AC-music-playback.md`（D5 修订）
- **回归测试**：`TestPrivateAclGate`（非 superuser 拒 / 放行者过）、
  `TestPlayOrchestration::test_private_refused_before_any_heavy_work`
  （断言 search/download/encode/send **全为 0** 且冷却未被消耗）
- **变异**：去掉私聊 ACL、把拒绝挪回重活之后 → 均失败 ✓

### M10 —— `song.id` 未校验直接进路径（绝对路径 / `..` 逃逸）

- **修复方式**：`_cache_path` 用 `^[A-Za-z0-9_-]{1,64}$` 校验 id，并二次断言 `resolve()` 仍在缓存根内；
  不合法即拒绝下载。
- **改动文件**：`plugins/qq_agent_adapter/music_route.py`
- **回归测试**：`TestCachePathValidation`（绝对路径/`..`/超长 拒绝，正常 id 通过）、
  `TestPlayOrchestration::test_bad_song_id_rejected`
- **变异**：去掉校验 → 用例失败 ✓

### M11 —— 缓存不省下载 + 磁盘无界 + 失败留残file

- **修复方式**：查缓存的判断提到下载**之前**（命中即完全跳过下载与编码）；编码成功后
  `src.unlink(missing_ok=True)`；新增 `_purge_disk_cache` 按 mtime 压回 `AGENT_MUSIC_CACHE_MB`。
- **改动文件**：`plugins/qq_agent_adapter/music_route.py`
- **回归测试**：`TestPlayOrchestration::test_cache_hit_skips_download_and_encode`（第二次 download==1）、
  `test_disk_file_removed_after_encode`、`TestSilkCache`（命中/淘汰/单条超配额保留 1 条/空值忽略）
- **变异**：跳过缓存查询、不删文件 → 均失败 ✓

### M12 —— SSRF「逐跳复检」的开关无护栏

- **修复方式**：不是改实现（实现本就正确），而是**补上守卫**：测试替身保留 `**kwargs`，
  用例断言 `follow_redirects is False` 且**键必须存在**（防止依赖 httpx 默认值）。
- **改动文件**：`tests/test_music.py`
- **回归测试**：`TestRedirectFollowingIsDisabled`
- **变异**：`follow_redirects=False → True` → 用例失败 ✓（修复前 69 passed 全绿）

### M13 —— FIX 验收数字在干净检出不成立

- **修复方式**：在 `review/FIX-46c85d1..6ec3f7c.md` 的「验证口径」上方加**勘误块**，
  写明干净检出实测 `1 failed / 1054 passed / 43 skipped`、CI run 结论为 failure、
  由 `b885f26` 修复，并记下教训（数字须在无 `.env` 的干净检出复跑后再写）。
- **改动文件**：`review/FIX-46c85d1..6ec3f7c.md`
- **验证**：本轮所有数字均按此纪律实测（本文件顶部的两行即两种环境的真实输出）

### M14 / L3 —— §9 索引链断裂，三份文档未入索引

- **修复方式**：§9 补 `REVIEW-733f57e..46c85d1.md`；§9.1 补
  `FIX-81a8521..workdir.md` 与 `FIX-embedding-deploy-20260918.md`，并在备注列说明其
  **命名不符 §7 约定**（分别用字面 `workdir` 与日期）的理由。
- **改动文件**：`review/REVIEW-WORKFLOW.md`
- **验证**：脚本比对 `review/*.md` 与索引正文 → **无未入索引文件**

### M15 —— embedding 断连类异常不在重试集合内

- **修复方式**：`except` 集合放宽到 `(httpx.TimeoutException, httpx.TransportError)`；
  `UnsupportedProtocol`（base_url 非法）单独给配置指引并**不重试**。
- **改动文件**：`agentcore/embedding/client.py`
- **回归测试**：`tests/test_embedding.py::TestDisconnectFamilyRetries`
  （`RemoteProtocolError`/`ProxyError`/`ReadError`/`WriteError`/`CloseError` 必须重试到耗尽；
  `UnsupportedProtocol` 必须立即失败且提示 `EMBEDDING_BASE_URL`）
- **变异**：`except` 退回 `NetworkError` → 用例失败 ✓

### M16 —— 用户面文案与注释仍写已删除的旧语义

- **修复方式**：① `client.py` 模块 docstring 改写（"未配置→hash；配置了但失败→响亮失败"）；
  ② 插件注释同步（probe 单次尝试 + 运行期不再降级 hash）；
  ③ 管理员告警文案改为**按异常类型分流**（404→模型名 / 协议→base_url / 401·403→API_KEY /
  其它→超时类），并去掉被推翻的「聊天不受影响」，改为如实说明本轮影响；
  ④ `FIX-embedding-deploy-20260918.md` 加勘误："ingest 中止报错"实为"逐单元跳过并继续"。
- **改动文件**：`agentcore/embedding/client.py`、`plugins/qq_agent_adapter/__init__.py`、
  `review/FIX-embedding-deploy-20260918.md`
- **回归测试**：`tests/test_admin_import.py::TestEmbeddingHintRouting`（四条分支各断言对应 env 名）

### M17 —— `1632b72` body 声称「`.env.example` docs」与实际 diff 不符

- **修复方式**：入档勘误（写在 `FIX-embedding-deploy-20260918.md` 的勘误段；该 commit 的
  `/usage` 实际不需要新 env，代码无需改）。同 commit 的「1079 tests」经 `git archive` 干净树复跑
  **逐字属实**，故只记这一半。
- **改动文件**：`review/FIX-embedding-deploy-20260918.md`

### L 级（25 条）

| # | 修复方式 | 改动文件 | 回归/验证 |
|---|---|---|---|
| L1 | `_spawn()` 持强引用 + `add_done_callback` 释放；新增 `aclose()` 供停机取消 | `agentcore/personas/growth.py` | `TestGrowthTaskReference`（在飞被引用、完成释放、close 取消）；变异去掉持引用 → 失败 ✓ |
| L2 | `usage_cmd` 补 `rule=_not_self_message` | `plugins/qq_agent_adapter/admin.py` | `TestUsageHandlerOffload`（组合 Rule 含该检查器 + 全量 on_command 扫描，**并自检扫到 ≥10 个**防空转）；变异去掉 rule → 失败 ✓ |
| L3 | 抽 `_superuser_ids()` 统一 `isascii() and isdigit()`，两处调用点（embedding 告警/成长提议）共用 | `plugins/qq_agent_adapter/__init__.py` | `tests/test_admin_import.py::TestSuperuserIdParsing`（全角被跳过、只发 ASCII、守住 `int("１２３")==123`）；变异退回 `isdigit()` → 失败 ✓ |
| L4 | `confirm` 写成功后才 `pop`（失败保码可重试）；handler 包 try/except 回"确认失败，请稍后重试（码仍有效）" | `agentcore/personas/growth.py`、`admin.py` | `TestConfirmWriteFailureKeepsCode`；变异改回先 pop → 失败 ✓ |
| L5 | `AGENT_PERSONA_GROWTH_INTERVAL=0/负` → **关闭**（不构造 GrowthManager，admin 侧已有"未启用"分支）；脏值告警回退 30 | `plugins/qq_agent_adapter/__init__.py`、`.env.example` | `TestGrowthIntervalSemantics`；变异退回 `max(1, ...)` → 失败 ✓ |
| L6 | README 明示成长层按 **user_id** 存储（私聊养成的细节会影响其群内语气），需严格按会话隔离时用 facts | `README.md` | 文档核对 |
| L7 | 补 `/kb samples` 通知的回归用例：假 bot + **int** `self_id` 的事件，断言 `get_bot` 收到 **str** | `tests/test_admin_import.py`（`_Ev` 补 `self_id=10000`） | `test_samples_notify_uses_str_self_id`；变异去掉 `str()` → 失败 ✓ |
| L8 | §9.1 被 blockquote 截断的一行移回表内；顺带修掉另一处把表格切成两半的遗留空行 | `review/REVIEW-WORKFLOW.md` | 结构核对 |
| L9 | ffmpeg 子进程加 `timeout=_FFMPEG_TIMEOUT`（60s）+ 超时转 RuntimeError | `agentcore/music/silk.py` | `tests/test_music.py::TestFfmpegHasTimeout`（断言 `subprocess.run` 收到 timeout 且仍用 argv 列表）；变异去掉 timeout → 失败 ✓ |
| L10 | `max_seconds()` 负值**回退默认 300**（不再是 fail-open 的 0）；0 仍表示"不限制" | `plugins/qq_agent_adapter/music_route.py` | `TestConfig`（负值回退+告警 / 0=不限制）；变异关掉负值分支 → 失败 ✓ |
| L11 | `filter_by_duration` 改为在**毫秒**上比较，不再先 `//1000` | `agentcore/music/client.py` | `TestDurationBoundarySubSecond`（300.9s 必须被拒、299.999s 保留）；变异退回整秒语义 → 失败 ✓ |
| L12 | 429 优先听 `Retry-After`（受本次退避上限夹住）；`413/422` 单独分支提示调小 `EMBEDDING_BATCH` | `agentcore/embedding/client.py` | `TestRetryAfterAndBatchErrors`（听 Retry-After / 不被超大值拖死 / 413 指引 batch）；变异忽略 Retry-After → 失败 ✓ |
| L13 | 抽 `_env_clamped_int`（告警 + clamp）与 `_env_nonneg_float`（允许 0）；`EMBEDDING_RETRY_COUNT` 夹到 `[0,10]` | `agentcore/embedding/client.py` | `TestRetryEnvClamping`（脏值告警回退 / 100 夹到 10 / delay=0 合法）；变异退回无约束解析 → 失败 ✓ |
| L14 | 去掉自引用断言（`== client.retry_count + 1` → 字面量 `== 6` + 独立断言默认值 5）；补退避序列断言 | `tests/test_embedding.py` | `test_backoff_sequence_is_linear_growth` 断言 `[10,20,30,40]` |
| L15 | 把 AC A5 的「60s 编码 < 2s」落成用例 | `tests/test_music.py` | `TestSilkPerformance`（实测 ≈1.2s；无 ffmpeg 时 skip） |
| L16 | `REVIEW-WORKFLOW` 的产物布局表补 **AC** 与 **复盘** 两类（此前只列三类，而仓库已在用） | `review/REVIEW-WORKFLOW.md` | 规范自洽 |
| L17 | §9.1 的 FIX 行由「2H/6M/8L 全修」改为如实分列（L-1 并入 M-1、**L-8 降级入档**），并标注 H-1/L-2 在 HEAD 的回退 | `review/REVIEW-WORKFLOW.md` | 与 FIX 文档"遗留/降级"段一致 |
| L18 | 补 `_play` 编排顺序、缓存命中、`SilkCache`、`_music_env_ready` 两态、`sys.modules` 等用例 | `tests/test_music_route.py` | `TestPlayOrchestration`/`TestSilkCache`/`TestMusicEnvReadyGate` 等；6 处原存活变异现全部被捕获 ✓ |
| L19 | 新增**总字符数**护栏 `_MAX_TOTAL_CHARS=20000`（超限再砍行并注明）——护栏从"限输入"补齐到"限墙钟" | `agentcore/render/table.py` | `TestTotalCharsGuard`（三护栏顶满 <5s、确会截断、小表不受影响）；**实测最坏 24.0s → 0.72s**；变异关掉护栏 → 失败 ✓ |
| L20 | 源头修复：`budget._normalize_bucket` 统一规范化明细桶的**值**，`_day()` 与 `today()` 共用（`total()` 原有守卫保持） | `agentcore/budget.py` | `TestLayeredRobustnessIndependently`（budget 层断言值必为 dict；admin 层用**脏 stub** 独立验证）；变异各自关掉一层 → 均失败 ✓ |
| L21 | 新增 `route_context()` 上下文助手；蒸馏（`kb:distill`）与成长提议/合并（`admin:growth-*`）的 LLM 调用纳入路由统计，使「对话请求」与「按路由之和」对平 | `agentcore/budget.py`、`agentcore/rag/distill.py`、`agentcore/personas/growth.py` | `tests/test_budget.py::TestRouteContext` + `TestBackgroundJobsHaveRoutes`；变异去掉 route 包装/不还原 token → 均失败 ✓ |
| L22 | 补 `handle_usage` 的三条用例（卸载/渲染异常降级/账本失败），并把该 coroutine 纳入覆盖 | `tests/test_usage.py` | `TestUsageHandlerOffload` |
| L23 | 行截断用例由恒真改为**可失败**：`_MAX_ROWS` 放开后断言 PNG 高度显著更大；另加"注释文本确实被绘制"（打桩 `ImageDraw.text`） | `tests/test_table_render.py` | 变异删掉行护栏 → 用例失败 ✓（修复前该用例照样通过） |
| L24 | `tests/test_outbound.py` 加 autouse fixture 隔离 12 个出站相关 env（`b885f26` 只 pin 了其中一个） | `tests/test_outbound.py` | 设 `AGENT_TABLE_TO_IMAGE=0 AGENT_REPLY_SINGLE_MAX=300` 后**仍 76 passed**（修复前 4 failed） |
| L25 | 路由键脱敏：`private:<QQ号>` → `private:1***6`（群号保留，群号本就地可见）；并在**非管理员**回复中整段省略"按路由"明细 | `plugins/qq_agent_adapter/admin.py` | `TestNonAdminRouteVisibility`（非管理员回复不含路由行/含"仅管理员可见"；管理员保留）+ `test_private_route_is_masked`；变异 `include_routes=True` / 不脱敏 → 均失败 ✓ |

### 附带修复（评审未列出）

- **`test_render_does_not_block_event_loop` 的时序 flaky**：全量负载下 `max(ticks) < 0.2` 偶发假红
  （本轮实测 1 次）。加固为：保留**强信号**（`ticks` 非空且 ≥5 次——旧缺陷下心跳一次都跑不成），
  时序阈值放宽到 `0.5s`（原缺陷 7.3s，仍保留 ~14× 裕度）。变异复核确认改回同步渲染仍**必失败**。

## 遗留 / 降级项

- **(a) 真机 QQ 投递未验证**：音乐语音的实际送达、40+ 秒语音是否被 QQ 接受、`/usage` 图片观感，
  均需真实协议端与 QQ 环境；该功能**不得**据此声称可上线。
  （`ci.yml` 的 ffmpeg 安装**已销项**：run #66 步骤级确认执行成功，见上「验证口径」。）
- **(b) PG 门控用例（`TEST_DATABASE_URL`）**：本轮**未改** `memory/store.py` 的行为语义
  （仅 `budget.py`/`embedding`/`personas`/`admin` 等非存储面），本机默认套件 44 skipped 的 PG 面未跑。
- **(c) 非 editable 安装 / wheel 打包字体**：`data/fonts` 不在 wheel、系统字体候选分支，
  与上一轮 FIX 的降级项一致，本机不可触发。
- **(d) budget 多进程丢账**：已入档的历史遗留（`FIX-bbd8913..f6dffcc` §8），本轮未新增根因、未修。
- **(e) `LLM_EMBEDDING_MODEL` 未入 `.env.example`**：`BACKLOG.md:193` 已记为已知死代码路径，
  维持原判（不补文档）。
- **(f) `AGENT_MUSIC_*` 与 `ALLOWED_GROUPS` 的独立性**：AC B1 显式要求"不继承"，
  故只被加入音乐白名单的群能收语音而普通聊天可能被 ACL 拒——设计选择，仅记录。

## 验证命令（可复跑）

```bash
# 全量（有 ffmpeg）
.venv/bin/python -m pytest -q                     # 1346 passed / 44 skipped

# 全量（CI 等价：PATH 独缺 ffmpeg）
python3 - <<'EOF'
import os, pathlib
b = pathlib.Path("/tmp/nofm/bin"); b.mkdir(parents=True, exist_ok=True)
for d in ("/usr/local/bin", "/usr/bin", "/bin"):
    for f in pathlib.Path(d).glob("*"):
        if f.name == "ffmpeg" or (b / f.name).exists():
            continue
        try: os.symlink(f, b / f.name)
        except OSError: pass
EOF
PATH=/tmp/nofm/bin .venv/bin/python -m pytest -q  # 1340 passed / 50 skipped

# lint（版本须与 pyproject 的 pin 一致）
.venv/bin/python -m ruff check agentcore plugins tests bot.py scripts
.venv/bin/python -m ruff format --check agentcore plugins tests bot.py scripts

# 变异复核（在 /tmp 副本上跑，不触碰仓库）
.venv/bin/python /tmp/mut.py /tmp/m_g12.json   # 16/16
.venv/bin/python /tmp/mut.py /tmp/m_g3b.json   # 13/13
.venv/bin/python /tmp/mut.py /tmp/m_g4b.json   # 11/11
.venv/bin/python /tmp/mut.py /tmp/m_hb.json    #  1/1
```

> 修复阶段未自动 commit / push（待用户指示）。所有改动均在 `agentcore/`、`plugins/`、`tests/`、
> `review/`、`.env.example`、`README.md`、`pyproject.toml`、`.github/workflows/ci.yml` 内，
> 未新增根目录评审产物（§7 自检为空）。

---

## 追加：真机联调发现的两个生产阻断（2026-09-19，来自首次线上点歌）

首次在真实 QQ 群点歌（`@机器人 点歌 稻香`）暴露两个**单测完全抓不到**的问题。
两者都源于**测试替身与真实对象不一致**，已修并补齐"用真实对象"的回归。

### P1 —— `event.reply(...)` 在 OneBot v11 事件上不可调用（功能一个消息都发不出去）

- **现象**：线上 `TypeError: 'NoneType' object is not callable`，`music_route` 的
  **14 处**回复全部失败——包括"音频地址不可用""放歌出错啦"这类错误提示，用户侧完全静默。
- **根因**：OneBot v11 的 `MessageEvent.reply` 是**「引用消息」数据字段**
  （`Optional[Reply]`，普通消息为 `None`），**不是**协程方法。仓库既有范式是
  `matcher.send/finish`。旧测试替身 `_FakeEv` 自己定义了 `async def reply`，
  把 API 上的这个事实完全掩盖了。
- **修复**：`_play(event, song_name, reply)` 改为**注入**回复函数，handler 传
  `music_matcher.send`；测试不再使用鸭子类型替身，一律构造**真实**
  `GroupMessageEvent` / `PrivateMessageEvent`。
- **回归**：`tests/test_music_route.py::TestEventReplyIsNotCallable`
  （钉住"真实事件 `reply is None` 且不可调用"、用真实事件跑通成功与失败两条路径）。
  该用例是**行为级**的：谁再把 `event.reply` 写回来，立刻 TypeError 失败。
- **变异**：把任一回复点改回 `event.reply` → 用例失败 ✓

### P2 —— content-type 不可靠导致正常歌曲被拒

- **现象**：线上 `UnsafeURLError: content-type 非音频：application/octet-stream`
  ——而下载到的其实是**合法 MP3**（同 URL 换 UA/scheme/换时间均复现不出，是网易云
  CDN 的**节点差异**）。
- **根因**：旧实现要求 `content-type` 必须以 `audio/` 开头。CDN 会返回
  `application/octet-stream` 甚至缺失，于是正常点歌**间歇性**失败。
- **修复**：内容校验改为两步——① content-type 只用来**快速否掉确定是错误页**的类型
  （`text/*`、`application/json`、`application/xml`）；② 真正判据是下载后的
  **魔数嗅探**（`ID3`/帧同步/`fLaC`/`OggS`/`RIFF+WAVE`/`ftyp`/Matroska），
  不通过则删掉刚落盘的脏文件并报错。比信任响应头更可靠，也更安全。
- **回归**：`TestFetchAudio::test_octet_stream_with_mp3_body_is_accepted`（线上形态）、
  `test_missing_content_type_defers_to_magic_bytes`、`test_error_page_is_rejected_early`；
  测试假 body 也统一换成带合法魔数的 `MP3_BYTES`（旧用例用 `b"audio"` 就能过，
  说明它们其实没验证内容校验）。
- **变异**：退回"非 audio/* 一律拒"、去掉魔数校验、破坏 MP3 魔数、删错误页早退 → 全部失败 ✓

### 联调结果

真实链路（真搜索→真下载→真编码，仅群发送用桩）：
```
搜索 稻香 → 取址 → 下载(魔数校验通过) → silk 87019 字节 → 回复「♪ 稻香 - Lucky小爱」
发送调用：group=1051425116（用户实际群）  label=稻香 - Lucky小爱（231s）
```
全量：**1359 passed / 44 skipped**（有 ffmpeg）、**1353 / 50**（无 ffmpeg）；ruff 全绿。

### 教训（已可固化为纪律）

**测试替身不得凭空发明 API 表面。** 两处事故都是"替身比真实对象更宽松"造成的：
假事件多了个 `reply` 方法；假响应少了 content-type/魔数约束。凡涉及外部对象
（NoneBot 事件、httpx 响应），**优先构造真实对象**，无法构造时至少钉住真实形状
（如 `assert ev.reply is None`）。
