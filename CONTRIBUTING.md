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

- Python 3.10+
- 使用 `ruff` 做格式化/lint：`ruff check .` / `ruff format .`
- 功能测试：`pytest`（默认套件秒级，含 32 项 `TEST_DATABASE_URL` 门控的 PG 集成）
- 性能/泄漏基线：`RUN_PERF=1 pytest tests/test_perf.py -s`（默认跳过；阈值宽松，只抓 O(n²) 与无界增长）
  - 新增热路径函数 → 补一条延迟用例；新增缓存/缓冲 → 补一条有界性用例
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
