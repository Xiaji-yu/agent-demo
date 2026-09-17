# 复盘：embedding 部署链路修复（2026-09-18）

**事件**：QQ bot 的知识库（KB）语义检索长期不可用——从今晨的"今汐是冷凝吗"连续幻觉答错，
到"菲比是谁"精准命中鸣潮百科全文，排查出一条由 6 个静默失败叠加而成的链路。
**终态**：硅基流动云端 bge-m3（OpenAI 兼容 API），全库 9107 块真语义向量，
6 个来源导入耗时约 3 分钟，语义检索实战验证通过。

## 时间线（关键节点）

| 时间 | 事件 |
|---|---|
| 09-17 09:51 | 首次大规模导入：embedding 超时 → **静默降级 hash**，24388 块垃圾向量入库（当时未察觉） |
| 09-17 深夜 | 逐层排查：bge-m3 CPU（40h 级）→ q4_0 量化 → 940MX GPU（CUDA context 反复 hang ×5） |
| 09-18 05:13 | 换硅基流动 bge-m3，但 base_url 带 `/embeddings` 后缀 → 双后缀 404 → **又一次静默降级 hash** |
| 09-18 05:17 | 修复 URL 后缀 → 重导 → **真向量**（29.7s/1731 块的稳定云端速度） |
| 09-18 05:21 | "菲比是谁"实战命中 KB 全文细节——语义检索真正工作 |

## 根因清单（6 个坑）

| # | 坑 | 现象 | 根因 |
|---|---|---|---|
| 1 | Ollama 模型名少 `tisandman/` 前缀 | 404 → 降级 hash | 模型名是精确匹配，命名空间前缀是名字一部分 |
| 2 | 硅基流动 base_url 带 `/embeddings` | 双后缀 404 → 降级 hash | OpenAI 兼容 base_url 是**基址**，资源路径客户端自己拼 |
| 3 | 940MX（2GB）跑 bge-m3 FP16（1.2GB） | CUDA context 反复 hang、NVML 报错 | 显存贴边，老卡驱动不稳 |
| 4 | EMBEDDING_TIMEOUT=30 用于 CPU 推理 | 每批 ReadTimeout → 降级 | 默认值是给云端 API 的；CPU 需 180-300 |
| 5 | 导入"快"得可疑时未查向量 | hash 被当成"导入成功" | 见下方改进项 |
| 6 | 判重提示"内容已变"实为指纹口径误读 | sha256sum（原始字节）vs content_digest（strip 后） | 排查方法错误，数据本身无恙 |

## 改进项（反馈项目代码，建议进下一轮评审）

### P1（高）：导入路径的 embedding 降级必须响亮失败

`EmbeddingClient` 的降级（`_enter_degraded`）设计意图是**对话场景**的容错（README：聊天不受影响）。
但 KB 导入（`ingest_file_smart` / `kb_add_chunks`）复用同一客户端时，远程失败被静默转成
hash 向量写入库中——**把上万块无检索价值的垃圾向量入库，还向用户报告"导入成功"**。
本次两次踩中（09-17 超时降级、09-18 的 404 降级），用户均无法从"导入很快"的表象分辨。

**修复方向**：`embed_many` 增加严格模式（或 ingest 调用处检测 `_degraded_since` 非 None），
KB 导入场景下远程失败应**抛异常中止导入**（ loudly ），而非降级 hash。对话场景保持降级语义不变。

### P2（中）：404 与连接失败应区分处置指引

`embedding 请求失败` 的 WARNING 目前带异常类型名（好），但对"404 / model not found"这类
**配置错误**（区别于 Ollama 没启动的网络错误），应在日志里直接给修复指引
（检查 `EMBEDDING_MODEL` 是否与 `ollama list`/服务商模型 ID 逐字一致）。

### P3（低，本次已顺手做）

`.env.example` 已加注释：`EMBEDDING_MODEL` 须逐字匹配（含命名空间前缀）、
`EMBEDDING_BASE_URL` 为基址不含资源路径。

## 当前部署状态

- **embedding**：硅基流动云端 `BAAI/bge-m3`（`https://api.siliconflow.cn/v1`），按量计费（日成本约几分钱）
- **本地 GPU 依赖解除**：940MX/9070GRE 与 embedding 无关；本地 Ollama 仅保留备用（CPU 模式）
- **KB**：鸣潮库街区百科 9107 块（6 份切块）+ hello 占位，全真语义向量
- **监控哨兵**：`journalctl -u agent-demo -f | grep -E "404|降级"`——出现即 embedding 又出问题
- 蒸馏来源（记忆蒸馏）随 TRUNCATE 被清，每日 03:00 的 `kb_digest` cron 会自动重建

## 运维要点

- 新增知识：`.md` 放 `data/kb_samples/` → QQ 发 `/kb samples`（管理员）
- 导入速度预期：云端 ~60 块/秒（1731 块约 30s）；**快得反常（秒级）先查向量**
- 验证向量真伪：`docker exec agent-demo-db-1 psql -U xiaji -d qqagent -c "SELECT substring(embedding::text from 1 for 60) FROM kb_chunks ORDER BY RANDOM() LIMIT 3;"`
  连续小数=真向量；`0.0xxxxx` 稀疏倍数=hash 残留
