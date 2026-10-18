# 群组与社区功能

> 本文自 `README.md` 拆分而来（2026-10-16）；返回 [README](../README.md)。

## 配置一览

> **必配** = 不设置则该功能不可用（示例值即占位符）；**选配** = 有可用默认值或仅用于调优。全局必配见 [configuration.md](configuration.md)。

| 配置项 | 标注 | 默认 | 说明 |
|---|---|---|---|
| `DATABASE_URL` | **必配**（PG 专属功能） | 留空 = 归档不可用、统计仅进程内 | 统计持久化与原文归档仅 PG |
| `AGENT_GROUP_STATS` | **必配**（启用统计） | `0`（关闭） | 群发言榜/词云总开关（opt-in） |
| `AGENT_GROUP_ARCHIVE` | **必配**（启用归档） | `0`（关闭） | 原文归档开关（opt-in，仅 PG） |
| `AGENT_GROUP_STATS_FLUSH_SECONDS` / `_RETENTION_DAYS` / `_TOP_N` / `_WORDS` / `_MIN_MSGS` / `_STOPWORDS` | 选配 | `30` / `35` / `5` / `80` / `20` / 空 | 统计聚合参数 |
| `AGENT_GROUP_STATS_CRON` | 选配 | `0 9 * * 1` | 周报时间 |
| `AGENT_GROUP_STATS_CMD_COOLDOWN` / `_WEEKLY_GROUPS` | 选配 | `30` / 空（不推） | 命令冷却 / 周报推送白名单 |
| `AGENT_GROUP_STATS_REVIEW_MIN_MSGS` / `_MAX_CHARS` / `_COOLDOWN` | 选配 | `10` / `80` / `30` | 个人点评 |
| `AGENT_GROUP_ARCHIVE_RETENTION_DAYS` / `_FLUSH_SECONDS` / `_BUFFER_MAX` | 选配 | `30` / `30` / `50000` | 归档保留与刷盘 |
| `AGENT_REMINDER_TICK` | 选配 | `30` | 提醒轮询秒数 |
| `AGENT_PUSH_ENABLED` / `_TICK` / `_RETRY_DELAY` / `_MAX_RETRY_DELAY` / `_MAX_FAILURES` / `_DAILY_CAP_PER_TARGET` / `_MAX_CHARS` | 选配 | `1` / `30` / `300` / `86400` / `5` / `1` / `500` | 定时内容推送 |
| `AGENT_SCHEDULER_TZ` | 选配 | `Asia/Shanghai`（或 `local`） | 调度时区；填不存在的 IANA 名会告警并回退服务器本地 |

## 群发言榜与词云（opt-in）

按群统计「谁话多 / 大家在聊什么」：`/发言榜`、`/词云` 两个群命令（渲染成图片），
加**每周一 9:00 自动周报**（连发榜图 + 词云图到每个达标的群）。

```env
AGENT_GROUP_STATS=0               # 总开关：默认关（opt-in），1 开启
AGENT_GROUP_STATS_FLUSH_SECONDS=30   # 聚合落库周期 = 榜单数据滞后上限
AGENT_GROUP_STATS_RETENTION_DAYS=35  # 保留 5 周（够「上周对比」）
AGENT_GROUP_STATS_TOP_N=5         # 榜单人数
AGENT_GROUP_STATS_WORDS=80        # 词云词数上限（贯通到渲染，钳 10..500）
AGENT_GROUP_STATS_MIN_MSGS=20     # 周报最低群友条数，不足不发
AGENT_GROUP_STATS_CRON=0 9 * * 1  # 周报时间；设为空串 = 只保留群命令
AGENT_GROUP_STATS_CMD_COOLDOWN=30 # 同群命令冷却（防刷屏）
AGENT_GROUP_STATS_STOPWORDS=      # 追加停用词（逗号分隔）
```

- **只统计 `ALLOWED_GROUPS` 白名单群；空集 = 不采集**（fail-closed，与聊天面
  同键同语义——开关开着却没配白名单时启动日志有 WARNING 提醒）；
  只统计群友，bot 自己的发言不计入
- **聚合统计只落数值**：每用户每日条数/字数/小时直方图 + 分词词频
  （jieba 中文分词）；**消息原文仅在显式开启 `AGENT_GROUP_ARCHIVE=1` 时**
  经原文归档落库（见下文）——这是对群聊上下文「绝不落库」
  原则的**有意识让步**，隐私收敛靠三点：opt-in、仅白名单群、聚合优先
- **合并转发消息整条不采集**（转发内容不是本人的发言，会同时扭曲榜单与词云）；
  引用（reply）照常计入用户自己写的文字
- 榜单口径与产品截图一致：默认出**上一个完整周**（`/发言榜 本周` 出进行中的周），
  含百分比、新上榜标记、名次变化箭头、合计与最热时段；数据不足（<3 人或 <10 条）
  回文案不发图
- 无 `DATABASE_URL` 时统计仅进程内有效（重启清零）；PG 用三张独立表
  （`group_msg_daily` / `group_word_daily` / `group_reviews`），35 天滚动清理
- Web 只读总览「群统计」tab 同数据（只读聚合，无正文）

**LLM 个人点评（`my_group_review` skill，无硬命令）**：群友唤醒机器人说
「点评我一下 / 我这周表现如何 / 锐评我」时，由 **LLM 自行判断**是否调用该
skill（与点歌 skill 同模式），生成个人周报**卡片图**发到当前群：

```env
AGENT_GROUP_STATS_REVIEW_MIN_MSGS=10    # 低于 10 条不评（回「发言较少，咱不评价」）
AGENT_GROUP_STATS_REVIEW_MAX_CHARS=80   # 评语长度上限（群友口吻、偏向正面）
AGENT_GROUP_STATS_REVIEW_COOLDOWN=30    # per-user 冷却（每次触发可能花一次 LLM 调用）
```

- **只评自己**：skill 的 schema 没有 user_id 参数，评价对象只由当前会话注入——
  模型无法查看他人评价；卡片只含本人数据 + 群话题词背景
- **同周缓存**：首次生成后评语落库（`group_reviews`），重复查看**零 LLM 成本**；
  LLM 失败/预算熔断时不缓存（下次可重试），但**事实卡照发**（条数/排名/时段）
- 安全口径：昵称与话题词进 prompt 前过 `safety.py` 围栏（含围栏伪装打散），
  预算硬闸门熔断时放弃生成（不打 WARNING，是设计）；usage 记
  `group_stats_review` 路由
- 卡片渲染/发送失败 → 降级纯文本；投递状态 uncertain 时不重发也不补发文本
- **原文归档增强**（开 `AGENT_GROUP_ARCHIVE=1` 时）：评语素材从“数字”升级为
  “有梗”——本人高频词、转发次数、发图次数、被引用次数、一条代表性原句
  （全部过围栏；归档关闭时评语素材与之前完全一致）

**周报推送白名单**（cron 周一 9:00 那条推送的目标控制）：

```env
AGENT_GROUP_STATS_WEEKLY_GROUPS=111,222  # 空 = 一条都不推（fail-closed）
```

- 与 `AGENT_GROUP_STATS_MIN_MSGS`（默认 20）叠加：白名单决定“推给谁”，
  阈值决定“够不够格”；白名单须是 `ALLOWED_GROUPS` 的子集（否则跳过并告警）
- **只管推送**：白名单外的群照常采集，`/发言榜`、`/词云`、点评 skill 不受影响

## 群消息原文归档（opt-in，PG）

把白名单群每条非 bot 消息落 PG 表 `group_messages_raw`，保留 30 天——
统计聚合的**原文底账**（路线甲：聚合管线不变，归档用于对账/回溯/点评增强）：

```env
AGENT_GROUP_ARCHIVE=0                    # 总开关，默认关
AGENT_GROUP_ARCHIVE_RETENTION_DAYS=30    # 保留天数
AGENT_GROUP_ARCHIVE_FLUSH_SECONDS=30     # 批量落库周期 = kill -9 丢失窗口
AGENT_GROUP_ARCHIVE_BUFFER_MAX=50000     # 内存缓冲行数上限（超了丢最旧）
```

- **与“只存聚合”的统计是两个独立开关**：归档存的是**消息原文**（text/
  reply_to/forward_id），隐私口径更重——仅 `ALLOWED_GROUPS` 白名单群、
  **空集 = 不归档**（fail-closed，与统计采集同语义）、
  opt-in、bot 自己的消息不存、原文不进日志与 web 响应体
- **合并转发照常归档一行但不展开内容**（只存 forward_id）：展开要调协议端
  API 且是他人历史发言；点评用「转发次数」数字维度表达习惯，不碰内容。
  被引内容同理只记 `reply_to`——被引/被转发内容从不进词云与 prompt
- **仅 PG**：无 `DATABASE_URL` 或 PG init 失败 = 功能不可用（不降级内存，
  降级等于没有底账还给人“存了”的错觉）；pg_dump 每日备份自动含该表
  （整库 dump）。⚠️ **JSONL 兜底策略只导 7 张核心表**（`db_backup.py` 的
  `_PGDATA_TABLES`），**不含** `group_messages_raw` / `group_msg_daily` /
  `group_word_daily` / `group_reviews`——宿主机无 pg 客户端又无 docker 的部署
  走 JSONL 时，群统计与原文归档实际不在备份内，恢复顺序同限
- 持久性语义：停机 flush 不丢（lifecycle 顺序内）；`kill -9` 丢 ≤ flush 周期
- 索引按查询模式建：PK `(group_id, message_id)`（幂等去重）+ `(group_id,
  user_id, ts DESC)` / `(group_id, ts DESC)` / `(day)` / `(group_id, reply_to)`
- 对账：可用归档原文重算任意窗口的条数/词频，与实时聚合互相校验


## 定时提醒

直接在聊天里说人话即可：「10 分钟后提醒我喝水」「每天 9 点提醒我吃药」「每周一 8 点半提醒我开周会」。

- 时间解析是**确定性**的（不靠模型算时间），支持：`N秒/分钟/小时/天后`（含 `2小时30分钟后`）、
  `明天 8点`、`今天 22:00`、`9月12日 9点`、`2026-09-15 08:00`、`每天9点`、`每天早上8点`、
  `每周一 9点`、`工作日 9点`；只写时间点时「今天已过」会顺延到明天，
  明确写了「今天/某月某日」却已过去则直接报错，不会偷偷改期
- 调度器每 `AGENT_REMINDER_TICK` 秒（默认 30）检查一次到点提醒；**投递失败会顺延重试**
  （机器人当时没连接也不会把提醒弄丢）
- 这个轮询只是「醒来查一次」，没到点的提醒不会发消息；apscheduler 的每轮执行日志已默认压到
  WARNING（否则一天要刷几千行，把 `[msg]`/`[reply]` 这类有用日志淹掉），
  任务异常与启动期的 job 注册日志仍然保留
- 提醒落库在 `schedules` 表（一次性 + cron 周期），可用 `reminder_list` 查看、`reminder_cancel` 取消

## 定时内容推送（M7）

与「定时提醒」的分界：**提醒的文案是用户自己写的、确定性的**；定时内容推送是**部署者配置的任务**，
到点由 LLM 按 `prompt` 生成正文主动发给群/私聊——机器人因此能"自己开口"（早安问候、每日一句、
固定播报等）。两者共用 `schedules` 表与同一个出站节流器，用 `action` 字段区分（`remind` / `push`），
提醒类工具只认 `remind`，所以 `reminder_list` 里看不到推送任务。

任务在 `config.yaml` 的 `push.jobs` 里配置（**没有 DDL 变更**）：

```yaml
push:
  jobs:
    - name: 早安问候            # 稳定标识：改名前停用旧任务，否则重启会重建
      cron: "0 8 * * *"        # 5 段 cron
      target: group:123456789  # group:<群号> 或 private:<QQ号>
      prompt: 用一句话向群友们道早安，语气温暖自然，不超过 60 字。
      template: 大家早上好呀，新的一天开始啦～   # 兜底文案
```

- **LLM 生成 + 模板兜底**：LLM 调用失败 / 返回空 / 当日预算熔断（`AGENT_BUDGET_ENFORCE=1`）时，
  自动回退 `template` 照发；`prompt` 与 `template` 至少留一个（都没有按配置错误跳过该任务）
- **权限红线**：群目标必须同时在 `ALLOWED_GROUPS` 白名单里（与主聊天同一套 ACL），不在名单里的
  任务会被**停用并私聊管理员**，一条消息都发不出去；`/push` 系列命令同样只给 SUPERUSERS
- **防刷屏**（**弱上限**）：每个目标每日最多 `AGENT_PUSH_DAILY_CAP_PER_TARGET` 条
  （默认 1，`0` = 不限）。计数落 `data/push-state.json`，重启/多进程基本生效，
  但属于尽力而为的限流而非硬保证（停机窗口内被砍掉的投递不计入）；
  超出则跳过本次并照常推进下次触发时间；正文长度受 `AGENT_PUSH_MAX_CHARS`（默认 500）截断，
  该值同时用作 LLM 的输出上限
- **失败处理**：投递失败按指数退避顺延重试（`AGENT_PUSH_RETRY_DELAY` 起、
  `AGENT_PUSH_MAX_RETRY_DELAY` 封顶）；连续失败 `AGENT_PUSH_MAX_FAILURES` 次（默认 5）就**停用该任务
  并私聊管理员**，避免配置错误每 tick 刷屏、也避免无限重试打脸用户
- **管理命令**（管理员）：`/push` 查看任务与下次触发时间、`/push off <id>` 停用、
  `/push run <id>` 立即触发一次（调试用，不影响原计划）、`/push help`
  —— 改配置（cron/target/文案）仍需改 `config.yaml` 后重启；`/push off` 停用的任务不会被重启复活
- 关闭：`AGENT_PUSH_ENABLED=0`（整个面不注册，消息照常走普通聊天）；任务轮询间隔 `AGENT_PUSH_TICK`
  （默认 30 秒，最小 5）
- 成本：推送的每次生成都记入同一个 usage 账本（`/status`、`/usage` 可见），受同一套预算约束

