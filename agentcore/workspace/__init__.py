"""agentcore/workspace：给 LLM 一个受限沙箱工作目录。

- fs：文件操作，路径锁定在 data/workspace/<user_id>/ 内（防穿越）
- runner：白名单命令执行（不经 shell、超时、截断）
- confirm：删除动作的二次确认（确认码 + 过期）
- utils：仅管理员判定
"""
