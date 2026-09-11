# agent-demo 修复记录（对应 REVIEW-6c57fd9..e86fba0.md）

**修复范围**：`REVIEW-6c57fd9..e86fba0.md` 中全部 High / Medium / Low 结论
**验证方式**：全量 pytest + 每条安全结论的端到端攻击复现（修复前可复现、修复后被阻断）
**测试基线变化**：`279 收集 / 277 通过 / 2 失败` → **`314 收集 / 312 通过 / 2 跳过`**（跳过项为平台能力探测）

各条修复均有回归用例锁定，防止无声回归。

---

## 一、安全（High）

### H1 — 沙箱命令白名单可被绕过 → 任意命令执行【已封堵 + 已复现验证】

**根因**：白名单只校验「命令 + 参数」，但命令的执行行为受**配置文件**影响。攻击者（或被提示注入的 LLM）只需在工作区落一个配置文件，即可绕过全部逐参数校验。

**三层封堵**（`agentcore/workspace/runner.py`）：

| 层 | 措施 | 覆盖的向量 |
|---|---|---|
| 环境层 | `HOME`/`USERPROFILE`/`CURL_HOME` 指向**工作区之外**的专用沙箱目录；`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` → 空设备；`GIT_CONFIG_NOSYSTEM=1`；`GIT_PAGER=cat`；`GIT_EXTERNAL_DIFF=""`；`GIT_TERMINAL_PROMPT=0` | `$HOME/.gitconfig`、`$HOME/.curlrc`、系统级配置 |
| 命令层 | 所有 `git` 调用前缀注入 `_GIT_HARDENING`（`-c core.fsmonitor=false`、`core.hooksPath`、`core.pager`、`diff.external=`、`credential.helper=`、`protocol.*.allow=never` 等）；`git diff` 追加 `--no-ext-diff`；`curl` 首参数加 `-q` | `diff.external`、`core.fsmonitor`、`core.pager` 等单例键 |
| 仓库层 | `_git_exec_guard`：仓库声明了**驱动型**执行配置（`filter.*.clean/smudge/process`、`diff.*.command/textconv`、`include.path`、`core.hooksPath` 等）时 **fail-closed 拒绝**执行 git，并给出命中原因 | 驱动名任意、`-c` 无法穷举的那一类（`.gitattributes` + `filter.evil.clean`） |

**验证（修复前 → 修复后）**：

| 向量 | 修复前 | 修复后 |
|---|---|---|
| repo-local `diff.external` + `git diff` | 逃逸（工作区外生成标记文件） | 已阻断（`-c` 覆盖 + `--no-ext-diff`） |
| repo-local `core.fsmonitor` + `git status` | 逃逸 | 拒绝执行（守卫） |
| `.gitattributes: * filter=evil` + `filter.evil.clean` + `git diff` | 逃逸 | 拒绝执行（守卫） |
| `$HOME/.gitconfig` 注入 | 逃逸 | 已阻断（环境层） |
| **正常仓库**（status/log/diff） | 可用 | **仍然可用**（不误伤） |

**性能**：`.gitattributes` 只有在其引用的驱动**在配置中确有定义**时才可能被执行，因此守卫先查配置、无驱动定义则跳过递归扫描——避免每次 git 调用遍历整个工作区。

**残留风险（已在代码与文档中声明）**：git 的配置驱动执行面较宽，仓库层是「拒绝已知形态」而非完备证明；根治方案仍是容器/独立低权用户运行。

---

## 二、功能与健壮性（Medium）

### M1 — `strip_symlinks` 从未被调用 → 已接线
`run()` 在 `unzip` 成功后按 `-d` 解析输出目录并调用 `strip_symlinks`（放线程池）。
新增**平台无关**的接线用例（spy 断言调用与入参），原依赖真实符号链接的用例改为能力探测跳过（本机文件系统会静默丢弃符号链接，属平台限制而非行为回归）。

### M2 — 「权限错误不重试」只写在 prompt → 改为代码强制
`agentcore/loop/engine.py`：捕获 `permission denied` 后把技能名记入 `denied_skills`；LLM 再次调用时**不再真正执行**（只回拒绝结果并提示勿重试）；累计超过 `_MAX_DENIED_RETRIES` 直接硬停返回。
用例断言：连续 4 次调用无权限技能，**只有 1 次**真正进入权限检查。

### M3 — SSRF 防护过弱 → 域名白名单 + IP 层校验
- `AGENT_IMAGE_HOSTS` **置空不再等于「允许任意域名」**（旧语义下一个空变量即可整体关闭防护），空值回落默认白名单；
- 确需放开须显式 `AGENT_IMAGE_ALLOW_ANY_HOST=1`；
- 新增 IP 层校验：每跳（含重定向）解析域名，**任一**地址落在内网/回环/链路本地/保留段即拒绝（`127.0.0.1`、`169.254.169.254` 等），用于防 DNS rebinding 与云元数据访问。
残留（已注释说明）：校验与实际连接是两次独立解析，仍有理论上的 rebinding 时间窗。

### M4 — DEBUG 日志泄漏引用消息原文与图片 URL → 已剥离
`resolve_quoted_media` 只记录 `shape/段数/段类型统计`，不再落 `str(data)[:160]`。用例断言 URL 与正文均不出现在日志中，且功能不受影响。

### M5 — `flush_all` 被 `CancelledError` 中断 → 逐窗口隔离
`debounce.py`：`_run_parts` 单独处理 `CancelledError` 并重新抛出，`flush_all` 用 `asyncio.shield` + 逐窗口 try/except 隔离。
用例：前一个窗口被取消后，同批后续窗口**仍被 flush**（这正是 L1 想保证的事）。

### M6 — 引用图优先级反转（行为变更）→ 已文档化
`README.md` 显式标注：同消息含直发图与引用图时，预算顺序为 **直发 > 引用 > 转发**。

### M7 — 文本解析异常回退为空串 → 回退原始消息
`pipeline._build_user_text` 异常分支改为回退 `str(event.get_message())`（并保留前缀剥离），不再整条消息失文本。

---

## 三、加固与小项（Low）

| 编号 | 修复 |
|---|---|
| L1 | 路径分词同时识别 `/`、`\`、盘符与 UNC（`_SEP_RE`/`_ABS_WIN_RE`）。此前 Windows 下 `..\..\x`、`\\host\share` 会被**整体跳过检查**；修复后一律拒绝 |
| L2 | 新增 `data_url_from_bytes_async`（`asyncio.to_thread`），pipeline 调用点改为异步，避免大图 base64 阻塞事件循环 |
| L3 | `README.md` 明示权限边界：`AGENT_VISION=1` 时**所有用户**的图都会被拉取识图，「仅管理员」限制的是**落盘**与 `fs_*`/`run_command` |

---

## 四、测试质量问题（全部修复）

| 问题 | 处理 |
|---|---|
| 截断用例假通过：空目录跑 `grep -r .`（零输出），仅断言 `isinstance(out, str)`，截断逻辑从未执行 | 改为真实制造 3× `_MAX_OUTPUT_BYTES` 输出，断言截断提示与字符上限 |
| `assert callable(...) or True` 恒真断言 | 删除 |
| 断言绑定中文文案（20 余处） | 生产侧导出 `MSG_WRITTEN`/`MSG_REFUSED`/`MSG_REJECTED`/`NOTE_*` 等常量，测试改为引用常量：文案调整只需改一处 |
| `conftest` 强删 `PERSONAS_DIR` 掩盖该分支 | 保留隔离（防开发者环境变量污染），并补 3 个正向/边界用例显式覆盖 env 生效、空白回落、目录缺失三种情形 |
| 2 个 Windows 符号链接用例失败 | 新增 `symlinks_supported` 能力探测 fixture：平台不支持时以明确原因**跳过**，不再误报为安全属性回归 |
| 2 个断言旧「空白名单 = 允许任意域名」的用例 | 按新语义重写为 fail-closed + 显式开关 |

---

## 五、新增回归用例清单（35 条）

- **H1**（11）：`diff.external` 不执行、`core.fsmonitor`/自定义 filter/`include.path` 被拒、干净仓库可用、守卫不误伤良性 attributes、无驱动时跳过扫描、`git diff` 带 `--no-ext-diff`、加固参数与环境变量注入、`HOME` 不再是工作区
- **M1**（2）：unzip 后调用 `strip_symlinks` 且入参为解析后的输出目录、非 unzip 命令不调用
- **M2**（2）：无权限技能只执行一次并被硬停、工具回包含「请勿重试」
- **M3**（6）：空白名单 fail-closed、显式开关、禁用 IP 判定、域名解析到内网被拒、公网放行
- **M4**（1）：日志不含 URL 与正文
- **L2**（2）：异步版本与同步结果一致、确实走线程池
- **M5**（2）：取消不中断整批 flush、异常逐窗口隔离
- **M7**（3）：异常回退原文、仍剥离前缀、正常路径不受影响
- **L1**（5）：反斜杠 `..`、UNC、盘符、`--opt=<反斜杠路径>` 均拒绝，正常相对路径放行
- **personas**（3）：env 生效 / 空白回落 / 目录缺失不崩

---

## 六、验证命令

```bash
cd agent-demo
python -m pytest -q          # 312 passed, 2 skipped
python -m ruff check agentcore plugins tests   # 未引入新的可修复告警
```

**未提交**：以上改动均在本地工作区，尚未 commit。`REVIEW-6c57fd9..e86fba0.md` 与本文件为未跟踪文件。
