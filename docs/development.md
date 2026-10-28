# 开发与代码地图

> 本文接 [README](../README.md) 的简版说明：README 只保留「是什么、怎么跑、必配什么」，
> 代码结构、平台边界、开发命令的完整版在这里。返回 [operations.md](operations.md)（运维）
> 或 [README](../README.md)。

## 目录结构

```text
agent-demo/
├─ agentcore/              # 纯 Python 包，不依赖 NoneBot
│  ├─ llm/                 # LLM 客户端（供应商无关）
│  ├─ embedding/           # Embedding 客户端（OpenAI 兼容 / 本地降级）
│  ├─ loop/                # tool-loop 引擎
│  ├─ personas/            # 人格系统（md 定义 + manager）
│  ├─ skills/              # 技能注册表 + 内置工具 + data/skills/*.py 自定义加载器（旧 agentcore/tools 已并入）
│  ├─ memory/              # 会话/记忆存储（内存/PG）+ 归档
│  ├─ rag/                 # RAG 摄取/检索/脱敏/蒸馏（M5）
│  ├─ workspace/           # LLM 沙箱工作区（白名单命令 + fs）
│  ├─ backup/              # 数据库备份/恢复
│  ├─ render/              # 回复渲染成图（MD 表格 → PNG；本机概览卡片 → PNG）
│  ├─ dashboard.py         # 看板只读 API 客户端（概览/曲线取数）
│  ├─ multiagent/          # 多 Agent 编排（M6，空壳）
│  └─ scheduler/           # 定时任务（M7）
├─ plugins/
│  └─ qq_agent_adapter/    # NoneBot 薄插件
│     ├─ matcher.py        # 消息路由（私聊 / 群唤醒词 / @机器人）+ 防抖 + 并发闸门
│     ├─ pipeline.py       # payload 组装、引用/转发解析、图片管线、最近图片缓冲
│     ├─ outbound.py       # 长回复分层投递 + 出站节流
│     ├─ debounce.py       # 按 chat+user 防抖合并（条数上限即结算）
│     ├─ group_context.py  # 群聊上下文环形缓冲（可选）
│     ├─ lifecycle.py      # 停机顺序编排（scheduler → flush → aclose）
│     ├─ wakewords.py      # 唤醒词加载/匹配/剥离（触发与剥前缀共用）
│     ├─ acl.py            # 权限控制（含戳一戳用的严格判据）
│     ├─ sink.py           # 主动推送
│     ├─ poke.py           # 戳一戳 → 本机概览图（notice 事件，不走 LLM）
│     ├─ memory_route.py   # /memory 查看/删除/清空自己的长期记忆
│     └─ admin.py          # /help /reset /status /skills /kb /usage /push /skill …
├─ data/kb_samples/        # 知识库样例语料（本地自备，gitignore 不入库）
├─ data/budget/            # 成本用量账本（运行时生成，gitignore）
├─ data/logs/              # 落盘日志（运行时生成，含聊天明文，gitignore）
├─ scripts/                # 运维与部署脚本
│  ├─ backup_db.py         #   备份 / 恢复 / 列出
│  ├─ deploy.py            #   宿主机一键部署（env/check/db/systemd/smoke）
│  ├─ compose_init.py      #   compose 项目目录一键引导（.env/config.yaml/data/）
│  ├─ docker-entrypoint.sh #   容器入口：修挂载点属权后降权到 app 运行
│  ├─ migrate.py           #   schema 迁移
│  ├─ check_review_docs.py #   评审产物与 BACKLOG 计数自检（pre-commit + CI 同款）
│  └─ perf_baseline.py     #   性能/内存基线
├─ docs/                   # 主题文档：配置 / 消息与媒体 / 记忆与知识库 / 群组 / 技能与权限 / 人格语音 / 运维 / 开发
├─ review/                 # 全部评审产物：REVIEW-*.md 报告 + FIX-*.md 修复记录 + REVIEW-WORKFLOW.md
├─ tests/                  # pytest 测试
├─ bot.py                  # NoneBot 启动入口
├─ AGENTS.md               # 交接文档：分层边界、硬性纪律、不变量、历史坑
├─ config.yaml             # Agent 行为配置
├─ docker-compose.yml      # PostgreSQL + pgvector（db）+ bot 全栈（容器化部署）
├─ Dockerfile              # bot 镜像（构建期 import 冒烟；含 ffmpeg / PG16 客户端 / 字体）
├─ .dockerignore
├─ pyproject.toml
└─ README.md
```

## 平台支持与移植边界

**一句话**：为 Linux 部署设计；macOS 可开发/试用；Windows 有已知阻塞项，暂不支持。

### 各平台现状

| 能力 | Linux | macOS | Windows |
|---|---|---|---|
| 对话 / 记忆 / RAG / 调度 / Web 总览 | ✅ | ✅ | ⚠ 有阻塞项（见下） |
| 备份恢复（pg_dump / JSONL） | ✅ | ✅（无 psql 时走 JSONL / docker） | ⚠ 文件锁降级 + 权限位语义缺失 |
| 运维技能（端口 / 进程 / systemctl） | ✅（`/proc` + systemd） | ⚠ 无 `/proc` → 端口列举/进程扫描返回空；无 systemctl → 服务控制不可用 | ⚠ 同 macOS |
| `proc_kill` / `run_shell` | ✅ | ✅（POSIX 信号 / bash 可用） | ❌ 信号语义不同，未适配 |
| 点歌语音（silk） | ✅（ffmpeg） | ⚠ 需自编 silk-python（官方仅 cp312 manylinux x86_64 wheel） | ⚠ 同上 |
| 文件权限不变量（落盘 0600 / 剥执行位） | ✅ | ✅ | ❌ Windows 无权限位语义，两条不变量静默失效 |
| Web 受控写入（`.env` + `config.yaml` 回写） | ✅ | ✅ | ❌ `os.O_NOFOLLOW` 在 Windows 不存在，保存即 500 |

Windows 上的实际表现：默认测试套件有约 200 条存量环境失败（`os.O_NOFOLLOW` 缺失、
CRLF 换行、缺 `systemctl`/`free`/`docker`/`psql`），**不是代码缺陷**；也因此
`scripts/check_review_docs.py --write-stats`（要求套件退出码为 0）在 Windows 上跑不了，
BACKLOG 的计数块只能在套件绿的 Linux 上重新生成。

### 移植到其它聊天平台

`agentcore/`（引擎 / 记忆 / RAG / 技能 / 预算 / 安全）是平台无关的纯 Python；平台耦合全部收敛在 `plugins/qq_agent_adapter/`（OneBot v11 适配）。移植步骤：

1. 新写适配层替代 `plugins/qq_agent_adapter/`：入站消息 → payload（text / images / user_text / ping_only / sender_card…）→ `engine.run(context, …)`；出站复用 `agentcore` 的 sink / 节流 / 分层投递接口
2. ACL 层当前假设用户 ID 是**纯数字**（`acl.py` 的 `isascii()+isdigit()` 校验，防全角数字穿透）——用户 ID 含字母的平台需同步放宽
3. 群语义（唤醒词 / @bot / 群上下文 / 群文件上传 / 合并转发）是 OneBot 形态，需按目标平台重做；`agentcore` 的围栏 / 脱敏 / 工具循环可直接沿用

### 部署假设（换环境前先读）

- **一个进程服务一个 bot 账号**：`self_id` 取连接中的协议端；`/reboot` 重启影响全部会话；预算是按日全局账本。多开请多进程 + 各自 `.env` / 数据目录 / 端口
- **相对路径按 CWD 解析**：`config.yaml` 与 env 默认的 `data/*` 目录随启动目录落盘，部署请改绝对路径（`config.yaml` 头部有完整清单）
- **默认时区 Asia/Shanghai**：提醒 / cron / 日键 / 每日上限都走 `agentcore.tz`；非中国部署设 `AGENT_SCHEDULER_TZ`（IANA 名或 `local`），显示层与日键同源
- **`/reboot` 执行链**末端是「退出等 supervisor 拉起」：systemd / docker / supervisorctl 可用；裸 `nohup` 部署退到退出即死亡（「工具集」节的 `/reboot` 部署前提有时序警示）

## 开发与测试命令

```bash
# 依赖（与 CI 完全一致，版本不漂移）
uv sync --locked --extra dev

# 默认套件（秒级；PG 用例自动跳过）
uv run --no-sync pytest -q

# 带 PG 的完整套件（PG 用例会真的建表/TRUNCATE，绝不能用生产库）
TEST_DATABASE_URL=postgresql://qqagent:qqagent@127.0.0.1:5432/qqagent_test \
  uv run --no-sync pytest -q

# 门禁三件套（CI 同款）
uv run --no-sync ruff check agentcore plugins tests bot.py scripts
uv run --no-sync ruff format --check agentcore plugins tests bot.py scripts
uv run --no-sync pyright agentcore

# 覆盖率门禁（CI 阈值 80%）
uv run --no-sync pytest -q --cov=agentcore --cov-fail-under=80

# 性能 / 内存泄漏基线（默认跳过，显式启用）
RUN_PERF=1 uv run --no-sync pytest -q tests/test_perf.py

# 评审产物与 BACKLOG 计数自检（归档 review/ 报告前必跑）
uv run --no-sync python scripts/check_review_docs.py --check-stats
```

提交流程与作者署名见 [CONTRIBUTING.md](../CONTRIBUTING.md)；接手者必读的边界与不变量见
[AGENTS.md](../AGENTS.md)；待办与已实证缺陷见 [BACKLOG.md](../BACKLOG.md)。
