"""运行期切换主模型：``/model`` 命令与 web 写面共用的编排。

为什么单独一个模块：这条链同时被两个入口调用（QQ 命令 / ``/api/settings``
写 ``LLM_MODEL``），而每一步都有必须守住的顺序——

1. **先探测再落盘**：打错一个模型名会让之后每一轮对话都失败。探测用一次
   16 token 的请求（``LLMClient.probe``），失败就原地返回，文件与运行态都
   不动。web 路径尤其需要这道闸门：它没有 ``/model reset`` 那样的即时自救。
2. **先落盘再改运行态**：文件是权威值（重启后由 ``_CFG.active`` 重新装配），
   运行态只是让当前进程立刻跟上。反过来的话，写盘失败会留下"改了却没生效"
   的进程。
3. **池成员校验**：``LLM_MODELS`` 是运营方维护的候选集，切一个不在池里的模型
   意味着降级链上从没验证过它。池为空（单模型部署）时不做此限。

安全边界：只 superuser 可调（门在 admin 的命令 rule 上）；活跃位**不携带
key**（``parse_model_spec(allow_key=False)``），key 只属于池条目。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SwitchResult:
    ok: bool
    message: str


def _client():
    from agentcore.skills.registry import get_shared_llm_client

    return get_shared_llm_client()


def _pool_match(entry, pool):
    """条目是否在池内（与 client._match_pool 同口径：端点精确 > 同名）。"""
    for e in pool:
        if e.model == entry.model and e.base_url == entry.base_url:
            return e
    if not entry.base_url:
        for e in pool:
            if e.model == entry.model:
                return e
    return None


async def probe_failure_detail(entry) -> str:
    """探测条目可用性；可用返回空串，否则返回面向管理员的失败原因。"""
    try:
        await _client().probe(entry)
    except httpx.HTTPStatusError as e:
        # 400/404 = 模型名或端点不对；401/403 = key 无权限——回状态码即可。
        # 不回显上游响应体：其中可能含内部错误细节/回显请求（REVIEW-1b16fa7..
        # 9c2d3c1 H3 + 2ed7c63..1b16fa7 L5：与 provider_probe_detail 同口径）。
        return f"探测失败：HTTP {e.response.status_code}"
    except httpx.HTTPError as e:
        return f"探测失败：{type(e).__name__}（连接/超时，请检查 base_url 与网络）"
    except Exception as e:  # 探测自身炸了也不能把命令带崩
        logger.exception("model probe crashed")
        return f"探测失败：{type(e).__name__}: {e}"
    return ""


async def provider_probe_detail(provider) -> str:
    """按 provider 的协议探测；可用返回空串。"""
    try:
        await _client().probe_provider(provider)
    except httpx.HTTPStatusError as e:
        # 不回显上游响应体：其中可能含内部错误细节/回显请求；管理员只需要状态码。
        return f"探测失败：HTTP {e.response.status_code}"
    except httpx.HTTPError as e:
        return f"探测失败：{type(e).__name__}（连接/超时，请检查端点与网络）"
    except Exception as e:
        logger.exception("provider probe crashed")
        return f"探测失败：{type(e).__name__}: {e}"
    return ""


def _safe_spec(spec: str) -> str:
    """回执/日志里的用户输入：去掉 ``|key`` 部分并截断，避免手滑把 key 带进群聊。"""
    return str(spec or "").split("|", 1)[0][:40]


def _find_provider(client, spec: str):
    """按 id / model / 展示名匹配 provider（大小写不敏感，唯一前缀也认）。"""
    cfg = client.provider_config()
    raw = (spec or "").strip().lower()
    if cfg is None or not raw:
        return None
    p = cfg.by_id(raw)
    if p is not None:
        return p
    exact = [
        x for x in cfg.providers if x.model.lower() == raw or x.name.lower() == raw
    ]
    if exact:
        return exact[0]
    # 唯一前缀（如 /model deepseek）——多个命中时不猜，交给错误信息列全表
    hits = [
        x
        for x in cfg.providers
        if x.id.startswith(raw)
        or x.model.lower().startswith(raw)
        or x.name.lower().startswith(raw)
    ]
    return hits[0] if len(hits) == 1 else None


async def _switch_provider(client, spec: str, by_uid: str) -> SwitchResult:
    from agentcore.llm import providers as store

    provider = _find_provider(client, spec)
    if provider is None:
        ids = "、".join(
            f"{p.id}（{p.model}）" for p in client.provider_config().providers
        )
        return SwitchResult(
            False, f"没有这个 provider：{_safe_spec(spec)}\n可用：{ids}"
        )
    if not provider.enabled:
        return SwitchResult(False, f"provider {provider.id} 已停用，先启用再切换。")
    detail = await provider_probe_detail(provider)
    if detail:
        return SwitchResult(False, detail)
    # 回执/审计要记**当前 provider**，不能用 active_entry()（那是 legacy env 位）
    status = client.provider_status()
    old_id = status.get("active_id")
    old_provider = client.provider_config().by_id(old_id or "")
    old = old_provider.model if old_provider is not None else provider.model
    client.set_active_provider(provider.id)
    try:
        store.save(client.provider_config())
    except Exception as e:
        if old_id:
            client.set_active_provider(old_id)
        logger.exception("provider active save failed")
        return SwitchResult(False, f"写入 provider 配置失败：{e}（未切换）")
    from agentcore.diagnostics import record as _record

    _record(
        "model_switch",
        old=old,
        new=provider.model,
        by=by_uid,
        probe="ok",
        scope="provider",
    )
    logger.info("model switched by=%s: %s -> %s", by_uid, old, provider.model)
    chain = " → ".join(p.model for p in client.provider_chain())
    return SwitchResult(
        True,
        f"已切换主模型：{old} → {provider.model}（{provider.id} / {provider.protocol}）\n"
        f"已写入 provider 配置。\n降级链：{chain}\n（在飞请求仍用旧模型收尾）",
    )


def _persist(entry) -> str:
    """把活跃位写进 .env 并同步运行态；失败返回原因（成功返回空串）。

    复用 web 写面同一套 ``config_write``：备份 / 手术式补丁 / 原子写 /
    写后验证 / 失败还原，不变量只有一处实现。
    """
    from agentcore.llm.client import canonical_spec
    from plugins.qq_agent_adapter import config_write as cw

    try:
        spec, normalized, file_text = cw.validate_value(
            "LLM_MODEL", canonical_spec(entry)
        )
    except cw.ValueValidationError as e:
        return f"条目不合法：{e}"
    try:
        cw.write_env_key(spec, file_text)
    except cw.ValueValidationError as e:
        return f"写入被拒：{e}（.env 可能是符号链接或同键重复出现）"
    except cw.EnvWriteError as e:
        return f"写入失败：{e}（已自动还原，模型未切换）"
    cw.apply_runtime(spec, normalized, file_text)
    from agentcore.diagnostics import record as _record

    _record("config_write", key=spec.key, new=file_text, mode="qq-model")
    return ""


async def switch_active_model(spec: str, *, by_uid: str) -> SwitchResult:
    """``/model <id|model>``：探测 → 落盘 → 运行态切换。"""
    from agentcore.llm.client import entry_display, parse_model_spec

    client = _client()
    if client.has_providers():
        return await _switch_provider(client, spec, by_uid)
    try:
        entry = parse_model_spec(spec, allow_key=False)
    except ValueError as e:
        return SwitchResult(False, f"无法识别的模型条目：{e}")

    pool = client.pool_entries()
    matched = _pool_match(entry, pool)
    if pool and matched is None:
        names = "、".join(entry_display(e) for e in pool)
        return SwitchResult(
            False,
            f"{entry_display(entry)} 不在候选池里。\n当前池：{names}\n"
            "（要新增模型请改 .env 的 LLM_MODELS 后重启）",
        )
    # 池内同名片段换成池条目本身：继承它登记的端点与 key
    if matched is not None:
        entry = matched

    old = client.active_entry()
    detail = await probe_failure_detail(entry)
    if detail:
        return SwitchResult(False, detail)

    err = _persist(entry)
    if err:
        return SwitchResult(False, err)
    client.set_active(entry)

    from agentcore.diagnostics import record as _record

    _record(
        "model_switch",
        old=entry_display(old),
        new=entry_display(entry),
        by=by_uid,
        probe="ok",
    )
    logger.info(
        "model switched by=%s: %s -> %s",
        by_uid,
        entry_display(old),
        entry_display(entry),
    )
    chain = " → ".join(entry_display(e) for e in client.chain_entries())
    return SwitchResult(
        True,
        f"已切换主模型：{entry_display(old)} → {entry_display(entry)}\n"
        f"已写入 .env（重启后仍生效），探测通过。\n"
        f"降级链：{chain}\n"
        "（在飞请求仍用旧模型收尾）",
    )


async def reset_active_model(*, by_uid: str) -> SwitchResult:
    """``/model reset``：provider 模式回第一个 enabled；legacy 回 .env 装配值。"""
    from agentcore.llm.client import entry_display

    client = _client()
    if client.has_providers():
        from agentcore.llm import providers as store

        status = client.provider_status()
        old_provider = client.provider_config().by_id(status.get("active_id") or "")
        old = old_provider.model if old_provider is not None else ""
        cfg = client.provider_config()
        target_id = next(
            (p.id for p in cfg.providers if p.enabled), cfg.providers[0].id
        )
        target_provider = cfg.by_id(target_id)
        if target_provider is not None:
            detail = await provider_probe_detail(target_provider)
            if detail:
                return SwitchResult(False, f"默认 provider 探测失败，未改动：{detail}")
        pid = client.reset_active_provider()
        try:
            store.save(client.provider_config())
        except Exception as e:
            return SwitchResult(False, f"写入 provider 配置失败：{e}（未改动）")
        from agentcore.diagnostics import record as _record

        _record("model_switch", old=old, new=pid, by=by_uid, probe="ok", reset="true")
        chain = " → ".join(p.model for p in client.provider_chain())
        return SwitchResult(
            True,
            f"已恢复默认 provider：{pid}\n降级链：{chain}",
        )
    old = client.active_entry()
    target = client.reset_active()
    if old == target:
        return SwitchResult(
            True,
            f"当前就是默认主模型 {entry_display(target)}（.env 装配值），未做改动。",
        )
    detail = await probe_failure_detail(target)
    if detail:
        # 探测不过就别把默认位也写坏：先把运行态改回去
        client.set_active(old)
        return SwitchResult(False, f"默认位探测失败，未改动：{detail}")
    err = _persist(target)
    if err:
        client.set_active(old)
        return SwitchResult(False, f"{err}（未改动）")

    from agentcore.diagnostics import record as _record

    _record(
        "model_switch",
        old=entry_display(old),
        new=entry_display(target),
        by=by_uid,
        probe="ok",
        reset="true",
    )
    logger.info("model reset by=%s: -> %s", by_uid, entry_display(target))
    chain = " → ".join(entry_display(e) for e in client.chain_entries())
    return SwitchResult(
        True,
        f"已恢复默认主模型：{entry_display(target)}（.env 装配值）\n"
        f"已写入 .env，探测通过。\n降级链：{chain}",
    )


def describe_status() -> str:
    """``/model`` 查看：当前生效位、默认位、候选池、降级链、独立备用线。"""
    from agentcore.llm.client import _CFG, entry_display

    client = _client()
    if client.has_providers():
        cfg = client.provider_config()
        active_id = client.provider_status().get("active_id")
        lines = [f"当前 provider：{active_id}（{cfg.by_id(active_id).model}）"]
        for p in cfg.providers:
            mark = "  ← 当前" if p.id == active_id else ""
            state = "" if p.enabled else "（已停用）"
            lines.append(
                f"  {p.id}: {p.model} · {p.protocol} · {p.base_url}{state}{mark}"
            )
        lines.append("降级链：" + " → ".join(x.model for x in client.provider_chain()))
        lines.append("用法：/model <id|model> 切换 · /model reset 回默认")
        return "\n".join(lines)
    st = client.model_status()
    active = client.active_entry()
    lines = [f"当前生效：{entry_display(active)}"]
    if st["line"] == "fallback":
        lines[0] += f"（不在活跃位上，实际服务：{st['active']}）"
    else:
        lines[0] += "（主线路）"
    if st["source"] == "runtime":
        lines.append(f"默认位（.env 装配值）：{entry_display(client.default_entry())}")
    pool = client.pool_entries()
    if pool:
        lines.append("候选池（书写序即降级序）：")
        lines.extend(
            f"  {i}. {entry_display(e)}" + ("  ← 当前" if e == active else "")
            for i, e in enumerate(pool, 1)
        )
    else:
        lines.append("候选池：未配置 LLM_MODELS（单模型部署）")
    lines.append(
        "降级链：" + " → ".join(entry_display(e) for e in client.chain_entries())
    )
    legacy = client.legacy_fallback_entry()
    if legacy is not None:
        lines.append(f"独立备用线（主端点外）：{entry_display(legacy, _CFG.base_url)}")
    lines.append("用法：/model <名字> 切换 · /model reset 回默认位")
    return "\n".join(lines)
