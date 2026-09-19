# AC — 群内语音放歌（直路由版）

> 状态：**待用户确认**。本文只定义"怎么算改好了"，不写实现。
> **已定方向**：`plugins/` 唤醒词**直路由**，**LLM 完全不参与**是否放歌的判断；
> 触发条件严格且确定性；未配置时对核心零影响。

## 0. 范围与分层

新增文件（拟）：

| 文件 | 平台 | 职责 |
|---|---|---|
| `agentcore/music/__init__.py` | 纯 Python | 导出 |
| `agentcore/music/client.py` | 纯 Python | 搜索 / 取音频地址（HTTP） |
| `agentcore/music/silk.py` | 纯 Python | ffmpeg → pysilk 编码 silk |
| `agentcore/music/download.py` | 纯 Python | 受控下载音频 |
| `agentcore/music/gate.py` | 纯 Python | 群白名单 + 账号级冷却（纯逻辑，可单测） |
| `agentcore/music/sender.py` | 纯 Python | OneBot HTTP 发送 record 段 |
| `plugins/qq_agent_adapter/music_route.py` | NoneBot | 唤醒词 + 子命令匹配 + 编排 |
| `tests/test_music.py` | — | A 组 |
| `tests/test_music_gate.py` | — | B 组 |
| `tests/test_music_route.py` | — | D/G 组 |

改动既有文件：**仅** `plugins/qq_agent_adapter/__init__.py`（`_load_plugin_modules` 加一个条件导入分支，约 3 行）。

**不碰**：`agentcore/skills/*`、`builtin.py`、`matcher.py`、`outbound.py`、`config.yaml`。
→ 核心零改动，音乐是纯增量。

**不做**：LLM skill、`MessageSegment`、asyncio 队列 / Lock、`permission` 闸。

---

## 注册闸门（三者全满足才导入 `music_route`）

| 条件 | 变量 | 缺它时 |
|---|---|---|
| 音乐 API 已配 | `AGENT_MUSIC_API_URL`（默认 `http://127.0.0.1:16300`，空 = 未配） | 不导入 |
| OneBot HTTP 可发送 | `NAPCAT_HTTP_URL`（**已有变量**，当前为空） | 不导入 |
| 依赖可用 | `pysilk` 可导入 + `ffmpeg` 在 PATH | 不导入 |

三者任一不满足 → `music_route` 模块**根本不导入** → 没有 matcher → 该消息落给 `chat_matcher` 走普通聊天。零影响。

**依赖隔离为何成立（verified）**：`agentcore/__init__.py` 是空的（`__all__ = []`），
实测 `import agentcore` 加载 0 个子模块。因此 `agentcore/music/silk.py` 顶层的
`import pysilk` **不会**被任何导入链牵连——只有 `music_route.py` 会导入它，而
`music_route.py` 只在闸门通过时才导入。（对比 skill 方案：那里 `agentcore/skills/__init__.py:3`
会连带加载 `builtin`，顶层硬依赖会炸掉整个技能包。直路由没有这个问题。）

---

## A. 歌源层（`agentcore/music/`，纯 Python，不 import nonebot）

**A1 搜索解析**
给定 `POST /search` 响应，解析 `Song(id, name, artists, album, duration_ms)`。
- 验收：喂固定 JSON 断言各字段；缺 `result.songs` 或结构异常 → **返回空列表而非抛异常**。

**A2 时长过滤（下载之前）**
`duration_ms > AGENT_MUSIC_MAX_SECONDS * 1000`（默认 300）的歌剔除。
- 验收：240s / 300s / 301s 三首 → 只留前两首。**边界 300s 必须保留**（`>` 不是 `>=`）。
- 变异复核：改 `>=` → 用例必须失败。

**A3 取音频地址**
`song_url(id)` → `(url, size_bytes)`；`level` 固定 `standard`（实测 3:59 = 0.46MB，无需 VIP）。
- 验收：mock HTTP 断言解析；`url` 为空 → 明确失败，不静默返回空串。

**A4 受控下载（安全面）**
只允许：`https://` + 主机后缀命中 `AGENT_MUSIC_AUDIO_HOSTS`（默认 `music.126.net`）+ 解析 IP 非私网/回环/链路本地/保留段 + 重定向逐跳复检（≤3 跳）+ `Content-Type: audio/*` + 大小 ≤ `AGENT_MUSIC_MAX_DOWNLOAD_MB`（默认 20）。
- 验收（各自独立用例）：`http://` 拒绝 / 非白名单主机拒绝 / 解析到内网 IP 拒绝 / 超大小拒绝 / content-type 非 audio 拒绝 / 重定向第 2 跳非白名单拒绝。
- **不得复用 `media.py` 的 `is_allowed_image_url`**（只认图片扩展名 + QQ 图床白名单）。音乐用专用校验。
- 变异复核：去掉 https 判断 → 对应用例必须失败。

**A5 silk 编码**
`encode_to_silk(src) -> bytes`，产物以 `\x02#!SILK_V3` 开头（**10 字节**：`\x02` + `#!SILK_V3` 九个字符）。
- 验收：ffmpeg 生成 3s wav → 编码 → 断言 `startswith(SILK_HEADER)`、非空、按 2 字节/样本推算时长一致、往返 decode 成功。
- 性能断言：60s 音频编码 < 2s（实测 1.05s）。
- ffmpeg 参数全固定，不接受外部输入进 argv（无 shell，`runner.py` 同款纪律）。

**A6 缓存**
同一 `(song_id, 参数)` 二次请求命中缓存，不再下载/编码；配额超限淘汰最旧。
- 验收：连点两次同一首歌，第二次 HTTP 请求数为 1；配额写满后最旧条目被淘汰。

---

## B. 群白名单与冷却（`agentcore/music/gate.py`，纯逻辑）

**B1 群白名单，默认全关**
`AGENT_MUSIC_ALLOWED_GROUPS` 空 → **所有群一律不允许**，且**不继承 `ALLOWED_GROUPS`**。
- 验收：env 空 → 任意群拒绝；env = `A` → A 放行、B 拒绝。
- 变异复核：改成继承 `ALLOWED_GROUPS` → 用例必须失败。（防「以后往 ALLOWED_GROUPS 加群就意外获得点歌能力」）
- 注：与 skill 版的 `config.yaml` 权限闸相比，这里用 env 是因为已不走 skill/permission 体系；
  且**不需要动 `config.yaml`**，也就没有 `superusers: ["*"]` 让闸失效的问题。

**B2 冷却哨兵用 `None`（§5 的坑）**
`_last_play: float | None = None`，`None` = 未放过 → **首次必放行**。
- 验收：进程刚启动、`time.monotonic()` 很小的第一次请求必须放行。
- 变异复核：改成 `0.0` → 用例必须失败。（`monotonic()` 是开机时长，`now - 0.0 < cooldown` 恒成立）

**B3 冷却拦截与提示**
冷却期内拒绝并告知剩余秒数；`AGENT_MUSIC_COOLDOWN=0`（默认 30）关闭冷却。
- 验收：连发两次，第二次被拒且提示秒数在 `(0, 30]`；冷却设 0 时两次都放行。

**B4 冷却是账号级，不是 per-group**
冷却状态是模块级单例，不按群分桶。
- 验收：A 群放过歌后，B 群**立刻**请求 → 被拒（防两个群各发一条 7.8s 上传互相排队）。
- 变异复核：改成 per-group 字典 → 用例必须失败。

**B5 无队列**
冷却 30s > 最坏任务耗时（编码 ~1s + 上传 ~8s）⇒ 结构上不存在并发，**不引入 Lock / 队列 / 排队提示**。
- 验收：两群并发请求时后者直接被拒而非等待。

---

## C. 发送（`agentcore/music/sender.py`，OneBot HTTP）

**C1 record 段形状**
POST `{NAPCAT_HTTP_URL}/send_group_msg`，body `{"group_id": <int>, "message": [{"type":"record","data":{"file":"base64://…"}}]}`。
- 验收：断言 URL / body 形状；`group_id` 必须是 int（字符串会被协议端忽略）。

**C2 禁用 `data:` URI**
任何路径不得产出 `data:audio/...;base64,`。
- 验收：断言 silk 载荷前缀恒为 `base64://`。（SnowLuma issue #236：`data:` 形态被当本地路径 stat，报 `ENAMETOOLONG`）

**C3 超长不截断**
duration > 上限 → **明确告知「超过 5 分钟」+ 不发语音**，不静默切掉歌尾。
- 注：A2 已在下载前剔除超长，本条是元数据缺失时的兜底。
- 验收：mock 一首 301s 的歌 → 断言无发送、提示含时长信息。

**C4 结果不确定不重发**
发送抛 `httpx.TimeoutException` / `TimeoutError` → 记 ERROR，**不重发、不降级**。
- 复用 `agentcore.skills.file_sender.is_uncertain_send_error`（全仓唯一判据，只复用不修改）。
- 验收：注入超时异常 → 断言只发一次、返回 uncertain 语义、日志 ERROR 级。
- **已知风险，不在本 AC 范围**：SnowLuma issue #422 —— 语音 highway 上传失败时 OneBot 层误报 ok + message_id，
  上层拿不到真失败，需带外确认。本条只保证"我们不重发"。

**C5 多账号串号（已知取舍，明确记录）**
HTTP 发送时账号由 `NAPCAT_HTTP_URL` 指向的协议端决定，**无法指定"触发事件的 bot"**。
当前单账号部署无影响。本条**记为已知限制，不修**。

---

## D. 触发条件（严格 + 确定性，LLM 不参与）

**D1 双层条件，缺一不触发**
matcher rule 必须同时满足：
1. `trigger_rule` 同款门槛 —— 群聊需命中唤醒词（`AGENT_WAKE_WORDS`）或 @机器人；
2. 剥掉唤醒词后，文本以**音乐子命令**开头（`AGENT_MUSIC_COMMANDS`，默认 `点歌,放歌`）。

- 验收（各自独立用例）：
  - `云崽 点歌 海阔天空` → **命中**
  - `@bot 点歌 海阔天空` → **命中**
  - `云崽 你觉得周杰伦哪首歌好听` → 不命中（剥词后不是子命令开头）→ 落普通聊天，**不发语音**
  - `点歌 海阔天空`（无唤醒词，群里）→ 不命中（遵守路由不变量）
  - `云崽 你好呀` → 不命中
- 变异复核：去掉第 1 层唤醒词门槛 → 「群里无唤醒词也命中」的用例必须失败。

**D2 为什么不违反路由不变量**
AGENTS.md:103 禁的是「恢复旧 `ai/!ai//ai` **触发前缀**」（那会取代唤醒词）。
本方案**仍然要求先命中唤醒词或 @bot**，`点歌` 只是唤醒词之后的**子命令**，不改变群聊的入口门槛。

**D3 优先级与 block**
`priority` 小于 `chat_matcher` 的 10（更先执行）且 `block=True`，命中即阻止普通聊天。
- 参考 `group_recorder` 的 `priority=5, block=False`——音乐要的是 `block=True`。
- 验收：mock 事件命中时 `chat_matcher` 不再处理（即只有一个 handler 生效）。

**D4 歌名为空**
`云崽 点歌`（无歌名）→ 回一句「想听哪首？」，**不发语音、不消耗冷却**。
- 验收：断言无发送、无冷却记录、回复含提示。

**D5 私聊**
私聊本就直接响应，同样支持子命令；白名单只作用于群聊。

> **修订（REVIEW-6ec3f7c..a36ea1d M9）**：上面这条与本仓 ACL 语义冲突且实现无法兑现——
> ① 主聊天路径对**私聊只放行 superuser**（`acl.py:36-37`），音乐私聊若不过 ACL 就是越权；
> ② `sender.py` 只有 `send_group_msg`，私聊根本发不出语音。
> 现行为：私聊**同样过 `is_allowed`**（与主路径一致），并在**冷却/搜索/下载/编码之前**
> 就回复「私聊暂时只支持文字，语音放歌仅在群里可用」——不白下载、不烧账号级冷却。
> 若要真正支持私聊语音，需另加 `send_private_msg` 与对应的 ACL 设计。

---

## E. 依赖与交付

**E1 依赖声明** `silk-python>=0.2.6` 加入 `pyproject.toml` 的 `dependencies`。
- 验收：干净 venv `pip install -e ".[dev]"` 成功，`import pysilk` 可用。（已实测有 `cp312-manylinux2014_x86_64` wheel）

**E2 ffmpeg 是外部二进制依赖** 缺失时明确报错，不静默失败。
- 验收：PATH 无 ffmpeg → 导入 `music_route` 时不注册 matcher 并打 INFO 日志说明原因。

**E3 全量验收命令**
```bash
.venv/bin/python -m pytest -q                                  # 1084 passed 基线不得退化
.venv/bin/python -m ruff check agentcore plugins tests bot.py scripts
.venv/bin/python -m ruff format --check agentcore plugins tests bot.py scripts
```

**E4 变异复核** 每条新用例都要「改坏 → 必须失败 → 还原」。单文件变异可能假阴性，以全量套件为准。

---

## F. 依赖隔离（直路由下已大幅简化）

**F1 `music_route.py` 必须在闸门通过后才导入**
`__init__.py::_load_plugin_modules()` 里加条件分支，照抄它已有的延迟导入模式（`:19-27`）。
- 验收：env 未配 → `agentcore.music` 与 `.music_route` **都不在 `sys.modules`**；
  已配但 pysilk 缺失 → 模块导入不抛异常、matcher 未注册、有 INFO 日志。
- 变异复核：把条件分支改成无条件导入 → 「未配置时 sys.modules 不含 music」的用例必须失败。

**F2 `music_route.py` 内部自行探测依赖并降级**
模块顶层 try/except 探测 pysilk 与 ffmpeg，任一不可用则只打日志、不注册 matcher——
**绝不向上抛异常**（否则 `_load_plugin_modules` 崩，影响整个插件加载）。
- 验收：monkeypatch 让 pysilk 导入失败 → 导入 `music_route` 成功、无 matcher、有日志。

---

## G. 防误触

直路由把最大的一类风险**结构性消除**了：是否放歌由**确定性规则**决定，
LLM 完全不参与判断，不存在"模型误判该不该调工具"的问题。

剩余风险与对策：

| 风险 | 对策 | 可测性 |
|---|---|---|
| 用户误敲 `云崽 点歌 什么歌好听` | **G2** 歌名校验在发送前拦下 | ✅ |
| 未授权群触发 | **B1** 群白名单 | ✅ |
| 连点刷屏 | **B3/B4** 30s 账号级冷却 | ✅ |
| 句中提及唤醒词 | 唤醒词是**前缀匹配**（`wakewords.py:19` `startswith`），句中提及不触发 | ✅ |

**G2 歌名校验（handler 级硬闸）**
歌名先过校验，任一条不过即拒绝，且**不发语音、不消耗冷却**：
- 长度 1–50 字符
- 不含问号 `？?` 与疑问词（什么 / 怎么 / 为什么 / 吗 / 呢 / 哪些 / 哪首 / 多少 / 推荐）
- 不是纯泛称（"歌" / "音乐" / "来一首" / "随便" / "好听"）
- 不含控制字符与换行
- 验收（负面，全部拒绝且零发送）：
  `"你喜欢听什么歌"`、`"这首歌叫什么"`、`推荐点歌`、`来一首`、`？？？`、`"周杰伦哪首歌最好听"`、`""`
- 验收（正面，全部通过）：`"海阔天空"`、`"周杰伦 晴天"`、`"光年之外"`
- 变异复核：去掉疑问词校验 → 至少一条负面用例必须失败。

**G3 不写恒真断言**
不写 `assert "必须显式请求" in <某字符串>` 这类换个常量也照样通过的测试（§5 的坑）。

---

## 需要你确认的三件事

1. **子命令词**：默认 `点歌,放歌` 两个别名（`AGENT_MUSIC_COMMANDS`）够不够？要不要加 `来首歌` / `听`？
2. **`NAPCAT_HTTP_URL` 现在为空**，是音乐发送的注册条件之一。填上 SnowLuma 地址（`http://<host>:3000` + token）音乐才可用；
   不填则音乐不注册、走普通聊天。顺带这也能让文件发送从 `base64://` 降级路径升级到 HTTP。
3. **文件布局**：`agentcore/music/` 拆成 6 个小模块（纯逻辑 + 可单测）是我推荐的。
   也可以合并成 `client.py` / `silk.py` / `gate.py` 三个，看你偏好。
