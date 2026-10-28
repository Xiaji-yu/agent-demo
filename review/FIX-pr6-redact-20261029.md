# FIX-pr6-redact-20261029 — H2 隐私止血 + L4 内网拓扑 + 防再犯守卫

对应评审：[REVIEW-pr6-3c00145.md](REVIEW-pr6-3c00145.md) 的 H2 与 L4（线4）。
决策记录：用户选择**只做止血 commit**（历史不重写——号码已随 PR #6 与 UGREEN 推送公开，
重写仅能防"翻历史"，防不了已缓存/已下载；QQ 群号/号非凭据，暴露面为骚扰与身份关联）。

## 改动

| 文件 | 改法 |
|---|---|
| `data/workspace/cron/jobs.json` | message 内 3 处 + `to` 1 处：真实群号/QQ 号 → 占位 `123456789`（基线文档既有惯例，`docs/features-community.md` 基线 L164 同款）；`sh /root/.picoclaw/workspace/scripts/energy_report.sh` → `sh /path/to/scripts/energy_report.sh` |
| `docs/features-community.md` | 示例 2 处同上 |
| `agentcore/scheduler/jobs.py:26` | 模块 docstring 示例 `to` 1 处（评审子代理漏报、主代理全树扫描补获的第 5 处） |
| `docs/deploy-nas.md` | `192.168.110.185` ×6 → `192.168.1.100`（与 `config-quickstart.md` 通用示例口径一致；L4） |
| `tests/test_repo_guards.py` | 新增 `TestQQNumberRedaction`：入库文本 QQ 语义上下文（`group:`/`private:`/`群 N`/`QQ N`）携带 5-11 位号即红，占位 `123456789` 豁免；`tests/`（假数据）与 `review/`（审计记录，号码另行打码）不扫。探测器自带红/绿自测夹具（= 改坏必红的变异复核） |
| `review/REVIEW-pr6-3c00145.md` | 报告内引用的号码打码为 `10816466**`/`815833**`（评审产物不新增明文） |
| `BACKLOG.md`、`agentcore/loop/engine.py`、`plugins/qq_agent_adapter/{matcher,media,pipeline}.py`、`docs/features-messaging.md` | **存量打码**（守卫上线即抓出，main 历史同样公开）：线上事故记录里的真实群号 `1051425116/1108838060/1076073471/667575205` 与 QQ 号 `2224513919/3629537600/1447458891` → 前 4 位 + `****`（审计语义保留，git 历史可查全文） |
| `plugins/qq_agent_adapter/sink.py`、`admin.py` | docstring 短号示例 `private:123456`/`group:789012` 统一为占位 `123456789` |

## 验证

- 全树 `grep 1081646667|815833475|/root/`（py/md/json）：**0 命中**
- `pytest tests/test_scheduler_jobs.py -q`：**56 passed**（`TestRepoJobsFile` 只断言格式+默认停用，不依赖具体号码，无需改断言）
- `pytest tests/test_repo_guards.py -q`：**23 passed**（新守卫含探测器红/绿自测；首跑抓出存量 3 文件 8 号码——即上表存量打码的来源，复跑全绿）
- 注释/docstring 改动不涉逻辑，默认套件全量另跑确认（见 commit 前 CI）
- main 分支验证 `git grep` 零命中（PR 两号码从未进 main；存量号码在 main 历史同样公开，本轮仅防新索引）

## 遗留（未在本轮修）

- H2 历史残留：本仓 UGREEN 旧 commit、PR #6 diff 页、贡献者 fork `Daiyimo:main` 三处仍可翻到——**用户已知情并选择不重写**；贡献者已被提醒 fork 侧同样含号码、其真实 jobs.json 属运行时数据不应入库
- H1（沙箱内配置）、H3（gitignore 三目录）、M×22：待架构定夺与后续 FIX
