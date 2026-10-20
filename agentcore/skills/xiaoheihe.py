"""小黑盒（xiaoheihe / heybox）帖子抓取 skill：分享链接 → 标题/作者/正文/图片。

与 ``fetch_url`` 的分工：``fetch_url`` 抓**网页 HTML** 交 trafilatura 抽正文；
小黑盒分享链接（``api.xiaoheihe.cn/v3/bbs/app/api/web/share?...``）实测 302 到
JS SPA 空壳（3.5KB，``trafilatura`` 无从抽取），必须走它 web/H5 端自用的**匿名
JSON 接口**（``/bbs/app/link/tree``，帖子本体在 ``result.link``）+ 本地复算的
hkey 签名（算法从官方 bundle 移植，无浏览器/JS 执行依赖）。

安全边界：
- link_id 用正则约束（``[0-9a-zA-Z]``，6~32 位），API 主机与路径**硬编码**——
  LLM 给出的任何文本都进不了 URL 结构，不存在 URL 注入面
- 连接前复用 ``web_fetch.url_rejection_reason`` 对 API 主机做 SSRF 校验
  （内网/回环/保留段一律拒绝；TOCTOU 残留与 web_fetch 相同，已在其 docstring 披露）
- 匿名接口、**无凭据**：请求不带 cookie/token，没有可泄漏的东西
- 帖子正文是**不可信外部内容**（典型注入载体），过 ``safety.fence_untrusted``
  围栏后才进 prompt
- 限时（15s）/ 限响应体（2MB）/ 限输出（6000 字）；日志只记 link_id 与结局，
  不记正文

使用自守：按用户请求单次抓取，不预热不轮询；接口风控（``status != ok``）时
把官方 msg 如实转告，不重试轰炸。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
import urllib.parse
from html.parser import HTMLParser

import httpx

from agentcore import tz
from agentcore.safety import fence_untrusted
from agentcore.skills.web_fetch import url_rejection_reason

logger = logging.getLogger(__name__)

_API_BASE = "https://api.xiaoheihe.cn"
_API_PATH = "/bbs/app/link/tree"
_WEB_LINK = "https://www.xiaoheihe.cn/app/bbs/link/"

_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_MAX_BYTES = 2 * 1024 * 1024
# 比 fetch_url 的 4000 宽：帖子是用户点名要的正文，长回复由出站分层投递兜底
_MAX_OUTPUT_CHARS = 6000

# hkey 签名字母表（官方 bundle 内常量，勿改）
_HKEY_ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
# 官方 bundle 的变体表 rr[3]="g" → St(path, t+1, nonce)：签名用 _time+1 秒
_HKEY_TIME_OFFSET = 1

# 公共参数（官方 web 端默认集；请求级参数可覆盖同名键）
_COMMON_PARAMS = {
    "os_type": "web",
    "app": "heybox",
    "x_client_type": "web",
    "x_os_type": "Mac",
    "x_app": "heybox",
    "x_client_version": "999.999.999",
}

_LINK_ID_QUERY_RE = re.compile(r"[?&]link_id=([0-9a-zA-Z]{6,32})")
_LINK_ID_PATH_RE = re.compile(r"/(?:app/bbs/)?link/([0-9a-zA-Z]{6,32})")
_LINK_ID_SHORT_RE = re.compile(r"/a/([0-9a-zA-Z]{6,32})")
_LINK_ID_BARE_RE = re.compile(r"^[0-9a-zA-Z]{6,32}$")


def _is_xhh_reference(raw: str) -> bool:
    """整串是否指向小黑盒：http(s) 看真实 host，``heybox://`` 深链看协议头。"""
    if raw.lower().startswith("heybox://"):
        return True
    try:
        host = (urllib.parse.urlsplit(raw).hostname or "").lower()
    except ValueError:
        return False
    return host == "xiaoheihe.cn" or host.endswith(".xiaoheihe.cn")


def extract_link_id(text: str) -> str | None:
    """从小黑盒分享链接 / 深链 / 裸 ID 中提取 link_id；不像则返回 None。

    支持的形态（2026-10 实测）：
    - ``https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?...&link_id=<id>&...``
    - ``https://www.xiaoheihe.cn/app/bbs/link/<id>``
    - ``https://www.xiaoheihe.cn/bbs/post_share?link_id=<id>``
    - ``heybox://v3/bbs/app/api/web/share?link_id=<id>``（App 深链）
    - 裸 ``<id>``（QQ 里经常只贴一截）
    """
    raw = (text or "").strip()
    if not raw:
        return None
    m = _LINK_ID_QUERY_RE.search(raw)
    if m:
        return m.group(1)
    # 路径形态只认小黑盒真实域名，避免误食他站 /link/ 路径或把域名藏在
    # query 值里的仿冒链接（host 判定走 urlsplit，不做子串匹配）
    if _is_xhh_reference(raw):
        for pat in (_LINK_ID_PATH_RE, _LINK_ID_SHORT_RE):
            m = pat.search(raw)
            if m:
                return m.group(1)
    if _LINK_ID_BARE_RE.match(raw):
        return raw
    return None


def _hkey(path: str, ts: int, nonce: str) -> str:
    """按小黑盒 web 端算法计算 hkey（官方 bundle ``St`` 的 Python 移植）。

    流程：路径规范化 → 三段字符按字母表映射 → 轮询交错取前 20 位 → MD5 →
    取 MD5 后 6 位 ASCII 码过混合矩阵求和取模 → 拼 MD5 前 5 位的映射串。
    """
    norm = "/" + "/".join(p for p in path.split("/") if p) + "/"
    s1 = _map_chars(str(ts), _HKEY_ALPHABET[:-2])
    s2 = _map_chars(norm, _HKEY_ALPHABET)
    s3 = _map_chars(nonce, _HKEY_ALPHABET)
    mixed = ""
    for i in range(max(len(s1), len(s2), len(s3))):
        for s in (s1, s2, s3):
            if i < len(s):
                mixed += s[i]
    md5hex = hashlib.md5(mixed[:20].encode()).hexdigest()
    codes = [ord(c) for c in md5hex[-6:]]
    total = sum(_mix_codes(codes)) % 100
    tail = _map_chars(md5hex[:5], _HKEY_ALPHABET[:-4])
    return f"{tail}{total:02d}"


def _map_chars(text: str, alphabet: str) -> str:
    """按字符码位从字母表取字（官方 ``Sb``/``Eb``）。"""
    return "".join(alphabet[ord(c) % len(alphabet)] for c in text)


def _mix_codes(codes: list[int]) -> list[int]:
    """官方 ``yF``：对前 4 个码位做 GF(2) 混合，其余原样（求和时含全部）。"""
    out = list(codes)

    def xtime(v: int) -> int:
        v &= 0xFF
        return ((v << 1) ^ 0x1B) & 0xFF if v & 0x80 else (v << 1) & 0xFF

    def qm(v: int) -> int:
        return xtime(v) ^ v

    def sm(v: int) -> int:
        return qm(xtime(v))

    def ym(v: int) -> int:
        return sm(qm(xtime(v)))

    def gm(v: int) -> int:
        return ym(v) ^ sm(v) ^ qm(v)

    out[0] = gm(codes[0]) ^ ym(codes[1]) ^ sm(codes[2]) ^ qm(codes[3])
    out[1] = qm(codes[0]) ^ gm(codes[1]) ^ ym(codes[2]) ^ sm(codes[3])
    out[2] = sm(codes[0]) ^ qm(codes[1]) ^ gm(codes[2]) ^ ym(codes[3])
    out[3] = ym(codes[0]) ^ sm(codes[1]) ^ qm(codes[2]) ^ gm(codes[3])
    return out


def _signed_params(path: str, ts: int | None = None) -> dict:
    """生成 hkey/_time/nonce/version 签名参数（官方 ``_F``）。

    ``ts`` 仅供测试注入（冻结时间戳锁偏移量行为）；生产走 ``time.time()``。
    """
    if ts is None:
        ts = int(time.time())
    seed = f"{ts}{int(time.time() * 1000)}{random.random()}{random.random()}"
    nonce = hashlib.md5(seed.encode()).hexdigest().upper()
    return {
        "hkey": _hkey(path, ts + _HKEY_TIME_OFFSET, nonce),
        "_time": str(ts),
        "nonce": nonce,
        "version": "999.0.4",
    }


class _HtmlTextExtractor(HTMLParser):
    """把帖子 html 块的编辑器 HTML 转成可读文本（保留图片，剥脚本样式）。"""

    _BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "li",
        "ul",
        "ol",
        "tr",
        "table",
        "thead",
        "tbody",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "section",
        "article",
        "figure",
        "figcaption",
        "hr",
    }
    _SKIP_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")
        elif tag == "img":
            attr = dict(attrs)
            src = attr.get("src") or attr.get("data-original")
            if src:
                self._parts.append(f"\n![图片]({src})\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _html_to_text(html: str) -> str:
    parser = _HtmlTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.exception("xiaoheihe html block parse failed")
        return ""
    return parser.text()


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _render_blocks(raw: object) -> str:
    """把 ``link.text``（JSON 块数组 / 纯文本）渲染成可读正文。

    块类型（实测）：``text`` 普通帖子正文、``html`` 长文正文、``img`` 图片
    （``url`` 为 CDN 直链）。
    """
    blocks: object = raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return ""
        try:
            blocks = json.loads(stripped)
        except ValueError:
            return _normalize_newlines(raw).strip()
    if not isinstance(blocks, list):
        return ""
    out: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                out.append(_normalize_newlines(text).strip())
        elif btype == "html":
            html = block.get("text")
            if isinstance(html, str) and html.strip():
                converted = _html_to_text(html)
                if converted:
                    out.append(converted)
        elif btype == "img":
            url = block.get("url")
            if isinstance(url, str) and url:
                out.append(f"![图片]({url})")
    return "\n\n".join(out)


def _format_post(link: dict, link_id: str) -> str:
    """把 API 返回的 link 字典组装成给模型看的文本（尚未围栏）。"""
    title = str(link.get("title") or "").strip() or "（无标题）"
    user = link.get("user") or {}
    author = (
        str(user.get("username") or "未知作者")
        if isinstance(user, dict)
        else "未知作者"
    )
    lines = [f"标题：{title}", f"作者：{author}"]
    created = link.get("create_at")
    if isinstance(created, int | float) and created > 0:
        # 时间显示走统一时区口径（AGENT_SCHEDULER_TZ，默认 Asia/Shanghai）
        stamp = tz.fmt_ts(created, "%Y-%m-%d %H:%M")
        lines.append(f"时间：{stamp}")
    stats = []
    for key, label in (
        ("click", "浏览"),
        ("favour_count", "点赞"),
        ("comment_num", "评论"),
    ):
        value = link.get(key)
        if isinstance(value, int):
            stats.append(f"{label} {value}")
    if stats:
        lines.append(" · ".join(stats))
    lines.append(f"链接：{_WEB_LINK}{link_id}")
    video = link.get("video")
    if isinstance(video, dict) and video.get("video_url"):
        lines.append(f"视频：{video['video_url']}")
    body = (
        _render_blocks(link.get("text")) or str(link.get("description") or "").strip()
    )
    head = "\n".join(lines)
    return f"{head}\n\n{body}" if body else head


async def fetch_post(link_id: str) -> tuple[dict | None, str]:
    """抓帖子本体，返回 ``(link 字典, 错误说明)``；成功时错误说明为空串。"""
    reason = await url_rejection_reason(_API_BASE + _API_PATH)
    if reason:
        logger.warning("xiaoheihe_post rejected: %s (%s)", link_id, reason)
        return None, f"拒绝抓取：{reason}"

    params = {
        **_COMMON_PARAMS,
        "link_id": link_id,
        "is_share": "1",
        "offset": "0",
        "limit": "0",  # 只取帖子本体，不要评论
        "use_concept_type": "",
        "x_app": "heybox_website",
        "web_version": "2.5",
        **_signed_params(_API_PATH),
    }
    body = b""
    encoding = "utf-8"
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=False
        ) as client:
            async with client.stream(
                "GET",
                _API_BASE + _API_PATH,
                params=params,
                headers={"User-Agent": "Mozilla/5.0 (agent-demo)"},
            ) as resp:
                if resp.status_code >= 400:
                    return None, f"抓取失败：HTTP {resp.status_code}"
                encoding = resp.encoding or "utf-8"
                size = 0
                chunks: list[bytes] = []
                async for chunk in resp.aiter_bytes(65536):
                    size += len(chunk)
                    if size > _MAX_BYTES:
                        return None, "抓取失败：响应超过 2MB 上限"
                    chunks.append(chunk)
                body = b"".join(chunks)
    except httpx.HTTPError as e:
        return None, f"抓取失败：{type(e).__name__}"

    try:
        data = json.loads(body.decode(encoding, errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None, "抓取失败：响应不是合法 JSON"
    if not isinstance(data, dict) or data.get("status") != "ok":
        status = data.get("status") if isinstance(data, dict) else None
        msg = data.get("msg") if isinstance(data, dict) else None
        logger.info(
            "xiaoheihe_post api failed: link_id=%s status=%s msg=%s",
            link_id,
            status,
            msg,
        )
        if status == "show_captcha":
            # 频次风控：明确告知等待，不要在 skill 内重试轰炸
            return None, "小黑盒当前触发了验证码风控（请求过于频繁），请稍后再试。"
        return None, f"小黑盒返回：{msg or '未知错误'}"
    link = (data.get("result") or {}).get("link")
    if not isinstance(link, dict):
        return None, "小黑盒返回异常：缺少帖子数据"
    return link, ""


async def fetch_post_text(url_or_id: str) -> str:
    """抓小黑盒帖子并返回**带不可信围栏**的正文，供模型直接阅读。"""
    link_id = extract_link_id(url_or_id)
    if not link_id:
        return "没找到小黑盒帖子 ID：请给出小黑盒分享链接（含 link_id）或帖子 ID。"
    link, err = await fetch_post(link_id)
    if link is None:
        return err
    text = _format_post(link, link_id)
    if len(text) > _MAX_OUTPUT_CHARS:
        text = (
            text[:_MAX_OUTPUT_CHARS] + f"\n…（内容过长，仅前 {_MAX_OUTPUT_CHARS} 字）"
        )
    return fence_untrusted("小黑盒帖子", text, "小黑盒社区抓取")


def register_xiaoheihe_skill(registry) -> None:
    @registry.register(
        "xiaoheihe_post",
        "抓取小黑盒（xiaoheihe.cn）帖子：标题、作者、时间、正文与图片链接。"
        "当用户给出小黑盒分享链接（api.xiaoheihe.cn/v3/bbs/app/api/web/share?...、"
        "www.xiaoheihe.cn/app/bbs/link/...、www.xiaoheihe.cn/bbs/post_share?link_id=...）"
        "或帖子 ID 时使用。fetch_url 抓不了这种链接（会跳到 JS 渲染的空壳页面），"
        "本技能走小黑盒官方匿名接口。仅支持公开帖子。",
        {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "小黑盒分享链接或帖子 ID（link_id）",
                }
            },
            "required": ["url"],
        },
        permission="public",
    )
    async def xiaoheihe_post_skill(url: str) -> str:
        """handler：纯只读，不需要会话身份（不声明 user_id/group_id 形参）。"""
        return await fetch_post_text(url)
