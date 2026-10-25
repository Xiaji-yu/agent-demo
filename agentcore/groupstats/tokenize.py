"""群聊文本分词：jieba 优先，缺失时降级内置 bigram。

只服务「群聊词云」：把一条群消息切成候选词，由采集器累加成**词频计数**落库
（``group_word_daily``）——**消息原文不进任何存储**（隐私口径见包 docstring）。

为什么不用 wordcloud 库的分词：它对中文按空白切（英文思维），必须外挂分词；
jieba 是纯 Python、无编译依赖，与仓库「依赖可降级」惯例一致（silk-python /
wikitextparser 同款）：import 失败时降级 bigram，功能变笨但不崩。

切分前的清洗（都发生在调用方给的 plain text 上）：
- URL / ``[CQ:...]`` 码 / ``@某人`` 一律剥掉——它们是消息里的"地址"不是"内容"
- 过滤规则：长度 ≥ 2、非停用词、至少含一个汉字或字母、非纯数字/符号
  （"笑死我了"这类被 jieba 切成单字的会整体落选，是可接受的质量损失）
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

try:  # 可选导入：缺失时降级 bigram（见 _bigram_cut）
    import jieba

    try:
        # 抑制首次 import 时的 "Building prefix dict ..." 刷屏
        jieba.setLogLevel(logging.WARNING)
    except Exception:  # pragma: no cover - 老版本没有 setLogLevel
        pass
    _JIEBA_READY = True
except Exception:  # pragma: no cover - 环境缺 jieba
    jieba = None  # type: ignore[assignment]
    _JIEBA_READY = False
    logger.warning("jieba 不可用，群聊词云降级为内置 bigram 分词（切分质量下降）")

# jieba 默认切错、且群聊高频的词：补用户词典（add_word 可重复调用，幂等）
_USER_WORDS = (
    "合并转发",
    "拍了拍",
    "笑死我了",
    "谢谢老板",
    "人工智能",
    "ChatGPT",
)


def _register_user_words() -> None:
    """把默认切错的高频群聊词补进用户词典（add_word 可重复调用，幂等）。"""
    if not _JIEBA_READY or jieba is None:
        return
    for _w in _USER_WORDS:
        try:
            jieba.add_word(_w)
        except Exception:  # pragma: no cover - 词典损坏等
            logger.debug("jieba.add_word(%r) failed", _w)


_register_user_words()  # import 期注册：不预热直接 cut 的调用方也拿到整词效果


def prewarm() -> None:
    """把 jieba 词典加载挪出事件循环（M2，REVIEW-1e0f5ae..a0a6bb4）。

    首次 ``lcut`` 承担词典加载（实测 ~0.8s）——采集器 ``record`` 在事件循环
    内同步调用，启动后第一条群消息撞上就是一次心跳冻结。装配后在线程池里
    预热一次，之后 ``lcut`` 是纯查表。jieba 自带初始化锁，预热与首条消息
    竞争时安全（最坏退化为旧行为的一次等待）。缺 jieba 时为空操作。

    ``initialize()`` 会**重建**词典、清掉 import 期注册的用户整词，所以
    预热后必须重新注册（幂等）。
    """
    if not _JIEBA_READY or jieba is None:
        return
    try:
        jieba.initialize()
        _register_user_words()
    except Exception:  # pragma: no cover - 词典损坏等
        logger.warning("jieba 预热失败（首次切分将退化为就地初始化）", exc_info=True)


# 预清洗：URL / CQ 码 / @符号
_URL_RE = re.compile(r"[a-zA-Z]+://\S+|www\.\S+")
CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
# 只剥 "@" 符号本身：后面的名字是内容（群里打字 "@张三" 时），整段删会把
# 真实词一起丢掉（初版 {1,32} 连吃，test_url_cq_at_stripped 抓住）
_AT_RE = re.compile(r"@+")

# 常用中文停用词：**只收虚词/代词/连接词/客套语**，不收实义名词——
# 「机器人」「东西」「兄弟」这类是群聊真实话题词（产品词云里就有），
# 收进来等于把用户想看的词滤掉（初版犯过，见 test_stopwords_filtered）。
# 单字已被长度规则滤掉，这里只收长度 ≥2 的词；部署者可用
# AGENT_GROUP_STATS_STOPWORDS 追加。
STOPWORDS = frozenset(
    """
    我们 你们 他们 她们 它们 自己 大家 别人 什么 怎么 怎样 为什么 难道
    这个 那个 这些 那些 这样 那样 如此 之类 等等 一下 一个 一些 一种 一样
    不是 没有 没什么 不能 可以 可能 应该 应当 需要 觉得 认为 知道 希望
    因为 所以 但是 可是 不过 然而 而且 并且 或者 还是 不但 不仅 除了 关于
    对于 根据 通过 按照 由于 为了 是否 如果 虽然 即使 尽管 无论 不管
    现在 当时 刚才 已经 曾经 正在 将要 马上 一直 总是 经常 常常 偶尔
    时候 今天 昨天 明天 后天 前天 早上 晚上 上午 下午 半夜 周末 星期 礼拜
    谢谢 感谢 辛苦 晚安 早安 再见 收到 好的 好吧 对啊 是的 行了 算了
    是不是 有没有 能不能 要不要 知不知道 怎么了 咋办 咋了
    有点儿 有些 什么的
    """.split()
)

# 至少含一个汉字或字母（拒绝纯数字/纯符号/纯标点）
_HAS_TEXT_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_MAX_WORD_CHARS = 24  # jieba 偶发超长 token（未剥净的地址残片），截断丢弃


def _preclean(text: str) -> str:
    """剥掉 CQ 码 / URL / @提及，压缩空白。

    顺序有讲究：**CQ 码必须最先剥**——URL 正则的 ``\\S+`` 会把 ``]`` 一起吃掉
    （``[CQ:image,url=http://x]`` → ``[CQ:image,url=``），之后再无完整 CQ 码
    可剥，残留的 "CQ/image/url" 会进词云（实测踩过）。
    """
    t = CQ_RE.sub(" ", text or "")
    t = _URL_RE.sub(" ", t)
    t = _AT_RE.sub(" ", t)
    return " ".join(t.split())


def _acceptable(word: str, extra_stopwords: frozenset[str] = frozenset()) -> bool:
    """词级过滤：长度/停用词/构成。返回 False 的词不进词频。"""
    w = word.strip()
    if len(w) < 2 or len(w) > _MAX_WORD_CHARS:
        return False
    if w in STOPWORDS or w in extra_stopwords:
        return False
    if not _HAS_TEXT_RE.search(w):
        return False  # 纯数字/符号/标点
    # 纯 ASCII 数字串（2024、520）不构成话题词；含字母的保留（OK、deepseek）
    if w.isascii() and w.isdigit():
        return False
    return True


def _bigram_cut(text: str) -> list[str]:
    """无 jieba 时的降级切分：连续汉字按二元滑窗 + ASCII 按单词。

    "服务器又炸了" -> 服务 务器 又炸 炸了；质量明显不如 jieba（无词典），
    但保证功能可用（与 silk 缺失时点歌不注册同款降级哲学）。
    """
    words: list[str] = []
    run: list[str] = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            run.append(ch)
            continue
        if run:
            words.extend(_cjk_bigrams(run))
            run = []
        if ch.isalnum():
            # ASCII 单词：连续字母数字视为一个候选（由 _acceptable 过滤长短）
            if (
                words
                and words[-1]
                and words[-1][-1].isascii()
                and words[-1][-1].isalnum()
            ):
                words[-1] = words[-1] + ch
            else:
                words.append(ch)
    if run:
        words.extend(_cjk_bigrams(run))
    return words


def _cjk_bigrams(run: list[str]) -> list[str]:
    if len(run) < 2:
        return []
    return ["".join(run[i : i + 2]) for i in range(len(run) - 1)]


def cut(
    text: str, extra_stopwords: frozenset[str] | set[str] = frozenset()
) -> list[str]:
    """把一条群消息切成词云候选词（已过滤）。空文本返回空列表。"""
    cleaned = _preclean(text)
    if not cleaned:
        return []
    extra = frozenset(extra_stopwords or ())
    if _JIEBA_READY and jieba is not None:
        raw = jieba.lcut(cleaned)
    else:
        raw = _bigram_cut(cleaned)
    return [w for w in raw if _acceptable(w, extra)]
