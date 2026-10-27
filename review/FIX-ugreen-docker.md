# FIX-ugreen-docker —— NAS 容器化部署修复（P1-2 / P1-3 / P1-5 / P2-1/5/6/8/11）

> 来源：`agent-demo-review-nas-2026-10-27.md`（Docker / NAS 部署方向全量 Review）
> 分支：`ugreen-docker`（自 `main` @ `5751f73` 切出）
> 目标环境：绿联 DX4600（x86_64 / N5105 / 16G / Docker RootDir 在 3.6T 存储池），
> 经 NapCat 反向 WS 替换 picoclaw，对标 openclaw 的 compose 部署形态。

## 1. 修了什么（按 Review 编号）

### P1-2 web 受控写面在容器内整体不可用（500）

- **根因**：`.env` 被 `.dockerignore:14` 排除、compose 未挂载 → `config_write.write_env_key`
  在 `path.exists()` 处抛 `EnvWriteError("env_file_missing")`，114 个 env 键写入全部 500；
  `config_yaml.config_path()` 解析到 `/app/config.yaml`，而 `/app` 目录属主是 root
  （`COPY --chown` 只改文件属主），`atomic_write` 的同目录临时文件无处落地，
  唯一热更键 `agent.max_iterations` 也写不了。QQ 侧 `/model` 同款报错。
- **修法**：compose 挂 `./.env:/app/.env` + `environment` 显式 `AGENT_ENV_FILE=/app/.env`；
  挂 `./config.yaml:/app/config.yaml`；**Dockerfile 补 `RUN chown app:app /app`**
  （关键：不补这行，单文件挂载也写不进——原子写在 `/app` 建 `*.tmp.write`）；
  `docker-entrypoint.sh` 对两个单文件挂载补属权修复（宿主 uid 非 1000 时目录 chown
  覆盖不到文件），自检（`check_writable`）同步覆盖。

### P1-3 data/ 根运行时单文件不挂载，容器重建即静默丢失

- **根因**：compose 只挂 10 个 `data/` 子目录；`llm-providers.json` / `2fa-secret.json` /
  `nudge.json` / `search-providers.json` / `nudge-hotspot.json` / `reboot-notice.json` /
  `push-state.json` 默认落 `data/` 根（CWD 相对），在容器可写层，`down && up` 或升级镜像
  recreate 后全部丢失（web 配过的 provider/key 静默回落 `.env` 旧键、2FA secret 重置）。
- **修法**：compose 新增 `data/state` 挂载；`.env.example` 新增「容器部署：状态文件重定向」
  注释块（7 个 `AGENT_*_FILE` 键指到 `data/state/…`，默认注释、宿主部署零影响）；
  compose 同批挂 `data/mirror`（`AGENT_BACKUP_MIRROR_DIR` 默认落点，异地换 override 文件）
  与 `data/ssh`（`ssh_run` 的 `known_hosts` TOFU 记录跨重建存活）；
  Dockerfile 预建三目录、entrypoint `MOUNTS` 纳入属权修复。
- **行为变更记录（从 X 改为 Y）**：文档原写「`data/` 根下的单文件刻意不挂：容器内自管理」
  → 改为「可重定向进 `data/state/` 挂载卷」；`docs/operations.md` 挂载边界表与 recreate
  语义、README「Docker 部署」数据隔离段同步改写。

### P1-5 6 个键声明 `live=True`，运行态实际完全不生效

- **根因**：`AGENT_OUTBOUND_MIN_INTERVAL` / `_GLOBAL_MIN_INTERVAL` / `_PER_MIN` /
  `_MAX_WAIT` / `_MAX_TARGETS` / `AGENT_RECENT_IMAGE_BYTES` 的运行态载体是 import 期
  固化的单例（`outbound.default_throttle` 首次调用即装配；pipeline 图片缓冲同理），
  `apply_runtime` 只改 `os.environ`，实例属性一个都不变——web 返回 `live`、前端显示
  「即时生效」是谎报。被 QQ 风控压制时调出站间隔/每分条数会完全无效且无日志痕迹。
- **修法**（Review 选项①，最小改动）：6 键回落 `boot`（`settings_spec.py`），设置页据此
  诚实提示「重启生效」。未选选项②（按 `_GROUP_CONTEXT_ATTRS` 模式补运行态回写）——
  自托管单管理员场景重启成本可接受，且回写要暴露可变单例、面更大。

### P2 项（与 NAS 部署直接相关）

| # | 修法 |
|---|---|
| P2-1 psql 依赖 | Dockerfile apt 补 `libedit2` / `libicu72`；`docs/operations.md` 与 `docs/deploy-nas.md` 写死 `ldd /usr/local/pg16/psql` 自检（缺 `not found` 即缺库）。恢复唯一路径是 psql，"备份能跑、恢复不能跑"是最坏组合 |
| P2-3 异地镜像无挂载点 | compose 挂 `data/mirror`；`AGENT_BACKUP_MIRROR_DIR=data/mirror`；跨盘（DX4600 双池）用 override 文件换宿主源、target 不变，entrypoint 属权修复覆盖该目录 |
| P2-5 db shm | compose `shm_size: "256mb"`（docker 默认 64MB，PG 并行查询会 "No space left on device"——不是磁盘满是 /dev/shm 满）。资源限额未加：16G 内存充裕，文档注明可选 |
| P2-6 PORT 写死三处 | compose 容器侧改 `${PORT:-8080}` 插值（`EXPOSE`/healthcheck 仍 8080 不动——改 PORT 的场景由文档说明耦合）；改 PORT 不再静默破坏映射 |
| P2-8 hotspot 键 | `AGENT_NUDGE_HOTSPOT_FILE` 补 `writable=False`（描述早写「结构路径，只读」但标志位缺失，与「结构路径刻意不可写」不变量矛盾） |
| P2-11 compose 变量无文档 | `.env.example` 新增「compose 层变量（不注入容器）」节：`AGENT_BOT_IMAGE`/`AGENT_BOT_PORT`/`AGENT_BOT_BIND`/`AGENT_DATA_DIR`/`AGENT_DB_PORT`/`AGENT_DB_NAME`/`TZ` + PORT 耦合 + compose 模式密码约束（`@` 不编码、`$` 要写 `$$`） |

### 明确不适用的项（NAS 实机复核）

- **P1-1**（libpq 拷贝写死 x86_64）：NAS `uname -m` = x86_64，不适用；ARM 构建失败仍存在，
  已在本文件与 `docs/deploy-nas.md` 标注。
- **P1-4**（pgdata named volume 落系统盘）：Docker RootDir=/volume1/@docker 在 3.6T
  存储池（925G 空闲），不适用。

## 2. 回归与变异复核

| 用例 | 锁住的行为 |
|---|---|
| `tests/test_config_write.py::test_singleton_backed_keys_report_boot_not_live` | 6 键 `apply_runtime` 必须返回 `boot`；**变异：任一键加回 `live=True` → 红** |
| `tests/test_web.py::test_structure_path_keys_are_readonly` | `AGENT_NUDGE_HOTSPOT_FILE` 等 5 个结构路径键不进 `WRITABLE`；**变异：去掉 `writable=False` → 红** |

既有 `test_web.py::test_schema_covers_env_example_and_invariants`（`.env.example` 未注释键
与 `ENV_SPECS` 集合相等）确认新增注释块未引入漂移。

## 3. 验证记录

| 检查 | 命令 | 结果 |
|---|---|---|
| 全量默认套件 | `.venv/bin/python -m pytest -q` | **209 failed / 3141 passed / 79 skipped**（287s）；review 的 Windows 基线 210/3138/79。收集 3429 = 基线 3427 + 新增 2 用例；通过 +3/失败 −1 属 review §1.1 记录的 Windows 时间戳分辨率抖动族 |
| 影响面零新增证明 | 改动影响面 7 个测试文件在 `main` 基线 worktree 与本分支各跑一遍，失败名单逐条比对 | **基线 91 = 分支 91，名单完全一致**（零新增、零变化） |
| 新回归用例 | `test_singleton_backed_keys_report_boot_not_live` / `test_structure_path_keys_are_readonly` | 均通过；两处变异（加回 `live=True` / 去掉 `writable=False`）分别被抓，反向精确编辑还原 |
| ruff 双门禁 | `ruff check` + `ruff format --check`（0.17.1，与 pyproject pin 一致） | **All checks passed / 233 files already formatted** |
| 评审产物自检 | `python scripts/check_review_docs.py` | **[ok]**（索引完整 / 产物位置 / 文档地图断链；首跑因本文件未登记 §9.1 而 FAIL，补登记后通过） |
| compose YAML | Python `yaml.safe_load` + 字段断言 | 通过（shm_size/AGENT_ENV_FILE/PORT 插值/15 个挂载均在位） |
| `docker compose config` | — | **unknown**：本机无 docker，按计划留 NAS 实机执行（AC 已声明） |
| `--check-stats`（BACKLOG 计数块） | — | **未跑**：该门禁对比的是 Linux/CI 计数（3354+73），Windows 上因环境差异族必不一致（与本次改动无关的既有状态）；本次 +2 用例后的 Linux 计数需在 CI/Linux 用 `--write-stats` 刷新，勿在 Windows 上手写 |
| pyright | — | **不适用**：CI 只查 `agentcore`，本次未触碰 `agentcore/` |

NAS 侧端到端（构建 → 切换 NapCat WS → 下线 picoclaw → 备份验收）按 `docs/deploy-nas.md` §8 验收清单执行，结果以实机记录为准。

## 4. 剩余项（不在本分支）

P2-15（web `disk_usage` 同步阻塞事件循环，NAS 本地盘风险低）、P2-13（`self_id`/admin 数字
参数漏 `isascii()`）、P2-14（reboot 回执临时文件漏 `os.fchmod`）、P2-16/17/19/20 等代码类
P2、P2-4（autoheal 看门狗，可 NAS 侧加容器解决）。
