"""公共知识库的脱敏与防注入后置过滤。

公共库是全局共享的：任何一条入库内容都可能被注入到别人的 prompt 里，因此
蒸馏时不仅要「不显示这是谁的知识」，还要防止知识库变成注入传播通道。

两层防护：
1. 蒸馏 prompt 要求模型脱敏（见 distill.py）——这是第一层，但模型不可全信；
2. 本模块做确定性后置过滤——命中即丢弃/脱敏，可测试、不依赖模型。

已知边界（如实声明，勿过度声称）：人名/昵称的防护只覆盖两件事——
  a) 调用方显式传入的词表（``scrub_pii`` 的 ``extra_terms``，词表由
     ``distill.load_extra_terms`` 从环境变量/文件加载）；
  b) 「X 的 + 私人物件」句式启发（``_POSSESSION_RE``）。
未登录人名（不在词表、也不落在上述句式里的）仍有漏网风险，本模块不能
声称「保证无 PII」。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable

logger = logging.getLogger(__name__)


def _normalize_for_match(text: str) -> str:
    """注入/噪声匹配前的归一化：NFKC（全角→半角）、小写、去空白与常见标点。

    「忽 略之前」「ＩＧＮＯＲＥ　ＰＲＥＶＩＯＵＳ」这类写法不再绕过黑名单（M2）。
    """
    s = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch for ch in s if ch.isalnum())


# 指令性/角色扮演内容：知识库不得成为注入载体，命中即丢弃
_INJECTION_HINTS = (
    "忽略之前", "忽略上面", "忽略以上", "忽略所有", "不要理会之前",
    "ignore previous", "ignore all previous", "ignore the above", "disregard previous",
    "system prompt", "系统提示", "你现在是", "从现在开始你", "越狱", "开发者模式",
    "jailbreak", "roleplay as", "扮演",
)
# 黑名单本身也按同一口径归一化后再比较（M2）
_INJECTION_HINTS_NORM = tuple(_normalize_for_match(h) for h in _INJECTION_HINTS)

# 共现规则（M2）：同一条内容同时出现「指令类」与「作废类」词 → 判为注入。
# 单短语黑名单挡不住同义改写（如「先前的指示一律作废」），共现更稳；
# 代价是会误伤少量正常讨论（如「忽略大小写的规则」），对公共库而言可接受。
_INSTRUCTION_WORDS = ("指示", "指令", "要求", "规则", "prompt", "instruction")
_INVALIDATION_WORDS = ("作废", "无效", "忽略", "无视", "覆盖", "取代", "不再适用")

# 指向特定个人的主语：出现则整条丢弃（改写成客观陈述是模型的责任）
_PERSONAL_SUBJECTS = (
    "用户", "该用户", "这位用户", "某人", "本人", "对方",
    "他的", "她的", "他本人", "她的", "该同学", "这位朋友",
)

# 「X 的 + 私人物件」句式（H3）：X 为 1–3 个汉字或英文词（英文词要求至少两个
# 字母，避免「8G 的服务器」这类技术表述误伤），命中即整条丢弃——即使人名不在
# 词表里，「王小明的服务器」这类归属表述也不该进公共库。宁可误杀，不可漏放。
_POSSESSION_OBJECTS = (
    "服务器", "电脑", "手机", "邮箱", "账号", "密码", "地址", "住址",
    "老板", "老婆", "老公", "男友", "女友", "室友", "同学", "同事",
    "身份证", "钱包", "车牌", "银行卡", "工资", "病史", "情史",
)
_POSSESSION_RE = re.compile(
    r"(?:[\u4e00-\u9fff]{1,3}|[A-Za-z]{2,}[A-Za-z0-9_.-]*)\s*的\s*(?:"
    + "|".join(_POSSESSION_OBJECTS) + r")"
)

# 明显无沉淀价值的内容（L9）：归一化后仍很短且包含任一 hint → 丢弃
_NOISE_HINTS = ("哈哈哈哈", "草", "666", "收到", "在吗", "谢谢")
_NOISE_HINTS_NORM = tuple(_normalize_for_match(h) for h in _NOISE_HINTS)

# 日期形态豁免（H2）：YYYY[-/.]M[-/.]D 的分隔数字串几乎总是日期而非标识
_DATEISH_RE = re.compile(r"^\s*\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}\s*$")


def _mask_separated_digits(m: re.Match) -> str:
    """容忍分隔符的数字串命中后的裁决：日期豁免；数字个数 <7 不掩（如「3-5 人」）。"""
    s = m.group(0)
    if _DATEISH_RE.match(s) or sum(ch.isdigit() for ch in s) < 7:
        return s
    return "[数字已脱敏]"


# 个人身份标识：命中即替换为占位符（保留句子结构）。
# 顺序很重要（H2）：URL、邮箱必须先于数字——否则含数字的邮箱会被数字模式
# 抢先打碎，永远打不上邮箱标签。
_PII_PATTERNS: list[tuple[re.Pattern, str | Callable[[re.Match], str]]] = [
    (re.compile(r"(?:https?://|www\.)\S+"), "[链接已脱敏]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"), "[邮箱已脱敏]"),
    (re.compile(r"(?:群|房间)\s*号?\s*\d+"), "[群号已脱敏]"),
    # 容忍分隔符（空格/连字符/句点/间隔号）的数字串：138 0013 8000、138-0013-8000
    (
        re.compile(r"(?<![0-9])(?:[0-9][0-9\s\-.·．]?){6,11}[0-9](?![0-9])"),
        _mask_separated_digits,
    ),
    # 连续数字保持原行为：5–12 位仍掩（QQ 号/订单号等）
    (re.compile(r"[1-9]\d{4,11}"), "[数字已脱敏]"),
]


def scrub_pii(text: str, extra_terms: list[str] | None = None) -> str:
    """把个人身份标识替换成占位符。

    extra_terms：人名/昵称词表（H3），按长度降序整串替换为「[人名已脱敏]」，
    且先于正则执行——避免词表里带数字的昵称被数字模式抢先打碎。
    输入会先做 NFKC 归一化，全角数字/字母因此也能被掩码（H2）。
    """
    out = unicodedata.normalize("NFKC", text or "")
    terms = sorted({t.strip() for t in (extra_terms or []) if t and t.strip()}, key=len, reverse=True)
    for term in terms:
        out = out.replace(term, "[人名已脱敏]")
    for pat, repl in _PII_PATTERNS:
        out = pat.sub(repl, out)
    return out


def rejection_reason(text: str, min_len: int = 8) -> str | None:
    """返回不应入库的原因；可入库返回 None。

    min_len 只对正文要点有意义——标题很短是正常的（如「部署经验」），
    调用方对标题传 min_len=0。
    """
    s = (text or "").strip()
    if not s:
        return "empty"
    norm = _normalize_for_match(s)
    for hint in _INJECTION_HINTS_NORM:
        if hint and hint in norm:
            return f"injection-like content ({hint})"
    if any(w in norm for w in _INSTRUCTION_WORDS) and any(
        w in norm for w in _INVALIDATION_WORDS
    ):
        return "injection-like content (instruction-invalidation co-occurrence)"
    m = _POSSESSION_RE.search(s)
    if m:
        return f"personal possession mentioned ({m.group(0)})"
    for subj in _PERSONAL_SUBJECTS:
        if subj in s:
            return f"refers to a specific person ({subj})"
    if len(norm) <= 8 and any(h in norm for h in _NOISE_HINTS_NORM):
        return f"noise-like content ({s})"
    if len(s) < min_len:
        return "too short"
    return None


def sanitize_point(point: str, extra_terms: list[str] | None = None) -> tuple[str | None, str | None]:
    """清洗一个要点：返回 (清洗后的文本, 丢弃原因)。丢弃时文本为 None。"""
    reason = rejection_reason(point)
    if reason:
        return None, reason
    return scrub_pii(point, extra_terms).strip(), None


def sanitize_entry(entry: dict, extra_terms: list[str] | None = None) -> tuple[dict | None, list[str]]:
    """清洗一条知识条目 {"title","points"}；返回 (条目或 None, 丢弃原因列表)。"""
    dropped: list[str] = []
    if not isinstance(entry, dict):
        return None, ["not a dict"]

    title = scrub_pii(str(entry.get("title") or "").strip(), extra_terms)
    if title:
        title_reason = rejection_reason(title, min_len=0)  # 标题允许很短
        if title_reason:
            dropped.append(f"title: {title_reason}")
            title = ""

    points: list[str] = []
    raw_points = entry.get("points")
    if isinstance(raw_points, str):
        raw_points = [raw_points]
    for p in raw_points or []:
        cleaned, reason = sanitize_point(str(p), extra_terms)
        if cleaned:
            points.append(cleaned)
        else:
            dropped.append(reason or "unknown")
    if not points:
        return None, dropped or ["no usable points"]
    return {"title": title, "points": points}, dropped


def format_entry(entry: dict) -> str:
    """把条目渲染成入库的知识块文本。"""
    title = (entry.get("title") or "").strip()
    body = "\n".join(f"- {p}" for p in entry.get("points") or [])
    return f"{title}\n{body}".strip() if title else body
