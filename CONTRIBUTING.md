# Contributing

感谢你愿意为 agent-demo 贡献代码。请先花 2 分钟阅读本文件，确保提交风格一致。

## 开发环境

```bash
git clone https://github.com/<your-username>/agent-demo.git
cd agent-demo
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# 编辑 .env 填入必要配置
docker compose up -d db   # M1+ 需要
python bot.py
```

## 代码风格

- Python 3.11+（`pyproject.toml` 的 `requires-python`；CI 用 3.12）
- 使用 `ruff` 做格式化/lint：`ruff check .` / `ruff format .`（CI 两者都检查，`ruff` 版本以 `pyproject.toml` 的 pin 为准——本地版本漂移会导致判定不一致）
- 功能测试：`pytest`（默认套件秒级；`TEST_DATABASE_URL` 门控的 PG 契约测试在无该变量时跳过，**本地/CI 都不跑**，改动存储层时请手动设该变量跑一遍 `tests/test_pg_store.py`）
- 性能门禁：`python scripts/perf_baseline.py`（与 `perf/baseline.json` 比对，劣化退出码 1；因机器抖动存在假阳，**建议只在夜间/专用机器上作为参考门禁**）
- 性能/泄漏基线：`RUN_PERF=1 pytest tests/test_perf.py -s`（默认跳过；阈值宽松，只抓 O(n²) 与无界增长）
  - 新增热路径函数 → 补一条延迟用例；新增缓存/缓冲 → 补一条有界性用例
- 性能基准存档/劣化对比：`python scripts/perf_baseline.py`（劣化退出码 1）；有意优化或换机器后 `--update` 重建基线
- 类型注解尽量补齐
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

## Issue / PR

- Issue 请描述预期行为、实际行为、复现步骤
- PR 请先开 Issue 讨论，再提交代码
