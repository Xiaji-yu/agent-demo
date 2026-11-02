# data/fonts — 渲染字体与许可状态

本目录是**本机渲染**（rank / cloud / review / overview / table / text_card /
help_render）的字体落点，加载链见 `agentcore/render/fonts.py`（`font()` 固定
文案 / `font_for()` 动态文本逐字形回落）。

| 文件 | 用途 | 许可状态 |
|---|---|---|
| `屏显臻宋-瘦金风格.ttf` | 默认展示字体（2026-10-31 起，候选链首位） | **未随附独立许可文件**，见下 |
| `wqy-zenhei.ttc` | 全字面兜底 | GPL-2.0（见同目录 `GPL-2.0.txt` 与 `LICENSE-WQY-ZenHei.txt`） |
| `GPL-2.0.txt` / `LICENSE-WQY-ZenHei.txt` | 仅覆盖 wqy-zenhei | — |

## 屏显臻宋-瘦金风格.ttf 的来源与再分发边界

- 来源：仓库使用者本人另一仓库（xiaji-home）同源文件，2026-10-31 复制入库；
  未随附作者许可文本，`review/FIX-default-font-20261031.md` §4 已如实记录
  「字体来源与再分发条款由使用者自负其责」。
- **对本仓库**：使用者即字体持有人，两仓库间复制属个人使用安排。
- **对第三方 fork / GHCR 镜像再分发**（`docker-publish.yml` 会把本目录打进
  镜像）：本文件不构成授权。再分发前请自行核实该字体的作者与条款；无法核实
  时应从构建中移除（`.dockerignore` 排除 `data/fonts`）并让 `fonts.py` 回落到
  系统字体或 wqy-zenhei。

> 背景：屏显臻宋是 GB2312 量级展示字体，缺 `▲▼°`/假名等字形且 Pillow 无逐字
> 形回落——缺字渲染为零墨迹空白（不是豆腐框），这也是 `fonts.font_for` 存在
> 的原因（REVIEW-4d5c1b8..63f9444 M10 同型问题的修复记录见
> `review/FIX-default-font-20261031.md`）。
