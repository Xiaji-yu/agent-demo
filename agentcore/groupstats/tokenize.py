"""群聊文本分词：jieba 优先，缺失时降级内置 bigram。

只服务「群聊词云」：把一条群消息切成候选词，由采集器累加成**词频计数**落库
（``group_word_daily``）——**消息原文不进任何存储**（隐私口径见包 docstring）。

为什么不用 wordcloud 库的分词：它对中文按空白切（英文思维），必须外挂分词；
jieba 是纯 Python、无编译依赖，与仓库「依赖可降级」惯例一致（silk-python /
wikitextparser 同款）：import 失败时降级 bigram，功能变笨但不崩。

切分前的清洗（都发生在调用方给的 plain text 上）：
- URL / ``[CQ:...]`` 码 / ``@某人`` 一律剥掉——它们是消息里的"地址"不是"内容"
- 过滤规则：长度 ≥ 2、非停用词/通用词/英文虚词、至少含一个汉字或字母、
  非纯数字/符号；再按词性只留内容词（名词 n* / 动词 v* / 形容词 a* /
  英文 eng / 拟声·状态 o,z / 成语 i,l / 简称 j），丢掉副词、连词、代词、
  数词、介词等虚词（"笑死我了"这类被 jieba 切成单字的会整体落选，是可接受
  的质量损失）。
- **旧数据也过滤**：``filter_words`` 对已落库的词做同一套判断，词性规则调整
  后不必等 35 天保留期（读取时多取一些再筛）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from functools import lru_cache

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

if _JIEBA_READY:
    try:  # 词性标注与 jieba 同包；极端裁剪环境可能没有
        import jieba.posseg as pseg
    except Exception:  # pragma: no cover - 缺 posseg 时退化为纯分词
        pseg = None  # type: ignore[assignment]
        _POS_READY = False
        logger.warning("jieba.posseg 不可用，词云退化为纯分词（不按词性过滤）")
    else:
        _POS_READY = True
else:
    pseg = None  # type: ignore[assignment]
    _POS_READY = False

# 词性表重建窗口的降级告警只打一次（prewarm 期可能出现，属已知瞬态）
_POS_DEGRADE_WARNED = False

# jieba 默认切错、且群聊高频的词：补用户词典（add_word 可重复调用，幂等）。
# 必须带 tag：posseg 对未标词性的自定义词回 x（unknown），会被内容词过滤丢掉。
_USER_WORDS: dict[str, str] = {
    "合并转发": "nz",
    "拍了拍": "v",
    "笑死我了": "v",
    "谢谢老板": "nz",
    "人工智能": "n",
    "ChatGPT": "eng",
    "好用": "a",
}


def _register_user_words() -> None:
    """把默认切错的高频群聊词补进用户词典（add_word 可重复调用，幂等）。"""
    if not _JIEBA_READY or jieba is None:
        return
    for _w, _tag in _USER_WORDS.items():
        try:
            jieba.add_word(_w, tag=_tag)
        except Exception:  # pragma: no cover - 词典损坏等
            logger.debug("jieba.add_word(%r) failed", _w)
    # 词性缓存可能在本轮注册前已经算过同名 token；注册后清掉，避免旧标签粘住
    try:
        _word_pos_ok.cache_clear()
    except NameError:  # pragma: no cover - 模块尚未定义完
        pass


def prewarm() -> None:
    """把 jieba 词典加载挪出事件循环（M2，REVIEW-1e0f5ae..a0a6bb4）。

    首次 ``lcut`` / ``posseg.cut`` 承担词典与词性模型加载（实测 ~0.8s）——
    采集器 ``record`` 在事件循环内同步调用，启动后第一条群消息撞上就是一次
    心跳冻结。装配后在线程池里预热一次，之后是纯查表。jieba 自带初始化锁，
    预热与首条消息竞争时安全（最坏退化为旧行为的一次等待）。缺 jieba 时为空
    操作。

    ``initialize()`` 会**重建**词典、清掉 import 期注册的用户整词，所以
    预热后必须重新注册（幂等）。
    """
    if not _JIEBA_READY or jieba is None:
        return
    try:
        jieba.initialize()
        if _POS_READY and pseg is not None:
            # posseg 有独立词性模型，首次 cut 仍会加载；一并预热
            pseg.dt.initialize()
        _register_user_words()
        if _POS_READY and pseg is not None:
            list(pseg.cut("预热"))
    except Exception:  # pragma: no cover - 词典损坏等
        logger.warning("jieba 预热失败（首次切分将退化为就地初始化）", exc_info=True)


# 预清洗：URL / CQ 码 / @符号
_URL_RE = re.compile(r"[a-zA-Z]+://\S+|www\.\S+")
CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
# 只剥 "@" 符号本身：后面的名字是内容（群里打字 "@张三" 时），整段删会把
# 真实词一起丢掉（初版 {1,32} 连吃，test_url_cq_at_stripped 抓住）
_AT_RE = re.compile(r"@+")

# 常用中文停用词：**只收虚词/代词/连接词/客套语**；实义名词/动词/形容词
# 交给词性过滤，其中「东西/感觉/还有/喜欢」这类高频但无话题性的实词单独放
# GENERIC_WORDS（避免把「兄弟/服务器」这类真实话题词一起误伤）。
# 单字已被长度规则滤掉，这里只收长度 ≥2 的词；部署者可用
# AGENT_GROUP_STATS_STOPWORDS 追加。
STOPWORDS = frozenset(
    """
    我们 你们 他们 她们 它们 自己 大家 别人 人家 什么 怎么 怎样 为什么 难道
    这个 那个 这些 那些 这样 那样 如此 之类 等等 一下 一个 一些 一种 一样
    不是 没有 没什么 不能 可以 可能 应该 应当 需要 觉得 认为 知道 希望
    因为 所以 但是 可是 不过 然而 而且 并且 或者 还是 不但 不仅 除了 关于
    对于 根据 通过 按照 由于 为了 是否 如果 虽然 即使 尽管 无论 不管
    现在 当时 刚才 已经 曾经 正在 将要 马上 一直 总是 经常 常常 偶尔
    时候 今天 昨天 明天 后天 前天 早上 晚上 上午 下午 半夜 周末 星期 礼拜
    谢谢 感谢 辛苦 晚安 早安 再见 收到 好的 好吧 对啊 是的 行了 算了
    是不是 有没有 能不能 要不要 知不知道 怎么了 咋办 咋了
    有点儿 有些 什么的
    就是 直接 然后 真的 多少 这么 主要 之前 只是 其实 正常 这边
    几个 一口 一家
    """.split()
)

# 英文虚词：posseg 把英文 token 统一标成 eng，只能靠词表收掉功能词；
# 技术/品牌/型号（deepseek / ai / token / byd / H255）不在此列，继续保留。
_EN_STOPWORDS = frozenset(
    """
    a an the and or but if then than so as
    is are was were be been being am
    do does did have has had
    i me my we us our you your he him his she her it its
    they them their this that these those
    to of in on at for with from by about into over after before
    not no yes can could will would shall should may might must
    very too just only also even still already
    many much more most some any all both each few other such own same
    really quite rather pretty
    get got make made like want need know think see look come go use used using
    one two
    """.split()
)

# 词性过滤后仍会残留的「高频但无话题性」实词：它们是名词/动词/副词形，
# 但语义泛化，进词云只会挤掉真正的话题词。与 STOPWORDS 分开便于审计；
# 部署者仍可用 AGENT_GROUP_STATS_STOPWORDS 继续追加。
GENERIC_WORDS = frozenset(
    """
    东西 感觉 还有 喜欢
    不会 出来 回去 有点 确实 只能 回来 不了
    我要 看看 不到 只会
    """.split()
)

# 内容词词性（jieba/ICTCLAS 口径）：
#   n* 名词 / v* 动词 / a* 形容词（不含 ad 副形词）/ eng 英文 /
#   o 拟声词 / z 状态词（嘻嘻、哈哈大笑）/ i,l 成语 / j 简称
# 刻意不收：d 副词、ad 副形词、c 连词、r 代词、p 介词、m 数词、q 量词、
# f 方位词、t 时间词、u 助词、x 标点、b 区别词。
_POS_OK = frozenset(
    {
        "n",
        "nr",
        "nrfg",
        "nrt",
        "ns",
        "nt",
        "nz",
        "nl",
        "ng",
        "v",
        "vd",
        "vn",
        "vf",
        "vx",
        "vi",
        "vl",
        "vg",
        "a",
        "an",
        "ag",
        "al",
        "eng",
        "o",
        "z",
        "i",
        "l",
        "j",
    }
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
    """词级过滤：长度/停用词/通用词/英文虚词/构成。返回 False 的词不进词频。"""
    w = word.strip()
    if len(w) < 2 or len(w) > _MAX_WORD_CHARS:
        return False
    if w in STOPWORDS or w in extra_stopwords:
        return False
    if w in GENERIC_WORDS:
        return False
    # 全大写缩写（IT/US/CAN）是大写形态的技术词，不参与小写停用词匹配
    # （REVIEW M-4：`it/us/can` 在表内，但 IT 总线/US 市场是真实话题词）
    if w.lower() in _EN_STOPWORDS and not w.isupper():
        return False
    if not _HAS_TEXT_RE.search(w):
        return False  # 纯数字/符号/标点
    # 纯 ASCII 数字串（2024、520）不构成话题词；含字母的保留（OK、deepseek）
    if w.isascii() and w.isdigit():
        return False
    return True


def _pos_ok(flag: str) -> bool:
    """单个 jieba 词性是否属于内容词（组合标记取首段）。"""
    return flag.split("|", 1)[0] in _POS_OK


def _pos_table_ready() -> bool:
    """词性表是否就绪。

    `pseg.dt.initialize()` 重建 `word_tag_tab` 时**不持锁**（jieba 实现）：
    先置空再逐行填充 ~35 万行，窗口内并发 `pseg.cut` 会把所有词标 `x`。
    该窗口内按「降级为纯分词」处理（与缺 posseg 同口径），而不是把整条
    消息的候选词静默丢掉（REVIEW M-5，2026-10-26 评审）。
    """
    return (
        _POS_READY and pseg is not None and bool(getattr(pseg.dt, "word_tag_tab", None))
    )


@lru_cache(maxsize=8192)
def _word_pos_ok(word: str) -> bool:
    """已落库的单词再判一次词性（读取旧数据用）。

    单词脱离上下文后词性可能略有偏差（"显卡"会被标 v），但足以清掉
    "就是/然后/真的"这类虚词；分词器异常时保守放行，避免整张词云变空。
    """
    if not _POS_READY or pseg is None or not _pos_table_ready():
        return True
    try:
        return any(_pos_ok(p.flag) for p in pseg.cut(word))
    except Exception:  # pragma: no cover - 分词器异常不杀词
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
        if _POS_READY and pseg is not None and _pos_table_ready():
            return [
                item.word
                for item in pseg.cut(cleaned)
                if _acceptable(item.word, extra) and _pos_ok(item.flag)
            ]
        global _POS_DEGRADE_WARNED
        if _POS_READY and pseg is not None and not _POS_DEGRADE_WARNED:
            _POS_DEGRADE_WARNED = True
            logger.warning(
                "jieba.posseg 词性表未就绪（初始化重建窗口），词云临时降级为纯分词"
            )
        raw = jieba.lcut(cleaned)
    else:
        raw = _bigram_cut(cleaned)
    return [w for w in raw if _acceptable(w, extra)]


def filter_words(
    words: Iterable[str],
    extra_stopwords: frozenset[str] | set[str] = frozenset(),
    limit: int | None = None,
) -> list[str]:
    """过滤已落库的词（旧数据读取路径）：与 ``cut`` 同一套长度/停用词/词性口径。

    读取方应多取一些（例如 limit 的 6~8 倍）再调用本函数，避免虚词占满
    Top-N 后内容词不够。``limit`` 非正 → 空结果（仓库统一口径）。
    """
    if limit is not None and limit <= 0:
        return []
    extra = frozenset(extra_stopwords or ())
    out: list[str] = []
    for raw in words:
        w = str(raw).strip()
        if not _acceptable(w, extra):
            continue
        if _POS_READY and pseg is not None and not _word_pos_ok(w):
            continue
        out.append(w)
        if limit is not None and len(out) >= limit:
            break
    return out


# import 期注册：放在所有定义之后，保证 _word_pos_ok 已存在可清缓存；
# 不预热直接调用 cut 的调用方也拿到整词效果。
_register_user_words()
