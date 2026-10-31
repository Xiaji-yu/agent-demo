# FIX-default-font-20261031 —— 默认渲染字体换成屏显臻宋 + font_for 整段回落

> 性质：用户指派的功能变更（非评审轮次），命名用日期，同 `FIX-permission-levels-202610.md` 的
> §7 约定例外。**从 X 改为 Y 的语义变更**记录见 §1。

## 0. 需求与结论

用户要求：把 `xiaji-home` 项目 `font/` 目录的字体（`屏显臻宋-瘦金风格.ttf`，家族名
Clear Han Serif，3.35 MB，8284 字形）复制进本仓并设为项目默认字体。

结论：字体已复制到 `data/fonts/屏显臻宋-瘦金风格.ttf`，并插到全部字体候选链**首位**。
但该字体是 GB2312 量级展示字体——本仓渲染代码实际用到的 `▲▼`（rank 名次箭头）、
`°`（overview 页脚 `45°C`）与昵称/词云词/LLM 输出里的假名等**不在字面里**，且 Pillow
没有逐字形回落：实测缺字形渲染为**零墨迹空白**（`getmask().getbbox() is None`，不是
豆腐框），图上无声丢字。因此配套新增 `fonts.font_for(text, …)` 整段回落，并把所有
动态文本绘制点改走它。

## 1. 语义变更（从 X 改为 Y）

| 项 | 从 | 改为 |
|---|---|---|
| 常规链首位（fonts.py） | 系统 `NotoSansCJK-Regular.ttc`（装了 Noto 的机器上仓库 wqy 根本没被用上） | 仓库 `data/fonts/屏显臻宋-瘦金风格.ttf` |
| 粗体链（fonts.py） | Noto Bold 优先于常规链 | 与常规同链（屏显臻宋无粗体字面；层级靠字号/斜纹，不靠字重）；Noto Bold 殿后 |
| table.py / help_render.py 链首位 | 仓库 wqy-zenhei | 屏显臻宋，wqy 退居兜底 |
| 动态文本绘制 | 直接 `font()`（单一字体，缺字空白/豆腐框） | `font_for(text, …)` 沿链整段回落（元素粒度：徽章/昵称/词/行/整表） |

静态核对：对本仓渲染层全部字符串常量做 AST 扫描（691 个非 ASCII 字符），屏显臻宋缺
9 个——`°²→∝≈≤≥▲▼`，其中真正画到图上的只有 `▲▼`（rank）与 `°`（overview 页脚），
其余仅存在于注释/docstring。help 菜单固定文案不含缺字符（grep 0 命中），故 help_render
只换链首、不引入 font_for。

## 2. 改动面

- `agentcore/render/fonts.py`：`_DISPLAY_FONT` 入链首；`font()` 重构为经 `_load`
  （(path, size) 缓存）；新增 `_covers()`（getmask 墨迹探测 + 按字体/码点进程内缓存）
  与 `font_for()`（链上第一个完整覆盖文本字形的字体；全缺时返回第一个能出墨迹的候选，
  如 Noto 的可见豆腐框 .notdef，不抛异常）。
- 动态文本改走 `font_for`：`rank.py`（箭头徽章、昵称——测宽/截断/绘制同字体）、
  `review.py`（标题含昵称、评语按行、话题词按词）、`cloud.py`（词层、含群名标题）、
  `overview.py`（页脚行〔°C〕、进程名）、`table.py`（**全表字符集**选一个字体，列宽
  测量与绘制保持一致，一表不混排）、`text_card.py`（正文按行、标题、页脚左段）。
- 测试：`tests/test_group_stats_render.py` 新增 `TestFontFallback`（8 用例）。
- 文档：README（跟踪体积 21→27 MB、字体清单）、`docs/features-messaging.md`、
  `docs/operations.md`、AGENTS.md 不变量表新增「渲染字体」行、BACKLOG 字体子集化行补注。

## 3. 验证（verified）

- `font(20).path == data/fonts/屏显臻宋-瘦金风格.ttf`（常规/粗体均命中）；
  `font_for("▲"/"温度 45°C"/"月見にゃん")` 命中链上后续字体且逐字符有墨迹，
  `font(20).getmask("▲").getbbox() is None`（默认字体零墨迹的直接证据）。
- 像素探针（rank 卡 720×448）：delta 行箭头区墨迹 194/179 px、平线行 16 px、
  无 delta 对照行 0 px（证明探针区域背景干净）；假名昵称区 616 px。
- 真实渲染冒烟：rank/cloud/review/table/text_card 五种卡 PNG 均产出且非空。
- 变异复核：`font_for` 的覆盖判定改 `if True`（禁用回落）后
  `test_display_font_misses_arrow_but_fallback_covers` 与
  `test_fallback_covers_fragments` **必红**后还原。首版
  `test_fallback_covers_fragments` 整串验墨迹属恒真断言（° 空白被同串字符掩盖），
  已改逐字符断言并在变异下验证真的会红。
- 默认套件 `pytest -q` 全绿；`ruff check` + `ruff format --check` 全绿；
  `pyright agentcore` 0 errors。

## 4. 未验证面 / 剩余项

- 未在 Docker 镜像内实测（容器内无系统 Noto 时回落终点是仓库 wqy，链已覆盖；CI 构建会
  `COPY data/fonts` 整目录，新字体自动进镜像）。
- 屏显臻宋未随附独立 LICENSE 文件（xiaji-home 源目录亦无）；wqy 的 GPL 声明不覆盖它。
  字体来源与再分发条款由使用者自负其责（用户本人两个仓库间复制）。
- emoji 在屏显臻宋/wqy 下均无字形（回落到 Noto 时为可见豆腐框）——与换字体前的
  历史行为一致，未另行处理。
