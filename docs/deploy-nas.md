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

```sh
sudo mkdir -p /volume2/docker/agent-demo/data/{logs,archive,backups,budget,workspace,cache,sessions,privacy,skills,kb_samples,state,mirror,ssh}
```

把仓库放到 `/volume2/docker/agent-demo`（ compose 文件、`Dockerfile`、`config.yaml`、
`.env.example` 要在；`.venv`/`data`/`.git` 不用带）：

```sh
# 有 git 仓库时（任选其一）：
git clone <你的 fork> /volume2/docker/agent-demo
# 或从开发机 rsync（排除本地产物）：
rsync -av --exclude .venv --exclude .git --exclude data --exclude .stepcode \
  <开发机>:/path/to/agent-demo/ /volume2/docker/agent-demo/
```

```sh
cd /volume2/docker/agent-demo
cp .env.example .env
```

> `.env` 已 gitignore，**绝不提交**；它同时是 compose 变量插值源、bot 的 env_file、
> web 受控写面的落点（挂进容器见 operations.md「挂载边界」）。

## 2. `.env` 模板

在 `.env.example` 基础上，按下面清单改（其余保持默认）：

```ini
# ---- 身份与 ACL（取值沿用你 openclaw compose 的 QQ_ADMINS / QQ_ALLOWED_GROUPS）----
SUPERUSERS=<你的 QQ 号>
ALLOWED_GROUPS=<群号,群号>

# ---- LLM（key 只进 .env，不进日志/截图/提交）----
LLM_BASE_URL=https://api.stepfun.com/v1
LLM_API_KEY=<你的 key>
LLM_MODEL=step-3.7-flash

# ---- 协议端对接（token 换成随机值，NapCat 侧同值）----
ONEBOT_ONEV11_ACCESS_TOKEN=<随机长令牌>
NAPCAT_HTTP_URL=http://192.168.110.185:3000
# NAPCAT_HTTP_TOKEN=<NapCat HTTP 服务的 token，没设就留空>

# ---- compose 层变量（不注入容器；5432 被占所以 55432）----
AGENT_DB_PORT=55432
POSTGRES_PASSWORD=<纯字母数字强密码>   # 避开 @ 和 $，见 .env.example 的密码约束
# AGENT_DB_NAME=qqagent               # 默认即可；用 -p 改项目名时才需要一起核对 PG_CONTAINER

# ---- 状态文件重定向（把 .env.example 里「容器部署」注释块的 7 行取消注释）----
# AGENT_LLM_PROVIDERS_FILE=data/state/llm-providers.json
# ……（该注释块整组取消）

# ---- 备份（推荐至少同盘镜像；跨盘镜像见 §6）----
AGENT_BACKUP_MIRROR_DIR=data/mirror

# ---- 可选：web 设置页写面（2FA 走私聊 /2fa）----
# AGENT_WEB_WRITE=1
# AGENT_WEB_TOKEN=<随机长令牌>
# AGENT_WEB_ALLOW_CIDRS=192.168.110.0/24
```

**必须换掉的弱令牌**：如果此前 picoclaw/openclaw 的 `QQ_ACCESS_TOKEN` /
`PICOCLAW_LAUNCHER_TOKEN` / `OPENCLAW_GATEWAY_TOKEN` 用的是同一个简单值，三个一起换随机值
（它们门控的是局域网内可触发的 bot 控制面）。

## 3. 构建与启动

```sh
cd /volume2/docker/agent-demo
sudo docker build --network host \
  --build-arg APT_MIRROR=mirrors.aliyun.com \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  -t agent-demo:local .
sudo docker compose up -d db bot
sudo docker compose ps          # db 与 bot 都应为 healthy/running
```

- `--network host`：docker 默认 DNS 在大陆被污染，apt 源会解析到假 IP
- 构建期会跑 `python -c "import bot"` 冒烟，缺件在 build 就炸
- **x86_64 专属**：ARM NAS 当前构建必失败（Dockerfile 的 libpq 拷贝写死
  `/lib/x86_64-linux-gnu/`），已登记在案，不适用本指南场景

启动后自检三连：

```sh
sudo docker compose logs bot --tail 30          # 无 CRITICAL；迁移/修复正常
sudo docker run --rm --entrypoint sh agent-demo:local -c 'ldd /usr/local/pg16/psql' | grep -c 'not found'
# ↑ 必须输出 0；非 0 就是缺库（恢复路径不可用），回 Dockerfile apt 清单补
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.110.185:8080/agent-web   # 401=正常（未带 token）
```

## 4. 切换 NapCat 反向 WS（替换 picoclaw 的关键一步）

反向 WS 是**协议端连 bot**：NapCat 主动连 `ws://192.168.110.185:8080/onebot/v11/ws`。

1. NapCat WebUI（`http://192.168.110.185:6099`）→ 网络配置 → 反向 WebSocket 客户端 →
   **新增**：URL 填上面的地址，token 填 `.env` 里 `ONEBOT_ONEV11_ACCESS_TOKEN` 的值。
2. **先 `sudo docker stop picoclaw`**：两个 bot 同时连着 NapCat 会都收到全部消息（双回复），
   停掉 picoclaw 即断开它的 WS。此时 agent-demo 是唯一在线 bot。
3. 验证：在白名单群发一句唤醒词（默认「小助手/助手」开头）或 @机器人；私聊直接发消息。
   有回复 → 切换成功。
4. 稳定运行几天后：删掉 NapCat 里 picoclaw 的 WS 条目，`sudo docker rm picoclaw`。
   **picoclaw 的 compose 文件与 `/volume2/docker/picoclaw` 数据目录先留着**（回滚用）。

握手排障：403 空响应体 = 先查 `X-Self-ID` 再查 token 的顺序问题（operations.md 有细节）。

## 5. HA 迁移（picoclaw 的 HA_URL/HA_TOKEN 搬过来）

1. **先轮换令牌**：HA → 个人资料 → 长期访问令牌，删除旧的、重新生成
   （旧值已明文存在于 picoclaw 的 compose 文件里，视为已泄露）。把新 token 从
   picoclaw 的 compose `environment:` 里删掉。
2. agent-demo 的 `.env` 加（三项都配才启用，缺任一整个功能关闭）：

   ```ini
   AGENT_HA_BASE_URL=http://192.168.110.185:8123
   AGENT_HA_TOKEN=<新令牌>
   AGENT_HA_ENTITIES=light.living_room,switch.desk_plug   # 只认登记表内的 entity_id
   ```

3. `sudo docker compose restart bot`（改 `.env` 必须 restart，`/reboot` 不重读文件）。

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
| 配置改坏了 | `data/backups/config/` 有 web 写前的快照；`.env` 每键改动也有 `.bak` |
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
