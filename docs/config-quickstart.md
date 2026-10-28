# 新手配置参考：极简 JSON + NapCat

> 这份是给「第一次部署、只想让它跑起来」的人看的。完整可配项有 176 个键，
> 详见 [`.env.example`](../.env.example)（键名与 JSON 里完全一致）——那份是**字典**，
> 不用全填。本文只讲「不填就不能用」和「填错会静默失效」的。

## 一、`config/agent.json` 完整参考

`init` 会生成好这个文件，你只要把「换成…」的占位换成真实值。

```json
{
  "_说明": "以 _ 开头的键只是备注，程序会忽略。改完执行 docker compose restart bot 生效。",
  "_说明_监听": "容器部署必须 0.0.0.0。填 127.0.0.1 协议端连不上，现象是 ECONNREFUSED。",
  "HOST": "0.0.0.0",
  "PORT": "8080",

  "_说明_权限": "SUPERUSERS=管理员 QQ 号；ALLOWED_GROUPS=允许响应的群，多个用逗号分隔。留空=群里不响应/没有超管。",
  "SUPERUSERS": "换成你的QQ号",
  "ALLOWED_GROUPS": "换成群号,另一个群号",

  "_说明_LLM": "OpenAI 兼容接口，不绑死供应商。key 只进这个文件，不进日志/截图/提交。",
  "LLM_BASE_URL": "https://api.stepfun.com/v1",
  "LLM_API_KEY": "换成你的key",
  "LLM_MODEL": "step-1-flash",

  "_说明_协议端": "ONEBOT_ONEV11_ACCESS_TOKEN 要与协议端「反向 WebSocket 客户端」里填的 Token 一致。NAPCAT_HTTP_URL 是协议端的 HTTP API 地址——NapCat 默认 3000，不是 WebUI 的 6099，且必须带 http:// 前缀。",
  "ONEBOT_ONEV11_ACCESS_TOKEN": "换成一个长随机串",
  "NAPCAT_HTTP_URL": "换成协议端的HTTP地址，如 http://192.168.1.100:3000",

  "_说明_状态文件": "下面 7 个把易失状态重定向进 data/state/（compose 挂了这个目录）。不配的话 recreate 时 web 配过的 provider/key、2FA secret、nudge 记录会全丢。",
  "AGENT_LLM_PROVIDERS_FILE": "data/state/llm-providers.json",
  "AGENT_SEARCH_PROVIDERS_FILE": "data/state/search-providers.json",
  "AGENT_2FA_SECRET_FILE": "data/state/2fa-secret.json",
  "AGENT_NUDGE_CONFIG": "data/state/nudge.json",
  "AGENT_NUDGE_HOTSPOT_FILE": "data/state/nudge-hotspot.json",
  "AGENT_REBOOT_NOTICE_FILE": "data/state/reboot-notice.json",
  "AGENT_PUSH_STATE_FILE": "data/state/push-state.json"
}
```

**你必须填的只有 5 个**：`SUPERUSERS` / `ALLOWED_GROUPS` / `LLM_API_KEY` /
`ONEBOT_ONEV11_ACCESS_TOKEN` / `NAPCAT_HTTP_URL`。其余照默认即可。

`DATABASE_URL` **不用填**——compose 已用服务名 `db` 拼好注入容器。

### 写法注意

| 项 | 说明 |
|---|---|
| 键名 | 与 `.env` 完全一致，大写 + 下划线 |
| 值 | 一律字符串，数字也要加引号：`"PORT": "8080"` |
| 布尔 | 用 `"1"` / `"0"` 或 `"true"` / `"false"`（都认，别写 `1` 不加引号） |
| 列表 | 写字符串用逗号分隔：`"ALLOWED_GROUPS": "111,222"` |
| 备注 | 键名以 `_` 开头即被忽略，可随意加 |
| 优先级 | 真实环境变量 > `config/agent.env`（web 写面）> `agent.json` > `.env` |

改完 **`docker compose restart bot`**——编辑文件不会热加载，bot 只在启动时读一次。

### 两个「改了不生效」的坑

| 键 | 真相 |
|---|---|
| `PORT` | **容器里不生效**。compose 的 `environment` 用 `AGENT_BOT_PORT` 显式注入监听口，而 compose 的变量插值读不到容器里的 `agent.json`。改端口改 compose 层的 `AGENT_BOT_PORT`（端口映射两侧与容器内监听同时变），只有本机 `python bot.py` 才读这里的 `PORT` |
| `DATABASE_URL` | compose 已用服务名 `db` 拼好注入，填了会被覆盖 |

### web 设置页改的键存在哪

`config/agent.env`（与 `agent.json` 同目录，compose init 会建出来）。设置页 / QQ
`/model` 改的键写进那个文件，**重启后覆盖 `agent.json` 里的同名项**，但盖不掉真实
环境变量。别手删它——删了设置页会报 `env_file_missing`。

## 二、NapCat 完整配置

两个必须的，一个要核对的。

### ① 反向 WebSocket 客户端（唯一必须的）

NapCat WebUI（默认 `http://<NapCat所在机器>:6099`）→ 网络配置 →
**反向 WebSocket 客户端** → 新增：

| 项 | 值 | 说明 |
|---|---|---|
| 启用 | **开** | 滑块在左上角，容易漏 |
| 名称 | 随意，如 `agent` | |
| URL | `ws://<NAS的IP>:8080/onebot/v11/ws` | **8080 是 bot 的端口**，路径不能改 |
| Token | 与 `ONEBOT_ONEV11_ACCESS_TOKEN` 一致 | 两边一字不差 |
| 消息格式 | `Array` | |
| 上报自身消息 | **关** | 开着 bot 会收到自己发的消息 |
| 心跳间隔 / 重连间隔 | `30000` / `30000` | 默认即可 |
| SSL 证书验证 | 关 | `ws://` 用不到；只有 `wss://` 才相关 |

### ② HTTP 服务（发图片/语音要用）

NapCat WebUI → 网络配置 → **HTTP Server**（不是 HTTP Client）：

| 项 | 值 | 说明 |
|---|---|---|
| 启用 | 开 | |
| Host | `0.0.0.0` | **关键**。绑 `127.0.0.1` 容器里的 bot 够不着 |
| Port | `3000` | |
| Token | 设了就填进 `NAPCAT_HTTP_TOKEN`，没设就留空 | 与 WS 的 Token 是两套 |

> 找「HTTP **Server**」，别在「HTTP Client」里填 URL——那是 NapCat 当客户端往外连，
> 本项目里用不到。`http://0.0.0.0:3000` 这种写法也没有意义（`0.0.0.0` 是监听地址，
> 不是可连的目标）。

### ③ 四个端口分别是谁的

| 端口 | 归属 | 方向 | 用不用 |
|---|---|---|---|
| **8080** | **bot** | NapCat → bot（反向 WS + web 总览） | ✅ 填这个 |
| 3000 | NapCat HTTP API | bot → NapCat（发图/语音） | ✅ |
| 3001 | NapCat WS 服务端 | 正向 WS | ❌ 本项目方向相反 |
| 6099 | NapCat WebUI | 你操作配置的界面 | ✅ |

### ④ 登录一个 QQ 号

没登录就没有 `self_id`，握手直接失败。

## 三、启动与自检

```sh
# 1. 生成配置文件（幂等；已存在的 agent.json / config.yaml 不覆盖）
docker run --rm -v "$PWD:/out" ghcr.io/daiyimo/agent-demo:latest init /out

# 2. 编辑 config/agent.json，填那 5 项

# 3. 启动
docker compose up -d db bot
docker compose ps                              # db 要 healthy，否则 bot 不会起

# 4. 自检三连
docker compose logs bot --tail 20              # 无 ERROR；Uvicorn 那行必须是 0.0.0.0
docker compose exec bot python -c "import socket;s=socket.create_connection(('db',5432),3)" && echo "DB 通"
curl -s -o /dev/null -w '%{http_code}\n' http://<NAS的IP>:8080/agent-web   # 401=正常
```

启动日志里这几行是**预期输出**，不是错误：

| 日志 | 含义 |
|---|---|
| `Embedding: local fallback dim=2048` | 没配 `EMBEDDING_*`，语义检索降级本地 hash |
| `web 总览未挂载：AGENT_WEB_TOKEN 未配置` | web 功能没开（fail-closed） |
| `主动联系未配置目标` | 默认关闭 |
| `Shell skill skipped (level<high)` | 权限级别 medium，`run_shell` 不注册 |
| `hnsw index creation failed ... fallback to btree` | 向量维度 >2000 时 pgvector 建不了 hnsw，降级 btree，不影响使用 |

## 四、排障速查

| 现象 | 先查什么 |
|---|---|
| NapCat 报 `ECONNREFUSED` | `docker compose logs bot --tail 5 \| grep Uvicorn`——必须是 `http://0.0.0.0:8080`；是 `127.0.0.1` 就是 `HOST` 没生效（改了 JSON 忘了 restart） |
| 403 空响应体 | 握手先校验 `X-Self-ID` 再校验 token，任一缺失都这症状——别只怀疑 token |
| 收得到消息但发不出图 | `NAPCAT_HTTP_URL` 是不是 `127.0.0.1`（容器里那是自己）、端口是不是 6099、有没有漏 `http://` |
| 双回复（同一句回两遍） | 两个 bot 连着同一协议端，先 `docker stop` 掉旧的那个 |
| `not a directory: Are you trying to mount a directory onto a file` | 挂载源被建成了目录。`stat -c '%F %n' config/agent.json config.yaml` 看类型；JSON 方案用目录挂载本不该出现这个 |
| 容器起来了但不能对话 | `docker compose logs bot \| grep -i "缺少必填键"` |

## 五、想加功能时改哪里

| 想干什么 | 加什么键 |
|---|---|
| web 总览（网页看日志/备份/改配置） | `AGENT_WEB_TOKEN=<随机长令牌>` + `AGENT_WEB_WRITE=1` + `AGENT_WEB_ALLOW_CIDRS=192.168.x.0/24` |
| 语义检索升级（不配就本地 hash） | `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` |
| 群发言榜 / 词云 | `AGENT_GROUP_STATS=1` |
| 主动联系（沉默检测器） | `AGENT_NUDGE_ENABLED=1` + 在 web「主动」页配 targets |
| 每日备份异地镜像 | `AGENT_BACKUP_MIRROR_DIR=data/mirror` |
| Home Assistant | `AGENT_HA_BASE_URL` / `AGENT_HA_TOKEN` / `AGENT_HA_ENTITIES`（三项都配才启用） |

每一项的取值与语义见 [`.env.example`](../.env.example) 对应键上的注释。
部署到 NAS 的完整流程（端口规划、NapCat 切换、回滚、验收清单）见 [deploy-nas.md](deploy-nas.md)。
