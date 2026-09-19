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

### P1（高）：远程故障分诊——429/超时重试、配置错误响亮失败（已修复）

**已落地**（2026-09-18，`agentcore/embedding/client.py`）：

- `_post_embeddings` 分诊：429 限流 / 5xx / 超时 / 断连 → **退避重试**
  （`EMBEDDING_RETRY_COUNT` 默认 5、`EMBEDDING_RETRY_BASE_DELAY` 默认 60s 递增）；
  4xx 配置错误（404 等）→ **立即 raise 不重试**；重试耗尽 → raise。
- `embed_many` **彻底移除降级 hash**：远程失败（重试耗尽/配置错误）通知宿主后
  响亮失败，由调用方处理（ingest 中止报错且重跑按指纹续传、facts 跳过、召回为空）。
- 未配置远程（无 base_url/key）→ 合法的本地 hash 模式保留。
- 回归：`TestRemoteRetryAndFail` 6 条 + `TestOnErrorNotify` 4 条重写；
  变异复核 4/4 抓住（429 不重试 / 404 重试 / 超时不重试 / 失败降级 hash）。

**待办数据修复**：429 限流期污染的 3854 块 hash（23 个来源，id 148-170）需
`/kb forget` 后 `/kb samples` 重导（重试机制下不再污染）。

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


## 勘误（M17，来源 `REVIEW-6ec3f7c..a36ea1d.md`）

本文件正文提到「ingest 中止报错」的措辞与实现不符：`_run_samples_job`
（`plugins/qq_agent_adapter/admin.py`）是**逐单元 except 后 continue**，
`tests/test_rag.py::test_failure_does_not_abort_batch` 明确断言不中止。
准确表述应为「失败单元跳过并继续，重跑按指纹续传」。

另：commit `1632b72`（/usage）的 message 写了「README + `.env.example` docs」，
但该 commit 从未改动 `.env.example`（`git show 1632b72 --name-only` 可证）。
`/usage` 实际不需要新 env，属纯记账问题。
