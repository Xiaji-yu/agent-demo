from __future__ import annotations

import asyncio
import copy
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from agentcore.boolenv import env_flag
from agentcore.budget import record_chat_usage, record_embedding_usage
from agentcore.llm import providers as provider_store
from agentcore.llm.protocols import get_protocol

logger = logging.getLogger(__name__)

# 主模型的瞬时故障重试（评审 C1）：一次抖动（超时/限流/网关 5xx）此前直接
# 降级到备用线路或失败给用户。只重试 1 次、退避 0.5s——再多就会叠加请求
# 超时把单轮回复拖爆（用户在 QQ 上等的不是重试风暴）。确定性错误（401/400）
# 不重试，重试只会白等同样的结果。
_PRIMARY_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.5
# 408 请求超时 / 429 限流 / 5x0 网关与服务器侧错误：重试有意义
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
# 切模型前的探测请求：只要"通不通、认不认这个型号"，16 token 足够
_PROBE_MAX_TOKENS = 16


def _env_nonneg_float(name: str, default: float) -> float:
    """读一个**非负**浮点 env（0 合法）；脏值/负数告警后回退默认（不崩启动）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，回退 %.0f", name, raw, default)
        return default
    if value < 0:
        logger.warning("%s=%r 为负，回退 %.0f", name, raw, default)
        return default
    return value


def _env_flag(name: str, default: bool) -> bool:
    """读一个布尔 env；认不出的值告警一次后取默认（L7，REVIEW-ee39c8e..e55cab5）。"""
    return env_flag(name, os.getenv(name), default)


# ==========================================================================
#  候选模型池（LLM_MODELS）与活跃位（LLM_MODEL）
#
#  条目语法：``模型[@base_url[|api_key]]``——三段都可选，空 base_url/key 表示
#  "用主端点/主 key"。逗号是条目分隔符，因此模型名里不能出现逗号。
#
#  为什么 key 允许写在条目里：.env 本来就是本项目的凭据载体（LLM_API_KEY 就在
#  里），多供应商池需要各自的门票；但 ``LLM_MODEL``（活跃位）**绝不携带 key**——
#  它会被 web 写面/审计日志/masked 展示，key 只属于池条目。
#
#  ``#`` 与引号一律拒绝：.env 是行格式，值里出现它们会改变 dotenv 的解析边界
#  （与 config_write._FORBIDDEN_CHARS 同一套口径）。
# ==========================================================================
_SPEC_BREAK_CHARS = ("\r", "\n", "#", '"', "'")
_MODEL_NAME_BREAK_CHARS = (",", " ", "\t", "@", "|")


@dataclass(frozen=True, repr=False)
class ModelEntry:
    """一个候选模型。``base_url`` / ``api_key`` 为空 = 用主端点/主 key。"""

    model: str
    base_url: str = ""
    api_key: str = ""

    def __repr__(self) -> str:
        # 绝不把 api_key 带进 repr：条目会被塞进列表、日志与异常消息
        if self.base_url:
            return f"ModelEntry({self.model}@{self.base_url})"
        return f"ModelEntry({self.model})"


def parse_model_spec(text: str, *, allow_key: bool = True) -> ModelEntry:
    """解析一条 ``模型[@base_url[|api_key]]``；不合法抛 ValueError。

    消息面向管理员（直接进 QQ 回复 / HTTP 422），所以用中文说清哪一段错了。
    ``allow_key=False`` 用于活跃位（``LLM_MODEL``）：那里不允许出现 key。
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("条目为空")
    if any(c in s for c in _SPEC_BREAK_CHARS):
        raise ValueError("含换行/引号/# 等行注入字符")
    key = ""
    if "|" in s:
        if not allow_key:
            raise ValueError("活跃位不携带 key（key 只写在 LLM_MODELS 的池条目里）")
        s, key = (part.strip() for part in s.split("|", 1))
        if not key:
            raise ValueError("| 后面是空的 key")
    base_url = ""
    if "@" in s:
        # rsplit：URL 的 userinfo 段也可能含 @（http://user@host），从右边切
        s, base_url = (part.strip() for part in s.rsplit("@", 1))
        base_url = base_url.rstrip("/")
        if not base_url:
            raise ValueError("@ 后面是空的 base_url")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
    model = s
    if not model:
        raise ValueError("模型名为空")
    if any(c in model for c in _MODEL_NAME_BREAK_CHARS):
        raise ValueError("模型名含非法字符（逗号/空白/@/|）")
    return ModelEntry(model=model, base_url=base_url, api_key=key)


def canonical_spec(entry: ModelEntry) -> str:
    """落盘用规范文本：**绝不含 api_key**（活跃位只记模型与端点）。"""
    return f"{entry.model}@{entry.base_url}" if entry.base_url else entry.model


def entry_display(entry: ModelEntry, main_base_url: str = "") -> str:
    """给人看的条目形态：与主端点相同的条目只显示模型名（不泄露内网 Host）。"""
    base = (entry.base_url or "").rstrip("/")
    main = (main_base_url or "").rstrip("/")
    if base and base != main:
        host = urlsplit(base).netloc or base
        return f"{entry.model}@{host}"
    return entry.model


def _parse_pool(text: str) -> tuple[ModelEntry, ...]:
    """解析 ``LLM_MODELS``。**绝不抛**：本函数在 import 期跑（模块级 _CFG），
    一个坏条目不能让 bot 起不来——告警后丢弃该条目（与 _LLMCfg 数值兜底同纪律）。
    """
    out: list[ModelEntry] = []
    for raw in (text or "").split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            out.append(parse_model_spec(item))
        except ValueError as e:
            logger.warning("LLM_MODELS 条目 %r 不合法，已忽略：%s", item[:80], e)
    return tuple(out)


def _match_pool(spec: ModelEntry, models: tuple[ModelEntry, ...]) -> ModelEntry:
    """把活跃位对回池：命中就复用池条目（继承它的端点与 key）。"""
    for e in models:
        if e.model == spec.model and e.base_url == spec.base_url:
            return e
    if not spec.base_url:
        for e in models:
            if e.model == spec.model:
                return e
    return spec


def _apply_entry(cfg: Any, entry: ModelEntry) -> Any:
    """在配置副本上套用条目：空 base_url/key 回落主端点/主 key。"""
    out = copy.copy(cfg)
    out.model = entry.model
    if entry.base_url:
        out.base_url = entry.base_url.rstrip("/")
    if entry.api_key:
        out.api_key = entry.api_key
    return out


class _LLMCfg:
    def __init__(self):
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.stepfun.com/v1").rstrip(
            "/"
        )
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "step-1-flash")
        # 候选池（逗号分隔）。空 = 单模型部署：只有活跃位 + 独立备用线
        self.models = _parse_pool(os.getenv("LLM_MODELS", ""))
        # 活跃位：LLM_MODEL 可写「模型[@base_url]」形态；空值/脏值回落池首，
        # 池也空则回落内置默认。**不抛异常**（本类是模块级单例，import 即构造）
        raw_model = (os.getenv("LLM_MODEL", "") or "").strip()
        active: ModelEntry
        if raw_model:
            try:
                active = parse_model_spec(raw_model, allow_key=False)
            except ValueError as e:
                logger.warning(
                    "LLM_MODEL=%r 不合法，回落池首/默认：%s", raw_model[:80], e
                )
                active = self.models[0] if self.models else ModelEntry("step-1-flash")
        else:
            active = self.models[0] if self.models else ModelEntry("step-1-flash")
        self.active = _match_pool(active, self.models)
        # model 与活跃位同源：payload 用的是 cfg.model
        self.model = self.active.model
        # embedding 模型名在 import 期快照。旧实现每次调用都读
        # os.getenv("LLM_EMBEDDING_MODEL", cfg.model)——运行态切主模型会把它
        # 一起带走（切成非 embedding 模型 → 语义检索静默退化），这里断开耦合
        self.embedding_model = (os.getenv("LLM_EMBEDDING_MODEL") or "").strip() or (
            self.model
        )
        # 空值/脏值回落默认而不是抛：本类是模块级单例（见文件末尾 _CFG），
        # 裸 int()/float() 遇到 "LLM_MAX_TOKENS="（.env 里留空是常见形态）
        # 会让 import 即 ValueError → bot 根本起不来（评审 M6，与 embedding/
        # pipeline 预算解析的 try/except 兜底同一模式）
        try:
            self.temperature = float(os.getenv("LLM_TEMPERATURE", "0.7"))
        except ValueError:
            self.temperature = 0.7
        try:
            self.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "1024"))
        except ValueError:
            self.max_tokens = 1024
        self.fallback_base_url = os.getenv("LLM_FALLBACK_BASE_URL", "").rstrip("/")
        self.fallback_api_key = os.getenv("LLM_FALLBACK_API_KEY", "")
        self.fallback_model = os.getenv("LLM_FALLBACK_MODEL", "")


_CFG = _LLMCfg()


class LLMClient:
    """直接从 .env 读取 LLM 配置，支持候选池内的自动降级与运行态切主。

    候选顺序（``_chain_entries``）：活跃位 → 刚被换下的旧主 → 池内其余 →
    ``LLM_FALLBACK_*`` 独立线。只有活跃位值得重试（后续候选本身就是"换个模型
    再试"，再叠一次重试会让 3 个池成员最坏打出 6+ 个请求）。

    主备切换是**逐请求无状态**的（每次从活跃位开始试，失败才往后走），所以
    "当前在用哪个"要由本类自己记（``_using_fallback`` + ``_last_served``）：
    它只用于识别**恢复**这一边沿（活跃位再次成功时通知一次），降级通知则按
    类型冷却周期发送。

    运行态切主（``set_active``）只改进程内状态，**不落盘**；持久化由插件层
    写 ``.env`` 的 ``LLM_MODEL``（重启后由 ``_CFG.active`` 重新装配，两者
    必须写同一个值，否则重启后模型会变）。
    """

    def __init__(self, clock=time.monotonic):
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        # 主备切换/恢复回调（由宿主注入，如私聊推送主人；agentcore 不认识 bot，
        # 与 embedding.on_error 同一注入模式）。事件是纯 dict（JSON 安全），
        # **不含** api_key/base_url 等配置秘密。
        self.on_fallback: Callable[[dict], Awaitable[None]] | None = None
        self._notify_enabled = _env_flag("LLM_FALLBACK_NOTIFY", True)
        # 同类事件的重复通知冷却（秒）：持续故障时每这么久最多提醒一次"还在
        # 降级"（调大到 86400 就近似"每次故障只提醒一次"）。
        self._notify_cooldown = _env_nonneg_float(
            "LLM_FALLBACK_NOTIFY_COOLDOWN", 1800.0
        )
        self._clock = clock
        # 按事件类型分别记冷却：切到备用后**恢复**通知要能立刻到主人（不能被子
        # 切换的冷却压掉），同时抖动时同一类型最多每冷却期一条。
        # 哨兵用「键不存在」而不是 0.0：time.monotonic() 是开机秒数，刚重启的
        # 机器 now < cooldown 会让 now-0.0 < cooldown 恒成立 → 吞掉首次通知
        # （AGENTS.md §5 的坑，embedding 侧同款修复）。
        self._last_notify: dict[str, float] = {}
        self._using_fallback = False
        # 运行态活跃位覆盖：None = 用 _CFG.active（.env 装配值）
        self._active: ModelEntry | None = None
        # 最近一次被换下的活跃位——降级时排第一个（"旧主优先"）
        self._previous: ModelEntry | None = None
        # 最近一次真正成功的候选：model_status 报"实际在服务的型号"用
        self._last_served: ModelEntry | None = None
        # 切模型前的探测超时（秒）；<=0 = 用共享连接池的默认超时
        self._probe_timeout = _env_nonneg_float("AGENT_MODEL_PROBE_TIMEOUT", 20.0)
        # 多 provider（独立 JSON 文件）。文件不存在 = 纯 env 旧路径（兼容）。
        self._provider_cfg = None
        self._provider_active: str | None = None
        self._provider_previous: str | None = None
        self._last_served_provider = None
        self.reload_providers()

    # ---- 候选池 / 活跃位 -------------------------------------------------

    # ---- 多 provider（独立 JSON 文件） ----------------------------------

    def reload_providers(self) -> None:
        """重新读 agent.json 里的 provider 配置；无数组/损坏时回落 env 旧路径（不崩）。"""
        try:
            cfg = provider_store.load()
        except FileNotFoundError:
            self._provider_cfg = None
            self._provider_active = None
            return
        except Exception:
            logger.warning("LLM providers 读取失败，回落 env 旧配置", exc_info=True)
            self._provider_cfg = None
            self._provider_active = None
            return
        self._provider_cfg = cfg
        # 文件是权威值：web 改了 set_active 之后 reload 必须跟上，不能保留
        # 进程里旧的 active（否则页面显示"已设为主"、实际还在打旧 provider）
        self._provider_active = cfg.active
        self._provider_previous = None

    def has_providers(self) -> bool:
        return bool(self._provider_cfg and self._provider_cfg.providers)

    def provider_config(self):
        return self._provider_cfg

    def provider_chain(self) -> list:
        """active → 刚换下的旧主 → 其余 enabled（数组序）。"""
        cfg = self._provider_cfg
        if cfg is None:
            return []
        by = {p.id: p for p in cfg.providers}
        out = []
        seen: set[str] = set()

        def push(pid: str | None) -> None:
            p = by.get(pid or "")
            if p is None or not p.enabled or p.id in seen:
                return
            seen.add(p.id)
            out.append(p)

        push(self._provider_active or cfg.active)
        push(self._provider_previous)
        for p in cfg.providers:
            push(p.id)
        return out

    def set_active_provider(self, pid: str) -> None:
        cfg = self._provider_cfg
        if cfg is None:
            raise ValueError("provider_not_found")
        p = cfg.by_id(pid)
        if p is None:
            raise ValueError("provider_not_found")
        current = self._provider_active or cfg.active
        if current != pid:
            self._provider_previous = current
        self._provider_active = pid
        cfg.active = pid
        self._using_fallback = False
        self._last_served_provider = None

    def reset_active_provider(self) -> str:
        cfg = self._provider_cfg
        if cfg is None or not cfg.providers:
            raise ValueError("provider_not_found")
        target = next((p.id for p in cfg.providers if p.enabled), cfg.providers[0].id)
        self._provider_previous = None
        self._provider_active = target
        cfg.active = target
        self._using_fallback = False
        self._last_served_provider = None
        return target

    def provider_status(self) -> dict:
        cfg = self._provider_cfg
        if cfg is None:
            return {"active_id": None, "providers": []}
        return {
            "active_id": self._provider_active or cfg.active,
            "providers": [p.admin_view() for p in cfg.providers],
        }

    async def probe_provider(self, provider, timeout: float | None = None) -> None:
        """按 provider 的协议发一次最小请求（切/测连接前的闸门）。"""
        proto = get_protocol(provider.protocol)
        url, headers, body = proto.probe_body(
            provider, default_max_tokens=_CFG.max_tokens
        )
        seconds = self._probe_timeout if timeout is None else timeout
        if seconds and seconds > 0:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(seconds, connect=min(10.0, seconds))
            ) as client:
                data = await self._post_raw(url, headers, body, client=client)
        else:
            data = await self._post_raw(url, headers, body)
        # 探测是真花钱的请求：usage 照实上报（L11）
        try:
            record_chat_usage(proto.parse(data).get("usage"), model=provider.model)
        except Exception:
            logger.debug("probe usage parse failed", exc_info=True)

    async def _chat_providers(self, providers, messages, tools, max_tokens) -> dict:
        entered_degraded = self._using_fallback
        last_err: Exception | None = None
        for idx, provider in enumerate(providers):
            proto = get_protocol(provider.protocol)
            attempts = _PRIMARY_ATTEMPTS if idx == 0 else 1
            data = None
            for attempt in range(attempts):
                try:
                    url, headers, body = proto.build(
                        provider,
                        messages,
                        tools,
                        max_tokens,
                        default_temperature=_CFG.temperature,
                        default_max_tokens=_CFG.max_tokens,
                    )
                    data = proto.parse(await self._post_raw(url, headers, body))
                    break
                except Exception as e:
                    last_err = e
                    if attempt + 1 >= attempts or not self._is_retryable(e):
                        break
                    logger.warning(
                        "LLM %s transient failure (%s: %s), retrying",
                        provider.model,
                        type(e).__name__,
                        e,
                    )
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
            if data is not None:
                record_chat_usage(data.get("usage"), model=provider.model)
                self._last_served = ModelEntry(provider.model, provider.base_url)
                self._last_served_provider = provider
                if idx > 0:
                    self._using_fallback = True
                    await self._notify_fallback(
                        "switched",
                        to_model=provider.model,
                        error=type(last_err).__name__ if last_err else "",
                    )
                elif entered_degraded and self._using_fallback:
                    self._using_fallback = False
                    await self._notify_fallback("recovered")
                return data
            logger.warning(
                "LLM provider failed (%s: %s), id=%s model=%s",
                type(last_err).__name__,
                last_err,
                provider.id,
                provider.model,
            )
        assert last_err is not None
        raise last_err

    def default_entry(self) -> ModelEntry:
        """.env 装配出的活跃位（_CFG 在 import 期固化，重启才会变）。"""
        cfg = _CFG
        entry = getattr(cfg, "active", None)
        return entry if entry is not None else ModelEntry(cfg.model)

    def active_entry(self) -> ModelEntry:
        """当前生效的活跃位（运行态覆盖优先）。"""
        return self._active if self._active is not None else self.default_entry()

    def pool_entries(self) -> tuple[ModelEntry, ...]:
        """候选池（``LLM_MODELS``）。空 = 单模型部署。"""
        return tuple(getattr(_CFG, "models", ()) or ())

    def resolve_entry(self, entry: ModelEntry) -> ModelEntry:
        """裸模型名对回池条目（继承它登记的端点/key）；显式条目原样返回。"""
        return _match_pool(entry, self.pool_entries())

    def legacy_fallback_entry(self) -> ModelEntry | None:
        """``LLM_FALLBACK_*`` 独立线（主端点之外的最后兜底）；未配返回 None。"""
        cfg = _CFG
        if cfg.fallback_base_url and cfg.fallback_api_key and cfg.fallback_model:
            return ModelEntry(
                cfg.fallback_model, cfg.fallback_base_url, cfg.fallback_api_key
            )
        return None

    def chain_entries(self) -> list[ModelEntry]:
        """本次请求的候选顺序（含独立备用线），去重。

        旧主排最前：刚被换下的那个模型是"最可能还想用"的，池内其余按书写序。
        """
        cfg = _CFG
        active = self.active_entry()
        out = [active]
        seen = {(active.model, active.base_url)}

        def _push(entry: ModelEntry) -> None:
            key = (entry.model, entry.base_url)
            if key in seen:
                return
            seen.add(key)
            out.append(entry)

        if self._previous is not None:
            _push(self._previous)
        for entry in self.pool_entries():
            _push(entry)
        if cfg.fallback_base_url and cfg.fallback_api_key and cfg.fallback_model:
            _push(
                ModelEntry(
                    cfg.fallback_model, cfg.fallback_base_url, cfg.fallback_api_key
                )
            )
        return out

    def _candidates(self) -> list:
        """候选顺序对应的配置副本（逐个套用条目，空字段回落主端点/主 key）。"""
        cfg = _CFG
        return [_apply_entry(cfg, e) for e in self.chain_entries()]

    def set_active(self, entry: ModelEntry) -> None:
        """运行态切换活跃位（不落盘；持久化由插件层写 .env）。

        传入裸模型名时先对回池条目：``LLM_MODEL=b`` 必须继承池里 ``b`` 登记的
        端点与 key，否则会拿主端点的门票去打另一家供应商（web 写面走的就是
        这条"只写裸名"的路径）。

        重复设置同一个条目时**保留**已有的 ``_previous``：落盘路径会先经
        ``apply_runtime`` 同步一次、再由调用方确认一次，若按"与当前相同就清空
        旧主"处理，第二次调用会把刚换下的模型从降级链上抹掉（2026-10-19 实测，
        池外旧主直接消失）。
        """
        entry = self.resolve_entry(entry)
        current = self.active_entry()
        if current != entry:
            self._previous = current
        self._active = entry
        # 降级状态属于旧主模型：换主必须清掉，否则 model_status 会谎报还在
        # 备用线，"已恢复"通知也会挂在一个已经不在用的模型上
        self._using_fallback = False
        self._last_served = None

    def reset_active(self) -> ModelEntry:
        """回到 .env 装配出的活跃位，返回目标条目（供回执文案）。"""
        target = self.default_entry()
        self._previous = None
        self._active = None
        self._using_fallback = False
        self._last_served = None
        return target

    async def probe(self, entry: ModelEntry, timeout: float | None = None) -> None:
        """用一次最小请求验证条目可用——切模型前的闸门。

        失败抛**原始** httpx 异常：agentcore 不掺展示逻辑，由插件层按异常类型
        （HTTP 状态码 / 连接错误）给管理员文案。usage 照实上报：探测是真花钱
        的请求，记账不该悄悄漏掉它。
        """
        cfg = _apply_entry(_CFG, entry)
        payload = {
            "model": cfg.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": _PROBE_MAX_TOKENS,
            "temperature": 0,
        }
        seconds = self._probe_timeout if timeout is None else timeout
        if seconds and seconds > 0:
            # 独立短超时客户端：共享池是 60s，一个探测不该把命令挂那么久。
            # 每次新建的握手成本由"管理员才触发"摊掉。
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(seconds, connect=min(10.0, seconds))
            ) as client:
                await self._post(cfg, payload, client=client)
            return
        await self._post(cfg, payload)

    def _payload(
        self,
        cfg: _LLMCfg,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": int(max_tokens) if max_tokens else cfg.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    async def _post_raw(
        self,
        url: str,
        headers: dict,
        payload: dict[str, Any],
        client: httpx.AsyncClient | None = None,
    ) -> dict:
        http = self._client if client is None else client
        resp = await http.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            logger.error(
                "LLM API %s error: %s body=%s",
                resp.status_code,
                resp.url,
                resp.text[:800],
            )
        resp.raise_for_status()
        return resp.json()

    async def _post(
        self,
        cfg: _LLMCfg,
        payload: dict[str, Any],
        client: httpx.AsyncClient | None = None,
    ) -> dict:
        data = await self._post_raw(
            f"{cfg.base_url}/chat/completions",
            {
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            payload,
            client=client,
        )
        # M7 成本预算：OpenAI 风格 usage 按日累计（主备两条路径都会经过这里）
        record_chat_usage(data.get("usage"), model=cfg.model)
        return data

    async def _notify_fallback(
        self, kind: str, *, to_model: str = "", error: str = ""
    ) -> None:
        """触发 on_fallback 回调（同类事件冷却内只发一次；首次必发）。

        冷却判定与置位之间**没有 await**：asyncio 单线程下并发请求只会有一个
        通过判定，不会出现"N 个请求同时失败就发 N 条通知"。

        ``to_model`` = 实际切到的那个模型（池内候选可能不是 _CFG.fallback_model）；
        事件键名保持 ``primary_model`` / ``fallback_model`` 不变——插件层的通知
        文案读这两个键，改键名会让线上通知静默失真。
        """
        if not self._notify_enabled or self.on_fallback is None:
            return
        now = self._clock()
        last = self._last_notify.get(kind)
        if last is not None and now - last < self._notify_cooldown:
            return
        self._last_notify[kind] = now
        event = {
            "kind": kind,
            "primary_model": self.active_entry().model,
            "fallback_model": to_model or _CFG.fallback_model,
            "cooldown": self._notify_cooldown,
            "error": error,
        }
        try:
            await self.on_fallback(event)
        except Exception:
            # 通知失败绝不能影响对话本身（主人收不到 QQ 不是用户犯的错）
            logger.warning("llm on_fallback callback failed: %s", kind, exc_info=True)

    @staticmethod
    def _is_retryable(e: Exception) -> bool:
        """瞬时故障才值得重试：传输类错误（超时/连接），或限流/网关类状态码。"""
        if isinstance(e, httpx.TransportError):
            return True
        status = getattr(getattr(e, "response", None), "status_code", None)
        return status in _RETRYABLE_STATUS

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """一次对话补全。max_tokens 可覆盖默认值——推理型模型会把预算耗在
        reasoning 上，输出 JSON/长文本时需要更大的上限（见 RAG 蒸馏）。"""
        providers = self.provider_chain()
        if providers:
            return await self._chat_providers(providers, messages, tools, max_tokens)
        if self.has_providers():
            # 全部 provider 被停用：不能静默回落 env（状态与路由会撕裂）
            raise RuntimeError("所有 LLM provider 均已停用")
        candidates = self._candidates()
        # 进入时快照：并发下其它请求的降级不能让「我这个主路径成功」误发
        # 「已恢复」并提前清标志（恢复通知只发给亲历降级窗口的请求）
        entered_degraded = self._using_fallback
        data: dict | None = None
        last_err: Exception | None = None
        for idx, cfg in enumerate(candidates):
            # 只有活跃位值得重试：后续候选本身就是"换个模型再试"，再叠一次
            # 重试会让 3 个池成员最坏打出 6+ 个请求（每个 60s 超时）
            attempts = _PRIMARY_ATTEMPTS if idx == 0 else 1
            data = None
            for attempt in range(attempts):
                try:
                    data = await self._post(
                        cfg, self._payload(cfg, messages, tools, max_tokens)
                    )
                    break
                except Exception as e:
                    last_err = e
                    if attempt + 1 >= attempts or not self._is_retryable(e):
                        break
                    logger.warning(
                        "LLM %s transient failure (%s: %s), retrying",
                        cfg.model,
                        type(e).__name__,
                        e,
                    )
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
            if data is not None:
                self._last_served = ModelEntry(cfg.model, cfg.base_url)
                if idx > 0:
                    self._using_fallback = True
                    await self._notify_fallback(
                        "switched",
                        to_model=cfg.model,
                        error=type(last_err).__name__ if last_err else "",
                    )
                elif entered_degraded and self._using_fallback:
                    self._using_fallback = False
                    await self._notify_fallback("recovered")
                return data
            logger.warning(
                "LLM candidate failed (%s: %s), model=%s",
                type(last_err).__name__,
                last_err,
                cfg.model,
            )
        # 全部候选失败：抛最后一个错误（与旧实现"备用也挂就抛备用的错"同口径）
        assert last_err is not None
        raise last_err

    async def embeddings(self, texts: list[str]) -> list[list[float]]:
        cfg = _CFG
        resp = await self._client.post(
            f"{cfg.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            # cfg.embedding_model 在 import 期快照：运行态切主模型不会把它带走
            json={"model": cfg.embedding_model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        record_embedding_usage(data.get("usage"))
        return [d["embedding"] for d in data.get("data", [])]

    async def aclose(self):
        await self._client.aclose()

    def model_status(self) -> dict:
        """当前生效的模型线路（供 skill 回答"你在用哪个模型"）。

        模型对自己的型号没有可靠认知（会按训练知识自称），所以这个问题必须由
        这里给准信。``line`` 为 ``fallback`` 表示此刻没走在活跃位上。

        读的是模块级 ``_CFG``——与 ``chat()`` 挑主模型用**同一份**配置，这是
        有意的耦合：两者必须一致，否则 skill 会报告一个实际没在用的型号。

        只含模型名与线路类型，**不含** api_key / base_url——返回值会经模型转述
        给用户，而群聊里任何人都可能问这个问题（与 on_fallback 事件同口径）。
        所以 ``pool`` / ``chain`` 也只要模型名：带端点的显示形态由管理员的
        ``/model`` 命令另外取（``entry_display``），不进这里。
        """
        if self._provider_cfg is not None:
            cfg_p = self._provider_cfg
            chain = self.provider_chain()
            active_id = self._provider_active or cfg_p.active
            active_p = cfg_p.by_id(active_id) or cfg_p.providers[0]
            degraded = self._using_fallback and len(chain) > 1
            served = (
                self._last_served_provider
                if (degraded and self._last_served_provider is not None)
                else (chain[1] if degraded else active_p)
            )
            return {
                "active": served.model,
                "line": "fallback" if degraded else "primary",
                "primary": active_p.model,
                "fallback": served.model if degraded else "",
                "default": cfg_p.providers[0].model,
                "source": "providers",
                "active_id": active_p.id,
                "pool": [p.model for p in cfg_p.providers],
                "chain": [p.model for p in chain],
            }
        cfg = _CFG
        active = self.active_entry()
        candidates = self.chain_entries()
        if self._using_fallback and len(candidates) > 1:
            # _last_served 缺失时（如测试直接置 _using_fallback）退到"活跃位之后
            # 的第一个候选"——与旧实现报 _CFG.fallback_model 同口径
            served = (
                self._last_served.model if self._last_served else candidates[1].model
            )
            return {
                "active": served,
                "line": "fallback",
                "primary": active.model,
                "fallback": served,
                "default": self.default_entry().model,
                "source": "runtime" if self._active is not None else "env",
                "pool": [e.model for e in self.pool_entries()],
                "chain": [e.model for e in candidates],
            }
        return {
            "active": active.model,
            "line": "primary",
            "primary": active.model,
            "fallback": cfg.fallback_model,
            "default": self.default_entry().model,
            "source": "runtime" if self._active is not None else "env",
            "pool": [e.model for e in self.pool_entries()],
            "chain": [e.model for e in candidates],
        }
