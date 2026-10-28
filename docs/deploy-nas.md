# NAS 部署指南（绿联 DX4600 / x86_64 Docker）

> 场景：把 agent-demo 以 Docker Compose 部署到 NAS，经 NapCat 反向 WS 当 QQ 机器人用，
> 替换此前的 picoclaw（或与 openclaw 同构的其它部署）。返回 [operations.md](operations.md)
> 的容器化部署（本文是它的 NAS 实操版，通用规则不重复）。
>
> 本文所有路径按 DX4600 实机写法（`/volume2/docker/agent-demo`）；换成群晖/飞牛改根路径即可。
> 端口 55432 是因为该机 `127.0.0.1:5432` 已被 appstore 的 PG 实例占用。

## 0. 部署前检查（NAS 上逐条确认）

```sh
uname -m                  # 必须 x86_64（ARM 机型当前构建必失败，libpq 拷贝写死架构）
docker info | grep RootDir  # 数据根确认在存储池（如 /volume1/@docker），别落在 eMMC
ss -tlnp | grep -E ':(5432|8080)\b'   # 两个端口都必须空闲；5432 被占就把 AGENT_DB_PORT 改掉
docker network ls         # 记录现有 bridge，别撞名
```

端口规划（与既有服务共存的前提）：**8080** 给 bot（web 总览 + 反向 WS 共用），
**55432** 给 PG 宿主查询口（只绑 127.0.0.1）。NapCat 已在跑的话，它的 3000/3001/6099 不动。

## 1. 目录与文件

项目目录只需要**一个文件**：[docker-compose.yml](deploy-nas/docker-compose.yml)。
其余全部由镜像里的 `init` 子命令生成，不用自己找模板、改名、建目录。

```sh
sudo mkdir -p /volume2/docker/agent-demo
cd /volume2/docker/agent-demo
curl -O https://raw.githubusercontent.com/<owner>/agent-demo/main/docs/deploy-nas/docker-compose.yml

# 生成 config/agent.json、config.yaml、data/（幂等；已存在的文件一律不覆盖）
# **现在可以跳过这步**：容器首次启动会自己补建（entrypoint 自举，缺什么建什么）。
# 仍想提前备好配置再启动，或有老部署要补目录时再跑：
sudo docker run --rm -v "$PWD:/out" ghcr.io/<owner>/agent-demo:latest init /out
```

`init` 做的事（与首次启动自举同一套逻辑，只是触发方式不同）：

| 目标 | 行为 |
|---|---|
| `config/agent.json` | **主配置**，只含必填键 + 自解释备注（`_` 开头的键被忽略） |
| `config/agent.env` | web 设置页 / QQ `/model` 的写面落点，建出来但是空的（只有注释） |
| `config/config.yaml` | 从镜像内复制（镜像已烤了一份默认值） |
| `data/` | 建 13 个运行期子目录 |

跑完它会打印「还要填这几项」清单。**已存在的 `agent.json` / `agent.env` / `config.yaml`
绝不覆盖**——重复跑 `init` 是安全的，用于补建漏掉的目录。直接 `docker compose up -d`
时不跑它也行：自举会用占位符生成同样的文件，启动日志里列出待填键，填完
`docker compose restart bot` 生效。

> `agent.env` 别手删：`config_write` 对不存在的文件是 fail-closed 的，删了设置页会
> 报 `env_file_missing`（表现为设置页整体 500）。

### 为什么是 JSON 而不是 `.env`

`config/` 是**整个目录挂载**进容器的，里面放 `agent.json`。这不是偏好问题，是踩过坑之后的结论：

* **dotfile 在部分 NAS 上建不住**——文件管理器反复把 `.env` 建成目录，而 Docker 的
  **单文件** bind mount 在源路径不存在时会自动补一个目录，再把目录绑到镜像里的文件上，
  报一句指不到病因的 `not a directory`（2026-10-28 在绿联 DX4600 上连踩两次）。
* **目录挂载没有这个坑**——源不存在时 Docker 自动建目录，那正是它想要的行为。
* `agent.json` 是普通文件名，文件管理器建不错、编辑不错。

> `.env` 那条路仍然可用：compose 里把 `env_file` 与 `./.env:/app/.env` 两行注释取消即可。
> 两者同时存在时优先级是 **真实环境变量 > `config/agent.env`（web 写面）>
> `agent.json` > `.env`**。

> 为什么不能全自动：`agent.json` 里必须装**你自己的** QQ 号、群号、LLM key、协议端
> 地址——换机器就不一样，烤进公开镜像等于公开你的 key。init 只能做到「文件就位 +
> 告诉你还差什么」。

## 2. `config/agent.json` 要改什么

**完整参考（含每个键的含义、写法注意、排障速查）见 [config-quickstart.md](config-quickstart.md)**，
那份是给第一次部署的人看的。这里只说要点。

`init` 生成的文件已经是容器部署形态（`HOST=0.0.0.0`、7 个状态文件重定向进
`data/state/` 都已就位）。**你只剩 5 项要填**：

```json
{
  "SUPERUSERS": "换成你的QQ号",
  "ALLOWED_GROUPS": "换成群号,另一个群号",
  "LLM_API_KEY": "换成你的key",
  "ONEBOT_ONEV11_ACCESS_TOKEN": "换成一个长随机串",
  "NAPCAT_HTTP_URL": "换成协议端的HTTP地址，如 http://192.168.110.185:3000"
}
```

`LLM_BASE_URL` / `LLM_MODEL` 模板默认 stepfun，按实际供应商改。
`DATABASE_URL` **不用填**——compose 已用服务名 `db` 拼好注入容器。

**必须换掉的弱令牌**：如果此前 picoclaw/openclaw 的 `QQ_ACCESS_TOKEN` /
`PICOCLAW_LAUNCHER_TOKEN` / `OPENCLAW_GATEWAY_TOKEN` 用的是同一个简单值，一起换随机值
（它们门控的是局域网内可触发的 bot 控制面）。

改完 **`sudo docker compose restart bot`**——编辑文件不热加载，bot 只在启动时读一次。

## 3. 获取镜像与启动

镜像由 **GitHub Actions 自动构建**（`.github/workflows/docker-publish.yml`）推送到
GHCR，NAS 侧只拉不建；`docs/deploy-nas/docker-compose.yml` 已硬编码镜像地址。

1. **触发构建**：合并进 `main` 后自动构建。
   （注意：GitHub 的硬性规则——`workflow_dispatch` 手动触发也要求 workflow 文件
   在**默认分支**上，所以功能分支推上去后要在 Actions 页手动跑，仍需先合并；
   合并即触发，是最省事的路径。）
2. **NAS 拉镜像**（`<owner>` = 你的 GitHub 用户名，全小写）：

   ```sh
   sudo docker pull ghcr.io/<owner>/agent-demo:latest
   ```

   > §1 的 `init` 已经拉过一次，这步是冗余保险（升级镜像时用）。

3. **启动**：

   ```sh
   cd /volume2/docker/agent-demo
   sudo docker compose up -d db bot
   sudo docker compose ps          # db 与 bot 都应为 healthy/running
   ```

   包可见性：仓库是 public 则 GHCR 包默认 public，NAS 可直接拉；若你的 fork 是
   private，包也是 private，要么把包改成 public（GitHub 头像 → Your packages →
   agent-demo → Package settings → Change visibility），要么在 NAS 上
   `docker login ghcr.io`（镜像零密钥，公开无泄漏面）。

镜像说明：构建期跑 `python -c "import bot"` 冒烟，缺件在 build 就炸；workflow
只编 `linux/amd64`（ARM NAS 当前构建必失败——Dockerfile 的 libpq 拷贝写死
`/lib/x86_64-linux-gnu/`，已登记在案，不适用本指南场景）。

**兜底：NAS 本机构建**（GHCR 不可用时；大陆网络必须加镜像源参数，否则 apt/pip
慢到超时）：

```sh
cd /volume2/docker/agent-demo
sudo docker build --network host \
  --build-arg APT_MIRROR=mirrors.aliyun.com \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  -t agent-demo:local .
```

`--network host`：docker 默认 DNS 在大陆被污染，apt 源会解析到假 IP
（`198.18.x.x`）。

启动后自检三连：

```sh
sudo docker compose logs bot --tail 30          # 无 CRITICAL；迁移/修复正常
sudo docker run --rm --entrypoint sh ghcr.io/<owner>/agent-demo:latest -c 'ldd /usr/local/pg16/psql' | grep -c 'not found'
# ↑ 必须输出 0；非 0 就是缺库（恢复路径不可用），回 Dockerfile apt 清单补
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.110.185:8080/agent-web   # 401=正常（未带 token）
```

### 绿联 Docker「创建项目」UI 兼容版

如果你不走 SSH、要用 NAS 的 Docker Web UI 粘贴 compose，注意：**那个解析器不吃本目录的
完整版 compose**，只会弹一句「无效的配置文件」不给具体行号。它**接受没有 `version:` 键的
Compose Spec 文件**（这点已实测——同机原有的 openclaw compose 就没有 version 且跑得通），
卡住的是下面这些构造：

| 不接受的构造 | 备注 |
|---|---|
| `${VAR:-default}` 带默认值的插值 | 尤其是嵌在卷路径里的 `${AGENT_DATA_DIR:-./data}/logs:/app/data/logs` |
| `depends_on` + `condition: service_healthy` 长语法 | 3.x schema 里被移除过 |
| `env_file:` | **可安全删除**——bot 自己读 `config/agent.json`（`bot.py` 的 `config_json.load()`），`env_file` 对它本是重复注入 |
| `init: true` / `stop_grace_period` / `shm_size` / `healthcheck` / `logging.driver+options` | 2.2+ 特性 |

对应的最小可用配置（**代价见下**，端口与密码写死在文件里，改这两处要编辑文件）：

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    container_name: agent-demo-db
    restart: always
    environment:
      POSTGRES_DB: qqagent
      POSTGRES_USER: qqagent
      POSTGRES_PASSWORD: 在这里填你的密码
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:55432:5432"

  bot:
    image: ghcr.io/<owner>/agent-demo:latest
    container_name: agent-demo-bot
    restart: always
    stop_grace_period: 60s        # ← 见下方「最值得加回的一行」
    depends_on:
      - db
    environment:
      DATABASE_URL: postgresql://qqagent:在这里填同一个密码@db:5432/qqagent
      TZ: Asia/Shanghai
      AGENT_ENV_FILE: /app/config/agent.env
      # 容器内监听口，与下面映射的容器侧同值——两边必须一致，否则映射打不通
      PORT: "8080"
    ports:
      - "8080:8080"
    volumes:
      # - ./.env:/app/.env   （老部署才需要）
      - ./config.yaml:/app/config.yaml
      - ./data/logs:/app/data/logs
      - ./data/archive:/app/data/archive
      - ./data/backups:/app/data/backups
      - ./data/budget:/app/data/budget
      - ./data/workspace:/app/data/workspace
      - ./data/cache:/app/data/cache
      - ./data/sessions:/app/data/sessions
      - ./data/privacy:/app/data/privacy
      - ./data/skills:/app/data/skills
      - ./data/kb_samples:/app/data/kb_samples
      - ./data/state:/app/data/state
      - ./data/mirror:/app/data/mirror
      - ./data/ssh:/app/data/ssh

volumes:
  pgdata:
```

**最值得加回的一行：`stop_grace_period: 60s`。** 停机 flush 最长要 30s（要过全局 LLM 闸门），
默认宽限期只有 10s，`docker stop` / `restart` / NAS 重启时会**被 SIGKILL 掐断，丢掉缓冲里
还没落库的消息**。这一行是 2.2+ 特性，UI 可能认也可能不认——能保存就赚到，保存不了就删掉，
不影响别的。其余三项的代价：`shm_size` 退回 64MB（PG 复杂并行查询会报 "No space left on
device"）；无 healthcheck + `depends_on: [db]` 只等容器启动，bot 可能先崩几轮再自愈
（`restart: always` 会拉起来）；无 tini 信号转发。

**更推荐走 SSH**：完整版文件原样吃得下，还给你真正的错误文本，不用跟那个输入框较劲
（粘贴 130 行带中文注释的 YAML 本身就会坏缩进/编码）：

```sh
cd /volume1/docker/agent-demo
sudo curl -O https://raw.githubusercontent.com/Daiyimo/agent-demo/main/docs/deploy-nas/docker-compose.yml
sudo docker compose config -q && echo OK      # 真错误在这里现形
```

> 若已在 UI 里建过项目，两边必须指向**同一个目录**，否则 UI 的项目和你命令行操作的文件不是一套。

## 4. 切换反向 WS（替换旧 bot 的关键一步）

反向 WS 是**协议端连 bot**：协议端主动连 `ws://192.168.110.185:8080/onebot/v11/ws`。

1. 协议端 WebUI（NapCat 是 `http://192.168.110.185:6099`；SnowLuma 见其自己的配置面）→
   反向 WebSocket 客户端 → **新增**：URL 填上面的地址，token 填 `config/agent.json` 里
   `ONEBOT_ONEV11_ACCESS_TOKEN` 的值。
2. **先停掉旧 bot**（`sudo docker stop picoclaw` / `openclaw`）：两个 bot 同时连着同一
   协议端会都收到全部消息（双回复），停掉即断开它的 WS。此时 agent-demo 是唯一在线 bot。
3. 验证：在白名单群发一句唤醒词（默认「小助手/助手」开头）或 @机器人；私聊直接发消息。
   有回复 → 切换成功。
4. 稳定运行几天后：删掉协议端里旧 bot 的 WS 条目，`sudo docker rm <旧容器>`。
   **旧 bot 的 compose 文件与数据目录先留着**（回滚用）。

握手排障：403 空响应体 = 先查 `X-Self-ID` 再查 token 的顺序问题（operations.md 有细节）。

### SnowLuma 协议端的已知行为（实测，与 NapCat 的差异）

本部署实际用 **SnowLuma**（非 NapCat）。下面几条是踩过的，配之前知道能省一轮排查：

| 行为 | 影响 |
|---|---|
| `get_msg` 返回**首次入库的存储副本**并在读取时重签图片 rkey | 跨重启的旧图可能取不到真实数据 |
| 群聊有 `SsoGetGroupMsg` 服务端历史回捞，**私聊没有** | 私聊旧消息的图片若入库时未进 MediaStore 即不可恢复；代码会显式引导「请重发图片」，不让模型照历史空猜 |
| 负数 `message_id` = 本地合成、无服务器权威序列 | 别拿它当跨重启的稳定标识 |
| 图片 URL 两形态 `multimedia.nt.qq.com.cn` / `gchat.qpic.cn` | 均在默认 SSRF 白名单内；私聊 offpic 通道的 `http://gchat.qpic.cn/...` 入口已按白名单主机升级 https |
| 语音 highway 上传失败时 OneBot 层**误报 ok + message_id**（SnowLuma issue #422） | 上层拿不到真失败。MVP 取舍：只记 message_id 与载荷大小，**绝不重发**；用户说没收到时这两项是唯一排查线索 |
| silk 载荷必须 `base64://` 前缀 | `data:audio/...;base64,` 形态会被当本地路径 stat，报 `ENAMETOOLONG`（SnowLuma issue #236） |

## 5. HA 迁移（picoclaw 的 HA_URL/HA_TOKEN 搬过来）

1. **先轮换令牌**：HA → 个人资料 → 长期访问令牌，删除旧的、重新生成
   （旧值已明文存在于 picoclaw 的 compose 文件里，视为已泄露）。把新 token 从
   picoclaw 的 compose `environment:` 里删掉。
2. agent-demo 的 `config/agent.json` 加（三项都配才启用，缺任一整个功能关闭）：

   ```json
   {
     "AGENT_HA_BASE_URL": "http://192.168.110.185:8123",
     "AGENT_HA_TOKEN": "换成你的长期访问令牌",
     "AGENT_HA_ENTITIES": "light.living_room,switch.desk_plug"
   }
   ```

   > `AGENT_HA_ENTITIES` 只认登记表内的 `entity_id`，多个用逗号分隔（值是字符串，
   > 不是数组）。改完必须 `restart`，`/reboot` 不重读文件。

3. `sudo docker compose restart bot`（改 `agent.json` 必须 restart，`/reboot` 不重读文件）。

## 6. 备份与异地镜像

- 每日 03:30 自动 pg_dump 到 `data/backups/`，保留 7 份；`data/mirror/` 是同盘镜像
  （`AGENT_BACKUP_MIRROR_DIR=data/mirror`）。
- **跨盘真异地**（DX4600 双池）：建 override 文件换宿主源、target 不变即可
  （entrypoint 的属权修复覆盖该目录）：

  ```yaml
  # docker-compose.override.yml（与 compose 同目录，别提交进 git）
  services:
    bot:
      volumes:
        - /volume1/agent-backups:/app/data/mirror
  ```

- 恢复（宿主 repo 里跑）：`python scripts/backup_db.py restore <file> --yes`；
  没有 repo 时用 §3 同款 psql 直灌。完整备份/恢复/加密说明见 operations.md。

## 7. 回滚

| 场景 | 操作 |
|---|---|
| agent-demo 有问题，要回 picoclaw | `docker stop agent-demo-bot-1 agent-demo-db-1` → `docker start picoclaw` → NapCat WS 条目切回 picoclaw |
| 配置改坏了 | `data/backups/config/` 有 web 写前的快照；`config/agent.env` 每键改动也有 `.bak` |
| 数据库要退回去 | `data/backups/` 最新 dump → `scripts/backup_db.py restore --yes` |

## 8. 验收清单（逐项打勾再下线 picoclaw）

- [ ] `docker compose ps`：db healthy、bot running，日志无 CRITICAL
- [ ] `ldd psql` 无 `not found`（恢复路径可用）
- [ ] 白名单群：唤醒词 / @bot 有回复；非白名单群无响应（fail-closed 是预期）
- [ ] 私聊 bot 直接对话有回复
- [ ] web 总览 `http://192.168.110.185:8080/agent-web` 带 token 可开（配了才验）
- [ ] `/2fa`（私聊）能拿到滚动码（开了 web 写面才验）
- [ ] `docker compose exec bot python -c "import socket;s=socket.create_connection(('db',5432),3)"` 通
- [ ] 手动触发一次备份：`docker compose exec bot pg_dump --version` + web「备份」页或等 03:30
- [ ] HA 技能（若配了）能读到实体状态
- [ ] NAS 侧：`ss -tlnp` 确认 8080/55432 由 agent-demo 容器持有

## 9. 与 picoclaw 的能力差（迁移前知道）

- picoclaw 的 agent 沙箱要跑容器（所以它 unhealthy 时先查 docker.sock）；agent-demo 的
  沙箱是进程内白名单执行，**不需要也不挂 docker.sock**——别为"对齐能力"把 socket 挂进去，
  那等于交出宿主机 docker 控制权。
- 容器内 `systemctl`/`journalctl` 一律拒并给说明文案（沙箱收敛，设计如此）；要看日志用
  `data/logs` + web 日志查看面。
- `/reboot` 在容器内走 `os.execve` 自替换（跳过外部命令），由 compose `restart` 兜底。
