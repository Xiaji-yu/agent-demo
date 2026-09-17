# 知识库投放目录

把 `.md` 文件放到本目录，然后（管理员身份）在 QQ 发送 `/kb samples` 后台导入，
或直接在服务器运行：

```bash
.venv/bin/python scripts/ingest_kb_samples.py
```

说明：

- 本文件是**目录占位**（保证 git clone 后目录存在），可以删除
- 你自己的知识文件请用 `.md` 格式；目录下其他文件不会被提交（.gitignore 已排除）
- 导入后任何会话都能语义检索命中（`/kb search 关键词`），也可在对话中自动召回
- 大文件会被自动切块落盘后逐块入库，不再受单文件 2MB 上限截断
