"""帮助菜单的**单一事实源**：纯文本（admin._HELP_TEXT）与图片（help_render
的 _SECTIONS）都从这里生成。

为什么单独抽模块：历史上文本菜单与图片菜单各写一份，结果图片菜单漏过一整批
命令（/词云、/发言榜、/memory、/2fa…），用户问「词云怎么调用」而帮助里根本
没有——两处菜单漂移是必然的。现在只有这一份数据，两边的渲染都读它，并且
`tests/test_help_menu.py` 用 AST 扫全部 `on_command` 注册面，新命令不进菜单
直接红。

行格式 = (命令, 说明)。命令写用户实际可输入的形式。注意 NoneBot 的命令
匹配是 ``command_start + 命令名`` 的前缀 Trie（默认 ``command_start={"/"}``），
**所有命令都必须带 `/``——包括 `/词云`、`/发言榜`。曾为群统计挂过免前缀
on_message 入口（群里直接打「词云」），2026-10-26 按用户要求撤掉，命令面统一。
"""

from __future__ import annotations

FOOTER = "「管理员」命令需 SUPERUSERS · 图片渲染失败会自动退回文本帮助"

# 以紧凑写法整族列出的命令族：用户实际输入带前缀（如「skill info」），菜单为
# 可读性写成「/skill  skill catalog / info / install / list / uninstall」。
# tests/test_help_menu.py 的守卫按「前缀 + 子命令词都出现」判定覆盖，其余命令
# 仍逐名严格匹配——避免为满足字符串匹配把菜单写成 skillcatalog / skillinfo /
# … 这种难读形态。
COMPACT_FAMILIES: dict[str, tuple[str, ...]] = {
    "skill": ("catalog", "info", "install", "list", "uninstall"),
}

# (节标题, [(命令, 说明), ...])
SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "对话",
        (
            ("群聊", "@我 或以唤醒词开头；引用消息 + @我 可带图提问"),
            ("私聊", "直接发消息"),
        ),
    ),
    (
        "群聊统计",
        (
            ("/词云", "本群高频词云图（需开启 AGENT_GROUP_STATS）"),
            ("/发言榜", "群友发言榜；加「本周」出进行中的周"),
            ("群评", "直接说「点评我一下」，出个人周报卡"),
        ),
    ),
    (
        "常用",
        (
            ("/help", "本菜单（「帮助」/ /aihelp 同效）"),
            ("/reset", "重置当前会话"),
            ("/status", "运行状态与今日 LLM 用量"),
            ("/usage", "今日用量明细表"),
            ("/memory", "查看 / 删除自己的长期记忆"),
            ("/persona", "查看与切换人格"),
            ("/2fa", "web 写操作验证码（私聊，5 分钟有效）"),
        ),
    ),
    (
        "技能与知识库",
        (
            ("/skills", "查看可用技能"),
            ("/skill", "skill catalog / info / install / list / uninstall"),
            ("/kb", "search 语义检索（/kb help 看全部子命令）"),
        ),
    ),
    (
        "提醒与娱乐",
        (
            ("定时提醒", "直接说「每天 9 点提醒我喝水」"),
            ("点歌 / 放歌", "直接说「点歌 海阔天空」（需开启点歌）"),
        ),
    ),
    (
        "管理（管理员）",
        (
            ("/push", "管理定时内容推送"),
            ("/nudge", "管理主动联系"),
            ("/growth", "管理人格成长层"),
            ("/model", "切换模型"),
            ("/reboot", "重启 bot"),
        ),
    ),
)


def render_text() -> str:
    """纯文本菜单：图片关闭/渲染失败时的回落，也是测试锚点。"""
    lines: list[str] = ["指令菜单（所有命令都带 / 前缀）："]
    for head, rows in SECTIONS:
        lines.append(f"【{head}】")
        lines.extend(f"{cmd}  {desc}" for cmd, desc in rows)
    lines.append(FOOTER)
    return "\n".join(lines)
