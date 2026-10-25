"""Home Assistant 技能（ha_state / ha_control）契约与安全边界。

锁定的面：
1. 配置关闭 / 级别过滤 / superuser 双层门（registry schema + handler）；
2. 实体登记表：读与写都只认 AGENT_HA_ENTITIES；
3. 动作与数值参数白名单（没有任意 service 调用面）；
4. 敏感域两段确认：确认码与参数绑定、单次消费、错误码拒绝、过期拒绝；
5. 结果围栏（设备文本进 prompt 前必须过 fence）；
6. 错误路径不泄漏 token / base_url。
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from agentcore.skills import homeassistant_skill as hs
from agentcore.skills.permissions import PermissionChecker
from agentcore.skills.registry import SkillRegistry

ADMIN = "10001"
OTHER = "20002"
TOKEN_RE = re.compile(r"确认码 ([0-9a-f]{8})")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    hs._PENDING.clear()
    monkeypatch.setattr(hs, "_warned_bad_base", False)
    monkeypatch.setenv("SUPERUSERS", ADMIN)
    monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "medium")
    monkeypatch.setenv("AGENT_HA_BASE_URL", "https://ha.example.com")
    monkeypatch.setenv("AGENT_HA_TOKEN", "secret-token")
    monkeypatch.setenv(
        "AGENT_HA_ENTITIES", "light.living_room,lock.front_door,climate.bedroom"
    )
    yield
    hs._PENDING.clear()


def _install(monkeypatch, handler):
    """把技能内部 client 换成 MockTransport，返回捕获的请求列表。"""
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(
        hs,
        "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)),
    )
    return calls


def _registry() -> SkillRegistry:
    reg = SkillRegistry()
    hs.register_homeassistant_skills(reg)
    reg.set_permission_checker(PermissionChecker(superusers={ADMIN}))
    return reg


def _state(entity_id: str, state: str, **attrs) -> dict:
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


class TestRegistration:
    def test_disabled_without_config(self, monkeypatch):
        monkeypatch.delenv("AGENT_HA_TOKEN")
        reg = SkillRegistry()
        assert hs.register_homeassistant_skills(reg) == []
        assert reg.skills == {}

    def test_control_requires_medium_level(self, monkeypatch):
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        reg = SkillRegistry()
        assert hs.register_homeassistant_skills(reg) == ["ha_state"]
        assert "ha_control" not in reg.skills

    def test_medium_registers_both_as_superuser(self):
        reg = _registry()
        assert set(reg.skills) == {"ha_state", "ha_control"}
        for name in ("ha_state", "ha_control"):
            assert reg.skills[name].permission == "superuser"
            # 只读标记集中在 builtin.py 的 mark_read_only 审计点
            assert reg.skills[name].read_only is False

    def test_builtin_marks_state_readonly_only(self, monkeypatch):
        """集中审计点：ha_state 只读可并行；ha_control 有副作用，永不进名单。"""
        import agentcore.skills.builtin as B

        monkeypatch.delenv("SEARCH_API_KEY", raising=False)
        reg = SkillRegistry()
        B.register_builtin_skills(reg)
        assert reg.is_read_only("ha_state") is True
        assert "ha_control" in reg.skills
        assert reg.is_read_only("ha_control") is False


class TestPermission:
    @pytest.mark.asyncio
    async def test_registry_hides_schema_from_non_superuser(self):
        reg = _registry()
        names = {s["function"]["name"] for s in reg.get_schemas(user_id=OTHER)}
        assert "ha_state" not in names and "ha_control" not in names
        names = {s["function"]["name"] for s in reg.get_schemas(user_id=ADMIN)}
        assert {"ha_state", "ha_control"} <= names

    @pytest.mark.asyncio
    async def test_handler_double_checks_superuser(self):
        reg = _registry()
        direct = await reg.skills["ha_state"].handler(user_id=OTHER)
        assert direct.startswith("无权限")
        # registry 层：与 unknown 同文案，不提供探测口
        via_registry = await reg.execute("ha_state", user_id=OTHER)
        assert via_registry == "Error: unknown skill ha_state"

    @pytest.mark.asyncio
    async def test_handler_rechecks_level_at_call_time(self, monkeypatch):
        reg = _registry()
        assert "ha_control" in reg.skills
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        out = await reg.skills["ha_control"].handler(
            entity_id="light.living_room", action="turn_on", user_id=ADMIN
        )
        assert "权限级别" in out


class TestState:
    @pytest.mark.asyncio
    async def test_lists_only_registered_entities_and_fences(self, monkeypatch):
        calls = _install(
            monkeypatch,
            lambda req: httpx.Response(
                200,
                json=[
                    _state("light.living_room", "on", friendly_name="客厅灯"),
                    _state("sensor.private", "42", friendly_name="不该出现"),
                ],
            ),
        )
        reg = _registry()
        out = await reg.execute("ha_state", user_id=ADMIN)
        assert "light.living_room" in out
        assert "客厅灯" in out
        assert "sensor.private" not in out
        assert out.startswith("----- Home Assistant 状态开始")
        assert len(calls) == 1 and calls[0].url.path == "/api/states"

    @pytest.mark.asyncio
    async def test_single_entity_uses_path_and_bearer(self, monkeypatch):
        calls = _install(
            monkeypatch,
            lambda req: httpx.Response(
                200, json=_state("light.living_room", "off", friendly_name="客厅灯")
            ),
        )
        reg = _registry()
        out = await reg.execute(
            "ha_state", user_id=ADMIN, entity_id="light.living_room"
        )
        assert "off" in out
        assert calls[0].url.path == "/api/states/light.living_room"
        assert calls[0].headers["authorization"] == "Bearer secret-token"
        assert "secret-token" not in out

    @pytest.mark.asyncio
    async def test_unregistered_entity_is_rejected_without_request(self, monkeypatch):
        calls = _install(monkeypatch, lambda req: httpx.Response(200, json=[]))
        reg = _registry()
        out = await reg.execute("ha_state", user_id=ADMIN, entity_id="sensor.private")
        assert "不在 AGENT_HA_ENTITIES" in out
        assert calls == []


class TestControl:
    @pytest.mark.asyncio
    async def test_non_sensitive_action_executes_directly(self, monkeypatch):
        calls = _install(
            monkeypatch,
            lambda req: httpx.Response(
                200, json=[_state("light.living_room", "on", brightness=102)]
            ),
        )
        reg = _registry()
        out = await reg.execute(
            "ha_control",
            user_id=ADMIN,
            entity_id="light.living_room",
            action="turn_on",
            value="40",
        )
        assert "已执行 light.turn_on" in out
        assert out.startswith("----- Home Assistant 执行结果开始")
        body = json.loads(calls[0].content)
        assert calls[0].url.path == "/api/services/light/turn_on"
        assert body == {"brightness_pct": 40, "entity_id": "light.living_room"}

    @pytest.mark.asyncio
    async def test_brightness_out_of_range_rejected(self, monkeypatch):
        calls = _install(monkeypatch, lambda req: httpx.Response(200, json=[]))
        reg = _registry()
        out = await reg.execute(
            "ha_control",
            user_id=ADMIN,
            entity_id="light.living_room",
            action="turn_on",
            value="101",
        )
        assert "亮度需为 1~100" in out
        assert calls == []

    @pytest.mark.asyncio
    async def test_unexpected_value_rejected(self, monkeypatch):
        calls = _install(monkeypatch, lambda req: httpx.Response(200, json=[]))
        reg = _registry()
        out = await reg.execute(
            "ha_control",
            user_id=ADMIN,
            entity_id="light.living_room",
            action="turn_off",
            value="40",
        )
        assert "不接受 value" in out
        assert calls == []

    @pytest.mark.asyncio
    async def test_unknown_action_rejected(self, monkeypatch):
        calls = _install(monkeypatch, lambda req: httpx.Response(200, json=[]))
        reg = _registry()
        out = await reg.execute(
            "ha_control",
            user_id=ADMIN,
            entity_id="light.living_room",
            action="open_cover",
        )
        assert "不支持动作" in out
        assert calls == []

    @pytest.mark.asyncio
    async def test_unlisted_entity_rejected(self, monkeypatch):
        calls = _install(monkeypatch, lambda req: httpx.Response(200, json=[]))
        reg = _registry()
        out = await reg.execute(
            "ha_control", user_id=ADMIN, entity_id="lock.back_door", action="unlock"
        )
        assert "不在 AGENT_HA_ENTITIES" in out
        assert calls == []


class TestSensitiveConfirmation:
    @pytest.mark.asyncio
    async def test_two_stage_unlock(self, monkeypatch):
        calls = _install(
            monkeypatch,
            lambda req: httpx.Response(
                200, json=[_state("lock.front_door", "unlocked")]
            ),
        )
        reg = _registry()

        first = await reg.execute(
            "ha_control", user_id=ADMIN, entity_id="lock.front_door", action="unlock"
        )
        assert first.startswith("确认：")
        assert calls == [], "第一次调用只发确认码，不得真正开门"
        token = TOKEN_RE.search(first).group(1)

        second = await reg.execute(
            "ha_control",
            user_id=ADMIN,
            entity_id="lock.front_door",
            action="unlock",
            token=token,
        )
        assert "已执行 lock.unlock" in second
        assert len(calls) == 1
        assert calls[0].url.path == "/api/services/lock/unlock"

        # 确认码单次消费：同一 token 再来一次必须重新确认
        third = await reg.execute(
            "ha_control",
            user_id=ADMIN,
            entity_id="lock.front_door",
            action="unlock",
            token=token,
        )
        assert "确认码不正确或已过期" in third
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(self, monkeypatch):
        calls = _install(monkeypatch, lambda req: httpx.Response(200, json=[]))
        reg = _registry()
        await reg.execute(
            "ha_control", user_id=ADMIN, entity_id="lock.front_door", action="unlock"
        )
        out = await reg.execute(
            "ha_control",
            user_id=ADMIN,
            entity_id="lock.front_door",
            action="unlock",
            token="deadbeef",
        )
        assert "确认码不正确或已过期" in out
        assert calls == []

    @pytest.mark.asyncio
    async def test_token_is_bound_to_parameters(self, monkeypatch):
        calls = _install(monkeypatch, lambda req: httpx.Response(200, json=[]))
        reg = _registry()
        first = await reg.execute(
            "ha_control", user_id=ADMIN, entity_id="lock.front_door", action="unlock"
        )
        token = TOKEN_RE.search(first).group(1)
        # 换成 lock（换参数）后，旧 token 不适用
        out = await reg.execute(
            "ha_control",
            user_id=ADMIN,
            entity_id="lock.front_door",
            action="lock",
            token=token,
        )
        assert "确认码不正确或已过期" in out
        assert calls == []

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, monkeypatch):
        calls = _install(monkeypatch, lambda req: httpx.Response(200, json=[]))
        reg = _registry()
        first = await reg.execute(
            "ha_control", user_id=ADMIN, entity_id="lock.front_door", action="unlock"
        )
        token = TOKEN_RE.search(first).group(1)
        for entry in hs._PENDING.values():
            entry["ts"] -= hs._CONFIRM_TTL + 1
        out = await reg.execute(
            "ha_control",
            user_id=ADMIN,
            entity_id="lock.front_door",
            action="unlock",
            token=token,
        )
        assert "确认码不正确或已过期" in out
        assert calls == []

    @pytest.mark.asyncio
    async def test_climate_temperature_payload_and_confirm(self, monkeypatch):
        calls = _install(
            monkeypatch,
            lambda req: httpx.Response(
                200,
                json=[_state("climate.bedroom", "heat", temperature=26.0)],
            ),
        )
        reg = _registry()
        first = await reg.execute(
            "ha_control",
            user_id=ADMIN,
            entity_id="climate.bedroom",
            action="set_temperature",
            value="26",
        )
        token = TOKEN_RE.search(first).group(1)
        out = await reg.execute(
            "ha_control",
            user_id=ADMIN,
            entity_id="climate.bedroom",
            action="set_temperature",
            value="26",
            token=token,
        )
        assert "已执行 climate.set_temperature" in out
        body = json.loads(calls[0].content)
        assert body == {"temperature": 26.0, "entity_id": "climate.bedroom"}


class TestTransportErrors:
    @pytest.mark.asyncio
    async def test_http_401_reports_without_leaking_token(self, monkeypatch):
        _install(monkeypatch, lambda req: httpx.Response(401, json={}))
        reg = _registry()
        out = await reg.execute("ha_state", user_id=ADMIN)
        assert "认证失败" in out
        assert "secret-token" not in out
        assert "ha.example.com" not in out

    @pytest.mark.asyncio
    async def test_oversized_response_rejected(self, monkeypatch):
        big = b"x" * (hs._MAX_RESPONSE_BYTES + 1)
        _install(monkeypatch, lambda req: httpx.Response(200, content=big))
        reg = _registry()
        out = await reg.execute("ha_state", user_id=ADMIN)
        assert "响应过大" in out

    @pytest.mark.asyncio
    async def test_redirect_is_not_followed(self, monkeypatch):
        calls = _install(
            monkeypatch,
            lambda req: httpx.Response(
                302, headers={"location": "http://127.0.0.1:8123/api/states"}
            ),
        )
        reg = _registry()
        out = await reg.execute("ha_state", user_id=ADMIN)
        assert "重定向" in out
        assert len(calls) == 1, "不得跟随重定向（防被反代/中间人引到内网）"
