"""自定义唤醒词（AGENT_WAKE_WORDS）：加载、匹配与剥离。

触发判定（matcher.trigger_rule）与剥前缀（pipeline._build_user_text）共用同一实现，
保证「触发命中什么、对话内容就剥什么」；环境变量每条消息动态读取，运行期改值即时生效。
"""
import os


def load_wake_words() -> list[str]:
    raw = os.getenv("AGENT_WAKE_WORDS", "").strip()
    return [w.strip() for w in raw.split(",") if w.strip()] if raw else []


def match_wake_word(text: str) -> str | None:
    """返回 text 开头命中的最长唤醒词（大小写不敏感的前缀匹配）；未命中返回 None。"""
    lowered = text.lower()
    return max(
        (w for w in load_wake_words() if lowered.startswith(w.lower())),
        key=len,
        default=None,
    )


def strip_wake_word(text: str) -> str:
    """剥掉开头的唤醒词及其后空白；未命中原样返回。

    评审 REVIEW-bbd8913..f6dffcc.md 的 M9：@bot 段被适配器移除后，文本段常以空格
    开头（如 ``" 小助手 帮我查天气"``），``startswith`` 会失配、唤醒词原样进入 prompt。
    故先 ``lstrip()`` 再匹配。
    """
    if not text:
        return text
    stripped = text.lstrip()
    hit = match_wake_word(stripped)
    return stripped[len(hit) :].lstrip() if hit else text
