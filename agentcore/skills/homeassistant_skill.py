"""Home Assistant 智能家居（仅管理员）：只读状态查询 + 白名单内设备控制。

安全边界（与 ssh_run / npm_update 同一信任模型）：
- 连接参数只来自 .env：``AGENT_HA_BASE_URL`` / ``AGENT_HA_TOKEN``——LLM 无法
  指定地址（无 SSRF 面），token 不进日志/返回值；不跟随重定向。
- 可访问实体只来自 ``AGENT_HA_ENTITIES`` 登记表：读与写都只认登记过的
  entity_id，LLM 不能拼出越界目标。
- 动作按 domain 枚举（``light.turn_on`` / ``climate.set_temperature`` 等），
  没有任意 service 调用；数值参数只有 brightness_pct（1~100）与 temperature
  （5~35°C），逐项校验。
- 敏感域（lock / cover / alarm_control_panel / climate）走两段确认：首次调用
  只返回确认码（TTL 120s），同参数 + 正确确认码二次调用才真正执行
  （与 proc_kill 同款 token 机制）。
- 设备返回的状态/属性是设备侧可控文本，**在技能内**过 ``fence_untrusted``：
  只靠引擎当轮围栏时，写入历史的 tool 结果仍是原文（跨轮重放裸奔），技能内
  围栏让历史里的副本也带围栏。
- 权限：``permission="superuser"``（schema 过滤）+ handler ``is_superuser``
  双层；``ha_control`` 另需 ``AGENT_PERMISSION_LEVEL`` ≥ medium（low 档不注册，
  handler 内复查）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import urllib.parse as up

import httpx

from agentcore.safety import fence_untrusted
from agentcore.skills.levels import at_least
from agentcore.skills.registry import SkillRegistry
from agentcore.workspace.utils import is_superuser

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
_ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

# 设备控制动作表：domain → 允许的 action（service 名与 action 同名）
_ACTIONS_BY_DOMAIN: dict[str, frozenset[str]] = {
    "light": frozenset({"turn_on", "turn_off", "toggle"}),
    "switch": frozenset({"turn_on", "turn_off", "toggle"}),
    "input_boolean": frozenset({"turn_on", "turn_off", "toggle"}),
    "fan": frozenset({"turn_on", "turn_off", "toggle"}),
    "climate": frozenset({"turn_on", "turn_off", "set_temperature"}),
    "cover": frozenset({"open_cover", "close_cover", "stop_cover"}),
    "lock": frozenset({"lock", "unlock"}),
    "scene": frozenset({"turn_on"}),
}

# 需要两段确认的域：误操作代价高（开门/开锁/改温控）
_SENSITIVE_DOMAINS = frozenset({"lock", "cover", "alarm_control_panel", "climate"})

_TIMEOUT = 10.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_OUTPUT_CHARS = 4000
_CONFIRM_TTL = 120

_STATE_ATTRS = (
    "friendly_name",
    "unit_of_measurement",
    "brightness",
    "color_temp_kelvin",
    "temperature",
    "current_temperature",
    "hvac_mode",
    "hvac_action",
    "position",
    "current_position",
    "battery_level",
    "device_class",
)

# key=(user_id, entity_id, action, value) → {"token", "ts"}；单事件循环内
# 读写在第一个 await 之前完成，天然原子（与 proc_kill 同构）。
_PENDING: dict[tuple[str, str, str, str], dict] = {}

_warned_bad_base = False


def _base_url() -> str:
    """``AGENT_HA_BASE_URL`` 归一化；非法返回空串（整体 fail-closed）。"""
    global _warned_bad_base
    raw = (os.getenv("AGENT_HA_BASE_URL") or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = up.urlsplit(raw)
    except ValueError:
        parsed = None
    ok = bool(parsed) and (
        parsed.scheme.lower() in _ALLOWED_SCHEMES
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )
    if not ok:
        if not _warned_bad_base:
            _warned_bad_base = True
            logger.warning(
                "AGENT_HA_BASE_URL 形态非法（需 http/https、不带凭据/查询串），"
                "Home Assistant 技能整体禁用"
            )
        return ""
    return raw


def _token() -> str:
    return (os.getenv("AGENT_HA_TOKEN") or "").strip()


def load_entities() -> set[str]:
    """解析 ``AGENT_HA_ENTITIES``（逗号分隔的 entity_id）；非法条目告警后跳过。"""
    entities: set[str] = set()
    for part in (os.getenv("AGENT_HA_ENTITIES") or "").split(","):
        eid = part.strip().lower()
        if not eid:
            continue
        if not _ENTITY_RE.match(eid):
            logger.warning("AGENT_HA_ENTITIES: 忽略非法条目 %r", part.strip())
            continue
        entities.add(eid)
    return entities


def _configured() -> bool:
    return bool(_base_url() and _token() and load_entities())


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False)


async def _request(
    method: str, path: str, payload: dict | None = None
) -> tuple[int, object | None, str]:
    """调用 HA REST API；返回 (status, json, error)。错误信息不含 URL/token。"""
    base, token = _base_url(), _token()
    if not base or not token:
        return (
            0,
            None,
            "未配置 AGENT_HA_BASE_URL / AGENT_HA_TOKEN，智能家居技能已关闭。",
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        client = _make_client()
        async with client:
            async with client.stream(
                method, f"{base}{path}", headers=headers, json=payload
            ) as resp:
                status = resp.status_code
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes(65536):
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        return status, None, "Home Assistant 响应过大，已拒绝处理"
                    chunks.append(chunk)
                body = b"".join(chunks)
    except httpx.TimeoutException:
        return 0, None, f"请求 Home Assistant 超时（{_TIMEOUT:.0f}s）"
    except httpx.HTTPError:
        return 0, None, "无法连接 Home Assistant（检查 AGENT_HA_BASE_URL 与网络）"

    if 300 <= status < 400:
        return (
            status,
            None,
            "Home Assistant 返回了重定向：请把 AGENT_HA_BASE_URL 直接配成最终地址",
        )
    if status in (401, 403):
        return status, None, "认证失败（AGENT_HA_TOKEN 无效或权限不足）"
    if status == 404:
        return status, None, "实体不存在（检查 entity_id 拼写）"
    if status >= 400:
        return status, None, f"Home Assistant 返回 HTTP {status}"
    if not body:
        return status, None, ""
    try:
        return status, json.loads(body), ""
    except ValueError:
        return status, None, "Home Assistant 响应不是合法 JSON"


def _format_state(state: dict) -> str:
    eid = str(state.get("entity_id") or "?")
    attrs = state.get("attributes")
    if not isinstance(attrs, dict):
        attrs = {}
    name = str(attrs.get("friendly_name") or "").replace("\n", " ")[:60]
    parts = [str(state.get("state") or "unknown")]
    for key in _STATE_ATTRS:
        if key == "friendly_name" or key not in attrs:
            continue
        value = attrs[key]
        if isinstance(value, dict | list):
            continue
        parts.append(f"{key}={str(value).replace(chr(10), ' ')[:60]}")
    line = eid + (f"（{name}）" if name else "") + "：" + "，".join(parts)
    return line[:300]


def _denied_text(user_id: str) -> str | None:
    if not is_superuser(user_id):
        return "无权限：仅管理员可使用智能家居技能。"
    return None


async def ha_state(entity_id: str = "", user_id: str = "") -> str:
    """查询登记实体的当前状态；不传 entity_id 时列出全部登记实体。"""
    denied = _denied_text(user_id)
    if denied:
        return denied
    entities = load_entities()
    if not entities:
        return "未配置 AGENT_HA_ENTITIES（或为空），智能家居技能已关闭。"

    eid = (entity_id or "").strip().lower()
    if eid:
        if not _ENTITY_RE.match(eid):
            return "参数错误：entity_id 形态不合法（形如 light.living_room）"
        if eid not in entities:
            return f"拒绝：{eid} 不在 AGENT_HA_ENTITIES 登记表内"
        _, data, err = await _request("GET", f"/api/states/{eid}")
        if err:
            return err
        if not isinstance(data, dict):
            return "Home Assistant 返回了意外的数据"
        body = _format_state(data)
    else:
        _, data, err = await _request("GET", "/api/states")
        if err:
            return err
        if not isinstance(data, list):
            return "Home Assistant 返回了意外的数据"
        rows = [
            _format_state(st)
            for st in data
            if isinstance(st, dict) and st.get("entity_id") in entities
        ]
        if not rows:
            return "登记表内的实体当前都没有状态（检查 entity_id 是否拼写正确）"
        body = "\n".join(sorted(rows))

    if len(body) > _MAX_OUTPUT_CHARS:
        body = body[:_MAX_OUTPUT_CHARS] + "\n…（状态过长，已截断）"
    return fence_untrusted("Home Assistant 状态", body, "智能家居设备上报")


def _prune_pending(now: float) -> None:
    for key in [k for k, v in _PENDING.items() if now - v["ts"] > _CONFIRM_TTL]:
        _PENDING.pop(key, None)


def _payload(domain: str, action: str, value: str) -> tuple[dict | None, str]:
    raw = (value or "").strip()
    if action == "set_temperature":
        if not raw:
            return None, "参数错误：set_temperature 需要 value（目标温度 °C）"
        try:
            temp = float(raw)
        except ValueError:
            return None, "参数错误：value 必须是数字（°C）"
        if not 5.0 <= temp <= 35.0:
            return None, "参数错误：温度需在 5~35°C"
        return {"temperature": temp}, ""
    if action == "turn_on" and domain == "light" and raw:
        try:
            pct = int(raw)
        except ValueError:
            return None, "参数错误：亮度需为 1~100 的整数"
        if not 1 <= pct <= 100:
            return None, "参数错误：亮度需为 1~100"
        return {"brightness_pct": pct}, ""
    if raw:
        return None, (
            f"参数错误：{domain}.{action} 不接受 value"
            "（仅 light.turn_on 亮度 / climate.set_temperature 温度）"
        )
    return None, ""


async def ha_control(
    entity_id: str,
    action: str,
    value: str = "",
    token: str = "",
    user_id: str = "",
) -> str:
    """对登记实体执行枚举动作；敏感域需 token 二次确认。"""
    denied = _denied_text(user_id)
    if denied:
        return denied
    if not at_least("medium"):
        return (
            "当前权限级别不允许设备控制（需要 AGENT_PERMISSION_LEVEL=medium 或 high）。"
        )

    entities = load_entities()
    if not entities:
        return "未配置 AGENT_HA_ENTITIES（或为空），智能家居技能已关闭。"

    eid = (entity_id or "").strip().lower()
    act = (action or "").strip().lower()
    if not _ENTITY_RE.match(eid):
        return "参数错误：entity_id 形态不合法（形如 light.living_room）"
    if eid not in entities:
        return f"拒绝：{eid} 不在 AGENT_HA_ENTITIES 登记表内"
    domain = eid.split(".", 1)[0]
    allowed = _ACTIONS_BY_DOMAIN.get(domain)
    if not allowed:
        return f"拒绝：不支持控制 domain「{domain}」"
    if act not in allowed:
        return f"拒绝：{domain} 不支持动作「{act}」；可选：{'、'.join(sorted(allowed))}"

    payload, err = _payload(domain, act, value)
    if err:
        return err

    norm_value = (value or "").strip()
    if domain in _SENSITIVE_DOMAINS:
        now = time.monotonic()
        _prune_pending(now)
        key = (user_id or "-", eid, act, norm_value)
        if not token:
            new_token = secrets.token_hex(4)
            _PENDING[key] = {"token": new_token, "ts": now}
            detail = f"（value={norm_value}）" if norm_value else ""
            return (
                f"确认：将对 {eid} 执行 {domain}.{act}{detail}\n"
                f"确认码 {new_token}（{_CONFIRM_TTL} 秒内有效）。"
                "确认无误后带上该确认码、参数完全一致地重新调用本工具即可执行；"
                "不确认则什么都不做。"
            )
        pending = _PENDING.get(key)
        if pending is None or not secrets.compare_digest(pending["token"], token):
            _PENDING.pop(key, None)
            return "错误：确认码不正确或已过期——请重新发起（不带 token 调用一次）"
        _PENDING.pop(key, None)

    _, data, err = await _request(
        "POST",
        f"/api/services/{domain}/{act}",
        {**(payload or {}), "entity_id": eid},
    )
    if err:
        return err
    logger.info("ha_control: user=%s entity=%s action=%s", user_id, eid, act)

    lines = [f"已执行 {domain}.{act} → {eid}"]
    if isinstance(data, list):
        for st in data[:5]:
            if isinstance(st, dict) and st.get("entity_id"):
                lines.append(_format_state(st))
    body = "\n".join(lines)[:_MAX_OUTPUT_CHARS]
    return fence_untrusted("Home Assistant 执行结果", body, "智能家居设备上报")


_STATE_DESCRIPTION = (
    "查询 Home Assistant 中**管理员登记过**的实体状态（仅管理员）。"
    "不给 entity_id 时返回全部登记实体的当前状态，给了则只查那一个；"
    "读不到未登记的实体。控制设备请用 ha_control。"
)

_CONTROL_DESCRIPTION = (
    "控制 Home Assistant 中**管理员登记过**的设备（仅管理员，medium+）。"
    "entity_id 必须在 AGENT_HA_ENTITIES 内，动作按 domain 固定："
    + "；".join(
        f"{domain}: {'/'.join(sorted(actions))}"
        for domain, actions in sorted(_ACTIONS_BY_DOMAIN.items())
    )
    + "。lock/cover/alarm_control_panel/climate 需两段确认：先不带 token 调用"
    "拿到确认码，再带 token 且参数完全一致地二次调用才执行。"
)


def register_homeassistant_skills(registry: SkillRegistry) -> list[str]:
    """注册 HA 技能；未配置 base_url/token/entities 时整体关闭。"""
    if not _configured():
        return []
    names = ["ha_state"]

    @registry.register(
        "ha_state",
        _STATE_DESCRIPTION,
        {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "要查询的实体，如 light.living_room；省略则列出全部登记实体",
                }
            },
        },
        permission="superuser",
    )
    async def ha_state_skill(entity_id: str = "", user_id: str = "") -> str:
        return await ha_state(entity_id=entity_id, user_id=user_id)

    if at_least("medium"):

        @registry.register(
            "ha_control",
            _CONTROL_DESCRIPTION,
            {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "登记过的实体，如 light.living_room",
                    },
                    "action": {
                        "type": "string",
                        "description": "动作名（按 domain 枚举，见工具描述）",
                    },
                    "value": {
                        "type": "string",
                        "description": "可选数值：light.turn_on 亮度 1~100 / "
                        "climate.set_temperature 温度 5~35°C",
                    },
                    "token": {
                        "type": "string",
                        "description": "敏感域第二次调用时填第一次返回的确认码",
                    },
                },
                "required": ["entity_id", "action"],
            },
            permission="superuser",
        )
        async def ha_control_skill(
            entity_id: str,
            action: str,
            value: str = "",
            token: str = "",
            user_id: str = "",
        ) -> str:
            return await ha_control(
                entity_id=entity_id,
                action=action,
                value=value,
                token=token,
                user_id=user_id,
            )

        names.append("ha_control")

    return names
