# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)与 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 格式。
版本号以 `pyproject.toml` 为准（PEP 440：`1.0.1b1` = 1.0.1-beta）。

## [1.0.1-beta] - 2026-10-21

自 `1.0.0`（tag `v1.0.0b1`）以来的改动，按主题归类。

### LLM 与模型

- **多 LLM Provider**：`data/llm-providers.json`（0600、原子写、备份、写后校验）支持 OpenAI / Anthropic 双协议，web 模型页 CRUD / 设主 / 排序 / 测试连接，key 只写不回显；运行时 `/model` 切模型（探测通过才切）。
- **模型页 Embedding / 搜索配置（M3/M4）**：embedding 端点/key/模型/维度与搜索配置入口；`SEARCH_API_KEY` / `SEARCH_BASE_URL` / `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` 四个凭据从「走 SSH 不可写」放开为 web 可写（只写不回显、改后重启生效）。
- **联网搜索多源 + fallback 容错**：`data/search-providers.json`（0600，同 llm-providers）支持多搜索源，按列表顺序 fallback——一个源失败（超时/断连/非 2xx）自动切下一个；env 单配置回落兼容。
- **stepfun（阶跃星辰）搜索格式**：新增第三种搜索格式（Bearer 鉴权、请求 `{query, n}`、解析 `results:[{url,title,time,snippet,content}]`）。

### Web 与设置

- **17 个设置簇**：设置页折叠 + 锚点 + 跨簇搜索，gate 关闭时同簇后端拒写（409）；多 provider 面板 + 群统计双周视图。
- **顶层「模型」「人格」标签**：provider 面板与人格导入/编辑/删除从设置页搬入顶层标签，防双入口漂移。
- 修复人格标签表单被 5s 轮询清空的问题。

### 人格

- **群人格**：每群独立、群内共享；assistant 默认位让出。

### 群统计

- 修复含 NUL 群名片毒化全部统计 flush（落库前剥控制字符）。
- 群历史图片可按上下文回捞 + 统计白名单可收窄。

### Skills 与工具

- `xiaoheihe_post`：小黑盒帖子抓取（分享链接 → 标题/作者/正文）。
- `npm_update`：登记表制 npm 全局包更新（medium+）。
- `render_text_image`：文字/歌词/长文 → 项目风格长图（纸色底、墨线面板）；
  门禁：6000 字软上限截断 / 12000 字硬上限拒绝 / 画布高 ≤4000px / 渲染 10s 超时；
  无中文字体、渲染失败或图片过大均降级为文本，发送结果未知时不重发。
- `/skill list`：已安装技能查询命令。
- 支持 `data/skills/*.py` 自定义工具型技能。

### 诊断与日志

- 未触发群消息补 `[trigger-miss]` 诊断行。
- 工具调用与回合三层耗时日志（LLM step / skill done / reply）。

### 沙箱与安全

- 解压线退役外部 `zip`/`unzip`/`tar`，改用标准库实现（0600 落盘）。

### 工程与文档

- CI 升级 `actions/checkout@v5` 与 `setup-python@v6`。
- 全量文档体检修复漂移 + 清理过期继任文档。
- `data/search-providers.json` 加入 `.gitignore`（含 key 运行时产物，防误提交泄漏）。
