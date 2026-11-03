# NAS 部署指南（Docker Compose 实操版）

> 场景：把 agent-demo 以 Docker Compose 部署到 NAS，经协议端（NapCat / SnowLuma 等）
> 反向 WS 当 QQ 机器人用。
>
> 本文是 [operations.md](operations.md)「容器化部署」的 **NAS 实操补充**：通用规则
> （挂载边界、属权修复、镜像里刻意做的四件事、测试隔离）那边已经写对，这里不重复，
> 只写 NAS 场景的差异与踩过的坑。
>
> 路径按绿联 DX4600 实机写法（`/volume2/docker/agent-demo`）；群晖/飞牛改根路径即可。
> 端口 55432 是因为该机 `127.0.0.1:5432` 已被 appstore 的 PG 实例占用。

## 0. 部署前检查（NAS 上逐条确认）

```sh
uname -m                          # x86_64 或 aarch64：docker-publish 两个架构都发
docker info | grep RootDir        # 数据根确认在存储池（如 /volume1/@docker），别落 eMMC
ss -tlnp | grep -E ':(5432|8080)\b'   # 两个端口都必须空闲；5432 被占就改 AGENT_DB_PORT
docker network ls                 # 记录现有 bridge，别撞名
```

端口规划（与既有服务共存的前提）：**8080** 给 bot（web 总览 + 反向 WS 共用），
**55432** 给 PG 宿主查询口（只绑 `127.0.0.1`）。协议端已在跑的话，它的
3001/6099 之类不动。

## 1. 一条命令自举

```sh
cd /volume2/docker/agent-demo
python3 scripts/deploy.py docker          # 或 --up 直接起服务
```

它做四件事，全部幂等（`.env` 已存在**绝不覆盖**，已存在的目录不动内容）：

| 产物 | 说明 |
|---|---|
| `.env`（0600） | 从 `.env.example` 复制；你的密钥在里面，别提交进 git |
| `data/` 下 14 个运行期子目录 | 清单与 compose 挂载点由 `tests/test_deploy.py` 锁死，漏一个就是容器里某功能静默降级 |
| `config.yaml` | **必须预先存在**：compose 是单文件挂载，源路径不存在时 Docker 会补一个目录绑上去，bot 直接起不来 |
| `docker compose config -q` | 解析期预检；缺 `.env` 连 `db` 都起不来，这步把错误提前到人话 |

- 尊重 `.env` 的 `AGENT_DATA_DIR`（相对 compose 文件目录解析），NAS 上可指到存储池；
- LLM 三件套仍是占位时只 warn 不阻断：可以先起库再填 key；
- 目录属主由镜像里的 `scripts/docker-entrypoint.sh` 启动时修成 `1000:1000`，**不需要手工 mkdir/chown**。

> **dotfile 坑**：部分 NAS 文件管理器会把 `.env` 建成目录。用上面的脚本或 SSH 建，
> 别在 Docker UI 里手建这个文件。只有图形界面、连不上 SSH 时的等价操作见
> operations.md「只有图形界面、连不上 SSH 的 NAS」那一段。

## 2. `.env` 要改的 5 个键

```bash
SUPERUSERS=你的QQ号
ALLOWED_GROUPS=群号,另一个群号        # 空集 = 所有群都不响应（fail-closed）
LLM_BASE_URL / LLM_API_KEY / LLM_MODEL # 三件套，按实际供应商
ONEBOT_ONEV11_ACCESS_TOKEN=长随机串    # 协议端反向 WS 握手用
ONEBOT_ONEV11_SECRET=长随机串
NAPCAT_HTTP_URL=http://192.168.1.200:3000   # 协议端 HTTP 地址（用 NapCat 才需要）
```

`DATABASE_URL` **不用填**——compose 的 `environment:` 已用服务名 `db` 拼好注入容器
（`environment` 优先于 `env_file`，这一行足以改掉 `.env` 里指向 127.0.0.1 的旧值）。

改完 `sudo docker compose restart bot`：`.env` 只在启动时读一次，`/reboot` 不重读文件。

> 从 picoclaw/openclaw 迁移时，把旧的 `QQ_ACCESS_TOKEN` / `*_GATEWAY_TOKEN` 一起换成随机值
> ——它们门控的是局域网内可触发的 bot 控制面，旧值已明文留在旧 compose 里，视为已泄露。

## 3. NAS 特有的四个旋钮

都不需要改 compose，只在 `.env` 里设：

| 键 | 默认 | NAS 上为什么改 |
|---|---|---|
| `AGENT_DB_PORT` | `5432` | 被 appstore 的 PG 占了就改（如 `55432`）；仍只绑回环 |
| `AGENT_BOT_IMAGE` | `agent-demo:local` | 拉 GHCR 不本机构建：`ghcr.io/<owner>/agent-demo:latest` |
| `AGENT_BOT_PORT` / `AGENT_BOT_BIND` | `8080` / `0.0.0.0` | 宿主端口冲突时改前者；协议端同机可把后者收成 `127.0.0.1` |
| `AGENT_DATA_DIR` | `./data` | 指到存储池（大目录是 archive/backups） |

**存储规划**：`pgdata` 是 docker named volume，落在 Docker RootDir（你的存储池），
**不占项目目录**；项目目录里只有 `.env`、`config.yaml` 和 `data/` 的 14 个子目录。
备份落 `data/backups`（每日 03:30、保留 7 份），异地镜像落 `data/mirror`。

**镜像**：合并进 `main` 即由 CI 构建推送双架构（`linux/amd64` + `linux/arm64`）到
`ghcr.io/<owner>/agent-demo:latest`（另有 `:sha-<短哈希>` 可锁版本）。ARM NAS 直接拉，
不要本机构建（qemu 慢、且国内 DNS 污染会让 apt/pip 超时）。包可见性：仓库 public 则包
默认 public；private 需 `docker login ghcr.io`（镜像零密钥）。

拉不通时的本机构建兜底（大陆网络必须加镜像源参数）：

```sh
sudo docker build --network host \
  --build-arg APT_MIRROR=mirrors.aliyun.com \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  -t agent-demo:local .
```

## 4. 只有 Docker Web UI（粘贴 compose）时的最小可用配置

绿联的「创建项目」解析器不吃完整版 compose，只会弹「无效的配置文件」不给行号。
它卡的是这些构造：

| 不接受的构造 | 备注 |
|---|---|
| `${VAR:-default}` 带默认值的插值 | 尤其是卷路径里的 `${AGENT_DATA_DIR:-./data}/logs:/app/data/logs` |
| `depends_on` + `condition: service_healthy` 长语法 | |
| `env_file:` | 可安全删除：`.env` 已由单文件挂载进容器 |
| `init: true` / `stop_grace_period` / `shm_size` / `healthcheck` / `logging` | 2.2+ 特性 |

对应的最小可用配置（**刻意降级**：端口与密码写死在文件里，改这两处要编辑文件；
权威版本永远是仓库根目录的 `docker-compose.yml`）：

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
    stop_grace_period: 60s        # ← 最值得加回的一行，见下
    depends_on:
      - db
    environment:
      DATABASE_URL: postgresql://qqagent:在这里填同一个密码@db:5432/qqagent
      TZ: Asia/Shanghai
      AGENT_ENV_FILE: /app/.env
      PORT: "8080"                # 必须与下面映射的容器侧同值
    ports:
      - "8080:8080"
    volumes:
      - ./.env:/app/.env
      - ./config.yaml:/app/config.yaml
      - ./data/logs:/app/data/logs
      - ./data/archive:/app/data/archive
      - ./data/backups:/app/data/backups
      - ./data/budget:/app/data/budget
      - ./data/workspace:/app/data/workspace
      - ./data/cron:/app/data/cron
      - ./data/personas:/app/data/personas
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

**`stop_grace_period: 60s` 是最值得加回的一行**：停机 flush 最长要 30s（要过全局 LLM
闸门），默认宽限期只有 10s，`docker stop` / NAS 重启时会**被 SIGKILL 掐断，丢掉缓冲里
还没落库的消息**。其余降级的代价：`shm_size` 退回 64MB（PG 复杂并行查询会报
"No space left on device"）；无 healthcheck + `depends_on: [db]` 只等容器启动，bot 可能
先崩几轮再自愈（`restart: always` 会拉起来）；无 tini 信号转发。

**更推荐走 SSH**：完整版文件原样吃得下，还给你真正的错误文本：

```sh
cd /volume2/docker/agent-demo
sudo docker compose config -q && echo OK     # 真错误在这里现形
```

## 5. 切换反向 WS（替换旧 bot 的关键一步）

反向 WS 是**协议端连 bot**。适配器实际挂载三个路径（`/onebot/v11/`、`/onebot/v11/ws`、
`/onebot/v11/ws/`），填哪个都行：

```
ws://192.168.1.200:8080/onebot/v11/ws
```

1. 协议端 WebUI（NapCat 默认 `http://<NAS IP>:6099`）→ 反向 WebSocket 客户端 →
   **新增**：URL 填上面的地址，token 填 `.env` 里 `ONEBOT_ONEV11_ACCESS_TOKEN` 的值。
2. **先停掉旧 bot**（`sudo docker stop <旧容器>`）：两个 bot 同时连同一协议端会都收到
   全部消息（双回复）。
3. 验证：在白名单群发唤醒词或 @bot；私聊直接发消息。有回复即切换成功。
4. 稳定运行几天后再删协议端里旧 bot 的 WS 条目、`docker rm` 旧容器。
   **旧 bot 的 compose 与数据目录先留着**（回滚用）。

握手排障：403 空响应体 = 先查 `X-Self-ID` 再查 token 的顺序问题，operations.md 有细节。

### SnowLuma 协议端的已知行为（实测，与 NapCat 的差异）

| 行为 | 影响 |
|---|---|
| `get_msg` 返回**首次入库的存储副本**，读取时重签图片 rkey | 跨重启的旧图可能取不到真实数据 |
| 群聊有 `SsoGetGroupMsg` 服务端历史回捞，**私聊没有** | 私聊旧消息的图片若入库时未进 MediaStore 即不可恢复；代码会显式引导「请重发图片」 |
| 负数 `message_id` = 本地合成、无服务器权威序列 | 别拿它当跨重启的稳定标识 |
| 语音 highway 上传失败时 OneBot 层**误报 ok + message_id** | 上层拿不到真失败；只记 message_id 与载荷大小，**绝不重发** |
| silk 载荷必须 `base64://` 前缀 | `data:audio/...;base64,` 形态会被当本地路径 stat，报 `ENAMETOOLONG` |

## 6. 备份与异地镜像

- 每日 03:30 自动 `pg_dump` 到 `data/backups/`，保留 7 份；`data/mirror/` 是同盘镜像
  （`.env` 里 `AGENT_BACKUP_MIRROR_DIR=data/mirror`）。
- **跨盘真异地**（DX4600 双池）用 override 文件换宿主源、target 不变：

  ```yaml
  # docker-compose.override.yml（与 compose 同目录，别提交进 git）
  services:
    bot:
      volumes:
        - /volume1/agent-backups:/app/data/mirror
  ```

- 恢复（在仓库里跑）：`python scripts/backup_db.py restore <file> --yes`；
  U 盘冷备走 `scripts/backup_usb.sh`（配置源按 `.env` 或 JSON 配置二选一探测——
  宿主机与容器部署都只有 `.env`，两条路径都覆盖）。完整备份/恢复/加密说明见 operations.md。

## 7. 回滚

| 场景 | 操作 |
|---|---|
| agent-demo 有问题，要回旧 bot | `docker stop agent-demo-bot-1 agent-demo-db-1` → `docker start <旧容器>` → 协议端 WS 条目切回 |
| 配置改坏了 | `data/backups/config/` 有 web 写前的快照；`.env` 每键改动也有 `.bak` |
| 数据库要退回去 | `data/backups/` 最新 dump → `python scripts/backup_db.py restore <file> --yes` |

## 8. 验收清单（逐项打勾再下线旧 bot）

- [ ] `docker compose ps`：db healthy、bot running，`logs bot --tail 30` 无 CRITICAL
- [ ] `docker run --rm --entrypoint sh <镜像> -c 'ldd /usr/local/pg16/psql' \| grep -c 'not found'` 输出 0（恢复路径可用）
- [ ] 白名单群：唤醒词 / @bot 有回复；非白名单群无响应（fail-closed 是预期）
- [ ] 私聊直接对话有回复
- [ ] web 总览 `http://192.168.1.200:8080/agent-web` 返回 401（未带 token）= 正常；带 token 能开
- [ ] `docker compose exec bot python -c "import socket;s=socket.create_connection(('db',5432),3)"` 通
- [ ] 手动触发一次备份：web「备份」页或等 03:30，`data/backups` 有新文件
- [ ] `ss -tlnp` 确认 8080/55432 由 agent-demo 容器持有

## 9. 容器内的能力边界（迁移前知道）

- 旧方案（picoclaw 等）的 agent 沙箱要跑容器，所以它 unhealthy 时先查 `docker.sock`；
  agent-demo 的沙箱是**进程内白名单执行，不需要也不挂 docker.sock**——别为"对齐能力"
  把 socket 挂进去，那等于交出宿主机 docker 控制权。
- 容器内 `systemctl` / `journalctl` / `dmesg` 一律拒并返回自解释文案（沙箱收敛，设计如此）；
  看日志用 `data/logs` + web 日志查看面。
- `/reboot` 在容器内走 `os.execve` 自替换（跳过外部命令），由 compose `restart` 兜底。
