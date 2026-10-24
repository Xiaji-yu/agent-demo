# Contributing

感谢你愿意为 agent-demo 贡献代码。请先花 2 分钟阅读本文件，确保提交风格一致。

## 开发环境

```bash
git clone https://github.com/<your-username>/agent-demo.git
cd agent-demo

# 推荐：按 uv.lock 装依赖（与 CI 一致）
uv sync --locked --extra dev
source .venv/bin/activate

# 备选（无 uv）：pip install -e ".[dev]"

cp .env.example .env
# 编辑 .env 填入必要配置
docker compose up -d db   # M1+ 需要
python bot.py
```

> 依赖升级：改 `pyproject.toml` 后跑 `uv lock` 并提交 `uv.lock`；不要手工编辑锁文件。
> Dependabot 每月提升级 PR，必须过全量 CI（含 PG 套件）才合入。

> 不需要完整 git 历史时用**浅克隆**省流量（跟踪内容约 21 MB，其中 `data/fonts/` 中文字体占 16.8 MB）：
> `git clone --depth 1 https://github.com/Xiaji-yu/agent-demo.git`；需要按需拉取大文件用 `--filter=blob:none`。

## 代码风格

- Python 3.11+（`pyproject.toml` 的 `requires-python`；CI 用 3.12）
- 使用 `ruff` 做格式化/lint：`ruff check .` / `ruff format .`（CI 两者都检查，`ruff` 版本以 `pyproject.toml` 的 pin 为准——本地版本漂移会导致判定不一致）
- 功能测试：`pytest`（默认套件秒级，`TEST_DATABASE_URL` 门控的 PG 用例显示为 skipped）
- 覆盖率：`pytest --cov=agentcore --cov-report=term-missing`（CI 同款，并以
  `--cov-fail-under=80` 门禁；阈值只在 CI 生效，本地跑单个测试文件不会被覆盖率卡住）
- **存储层改动必须跑 PG 用例**：CI 已加 `pgvector/pgvector:pg16` service 并导出 `TEST_DATABASE_URL`，
  所以 PG 侧不再是"永久跳过"。本地请用独立库手动跑（绝不能用生产库，`conftest.py` 有硬断言）：
  ```bash
  TEST_DATABASE_URL="postgresql://qqagent:qqagent@127.0.0.1:5432/qqagent_test" pytest \
    tests/test_pg_store.py tests/test_store_contract.py -q
  ```
  `tests/test_store_contract.py` 把内存与 PG 两套实现用**同一批断言**参数化锁死；改任一侧都要跑它
- 性能门禁：`python scripts/perf_baseline.py`（与 `perf/baseline.json` 比对，劣化退出码 1；因机器抖动存在假阳，**建议只在夜间/专用机器上作为参考门禁**）
- 评审产物自检（归档 `review/` 报告前必跑，pre-commit/CI 同款）：`python scripts/check_review_docs.py`；测试数变化后跑一次 `--write-stats` 重新生成 BACKLOG 头部计数块（该块机器生成，勿手改）
- 性能/泄漏基线：`RUN_PERF=1 pytest tests/test_perf.py -s`（默认跳过；阈值宽松，只抓 O(n²) 与无界增长）
  - 新增热路径函数 → 补一条延迟用例；新增缓存/缓冲 → 补一条有界性用例
- 性能基准存档/劣化对比：`python scripts/perf_baseline.py`（劣化退出码 1）；有意优化或换机器后 `--update` 重建基线
- 类型检查：`pyright agentcore`（CI 同款门禁；只覆盖 `agentcore`，配置见 `pyrightconfig.json`。
  `pyright` 版本以 `pyproject.toml` 的 pin 为准，理由同 ruff）
- 提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/)：
  - `feat: ...`
  - `fix: ...`
  - `docs: ...`
  - `refactor: ...`

## 目录约定

```
agentcore/     # 纯 Python 包，不依赖 NoneBot
plugins/       # NoneBot 薄插件
data/          # 示例语料/测试数据
```

## 版本与分支管理

- **`main`**：始终保持可发布（CI 绿）；不直接在 `main` 上堆未完成的改动。
- **版本分支**：每个版本从 `main` 切一条 `release/<version>`，在其上完成该版本的收尾与修复。
  当前版本分支：**`release/1.0.0-beta`**（版本号 `1.0.0b1`，以 `pyproject.toml` 为准）。
- **后续更新**：每次更新（功能 / 修复 / 文档）都**开新分支**——`feat/<主题>`、`fix/<主题>`，
  或下一个版本的 `release/<version>`——经 PR 合回 `main`；**不要在 `main` 或已发布的 release 分支上继续开发**。
- **分支命名**：`release/1.0.0-beta`、`release/1.1.0`、`feat/group-stats`、`fix/runner-whitelist`。
- **版本号**（PEP 440，语义参照 SemVer）：`1.0.0b1` = 1.0.0 beta 1；正式 `1.0.0`；补丁 `1.0.1`（兼容修复）；
  次版本 `1.1.0`（兼容新增）；主版本 `2.0.0`（不兼容变更）。
- **发版流程**：
  1. 在 `release/<version>` 分支上确认 `pyproject.toml`、README 徽章与文档齐平；
  2. 跑交付前检查清单（默认套件 + `ruff check` / `ruff format --check`；动过存储/备份再跑带 `TEST_DATABASE_URL` 的套件；`scripts/check_review_docs.py`）；
  3. 打标签并推送：`git tag -a v<version> -m "<version>"` → `git push origin release/<version> --follow-tags`；
  4. 在 GitHub 以该 tag 建 Release（写变更摘要）。

## Issue / PR

- Issue 请描述预期行为、实际行为、复现步骤
- PR 请先开 Issue 讨论，再提交代码
