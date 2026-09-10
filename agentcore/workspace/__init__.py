"""agentcore/workspace：给 LLM 一个受限沙箱工作目录。

- fs：文件操作，路径锁定在共享工作区根目录（WORKSPACE_DIR，默认 data/workspace/）内（防穿越）
- runner：白名单命令执行（不经 shell、逐参数校验、最小环境变量、超时、流式截断、审计带 uid）
- confirm：删除动作的二次确认（确认码 + 过期 + 失败限速）
- utils：仅管理员判定与工作区根目录解析
"""
