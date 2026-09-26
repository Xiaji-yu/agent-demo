from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agentcore.llm.client import LLMClient
from agentcore.skills.permissions import PermissionChecker

logger = logging.getLogger(__name__)

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
    ):
        self.name = name
        self.description = description
        self.params_schema = params_schema
        self.handler = handler
        self.permission = permission
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
    ):
        def decorator(handler: Handler):
            self.skills[name] = Skill(
                name,
                description,
                params_schema,
                handler,
                permission,
                manifest,
                read_only,
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
        self, user_id: str | None = None, group_id: str | None = None
    ) -> list[dict]:
        return [
            s.to_openai_schema()
            for s in self.skills.values()
            if self._is_allowed(s, user_id, group_id)
        ]

    async def execute(
        self,
        name: str,
        user_id: str | None = None,
        group_id: str | None = None,
        **kwargs,
    ) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"Error: unknown skill {name}"
        if not self._is_allowed(skill, user_id, group_id):
            return f"Error: permission denied for skill {name}"
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

    def _is_allowed(
        self, skill: Skill, user_id: str | None, group_id: str | None
    ) -> bool:
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
