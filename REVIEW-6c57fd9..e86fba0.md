# agent-demo 近期 Commit 评审报告

**评审范围**：`6c57fd9..e86fba0`（HEAD 最近 3 个 commit，此前 7 个已在库内两份 REVIEW 中覆盖，本次做抽样复核）
**评审日期**：2026-09-10
**评审方式**：4 个并行子代理分线审查（沙箱安全 / 消息流水线 / 媒体视觉 / 测试文档）+ 主代理对关键结论**逐条实证**（运行全量测试、复现逃逸链、证伪可疑结论）
**工作区状态**：`git status` 干净，评审未修改任何受版本控制的文件

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 最高风险 | **工作区沙箱命令白名单可被绕过 → 任意命令执行**（已实证复现，见 H1） |
| 沙箱宣称 vs 实现 | 文档声称「unzip 后清除符号链接」，但 `strip_symlinks` **从未被调用**（M1） |
| 测试 | **279 收集 / 277 通过 / 2 失败**（2 个失败均为 Windows 符号链接用例，平台相关） |
| 文档一致性 | `README.md` / `.env.example` 与代码默认值**逐项一致**，无发现不一致 |
| 在库两份 REVIEW | 其 High 结论在当前 HEAD **均已修复**；但报告未声明覆盖范围上限，易误导（见 §5） |
| 跨用户隐私 | 最近图片缓冲作用域为 `(group, user)`，**确认不存在跨用户泄漏** |

**总体判断**：这 3 个 commit（沙箱加固、流水线重写、测试矩阵扩充）方向正确、工程密度高，测试与文档同步到位。但 `2f338a7` 声称「kills 6 escape vectors」是**不完整的安全声明**——逐参数校验堵住了 shell 元字符/路径穿越类向量，却漏掉了「配置驱动」这一类，导致白名单形同虚设。这是本次评审唯一的高危项，建议优先修复。

---

## 1. 已实证的问题

### H1 — 沙箱白名单可被绕过：工作区内 git 配置驱动任意命令执行【已复现】

- **位置**：`agentcore/workspace/runner.py:191-195`（`_minimal_env`）、`:36-64`（git flag 白名单）、`:104`（`_check_path_args`）；配合 `agentcore/workspace/fs.py:31-36`（`resolve` 不拦 `.git/config`）
- **严重度**：**High**
- **证据（实跑复现）**：在工作区内先写 `.git/config`，再调用白名单内的 `git diff`：

```
fs.write('.git/config') -> '已写入 .git/config（105 字节）'
被注入的 diff.external = sh -c "echo PWNED > C:/…/Temp/agentdemo_pwned_marker.txt"
permitted(git diff) = True  reason=''   ← 白名单判定为安全
runner 输出: "warning: unable to unlink '/tmp/git-blob-…'"
================ 结论 ================
!! 逃逸成功：工作区外出现标记文件 …/agentdemo_pwned_marker.txt
```

  根因链路：
  1. `runner.py:195` 把子进程 `HOME` 指向**工作区**（`"HOME": str(root)`），而工作区是可写的 → git/curl 会读取 `$HOME/.gitconfig`、`$HOME/.curlrc`（Linux 部署目标上成立）；
  2. `fs.py` 的 `resolve` 只拦「越界路径」，**不拦 `.git/config`**，管理员/被注入的 LLM 可写入仓库本地配置；
  3. 白名单只拦 `-c` / `--ext-diff` **参数**，但 `diff.external`、`core.fsmonitor` 是**配置项**，不经过参数校验即被 git 自动执行；
  4. 结果：`--no-shell` 执行、逐参数校验、路径遏制**全部形同虚设**，攻击者获得工作区外任意命令执行。

  独立验证：`diff.external`（`git diff` 触发）与 `core.fsmonitor`（`git status` 触发）在本机均确认执行了指定命令。
- **影响**：沙箱存在的意义正是「约束被提示注入/被劫持的 LLM」，而该缺陷使约束完全失效——可执行任意命令、越权读写工作区外文件。
- **建议**（按可靠性排序）：
  1. 用 `GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM` 指向不存在的文件，并在 `_minimal_env` 里补 `GIT_CONFIG_NOSYSTEM=1`；
  2. 对危险配置键做**显式覆盖**：`git -c diff.external= -c core.fsmonitor=false -c core.pager=cat -c diff.*.command= …`；
  3. `git diff` 追加 `--no-ext-diff`；`git status` 追加 `--no-ahead-behind` 等不触发 hook 的开关；
  4. `HOME` 不指向可写目录（或改为只读临时目录）；curl 同理补 `--config /dev/null`（`-q`）；
  5. 根治方案仍是在容器/低权独立用户中运行（`runner.py` 文档已列为「待运维落地」）。

### M1 — 文档声称 unzip 后清除符号链接，实际从未调用

- **位置**：`agentcore/workspace/runner.py:10`（docstring 承诺）vs `:205`（`strip_symlinks` 定义）
- **严重度**：**Medium**
- **证据**：全仓 grep `strip_symlinks` 仅 3 处命中——定义 `runner.py:205`、测试导入 `tests/test_runner_security.py:9`、测试用例 `:169/:184`。**生产代码零调用点**。
- **影响**：`find -L` 等仍可沿解压残留的符号链接读到工作区外文件；安全承诺与实现不符，且该属性目前只在测试里「验证」，集成路径无覆盖。
- **建议**：在 `unzip` 成功分支真正调用 `strip_symlinks(out_dir)`，并补一条「unzip → 断言链接被清除」的集成测试。

### M2 — engine「权限错误不重试」只改了 prompt，无代码强制

- **位置**：`agentcore/loop/engine.py:74-75`
- **严重度**：**Medium**（提交说明夸大）
- **证据**：`git show ab6f171 -- agentcore/loop/engine.py` 中该条实现为 system prompt 文案追加（`"…但「无权限 / permission denied / 仅管理员」类错误…不要重试…"`）。全文 grep `permission|denied|retry` 仅命中该 prompt 文本与空输出重试（`:266-270`），**没有任何按错误类型短路重试的代码**。
- **影响**：LLM 仍可能反复重试无权限工具，浪费往返；提交信息「permission-denied errors not retried (M5)」应表述为「prompt 引导」。
- **建议**：在 tool-loop 重试判定中对 `permission denied / 无权限` 立即短路返回，或将提交说明改为「prompt 层面引导」。

### M3 — SSRF 防护仅做 host 字符串后缀匹配，且空值可整体关闭

- **位置**：`plugins/qq_agent_adapter/media.py:52-56`（白名单解析）、`:59-72`（`is_allowed_image_url`）
- **严重度**：**Medium**
- **证据**：校验只比较 `httpx.URL(url).host` 的**字符串后缀**，连接时 DNS 解析与之解耦 → 子域接管 / DNS rebinding 可指向 `127.0.0.1`、`169.254.169.254`、内网段；且 `AGENT_IMAGE_HOSTS=""` 时白名单为空列表 → 直接返回 `True`，等同于**对所有用户开放任意 https 抓取**。
- **影响**：攻击者可让以 `qpic.cn` 等结尾的域名解析到内网/云元数据地址。
- **建议**：连接前解析并拒绝 loopback / link-local / 私网段（RFC1918 + `169.254.169.254`）；`AGENT_IMAGE_HOSTS` 显式置空应视为「仅允许已解析公网 IP」或直接拒绝，而非放开。

### M4 — `get_msg` 容错解析把原始结构写入 DEBUG 日志（URL/内容泄漏）

- **位置**：`plugins/qq_agent_adapter/media.py:369-378`
- **严重度**：**Medium**
- **证据**：DEBUG 日志含 `str(data)[:160]`，即被引用消息的**原始协议结构**（含图片 URL、文本片段）。对比转发分支已收敛为只记 `type(data).__name__`。
- **影响**：DEBUG 级别把用户内容/图片 URL 落到日志。
- **建议**：改为只记类型与段数，剥离 URL 与文本。

### M5 — debounce `flush_all` 对 `CancelledError` 不设防

- **位置**：`plugins/qq_agent_adapter/debounce.py:65`（`except Exception`）、`:81-87`（`flush_all`）
- **严重度**：**Medium**
- **证据**：`_run_parts` 只捕 `Exception`；停机时若 runner 被 `cancel_all` 取消，`CancelledError`（`BaseException`）会穿透并中断 `flush_all` 循环，导致同批剩余窗口消息仍未 flush。
- **影响**：极端关机竞态下，「flush 防丢失」承诺打折——恰好是 `ab6f171` 声称修好的 L1。
- **建议**：`flush_all` 内屏蔽取消，或对 `CancelledError` 单独处理为「继续 flush 剩余键」。

### M6 — 引用图片优先级由「引用优先」变为「直发优先」

- **位置**：`plugins/qq_agent_adapter/pipeline.py:301-317`
- **严重度**：**Medium**（行为变更，非缺陷）
- **证据**：旧 `matcher.py` 将引用图片**前置**（`[m for m in quoted_imgs …] + media`）；新代码按 `media + quoted_imgs + fwd_imgs` 排序，识图预算优先消耗直发图。
- **影响**：同窗口既有直发图又有引用图时，引用图可能因预算耗尽被丢。看起来是有意设计（M7/M8），但属**用户可见行为变化**。
- **建议**：保留现状即可，但应在 CHANGELOG / 提交说明显式标注。

### M7 — 文本解析异常时回退为空串（轻微回归）

- **位置**：`plugins/qq_agent_adapter/pipeline.py:158-160`
- **严重度**：**Low–Medium**
- **证据**：异常分支 `except Exception: … return ""`；旧 `matcher._plain_text` 为 `return str(event.get_message())`（回退含 CQ 码的原文）。现依赖 `_build` 的兜底占位才不静默丢消息。
- **建议**：异常分支回退为 `str(event.get_message())` 更稳健。

### L1 — 反斜杠路径穿越：检查被绕过，可利用性未证实

- **位置**：`agentcore/workspace/runner.py:85-104`（`_path_candidate` 仅按 `/` 分词；`..` 判定用 `a.split("/")`）
- **严重度**：**Low**（Windows 专属；Linux 非分隔符，无影响）
- **证据（实跑）**：

```
check(cat, ['..\\..\\Windows\\win.ini']) -> True  reason=''      ← 检查被绕过
check(cat, ['../../Windows/win.ini'])    -> False reason="参数含 .." ← 正常拒绝
实际执行: /usr/bin/cat: '..\..\Windows\win.ini': No such file or directory
```

  即：**校验层确实漏判**，但 MSYS `cat` 不把反斜杠当分隔符，攻击**未落地**。
- **建议**：路径分词同时接受 `\`（用 `os.path.split` / `ntpath`），保持防御一致性。

### L2 — 同步 base64 编码在事件循环内执行

- **位置**：`plugins/qq_agent_adapter/media.py:241-248`
- **严重度**：**Low**
- **证据**：`data_url_from_bytes` 同步 `b64encode` 被直接 await 调用（单图上限 5MB、单消息 ≤3 张）。已有大小预算，阻塞短。
- **建议**：用 `asyncio.to_thread` 包裹。

### L3 — vision 拉取对全体用户开放，与「admin-only 下载」表述不一致

- **位置**：`plugins/qq_agent_adapter/pipeline.py:290-344`
- **严重度**：**Low**
- **证据**：非 vision 模式仅 superuser 落盘；vision 模式对所有用户 fetch + 识图，仅 `is_su` 时才写工作区。
- **影响**：`3b978d2` 的「admin-only 下载」在 vision 路径被放宽为「全员可拉取」；敏感产物（工作区文件）仍仅管理员可得，故风险有限。
- **建议**：若需严格对齐，vision 拉取也加 admin 开关；否则在文档明示差异。

---

## 2. 被证伪的发现（避免误导后续评审）

评审过程中有一个初始怀疑被**实证证伪**，记录在此以免误报固化：

| 初始怀疑 | 实证结论 |
|---|---|
| `.gitconfig` 中 `[alias] log = !id` 可覆盖内置命令 → 任意执行 | **误报**。实测 `git alias` **无法覆盖内置命令**（`git config alias.status '!echo X'` 后 `git status` 仍走内置）。但**同一根因换配置键后成立**——`diff.external` / `core.fsmonitor` 确被自动执行，即 H1。教训：报告攻击面时要给**可复现验证**，不能停在「配置能被读取」的推断。 |
| 最近图片缓冲会把 A 用户的图泄漏给 B 用户 | **不成立**。缓冲键为 `(group, user)`（`pipeline.py:91-92, 464-472`）。群内 A 发图 → B 追问 → B 取不到 A 的图。缓冲有 TTL(180s) + max_entries(32) + 每键上限，**内存有界、无泄漏**。 |
| Windows 反斜杠可越界读文件 | **半成立**：校验层漏判已复现，但**利用未成功**（见 L1）。已降级为 Low，未按 Medium 上报。 |

---

## 3. 测试与文档状况（实跑数据）

**全量运行**：`pytest -q` → **279 collected / 277 passed / 2 failed**，0 skip。声称的「168 → 279」属实，无 `skip` 或 `try/except: pass` 充数。

**2 个失败（均为 `tests/test_runner_security.py`，Windows 平台相关）**：
- `TestH3PathContainment::test_symlink_escape_rejected` — `cat escape/passwd` 应被拒，实测放行（`Path.resolve()` 在 Windows 不跟随指向不存在目标的链接）
- `TestH3UnzipSymlink::test_strip_symlinks` — `assert 0 == 2`（`is_symlink()` 未识别）

→ 部署目标是 Linux，代码语义在 Linux 正确；但**该安全属性在 Windows 上无覆盖**，CI 应加 Linux runner 或将用例标记平台。

**测试质量问题**：
- `tests/test_runner_security.py:257-262` — 声称验证「大输出流式截断」，却在**空目录**跑 `grep -r .`（零输出正常退出），断言仅 `isinstance(out, str)`，**截断逻辑从未被执行**，属假通过。
- `tests/test_admin_import.py:42` — `assert callable(build_payload) or True`，**恒真断言**（同行注释也承认是凑数）。
- `tests/conftest.py:6-9` — autouse 无条件 `delenv("PERSONAS_DIR")`，导致 `PERSONAS_DIR` 自定义路径分支**从未被测**（生产代码 `personas/manager.py:67` 会读它）。
- 多处断言绑定中文文案（`assert "已写入" in out` 等，`test_pipeline.py:205,225,280`、`test_workspace_skills.py:56,63,70,99`），文案改动即红，与行为耦合弱。

**文档一致性（高价值项，全绿）**：逐一核对 `README.md:124-132,205-227`、`.env.example:88-112` 与代码：
`AGENT_DEBOUNCE`=3（`matcher.py:41`）、`AGENT_VISION`=0（`pipeline.py:51`）、`AGENT_VISION_MAX_IMAGE_KB`=5120（`pipeline.py:71`）、`AGENT_VISION_TOTAL_KB`=8192（`:79`）、`AGENT_RECENT_IMAGE_TTL`=180（`:54`）、`AGENT_RECENT_ENTRIES`=32（`:61`）、`WORKSPACE_DIR`、`python3/node/npm` 白名单外——**全部一致**，`e86fba0` 的「docs aligned with behavior」成立。

---

## 4. 已验证为「无问题」的关键项

- **去抖并发正确性**：`_pending` 在 `self._lock` 内 pop（`debounce.py:53-54`），pop 后新消息开新窗口入队，不会拆成两次并发；`_run_parts` 用 per-key `asyncio.Lock` 串行化（`:61-62`），LLM 长延迟下同 key 不并发、不拆句。**串行化是完整的**，非「partial」。
- **message pipeline 重写无行为丢失**：权限 `is_allowed`、前缀/`@` 触发、私聊免前缀、群聊限制**全部保留**；文件触发改为只看 `user_text`（防引用内容注入触发发文件，M5 增强）；旧「payload 为 None 则静默 return」改为降级 payload（更优）。
- **无 `shell=True` / `os.system`**：全仓 grep 0 命中，`asyncio.create_subprocess_exec` 执行。
- **删除闸门 `DeletionGate`**：8 位 hex（`secrets.token_hex(4)`）+ 5 次错误作废，不可爆破；加锁原子读取+弹出，无重放/竞态；按 `user_id` 分桶，跨用户越权被拒；确认码绑定 `path` 且执行前二次遏制。
- **权限不可伪造**：`engine.py:228-235` 显式 `func_args.pop("user_id"/"group_id")`，LLM 无法伪造身份；`is_superuser` 用字符串比较，无 int/str 混淆。
- **媒体下载资源控制**：20MB 流式上限、15s/30s 超时、并发 ≤3、Content-Type + 魔数双重校验、重定向 3 跳且**每跳重新过白名单**、写盘名哈希化（杜绝 `../` 与 Windows 保留名）、图片不落盘不解压（无解压炸弹）。
- **不可信数据围栏**：引用/转发内容统一 `_fence_untrusted` 包裹，防间接提示注入。
- **`conftest` 的 `SUPERUSERS` 归一化与 `bot.py:17-20` 逻辑一致**，测试与生产行为对齐，未掩盖 bug。

---

## 5. 在库两份 REVIEW 报告的独立复核

两份报告覆盖 `be1fb07..6c57fd9` 与 `f6f749b..6c57fd9`，**均不含 HEAD 最后 3 个 commit**（`2f338a7`、`ab6f171`、`e86fba0`）。逐条到 HEAD 核实：

| 报告发现 | HEAD 复核判定 | 证据 |
|---|---|---|
| REVIEW1 H3（runner 6 类逃逸） | **已修复** | `2f338a7` 引入逐参数校验（`runner.py:104-188`），安全测试全绿 |
| REVIEW1 H4（安全测试矩阵缺失） | **已修复** | `e86fba0` 新增 `test_runner_security.py` 等 |
| REVIEW1 H5（裸 lambda 致 admin import 崩溃） | **已修复** | `4283beb`；并已锁回归测试 |
| REVIEW2 H1（引用解析主路径死代码） | **已修复** | `ab6f171` 重写 `build_payload` 并被真实调用 |
| REVIEW2 H2（matcher 交叉逻辑测试缺失） | **已修复** | `e86fba0` `test_pipeline.py` 覆盖 |

**未发现**把 Low 拔高为 High 的夸大。**主要缺陷是呈现层面**：两份报告均**未声明「覆盖范围上限 commit」**，读者容易误以为当前 HEAD 仍有这些 High 缺陷（实际已修复），反之也掩盖了 HEAD 新增的问题（H1/M1 等）。建议在两份报告头部加显眼的范围声明。

**两份报告的共同遗漏**：均未预见最后 3 个 commit 引入的问题——沙箱配置注入（H1）、`strip_symlinks` 未接线（M1）、测试矩阵新增的假通过用例（§3）。提示：**高密度新增测试与安全加固必须配套同等密度的评审**。

---

## 6. 修复优先级建议

1. **P0 — H1**：切断 git/curl 的配置注入面（`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_NOSYSTEM` + 危险配置键显式覆盖 + `--no-ext-diff`；`HOME` 不指向可写目录）。**这是沙箱唯一的实际突破口**。
2. **P1 — M1**：在 `unzip` 后真正调用 `strip_symlinks`，并补集成测试；否则删除文档中的该项承诺。
3. **P1 — M2**：为「权限错误不重试」补代码强制，或修正提交说明。
4. **P2 — M3/M4**：图片 URL 加 IP 层校验（拒私网/元数据地址）；`AGENT_IMAGE_HOSTS` 空值语义改为安全默认；剥离 DEBUG 日志中的原始结构。
5. **P2 — M5**：`flush_all` 屏蔽 `CancelledError`。
6. **P3 — 测试质量**：修掉假通过（截断用例、`or True`）、补 `PERSONAS_DIR` 正向用例、断言去中文文案耦合、CI 加 Linux runner。
7. **P3 — L1/L2/L3**：路径分词兼容 `\`；base64 移入线程池；vision 权限边界在文档明示。

---

*本报告的所有 High/Medium 结论均由主代理实跑验证（全量 pytest、逃逸链端到端复现、误报证伪），行号对应当前 HEAD（`e86fba0`）。*
