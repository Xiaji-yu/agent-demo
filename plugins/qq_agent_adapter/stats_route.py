"""群发言榜 / 词云：群命令 + 每周定时周报（NoneBot 接线层）。

数据与渲染全在 agentcore（``groupstats`` / ``render.rank`` / ``render.cloud``），
本文件只做平台相关的事：

- ``on_command("发言榜")`` / ``on_command("词云")``：priority=5 block=True，
  与 admin 的 /status /push 对齐；ACL 走 ``acl.is_allowed`` + ``acl.deny``
  统一出口（白名单群任何群友可看——数据本来就是这群人的发言）。
  **命令必须带 ``/`` 前缀**：NoneBot 的命令匹配是 ``command_start + 命令名``
  的前缀 Trie，默认 ``command_start={"/"}``——`/词云`、`/发言榜` 也不例外
  （曾挂过免前缀 on_message 入口，2026-10-26 按用户要求撤掉，命令面统一）
- 窗口选择：默认**上一个完整周**（与产品截图 10/5~10/11 同口径），
  「/发言榜 本周」出进行中的周
- per-group 冷却（默认 30s）：命令零成本（不打 LLM、不花预算），但图片
  消息会占屏幕，防连刷
- 图片经 ``base64://`` CQ:image 发送（与 /usage、/help 同款链路）
- 周报 cron：到点给每个「上周有数据且 ≥ 阈值」的白名单群连发榜图 + 词云图，
  走 ``Sink``（三态语义：结果不确定不重发，与提醒/推送同一出口）

30s 数据滞后：采集 flush 周期即榜单新鲜度上限（AGENT_GROUP_STATS_FLUSH_SECONDS），
命令帮助文案里注明。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment

from agentcore import groupstats
from agentcore.groupstats import ranking, review
from agentcore.groupstats import tokenize as gs_tokenize
from agentcore.groupstats.tokenize import cut
from agentcore.render.cloud import render_cloud_png
from agentcore.render.rank import RankRow, render_rank_png
from agentcore.render.review import render_review_png

from . import _get_driver
from .acl import ALLOWED_GROUPS, deny, is_allowed

logger = logging.getLogger(__name__)

# 命令侧数据门槛：不足 3 人或不足 10 条的群出了也是张废图，直接文案
_MIN_USERS = 3
_MIN_MSGS = 10

_THIS_WEEK_HINTS = ("本周", "这周", "current")

# per-group 命令冷却：{group_id 或 private:uid: monotonic 截止时间}
_cooldowns: dict[str, float] = {}


def _not_self_message(event: MessageEvent) -> bool:
    """排除 bot 自身消息（协议端上报自身消息时回传的事件）。

    与 matcher._is_self_message / admin._not_self_message 同一判据；三处各自
    实现是仓库既有形态（判据两行，不值得引入模块耦合）。
    """
    self_id = str(getattr(event, "self_id", "") or "")
    return not (self_id and str(event.get_user_id()) == self_id)


rank_cmd = on_command("发言榜", priority=5, block=True, rule=_not_self_message)
cloud_cmd = on_command("词云", priority=5, block=True, rule=_not_self_message)


def _plain_text(event: MessageEvent) -> str:
    """只取 text 段拼接（忽略 reply/at/image 等），用于「本周」参数识别。"""
    try:
        return "".join(
            str(seg.data.get("text") or "")
            for seg in event.get_message()
            if seg.type == "text"
        ).strip()
    except Exception:
        return str(event.get_message()).strip()


def _wants_current_week(event: MessageEvent) -> bool:
    text = _plain_text(event)
    return any(text.startswith(h) or f" {h}" in text for h in _THIS_WEEK_HINTS)


def _cooldown_key(event: MessageEvent) -> str:
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        return f"group:{group_id}"
    return f"private:{event.get_user_id()}"


def _cooling_down(event: MessageEvent) -> bool:
    seconds = groupstats.cooldown()
    if seconds <= 0:
        return False
    key = _cooldown_key(event)
    now = time.monotonic()
    until = _cooldowns.get(key, 0.0)
    if now < until:
        return True
    _cooldowns[key] = now + seconds
    # 顺手清理过期键，防长生命周期进程里字典无限增长
    if len(_cooldowns) > 512:
        for k in [k for k, v in _cooldowns.items() if v < now]:
            del _cooldowns[k]
    return False


def _store():
    return getattr(_get_driver(), "_agent_group_stats_store", None)


async def _rank_png(
    store,
    group_id: str,
    start,
    end,
    title: str,
    *,
    min_users: int = _MIN_USERS,
    min_msgs: int = _MIN_MSGS,
):
    """装配并渲染榜图。返回 (png 或 None, 拒绝原因或 None)。"""
    top_n = groupstats.top_n()
    rows = await store.user_ranking(group_id, start, end, top_n)
    if len(rows) < min_users:
        return None, "人数太少"
    total = await store.window_total(group_id, start, end)
    if total < min_msgs:
        return None, "条数太少"
    # 上一窗（用于新上榜/名次变化）：窗口起点往前推 7 天
    prev_start = start - timedelta(days=7)
    prev_rows = await store.user_ranking(
        group_id, prev_start, prev_start + timedelta(days=6), top_n
    )
    entries, _ = ranking.build_leaderboard(rows, prev_rows, top_n, total=total)
    names = await store.nicknames(group_id, start, end)
    hist = await store.hour_histogram(group_id, start, end)
    card_rows = [
        RankRow(
            rank=e.rank,
            nickname=names.get(e.user_id) or e.user_id,
            msgs=e.msgs,
            pct=e.pct,
            is_new=e.is_new,
            delta=e.delta,
        )
        for e in entries
    ]
    png = await asyncio.to_thread(
        render_rank_png,
        title,
        card_rows,
        total=total,
        peak_text=ranking.fmt_peak(ranking.peak_hour(hist)),
    )
    if png is None:
        return None, "渲染失败"
    return png, None


async def _cloud_png(store, group_id: str, start, end, title: str):
    # L6（REVIEW-1e0f5ae..a0a6bb4）：AGENT_GROUP_STATS_WORDS 必须真正生效——
    # 旧链路 config 允许 >80 但 render 默认恒截 80、调用又不传 max_words，
    # 配了等于没配。上限一并传给渲染层。
    limit = groupstats.words()
    # 多取再筛：旧数据里的虚词不能占满词云 Top-N（读取时不改库）
    raw = await store.word_freq(group_id, start, end, max(limit * 6, 180))
    counts = dict(raw)
    words = [
        (w, counts[w])
        for w in gs_tokenize.filter_words(
            counts, groupstats.extra_stopwords(), limit=limit
        )
    ]
    if not words:
        return None, "没有可统计的词"
    png = await asyncio.to_thread(render_cloud_png, title, words, max_words=limit)
    if png is None:
        return None, "渲染失败"
    return png, None


def _image_segment(png: bytes) -> MessageSegment:
    return MessageSegment.image(f"base64://{base64.b64encode(png).decode('ascii')}")


async def _respond(event: MessageEvent, matcher, kind: str) -> None:
    """命令主流程：ACL → 冷却 → 窗口 → 渲染 → 发图。"""
    if not is_allowed(event):
        await deny(matcher, event, "你没有权限使用这个功能。")
        return
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        await matcher.finish("发言榜/词云只在群里用（私聊没有群统计）。")
        return
    store = _store()
    if store is None or not groupstats.enabled():
        await matcher.finish("发言统计未开启（AGENT_GROUP_STATS=0）。")
        return
    if _cooling_down(event):
        await matcher.finish(f"榜图每 {groupstats.cooldown()}s 最多刷新一次。")
        return

    if _wants_current_week(event):
        start, end = ranking.current_week()
    else:
        start, end = ranking.previous_week()
    title = f"{'发言榜' if kind == 'rank' else '群聊词云'} | {ranking.fmt_range(start, end)}"

    if kind == "rank":
        png, why = await _rank_png(store, str(group_id), start, end, title)
    else:
        png, why = await _cloud_png(store, str(group_id), start, end, title)
    if png is None:
        await matcher.finish(f"这段时间的群数据还太少（{why}），再等等。")
        return
    await matcher.finish(_image_segment(png))


@rank_cmd.handle()
async def handle_rank(event: MessageEvent):
    await _respond(event, rank_cmd, "rank")


@cloud_cmd.handle()
async def handle_cloud(event: MessageEvent):
    await _respond(event, cloud_cmd, "cloud")


# ---------------------------------------------------------------- 周报


async def weekly_report() -> None:
    """cron 到点：给**周报推送白名单**内达标的群连发榜图 + 词云图。

    两道闸门（叠加关系）：
    1. ``AGENT_GROUP_STATS_WEEKLY_GROUPS`` 白名单——**空 = 一条都不推**
       （fail-closed：忘记配 = 静默不推，不会误发到不想收的群；只管推送，
       采集与群命令/skill 按 AGENT_GROUP_STATS_ALLOWED_GROUPS，空集回落 ALLOWED_GROUPS）
    2. 上周群友条数 ≥ AGENT_GROUP_STATS_MIN_MSGS（默认 20）——新群/死群
       不推空气周报

    白名单里的群不在 ALLOWED_GROUPS = 配置错误：跳过并 WARNING（store 只
    应有白名单群的数据，这里再按 ACL 过一次，纵深防御）。单群失败只记日志
    继续下一群（不重试刷屏，下周一自然重试）。不用 schedules 表：纯确定性
    任务，无 schema 依赖（与 push 的 LLM 生成内容是两回事）。
    """
    store = _store()
    if store is None or not groupstats.enabled():
        return
    sink = getattr(_get_driver(), "_agent_sink", None)
    if sink is None:
        logger.warning("group stats weekly: sink 未就绪，本周跳过")
        return
    weekly = groupstats.weekly_groups()
    if not weekly:
        logger.info(
            "group stats weekly: 未配置 AGENT_GROUP_STATS_WEEKLY_GROUPS，本周不推送"
        )
        return
    start, end = ranking.previous_week()
    min_msgs = groupstats.min_msgs()
    title = f"发言榜 | {ranking.fmt_range(start, end)}"
    try:
        totals = await store.group_totals(start, end)
    except Exception:
        logger.exception("group stats weekly: 读群合计失败，本周跳过")
        return
    for row in totals:
        gid = str(row.get("group_id") or "")
        if not gid:
            continue
        if gid not in weekly:
            continue
        # 纵深防御：store 只应有白名单群的数据，这里再按 ACL 白名单过一次
        if ALLOWED_GROUPS and gid not in ALLOWED_GROUPS:
            logger.warning(
                "group stats weekly: %s 在周报白名单但不在 ALLOWED_GROUPS，跳过", gid
            )
            continue
        if int(row.get("msgs") or 0) < min_msgs:
            continue
        try:
            rank_png, why = await _rank_png(
                store, gid, start, end, title, min_users=1, min_msgs=min_msgs
            )
            if rank_png is not None:
                state = await sink.send_once(f"group:{gid}", _image_segment(rank_png))
                logger.info("group stats weekly: rank → %s (%s)", gid, state)
            cloud_title = f"群聊词云 | {ranking.fmt_range(start, end)}"
            cloud_png, why = await _cloud_png(store, gid, start, end, cloud_title)
            if cloud_png is not None:
                state = await sink.send_once(f"group:{gid}", _image_segment(cloud_png))
                logger.info("group stats weekly: cloud → %s (%s)", gid, state)
            if rank_png is None and cloud_png is None:
                logger.info("group stats weekly: %s 跳过（%s）", gid, why)
        except Exception:
            logger.exception("group stats weekly: 群 %s 周报发送失败", gid)


# ---------------------------------------------------------------- 个人点评 skill

# per-user 冷却：{"群:用户": monotonic 截止时间}
_review_cooldowns: dict[str, float] = {}

# 落库快照的键（渲染卡片只需要这些；昵称只进 prompt 不落盘）
_SNAPSHOT_KEYS = (
    "msgs",
    "chars",
    "rank",
    "users",
    "days",
    "peak_text",
    "words",
    "own_words",
    "forwards",
    "images",
    "reply_count",
    "quote",
)
# 单次 review 从归档拉取的消息行数上限（切词/选原句的边界）
_ARCHIVE_TEXTS_LIMIT = 2000


def register_group_review_skill(registry, store, llm) -> None:
    """注册 ``my_group_review`` skill：LLM 判断用户意图后调用。

    **为什么住 plugins 层而不是 agentcore/skills**：skill 要发图片（Sink/
    MessageSegment），而 AGENTS 铁律禁止 agentcore 新增平台依赖（唯一既存
    豁免是 file_sender）。点歌 skill 已确立「plugins 层注册」的先例
    （``music_route.register_music_skill``），本项目照做。

    越权面归零：schema **没有 user_id 参数**，user_id 只由 registry 从当前
    会话注入——模型无法查看他人的评价。
    """

    @registry.register(
        "my_group_review",
        "查看/生成自己的群发言周点评（LLM 锐评，渲染成卡片图）。"
        "用户说『点评我一下』『我这周表现如何』『锐评我』『我的群周报』时调用。"
        "仅群聊可用；默认上一个完整周；上周发言不足 10 条不评。",
        {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": "可选：传『本周』看进行中的周；不传=上一个完整周",
                }
            },
            "required": [],
        },
        permission="public",
    )
    async def my_group_review_skill(
        window: str = "", user_id: str = "", group_id: str | None = None
    ) -> str:
        return await _handle_review(store, llm, window, user_id, group_id)


async def _handle_review(
    store, llm, window: str, user_id: str, group_id: str | None
) -> str:
    """个人点评主流程：冷却 → 门槛 → 缓存/生成 → 发卡/降级文本。"""
    if group_id is None:
        return "这是群功能：在群聊里 @ 我并说「点评我一下」，才能看你的群周报。"
    if store is None or not groupstats.enabled():
        return "发言统计未开启（AGENT_GROUP_STATS=0）。"
    gid, uid = str(group_id), str(user_id)

    # 冷却（per-user）：每次触发可能花一次 LLM 调用
    seconds = groupstats.review_cooldown()
    key = f"{gid}:{uid}"
    now = time.monotonic()
    if seconds > 0 and now < _review_cooldowns.get(key, 0.0):
        return f"点评每 {seconds}s 最多一次，稍后再来。"
    if seconds > 0:
        _review_cooldowns[key] = now + seconds
    if len(_review_cooldowns) > 512:  # 防长生命周期进程里字典无限增长
        for k in [k for k, v in _review_cooldowns.items() if v < now]:
            del _review_cooldowns[k]

    if str(window or "").startswith(("本周", "这周", "current")):
        start, end = ranking.current_week()
    else:
        start, end = ranking.previous_week()

    try:
        st = await store.user_window_stats(gid, uid, start, end)
        min_msgs = groupstats.review_min_msgs()
        if int(st.get("msgs") or 0) < min_msgs:
            return f"上周你在群里发言较少（{st.get('msgs') or 0} 条），咱不评价。"

        cached = await store.get_review(gid, start, uid)
        if cached is not None:
            return await _send_review_card(
                gid, start, end, cached.get("stats") or st, cached.get("text") or ""
            )

        # 未命中缓存：组装事实 → LLM 生成 → 落库 → 发卡
        names = await store.nicknames(gid, start, end)
        raw_words = await store.word_freq(gid, start, end, 60)
        words = gs_tokenize.filter_words(
            [w for w, _ in raw_words],
            groupstats.extra_stopwords(),
            limit=10,
        )
        hist = await store.hour_histogram(gid, start, end)
        facts = {
            "nickname": names.get(uid) or "",
            "msgs": st.get("msgs") or 0,
            "chars": st.get("chars") or 0,
            "rank": st.get("rank") or 0,
            "users": st.get("users") or 0,
            "days": st.get("days") or 0,
            "peak_text": ranking.fmt_peak(ranking.peak_hour(hist)),
            "words": words,
        }
        # 原文归档增强（5 项）：归档未开启/查询失败都静默退回基础事实
        archive_store = getattr(_get_driver(), "_agent_group_archive_store", None)
        if archive_store is not None:
            await _enrich_from_archive(archive_store, gid, uid, start, end, facts)
        comment = await review.generate(
            llm, facts, max_chars=groupstats.review_max_chars()
        )
        if comment:
            # 只缓存**成功**的评语：LLM 失败/预算熔断时不落盘，
            # 下次触发允许重试（熔断时闸门内快速返回，不烧钱）
            snapshot = {k: facts.get(k) for k in _SNAPSHOT_KEYS}
            await store.upsert_review(gid, start, uid, comment, snapshot)
        return await _send_review_card(gid, start, end, facts, comment or "")
    except Exception:
        logger.exception("group review: 个人点评流程失败")
        return "点评生成失败了，稍后再试。"


async def _enrich_from_archive(
    archive_store, gid: str, uid: str, start, end, facts: dict
) -> None:
    """从原文归档取 5 项增强事实，就地写进 facts。

    - ``own_words``：本人窗口内 text 重切（``cut()`` + 停用词，与词云同口径）
    - ``forwards``/``images``：按 kind 计数（只计次数，不碰转发内容）
    - ``reply_count``：本人 message_id 在窗口内被引次数
    - ``quote``：窗口内最长的一条自己的文字（tie-break 最早 ts——确定性）

    任何一步失败都只记日志并保留已取到的部分——增强是增益，不拖垮点评。
    """
    try:
        texts = await archive_store.user_texts(
            gid, uid, start, end, _ARCHIVE_TEXTS_LIMIT
        )
        if texts:
            counter: dict[str, int] = {}
            best = ""
            for row in texts:
                text = (row.get("text") or "").strip()
                if not text:
                    continue
                for w in cut(text, groupstats.extra_stopwords()):
                    counter[w] = counter.get(w, 0) + 1
                if len(text) >= len(best):  # ts 降序遍历：>= 留下最早的最长句
                    best = text
            if counter:
                facts["own_words"] = [
                    w
                    for w, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[
                        :8
                    ]
                ]
            if best:
                facts["quote"] = best
            ids = [
                str(row.get("message_id") or "")
                for row in texts
                if row.get("message_id")
            ]
            facts["reply_count"] = await archive_store.reply_count(gid, ids, start, end)
        facts["forwards"] = await archive_store.kind_count(
            gid, uid, start, end, groupstats.KIND_FORWARD
        )
        facts["images"] = await archive_store.kind_count(
            gid, uid, start, end, groupstats.KIND_IMAGE
        )
    except Exception:
        logger.exception("group review: 归档增强失败，退回基础事实")


async def _send_review_card(
    group_id: str, start, end, facts: dict, comment: str
) -> str:
    """渲染并投递个人周报卡；任何一步失败都降级为纯文本。"""
    title = f"我的群周报 | {ranking.fmt_range(start, end)}"
    # 有本人高频词时优先展示它（比群话题词更个人化）；否则退回群话题背景
    own = list(facts.get("own_words") or [])
    words = own or list(facts.get("words") or [])
    png = await asyncio.to_thread(
        render_review_png,
        title,
        facts,
        comment,
        words=words,
        words_label="你的高频词" if own else "群话题",
    )
    if png is None:
        return _review_text(start, end, facts, comment)
    sink = getattr(_get_driver(), "_agent_sink", None)
    if sink is None:
        return _review_text(start, end, facts, comment)
    try:
        state = await sink.send_once(f"group:{group_id}", _image_segment(png))
    except Exception:
        logger.exception("group review: 卡片发送异常")
        return _review_text(start, end, facts, comment)
    if state == "sent":
        return "已经做好你的群周报卡片了，看图～"
    if state == "uncertain":
        # 结果不确定：不重发、也不再发一份文本（那会造成内容重复投递）
        return "卡片已发送（投递状态不确定，没收到就稍后再试一次）。"
    return _review_text(start, end, facts, comment)


def _review_text(start, end, facts: dict, comment: str) -> str:
    """纯文本降级：卡片渲染失败/发送失败时保证用户拿到内容。"""
    extras = []
    if facts.get("forwards") is not None:
        extras.append(f"转发 {facts['forwards']} 次")
    if facts.get("images") is not None:
        extras.append(f"发图 {facts['images']} 条")
    if facts.get("reply_count") is not None:
        extras.append(f"被引用 {facts['reply_count']} 次")
    lines = [
        f"你的群周报（{ranking.fmt_range(start, end)}）：",
        f"{facts.get('msgs') or 0} 条 · {facts.get('chars') or 0} 字 · "
        f"第 {facts.get('rank') or 0}/{facts.get('users') or 0} 名 · "
        f"活跃 {facts.get('days') or 0} 天 · 最热 {facts.get('peak_text') or '数据不足'}"
        + (f" · {' · '.join(extras)}" if extras else ""),
    ]
    if comment:
        lines.append(f"锐评：{comment}")
    return "\n".join(lines)
