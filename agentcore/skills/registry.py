from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agentcore.llm.client import LLMClient
from agentcore.skills.permissions import PermissionChecker

logger = logging.getLogger(__name__)


def _group_extra_skills() -> set[str]:
    """AGENT_GROUP_EXTRA_SKILLS：群聊显式加开的技能名（csv，默认空）。

    只对 private 技能生效（白名单加不开别的）；脏值不 crash（未知名字
    无效果），「登记清单与实际注册集一致性」由 drift 守卫在 CI 锁
    （test_group_skill_drift）。
    """
    import os

    raw = (os.getenv("AGENT_GROUP_EXTRA_SKILLS") or "").strip()
    return {x.strip() for x in raw.split(",") if x.strip()}


Handler = Callable[..., Awaitable[Any]]


class Skill:
    def __init__(
        self,
        name: str,
        description: str,
        params_schema: dict,
        handler: Handler,
        permission: str = "public",
        manifest: Any = None,
        read_only: bool = False,
        sessions: str = "all",
    ):
        self.name = name
        self.description = description
        self.params_schema = params_schema
        self.handler = handler
        self.permission = permission
        # where 轴（AC-group-guest-mode D1）：all=群聊/私聊均可见；
        # private=仅私聊（群聊不可见，AGENT_GROUP_EXTRA_SKILLS 可显式加开）。
        # **superuser-only 技能默认即 private**（register 的默认规则，31 处
        # 既有注册零标注自动收窄）——群内 owner 的高危技能面整体移除；
        # 要 group 里用只读类技能走白名单显式加开。
        self.sessions = sessions
        self.manifest = manifest
        # 只读声明：引擎据此判断一步内的多个工具调用可否并行执行。
        # 默认 False——副作用类工具（发文件/写工作区/提醒增删）绝不能并行。
        self.read_only = read_only

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_schema,
            },
        }


class SkillRegistry:
    """Skill 注册表：带权限过滤 + 按用户/群可见性 + 动态安装/卸载。"""

    def __init__(self, permission_checker: PermissionChecker | None = None):
        self.skills: dict[str, Skill] = {}
        self.permission_checker = permission_checker

    def register(
        self,
        name: str,
        description: str,
        params_schema: dict,
        permission: str = "public",
        manifest: Any = None,
        read_only: bool = False,
        sessions: str | None = None,
    ):
        def decorator(handler: Handler):
            # sessions 缺省按权限自动推导：superuser-only → private（群聊
            # 不可见），其余 → all。显式传值可覆盖（只读运维类在白名单
            # AGENT_GROUP_EXTRA_SKILLS 里加开，不改注册）。
            effective_sessions = (
                sessions
                if sessions is not None
                else ("private" if permission == "superuser" else "all")
            )
            self.skills[name] = Skill(
                name,
                description,
                params_schema,
                handler,
                permission,
                manifest,
                read_only,
                effective_sessions,
            )
            return handler

        return decorator

    def mark_read_only(self, *names: str) -> None:
        """给已注册技能补打只读标记（builtin 注册表尾的**集中审计点**）。

        新工具默认非只读（fail-closed）；确认无副作用后在白名单里加名字。
        未知名字静默跳过：条件注册的技能（如未配 SEARCH_API_KEY 的搜索）
        不该让这里报错。
        """
        for name in names:
            skill = self.skills.get(name)
            if skill is None:
                logger.debug("mark_read_only: unknown skill %s", name)
                continue
            skill.read_only = True

    def is_read_only(self, name: str) -> bool:
        """该技能是否声明为只读；未知/未标记一律 False。"""
        skill = self.skills.get(name)
        return bool(skill and skill.read_only)

    def set_permission_checker(self, checker: PermissionChecker) -> None:
        self.permission_checker = checker

    def get_schemas(
        self,
        user_id: str | None = None,
        group_id: str | None = None,
        perm_user_id: str | None = None,
    ) -> list[dict]:
        """按可见性过滤技能 schema。

        ``perm_user_id``：权限身份（AC-cron-trusted，cron trusted 轮次传
        owner）——只影响谁看得到/调得动；数据身份仍用 user_id。
        """
        return [
            s.to_openai_schema()
            for s in self.skills.values()
            if self._is_allowed(s, perm_user_id or user_id, group_id)
        ]

    async def execute(
        self,
        name: str,
        /,
        user_id: str | None = None,
        group_id: str | None = None,
        *,
        delivery_user_id: str | None = None,
        delivery_group_id: str | None = None,
        perm_user_id: str | None = None,
        **kwargs,
    ) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"Error: unknown skill {name}"
        # perm_user_id：权限身份（cron trusted 轮次 = owner）；数据身份仍是
        # user_id（handler 收到的还是它，投递走 delivery_*）。
        if not self._is_allowed(skill, perm_user_id or user_id, group_id):
            # 拒绝与 unknown 同文案（「直接忽略」语义）：对无权限者不确认
            # 该技能的存在——schema 未过滤到的幻觉调用得不到任何探针信息。
            # 只在 execute 拒绝时留 WARNING（含会话键）：get_schemas 每轮
            # LLM 请求都调，若也记会把日志刷爆（AC D1 的留痕口径）
            logger.warning(
                "skill denied by where axis: %s (user=%s group=%s)",
                name,
                user_id,
                group_id or "-",
            )
            return f"Error: unknown skill {name}"
        try:
            params = dict(kwargs)
            # schema 必填项缺失：返回指名错误让模型自纠（此前与真异常同文案，
            # 模型只会原样空转重试）；只查 required，不做类型强校验（避免把
            # "5" 这类能被 handler 容忍的调用打破）
            required = (skill.params_schema or {}).get("required") or []
            missing = [k for k in required if k not in params]
            if missing:
                return f"Error: skill {name} missing required parameters: " + ", ".join(
                    missing
                )
            try:
                sig = inspect.signature(skill.handler)
                if "user_id" in sig.parameters and user_id is not None:
                    params["user_id"] = user_id
                if "group_id" in sig.parameters and group_id is not None:
                    params["group_id"] = group_id
                # 无人值守轮次的投递目标（agent_turn 写入 context）：只注入
                # 声明了这两个参数的主动投递类技能，其余技能不受影响。
                if (
                    "delivery_user_id" in sig.parameters
                    and delivery_user_id is not None
                ):
                    params["delivery_user_id"] = delivery_user_id
                if (
                    "delivery_group_id" in sig.parameters
                    and delivery_group_id is not None
                ):
                    params["delivery_group_id"] = delivery_group_id
            except (TypeError, ValueError):
                pass
            return await skill.handler(**params)
        except Exception:
            logger.exception("skill execution failed: %s", name)
            return "Error: skill 执行失败，请稍后再试。"

    def is_allowed(
        self, skill_name: str, user_id: str | None, group_id: str | None
    ) -> bool:
        skill = self.skills.get(skill_name)
        if not skill:
            return False
        return self._is_allowed(skill, user_id, group_id)

    def _sessions_allowed(self, skill: Skill, group_id: str | None) -> bool:
        """where 轴：private 技能在群聊不可见；显式白名单可加开（只读类）。

        判据用调用参数 group_id（engine 在群聊/私聊分别传真实群号/None）；
        cron 的 agent_turn 同样带目标群 → 无人值守任务天然看不到运维技能
        （纵深：即使 cron 配置被改坏，它能调用的工具面也收窄）。
        """
        if not group_id:
            return True
        if skill.sessions != "private":
            return True
        from agentcore.skills.registry import _group_extra_skills

        return skill.name in _group_extra_skills()

    def _is_allowed(
        self, skill: Skill, user_id: str | None, group_id: str | None
    ) -> bool:
        if not self._sessions_allowed(skill, group_id):
            return False
        if self.permission_checker:
            return self.permission_checker.is_allowed(
                skill.name, user_id, group_id, skill_permission=skill.permission
            )
        return skill.permission == "public"

    def install(self, manifest: Any, handler: Handler | None = None) -> None:
        """动态安装 skill：若未提供 handler，则安装为 prompt skill。

        tool 型 manifest **必须**携带 handler：旧实现会把它静默包装成 prompt
        skill——真搜索技能被同名空 prompt 桩覆盖后，模型只能对着 JSON 编造
        结果，且落盘后每次重启重新覆盖（持久损坏）。这里改为 fail-closed 拒装。
        """
        if manifest.type == "tool" and handler is None:
            raise ValueError(
                f"tool 型技能 {manifest.name} 缺少 handler：工具必须由代码注册，"
                "不能降级为 prompt skill（会静默覆盖同名内置工具）"
            )
        # L10（REVIEW-de09478..workdir）：prompt 型 manifest 不得静默覆盖代码
        # 注册的内置工具。旧实现下 data/skills 里一个同名 manifest 就会让真
        # handler 被空 prompt 桩顶替（模型只能对着 JSON 编造结果），且每次重启
        # 重新覆盖（持久损坏）。tool 型 + 显式 handler 的安装是管理员显式动作
        # （admin handler 对 tool 型直接拒绝），这里只对 prompt 路径 fail-closed。
        if handler is None and manifest.name in self.skills:
            existing = self.skills[manifest.name]
            if existing.manifest is None:  # 代码注册（register()）的内置工具
                raise ValueError(
                    f"拒绝安装：{manifest.name} 是代码注册的内置工具，"
                    "prompt 型 manifest 不得覆盖它"
                )
        # 参数定义里的 name/required 是清单元数据，不是 JSON Schema 字段：
        # 原样塞进 properties 会产出非标准 schema（properties.query={"name":…}），
        # 严格网关按结构校验会整请求 400
        params_schema = {
            "type": "object",
            "properties": {
                p["name"]: {k: v for k, v in p.items() if k not in ("name", "required")}
                for p in (manifest.parameters or [])
            },
            "required": [
                p["name"] for p in (manifest.parameters or []) if p.get("required")
            ],
        }
        if handler is None:
            handler = _make_prompt_skill_handler(manifest)
        self.skills[manifest.name] = Skill(
            name=manifest.name,
            description=manifest.description,
            params_schema=params_schema,
            handler=handler,
            permission=manifest.permission,
            manifest=manifest,
        )
        logger.info("Installed skill: %s", manifest.name)

    def uninstall(self, name: str) -> bool:
        if name not in self.skills:
            return False
        del self.skills[name]
        logger.info("Uninstalled skill: %s", name)
        return True


def _make_prompt_skill_handler(manifest: Any):
    async def _handler(**kwargs):
        return _run_prompt_skill(manifest, kwargs)

    _handler.__name__ = f"prompt_skill_{manifest.name}"
    return _handler


_shared_llm_client = None


def get_shared_llm_client() -> LLMClient:
    """进程内共享 LLMClient（一个 httpx 连接池），供 prompt skill 与引擎复用。

    避免每次执行 prompt 型 skill 都 new 一个连接池且从不释放（P1-4）。
    """
    global _shared_llm_client
    if _shared_llm_client is None:
        _shared_llm_client = LLMClient()
    return _shared_llm_client


async def close_shared_llm_client() -> None:
    """停机回收共享连接池（P1-6）。"""
    global _shared_llm_client
    if _shared_llm_client is not None:
        await _shared_llm_client.aclose()
        _shared_llm_client = None


async def _run_prompt_skill(manifest: Any, arguments: dict) -> str:
    llm = get_shared_llm_client()
    try:
        user_content = json.dumps(arguments, ensure_ascii=False)
        messages = [
            {"role": "system", "content": manifest.prompt},
            {"role": "user", "content": user_content},
        ]
        response = await llm.chat(messages, tools=None)
        choice = (response.get("choices") or [{}])[0].get("message") or {}
        return (choice.get("content") or "").strip() or "（skill 无输出）"
    except Exception:
        logger.exception("prompt skill execution failed: %s", manifest.name)
        return "Error: prompt skill 执行失败，请稍后再试。"


registry = SkillRegistry()
