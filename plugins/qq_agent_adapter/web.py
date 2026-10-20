"""Web 只读总览：在浏览器里看运行状态（**零写入接口**）。

安全模型——这是本仓新增的攻击面，写错等于把 bot 数据暴露全网：

1. **fail-closed**：未配置 ``AGENT_WEB_TOKEN`` 时**整个 web 面不挂载**。
   默认部署（照抄 .env.example 但没设 token） therefore 没有任何暴露。
2. token 从 ``Authorization: Bearer <token>`` 取，``secrets.compare_digest``
   恒等比较；**不接受 URL query**（query 会进反代/访问日志）。
3. 可选 ``AGENT_WEB_ALLOW_CIDRS`` 源 IP 白名单（直连场景）。反代部署下
   ``request.client.host`` 是反代地址，那种场景应在反代层做 IP 限制。
4. 路由统一挂在 ``/agent-web`` 前缀，避开 OneBot 反向 WS 的路径。
5. **只读**：没有任何写接口（总览 + 设置视图都只读），因而不存在"网页改坏
   配置/删数据"的风险。后续要加写入时必须同时补：审计（``diagnostics.record``）
   + 二次确认 + 备份原值。见 BACKLOG 的 Web 分项。

数据口径：所有数字都来自现成来源（``budget`` 账本、``kb.stats()``、
``LLMClient.model_status()``、``driver._agent_*``），取不到就置 null 并在
``errors`` 里说明原因——**不猜、不补零**（恒真数据比没有更糟）。
设置视图（``/api/settings``）额外口径：只展示白名单键、标注生效时机
（即时 / 需重启）、``base_url`` 与 api_key 不进响应体。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from agentcore.diagnostics import recent as _recent_events
from plugins.qq_agent_adapter import config_write as cw

logger = logging.getLogger(__name__)

# 必须**模块级**导入：本文件有 `from __future__ import annotations`，注解是惰性
# 字符串，FastAPI 靠模块全局解析它们——若在函数内 import，`request: Request`
# 会被当成 query 参数（实测 422 missing query request）。非 fastapi driver 的
# 部署下降级为 None，mount_web 直接不挂载。
try:
    from fastapi import Depends, FastAPI, HTTPException, Query, Request
    from fastapi.responses import (
        FileResponse,
        HTMLResponse,
        JSONResponse,
    )
except Exception:  # pragma: no cover - 没有 fastapi 的部署
    Depends = FastAPI = HTTPException = Query = Request = None  # type: ignore[assignment]
    FileResponse = HTMLResponse = JSONResponse = None  # type: ignore[assignment]

PREFIX = "/agent-web"
# 群统计只读面：每个群展示的词数上限（榜单条数走 groupstats.top_n()）
_GROUP_STATS_WORDS = 30

# 历史累计要遍历所有月份账本，不该跟着 5s 轮询反复算
_HISTORY_TTL = 60.0
_history_cache: dict[str, Any] = {"at": 0.0, "value": None}

# ---------- 受控写入（D2-1）----------
# 门禁定稿（BACKLOG D 组）：AGENT_WEB_ALLOW_CIDRS 已配置 **且** AGENT_WEB_WRITE=1，
# 缺一写路由整体不挂载（比读面更严的 fail-closed）。确认码 30s 窗口、单次有效。
_WRITE_WINDOW = 30
_write_lock = asyncio.Lock()
# nonce → 实际命中的窗口。判重必须按 **nonce** 记账：原先按 (nonce, 当前窗口)
# 记账，跨 30s 窗口边界时同一码的第二次消费会落到新键上被放行（REVIEW
# 26fec4d..3ce6e0a 的 M1，时钟推进复现实锤）。
_used_confirms: dict[str, int] = {}


def _write_enabled() -> bool:
    return bool(_allow_cidrs()) and (os.getenv("AGENT_WEB_WRITE") or "").strip() == "1"


def _env_value_or_none(key: str) -> str | None:
    """读 .env 某键当前值；文件缺失/读失败返回 None（展示层显示"未设置"）。

    挑战段与 preview 的旧值展示是**非关键路径**——文件层面的错误留给写入
    阶段（那里有 exists 预检 + env_file_missing），不在这两处裸 500。
    """
    try:
        return cw.parse_env_value(cw.read_env_text(cw.env_file_path()), key)
    except OSError:
        return None


def _confirm_token(
    nonce: str, key: str, file_text: str, window: int | None = None
) -> str:
    """写入确认码：HMAC(web_token, nonce|key|新值|30s 窗口)。

    绑定 web token（换 token 旧码全失效）、拟写入值（拿到码改不了别的值/
    别的键）与挑战 nonce（确定性 HMAC 会让"同窗口写同键同值"的第二个请求
    被误判重放——线上实测 2026-09-29，nonce 使每次挑战的码唯一）。单次有效
    由 ``_used_confirms``（按 nonce 记账）保证。
    """
    w = int(time.time() // _WRITE_WINDOW) if window is None else window
    msg = f"{nonce}|{key}|{file_text}|{w}".encode()
    return hmac.new(_web_token().encode(), msg, hashlib.sha256).hexdigest()[:32]


def _consume_confirm(
    nonce: str, token: str, key: str, file_text: str, now: float | None = None
) -> str | None:
    """校验并消费确认码；返回错误原因，None = 通过。

    ``now`` 可注入（测试用）；判重按 nonce 单集合——每次挑战的 nonce 随机，
    同一 nonce 第二次到达（无论落在哪个窗口）都是重放。HMAC 试算窗口仍保留
    一格宽限（`now_w, now_w-1`），覆盖请求跨越窗口边界的合法时序。
    """
    if not (nonce.isascii() and token.isascii()):
        return "confirm_invalid_or_expired"
    now_w = int((time.time() if now is None else now) // _WRITE_WINDOW)
    for w in (now_w, now_w - 1):  # 允许跨窗口边界的一格宽限
        if hmac.compare_digest(token, _confirm_token(nonce, key, file_text, window=w)):
            break
    else:
        return "confirm_invalid_or_expired"
    if nonce in _used_confirms:
        return "confirm_reused"
    _used_confirms[nonce] = now_w
    # 窗口推进后清理过期记录，防集合无界增长
    for stale in [n for n, ww in _used_confirms.items() if ww < now_w - 2]:
        del _used_confirms[stale]
    return None


# ---------- 配置 ----------
def _web_token() -> str:
    return (os.getenv("AGENT_WEB_TOKEN") or "").strip()


def _allow_cidrs() -> list[str]:
    raw = (os.getenv("AGENT_WEB_ALLOW_CIDRS") or "").strip()
    return [c.strip() for c in raw.split(",") if c.strip()]


def _ip_allowed(host: str) -> bool:
    """未配置白名单 = 只靠 token；配置了则源 IP 必须在任一网段内。"""
    cidrs = _allow_cidrs()
    if not cidrs:
        return True
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    for c in cidrs:
        try:
            if addr in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            logger.warning("AGENT_WEB_ALLOW_CIDRS 含非法网段：%r", c)
    return False


# ---------- 数据装配（never raises） ----------
async def build_overview() -> dict:
    """汇总总览数据。任何一块取不到只记 ``errors``，绝不影响其他块与 HTTP 200。"""
    out: dict[str, Any] = {"generated_at": time.time(), "errors": []}

    def _fail(section: str, exc: Exception) -> None:
        out["errors"].append(f"{section}: {type(exc).__name__}")

    # LLM 主/备实时线路（刚加的能力，主页最该看的一项）
    try:
        from agentcore.skills.registry import get_shared_llm_client

        out["llm"] = get_shared_llm_client().model_status()
    except Exception as e:
        out["llm"] = None
        _fail("llm", e)

    # 协议端连接：reverse-WS 下 driver.bots 只含**已连接**的 bot，空 = 没连上
    try:
        from nonebot import get_driver

        driver = get_driver()
        out["bots"] = [
            {"self_id": str(sid)} for sid in (getattr(driver, "bots", None) or {})
        ]
    except Exception as e:
        out["bots"] = []
        _fail("bots", e)

    # 预算（今日 + 成本 + 限额；历史累计带 TTL 缓存）
    try:
        from agentcore.budget import get_budget

        b = get_budget()
        day = b.today()
        now = time.monotonic()
        if now - _history_cache["at"] > _HISTORY_TTL:
            total = b.total()
            # 先算后记账：total() 一抛就把「at=now + value=None」一起写进去，
            # 5s 轮询下预算块要闪一分钟「读取失败」且 today 数据陪着一起丢
            _history_cache["at"] = now
            _history_cache["value"] = total
        out["budget"] = {
            "today": day,
            "cost_today": b.estimate_cost(),
            "daily_tokens": b.daily_tokens,
            "enforce": b.enforce,
            "history": _history_cache["value"],
        }
    except Exception as e:
        out["budget"] = None
        _fail("budget", e)

    # 组件：记忆后端 / 人格 / skill 数 / 知识库
    # key 先落默认值：取数失败时是 null 而不是"键不存在"（前端要能稳定渲染）
    out.setdefault("memory_backend", None)
    out.setdefault("skills", [])
    out.setdefault("persona_default", None)
    try:
        from nonebot import get_driver

        driver = get_driver()
        memory = getattr(driver, "_agent_memory", None)
        out["memory_backend"] = type(memory).__name__ if memory is not None else None
        engine = getattr(driver, "_agent_engine", None)
        skills = getattr(engine, "skills", None)
        out["skills"] = (
            [
                {"name": s.name, "permission": s.permission}
                for s in skills.skills.values()
            ]
            if skills is not None
            else []
        )
        pm = getattr(driver, "_agent_persona_manager", None)
        if pm is not None:
            default = pm.default()
            out["persona_default"] = default.name if default else None
        else:
            out["persona_default"] = None
    except Exception as e:
        _fail("components", e)

    try:
        from nonebot import get_driver

        kb = getattr(get_driver(), "_agent_kb", None)
        if kb is None:
            out["kb"] = None
        else:
            out["kb"] = {"stats": await kb.stats(), "describe": kb.describe()}
    except Exception as e:
        out["kb"] = None
        _fail("kb", e)

    # 运行时开关（这些函数本来就是调用时读 env，值即当前生效值）
    try:
        from plugins.qq_agent_adapter.group_context import context_enabled
        from plugins.qq_agent_adapter.pipeline import vision_enabled

        out["flags"] = {
            "group_context": context_enabled(),
            "vision": vision_enabled(),
        }
    except Exception as e:
        out["flags"] = None
        _fail("flags", e)

    out["recent_events"] = _recent_events(20)
    return out


# ---------- 设置视图（只读） ----------
# config.yaml 各段**允许展示**的键白名单：设置页是配置明细面，绝不做整段倒出——
# 万一将来有人在某个配置段里放了敏感值，白名单能兜住（与 overview 的"响应体
# 不得含 api_key/base_url/消息正文"同一纪律）。
_AGENT_CFG_KEYS = (
    "max_iterations",
    "extract_facts",
    "memory_facts_top_k",
    "memory_facts_threshold",
    "summary_enabled",
    "history_token_budget",
    "summary_max_tokens",
    "summary_fetch_limit",
    "summary_max_chars",
    "history_stale_after",
)
_ARCHIVE_KEYS = ("dir", "keep_days")
_BACKUP_KEYS = ("dir", "keep", "cron", "mirror_dir")


def _boot_config() -> dict:
    """读启动配置（``AGENT_CONFIG`` 指向的文件，默认 config.yaml）。

    与 ``__init__.py`` 启动路径读**同一份文件**——那里解析出的 CONFIG 是函数内
    局部量拿不到，设置页按需重读（几 KB 的 yaml，5s 轮询下开销可忽略）。
    """
    import yaml

    path = os.getenv("AGENT_CONFIG", "config.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _memory_summary_from(cfg: dict) -> dict:
    """agent: 段 → 设置视图块。独立成纯函数：白名单纪律不依赖 driver 可单测。"""
    return {"effective": "boot", **{k: cfg.get(k) for k in _AGENT_CFG_KEYS}}


async def _collect_group_stats(store, start, end) -> list[dict]:
    """一个窗口内的每群聚合（Top5 / 词 / 小时直方图）。"""
    from agentcore.groupstats import config as gs_config

    groups: list[dict] = []
    totals = await store.group_totals(start, end)
    for row in totals:
        gid = str(row.get("group_id") or "")
        if not gid:
            continue
        top = await store.user_ranking(gid, start, end, gs_config.top_n())
        total = await store.window_total(gid, start, end)
        names = await store.nicknames(gid, start, end)
        words = await store.word_freq(gid, start, end, _GROUP_STATS_WORDS)
        hist = await store.hour_histogram(gid, start, end)
        groups.append(
            {
                "group_id": gid,
                "msgs": int(row.get("msgs") or 0),
                "users": int(row.get("users") or 0),
                "window_total": total,
                "top": [
                    {
                        "user_id": r["user_id"],
                        "nickname": names.get(str(r["user_id"])) or "",
                        "msgs": int(r.get("msgs") or 0),
                        "pct": round(int(r.get("msgs") or 0) / total * 100, 1)
                        if total
                        else 0.0,
                    }
                    for r in top
                ],
                "words": [{"word": w, "count": c} for w, c in words],
                "hours": {str(h): n for h, n in sorted(hist.items())},
            }
        )
    return groups


async def build_group_stats() -> dict:
    """群发言统计只读数据：**上周（完整）+ 本周（进行中）**，每群 Top5/词云/直方图。

    与其它 api 面同门禁（Bearer + CIDR，见 mount_web 的 _auth）。响应只含
    **聚合数据**（条数/词频/名片快照）——不含消息正文，符合 web 面不变量。

    返回结构：
    - ``windows``：``[{key,label,start,end,groups}]``，key = last/current；
    - ``window`` / ``groups``：**上周**的旧字段（向后兼容旧脚本/旧页面）。

    统计未开启/无 store 时返回 enabled=False 的空结构（页面显示"未开启"，
    不是报错——与 build_settings 的降级口径一致）。
    """
    from agentcore.groupstats import config as gs_config
    from agentcore.groupstats import ranking

    out: dict = {
        "enabled": False,
        "window": None,
        "groups": [],
        "windows": [],
        "generated_at": time.time(),
    }
    if not gs_config.enabled():
        return out
    last_start, last_end = ranking.previous_week()
    cur_start, cur_end = ranking.current_week()
    out["enabled"] = True
    out["window"] = {"start": last_start.isoformat(), "end": last_end.isoformat()}
    out["windows"] = [
        {
            "key": "last",
            "label": "上周",
            "start": last_start.isoformat(),
            "end": last_end.isoformat(),
            "groups": [],
        },
        {
            "key": "current",
            "label": "本周（进行中）",
            "start": cur_start.isoformat(),
            "end": cur_end.isoformat(),
            "groups": [],
        },
    ]
    try:
        from nonebot import get_driver

        store = getattr(get_driver(), "_agent_group_stats_store", None)
        if store is None:
            return out  # 功能开着但 store 没装配（启动降级）：空 groups
        last_groups = await _collect_group_stats(store, last_start, last_end)
        current_groups = await _collect_group_stats(store, cur_start, cur_end)
        out["groups"] = last_groups
        out["windows"][0]["groups"] = last_groups
        out["windows"][1]["groups"] = current_groups
    except Exception as e:
        # 取数失败不拖垮整个面：带上错误标记，页面其余部分照常渲染
        out["error"] = type(e).__name__
        logger.warning("build_group_stats failed", exc_info=True)
    return out


# provider 文件存在时，这些键只是迁移前的 legacy 入口：再写它们不会改变
# 实际路由（chat 走 provider_chain），必须显式拒写而不是静默无效。
LEGACY_MODEL_KEYS = {
    "LLM_MODEL",
    "LLM_MODELS",
    "LLM_FALLBACK_MODEL",
    "LLM_FALLBACK_BASE_URL",
    "LLM_FALLBACK_API_KEY",
}


def _has_providers() -> bool:
    try:
        from agentcore.skills.registry import get_shared_llm_client

        return get_shared_llm_client().has_providers()
    except Exception:
        return False


def _env_settings_payload() -> dict:
    """全部 .env 设置项（schema 驱动设置页的数据源）。

    凭据/URL 类只回 ``is_set``，**永不回值**——与 ``model_status`` 的
    "响应体不含 api_key/base_url" 同一道纪律。``live`` 决定页面标"即时"
    还是"重启生效"。
    """
    from .settings_spec import ENV_SPECS, OPTIONS

    groups: dict[str, list[dict]] = {}
    secret_count = 0
    providers_active = _has_providers()
    for s in ENV_SPECS:
        if s.secret:
            # 凭据/URL 类**连键名都不出**：响应体不得含 api_key/base_url
            # （AGENTS.md 不变量，键名也算），页面只显示"另有 N 个走 SSH"。
            secret_count += 1
            continue
        raw = os.getenv(s.key)
        item: dict[str, Any] = {
            "key": s.key,
            "kind": s.kind,
            "group": s.group,
            "label": s.label or s.key,
            "description": s.description,
            "default": s.default,
            "live": s.live,
            "writable": s.writable,
            "secret": s.secret,
            "min": s.min_value,
            "max": s.max_value,
            "max_items": s.max_items,
            "item_max_len": s.item_max_len,
            "max_len": s.max_len,
            "cluster": s.cluster,
            "options": list(OPTIONS.get(s.key, ())),
            "legacy": providers_active and s.key in LEGACY_MODEL_KEYS,
        }
        # provider 模式下 legacy 模型键的值可能内嵌 endpoint，不回显
        item["value"] = None if item.get("legacy") else raw
        item["is_set"] = bool(raw)
        groups.setdefault(s.group, []).append(item)
    return {
        "total": len(ENV_SPECS) - secret_count,
        "secret_count": secret_count,
        "writable": sum(1 for s in ENV_SPECS if s.writable),
        "groups": [{"name": name, "items": items} for name, items in groups.items()],
    }


def _yaml_settings_payload() -> dict:
    """config.yaml 的可编辑标量项（列表类如 push.jobs 只展示、不可写）。"""
    from .config_yaml import YAML_OPTIONS, YAML_SPECS, config_path

    path = config_path()
    out: dict[str, Any] = {
        "path": str(path),
        "total": len(YAML_SPECS),
        "writable": sum(1 for s in YAML_SPECS if s.writable),
        "groups": [],
        "error": None,
    }
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        out["error"] = type(e).__name__
        return out

    def _get(dotted: str):
        cur = data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    groups: dict[str, list[dict]] = {}
    for s in YAML_SPECS:
        groups.setdefault(s.group, []).append(
            {
                "key": "yaml:" + s.path,
                "path": s.path,
                "kind": s.kind,
                "group": s.group,
                "label": s.label,
                "description": s.description,
                "default": s.default,
                "live": False,  # config.yaml 全部启动期装配
                "writable": s.writable,
                "secret": False,
                "min": s.min_value,
                "max": s.max_value,
                "max_len": s.max_len,
                "cluster": s.cluster,
                "options": list(YAML_OPTIONS.get(s.path, ())),
                "value": _get(s.path),
                "is_set": _get(s.path) is not None,
            }
        )
    out["groups"] = [{"name": n, "items": i} for n, i in groups.items()]
    return out


def _gate_value(gate_key: str | None):
    """从**文件**读簇开关（env 或 yaml）；缺失时回落进程 env / Schema 默认。

    必须读文件而不是 ``os.environ``：boot 键改了之后进程里还是旧值，
    只信 env 会让"已存盘待重启的关闭状态"被绕过。
    """
    if not gate_key:
        return None
    if gate_key.startswith("yaml:"):
        from .config_yaml import config_path

        try:
            import yaml

            data = yaml.safe_load(config_path().read_text(encoding="utf-8")) or {}
            cur = data
            for part in gate_key[5:].split("."):
                cur = cur[part]
            return bool(cur)
        except Exception:
            return None
    from .config_write import env_file_path, parse_env_value, read_env_text

    raw = None
    try:
        raw = parse_env_value(read_env_text(env_file_path()), gate_key)
    except Exception:
        raw = None
    if raw is None:
        raw = os.getenv(gate_key)
    if raw is None:
        from .settings_spec import ENV_SPECS

        spec = next((x for x in ENV_SPECS if x.key == gate_key), None)
        raw = spec.default if spec is not None else "1"
    return (raw or "").strip().lower() not in {"0", "false", "no", "off"}


def _clusters_payload(env_payload, yaml_payload) -> list[dict]:
    """把 env + config.yaml 的项并进簇，gate 项排第一。"""
    from .settings_spec import CLUSTERS

    by_cluster: dict[str, list[dict]] = {}
    for src in (env_payload, yaml_payload):
        for g in (src or {}).get("groups", []):
            for item in g["items"]:
                by_cluster.setdefault(item.get("cluster") or "advanced", []).append(
                    item
                )
    out: list[dict] = []
    for c in CLUSTERS:
        items = by_cluster.get(c.id, [])
        if not items and not c.gate_key:
            continue
        gate_value = _gate_value(c.gate_key) if c.gate_key else None
        if c.gate_key:
            items.sort(key=lambda it: 0 if it.get("key") == c.gate_key else 1)
        out.append(
            {
                "id": c.id,
                "title": c.title,
                "description": c.description,
                "gate_key": c.gate_key,
                "gate_value": gate_value,
                "enabled": gate_value is not False,
                "total": len(items),
                "writable": sum(1 for it in items if it.get("writable")),
                "items": items,
            }
        )
    return out


def _ensure_cluster_enabled(spec) -> None:
    """gate 为关时拒绝写同簇其余项（前端灰置只是提示，这里是真门）。"""
    from .settings_spec import CLUSTER_BY_ID

    c = CLUSTER_BY_ID.get(getattr(spec, "cluster", "") or "")
    if c is None or not c.gate_key:
        return
    item_key = getattr(spec, "key", None) or (
        "yaml:" + spec.path if hasattr(spec, "path") else ""
    )
    if item_key == c.gate_key:
        return
    if _gate_value(c.gate_key) is False:
        raise cw.ValueValidationError(f"cluster_disabled:{c.gate_key}")


def _validate_write(key: str, raw: object):
    """按 key 前缀分派校验；返回 ``(scope, spec, normalized, file_text)``。

    ``yaml:`` 前缀让 .env 与 config.yaml 共用同一套两段确认/审计/回滚，
    又不必给确认码再加一个 scope 维度（key 本身已经绑定进 HMAC）。
    """
    from . import config_yaml as cy

    if key.startswith(cy.YAML_PREFIX):
        try:
            spec, normalized, file_text = cy.validate_value(
                key[len(cy.YAML_PREFIX) :], raw
            )
        except ValueError as e:
            raise cw.ValueValidationError(str(e)) from None
        _ensure_cluster_enabled(spec)
        return "yaml", spec, normalized, file_text
    spec, normalized, file_text = cw.validate_value(key, raw)
    if key in LEGACY_MODEL_KEYS and _has_providers():
        raise cw.ValueValidationError("legacy_model_key")
    _ensure_cluster_enabled(spec)
    return "env", spec, normalized, file_text


async def build_settings() -> dict:
    """设置视图数据（**只读**）：当前生效值 + 生效时机，绝不包含写入能力。

    ``effective`` 标注的是**生效时机**而不是来源（env 还是 config.yaml 猜了
    也不可靠）：``live`` = 调用时读 env/实时对象，改完下次调用即生效；
    ``boot`` = 启动期装配进对象，改配置文件要重启才生效。降级口径与
    ``build_overview`` 相同：某块取不到只进 ``errors``，该块为 null。
    """
    out: dict[str, Any] = {"generated_at": time.time(), "errors": []}
    # 设置页据此决定是否渲染编辑入口（门禁 = CIDR 已配 + AGENT_WEB_WRITE=1）
    out["write_enabled"] = _write_enabled()

    def _fail(section: str, exc: Exception) -> None:
        out["errors"].append(f"{section}: {type(exc).__name__}")

    # 全部设置项（schema 驱动）：env 一份、config.yaml 一份
    try:
        out["settings"] = _env_settings_payload()
    except Exception as e:
        out["settings"] = None
        _fail("settings", e)
    try:
        out["config"] = _yaml_settings_payload()
    except Exception as e:
        out["config"] = None
        _fail("config", e)
    try:
        out["clusters"] = _clusters_payload(out.get("settings"), out.get("config"))
    except Exception as e:
        out["clusters"] = None
        _fail("clusters", e)

    # LLM 线路：model_status 只含模型名与线路类型，不含 api_key/base_url
    # （其 docstring 明文约定，返回值会经模型转述给用户）——设置页直接复用
    try:
        from agentcore.skills.registry import get_shared_llm_client

        client = get_shared_llm_client()
        out["llm"] = {
            "effective": "live",
            **(client.model_status() or {}),
            # 完整 endpoint 只在这里（鉴权 web 面）出现；model_status/skill 不含
            "providers": (client.provider_status() or {}).get("providers", []),
        }
    except Exception as e:
        out["llm"] = None
        _fail("llm", e)

    # 运行开关：这些函数本来就是调用时读 env，值即当前生效值
    try:
        from plugins.qq_agent_adapter.group_context import context_enabled
        from plugins.qq_agent_adapter.pipeline import vision_enabled
        from plugins.qq_agent_adapter.wakewords import load_wake_words

        out["runtime_flags"] = {
            "effective": "live",
            "vision": vision_enabled(),
            "group_context": context_enabled(),
            # 唤醒词是运营配置（README 明文的配置项），不是用户消息内容
            "wake_words": load_wake_words(),
        }
    except Exception as e:
        out["runtime_flags"] = None
        _fail("runtime_flags", e)

    # 预算：账本实时值
    try:
        from agentcore.budget import get_budget

        b = get_budget()
        out["budget"] = {
            "effective": "live",
            "daily_tokens": b.daily_tokens,
            "enforce": b.enforce,
            "today": b.today(),
            "cost_today": b.estimate_cost(),
        }
    except Exception as e:
        out["budget"] = None
        _fail("budget", e)

    # 记忆与摘要：engine.config = config.yaml 的 agent: 段（启动期装配），
    # 只取白名单键
    try:
        from nonebot import get_driver

        engine = getattr(get_driver(), "_agent_engine", None)
        cfg = dict(getattr(engine, "config", None) or {})
        out["memory_summary"] = _memory_summary_from(cfg)
    except Exception as e:
        out["memory_summary"] = None
        _fail("memory_summary", e)

    # RAG：kb 实例即启动期装配结果，describe() 是它的当前生效值
    try:
        from nonebot import get_driver

        kb = getattr(get_driver(), "_agent_kb", None)
        if kb is not None:
            out["rag"] = {"effective": "boot", **(kb.describe() or {})}
        else:
            # kb 未初始化也是"取不到"：必须留痕。否则"driver 已 init 但属性
            # 缺失"与"driver 未 init（这里直接抛）"两条路径行为不一致，
            # 设置页无从区分"没有 RAG"和"没取到"（全量套件实测踩过）
            out["rag"] = None
            _fail("rag", RuntimeError("kb_not_initialized"))
    except Exception as e:
        out["rag"] = None
        _fail("rag", e)

    # 备份归档：config.yaml（启动期生效；改 cron 后需重启——与 admin push 命令
    # 对 push.jobs 的既有口径一致）
    try:
        raw = _boot_config()
        archive = raw.get("archive") or {}
        backup = raw.get("backup") or {}
        out["backup_archive"] = {
            "effective": "boot",
            "archive": {k: archive.get(k) for k in _ARCHIVE_KEYS},
            "backup": {k: backup.get(k) for k in _BACKUP_KEYS},
        }
    except Exception as e:
        out["backup_archive"] = None
        _fail("backup_archive", e)

    return out


# ---------- 日志查看（只读） ----------
# 内容口径（2026-09-29 管理员决策，D4）：**原始日志不脱敏**——matcher 的
# [msg]/[reply] 行含消息正文前 200 字（_truncate(text, 200)），这是与"响应体
# 不含消息正文"不变量之间**显式批准的例外**（AGENTS.md §4 已注明）。门禁与
# settings 相同（Bearer + 可选 CIDR），README 明示"拿到 token 即可读日志"。
_LOG_FILE_RE = re.compile(r"^agent\.log(\.\d{4}-\d{2}-\d{2})?$")
_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) ([A-Z]+) ([\w.]+): (.*)$",
    re.DOTALL,
)
_LOG_WINDOW_DEFAULT = 64 * 1024
_LOG_WINDOW_MAX = 256 * 1024
_LOG_DOWNLOAD_MAX = 200 * 1024 * 1024
_LOG_GREP_MAX = 100
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _log_dir() -> Path:
    """与 agentcore.logging_setup.DIR_ENV 同源（复制常量避免 import 期耦合）。"""
    return Path(os.getenv("AGENT_LOG_DIR", "data/logs"))


def _safe_log_name(name: str) -> str | None:
    """日志文件名白名单：只允许 agent.log 与其轮转名，其余（含任何路径分隔
    符、编码绕行）一律 None——目录本身钉死在 AGENT_LOG_DIR，文件名不得携带
    任何路径成分。"""
    name = str(name or "")
    return name if _LOG_FILE_RE.fullmatch(name) else None


def list_log_files() -> dict:
    """可查看的日志文件清单（最新在前）。任何失败返回空清单不抛。"""
    out: dict[str, Any] = {"generated_at": time.time(), "files": [], "errors": []}
    try:
        d = _log_dir()
        files = []
        for p in d.iterdir():
            if not _LOG_FILE_RE.fullmatch(p.name):
                continue
            st = p.stat()
            files.append(
                {
                    "name": p.name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "current": p.name == "agent.log",
                }
            )
        files.sort(key=lambda x: x["mtime"], reverse=True)
        out["files"] = files
    except Exception as e:
        out["errors"].append(f"list: {type(e).__name__}")
    return out


def _parse_log_lines(text: str) -> list[dict]:
    """按 ``logging`` 格式拆结构化字段；异常栈等续行归并进上一条。"""
    entries: list[dict] = []
    for line in text.split("\n"):
        if not line:
            continue
        m = _LOG_LINE_RE.match(line)
        if m:
            entries.append(
                {
                    "ts": m.group(1),
                    "level": m.group(2),
                    "logger": m.group(3),
                    "msg": m.group(4),
                }
            )
        elif entries:
            entries[-1]["msg"] += "\n" + line
        # 无前导条目的残行：行对齐后理论上不出现，丢弃
    return entries


def read_log_window(
    name: str,
    *,
    window: int = _LOG_WINDOW_DEFAULT,
    after: int = 0,
    levels: tuple[str, ...] = (),
    module: str = "",
    grep: str = "",
) -> dict:
    """tail 读取一个日志窗口，结构化返回。

    - ``after`` 增量游标：从上次 ``end_offset`` 续读；游标超过当前大小（轮转/
      截断）时置 ``reset`` 回到尾部窗口
    - 行对齐：尾部窗口切在半行上时丢弃首残行；增量窗口切在半行上时丢弃**尾**
      残行并回退 ``end_offset``——任何路径下页面都不会出现半个时间戳
    - 过滤三条件 AND，只作用于本窗口（增量模式下被滤掉的行不追补，口径见 README）
    """
    out: dict[str, Any] = {
        "file": name,
        "lines": [],
        "errors": [],
        "generated_at": time.time(),
    }
    safe = _safe_log_name(name)
    if safe is None:
        out["errors"].append("file: invalid_name")
        return out
    path = _log_dir() / safe
    try:
        size = path.stat().st_size
    except OSError as e:
        out["errors"].append(f"stat: {type(e).__name__}")
        return out
    out["size"] = size
    after = max(0, int(after))
    window = max(1024, min(int(window), _LOG_WINDOW_MAX))
    reset = after > size
    if reset:
        after = 0
    try:
        with path.open("rb") as f:
            if after:
                f.seek(after)
                chunk = f.read(window)
                out["start_offset"] = after
                # 尾残行回退：最后一个 \n 之后不算完整行
                last_nl = chunk.rfind(b"\n")
                if last_nl == -1:
                    out["end_offset"] = after  # 窗口内没有完整行，游标原地等下一条
                    out["truncated"] = False
                    out["reset"] = reset
                    return out
                chunk = chunk[: last_nl + 1]
                end_offset = after + last_nl + 1
            else:
                window_start = max(0, size - window)
                f.seek(window_start)
                chunk = f.read(window)
                text = chunk.decode("utf-8", errors="replace")
                dropped = text.find("\n") + 1 if size > window else 0
                text = text[dropped:]
                out["start_offset"] = window_start + dropped
                end_offset = size
                out["truncated"] = size > window
    except OSError as e:
        out["errors"].append(f"read: {type(e).__name__}")
        return out
    lines = _parse_log_lines(chunk.decode("utf-8", errors="replace"))
    if levels:
        lines = [x for x in lines if x["level"] in levels]
    if module:
        lines = [x for x in lines if x["logger"].startswith(module)]
    if grep:
        # 字面量子串匹配整行原文（含时间戳），不做正则——免 ReDoS 面
        lines = [
            x
            for x in lines
            if grep in f"{x['ts']} {x['level']} {x['logger']}: {x['msg']}"
        ]
    out["lines"] = lines
    out["returned"] = len(lines)
    out["end_offset"] = end_offset
    out["reset"] = reset
    out["truncated"] = out.get("truncated", False)
    return out


# ---------- HTTP 面 ----------
# 页面（HTML+CSS+JS，自包含、不引任何外部资源——这是局域网管理页，不能依赖外网）。
# **必须是 raw string**：内容里的反斜杠要原样进浏览器（如 JS 的 join("\n")）。
# 普通三引号会把 \n 吃成真实换行 → 整个 <script> 语法错误、页面白屏，
# 而且只有浏览器（或 node --check）才看得出来，pytest 抓不到。
_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-demo 总览</title>
<style>
:root{
  --paper:#f5f4f1; --paper-2:#efeeea; --ink:#111111; --ink-soft:#3a3a3a;
  --muted:#8a8782; --line:#111111; --line-soft:#cbc9c3; --white:#fff;
  --alert:#b23c2a;
  --sans:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei','PingFang SC','Hiragino Sans GB',sans-serif;
  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace;
  --pad:clamp(18px,3.6vw,54px); --maxw:1440px; --ease:cubic-bezier(.2,.7,.2,1);
}
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
::selection{background:var(--ink);color:var(--paper)}
:focus-visible{outline:1px solid var(--ink);outline-offset:3px}
h1,h2,h3,p{margin:0}
input,button{font:inherit;color:inherit}
.mono{font-family:var(--mono);font-size:11px;font-weight:400;letter-spacing:.14em;text-transform:uppercase}
.wrap{width:100%;max-width:var(--maxw);margin:0 auto;padding:0 var(--pad)}

/* 页眉 ------------------------------------------------------------------ */
.hdr{position:sticky;top:0;z-index:10;background:var(--paper);border-bottom:1px solid var(--line)}
.hdr__in{display:flex;align-items:center;gap:18px;height:56px}
.hdr__brand{display:flex;align-items:baseline;gap:12px;min-width:0}
.hdr__mark{font-size:13px;line-height:1}
.hdr__name{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.2em}
.hdr__sub{color:var(--muted)}
.hdr__right{margin-left:auto;color:var(--muted);white-space:nowrap}
.dot{display:inline-block;width:7px;height:7px;border:1px solid var(--line);
  border-radius:var(--radius-pill,999px);margin-right:7px;vertical-align:1px}
.dot--on{background:var(--ink)}
.dot--off{background:repeating-linear-gradient(45deg,var(--ink) 0 1px,transparent 1px 4px)}

/* 斜纹分隔条（点阵/工程图纸感） ---------------------------------------- */
.hatch{height:16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);
  background-image:repeating-linear-gradient(45deg,var(--ink) 0 1px,transparent 1px 5px);
  background-size:auto 100%;opacity:.55}
@media (prefers-reduced-motion:no-preference){
  .hatch{background-size:254.5585px 100%;animation:hatchFlow 32s linear infinite}
  @keyframes hatchFlow{to{background-position:-254.5585px 0}}
}

/* 视图切换 -------------------------------------------------------------- */
.tabs{display:flex;gap:8px;margin:22px 0 20px}
.tab{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  background:none;border:1px solid var(--line-soft);color:var(--muted);
  padding:8px 16px;cursor:pointer;transition:border-color var(--ease),color var(--ease)}
.tab:hover{border-color:var(--ink);color:var(--ink)}
.tab.is-on{border-color:var(--line);color:var(--ink);background:var(--paper-2)}

/* 日志查看 -------------------------------------------------------------- */
.logbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:18px 0 10px}

/* 受控写入 -------------------------------------------------------------- */
.wrow{display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:7px 0;border-bottom:1px solid var(--paper-2)}
.wk{display:flex;flex-direction:column;gap:2px;font-size:12px;color:var(--ink)}
.sel,.inp{font-family:var(--mono);font-size:11px;letter-spacing:.06em;background:var(--white);
  border:1px solid var(--line-soft);color:var(--ink);padding:7px 10px}
.sel:focus,.inp:focus{border-color:var(--line);outline:none}
.inp{width:min(240px,44vw)}
.logauto{display:flex;align-items:center;gap:6px;color:var(--ink-soft);cursor:pointer}
.logmeta{color:var(--muted);margin:0 0 8px}
.logpre{background:var(--white);border:1px solid var(--line-soft);padding:12px 14px;
  max-height:70vh;overflow:auto;font-size:11px;line-height:1.7;letter-spacing:0;
  white-space:pre-wrap;word-break:break-all;text-transform:none}
.logpre div{padding:1px 0;border-bottom:1px solid var(--paper-2)}
.logph{color:var(--muted)}
.lv--err{color:var(--alert)}
.lv--warn{color:#8a6d1a}

/* 登录门 ---------------------------------------------------------------- */
.gate{display:flex;align-items:center;justify-content:center;min-height:calc(100vh - 73px);
  padding:clamp(28px,7vh,90px) var(--pad)}
.gate__box{width:100%;max-width:560px;border:1px solid var(--line);background:var(--paper);padding:clamp(22px,3.4vw,40px)}
.gate__kicker{color:var(--muted);margin-bottom:14px}
.gate__title{font-size:clamp(24px,3.4vw,38px);font-weight:600;letter-spacing:-.02em;line-height:1.1;margin-bottom:12px}
.gate__lead{color:var(--ink-soft);font-size:14px;line-height:1.85;margin-bottom:26px}
.field{display:flex;gap:10px;align-items:stretch}
.field input{flex:1;min-width:0;height:44px;padding:0 14px;background:var(--paper);
  border:1px solid var(--line);font-family:var(--mono);font-size:13px;letter-spacing:.08em}
.field input::placeholder{color:var(--muted);letter-spacing:.16em}
.gate__err{color:var(--alert);margin-top:14px;letter-spacing:.06em;text-transform:none;
  font-family:var(--sans);font-size:12.5px;white-space:pre-wrap}

/* 按钮：悬停时黑色自左填满 --------------------------------------------- */
.btn{position:relative;display:inline-flex;align-items:center;justify-content:center;height:44px;
  padding:0 26px;border:1px solid var(--line);border-radius:999px;background:var(--paper);
  color:var(--ink);font-size:13.5px;white-space:nowrap;overflow:hidden;cursor:pointer;
  transition:color .45s var(--ease)}
.btn>span{position:relative;z-index:1}
.btn::before{content:'';position:absolute;inset:0;z-index:0;background:var(--ink);
  transform:translateX(-101%);transition:transform .55s var(--ease)}
.btn:hover::before,.btn:focus-visible::before{transform:translateX(0)}
.btn:hover,.btn:focus-visible{color:var(--paper)}
.btn--solid{background:var(--ink);color:var(--paper)}
.btn--solid::before{background:var(--paper)}
.btn--solid:hover,.btn--solid:focus-visible{color:var(--ink)}

/* 首屏数据带 ------------------------------------------------------------ */
.hero{padding:clamp(30px,4.4vw,64px) 0 clamp(24px,3vw,44px)}
.hero__lead{color:var(--muted);padding-bottom:16px;border-bottom:1px solid var(--line)}
.hero__grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(216px,1fr));
  gap:0 clamp(18px,2.4vw,44px);margin-top:clamp(18px,2.6vw,34px)}
.stat{padding:16px 0;border-bottom:1px solid var(--line-soft)}
.stat__k{color:var(--muted);margin-bottom:8px}
.stat__v{font-family:var(--mono);font-size:clamp(19px,2.5vw,29px);font-weight:500;
  letter-spacing:-.01em;line-height:1.15;word-break:break-all}
.stat__x{color:var(--muted);font-size:12px;margin-top:6px}

/* 章节标题 -------------------------------------------------------------- */
.sec{margin:clamp(34px,4.6vw,66px) 0 0}
.sec-head{display:flex;align-items:baseline;gap:16px;padding-bottom:14px;
  border-bottom:1px solid var(--line);margin-bottom:clamp(20px,2.6vw,34px)}
.sec-head__num{color:var(--muted)}
.sec-head__title{font-size:clamp(19px,2vw,26px);font-weight:600;letter-spacing:-.015em;line-height:1.1}
.sec-head__en{margin-left:auto;color:var(--muted);white-space:nowrap}
.sec__grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:clamp(14px,1.8vw,24px)}

/* 面板：1px 描边、无圆角、表头等宽大写 --------------------------------- */
.panel{border:1px solid var(--line);background:var(--paper);display:flex;flex-direction:column}
.panel__hd{display:flex;align-items:center;gap:10px;padding:11px 16px;color:var(--ink);
  border-bottom:1px solid var(--line);background:var(--paper-2)}
.panel__hd em{margin-left:auto;font-style:normal;color:var(--muted)}
.panel__bd{padding:6px 16px 14px}
.row{display:flex;align-items:baseline;justify-content:space-between;gap:16px;
  padding:9px 0;border-bottom:1px solid var(--line-soft)}
.row:last-child{border-bottom:0}
.row__k{color:var(--ink-soft);font-size:13.5px}
.row__v{font-family:var(--mono);font-size:13px;text-align:right;word-break:break-all}
.row__v--muted{color:var(--muted)}
.row--stack{display:block}
.row--stack .row__v{text-align:left;margin-top:4px;color:var(--muted);font-size:11.5px}

/* 状态标记：黑白两色表达，不用红绿 ------------------------------------- */
.chip{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;
  padding:1px 9px;font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
  white-space:nowrap}
.chip--ink{background:var(--ink);color:var(--paper)}
.chip--hatch{background-image:repeating-linear-gradient(45deg,var(--ink) 0 1px,transparent 1px 4px)}
.chip--alert{color:var(--alert);border-color:var(--alert)}
.chip--muted{color:var(--muted);border-color:var(--line-soft)}
.srow{font-size:12px}
.ctools{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:4px 0 8px}
.cnav{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}
.cbtn{font-family:var(--mono);font-size:10px;letter-spacing:.06em;cursor:pointer;
  background:var(--white);border:1px solid var(--line-soft);color:var(--muted);
  padding:6px 10px}
.cbtn:hover{color:var(--ink);border-color:var(--line)}
.cbtn.is-on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.cbtn__off{color:var(--alert);margin-left:5px;font-weight:700}
.sgrp-h{font-size:10px;letter-spacing:.1em;color:var(--muted);
  text-transform:uppercase;padding:8px 0 2px}
.is-locked{opacity:.42;filter:grayscale(1)}
.view--enter{animation:view-in .24s ease-out}
@keyframes view-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.toast-wrap{position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;
  gap:8px;z-index:99;pointer-events:none}
.toast{font-family:var(--mono);font-size:11px;letter-spacing:.04em;background:var(--ink);
  color:var(--paper);padding:10px 14px;border:1px solid var(--ink);
  box-shadow:4px 4px 0 var(--line);max-width:min(460px,80vw);
  animation:toast-in .2s ease-out;transition:opacity .22s,transform .22s}
.toast--ok{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.toast--err{background:var(--paper);color:var(--alert);border-color:var(--alert)}
.toast--out{opacity:0;transform:translateY(8px)}
.srow--flash{animation:row-flash 1.5s ease-out}
@keyframes row-flash{0%{background:var(--paper-2);box-shadow:inset 0 0 0 2px var(--ink)}
  65%{background:var(--paper-2);box-shadow:inset 0 0 0 2px var(--ink)}
  100%{background:transparent;box-shadow:none}}
.btn--busy{opacity:.5;pointer-events:none}
.modal-mask{position:fixed;inset:0;background:rgba(20,20,20,.38);display:flex;
  align-items:center;justify-content:center;z-index:100}
.modal{background:var(--paper);border:1px solid var(--ink);width:min(560px,92vw);
  max-height:86vh;display:flex;flex-direction:column;box-shadow:8px 8px 0 var(--line)}
.modal__hd{padding:12px 16px;border-bottom:1px solid var(--line);font-size:11px;
  letter-spacing:.08em}
.modal__bd{padding:14px 16px;overflow:auto}
.modal__ft{display:flex;justify-content:flex-end;gap:8px;padding:12px 16px;
  border-top:1px solid var(--line)}
.mfield{margin-bottom:12px}
.mfield__label{display:block;font-size:10px;letter-spacing:.1em;color:var(--muted);
  margin-bottom:4px}
.mfield__input,.mfield__sel{width:100%;font-family:var(--mono);font-size:12px;
  padding:8px 10px;border:1px solid var(--line-soft);background:var(--white);
  color:var(--ink);box-sizing:border-box}
.mfield__hint{color:var(--muted);font-size:11px;margin-top:4px}

/* 进度：8px 描边条 ------------------------------------------------------ */
.bar{position:relative;height:8px;border:1px solid var(--line);margin-top:10px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--ink);transition:width .9s var(--ease)}
.bar.is-over i{background-image:repeating-linear-gradient(45deg,var(--ink) 0 1px,transparent 1px 4px)}

/* 表格：细线、行悬停 ---------------------------------------------------- */
.tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.tbl th{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);font-weight:400;text-align:left;padding:8px 8px 8px 0;border-bottom:1px solid var(--line)}
.tbl td{padding:9px 8px 9px 0;border-bottom:1px solid var(--line-soft);
  font-family:var(--mono);font-size:12px;vertical-align:top}
.tbl tr:last-child td{border-bottom:0}
.tbl tbody tr{transition:background-color .35s var(--ease)}
.tbl tbody tr:hover{background:rgba(17,17,17,.028)}
.tbl .num{text-align:right;white-space:nowrap}
.tbl .kind{font-family:var(--sans);font-size:13px}

/* 事件流 ---------------------------------------------------------------- */
.ev{color:var(--muted);font-size:11.5px;font-family:var(--mono);
  max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* 错误块 ---------------------------------------------------------------- */
.errs{color:var(--alert);font-family:var(--mono);font-size:11.5px;letter-spacing:.04em;white-space:pre-wrap}
.foot{padding:clamp(26px,3.4vw,48px) 0 clamp(34px,5vw,72px);color:var(--muted);
  border-top:1px solid var(--line);margin-top:clamp(34px,4.6vw,66px)}

@media (max-width:640px){
  .hdr__sub{display:none}
  .sec-head__en{display:none}
  .hero__grid{grid-template-columns:1fr 1fr}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms!important;transition-duration:.001ms!important}
}
</style>
</head>
<body>

<header class="hdr">
  <div class="wrap hdr__in">
    <div class="hdr__brand">
      <span class="hdr__mark">&#9670;</span>
      <span class="hdr__name">AGENT-DEMO</span>
      <span class="mono hdr__sub">CONTROL / OVERVIEW · SETTINGS</span>
    </div>
    <div class="hdr__right mono" id="hdrStatus">&mdash;</div>
  </div>
</header>
<div class="hatch"></div>

<div id="login" class="gate">
  <div class="gate__box">
    <div class="mono gate__kicker">RESTRICTED / READ-ONLY</div>
    <h1 class="gate__title">agent-demo 总览</h1>
    <p class="gate__lead">输入 <span class="mono">AGENT_WEB_TOKEN</span> 进入。token 只存在当前标签页
      （sessionStorage），不进 URL、不落盘；也可以在地址后加
      <span class="mono">#token=&lt;值&gt;</span>，页面读到手即从地址栏抹除。</p>
    <div class="field">
      <input id="tok" type="password" placeholder="TOKEN" autocomplete="off" spellcheck="false">
      <button class="btn btn--solid" onclick="save()"><span>进入</span></button>
    </div>
    <div id="lerr" class="gate__err"></div>
  </div>
</div>

<main id="app" class="wrap" hidden>
  <div class="tabs mono" role="tablist">
    <button id="tabOverview" class="tab is-on" onclick="setView('overview')">总览</button>
    <button id="tabSettings" class="tab" onclick="setView('settings')">设置</button>
    <button id="tabModel" class="tab" onclick="setView('model')">模型</button>
    <button id="tabPersona" class="tab" onclick="setView('persona')">人格</button>
    <button id="tabStats" class="tab" onclick="setView('stats')">群统计</button>
    <button id="tabLogs" class="tab" onclick="setView('logs')">日志</button>
  </div>
  <div id="overviewView">
  <section class="hero">
    <div class="mono hero__lead">LIVE STATUS</div>
    <div class="hero__grid" id="hero"></div>
  </section>
  <div class="hatch"></div>
  <div id="sections"></div>
  </div>
  <div id="settingsView" hidden></div>
  <div id="modelView" hidden></div>
  <div id="personaView" hidden></div>
  <div id="statsView" hidden><div id="statsOut"></div></div>
  <div id="logsView" hidden>
    <div class="logbar">
      <select id="logFile" class="sel" onchange="logReset()"></select>
      <select id="logLevel" class="sel" onchange="logReset()">
        <option value="">全部级别</option>
        <option value="WARNING">WARNING</option>
        <option value="ERROR">ERROR</option>
        <option value="CRITICAL">CRITICAL</option>
        <option value="INFO">INFO</option>
        <option value="DEBUG">DEBUG</option>
      </select>
      <input id="logModule" class="inp mono" placeholder="模块前缀（如 agentcore.llm）" spellcheck="false" onchange="logReset()">
      <input id="logGrep" class="inp mono" placeholder="关键字（原文子串）" spellcheck="false" onchange="logReset()">
      <label class="logauto mono"><input type="checkbox" id="logAuto"> 自动刷新</label>
      <button class="btn" onclick="logReset()"><span>刷新</span></button>
      <button class="btn" onclick="logDownload()"><span>下载</span></button>
    </div>
    <div class="logmeta mono" id="logMeta">&mdash;</div>
    <div id="logOut" class="logpre mono"></div>
  </div>
  <div class="foot mono">总览只读 &middot; 设置写入需显式开启 &middot; 每 5s 刷新 &middot; <span id="ts">&mdash;</span></div>
</main>

<script>
const KEY = "agent_web_token";
function tok() { return sessionStorage.getItem(KEY) || ""; }
function save() {
  sessionStorage.setItem(KEY, document.getElementById("tok").value.trim());
  load();
}
// 支持 #token=xxx：fragment 不会发给服务器（不进反代/访问日志，比 ?token= 安全），
// 读到手就收进 sessionStorage 并立刻从地址栏抹掉，避免 token 留在历史/截屏里。
(function () {
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    sessionStorage.setItem(KEY, decodeURIComponent(m[1]));
    history.replaceState(null, "", location.pathname + location.search);
  }
})();

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function num(n) { return (n === null || n === undefined) ? "—" : Number(n).toLocaleString(); }
function pct(a, b) { return (b > 0) ? Math.min(100, Math.round(a / b * 100)) : 0; }
function row(k, v, cls) {
  return '<div class="row"><span class="row__k">' + k + '</span><span class="row__v ' + (cls || "") + '">' + v + "</span></div>";
}
function chip(t, kind) { return '<span class="chip ' + (kind || "") + '">' + t + "</span>"; }
function panel(label, note, inner) {
  return '<div class="panel"><div class="panel__hd mono">' + label +
    (note ? "<em>" + esc(note) + "</em>" : "") + '</div><div class="panel__bd">' + inner + "</div></div>";
}
function secHead(n, title, en) {
  return '<div class="sec-head"><span class="mono sec-head__num">' + n + '</span>' +
    '<span class="sec-head__title">' + title + '</span>' +
    '<span class="mono sec-head__en">' + en + "</span></div>";
}
function stat(k, v, x) {
  return '<div class="stat"><div class="mono stat__k">' + k +
    '</div><div class="stat__v">' + v + "</div>" + (x ? '<div class="stat__x">' + x + "</div>" : "") + "</div>";
}

let view = "overview";
let lastSettings = null;
function setView(v) {
  view = v;
  const tabs = [["overview", "Overview", "overviewView"], ["settings", "Settings", "settingsView"], ["model", "Model", "modelView"], ["persona", "Persona", "personaView"], ["stats", "Stats", "statsView"], ["logs", "Logs", "logsView"]];
  let target = null;
  for (const [key, tab, div] of tabs) {
    document.getElementById("tab" + tab).classList.toggle("is-on", v === key);
    const pane = document.getElementById(div);
    pane.hidden = v !== key;
    if (v === key) target = pane;
  }
  if (target) {
    target.classList.remove("view--enter");
    void target.offsetWidth; // 重排以重启动画
    target.classList.add("view--enter");
    setTimeout(() => target.classList.remove("view--enter"), 300);
  }
  if (v === "logs") logState.dirty = true; // 进入日志视图强制首刷
  load();
}

// 在途互斥：5s 轮询不等上一次 fetch，响应 >5s 时两个增量请求会带同一游标、
// 同批行追加两次（REVIEW-3ce6e0a..de09478 L2）。手动刷新撞上在途轮询时跳过
// 本次（下个 tick 自带新游标，最多晚 5s）。
let logInFlight = false;
async function load() {
  if (view === "logs") {
    if (logInFlight) return;
    logInFlight = true;
  }
  try {
    await doLoad();
  } finally {
    logInFlight = false;
  }
}
async function doLoad() {
  // 日志视图：自动刷新关闭时轮询不取数也不推进游标（手动刷新置 dirty）
  if (view === "logs" && !document.getElementById("logAuto").checked && !logState.dirty) return;
  logState.dirty = false;
  let d;
  try {
    const url = view === "settings" || view === "model" ? "api/settings" : view === "persona" ? "api/personas" : view === "logs" ? logUrl() : view === "stats" ? "api/group-stats" : "api/overview";
    const r = await fetch(url, { headers: { "Authorization": "Bearer " + tok() } });
    if (r.status === 401 || r.status === 403) {
      document.getElementById("login").hidden = false;
      document.getElementById("app").hidden = true;
      document.getElementById("lerr").textContent =
        r.status === 403 ? "来源 IP 不在允许列表内。" : "token 不正确。";
      return;
    }
    d = await r.json();
  } catch (e) {
    document.getElementById("lerr").textContent = "请求失败：" + e;
    return;
  }
  document.getElementById("login").hidden = true;
  document.getElementById("app").hidden = false;
  const when = new Date(d.generated_at * 1000);
  document.getElementById("ts").textContent = when.toLocaleString();
  if (view === "settings") { renderSettings(d); return; }
  if (view === "model") { renderModelView(d); return; }
  if (view === "persona") { renderPersonaView(d); return; }
  if (view === "logs") { await renderLogs(d); return; }
  if (view === "stats") { renderStats(d); return; }

  const l = d.llm || {};
  const degraded = l.line === "fallback";
  const online = (d.bots || []).length > 0;
  document.getElementById("hdrStatus").innerHTML =
    '<span class="dot ' + (online ? "dot--on" : "dot--off") + '"></span>' +
    (online ? "ONLINE" : "OFFLINE") + " &nbsp;/&nbsp; " + when.toLocaleTimeString();

  /* ---- 首屏数据带 ---- */
  const b = d.budget || {}, t = (b.today || {}), h = b.history || {};
  const used = t.total || 0, cap = b.daily_tokens || 0;
  let hero = "";
  hero += stat("当前模型", esc(l.active || "—"),
    degraded ? chip("备用线路", "chip--hatch") : chip("主线路", "chip--ink"));
  hero += stat("今日对话", num(used) + ' <span class="mono" style="font-size:12px;color:var(--muted)">tok</span>',
    cap ? num(cap) + " 上限 · " + pct(used, cap) + "%" + (b.enforce ? " · 硬闸" : " · 软闸") : "未设上限");
  hero += stat("今日请求", num(t.chat_requests),
    b.cost_today === null || b.cost_today === undefined ? "未配单价" : "≈ " + Number(b.cost_today).toFixed(2) + " 元");
  hero += stat("协议端", online ? (d.bots || []).length + ' <span class="mono" style="font-size:12px;color:var(--muted)">个</span>' : "0",
    online ? "reverse-WS 已连接" : "reverse-WS 未连接");
  document.getElementById("hero").innerHTML = hero;

  /* ---- 01 运行状态 ---- */
  let s1 = "";
  s1 += panel("LLM 线路",
    degraded ? "DEGRADED" : "PRIMARY",
    row("当前生效", '<span class="mono">' + esc(l.active || "—") + "</span>") +
    row("线路", degraded ? chip("备用 · 主模型不可用", "chip--alert") : chip("主线路", "chip--ink")) +
    row("主模型", '<span class="mono">' + esc(l.primary || "—") + "</span>") +
    row("备用模型", l.fallback ? '<span class="mono">' + esc(l.fallback) + "</span>" : "未配置", "row__v--muted"));
  if (cap) {
    const p = pct(used, cap);
    s1 += panel("今日预算", p + "%",
      row("已用", num(used) + " / " + num(cap)) +
      row("闸门", b.enforce ? chip("硬闸 · 超限拦截", "chip--hatch") : "软闸 · 仅告警") +
      '<div class="bar' + (p >= 100 ? " is-over" : "") + '"><i style="width:' + p + '%"></i></div>');
  }
  s1 += panel("运行组件", null,
    row("记忆后端", '<span class="mono">' + esc(d.memory_backend || "未初始化") + "</span>") +
    row("默认人格", esc(d.persona_default || "（无）")) +
    row("已装 skill", num((d.skills || []).length)) +
    (d.flags ? row("群上下文", d.flags.group_context ? chip("开启", "chip--ink") : "关闭") +
      row("vision", d.flags.vision ? chip("开启", "chip--ink") : "关闭") : ""));
  s1 += panel("协议端", online ? "ONLINE" : "OFFLINE",
    online ? (d.bots || []).map(x => row("bot " + esc(x.self_id), chip("已连接", "chip--ink"))).join("")
      : row("状态", chip("未连接", "chip--alert")));

  /* ---- 02 用量 ---- */
  let s2 = "";
  s2 += panel("今日构成", t.date || null,
    row("prompt", num(t.prompt)) +
    row("completion", num(t.completion)) +
    row("合计", num(t.total)) +
    row("embedding", num(t.embedding_tokens) + " tok / " + num(t.embedding_requests) + " 次", "row__v--muted"));
  s2 += panel("历史累计",
    h.chat_requests ? num(h.chat_requests) + " 次请求" : null,
    row("对话 token", num(h.total)) +
    row("prompt / completion", num(h.prompt) + " / " + num(h.completion)) +
    row("embedding token", num(h.embedding_tokens), "row__v--muted"));
  const models = Object.entries(t.by_model || {})
    .sort((a, c) => (c[1].prompt + c[1].completion) - (a[1].prompt + a[1].completion));
  s2 += panel("今日按模型", models.length ? models.length + " 个" : null,
    models.length
      ? '<table class="tbl"><thead><tr><th>model</th><th class="num">prompt</th>' +
        '<th class="num">completion</th><th class="num">req</th></tr></thead><tbody>' +
        models.map(([m, v]) => "<tr><td>" + esc(m) + '</td><td class="num">' + num(v.prompt) +
          '</td><td class="num">' + num(v.completion) + '</td><td class="num">' + num(v.requests) +
          "</td></tr>").join("") + "</tbody></table>"
      : '<div class="row row__v--muted">今日暂无调用</div>');
  const routes = Object.entries(t.by_route || {})
    .sort((a, c) => (c[1].prompt + c[1].completion) - (a[1].prompt + a[1].completion)).slice(0, 8);
  s2 += panel("今日按路由", routes.length ? "TOP " + routes.length : null,
    routes.length
      ? '<table class="tbl"><thead><tr><th>route</th><th class="num">tok</th>' +
        '<th class="num">req</th></tr></thead><tbody>' +
        routes.map(([k, v]) => "<tr><td>" + esc(k) + '</td><td class="num">' +
          num(v.prompt + v.completion) + '</td><td class="num">' + num(v.requests) +
          "</td></tr>").join("") + "</tbody></table>"
      : '<div class="row row__v--muted">今日暂无调用</div>');

  /* ---- 03 知识库 ---- */
  let s3 = "";
  if (d.kb) {
    const ks = d.kb.stats || {}, kd = d.kb.describe || {};
    s3 += panel("知识库", kd.enabled ? "ENABLED" : "DISABLED",
      row("状态", kd.enabled ? chip("启用", "chip--ink") : "关闭") +
      row("chunks", num(ks.chunks)) +
      row("来源数", num(ks.sources)) +
      row("top_k / 阈值", num(kd.top_k) + " / " + (kd.threshold === null || kd.threshold === undefined ? "—" : kd.threshold)) +
      row("向量", kd.embedding === "on" ? chip("开启", "chip--ink") : "关闭") +
      row("蒸馏 cron", '<span class="mono">' + esc(kd.digest_cron || "—") + "</span>", "row__v--muted"));
  } else {
    s3 += panel("知识库", null, '<div class="row row__v--muted">未初始化</div>');
  }
  const skills = d.skills || [];
  const pub = skills.filter(s => s.permission === "public").length;
  s3 += panel("已注册 skill", skills.length + " 个",
    row("public", num(pub)) +
    row("非 public", num(skills.length - pub)) +
    row("清单", skills.map(s => '<span class="mono">' + esc(s.name) + "</span>").join(" &middot; "), "row--stack"));

  /* ---- 04 事件 ---- */
  const evs = d.recent_events || [];
  let s4 = panel("最近事件", evs.length ? evs.length + " 条" : null,
    evs.length
      ? '<table class="tbl"><thead><tr><th>time</th><th>kind</th><th>detail</th></tr></thead><tbody>' +
        evs.map(e => {
          const detail = Object.fromEntries(Object.entries(e).filter(([k]) => k !== "ts" && k !== "kind"));
          return "<tr><td>" + new Date(e.ts * 1000).toLocaleTimeString() + "</td>" +
            '<td class="kind">' + esc(e.kind) + "</td>" +
            '<td><div class="ev" title="' + esc(JSON.stringify(detail)) + '">' +
            esc(JSON.stringify(detail)) + "</div></td></tr>";
        }).join("") + "</tbody></table>"
      : '<div class="row row__v--muted">暂无事件</div>');
  if (d.errors && d.errors.length) {
    s4 += panel("取数失败的部分", d.errors.length + " 项",
      '<div class="errs">' + d.errors.map(esc).join("\n") + "</div>");
  }

  document.getElementById("sections").innerHTML =
    '<section class="sec">' + secHead("01", "运行状态", "RUNTIME") +
      '<div class="sec__grid">' + s1 + "</div></section>" +
    '<section class="sec">' + secHead("02", "用量与成本", "USAGE") +
      '<div class="sec__grid">' + s2 + "</div></section>" +
    '<section class="sec">' + secHead("03", "知识与技能", "KNOWLEDGE") +
      '<div class="sec__grid">' + s3 + "</div></section>" +
    '<section class="sec">' + secHead("04", "事件", "EVENTS") +
      '<div class="sec__grid">' + s4 + "</div></section>";
}
/* ---- 设置视图：只读展示当前生效配置；受控写入面板（D2-1）---- */
function mono(s) {
  return '<span class="mono">' + esc(s === null || s === undefined || s === "" ? "—" : s) + "</span>";
}
function boolRow(k, v) {
  if (v === null || v === undefined) return row(k, "—");
  return row(k, v ? chip("开启", "chip--ink") : "关闭");
}
let SETTINGS_INDEX = new Map();
function indexSettings(d) {
  const m = new Map();
  const put = items => (items || []).forEach(it => m.set(it.key, it));
  ((d.settings || {}).groups || []).forEach(g => put(g.items));
  ((d.config || {}).groups || []).forEach(g => put(g.items));
  SETTINGS_INDEX = m;
}
function fmtSetting(it) {
  const v = it.value;
  if (v === null || v === undefined) return it.default ? "（未设置，默认 " + it.default + "）" : "（未设置）";
  if (Array.isArray(v)) return v.length ? v.join(",") : "（空）";
  if (String(v) === "") return it.default ? "（空，默认 " + it.default + "）" : "（空）";
  return String(v);
}
function settingRow(it, cluster) {
  const isGate = cluster && cluster.gate_key === it.key;
  const locked = cluster && !cluster.enabled && !isGate;
  const eff = it.live ? chip("即时", "chip--ink") : chip("重启生效", "chip--hatch");
  const ro = it.legacy
    ? chip("已迁移", "chip--muted")
    : (it.writable ? "" : chip("只读", "chip--muted"));
  const btn = locked
    ? '<span class="row__v--muted" style="font-size:11px">簇已关闭</span>'
    : it.legacy
      ? '<span class="row__v--muted" style="font-size:11px">已迁移 → Providers</span>'
      : it.writable
        ? '<button class="btn" onclick="editKey(\'' + it.key + '\')"><span>编辑</span></button>'
        : '<span class="row__v--muted" style="font-size:11px">SSH</span>';
  const hay = (it.key + " " + (it.label || "") + " " + (it.description || "")).toLowerCase();
  const rowOpen = locked
    ? '<div class="wrow srow is-locked" data-k="'
    : '<div class="wrow srow" data-k="';
  return rowOpen + esc(hay) + '">' +
    '<span class="wk"><b>' + esc(it.label || it.key) + '</b> ' +
    '<span style="color:var(--muted)">' + esc(it.key) + '</span>' +
    '<span style="color:var(--muted);font-size:11px">' + esc(it.description || "") + '</span></span>' +
    '<span style="display:flex;gap:6px;align-items:center;flex-shrink:0">' + eff + ro +
    '<span class="mono" style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(fmtSetting(it)) + '</span>' +
    btn + '</span></div>';
}
function toast(msg, kind) {
  let wrap = document.getElementById("toasts");
  if (!wrap) {
    document.body.insertAdjacentHTML("beforeend", '<div id="toasts" class="toast-wrap"></div>');
    wrap = document.getElementById("toasts");
  }
  const el = document.createElement("div");
  el.className = "toast " + (kind === "err" ? "toast--err" : "toast--ok");
  el.textContent = String(msg);
  wrap.appendChild(el);
  setTimeout(() => {
    el.classList.add("toast--out");
    setTimeout(() => el.remove(), 240);
  }, 2800);
}
function flashRow(pid) {
  const el = document.querySelector('[data-pid="' + pid + '"]');
  if (!el) return;
  el.classList.add("srow--flash");
  setTimeout(() => el.classList.remove("srow--flash"), 1600);
}
function setBusy(btn, text) {
  if (!btn) return () => {};
  const html = btn.innerHTML;
  btn.classList.add("btn--busy");
  btn.innerHTML = "<span>" + text + "</span>";
  return () => {
    btn.classList.remove("btn--busy");
    btn.innerHTML = html;
  };
}
async function doPost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Authorization": "Bearer " + tok() },
    body: JSON.stringify(body),
  });
  return { status: r.status, data: await r.json().catch(() => ({})) };
}
function providerPanel(d) {
  const l = d.llm || {};
  const ps = l.providers || [];
  const activeId = l.active_id || "";
  const rows = ps.map((p, i) => {
    const cur = p.id === activeId;
    const meta = esc(p.protocol) + " · " + esc(p.endpoint || "") + " · " + esc(p.model) +
      (p.key_set ? " · key 已设置" : " · 无 key") + (p.enabled ? "" : " · 已停用");
    return '<div class="wrow" data-pid="' + esc(p.id) + '"><span class="wk"><b>' + esc(p.name) + '</b> ' +
      '<span style="color:var(--muted)">' + esc(p.id) + '</span>' +
      '<span style="color:var(--muted);font-size:11px">' + meta + '</span></span>' +
      '<span style="display:flex;gap:6px;align-items:center">' +
      (cur ? chip("当前", "chip--ink")
        : (p.enabled
          ? '<button class="btn" onclick="providerAct(event,\'set_active\',\'' + p.id + '\')"><span>设为主</span></button>'
          : chip("已停用", "chip--muted"))) +
      '<button class="btn" onclick="providerTest(event,\'' + p.id + '\')"><span>测试</span></button>' +
      '<button class="btn" onclick="providerEdit(\'' + p.id + '\')"><span>编辑</span></button>' +
      (i > 0 ? '<button class="btn" onclick="providerMove(\'' + p.id + '\',-1)"><span>↑</span></button>' : '') +
      (i < ps.length - 1 ? '<button class="btn" onclick="providerMove(\'' + p.id + '\',1)"><span>↓</span></button>' : '') +
      '<button class="btn" onclick="providerAct(event,\'delete\',\'' + p.id + '\')"><span>删除</span></button>' +
      "</span></div>";
  }).join("");
  return '<div class="sgrp-h">LLM Providers（' + ps.length + '）</div>' +
    (rows || '<div class="row row__v--muted">尚未配置 provider</div>') +
    '<div class="wrow"><span class="wk">新增 provider' +
    '<span style="color:var(--muted);font-size:11px">OpenAI / Anthropic；key 只写不回显</span></span>' +
    '<button class="btn btn--solid" onclick="providerEdit()"><span>新增</span></button></div>';
}
async function providerPost(body) {
  let res = await doPost("api/settings/llm/providers", body);
  if (res.status !== 200) {
    toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err");
    return null;
  }
  if (res.data.need_confirm) {
    const pv = res.data.preview || {};
    const ok = await confirmDialog(
      "确认 " + (res.data.action || "provider") + "？",
      '<div class="mfield"><span class="mfield__label">' + esc(res.data.action || "provider") + "</span>" +
        '<div class="mfield__hint">' + esc(JSON.stringify(pv)) + "</div>" +
        '<div class="mfield__hint">key 不回显</div></div>',
      "确认");
    if (!ok) return null;
    res = await doPost("api/settings/llm/providers", {
      ...body,
      confirm_token: res.data.confirm_token,
      confirm_nonce: res.data.confirm_nonce,
    });
    if (res.status !== 200) {
      toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err");
      return null;
    }
  }
  return res.data;
}
async function providerTest(ev, id) {
  const done = setBusy(ev && ev.target && ev.target.closest("button"), "测试中…");
  const res = await doPost("api/settings/llm/providers", { action: "test", id: id });
  done();
  if (res.status !== 200) { toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err"); return; }
  toast(res.data.ok ? "连接正常：" + id : res.data.detail, res.data.ok ? "ok" : "err");
}
async function providerAct(ev, action, id) {
  const ps = (lastSettings.llm || {}).providers || [];
  const p = ps.find(x => x.id === id);
  const label = p ? (p.name + " / " + p.model) : id;
  const verb = action === "set_active" ? "切换中…" : action === "delete" ? "删除中…" : "保存中…";
  const done = setBusy(ev && ev.target && ev.target.closest("button"), verb);
  const data = await providerPost({ action: action, id: id });
  done();
  if (!data || !data.ok) return;
  await load();
  if (action === "set_active") {
    toast("已切换主模型 → " + label);
    flashRow(id);
  } else if (action === "delete") {
    toast("已删除 provider：" + id);
  } else {
    toast("已更新 provider：" + id);
  }
}
async function providerMove(id, dir) {
  const ps = (lastSettings.llm || {}).providers || [];
  const ids = ps.map(p => p.id);
  const i = ids.indexOf(id), j = i + dir;
  if (i < 0 || j < 0 || j >= ids.length) return;
  const tmp = ids[i]; ids[i] = ids[j]; ids[j] = tmp;
  const data = await providerPost({ action: "reorder", ids: ids });
  if (data && data.ok) { await load(); toast("已更新 provider 顺序"); }
}
let modalOnSave = null;
let modalResolve = null;
function _removeModal() {
  const m = document.getElementById("modalMask");
  if (m) m.remove();
}
function closeModal() {
  _removeModal();
  modalOnSave = null;
  const r = modalResolve;
  modalResolve = null;
  if (r) r(false);
}
function confirmDialog(title, body, label) {
  return new Promise(resolve => {
    closeModal();
    modalResolve = resolve;
    document.body.insertAdjacentHTML("beforeend",
      '<div id="modalMask" class="modal-mask"><div class="modal">' +
        '<div class="modal__hd mono">' + esc(title) + "</div>" +
        '<div class="modal__bd">' + body + "</div>" +
        '<div class="modal__ft">' +
          '<button class="btn" onclick="closeModal()"><span>取消</span></button>' +
          '<button class="btn btn--solid" id="modalOk"><span>' + esc(label || "确认") + "</span></button>" +
        "</div></div></div>");
    const mask = document.getElementById("modalMask");
    mask.addEventListener("click", e => { if (e.target === mask) closeModal(); });
    document.getElementById("modalOk").onclick = () => {
      _removeModal();
      modalOnSave = null;
      const r = modalResolve;
      modalResolve = null;
      if (r) r(true);
    };
  });
}
function openModal(title, body, onSave) {
  closeModal();
  document.body.insertAdjacentHTML("beforeend",
    '<div id="modalMask" class="modal-mask"><div class="modal">' +
      '<div class="modal__hd mono">' + esc(title) + "</div>" +
      '<div class="modal__bd">' + body + "</div>" +
      '<div class="modal__ft">' +
        '<button class="btn" onclick="closeModal()"><span>取消</span></button>' +
        '<button class="btn btn--solid" id="modalSave"><span>保存</span></button>' +
      "</div></div></div>");
  const mask = document.getElementById("modalMask");
  mask.addEventListener("click", (e) => { if (e.target === mask) closeModal(); });
  modalOnSave = onSave;
  document.getElementById("modalSave").onclick = async () => {
    if (modalOnSave) await modalOnSave();
  };
}
function field(label, html, hint) {
  return '<div class="mfield"><span class="mfield__label">' + esc(label) + "</span>" + html +
    (hint ? '<div class="mfield__hint">' + esc(hint) + "</div>" : "") + "</div>";
}
function providerEdit(id) {
  const ps = (lastSettings.llm || {}).providers || [];
  const p = ps.find(x => x.id === id) ||
    { name: "", protocol: "openai", endpoint: "", model: "", enabled: true, key_set: false, temperature: null, max_tokens: null };
  const sel = (v, want) => (v === want ? " selected" : "");
  const body =
    field("展示名", '<input id="pvName" class="mfield__input" value="' + esc(p.name) + '">') +
    field("协议",
      '<select id="pvProtocol" class="mfield__sel">' +
        '<option value="openai"' + sel(p.protocol, "openai") + ">OpenAI 兼容（/chat/completions）</option>" +
        '<option value="anthropic"' + sel(p.protocol, "anthropic") + ">Anthropic Messages（/v1/messages）</option>" +
      "</select>") +
    field("端点", '<input id="pvEndpoint" class="mfield__input" value="' + esc(p.endpoint || "") + '" placeholder="https://api.stepfun.com/v1">') +
    field("模型名", '<input id="pvModel" class="mfield__input" value="' + esc(p.model) + '">') +
    field("API key",
      '<input id="pvKey" class="mfield__input" type="password" value="" placeholder="' +
        (p.key_set ? "已设置；留空 = 保持不变" : "只写不回显") + '">') +
    field("启用",
      '<select id="pvEnabled" class="mfield__sel">' +
        '<option value="1"' + (p.enabled ? " selected" : "") + ">启用</option>" +
        '<option value="0"' + (!p.enabled ? " selected" : "") + ">停用</option>" +
      "</select>") +
    field("温度（可选）", '<input id="pvTemp" class="mfield__input" value="' + esc(p.temperature == null ? "" : p.temperature) + '" placeholder="留空 = 使用全局默认">') +
    field("max_tokens（可选）", '<input id="pvMaxTokens" class="mfield__input" value="' + esc(p.max_tokens == null ? "" : p.max_tokens) + '" placeholder="留空 = 使用全局默认">');
  openModal(id ? "编辑 Provider · " + p.name : "新增 Provider", body, async () => {
    const name = document.getElementById("pvName").value.trim();
    const protocol = document.getElementById("pvProtocol").value;
    const endpoint = document.getElementById("pvEndpoint").value.trim();
    const model = document.getElementById("pvModel").value.trim();
    const key = document.getElementById("pvKey").value;
    const enabled = document.getElementById("pvEnabled").value === "1";
    const temp = document.getElementById("pvTemp").value.trim();
    const maxTokens = document.getElementById("pvMaxTokens").value.trim();
    if (!name || !endpoint || !model) { toast("展示名 / 端点 / 模型名不能为空", "err"); return; }
    const provider = { name: name, protocol: protocol, endpoint: endpoint, model: model, enabled: enabled };
    if (id) provider.id = id;
    if (key) provider.api_key = key;
    if (temp !== "") provider.temperature = temp;
    if (maxTokens !== "") provider.max_tokens = maxTokens;
    const data = await providerPost({ action: "save", provider: provider });
    if (data && data.ok) {
      closeModal();
      await load();
      toast("已保存 provider：" + name);
    }
  });
}
function clusterBody(c) {
  const gate = c.gate_key ? c.items.find(it => it.key === c.gate_key) : null;
  const rest = c.items.filter(it => !gate || it.key !== gate.key);
  const groups = new Map();
  rest.forEach(it => {
    const g = it.group || "其他";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(it);
  });
  let html = (c.id === "llm"
    ? '<div class="wrow" style="border-bottom:none"><span class="wk">LLM Provider 管理' +
      '<span style="color:var(--muted);font-size:11px">已移至顶层「模型」标签（数据与接口同源）</span></span>' +
      '<button class="btn" onclick="setView(\'model\')"><span>前往模型</span></button></div>'
    : "") +
    (gate ? settingRow(gate, c) : "");
  groups.forEach((items, name) => {
    html += '<div class="sgrp-h">' + esc(name) + "</div>" +
      items.map(it => settingRow(it, c)).join("");
  });
  return html;
}
let CLUSTERS = [];
let currentCluster = "llm";
let clusterSearch = "";
function itemHay(it) {
  return (it.key + " " + (it.label || "") + " " + (it.description || "")).toLowerCase();
}
function renderClusterNav() {
  const nav = document.getElementById("clusterNav");
  if (!nav) return;
  nav.innerHTML = CLUSTERS.map(c => {
    const on = c.id === currentCluster;
    const open = on ? '<button class="cbtn is-on"' : '<button class="cbtn"';
    const off = c.gate_key && c.gate_value === false
      ? '<span class="cbtn__off">关</span>' : "";
    return open + ' onclick="selectCluster(\'' + c.id + '\')">' +
      esc(c.title) + off + "</button>";
  }).join("");
}
function renderClusterPanel() {
  const box = document.getElementById("clusterPanel");
  if (!box) return;
  if (clusterSearch) {
    const q = clusterSearch.toLowerCase();
    const hits = [];
    CLUSTERS.forEach(c => {
      const clusterHit = (c.title + " " + (c.description || "")).toLowerCase().includes(q);
      c.items.forEach(it => {
        if (clusterHit || itemHay(it).includes(q)) hits.push([it, c]);
      });
    });
    const byCluster = new Map();
    hits.forEach(([it, c]) => {
      if (!byCluster.has(c.id)) byCluster.set(c.id, { c: c, items: [] });
      byCluster.get(c.id).items.push(it);
    });
    box.innerHTML = panel("搜索结果", num(hits.length) + " 项", hits.length
      ? Array.from(byCluster.values()).map(({ c, items }) =>
          '<div class="sgrp-h">' + esc(c.title) + "（" + items.length + "）</div>" +
          items.map(it => settingRow(it, c)).join("")
        ).join("")
      : '<div class="row row__v--muted">没有匹配的设置项（可试键名或说明）。</div>');
    return;
  }
  const c = CLUSTERS.find(x => x.id === currentCluster) || CLUSTERS[0];
  if (!c) { box.innerHTML = ""; return; }
  const meta = num(c.total) + " 项 · 可写 " + num(c.writable) +
    (c.gate_key ? (c.enabled ? " · 已开启" : " · 簇已关闭") : "");
  box.innerHTML = panel(c.title, meta, clusterBody(c));
}
function selectCluster(id) {
  currentCluster = id;
  clusterSearch = "";
  const inp = document.getElementById("settingSearch");
  if (inp) inp.value = "";
  renderClusterNav();
  renderClusterPanel();
}
function initClusters(d) {
  CLUSTERS = d.clusters || [];
  if (!CLUSTERS.find(c => c.id === currentCluster)) {
    currentCluster = (CLUSTERS[0] || {}).id || "";
  }
  const inp = document.getElementById("settingSearch");
  if (inp && inp.value !== clusterSearch) inp.value = clusterSearch;
  renderClusterNav();
  renderClusterPanel();
}
function filterSettings(q) {
  clusterSearch = (q || "").trim();
  renderClusterPanel();
}
function settingEditor(it) {
  const raw = (it.value === null || it.value === undefined) ? "" : String(it.value);
  // 未设置时预选默认值：否则下拉会落到第一项（例如温度默认 0.7 会显示成 0）
  const cur = raw !== "" ? raw : String(it.default == null ? "" : it.default);
  if (it.kind === "bool") {
    const on = !["0", "false", "no", "off", ""].includes(cur.trim().toLowerCase());
    return '<select id="modalValue" class="mfield__sel">' +
      '<option value="true"' + (on ? " selected" : "") + ">开</option>" +
      '<option value="false"' + (!on ? " selected" : "") + ">关</option>" +
      "</select>";
  }
  if (it.options && it.options.length) {
    const opts = it.options.slice();
    if (cur && !opts.includes(cur)) opts.push(cur);
    return '<select id="modalValue" class="mfield__sel" onchange="toggleCustom(this)">' +
      opts.map(o => '<option value="' + esc(o) + '"' + (o === cur ? " selected" : "") + ">" + esc(o) + "</option>").join("") +
      '<option value="__custom">自定义…</option></select>' +
      '<input id="modalCustom" class="mfield__input" style="margin-top:6px;display:none" value="' + esc(cur) + '">';
  }
  return '<input id="modalValue" class="mfield__input" value="' + esc(cur) + '">';
}
function toggleCustom(sel) {
  const inp = document.getElementById("modalCustom");
  if (!inp) return;
  inp.style.display = sel.value === "__custom" ? "" : "none";
  if (sel.value === "__custom") inp.focus();
}
function readSettingValue() {
  const el = document.getElementById("modalValue");
  let v = el ? el.value : "";
  if (v === "__custom") {
    const c = document.getElementById("modalCustom");
    v = c ? c.value : "";
  }
  return v;
}
async function writeSetting(key, value) {
  let res = await doPost("api/settings/write", { key: key, value: value });
  if (res.status !== 200) {
    toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err");
    return false;
  }
  if (res.data.need_confirm) {
    const tail = res.data.effective === "live" ? "保存后立即生效。" : "保存后需重启才生效。";
    const ok = await confirmDialog(
      "确认写入",
      '<div class="mfield"><span class="mfield__label">' + esc(key) + "</span>" +
        '<div class="mfield__hint">' + esc(String(res.data.old ?? "（未设置）")) +
        " → " + esc(String(res.data.new)) + "</div>" +
        '<div class="mfield__hint">' + esc(tail) + "</div></div>",
      "确认写入");
    if (!ok) return false;
    res = await doPost("api/settings/write", {
      key: key, value: value,
      confirm_token: res.data.confirm_token,
      confirm_nonce: res.data.confirm_nonce,
    });
  }
  if (res.status === 200 && res.data.ok) {
    toast((res.data.effective === "live" ? "已生效：" : "已保存（重启后生效）：") + key);
    load();
    return true;
  }
  toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err");
  return false;
}
function editKey(key) {
  const it = SETTINGS_INDEX.get(key);
  if (!it) { toast("未知设置项：" + key, "err"); return; }
  const body =
    field(it.label || it.key,
      '<div class="mfield__hint"><span class="mono">' + esc(it.key) + "</span> · " +
        (it.live ? "即时生效" : "重启生效") + " · 默认 " + esc(it.default || "—") + "</div>" +
      (it.description ? '<div class="mfield__hint">' + esc(it.description) + "</div>" : ""),
      "") +
    field("值", settingEditor(it));
  openModal("修改 · " + (it.label || it.key), body, async () => {
    const v = readSettingValue();
    if (v === "") { toast("值不能为空", "err"); return; }
    closeModal(); // 先收起编辑表单，再弹页内二次确认
    await writeSetting(key, v);
  });
}
async function rebootBot() {
  const ok = await confirmDialog("确认重启 bot？",
    '<div class="mfield"><div class="mfield__hint">会先保存状态（防抖 flush + 归档），重启期间无法应答。</div></div>',
    "继续");
  if (!ok) return;
  let res = await doPost("api/reboot", { reason: "web-ui" });
  if (res.status !== 200) {
    toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err");
    return;
  }
  if (res.data.need_confirm) {
    const ok2 = await confirmDialog("再次确认：立即重启",
      '<div class="mfield"><div class="mfield__hint">方式：' + esc(res.data.strategy) + "</div>" +
        '<div class="mfield__hint">恢复时间取决于看门狗（通常几秒）。</div></div>',
      "立即重启");
    if (!ok2) return;
    res = await doPost("api/reboot", {
      confirm_token: res.data.confirm_token,
      confirm_nonce: res.data.confirm_nonce,
      reason: "web-ui",
    });
  }
  if (res.status === 200 && res.data.ok) {
    toast("重启指令已接受（" + res.data.strategy + "），页面将在数秒内断开……");
  } else {
    toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err");
  }
}
// 「模型」标签：LLM provider 管理面板。数据与接口和设置页完全同源
// （GET /api/settings 的 llm 块 + POST /api/settings/llm/providers）——
// 2026-10-20 起设置 llm 簇只留跳转指引，管理入口收敛到这一处，避免双入口漂移。
function renderModelView(d) {
  lastSettings = d;
  const el = document.getElementById("modelView");
  if (!el) return;
  const l = d.llm || {};
  const gate = d.write_enabled ? "" :
    '<div class="row row__v--muted">受控写入未启用：可查看 provider 列表，新增/编辑/删除需配置 ' +
    'AGENT_WEB_ALLOW_CIDRS 且 AGENT_WEB_WRITE=1（改后重启生效）。</div>';
  el.innerHTML =
    '<section class="sec">' + secHead("M1", "对话模型", "MODELS") +
      '<div class="sec__grid">' +
        panel("当前线路", l.line === "fallback" ? "DEGRADED" : "PRIMARY",
          row("当前生效", mono(l.active)) +
          row("线路", l.line === "fallback"
            ? chip("备用 · 主模型不可用", "chip--alert")
            : chip("主线路", "chip--ink")) +
          row("主模型", mono(l.primary)) +
          row("备用模型", l.fallback ? mono(l.fallback) : "未配置", "row__v--muted")) +
      "</div></section>" +
    '<section class="sec">' + secHead("M2", "Providers", "CRUD") +
      '<div class="sec__grid">' + panel("LLM Providers", (l.providers || []).length + " 个",
        gate + providerPanel(d)) +
      "</div></section>";
}
// 「人格」标签：导入 / 编辑 / 删除 persona（写面门禁 + 两段确认，同 provider）。
// 表单化导入：.md 文件只做前端预填（解析 frontmatter），最终保存走后端
// render_persona_md 校验——前端解析填错最多填错表单，落盘内容以后端为准。
//
// **表单只建一次、列表局部刷新**：5s 轮询的 load() 会重进本函数，整体重写
// innerHTML 会把用户正在填的表单清空（2026-10-20 实测反馈：填到一半每几秒
// 被清）。provider 面板没这问题因为它的表单在 modal 里（挂在 body 下）。
function renderPersonaView(d) {
  const el = document.getElementById("personaView");
  if (!el) return;
  if (!el.dataset.built) {
    el.innerHTML =
      '<section class="sec">' + secHead("P1", "导入 / 编辑人格", "IMPORT") +
        '<div class="sec__grid">' + panel("新建人格", "frontmatter 自动生成 · 同名覆盖自动备份",
          '<div class="mfield"><span class="mfield__label">名字（字母/数字/_-，即文件名）</span>' +
            '<input id="psName" class="mfield__input" placeholder="如 fortune_teller" spellcheck="false"></div>' +
          '<div class="mfield"><span class="mfield__label">描述（/persona list 展示，≤500 字）</span>' +
            '<input id="psDesc" class="mfield__input" placeholder="玄学顾问人格"></div>' +
          '<div class="mfield"><span class="mfield__label">正文（注入 system prompt 的行为指南，≤20000 字）</span>' +
            '<textarea id="psBody" class="mfield__input" rows="10" placeholder="现在你是一位……"></textarea></div>' +
          '<div class="mfield"><span class="mfield__label">默认人格</span>' +
            '<select id="psDefault" class="mfield__sel"><option value="0">否</option><option value="1">是</option></select>' +
            '<div class="mfield__hint">多个 default 时按名称排序取第一个；本操作不改其它文件</div></div>' +
          '<div class="mfield"><span class="mfield__label">从 .md 文件填入（解析 frontmatter 预填表单）</span>' +
            '<input id="psFile" type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" class="inp" onchange="personaFillFromFile(this)"></div>' +
          '<div class="wrow" style="border-bottom:none"><span class="wk">保存' +
            '<span style="color:var(--muted);font-size:11px">同名覆盖（自动备份旧文件）· 两段确认</span></span>' +
            '<button class="btn btn--solid" onclick="personaSave()"><span>保存</span></button></div>') +
        "</div></section>" +
      '<section class="sec">' + secHead("P2", "已有的人格", "LIST") +
        '<div class="sec__grid"><div id="personaLists"></div></div></section>' +
      '<section class="sec">' + secHead("P3", "群人格", "GROUP") +
        '<div class="sec__grid">' + panel("设置群人格", "群内所有人共享 · 优先级最高 · 立即生效",
          '<div class="mfield"><span class="mfield__label">群号（ASCII 数字）</span>' +
            '<input id="pgGroup" class="mfield__input" list="psGroupList" placeholder="如 123456789" spellcheck="false">' +
            '<datalist id="psGroupList"></datalist>' +
            '<div class="mfield__hint">下拉建议来自已知会话群与群统计白名单</div></div>' +
          '<div class="mfield"><span class="mfield__label">人格</span>' +
            '<select id="pgPersona" class="mfield__sel"></select></div>' +
          '<div class="wrow" style="border-bottom:none"><span class="wk">保存' +
            '<span style="color:var(--muted);font-size:11px">覆盖已有配置 · 两段确认</span></span>' +
            '<button class="btn btn--solid" onclick="personaGroupSave()"><span>保存</span></button></div>') +
        "</div></section>" +
      '<section class="sec">' + secHead("P4", "已配置的群人格", "LIST") +
        '<div class="sec__grid"><div id="personaGroups"></div></div></section>';
    el.dataset.built = "1";
  }
  renderPersonaList(d);
  renderPersonaGroups(d);
}

// 群人格区块的局部刷新：下拉建议 / 人格选项 / 已配置列表。轮询只更新这些，
// 不碰用户正在填的表单（同 P1 的原因）。
function renderPersonaGroups(d) {
  const groups = d.group_personas || [];
  const dl = document.getElementById("psGroupList");
  if (dl) {
    dl.innerHTML = (d.known_groups || [])
      .map(g => '<option value="' + esc(g) + '"></option>').join("");
  }
  const sel = document.getElementById("pgPersona");
  if (sel) {
    const ps = d.personas || [];
    const cur = sel.value;
    const sig = ps.map(p => p.name).join(",");
    // 只有人格列表变化才重建 options，避免轮询打断正在展开的下拉
    if (sel.dataset.sig !== sig) {
      sel.innerHTML = ps.map(p => '<option value="' + esc(p.name) + '">' +
        esc(p.name) + "</option>").join("");
      sel.dataset.sig = sig;
    }
    if (cur && ps.some(p => p.name === cur)) sel.value = cur;
  }
  const box = document.getElementById("personaGroups");
  if (!box) return;
  const gate = d.write_enabled ? "" :
    '<div class="row row__v--muted">受控写入未启用：群人格配置需配置 ' +
    'AGENT_WEB_ALLOW_CIDRS 且 AGENT_WEB_WRITE=1（改后重启生效）。</div>';
  const rows = groups.map(g =>
    '<div class="wrow" data-gid="' + esc(g.group_id) + '"><span class="wk"><b>' +
      esc(g.group_id) + "</b> " +
      '<span style="color:var(--muted);font-size:11px">→ ' + esc(g.persona) + "</span></span>" +
      '<span><button class="btn" onclick="personaGroupDelete(\'' + esc(g.group_id) +
      '\')"><span>清除</span></button></span></div>').join("");
  box.innerHTML = panel("群人格配置", groups.length + " 个",
    gate + (rows || '<div class="row row__v--muted">尚未配置群人格</div>'));
}
function renderPersonaList(d) {
  const box = document.getElementById("personaLists");
  if (!box) return;
  const ps = d.personas || [];
  const gate = d.write_enabled ? "" :
    '<div class="row row__v--muted">受控写入未启用：可查看人格列表，导入/编辑/删除需配置 ' +
    'AGENT_WEB_ALLOW_CIDRS 且 AGENT_WEB_WRITE=1（改后重启生效）。</div>';
  const rows = ps.map(p =>
    '<div class="wrow" data-pname="' + esc(p.name) + '"><span class="wk"><b>' + esc(p.name) + "</b> " +
      (p.default ? chip("默认", "chip--ink") : "") +
      '<span style="color:var(--muted);font-size:11px">' + esc(p.description || "（无描述）") +
      (p.mtime ? " · " + new Date(p.mtime * 1000).toLocaleString() : "") + "</span></span>" +
      '<span style="display:flex;gap:6px;align-items:center">' +
      '<button class="btn" onclick="personaEdit(\'' + esc(p.name) + '\')"><span>编辑</span></button>' +
      (p.default ? "" : '<button class="btn" onclick="personaDelete(\'' + esc(p.name) + '\')"><span>删除</span></button>') +
      "</span></div>").join("");
  box.innerHTML = panel("Personas", ps.length + " 个",
    gate + (rows || '<div class="row row__v--muted">目录下没有可用的人格文件</div>'));
}
async function personaFillFromFile(input) {
  const f = input.files && input.files[0];
  if (!f) return;
  const text = await f.text();
  let name = "", desc = "", body = text;
  if (text.startsWith("---")) {
    const end = text.indexOf("\n---", 3);
    if (end > 0) {
      const head = text.slice(4, end);
      body = text.slice(end + 4);
      head.split("\n").forEach(line => {
        const m = line.match(/^(name|description):\s*(.*)$/);
        if (m) {
          if (m[1] === "name") name = m[2].trim();
          else desc = m[2].trim();
        }
      });
    }
  }
  document.getElementById("psName").value = name || f.name.replace(/\.(md|markdown|txt)$/i, "");
  document.getElementById("psDesc").value = desc;
  document.getElementById("psBody").value = body.trim();
  toast("已从文件填入：" + f.name, "ok");
}
async function personaSave() {
  const name = document.getElementById("psName").value.trim();
  const description = document.getElementById("psDesc").value.trim();
  const body = document.getElementById("psBody").value;
  const isDefault = document.getElementById("psDefault").value === "1";
  if (!name) { toast("名字不能为空", "err"); return; }
  const payload = { name: name, description: description, body: body, default: isDefault };
  let res = await doPost("api/personas/save", payload);
  if (res.status !== 200) { toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err"); return; }
  if (res.data.need_confirm) {
    const pv = res.data.preview || {};
    const ok = await confirmDialog(
      pv.overwrite ? "覆盖已有同名人格？（旧文件自动备份）" : "确认导入人格？",
      '<div class="mfield"><span class="mfield__label">' + esc(pv.name || name) + "</span>" +
        '<div class="mfield__hint">' + esc("描述：" + (pv.description || "（空）") + " · 正文 " +
          (pv.body_len || 0) + " 字" + (pv.default ? " · 设为默认" : "") +
          (pv.overwrite ? " · 覆盖并备份" : "")) + "</div></div>",
      "确认");
    if (!ok) return;
    res = await doPost("api/personas/save", {
      ...payload,
      confirm_token: res.data.confirm_token,
      confirm_nonce: res.data.confirm_nonce,
    });
    if (res.status !== 200) { toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err"); return; }
  }
  await load();
  toast("已保存人格：" + name + (res.data.backup ? "（旧文件已备份）" : ""));
}
async function personaEdit(name) {
  const r = await fetch("api/personas/detail/" + encodeURIComponent(name), {
    headers: { "Authorization": "Bearer " + tok() },
  });
  if (r.status !== 200) { toast("读取失败：" + name, "err"); return; }
  const p = await r.json();
  document.getElementById("psName").value = p.name;
  document.getElementById("psDesc").value = p.description || "";
  document.getElementById("psBody").value = p.body || "";
  document.getElementById("psDefault").value = p.default ? "1" : "0";
  toast("已载入 " + name + "，改完点保存（同名覆盖）");
}
async function personaDelete(name) {
  let res = await doPost("api/personas/delete", { name: name });
  if (res.status !== 200) { toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err"); return; }
  if (res.data.need_confirm) {
    const ok = await confirmDialog("删除人格 " + name + "？",
      '<div class="mfield"><span class="mfield__label">' + esc(name) + "</span>" +
      '<div class="mfield__hint">文件将从人格目录移除，用户切换记录不受影响</div></div>', "删除");
    if (!ok) return;
    res = await doPost("api/personas/delete", {
      name: name,
      confirm_token: res.data.confirm_token,
      confirm_nonce: res.data.confirm_nonce,
    });
    if (res.status !== 200) { toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err"); return; }
  }
  await load();
  toast("已删除人格：" + name);
}
async function personaGroupSave() {
  const groupId = document.getElementById("pgGroup").value.trim();
  const persona = document.getElementById("pgPersona").value;
  if (!groupId || !persona) { toast("群号和人格都不能为空", "err"); return; }
  const payload = { action: "save", group_id: groupId, persona: persona };
  let res = await doPost("api/personas/group", payload);
  if (res.status !== 200) { toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err"); return; }
  if (res.data.need_confirm) {
    const pv = res.data.preview || {};
    const ok = await confirmDialog("设置群人格？",
      '<div class="mfield"><span class="mfield__label">群 ' + esc(pv.group_id || groupId) + "</span>" +
      '<div class="mfield__hint">' + esc("本群所有人使用 " + (pv.persona || persona) +
        " · 覆盖已有配置 · 立即生效") + "</div></div>", "确认");
    if (!ok) return;
    res = await doPost("api/personas/group", {
      ...payload,
      confirm_token: res.data.confirm_token,
      confirm_nonce: res.data.confirm_nonce,
    });
    if (res.status !== 200) { toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err"); return; }
  }
  await load();
  toast("已设置群人格：群 " + groupId + " → " + persona);
}
async function personaGroupDelete(groupId) {
  const payload = { action: "delete", group_id: groupId };
  let res = await doPost("api/personas/group", payload);
  if (res.status !== 200) { toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err"); return; }
  if (res.data.need_confirm) {
    const ok = await confirmDialog("清除群 " + groupId + " 的人格？",
      '<div class="mfield"><span class="mfield__label">群 ' + esc(groupId) + "</span>" +
      '<div class="mfield__hint">清除后本群回落到用户选择 / 全局默认人格</div></div>', "清除");
    if (!ok) return;
    res = await doPost("api/personas/group", {
      ...payload,
      confirm_token: res.data.confirm_token,
      confirm_nonce: res.data.confirm_nonce,
    });
    if (res.status !== 200) { toast("失败：" + (res.data.detail || JSON.stringify(res.data)), "err"); return; }
  }
  await load();
  toast("已清除群人格：" + groupId);
}
function renderSettings(d) {
  lastSettings = d;
  const live = [], boot = [];

  const l = d.llm || {};
  live.push(panel("LLM 线路", null,
    row("主模型", mono(l.primary)) +
    row("备用模型", l.fallback ? mono(l.fallback) : "未配置", "row__v--muted") +
    row("当前生效", mono(l.active)) +
    row("线路", l.line === "fallback" ? chip("备用线路", "chip--alert") : chip("主线路", "chip--ink"))));
  const f = d.runtime_flags || {};
  const ww = f.wake_words || [];
  live.push(panel("运行开关", null,
    boolRow("vision", f.vision) +
    boolRow("群聊上下文", f.group_context) +
    row("唤醒词", ww.length ? ww.map(w => '<span class="mono">' + esc(w) + "</span>").join(" &middot; ") : "未配置", "row--stack")));
  const b = d.budget || {};
  const today = b.today || {};
  live.push(panel("预算限额", null,
    row("今日上限", b.daily_tokens ? num(b.daily_tokens) + " tok" : "未设上限") +
    row("闸门", b.enforce ? chip("硬闸 · 超限拦截", "chip--hatch") : "软闸 · 仅告警") +
    row("今日已用", num(today.total) + " tok", "row__v--muted") +
    row("今日成本", (b.cost_today === null || b.cost_today === undefined) ? "未配单价" : "≈ " + Number(b.cost_today).toFixed(2) + " 元", "row__v--muted")));

  const m = d.memory_summary || {};
  boot.push(panel("记忆与摘要", null,
    boolRow("事实抽取", m.extract_facts) +
    boolRow("历史裁剪与滚动摘要", m.summary_enabled) +
    row("召回条数 / 阈值", num(m.memory_facts_top_k) + " / " + (m.memory_facts_threshold === null || m.memory_facts_threshold === undefined ? "—" : m.memory_facts_threshold)) +
    row("历史 token 预算", num(m.history_token_budget)) +
    row("摘要参数", "out " + num(m.summary_max_tokens) + " · fetch " + num(m.summary_fetch_limit) + " · chars " + num(m.summary_max_chars), "row__v--muted") +
    row("历史闲置阈值", num(m.history_stale_after) === 0 ? "关闭" : num(m.history_stale_after) + " 秒", "row__v--muted") +
    row("工具循环上限", num(m.max_iterations), "row__v--muted")));
  const r = d.rag || null;
  boot.push(panel("RAG 检索", null, r ?
    boolRow("启用", r.enabled) +
    row("top_k / 阈值", num(r.top_k) + " / " + (r.threshold === null || r.threshold === undefined ? "—" : r.threshold)) +
    row("蒸馏 cron", mono(r.digest_cron)) +
    row("向量", r.embedding === "on" ? chip("开启", "chip--ink") : "关闭")
    : '<div class="row row__v--muted">知识库未初始化</div>'));
  const ba = d.backup_archive || {};
  const ar = ba.archive || {}, bk = ba.backup || {};
  boot.push(panel("备份与归档", null,
    row("归档目录", mono(ar.dir)) +
    row("归档保留", ar.keep_days ? num(ar.keep_days) + " 天" : "—", "row__v--muted") +
    row("备份目录", mono(bk.dir)) +
    row("备份保留", bk.keep ? num(bk.keep) + " 份" : "—", "row__v--muted") +
    row("备份 cron", mono(bk.cron)) +
    row("异地镜像", bk.mirror_dir ? mono(bk.mirror_dir) : "未配置", "row__v--muted")));

  let html =
    '<section class="sec">' + secHead("S1", "即时生效", "LIVE") +
      '<div class="sec__grid">' + live.join("") + "</div></section>" +
    '<section class="sec">' + secHead("S2", "重启生效", "BOOT") +
      '<div class="sec__grid">' + boot.join("") + "</div></section>";

  /* 受控写入（D2-1）：簇导航 + 单簇 panel。一次只看一个簇，避免长列表下滑；
     搜索时切换成跨簇结果视图。 */
  if (d.write_enabled) {
    indexSettings(d);
    const sc = (d.settings || {}).secret_count || 0;
    const tw = (d.settings || {}).total || 0;
    html += '<section class="sec">' + secHead("S3", "全部设置", "WRITE") +
      '<div class="ctools">' +
        '<input id="settingSearch" class="inp" style="flex:1;min-width:220px" ' +
          'placeholder="搜索键名 / 说明…（跨簇）" oninput="filterSettings(this.value)">' +
        '<span class="row__v--muted" style="font-size:11px">' + num(tw) +
          ' 个环境变量 · ' + num(sc) + ' 个凭据/URL 类走 SSH</span>' +
      "</div>" +
      '<div class="cnav" id="clusterNav"></div>' +
      '<div id="clusterPanel"></div>' +
      '<div class="sec__grid" style="margin-top:14px">' +
        panel("写入与重启", "标“重启生效”的项改完必须重启",
          '<div class="wrow" style="border-bottom:none"><span class="wk">重启 bot' +
          '<span style="color:var(--muted);font-size:11px">先保存状态（防抖 flush + 归档）再重启</span>' +
          '</span><button class="btn btn--solid" onclick="rebootBot()"><span>重启</span></button></div>') +
      "</div></section>";
  } else {
    html += '<section class="sec">' + secHead("S3", "全部设置", "WRITE") +
      '<div class="sec__grid">' + panel("受控写入", "未启用",
        '<div class="row row__v--muted">需配置 AGENT_WEB_ALLOW_CIDRS 且 AGENT_WEB_WRITE=1（改后重启生效）。凭据与安全边界键（API_KEY/TOKEN/SUPERUSERS 等）一律走 SSH，不做 web 写入。</div>') +
      "</div></section>";
  }

  if (d.errors && d.errors.length) {
    html += '<section class="sec">' + secHead("!!", "取数失败的部分", "ERRORS") +
      '<div class="sec__grid">' + panel("失败项", d.errors.length + " 项",
        '<div class="errs">' + d.errors.map(esc).join("\n") + "</div>") + "</div></section>";
  }
  document.getElementById("settingsView").innerHTML = html;
  initClusters(d);
}

/* ---- 日志查看：原始日志不脱敏（管理员决策 D4），同 token 门禁 ---- */
const logState = { after: 0 };
function logParams() {
  const p = new URLSearchParams({ file: document.getElementById("logFile").value || "agent.log" });
  if (logState.after > 0) p.set("after", logState.after);
  const lv = document.getElementById("logLevel").value;
  const md = document.getElementById("logModule").value.trim();
  const gr = document.getElementById("logGrep").value.trim();
  if (lv) p.set("level", lv);
  if (md) p.set("module", md);
  if (gr) p.set("grep", gr);
  return p;
}
function logUrl() { return "api/logs?" + logParams(); }
function logReset() { logState.after = 0; logState.dirty = true; load(); }
async function logDownload() {
  // fetch + Blob：location.href 导航带不上 Authorization 头，Bearer 门禁下
  // 必然 401（REVIEW M7 复现实锤——旧实现页面里点了只会拿到 401）
  const f = document.getElementById("logFile").value || "agent.log";
  let r;
  try {
    r = await fetch("api/logs/download?file=" + encodeURIComponent(f),
      { headers: { "Authorization": "Bearer " + tok() } });
  } catch (e) { alert("下载失败：" + e); return; }
  if (!r.ok) { alert("下载失败：HTTP " + r.status); return; }
  const blob = await r.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = f;
  a.click();
  URL.revokeObjectURL(a.href);
}
function logCls(level) {
  if (level === "ERROR" || level === "CRITICAL") return "lv--err";
  if (level === "WARNING") return "lv--warn";
  return "";
}
async function renderLogs(d) {
  // 文件清单：随每次加载刷新（目录最多十几个文件，开销可忽略）
  let files = [];
  try {
    const fr = await fetch("api/logs/files", { headers: { "Authorization": "Bearer " + tok() } });
    files = (await fr.json()).files || [];
  } catch (e) { /* 清单失败不挡日志正文 */ }
  const sel = document.getElementById("logFile");
  const want = files.map(f => f.name).join("\u0000");
  if (sel.dataset.sig !== want) {
    sel.dataset.sig = want;
    sel.innerHTML = files.map(f =>
      "<option value=\"" + esc(f.name) + "\">" + esc(f.name) +
      (f.current ? "（当前）" : "") + " · " + num(f.size) + "B</option>").join("");
    if (!files.some(f => f.name === sel.value) && files.length) sel.value = files[0].name;
  }
  // incremental：入口处的 logState.after 是上一轮游标——>0 说明本次请求带了
  // after（自动刷新的增量轮询）。增量必须**追加**而非整体替换：否则勾了自动
  // 刷新后每 5s 只剩最近 5 秒的 0–2 行，历史窗口被冲掉（线上实测反馈）。
  // 服务端 reset=true（游标越过文件尺寸 = 轮转/截断）时必须整体替换：增量
  // 追加会把**新文件**的尾窗接在**旧文件**遗留行后面，两文件无缝混排
  // （REVIEW-3ce6e0a..de09478 L1）。
  const incremental = logState.after > 0 && !d.reset;
  logState.after = d.end_offset || 0;
  const out = document.getElementById("logOut");
  if (incremental) {
    out.querySelectorAll(".logph").forEach(el => el.remove());  // 清掉空态占位
    for (const x of d.lines || []) out.appendChild(logLineDiv(x));
    while (out.childElementCount > LOG_DOM_CAP) out.removeChild(out.firstElementChild);
  } else if ((d.lines || []).length) {
    out.replaceChildren(...(d.lines || []).map(logLineDiv));
  } else {
    out.innerHTML = '<div class="logph">（窗口内没有匹配的行）</div>';
  }
  const meta = document.getElementById("logMeta");
  const bits = [];
  bits.push(d.file + " · " + num(d.size) + "B");
  if (incremental) {
    bits.push("新增 " + num(d.returned) + " · 累计 " + num(out.childElementCount) + " 行");
  } else {
    bits.push("显示 " + num(d.returned) + " 行（" + num(d.start_offset) + "–" + num(d.end_offset) + "）");
  }
  if (d.truncated) bits.push("仅尾部窗口");
  if (d.reset) bits.push("⚠ 游标失效（文件已轮转），已回到尾部");
  if ((d.errors || []).length) bits.push("错误: " + d.errors.join("; "));
  meta.textContent = bits.join(" · ");
}

// 日志行 DOM：textContent 而非 innerHTML 拼接——日志正文天然不可信，
// 拼接 HTML 等于把日志内容当标记解析（XSS 面）。
const LOG_DOM_CAP = 3000;  // 追加模式的行数上限，防长时间挂机时 DOM 无界增长
function logLineDiv(x) {
  const div = document.createElement("div");
  div.className = logCls(x.level);
  div.textContent = x.ts + " " + x.level + " " + x.logger + ": " + x.msg;
  return div;
}

// 群发言统计（只读聚合数据：条数/名片快照/词频/小时直方图，无消息正文）
function statsRange(w) {
  return (w && w.start) ? (w.start + " ~ " + w.end) : "";
}
function statsWindowBody(g) {
  if (!g) return '<div class="row row__v--muted">无记录</div>';
  let inner = row("群友条数", num(g.window_total) + " 条 · " + num(g.users) + " 人");
  if ((g.top || []).length) {
    inner += '<div class="logmeta mono" style="margin:10px 0 4px">TOP ' + g.top.length + "</div>";
    for (const r of g.top) {
      const name = r.nickname
        ? esc(r.nickname) + ' <span class="mono" style="color:var(--muted)">' + esc(r.user_id) + "</span>"
        : '<span class="mono">' + esc(r.user_id) + "</span>";
      inner += row(name, num(r.msgs) + " 条 · " + r.pct + "%");
    }
  }
  if ((g.words || []).length) {
    const ws = g.words
      .map(x => esc(x.word) + '<em style="color:var(--muted);font-style:normal"> ×' + num(x.count) + "</em>")
      .join(" &nbsp;");
    inner += '<div class="logmeta mono" style="margin:10px 0 4px">高频词 TOP ' + g.words.length + "</div>" +
      '<div style="font-size:13px;line-height:2">' + ws + "</div>";
  }
  return inner;
}
function renderStats(d) {
  const out = document.getElementById("statsOut");
  if (!d || !d.enabled) {
    out.innerHTML = '<div class="sec">' + secHead("05", "群聊发言统计", "GROUP STATS") +
      '<div class="panel"><div class="panel__bd">' +
      row("状态", "未开启（AGENT_GROUP_STATS=0）") + "</div></div></div>";
    return;
  }
  const wins = (d.windows && d.windows.length) ? d.windows : [
    { key: "last", label: "上周", start: (d.window || {}).start, end: (d.window || {}).end, groups: d.groups || [] },
  ];
  const lastW = wins.find(x => x.key === "last") || wins[0] || {};
  const curW = wins.find(x => x.key === "current") || wins[1] || {};
  // 两周边并集：某群只在其中一周出现也要显示（另一侧标"无记录"）
  const byGroup = new Map();
  wins.forEach(w => (w.groups || []).forEach(g => {
    if (!byGroup.has(g.group_id)) byGroup.set(g.group_id, {});
    byGroup.get(g.group_id)[w.key] = g;
  }));
  let html = '<div class="sec">' + secHead("05", "群聊发言统计", "GROUP STATS") +
    '<div class="logmeta mono">上周 ' + esc(statsRange(lastW)) +
    " · 本周（进行中）" + esc(statsRange(curW)) +
    " · 数据每 flush 周期聚合（见 AGENT_GROUP_STATS_FLUSH_SECONDS）</div>";
  if (d.error) {
    html += '<div class="logmeta mono">部分数据读取失败：' + esc(d.error) + "</div>";
  }
  if (!byGroup.size) {
    html += '<div class="panel"><div class="panel__bd">' +
      row("数据", "上周与本周都没有任何群的发言记录") + "</div></div>";
  }
  for (const [gid, pair] of byGroup) {
    html += '<div class="sec__grid" style="margin-top:12px">' +
      panel("群 " + esc(gid) + " · 上周", esc(statsRange(lastW)),
        statsWindowBody(pair.last)) +
      panel("群 " + esc(gid) + " · 本周（进行中）", esc(statsRange(curW)),
        statsWindowBody(pair.current)) +
      "</div>";
  }
  html += "</div>";
  out.innerHTML = html;
}
// 支持 ?view=settings 深链（tab 名不是机密，放 query 无妨；token 仍只走 #fragment）
const _initialView = new URLSearchParams(location.search).get("view");
setView(["overview", "settings", "model", "persona", "stats", "logs"].includes(_initialView) ? _initialView : "overview");
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.getElementById("modalMask")) closeModal();
});
load();
setInterval(load, 5000);
</script>
</body>
</html>"""


def mount_web(app=None) -> bool:
    """把只读总览挂到 ASGI 应用上；返回是否挂载。

    ``app`` 为 None 时取 NoneBot 的 app（生产路径）。测试可传独立 FastAPI
    实例，避免污染全局 NoneBot 应用。未配 token / 取不到 app 都不挂载。
    """
    token = _web_token()
    if not token:
        logger.info("web 总览未挂载：AGENT_WEB_TOKEN 未配置（fail-closed）")
        return False
    if not token.isascii():
        # compare_digest 对非 ASCII str 抛 TypeError（会变 500 而非 401），
        # 整个 web 面不可用——挂载期直接拒绝并给出可行动的告警
        logger.error(
            "web 总览未挂载：AGENT_WEB_TOKEN 含非 ASCII 字符，请改用纯 ASCII token"
        )
        return False
    if Request is None or FastAPI is None:
        logger.info("web 总览未挂载：当前环境没有 fastapi")
        return False
    try:
        if app is None:
            from nonebot import get_app

            app = get_app()
        if not isinstance(app, FastAPI):
            logger.warning("web 总览未挂载：当前 driver 的 app 不是 FastAPI 实例")
            return False

        async def _auth(request: Request) -> None:
            if not _ip_allowed(request.client.host if request.client else ""):
                raise HTTPException(status_code=403, detail="source ip not allowed")
            header = request.headers.get("authorization") or ""
            supplied = header[7:].strip() if header[:7].lower() == "bearer " else ""
            # 恒等比较：避免按字节比较带来的时序侧信道。
            # 非 ASCII 必须先挡：compare_digest 对含非 ASCII 的 str 直接抛
            # TypeError（未认证输入 → 500 而非 401，REVIEW M5 复现实锤）
            if not supplied or not supplied.isascii():
                raise HTTPException(status_code=401, detail="invalid token")
            if not secrets.compare_digest(supplied, token):
                raise HTTPException(status_code=401, detail="invalid token")

        # 认证依赖放 decorator 的 dependencies=（而非参数默认值）：既是 FastAPI
        # 对"只做鉴权的依赖"的惯用法，也避开 bugbear B008。
        #
        # **页面故意不挂 _auth**：它只是登录表单 + JS，本身零机密。若连页面也锁，
        # 未认证用户拿到的就是 401 JSON，永远看不到输入框（鸡生蛋，线上实测）。
        # 数据面（api/overview）仍然强制鉴权。
        @app.get(f"{PREFIX}/", response_class=HTMLResponse)
        async def _page() -> HTMLResponse:
            return HTMLResponse(_PAGE)

        @app.get(f"{PREFIX}/api/overview", dependencies=[Depends(_auth)])
        async def _overview() -> JSONResponse:
            return JSONResponse(await build_overview())

        @app.get(f"{PREFIX}/api/settings", dependencies=[Depends(_auth)])
        async def _settings() -> JSONResponse:
            return JSONResponse(await build_settings())

        @app.get(f"{PREFIX}/api/group-stats", dependencies=[Depends(_auth)])
        async def _group_stats() -> JSONResponse:
            return JSONResponse(await build_group_stats())

        @app.get(f"{PREFIX}/api/logs/files", dependencies=[Depends(_auth)])
        async def _log_files() -> JSONResponse:
            return JSONResponse(list_log_files())

        @app.get(f"{PREFIX}/api/logs", dependencies=[Depends(_auth)])
        async def _logs(
            file: str = "agent.log",
            bytes: int | None = Query(default=None, alias="bytes"),
            after: int = 0,
            level: str = "",
            module: str = "",
            grep: str = "",
        ) -> JSONResponse:
            if len(grep) > _LOG_GREP_MAX:
                raise HTTPException(status_code=400, detail="grep too long")
            levels = tuple(
                lv
                for lv in (x.strip().upper() for x in level.split(","))
                if lv in _LOG_LEVELS
            )
            return JSONResponse(
                read_log_window(
                    file,
                    window=bytes if bytes else _LOG_WINDOW_DEFAULT,
                    after=after,
                    levels=levels,
                    module=module.strip()[:80],
                    grep=grep.strip(),
                )
            )

        @app.get(f"{PREFIX}/api/logs/download", dependencies=[Depends(_auth)])
        async def _log_download(file: str = "agent.log"):
            safe = _safe_log_name(file)
            if safe is None:
                raise HTTPException(status_code=400, detail="invalid log file name")
            path = _log_dir() / safe
            if not path.is_file():
                raise HTTPException(status_code=404, detail="log file not found")
            size = path.stat().st_size
            if size > _LOG_DOWNLOAD_MAX:
                raise HTTPException(
                    status_code=400,
                    detail=f"log file too large: {size} bytes",
                )
            # 下载是显著动作且含消息正文：留审计（只记文件名与字节数）
            from agentcore.diagnostics import record as _record

            _record("logs_downloaded", file=safe, size=size)
            return FileResponse(
                path,
                filename=safe,
                media_type="text/plain; charset=utf-8",
            )

        # ---------- 人格导入/管理（只读列表 + 详情） ----------
        # persona 是管理员显式配置（等同 .env/config.yaml 的受信内容，不过
        # safety 围栏——围栏是给引用/转发/检索结果等不可信外部文本的），但
        # 落盘 = 写配置目录，写操作沿用写面全套门禁（见下方写面块）。
        # 正文上限在 manager.render_persona_md（20000 字：body 每轮进
        # system prompt，无上限会爆 token 预算）。

        def _persona_manager():
            """driver 上共享的 PersonaManager；未初始化返回 None（页面降级提示）。"""
            try:
                from nonebot import get_driver

                return getattr(get_driver(), "_agent_persona_manager", None)
            except Exception:
                return None

        def _memory_store():
            """driver 上共享的 MemoryStore（群人格读写）；未初始化返回 None。"""
            try:
                from nonebot import get_driver

                return getattr(get_driver(), "_agent_memory", None)
            except Exception:
                return None

        @app.get(f"{PREFIX}/api/personas", dependencies=[Depends(_auth)])
        async def _personas() -> JSONResponse:
            pm = _persona_manager()
            mem = _memory_store()
            items = []
            for p in pm.list() if pm is not None else []:
                try:
                    mtime = Path(p.file).stat().st_mtime
                except OSError:
                    mtime = None
                items.append(
                    {
                        "name": p.name,
                        "description": p.description,
                        "default": p.default,
                        "mtime": mtime,
                    }
                )
            groups: list[dict] = []
            known: set[str] = set()
            if mem is not None:
                try:
                    gp = await mem.list_group_personas()
                    groups = [
                        {"group_id": g, "persona": per} for g, per in sorted(gp.items())
                    ]
                except Exception:
                    groups = []
                try:
                    known.update(await mem.list_known_group_ids())
                except Exception:
                    pass
            # 群统计白名单里的群也作为建议（可能还没有会话记录）
            try:
                from agentcore.groupstats.config import allowed_groups

                known.update(allowed_groups())
            except Exception:
                pass
            return JSONResponse(
                {
                    "personas": items,
                    "dir": str(pm.dir) if pm is not None else None,
                    "write_enabled": _write_enabled(),
                    "group_personas": groups,
                    "known_groups": sorted(known),
                }
            )

        @app.get(
            f"{PREFIX}/api/personas/detail/{{name}}", dependencies=[Depends(_auth)]
        )
        async def _persona_detail(name: str) -> JSONResponse:
            pm = _persona_manager()
            # get 内部过 _NAME_RE：非法名（含路径字符）直接 None → 404，不碰盘
            p = pm.get(name) if pm is not None else None
            if p is None:
                raise HTTPException(status_code=404, detail="persona_not_found")
            return JSONResponse(
                {
                    "name": p.name,
                    "description": p.description,
                    "default": p.default,
                    "body": p.body,
                }
            )

        # ---- 受控写入（D2-1）：门禁不齐整体不注册，读面零变化 ----
        if _write_enabled():

            @app.post(f"{PREFIX}/api/settings/preview", dependencies=[Depends(_auth)])
            async def _settings_preview(request: Request) -> JSONResponse:
                try:
                    payload = await request.json()
                except Exception as e:
                    raise HTTPException(status_code=422, detail="invalid json") from e
                key = str(payload.get("key") or "")
                try:
                    scope, spec, _normalized, file_text = _validate_write(
                        key, payload.get("value")
                    )
                except cw.ValueValidationError as e:
                    code = (
                        409
                        if str(e).startswith("cluster_disabled:")
                        or str(e) == "legacy_model_key"
                        else 422
                    )
                    raise HTTPException(status_code=code, detail=str(e)) from e
                if scope == "yaml":
                    import yaml

                    from .config_yaml import config_path

                    try:
                        data = (
                            yaml.safe_load(config_path().read_text(encoding="utf-8"))
                            or {}
                        )
                        cur = data
                        for part in spec.path.split("."):
                            cur = cur[part]
                    except Exception:
                        cur = None
                    old_file = cur
                    current_runtime = None
                else:
                    old_file = _env_value_or_none(key)
                    current_runtime = os.getenv(key)
                return JSONResponse(
                    {
                        "key": key,
                        "scope": scope,
                        "description": spec.description,
                        "kind": spec.kind,
                        "current_file": old_file,
                        "current_runtime": current_runtime,
                        "new_value": cw.masked(file_text),
                        "effective": "live" if scope == "env" and spec.live else "boot",
                    }
                )

            @app.post(f"{PREFIX}/api/settings/write", dependencies=[Depends(_auth)])
            async def _settings_write(request: Request) -> JSONResponse:
                try:
                    payload = await request.json()
                except Exception as e:
                    raise HTTPException(status_code=422, detail="invalid json") from e
                key = str(payload.get("key") or "")
                try:
                    scope, spec, normalized, file_text = _validate_write(
                        key, payload.get("value")
                    )
                except cw.ValueValidationError as e:
                    reason = str(e)
                    if reason in {"key_not_writable", "yaml_key_not_writable"}:
                        raise HTTPException(status_code=403, detail=reason) from e
                    if (
                        reason.startswith("cluster_disabled:")
                        or reason == "legacy_model_key"
                    ):
                        # 簇开关关闭 / legacy 模型键：与"文件/键冲突"同档 409
                        raise HTTPException(status_code=409, detail=reason) from e
                    raise HTTPException(status_code=422, detail=reason) from e

                token = str(payload.get("confirm_token") or "")
                nonce = str(payload.get("confirm_nonce") or "")
                if not token or not nonce:
                    # 第一段：校验通过，发确认码（绑定 nonce+key+新值+30s 窗口）
                    nonce = secrets.token_hex(8)
                    cur_old = _env_value_or_none(
                        key
                    )  # None = 文件缺失，前端显示"未设置"
                    return JSONResponse(
                        {
                            "need_confirm": True,
                            "confirm_token": _confirm_token(nonce, key, file_text),
                            "confirm_nonce": nonce,
                            "expires_in": _WRITE_WINDOW,
                            "key": key,
                            "scope": scope,
                            # 前端在确认框里就要说清"保存后立即生效/需重启"
                            "effective": (
                                "live" if scope == "env" and spec.live else "boot"
                            ),
                            "old": cw.masked(cur_old) if cur_old is not None else None,
                            "new": cw.masked(file_text),
                        }
                    )
                if key == "LLM_MODEL":
                    # 切模型前先探测：写坏一个模型名会让每轮对话都失败，而 web
                    # 路径没有 /model reset 那样的即时自救入口。探测放在消耗确认码
                    # **之前**——失败时码还有效，管理员改个名字能直接重试。
                    from agentcore.llm.client import parse_model_spec

                    # 裸模型名先对回池条目：否则会拿主端点/主 key 去探测
                    # 另一家供应商的同名模型（与 /model 的池继承同口径）
                    from agentcore.skills.registry import get_shared_llm_client

                    from .model_switch import probe_failure_detail

                    entry = get_shared_llm_client().resolve_entry(
                        parse_model_spec(file_text, allow_key=False)
                    )
                    detail = await probe_failure_detail(entry)
                    if detail:
                        raise HTTPException(status_code=409, detail=detail)

                err = _consume_confirm(nonce, token, key, file_text)
                if err:
                    raise HTTPException(status_code=400, detail=err)

                async with _write_lock:  # 单进程写者串行化
                    try:
                        # 备份/补丁/原子写/写后验证/失败还原集中在一处：
                        # .env 与 config.yaml 走同一组不变量
                        if scope == "yaml":
                            from . import config_yaml as cy

                            result = cy.write_yaml_key(spec, normalized, file_text)
                        else:
                            result = cw.write_env_key(spec, file_text)
                    except cw.ValueValidationError as e:
                        # symlink fail-closed / 同键重复出现（合法请求撞脏文件）
                        raise HTTPException(status_code=409, detail=str(e)) from e
                    except ValueError as e:
                        # config_yaml 的定位错误（key_not_found/重复行）
                        raise HTTPException(status_code=409, detail=str(e)) from e
                    except cw.EnvWriteError as e:
                        raise HTTPException(status_code=500, detail=str(e)) from e
                old_value = result.old
                backup = result.backup
                if scope == "yaml":
                    effective = "boot"  # config.yaml 全部启动期装配
                else:
                    effective = cw.apply_runtime(spec, normalized, file_text)
                from agentcore.diagnostics import record as _record

                _record(
                    "config_write",
                    key=key,
                    old=cw.masked(old_value),
                    new=cw.masked(file_text),
                    backup=backup.name,
                    effective=effective,
                    # 定稿「审计 who/when/key/old→new」的 who：CIDR 是门禁不是
                    # 记录，同一 token 多人共持时靠它区分操作者（REVIEW M10）
                    source_ip=request.client.host if request.client else "",
                    confirm_nonce=nonce,
                )
                return JSONResponse(
                    {
                        "ok": True,
                        "key": key,
                        "scope": scope,
                        "old": cw.masked(old_value),
                        "new": cw.masked(file_text),
                        "effective": effective,
                        "backup": backup.name,
                    }
                )

            # ---- 多 LLM Provider：CRUD / 设主 / 排序 / 测试连接 ----
            # key 只写不回显：GET 的 provider 列表由 model_status().providers 提供，
            # 只含 key_set；本接口的确认码把最终 provider（含 key）绑进 HMAC。
            @app.post(
                f"{PREFIX}/api/settings/llm/providers",
                dependencies=[Depends(_auth)],
            )
            async def _llm_providers(request: Request) -> JSONResponse:
                try:
                    payload = await request.json()
                except Exception as e:
                    raise HTTPException(status_code=422, detail="invalid json") from e
                action = str(payload.get("action") or "save").strip().lower()
                from agentcore.llm import providers as ps
                from agentcore.skills.registry import get_shared_llm_client

                client = get_shared_llm_client()
                try:
                    cfg = ps.load()
                except FileNotFoundError:
                    from agentcore.llm.client import _CFG

                    cfg = ps.migrate_from_env(_CFG)
                except Exception as e:
                    raise HTTPException(
                        status_code=500, detail=f"providers_load:{type(e).__name__}"
                    ) from e

                if action == "test":
                    pid = str(payload.get("id") or "")
                    provider = cfg.by_id(pid)
                    if provider is None:
                        raise HTTPException(
                            status_code=404, detail="provider_not_found"
                        )
                    from .model_switch import provider_probe_detail

                    detail = await provider_probe_detail(provider)
                    # 测试连接会把 key 发到 provider 端点：留审计（不含 key）。
                    # 它也是写面唯一不需要两段确认的动作，审计是事后追责的唯一线索。
                    from agentcore.diagnostics import record as _record

                    _record(
                        "config_write",
                        key="llm:test",
                        new=provider.id,
                        mode="llm-provider-test",
                        source_ip=request.client.host if request.client else "",
                    )
                    return JSONResponse({"ok": not detail, "detail": detail})

                # ---- 先算出待确认的规范值（HMAC 绑定）----
                if action == "save":
                    raw = payload.get("provider")
                    if not isinstance(raw, dict):
                        raise HTTPException(status_code=422, detail="provider_required")
                    existing = cfg.by_id(str(raw.get("id") or "").strip().lower())
                    merged = dict(raw)
                    if existing is not None:
                        merged["id"] = existing.id
                        # H2：改端点/协议但 key 留空 = 把旧 key 发到新端点（凭据外带）。
                        # 必须要求显式提供新 key（或 clear_key）才允许改这两个字段。
                        new_endpoint = (
                            str(
                                merged.get("endpoint")
                                or merged.get("base_url")
                                or existing.base_url
                            )
                            .strip()
                            .rstrip("/")
                        )
                        new_protocol = (
                            str(merged.get("protocol") or existing.protocol)
                            .strip()
                            .lower()
                        )
                        key_provided = bool(
                            str(raw.get("api_key") or "").strip()
                        ) or bool(payload.get("clear_key"))
                        if (
                            new_endpoint != existing.base_url
                            or new_protocol != existing.protocol
                        ) and not key_provided:
                            raise HTTPException(
                                status_code=409,
                                detail="provider_key_required_for_endpoint_change",
                            )
                        if payload.get("clear_key"):
                            merged["api_key"] = ""
                        elif not str(raw.get("api_key") or "").strip():
                            merged["api_key"] = existing.api_key
                    else:
                        merged["api_key"] = str(raw.get("api_key") or "")
                        if not str(merged.get("id") or "").strip():
                            used = {p.id for p in cfg.providers}
                            merged["id"] = ps._unique_id(
                                ps._slug(
                                    str(merged.get("name") or merged.get("model") or "")
                                ),
                                used,
                            )
                    try:
                        provider = ps.validate_provider(merged)
                    except ps.ProviderConfigError as e:
                        raise HTTPException(status_code=422, detail=str(e)) from e
                    value_text = json.dumps(
                        provider.__dict__, sort_keys=True, ensure_ascii=False
                    )
                    confirm_key = f"llm:save:{provider.id}"
                    preview = {
                        "id": provider.id,
                        "name": provider.name,
                        "protocol": provider.protocol,
                        "model": provider.model,
                        "key_set": bool(provider.api_key),
                    }
                elif action == "delete":
                    pid = str(payload.get("id") or "")
                    if cfg.by_id(pid) is None:
                        raise HTTPException(
                            status_code=404, detail="provider_not_found"
                        )
                    if len(cfg.providers) <= 1:
                        raise HTTPException(status_code=409, detail="last_provider")
                    value_text = f"delete:{pid}"
                    confirm_key = "llm:delete"
                    preview = {"id": pid}
                elif action in {"set_active", "reorder"}:
                    value_text = (
                        str(payload.get("id") or "")
                        if action == "set_active"
                        else ",".join(str(x) for x in payload.get("ids") or [])
                    )
                    confirm_key = f"llm:{action}"
                    preview = {"action": action, "value": value_text}
                else:
                    raise HTTPException(status_code=422, detail="unknown_action")

                token = str(payload.get("confirm_token") or "")
                nonce = str(payload.get("confirm_nonce") or "")
                if not token or not nonce:
                    nonce = secrets.token_hex(8)
                    return JSONResponse(
                        {
                            "need_confirm": True,
                            "confirm_token": _confirm_token(
                                nonce, confirm_key, value_text
                            ),
                            "confirm_nonce": nonce,
                            "expires_in": _WRITE_WINDOW,
                            "action": action,
                            "preview": preview,
                        }
                    )
                err = _consume_confirm(nonce, token, confirm_key, value_text)
                if err:
                    raise HTTPException(status_code=400, detail=err)

                # ---- 提交：先改副本，保存成功后才 reload 运行态 ----
                if action == "save":
                    if existing is not None:
                        cfg.providers = [
                            provider if p.id == provider.id else p
                            for p in cfg.providers
                        ]
                    else:
                        cfg.providers.append(provider)
                        if cfg.by_id(cfg.active) is None:
                            cfg.active = provider.id
                elif action == "delete":
                    cfg.providers = [p for p in cfg.providers if p.id != pid]
                    if cfg.active == pid:
                        cfg.active = next(
                            (p.id for p in cfg.providers if p.enabled),
                            cfg.providers[0].id,
                        )
                elif action == "set_active":
                    target = cfg.by_id(value_text)
                    if target is None:
                        raise HTTPException(
                            status_code=404, detail="provider_not_found"
                        )
                    if not target.enabled:
                        raise HTTPException(status_code=409, detail="provider_disabled")
                    cfg.active = target.id
                else:  # reorder
                    ids = [x for x in value_text.split(",") if x]
                    if sorted(ids) != sorted(p.id for p in cfg.providers):
                        raise HTTPException(
                            status_code=422, detail="reorder_not_permutation"
                        )
                    by_id = {p.id: p for p in cfg.providers}
                    cfg.providers = [by_id[i] for i in ids]

                try:
                    ps.save(cfg)
                except ps.ProviderConfigError as e:
                    raise HTTPException(status_code=409, detail=str(e)) from e
                client.reload_providers()
                from agentcore.diagnostics import record as _record

                _record(
                    "config_write",
                    key=confirm_key,
                    new=json.dumps(preview, ensure_ascii=False)[:200],
                    mode="llm-provider",
                    source_ip=request.client.host if request.client else "",
                    confirm_nonce=nonce,
                )
                return JSONResponse(
                    {
                        "ok": True,
                        "action": action,
                        "active": cfg.active,
                        "providers": [p.public() for p in cfg.providers],
                    }
                )

            # ---- 人格导入/编辑/删除：两段确认（HMAC 绑定最终 md 文本）----
            # 确认码绑定的 file_text 由 manager.render_persona_md 生成——与
            # save 共用同一渲染函数，拿到码也改不了别的名字/别的正文。
            @app.post(f"{PREFIX}/api/personas/save", dependencies=[Depends(_auth)])
            async def _persona_save(request: Request) -> JSONResponse:
                try:
                    payload = await request.json()
                except Exception as e:
                    raise HTTPException(status_code=422, detail="invalid json") from e
                if not isinstance(payload, dict):
                    raise HTTPException(status_code=422, detail="not_an_object")
                pm = _persona_manager()
                if pm is None:
                    raise HTTPException(
                        status_code=409, detail="persona_manager_not_initialized"
                    )
                name = str(payload.get("name") or "").strip()
                description = str(payload.get("description") or "")
                body = str(payload.get("body") or "")
                default = bool(payload.get("default"))
                # 预校验（与确认码解耦：坏输入直接 422，不浪费一次挑战窗口）
                try:
                    from agentcore.personas.manager import render_persona_md

                    file_text = render_persona_md(name, description, body, default)
                except ValueError as e:
                    raise HTTPException(status_code=422, detail=str(e)) from e
                existing = pm.get(name)
                confirm_key = f"persona:save:{name}"
                preview = {
                    "name": name,
                    "description": description[:80],
                    "default": default,
                    "body_len": len(body),
                    "overwrite": existing is not None,
                }
                token = str(payload.get("confirm_token") or "")
                nonce = str(payload.get("confirm_nonce") or "")
                if not token or not nonce:
                    nonce = secrets.token_hex(8)
                    return JSONResponse(
                        {
                            "need_confirm": True,
                            "confirm_token": _confirm_token(
                                nonce, confirm_key, file_text
                            ),
                            "confirm_nonce": nonce,
                            "expires_in": _WRITE_WINDOW,
                            "action": "save",
                            "preview": preview,
                        }
                    )
                err = _consume_confirm(nonce, token, confirm_key, file_text)
                if err:
                    raise HTTPException(status_code=400, detail=err)
                backup = None
                async with _write_lock:  # 单进程写者串行化（备份 + 落盘一体）
                    try:
                        if existing is not None:
                            # 改前备份：覆盖是唯一会丢旧内容的动作（写面纪律）。
                            # 落点是 web 备份根（AGENT_WEB_BACKUP_DIR，测试注入
                            # tmp）下的 personas/ 子目录——不能用 backup_dir()
                            # 的 .parent：env 覆盖时父目录会撞上 PERSONAS_DIR
                            # （e2e 实测抓到：.bak 落进了人格目录本身）
                            bdir = (
                                Path(os.getenv("AGENT_WEB_BACKUP_DIR", "data/backups"))
                                / "personas"
                            )
                            bdir.mkdir(parents=True, exist_ok=True)
                            backup = (
                                bdir / f"{name}.md.{time.strftime('%Y%m%d-%H%M%S')}.bak"
                            )
                            backup.write_bytes(Path(existing.file).read_bytes())
                        pm.save(name, description, body, default)
                    except ValueError as e:
                        raise HTTPException(status_code=422, detail=str(e)) from e
                    except OSError as e:
                        raise HTTPException(status_code=500, detail=str(e)) from e
                from agentcore.diagnostics import record as _record

                _record(
                    "config_write",
                    key=confirm_key,
                    new=json.dumps(preview, ensure_ascii=False)[:200],
                    mode="persona-save",
                    source_ip=request.client.host if request.client else "",
                    confirm_nonce=nonce,
                    backup=backup.name if backup else None,
                )
                return JSONResponse(
                    {
                        "ok": True,
                        "action": "save",
                        "name": name,
                        "backup": backup.name if backup else None,
                    }
                )

            @app.post(f"{PREFIX}/api/personas/delete", dependencies=[Depends(_auth)])
            async def _persona_delete(request: Request) -> JSONResponse:
                try:
                    payload = await request.json()
                except Exception as e:
                    raise HTTPException(status_code=422, detail="invalid json") from e
                pm = _persona_manager()
                if pm is None:
                    raise HTTPException(
                        status_code=409, detail="persona_manager_not_initialized"
                    )
                name = str(payload.get("name") or "").strip()
                if not name:
                    raise HTTPException(status_code=422, detail="name_required")
                target = pm.get(name)
                if target is None:
                    raise HTTPException(status_code=404, detail="persona_not_found")
                if target.default:
                    # 删默认人格会让「未选人格的用户」失去兜底：拒删，先把别的
                    # persona 设为 default 再删（或走 SSH）
                    raise HTTPException(status_code=409, detail="persona_default")
                confirm_key = f"persona:delete:{name}"
                preview = {"name": name}
                token = str(payload.get("confirm_token") or "")
                nonce = str(payload.get("confirm_nonce") or "")
                if not token or not nonce:
                    nonce = secrets.token_hex(8)
                    return JSONResponse(
                        {
                            "need_confirm": True,
                            "confirm_token": _confirm_token(nonce, confirm_key, name),
                            "confirm_nonce": nonce,
                            "expires_in": _WRITE_WINDOW,
                            "action": "delete",
                            "preview": preview,
                        }
                    )
                err = _consume_confirm(nonce, token, confirm_key, name)
                if err:
                    raise HTTPException(status_code=400, detail=err)
                async with _write_lock:
                    try:
                        ok = pm.delete(name)
                    except ValueError as e:
                        raise HTTPException(status_code=422, detail=str(e)) from e
                    except OSError as e:
                        raise HTTPException(status_code=500, detail=str(e)) from e
                if not ok:
                    raise HTTPException(status_code=404, detail="persona_not_found")
                from agentcore.diagnostics import record as _record

                _record(
                    "config_write",
                    key=confirm_key,
                    new=name,
                    mode="persona-delete",
                    source_ip=request.client.host if request.client else "",
                    confirm_nonce=nonce,
                )
                return JSONResponse({"ok": True, "action": "delete", "name": name})

            @app.post(f"{PREFIX}/api/personas/group", dependencies=[Depends(_auth)])
            async def _persona_group(request: Request) -> JSONResponse:
                try:
                    payload = await request.json()
                except Exception as e:
                    raise HTTPException(status_code=422, detail="invalid json") from e
                if not isinstance(payload, dict):
                    raise HTTPException(status_code=422, detail="not_an_object")
                pm = _persona_manager()
                mem = _memory_store()
                if pm is None or mem is None:
                    raise HTTPException(
                        status_code=409, detail="persona_system_not_initialized"
                    )
                action = str(payload.get("action") or "save").strip().lower()
                group_id = str(payload.get("group_id") or "").strip()
                # 群号必须是 ASCII 数字：全角数字 isdigit() 也会 True（历史坑）
                if not (group_id.isascii() and group_id.isdigit()):
                    raise HTTPException(status_code=422, detail="invalid_group_id")
                if action == "save":
                    persona_name = str(payload.get("persona") or "").strip()
                    persona = pm.get(persona_name) if persona_name else None
                    if persona is None:
                        raise HTTPException(status_code=422, detail="persona_not_found")
                    confirm_key = f"persona:group:{group_id}"
                    value_text = f"{group_id}:{persona.name}"
                    preview = {"group_id": group_id, "persona": persona.name}
                elif action == "delete":
                    confirm_key = f"persona:group:{group_id}"
                    value_text = f"delete:{group_id}"
                    preview = {"group_id": group_id}
                else:
                    raise HTTPException(status_code=422, detail="unknown_action")

                token = str(payload.get("confirm_token") or "")
                nonce = str(payload.get("confirm_nonce") or "")
                if not token or not nonce:
                    nonce = secrets.token_hex(8)
                    return JSONResponse(
                        {
                            "need_confirm": True,
                            "confirm_token": _confirm_token(
                                nonce, confirm_key, value_text
                            ),
                            "confirm_nonce": nonce,
                            "expires_in": _WRITE_WINDOW,
                            "action": action,
                            "preview": preview,
                        }
                    )
                err = _consume_confirm(nonce, token, confirm_key, value_text)
                if err:
                    raise HTTPException(status_code=400, detail=err)
                async with _write_lock:
                    if action == "save":
                        await mem.set_group_persona(group_id, preview["persona"])
                    else:
                        existed = await mem.clear_group_persona(group_id)
                        if not existed:
                            raise HTTPException(
                                status_code=404, detail="group_persona_not_found"
                            )
                from agentcore.diagnostics import record as _record

                _record(
                    "config_write",
                    key=confirm_key,
                    new=json.dumps(preview, ensure_ascii=False)[:200],
                    mode="persona-group",
                    source_ip=request.client.host if request.client else "",
                    confirm_nonce=nonce,
                )
                return JSONResponse({"ok": True, "action": action, **preview})

            # ---- 重启面：与写面同门禁 + 两段确认（QQ /reboot 是 superuser 直执行）----
            @app.post(f"{PREFIX}/api/reboot", dependencies=[Depends(_auth)])
            async def _reboot(request: Request) -> JSONResponse:
                try:
                    payload = await request.json()
                except Exception as e:
                    raise HTTPException(status_code=422, detail="invalid json") from e
                if not isinstance(payload, dict):
                    raise HTTPException(status_code=422, detail="not_an_object")
                from plugins.qq_agent_adapter import reboot as rb

                strategy, argv = rb.reboot_plan()
                reason = str(payload.get("reason") or "")
                token = str(payload.get("confirm_token") or "")
                nonce = str(payload.get("confirm_nonce") or "")
                if not token or not nonce:
                    nonce = secrets.token_hex(8)
                    return JSONResponse(
                        {
                            "need_confirm": True,
                            "confirm_token": _confirm_token(
                                nonce, "__reboot__", reason
                            ),
                            "confirm_nonce": nonce,
                            "strategy": strategy,
                            # 与审计同口径只回 argv[:1]：运维可能把连接串/凭据
                            # 写进 AGENT_REBOOT_CMD 参数（REVIEW L8）
                            "cmd": argv[:1] if argv else [],
                            "expires_in": _WRITE_WINDOW,
                        }
                    )
                err = _consume_confirm(nonce, token, "__reboot__", reason)
                if err:
                    raise HTTPException(status_code=400, detail=err)
                from agentcore.diagnostics import record as _record

                _record(
                    "reboot_requested",
                    by=f"web:{request.client.host if request.client else ''}",
                    strategy=strategy,
                    cmd=(argv or [])[:1],
                    reason=reason[:80],
                )
                rb.schedule_reboot()
                return JSONResponse(
                    {
                        "ok": True,
                        "strategy": strategy,
                        "restart_in_seconds": int(rb.reboot_delay()) + 1,
                    }
                )

        logger.info("web 总览已挂载：%s/（token 已配置）", PREFIX)
        return True
    except Exception:
        logger.exception("web 总览挂载失败（不影响 bot 运行）")
        return False
