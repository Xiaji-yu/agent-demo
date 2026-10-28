# 人格、语音与总览面

> 本文自 `README.md` 拆分而来（2026-10-16）；返回 [README](../README.md)。

## 配置一览

> **必配** = 不设置则该功能不可用（示例值即占位符）；**选配** = 有可用默认值或仅用于调优。全局必配见 [configuration.md](configuration.md)。

| 配置项 | 标注 | 默认 | 说明 |
|---|---|---|---|
| `AGENT_CONFIG` | 选配 | `config.yaml` | 行为配置（人格文件、阈值）路径 |
| `AGENT_PERSONA_GROWTH_INTERVAL` / `_MAX_CHARS` | 选配 | `30` / `500` | 成长层轮数阈值（`0` = 关闭）/ 字符上限 |
| `LLM_MODELS` / `LLM_MODEL` / `AGENT_MODEL_PROBE_TIMEOUT` | 选配 | 空 / `.env` 值 / `20` | 候选池（`模型[@base_url[|api_key]]`，逗号分隔）/ 活跃位 / 切模型前的探测超时（秒） |
| `AGENT_MUSIC_API_URL` + `NAPCAT_HTTP_URL` | **必配**（启用点歌） | 空 = 不注册该 skill | 缺一不可 |
| `NAPCAT_HTTP_TOKEN` | **必配**（协议端开启 HTTP 鉴权时） | 空 | 协议端 HTTP 令牌 |
| `AGENT_MUSIC_ALLOWED_GROUPS` | **必配**（启用点歌） | 空 = 全部拒绝 | 点歌群白名单（fail-closed） |
| `AGENT_MUSIC_AUDIO_HOSTS` / `_MAX_DOWNLOAD_MB` / `_MAX_SECONDS` / `_COOLDOWN` / `_COMMANDS` / `_CACHE_DIR` / `_CACHE_MB` | 选配 | `music.126.net` / `20` / `300` / `15` / `点歌,放歌` / `data/cache/music` / `200` | 域名白名单 / 下载与时长上限 / 冷却 / 意图提示 / 缓存 |
| `AGENT_WEB_TOKEN` | **必配**（挂载 Web 总览） | 空 = 整个面不挂载 | 只读总览令牌（仅认 `Authorization: Bearer`） |
| `AGENT_WEB_WRITE` / `AGENT_WEB_ALLOW_CIDRS` | 选配（受控写入面） | 空 | 缺一不可才挂载写面 |
| `AGENT_DASHBOARD_URL` / `AGENT_DASHBOARD_TOKEN` / `AGENT_DASHBOARD_TIMEOUT` | 选配（看板部署在别处时必改 `_URL`；`_TOKEN` **必配**（本机概览出图）） | `http://127.0.0.1:8282` / 空 / `8` | 看板地址与只读令牌（`_URL` 不设置回落同机默认地址） |
| `AGENT_POKE_ENABLED` / `AGENT_POKE_COOLDOWN` | 选配 | `1` / `30` | 戳一戳开关与冷却 |

## 人格系统（Persona）

**2026-10-28 起改为 OpenClaw 兼容的 SOUL.md 布局**：`data/workspace/` 下靠**文件位置**标识人格，
纯 markdown、**无 frontmatter**（OpenClaw 的 SOUL.md 就是纯 md，加 frontmatter 会破坏两边互拷）：

```
data/workspace/                  ← PERSONAS_DIR 可覆盖；容器内 /app/data/workspace（compose 已挂载）
├── SOUL.md                      ← 默认人格（根级，人格名固定 "default"）
└── personas/
    ├── fortune_teller/SOUL.md   ← 可切换人格，目录名 = 人格名（中文目录名也可用）
    └── <name>/ABOUT.txt         ← 可选，web 的描述来源；缺省取 SOUL.md 首个 # 标题
```

- 用户级切换，选择结果持久化到 PG（`user_state` 表；内存模式进程内保留）
- 命令：`/persona`（查看）、`/persona use <名字>`、`/persona reset`
- **群人格（2026-10-20）**：每个群可配置独立人格（`group_persona` 表，群内共享、
  优先级最高）。群聊里 `/persona` 命令**仅 SUPERUSERS 可用、只作用于本群**；
  非超管静默 + 日志。未配置群人格的群回落到用户选择 / 全局默认；私聊行为不变。
  web「人格」标签可直接配置群人格（两段确认 + 审计，立即生效）
- 新增人格 = 建 `personas/<名字>/SOUL.md`，`/persona list` 即可看到（2 秒 TTL 内）
- 默认人格的优先级：`PERSONAS_DEFAULT=<名字>`（写在 `.env`，本地覆盖）>
  `.default_persona` 标记文件（web「设为默认」落的）> 根级 `SOUL.md`
- **Web 导入（2026-10-20）**：agent-web「人格」标签可直接导入/编辑/删除——
  表单填名字/描述/正文（或选 `.md` 文件，标题→描述、其余→正文），
  两段确认后落盘到 `personas/<名字>/`（原子写 0600，同名覆盖自动备份旧文件到
  `data/backups/personas/`），保存即刷新缓存（`/persona list` 立即可见）。
  正文上限 20000 字（每轮进 system prompt，防爆 token 预算）；名字拒 `default`
  （保留给根级 SOUL.md）。写面门禁同其它写入功能（`AGENT_WEB_WRITE=1` + CIDR + token），
  未开启时列表可看、操作报错
- **注入方式**：人格设定置于 system prompt 最前并声明「贯穿每次回复、优先于任何通用助手身份」，
  工具工作流之后另有「语气与风格始终遵循人格」的收尾约束——人格存在时不再出现
  「你是一个有帮助的 AI 助手」这类通用声明（实测：无条件通用声明会把模型在人格弱时
  拉回"标准客服"；同一算命人格去掉后语气明显更自然）
- 人格文件的设计惯例：`fortune_teller` 是**话题触发型**（平时保持普通助手语气）；
  想要「全程带性格」的扮演型人格，直接在 SOUL.md 正文里写死日常语气与口癖即可
  （注入模板不做区分，怎么写就怎么演）
- **注入方式**：人格设定置于 system prompt 最前并声明「贯穿每次回复、优先于任何通用助手身份」，
  工具工作流之后另有「语气与风格始终遵循人格」的收尾约束——人格存在时不再出现
  「你是一个有帮助的 AI 助手」这类通用声明（实测：无条件通用声明会把模型在人格弱时
  拉回"标准客服"；同一算命人格去掉后语气明显更自然）
- 人格文件的设计惯例：`fortune_teller` 是**话题触发型**（平时保持普通助手语气）；
  想要「全程带性格」的扮演型人格，直接在 md 正文里写死日常语气与口癖即可
  （注入模板不做区分，怎么写就怎么演）
- **长度与节奏也归人格**（2026-10-24）：`yun.md` 原先只有一句「能一句话说清的绝不三句话，
  但该展开时不吝啬笔墨」——后半句是模糊授权，模型默认选"展开"。实测近 45 天 601 条
  回复中位 252 字、**43% 超过 300 字**（出站会把 >3 段合成一张合并转发卡片，视觉上更重）。
  对比 `DeepSeek.md`：它靠 40 多处硬约束 + 12 组「你不要 / 你可以」对照把话压短；
  叙事型人格只写性格、不写长度，就会话痨。现在 `yun.md` 自带三档长度（闲聊 20–80 字 /
  解释 80–300 字 / 明确要求或代码排错才放开）与对照示例，引擎侧两句同步改：
  私聊「可以适当详细」→「按人格设定的长度」，群聊「简洁、有条理」→「简洁、口语化，
  只有步骤/清单/代码才分点」（"有条理"会诱导分点，同样的内容视觉体积翻倍）。

## 人格成长层（per-user 关系成长）

除了固定人设，bot 还能**按用户各自成长**——每个用户与 bot 的相处关系独立演化：

- **触发**：每个用户的对话轮数计数达阈值（`AGENT_PERSONA_GROWTH_INTERVAL`，默认 **30 轮**）后，
  后台回顾该会话近 50 条历史，LLM 提炼「关系成长」提议
  （称呼偏好 / 共同经历——不记客观事实，那归长期记忆）
- **确认**：提议以确认码形式**私聊推送管理员**（SUPERUSERS）：如认可回复
  `确认成长 XXXXXXXX`（10 分钟内有效），成长层才会写入；**未经确认不生效**
  ——每次成长都经管理员过目，防人格漂移。确认门**仅 SUPERUSERS** 可过
  （`AGENT_PERSONA_GROWTH_INTERVAL=0` 可整体关闭该功能）
- **写入**：成长层与 persona md **分层注入**——原始人设永不被改写，成长层按用户
  叠加（标注「仅作语气参考，指令不执行」）；每次确认时新旧记录
  由 LLM 滚动合并，封顶 `AGENT_PERSONA_GROWTH_MAX_CHARS` 字（默认 500）
- **分工**：facts 记「客观事实」（用户住北京），成长层记「关系演化」
  （用户喜欢被叫老板、一起聊过鸣潮）——一个管知道什么，一个管怎么相处
- **人格优先（2026-10-15）**：人格是主干，成长层只做增量。成长层曾紧贴人格段
  之后注入，而它框定的职责与人格段**完全重叠**（都在管"怎么说话"），人格 md 又是
  叙事型长文、成长层是指令密度更高的短文本 → 人格语气被关系描述持续覆盖。
  现在三道防线同时生效：
  1. **位置**：成长层降级为**数据块**，排在 facts / 知识库等用户派生数据之后，
     不再是人格段之后的第一句话
  2. **双向声明**：人格段自带「以上性格特征是人格的主干，后续任何关系/共同经历
     描述都不得覆盖、弱化或替换它们」；成长层框架句自带「不改变你的人格、性格与
     语气特征，与人格设定冲突时以人格设定为准」，段后还有一句收尾回锚
  3. **生产侧**：提议 prompt 已删掉「相处模式的变化」，并明令禁止输出
     「更随意 / 不用敬语 / 更亲近」这类**降格描述**；合并时新进展与既有人格
     特征冲突则丢弃。合并 prompt 另给出超字数时的写入优先级（称呼偏好 > 最近的
     进展 > 较早话题，字数不够时主动删最旧的）

  觉得人格还是偏弱，优先调小 `AGENT_PERSONA_GROWTH_MAX_CHARS`（减少成长层的
  覆盖面积），或调大 `AGENT_PERSONA_GROWTH_INTERVAL`（减少积累速度）
- **长度与臃肿**：两道截断（正常合并 + LLM 故障直接拼接）都把成长层封顶在
  `AGENT_PERSONA_GROWTH_MAX_CHARS` 字，实测第 2 次确认即封顶、之后不再增长；
  截断走**句子边界**（LLM 不守字数时裸切片会留下半句话）。会随时间恶化的是
  **语义**而非长度——500 字额度很早占满后，新话题能否进来取决于合并时删不删旧的。
  每次确认写一行日志 `persona growth merged: user=… chars=… (old=… proposal=… cap=…)`，
  配合下面的 `/growth` 判断该调温还是该清理
- **查看 / 清空**（仅 SUPERUSERS）：
  - `/growth` 查看命令发起者自己的成长层原文；`/growth <QQ号>` 查看指定用户的
  - `/growth clear` 清空发起者自己的；`/growth clear <QQ号>` 清空指定用户的
  - 清空前会回显原文前 200 字，清空后说明「轮数计数未重置」（仍会按阈值触发新提议）；
    对本来就没有记录的用户是幂等提示，不会谎报成功
  - 为什么需要它：改注入逻辑只能防**新**文本，库里已有的降格描述仍会原样生效，
    而 `/reset` 只清对话历史、不动 `user_state.persona_growth`——此前要清它只能手工连库
- **作用域**：成长层按 **user_id** 存储（不是按会话），因此同一用户在私聊里
  养成的称呼/相处细节，也会在他发言的群里影响语气——内容是**他自己**的，
  不跨用户；需要严格按会话隔离时应改用 facts。注入前与长期事实走同一道
  围栏处理（`agentcore/safety.py` 的 lookalike 打散）

## 运行期切换模型（多 provider）

支持多个 LLM，每个 provider 可选协议（**OpenAI Chat Completions / Anthropic
Messages**），并可在 web「模型」标签或 QQ 命令里设主、排序、启停、测试连接。

**存储**：`config/agent.json` 的 `LLM_PROVIDERS` 数组（2026-10-28 需求③，取代旧的
独立 `data/llm-providers.json`）。`save` 只补丁 `LLM_PROVIDERS` / `LLM_ACTIVE` 两个键，
其余键与键序原样保留；落 0600、原子写、改前备份、写后重读校验。结构：

```json
{
  "LLM_PROVIDERS": [
    {"id": "stepfun", "name": "StepFun", "protocol": "openai",
     "base_url": "https://api.stepfun.com/v1", "model": "step-3.7-flash",
     "api_key": "sk-...", "enabled": true},
    {"id": "claude", "name": "Claude", "protocol": "anthropic",
     "base_url": "https://api.anthropic.com", "model": "claude-...",
     "api_key": "sk-ant-...", "enabled": true}
  ],
  "LLM_ACTIVE": "stepfun"
}
```

没有 `LLM_PROVIDERS` 数组时回落 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 单
provider（兼容只有旧键的 agent.json）。从旧版升级时，`AGENT_LLM_PROVIDERS_FILE`
指向的旧独立文件会被一次性并入 agent.json（不覆盖已有数组、不删旧文件）。

- **key 只写不回显**：`GET /api/settings` 的 provider 只含 `key_set`；编辑时留空 =
  保持不变，`clear_key` 才清空。这是对"凭据不 web 写"的**显式例外**，仅限
  「模型」标签的 provider 面板（web token + 写门禁），审计只记 `key_set`。
  **完整 endpoint 只在鉴权 web 设置面返回**；`model_status()`/current_model skill
  只含模型名与 endpoint host（AGENTS「响应体不得含 base_url」的例外边界）。
- **自动迁移**：首次启动（插件初始化）若文件缺失，从 `LLM_MODEL` +
  `LLM_MODELS` + `LLM_FALLBACK_*` 生成 providers 并落盘；幂等，已存在不覆盖。
  文件损坏则告警并回落 env 旧路径（不崩启动）。
- **协议适配**：`agentcore/llm/protocols.py`。引擎只认识 OpenAI 形状；Anthropic
  适配器负责 `system` 提升、`tool_calls ↔ tool_use`、连续 `tool_result` 合并、
  `stop_reason/usage` 映射。Embedding 不纳入 provider 池（Anthropic 无 embeddings）。
- **降级链**：active → 刚换下的旧主 → 其余 **enabled** provider（数组序）；
  禁用项跳过；只有 active 保留 2 次瞬时重试。
- **`/model` 仍可用**：`/model <id|model>`、`/model reset`（回第一个 enabled）。
- **测试连接**：「模型」标签「测试」按钮，按该 provider 的协议发一次最小请求；
  失败只回报，不改 active。

| 命令/入口 | 行为 |
|---|---|
| web「模型」标签 | 新增 / 编辑 / 删除 / 设为主 / 上下移 / 测试；两段确认 |
| `/model` | 列出 provider、协议、端点、当前位与降级链 |
| `/model <id|model>` | 探测通过才切换，写入 JSON（重启后仍生效） |
| `/model reset` | 回第一个 enabled provider |

**曾经的 `.env` 单模型/池配置**仍被读取用于一次性迁移；迁移后 `LLM_MODEL` /
`LLM_FALLBACK_MODEL` 在设置页标为 **已迁移**，编辑按钮替换为"已迁移 → 模型标签"，
后端也返回 409 `legacy_model_key`——**热切换只走 Providers 面板或 `/model`**。
（2026-10-19 实测缺陷：旧实现仍允许写 `LLM_MODEL`，但它只改 legacy 活跃位，
provider 模式下的 `chat()` 根本不看那个字段 → 页面显示成功、实际仍在打旧 provider。）
`LLM_MODEL` 的运行期覆盖只在**没有 provider 文件**的兼容路径下生效（测试与旧部署）。

## 点歌 / 放歌（群语音）

在群里唤醒 bot 后说要听什么歌，bot 下载并转成 QQ 语音发出。**是否放歌由 LLM 判断**
（`play_music` skill），不再要求固定的「点歌/放歌」子命令：

```
云崽 点歌 海阔天空          # 唤醒词 + 点名要听
@机器人 放首歌听听          # @机器人 + 模糊意图（模型自行判断）
你> 想听周杰伦的晴天         # 唤醒后直接说歌名/歌手
```

> 触发仍是**两层**：① 群聊命中唤醒词或 @机器人；② 模型判断该调 `play_music`。
> `AGENT_MUSIC_COMMANDS`（默认 `点歌,放歌`）现在是 skill 描述里的**意图提示**
> （告诉模型哪些词代表想听歌），不再是硬触发词。
>
> 安全不靠模型自觉：handler 内有四道硬闸——**群白名单**（未开放群直接拒绝）、
> **私聊仅 superuser**、**歌名校验**（模型把「什么歌好听」当歌名传进来会拒绝，
> 且不烧冷却）、**账号级冷却**。模型只决定"要不要调"，不决定"能不能发"。

**多版本时先给候选，回复序号选择**（搜到 ≥2 个版本时）：

```
你> 云崽 点歌 稻香
bot> 找到 5 个版本，回复序号选择（60 秒内有效）：
     1. 稻香 - Lucky小爱（3:51）｜稻香(深情版)
     2. 稻香 - Lie（3:16）｜稻香
     ...
你> 2                      ← 直接回复序号即可，不需要唤醒词
bot> ♪ 稻香 - Lie + [语音]
```

列表里带**歌手与专辑**，翻唱/变体一眼可辨（专辑名常写「深情版 / Cover / 女声版」）。
**为什么需要这一步**：网易云**没有周杰伦的版权**，搜「稻香」「晴天」「青花瓷」等
只会返回翻唱——不列候选的话用户只能被动接受第一条，且要等语音放完才知道不是原唱。
列候选不消耗冷却，真正播放时才取额度。
序号只认 ASCII 数字，且仅在该用户**确实有待选项**（60 秒内）时生效；
其余情况一个裸「2」照常走普通聊天。

- **触发**：群聊唤醒词/@机器人 + **模型判断**调用 `play_music` skill（见上）；
  序号选歌是确定性兜底，不绕 LLM
- **默认关闭**：`AGENT_MUSIC_API_URL` 未配置就**不注册**该 skill，消息照常走
  普通聊天；启用需要三条同时成立：`AGENT_MUSIC_API_URL` + `NAPCAT_HTTP_URL`
  + （`pysilk` 与 PATH 中的 `ffmpeg`）。缺依赖只打 INFO 日志，绝不影响核心
- **群白名单**：`AGENT_MUSIC_ALLOWED_GROUPS`（逗号分隔）。**留空 = 全部拒绝**，
  且**不继承** `ALLOWED_GROUPS`——往后者加群不会顺带获得点歌能力
- **账号级冷却**：`AGENT_MUSIC_COOLDOWN`（默认 15s）。同一时间只允许一条语音
  在途，避免协议端的语音上传互相排队
- **时长上限**：`AGENT_MUSIC_MAX_SECONDS`（默认 300s = 5 分钟）；超限不发语音，
  只回一条说明。0 = 不限制
- **下载安全**：音频地址来自音乐接口响应（外部可控），故只允许 https + 域名后缀
  白名单（`AGENT_MUSIC_AUDIO_HOSTS`，默认 `music.126.net`）+ IP 禁段校验
  （含 CGNAT）+ 重定向逐跳复检 + content-type 只快速否定错误页（真正判据是魔数嗅探）+ 大小上限
- **缓存**：silk 字节按 song_id 缓存在内存（固定 200MB，当前版本不随 env 调整）；
  落盘 mp3 编码后即清理，磁盘配额由 `AGENT_MUSIC_CACHE_MB` 控制并按 mtime 最旧优先回收
- **技术细节**：ffmpeg 转 PCM → `pysilk` 编 SILK_V3 → OneBot `record` 段以
  `base64://` 上传（`data:` URI 形式会被协议端当本地路径 stat，报 ENAMETOOLONG）

## Web 总览与全部设置

浏览器里看运行状态，并（在显式开启后）改**全部行为设置**。挂载在 NoneBot 已有的
FastAPI 服务上（不新开端口），路径前缀 `/agent-web`，避开 OneBot 反向 WS。
数据面默认只读；写面无 `AGENT_WEB_WRITE=1` + CIDR 时**一个路由都不注册**。

```bash
# .env
AGENT_WEB_TOKEN=换成一串长随机值     # 留空 = 整个 web 面不挂载（fail-closed）
AGENT_WEB_ALLOW_CIDRS=127.0.0.1/32  # 可选：源 IP 白名单；不配则只靠 token
```

访问 `/agent-web/` —— **页面本身是公开的**（它只是登录表单 + JS，零机密；
被锁住的只有 `/api/overview` 数据面，否则未认证用户只会看到 401 JSON、永远摸不到
输入框）。三种带 token 的方式：

1. 直接打开 `/agent-web/`，在输入框里填一次（存当前标签页的 sessionStorage）
2. 书签/分享用 `/agent-web/#token=<值>` —— `#` 后的 fragment **不会发给服务器**，
   不进反代/访问日志；页面读到手就收进 sessionStorage 并立刻把 token 从地址栏抹掉
3. 脚本/curl 用 `-H "Authorization: Bearer <token>"` 打 `/agent-web/api/overview`
   或 `/agent-web/api/settings`（设置视图，见下）

**不接受** `?token=` query 形式（query 会进各类日志）。之后每 5 秒自动刷新：
（数据面同门槛：页面公开、`/api/*` 强制 Bearer 鉴权；页面顶部可在
「总览 / 设置 / 模型 / 人格 / 备份 / 群统计 / 日志」七个 tab 间切换）

- **LLM 线路**：当前生效型号、主/备线路（降级中会标红）、主备型号
- **协议端连接**：reverse-WS 是否连上（`driver.bots` 为空即未连接）
- **今日用量**：对话 token / 请求数 / 估算成本 / 预算占比与闸门档位，**按模型**拆分
- **历史累计**：遍历所有月份账本（60s 缓存，不跟着轮询反复算）
- **组件**：记忆后端、默认人格、已装 skill 数、群上下文/vision 开关
- **知识库**：是否启用、chunks 与来源数
- **最近事件**：embedding 告警、主备切换/恢复等诊断事件（结构化摘要，**不含消息正文**）

**模型 tab（`/api/settings` 同源，写面）**：LLM provider 管理面板（2026-10-20
从设置 llm 簇搬到顶层标签，设置里只留跳转指引，防双入口漂移）——

- 新增 / 编辑 / 删除 / 设为主 / 上下移 / 测试连接，全部走
  `POST /api/settings/llm/providers` 两段确认（HMAC 绑定最终 provider，key 不回显）
- 写面未开启时列表照常展示，操作按钮点击后报错（与设置页口径一致）
- **M3 Embedding / M4 搜索**（2026-10-21 新增）：embedding 端点/key/模型/维度（单源）
  与**多搜索源**（`data/search-providers.json`，0600）的 CRUD 面板。搜索源含
  format（bocha/tavily/stepfun）/端点/key/启停，可增删改、排序、测试连接；**按列表顺序
  fallback**——一个源失败（超时/断连/非 2xx）自动切下一个，全挂才报错。key 密码框
  **只写不回显**，保存走 `POST /api/settings/search/providers` 两段确认。embedding 为
  env 单源、启动期装配、**改后重启生效**；搜索多源每次调用**热读** JSON、**保存后即时生效**
  （2026-10-23 勘误：旧文档把两者一并写成「改后重启生效」，与实现不符）。env 单配置（`SEARCH_PROVIDER`/`SEARCH_API_KEY`/`SEARCH_BASE_URL`）
  为回落兼容（文件缺失时作单源）。数据来自 `build_settings()` 的 `model_config` 块

**人格 tab（`/api/personas`，写面）**：人格导入 / 编辑 / 删除（详见「人格系统」一节的
Web 导入条目）——

- 列表只读（名字/描述/default/文件时间）；编辑时按需拉 `/api/personas/detail/<name>` 取正文
- 保存/删除两段确认 + 审计 + 同名覆盖前备份；default 人格拒删

**备份 tab（`/api/backup`，只读 + 显式动作）**：本地备份列表与状态——

- 状态卡：目录/保留份数、最新备份时间与年龄（>30h 标红）、总份数/占用、磁盘剩余、
  异地镜像配置与本地可读性（不 SSH 到 NAS）
- 列表按 mtime 最新在前，显示类型与 `.sha256` sidecar 是否存在；**列表不做全量校验**
  （单份 183MB），「校验」是显式按钮（`POST /api/backup/verify`，带并发互斥）
- 「立即备份」「删除」在写面（`AGENT_WEB_WRITE=1` + CIDR），都要 2FA + 审计；
  删除**拒绝最新一份**（保护恢复底线）
- **「下载」两段 2FA（`POST /api/backup/download`）：第二段验完 TOTP 只回一次性
  票据（HttpOnly cookie，90s / 单次 / 绑定文件名与来源 IP），浏览器再用原生
  `GET /api/backup/download/file` 流式下载 pg_dump 明文（`Cache-Control: no-store`；
  不经 JS Blob，183MB 在手机/内嵌浏览器上也能落盘）；**不支持断点续传**
  （票据单次，中断需重走下载确认）；页面二次提示
  "含聊天记录/知识库/人格"
- 还原生产库仍只走 CLI（`scripts/backup_db.py restore --yes`），web 不提供一键恢复

**群统计 tab（`/api/group-stats`，只读）**：群发言榜/词云的同一份聚合数据
（`AGENT_GROUP_STATS` 未开启时显示"未开启"）——

- **同时展示上周（完整）与本周（进行中）**，每群两列并排：群友条数/人数、
  Top5 昵称与占比、高频词 Top30（web 固定 30；QQ 链路上限由
  `AGENT_GROUP_STATS_WORDS` 控制）、小时直方图；只在一周出现的群另一侧标"无记录"
- 只含**聚合数据**（条数/名片快照/词频），不含消息正文——符合 web 面不变量

**日志 tab（`/api/logs*`，只读）**：查看 `AGENT_LOG_DIR` 下的 agent.log 与轮转历史——

- 尾部窗口读取（默认 64KB/次，上限 256KB）；**自动刷新（5s，默认关）走增量追加**——新行接在历史后面（DOM 上限 3000 行），手动「刷新」/改过滤条件才重取整个尾部窗口；
  级别 / 模块前缀 / 关键字（字面量子串）三类过滤，历史日期下拉切换。过滤只作用于**新到行**：增量模式下被滤掉的行不追补（已显示的历史行保持原样）；文件轮转/截断时游标失效，自动回到尾部窗口整体重取
- 单文件下载（>200MB 拒绝，下载留 `logs_downloaded` 审计：文件名 + 字节数）
- ⚠️ **内容口径（管理员决策 2026-09-29）**：日志**不脱敏**——`[msg]`/`[reply]` 行含
  消息正文前 200 字。这是"响应体不含消息正文"不变量的**唯一显式例外**，拿到
  web token 即可读到消息内容，**不要分享日志页截图**
- 文件名走白名单（`agent.log` 及其轮转名），路径穿越一律拒绝

**设置 tab（`/api/settings`）**：当前生效配置 + **全部设置项** + **生效时机**标注——

- **即时生效**（调用时读 env / 实时对象）：LLM 主备型号与当前线路（复用
  `model_status()`，天然不含 key/base_url）、vision 与群聊上下文开关、唤醒词、
  预算上限与闸门档位、出站节流、图片限额、点歌、戳一戳、帮助菜单，以及
  「思考步数」（`agent.max_iterations`，写入后直接热更新运行中 engine）等
- **重启生效**（启动期装配）：LLM/Embedding 配置、调度 cron、归档/备份、
  人格成长，以及 `config.yaml` 的**绝大多数**标量项（唯一例外见上条）
- **`agent.max_iterations`（「模型与 LLM」簇里的「思考步数」）是例外**：写入后
  直接热更新运行中 engine 的下一次 tool-loop，不需要重启；只有 engine 尚未
  装配（启动失败/测试环境）时才回落重启生效
- **写入确认框里的「保存后立即生效 / 需重启才生效」取自 schema 的静态 `live` 位**
  （L3，REVIEW-e55cab5..4d5c1b8）：第一段响应据此提示，**不探测运行态**。真实
  生效时机以第二段响应与审计记录为准——例如上面那条 `max_iterations` 在 engine
  未装配时，确认框会说「立即生效」而实际要等重启。属已知取舍（preview 是纯读
  路径，不为此起一次运行态探测），不是文案错误。
- **全部设置的单一事实源**是 `plugins/qq_agent_adapter/settings_spec.py`：
  `.env` 的 176 个键逐个带 `kind/group/label/description/live/writable/secret/cluster`，
  设置页由 API 返回的 schema 动态渲染（不再前后端各手抄一份白名单）
- `config.yaml` 另有 35 个标量项（`agent` / `archive` / `backup` / `rag` / `push`），
  同样由 `config_yaml.py` 的 `YAML_SPECS` 驱动；`push.jobs` 这类列表只展示、不可逐键编辑
- **凭据与端点/URL 类（18 个）连键名都不进设置页响应体**：页面只显示"另有 N 个走 SSH"。
  其中 `SEARCH_API_KEY` / `SEARCH_BASE_URL` / `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY`
  四个改在 **「模型」标签**配置（M3/M4 面板，密码框只写不回显、改后重启生效），
  与 LLM provider 同源；其余只读项（ACL、保留期、结构路径等）可见但不可写。某块取不到置
  `null` 并进 errors，不影响整页
- **按 17 个设置簇**呈现：顶部簇导航 + 一次只显示一个簇的 `panel`（与 S1/S2 的
  「LLM 线路 / 运行开关」同款块），不再是一张长列表；搜索时切换为跨簇结果视图。
  簇：模型与 LLM、Embedding、消息与交互、图片与视觉、长期记忆、历史与摘要、
  知识库、搜索、点歌、群统计、出站与长回复、聊天记录归档、数据库备份、
  群原文归档、定时推送、人格、高级（只读）
- **簇开关**：有总开关的簇把开关排第一；开关关闭时同簇其余项灰置 **且后端拒写**
  （409 `cluster_disabled:<gate>`），直接 POST 绕不过去。gate 值从**文件**读，
  不用进程里的旧 `os.environ`（避免"已存盘待重启的关闭状态"被绕过）
- **编辑表单**：布尔项是「开/关」下拉；有稳定档位的键（温度、防抖、上下文条数、
  预算、RAG top_k 等）给预选项，仍可切「自定义」填任意值。未设置的键会**预选默认值**，
  避免下拉落到第一项把默认值改坏（provider 表单在「模型」标签，见上）

**重启（`/api/reboot`，默认关闭）**：设置 tab「受控写入」面板底部「重启」按钮——
与写面同门禁（`AGENT_WEB_WRITE=1` + CIDR）+ 浏览器内两段确认；服务端先跑
`lifecycle.shutdown_agent`（防抖 flush → 存储 aclose，幂等）再执行重启，期间bot
不应答。策略链（每步失败自动退下一步）：`AGENT_REBOOT_CMD`（白名单 argv：
systemctl/docker/supervisorctl/service）> 进程内自替换 > 退出等 supervisor
拉起。进程内自替换是 `os.execve` + **出生环境快照**（`agentcore/env_snapshot.py`：
bot.py 启动时抓的 environ，「重启 ≡ 全新启动」，`.env` 派生值全部丢弃、由新进程
从文件重读——2026-10-12 事故的修复点）；仅拿不到快照（测试 / 非 bot.py 启动）才
退回 `os.execv` 继承当前 environ。带 systemd `EnvironmentFile=.env` 的部署除外：
`.env` 由 unit 启动时进环境层，改 `.env` 仍需 `systemctl restart`。
QQ 侧同名 `/reboot`（superuser 直接执行，无二次确认；web 重启无会话上下文，
不发「重启完毕」回执）。审计事件 `reboot_requested`：web 侧记 `by=web:<ip>`
+ 策略 + 命令 + 原因；QQ 侧只记 `by=qq:<uid>` + 策略 + 命令（无 IP / 无原因）。

**受控写入（`/api/settings/write`，默认关闭）**：设置 tab 的「全部设置」面板——

- 开启条件**缺一不可**：`AGENT_WEB_ALLOW_CIDRS` 已配置 **且** `AGENT_WEB_WRITE=1`
  （改后重启生效）；任一缺失写路由整体不挂载
- **可写 109 个 `.env` 键 + 29 个 `config.yaml` 标量项**，按组渲染并支持搜索；
  每项标「即时 / 重启生效」，保存 boot 项后页面提示需重启（面板底部有重启按钮）
- **刻意不可写的边界**（仍是 SSH）：
  - 凭据/URL：API key、token、`DATABASE_URL`、`LLM_MODELS`（条目可内联 key）、
    LLM/协议端/看板端点（Embedding 端点与搜索 key 已改在「模型」标签可配）
  - 安全与权限：`SUPERUSERS`、`ALLOWED_GROUPS`、`BLOCKED_USERS`、
    `AGENT_PERMISSION_LEVEL`、`AGENT_WEB_*`
  - 数据保留/删除：归档/备份/日志保留期、备份 cron、群原文归档及其保留
  - 隐私面：原始消息诊断（`AGENT_DEBUG_RAW_DUMP`）、日志正文（`AGENT_LOG_CONTENT`）、日志白名单、
    私聊蒸馏（`AGENT_KB_DISTILL_PRIVATE`）、PII 词表
  - SSRF/白名单：图片 host 全放行、`fetch_url` 放行网段、npm 全局包登记表
  - 结构路径：工作区、归档/备份/日志/音乐缓存目录、`AGENT_CONFIG`
- **`.env` 与 `config.yaml` 共用同一套写入链**：两段确认（确认码 HMAC 绑定
  nonce+键+新值+5 分钟窗口，单次有效；窗口须覆盖私聊 bot 取码的往返，不能短于
  TOTP 的 5 分钟步长）；**两次确认都在页面内完成**（页内模态框，
  不调浏览器原生 `confirm()`/`prompt()`）→ 整文件快照（`data/backups/config/`，
  滚动 10 份）→ 手术式补丁（只改目标行）→ 原子写 → 写后验证 → 失败还原 +
  `config_write_rollback` 审计；成功留 `config_write`（键 + 掩码值 + source_ip）
- `config.yaml` 是**文本级逐键补丁**，注释/空行/顺序原样保留；写后重新
  `yaml.safe_load` 校验目标路径，类型不符即还原。列表类（`push.jobs`）不开放
- `yaml:` 前缀让两类文件共用同一套确认/审计（确认码把整个 key 绑进 HMAC，不需要
  再加 scope 维度）；值中含换行/引号/`#` 一律拒绝（行格式注入面）
- 另有内部路由 `POST /api/settings/preview`（只预演校验/返回 old→new 与生效时机，
  不落盘）

安全口径（这块是新增攻击面，改动前先读 `plugins/qq_agent_adapter/web.py` 的
模块 docstring）：

| 项 | 做法 |
|---|---|
| 默认不暴露 | 未配 `AGENT_WEB_TOKEN` 时一个路由都不挂载 |
| token 传递 | 只认 `Authorization: Bearer`，**不接受 URL query**（query 会进日志） |
| 备份下载票据 | POST 验完 2FA 后下发一次性 HttpOnly cookie（90s / 单次 / 绑定文件名与来源 IP）；浏览器原生 GET 用票据取文件，票据不进 URL，且仍复核 CIDR |
| 比较方式 | `secrets.compare_digest` 恒等比较（防时序侧信道） |
| 源 IP | 可选 `AGENT_WEB_ALLOW_CIDRS`；反代部署下应在反代层做限制 |
| 取不到的数据 | 置 `null` 并在页面"取数失败的部分"里说明原因，**不猜不补零** |

后续要加写入类功能（配置编辑、记忆删除等）时，必须同时补：审计
（`agentcore/diagnostics.py`）+ 二次确认 + 改前备份原值。当前**刻意只读**。

## 本机概览（戳一戳出图）

在 QQ 里**戳一戳 bot**，回一张**当前机器的概览图**——深色主题、**竖屏简版**：

```text
xiaji-computer                              ← 主机 + 实时状态
● 实时更新中                      9月22日 周二 09:45:36
┌────────────────────────────────────────┐
│ ▌CPU 占用                          60 %│  ← 5 个指标块，各带一条迷你曲线
│  4 核 · 3100 MHz                       │
│  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~     │
│ ▌内存                      4.4 / 11.6 GB│
│  已用 38% · 交换 0.0 GB                │
│  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~     │
│ ▌功耗 / ▌网速(↓下载 ↑上传) / ▌磁盘剩余  │
├────────────────────────────────────────┤
│ 此刻最忙的程序              共 297 个进程│  ← 前 5 条
│  python3              24.9%     144 MB │
│  …                                     │
└────────────────────────────────────────┘
服务 6/6 运行中 · 温度 76°C · 负载 1.34 / 0.91 / 0.69
已开机 11 小时 21 分钟 · 网卡 enx… · 进程 297
```

**为什么不照搬看板整页**：看板概览页是给 1600x900 桌面视口设计的——侧栏导航、
服务列表、页脚按钮放进一张聊天图片全是噪音，手机上看不清也占满屏幕。这里只留
"一眼要看的东西"。**风格仍与看板一致**：同一套 CSS 变量配色、同一套曲线口径
（`chart.js` 的量程与渐变填充），只是重排成竖屏。

数据来自看板的**只读 API**（`/api/overview` + `/api/series`），图是 **bot 本机用
Pillow 重绘**的：不依赖浏览器/无头 Chromium，也不要求看板和 bot 同机（只要网络通）。
画布宽 900、**高度按内容算**（进程条数会变），保证竖屏且不留大片空白。

```bash
# .env —— 看板页面侧栏「账号 → 新建只读令牌」生成 dshk_… 填进来
AGENT_DASHBOARD_URL=http://127.0.0.1:8282
AGENT_DASHBOARD_TOKEN=dshk_xxxxxxxx
AGENT_POKE_ENABLED=1     # 0 = 关闭
AGENT_POKE_COOLDOWN=30   # 同会话冷却秒数；0 = 不限
```

| 维度 | 行为 |
|---|---|
| 触发 | 只认「戳的对象是 bot」的 poke；bot 自己戳别人回传的 notice 被忽略（防自激） |
| 权限 | **只给 SUPERUSERS**；群聊还必须在 `ALLOWED_GROUPS` 白名单里。未授权**静默**——不回「你没权限」（回话等于把机器信息的存在性也说出去） |
| 冷却 | 同会话默认 30 秒（`AGENT_POKE_COOLDOWN`）；被连点不会刷屏；**发送成功后才记**（取数/发送失败不占冷却） |
| 未配令牌 | 回一句提示，不发图（fail-closed） |
| 看板没就绪 | 采样线程未产出首份快照时回「稍后再戳」 |
| 指标不可用 | 报 `available: false` 的指标写「不可用」**且不画曲线**（历史点还在，照画就自相矛盾） |
| 渲染失败 | 缺 Pillow/中文字体时降级为一行文本摘要，**不发豆腐块图** |
| 取数失败 | 回一句人话原因（超时 / 令牌无效 / 连不上），不带堆栈 |

为什么是"重绘"而不是截图：bot 机器上通常没有浏览器，起无头浏览器只为画一张图
既重又脆。`agentcore/render/overview.py` 按看板那套配色与曲线口径重画；改样式时
两边（`dashboard/static/style.css`、`static/chart.js`）要一起看。

