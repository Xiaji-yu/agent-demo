"""LLM 协议适配：请求构造 + 响应归一化（OpenAI Chat / Anthropic Messages）。

引擎只认识 **OpenAI Chat** 形状（``choices[0].message.content`` +
``tool_calls[].function.{name,arguments}``，``role=tool`` 回填）。所以协议层
只做两件事：

1. ``build()``：把 OpenAI 形状的对话转成目标协议的 HTTP 请求；
2. ``parse()``：把目标协议的响应归一化回 OpenAI 形状。

这样上层（工具循环、usage、预算、降级链）完全不需要知道底下是哪家。
Anthropic 的工具调用语义差异最大：tool_use / tool_result 是 content block，
且连续 tool_result 必须合并进同一条 user 消息——这两条在 ``_anthropic_messages``
里集中处理，单测覆盖。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"


class Protocol:
    name = ""

    def build(
        self,
        provider,
        messages: list[dict],
        tools: list[dict] | None,
        max_tokens: int | None,
        *,
        default_temperature: float,
        default_max_tokens: int,
    ) -> tuple[str, dict, dict]:
        raise NotImplementedError

    def parse(self, data: dict) -> dict:
        raise NotImplementedError

    def probe_body(
        self, provider, *, default_max_tokens: int
    ) -> tuple[str, dict, dict]:
        return self.build(
            provider,
            [{"role": "user", "content": "ping"}],
            None,
            16,
            default_temperature=0.0,
            default_max_tokens=default_max_tokens,
        )


class OpenAIProtocol(Protocol):
    name = "openai"

    def build(
        self,
        provider,
        messages,
        tools,
        max_tokens,
        *,
        default_temperature,
        default_max_tokens,
    ):
        body: dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": (
                provider.temperature
                if provider.temperature is not None
                else default_temperature
            ),
            "max_tokens": int(max_tokens or provider.max_tokens or default_max_tokens),
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        url = f"{provider.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }
        return url, headers, body

    def parse(self, data):
        # OpenAI 响应本来就是引擎要的形状；只保证 usage 键存在
        return data


def _image_block(url: str) -> dict | None:
    """OpenAI image_url → Anthropic image block；不支持的形式返回 None。"""
    url = str(url or "")
    if url.startswith("data:image/"):
        header, sep, data = url.partition(",")
        if not sep or ";base64" not in header or not data:
            return None
        media_type = header[len("data:") :].split(";", 1)[0]
        if not media_type.startswith("image/"):
            return None
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _content_blocks(content) -> list[dict]:
    """把 OpenAI 的 content（字符串或 block 数组）转成 Anthropic content blocks。

    数组里的 ``image_url`` 必须映射成 image block——旧实现 ``str(content)``
    会把 base64 整段当文本发出去：模型看不到图，且每步按文本计费（H1）。
    """
    if not isinstance(content, list):
        text = str(content or "")
        return [{"type": "text", "text": text}] if text else []
    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            text = str(part.get("text") or "")
            if text:
                blocks.append({"type": "text", "text": text})
        elif kind == "image_url":
            raw = part.get("image_url")
            url = raw.get("url") if isinstance(raw, dict) else raw
            block = _image_block(str(url or ""))
            if block is not None:
                blocks.append(block)
            else:
                logger.warning("Anthropic: 丢弃不支持的 image_url（scheme/格式）")
    return blocks


def _anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI messages → (system, anthropic messages)。

    - system 抽到顶层；
    - assistant 的 tool_calls → tool_use block；
    - role=tool → tool_result block，**连续多条合并进同一条 user**；
    - 连续同 role 的普通文本消息合并（Anthropic 要求 user/assistant 交替）。
    """
    system_parts: list[str] = []
    out: list[dict] = []

    def _append(role: str, blocks: list[dict]):
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            prev = out[-1]["content"]
            if isinstance(prev, str):
                prev = [{"type": "text", "text": prev}] if prev else []
            out[-1]["content"] = prev + blocks
        else:
            out.append({"role": role, "content": blocks})

    for m in messages or []:
        role = str(m.get("role") or "")
        content = m.get("content")
        if role == "system":
            if content:
                system_parts.append(str(content))
            continue
        if role == "tool":
            _append(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(m.get("tool_call_id") or ""),
                        "content": (
                            _content_blocks(content)
                            if isinstance(content, list)
                            else str(content or "")
                        ),
                    }
                ],
            )
            continue
        if role == "assistant":
            blocks: list[dict] = []
            if isinstance(content, list):
                blocks.extend(
                    b for b in _content_blocks(content) if b.get("type") == "text"
                )
            elif content:
                blocks.append({"type": "text", "text": str(content)})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tc.get("id") or ""),
                        "name": str(fn.get("name") or ""),
                        "input": args,
                    }
                )
            _append("assistant", blocks)
            continue
        _append("user", _content_blocks(content))

    # 空正文的 user/assistant 消息会让 Anthropic 400：给文本块兜底
    for item in out:
        if not item["content"]:
            item["content"] = [{"type": "text", "text": ""}]
    return "\n\n".join(system_parts), out


def _anthropic_tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    out = []
    for t in tools:
        fn = t.get("function") or {}
        out.append(
            {
                "name": str(fn.get("name") or ""),
                "description": str(fn.get("description") or ""),
                "input_schema": fn.get("parameters") or {"type": "object"},
            }
        )
    return out


class AnthropicProtocol(Protocol):
    name = "anthropic"

    def build(
        self,
        provider,
        messages,
        tools,
        max_tokens,
        *,
        default_temperature,
        default_max_tokens,
    ):
        base = provider.base_url.rstrip("/")
        url = f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
        system, msgs = _anthropic_messages(messages)
        body: dict[str, Any] = {
            "model": provider.model,
            "max_tokens": int(max_tokens or provider.max_tokens or default_max_tokens),
            "messages": msgs,
            "temperature": (
                provider.temperature
                if provider.temperature is not None
                else default_temperature
            ),
        }
        if system:
            body["system"] = system
        tools_out = _anthropic_tools(tools)
        if tools_out:
            body["tools"] = tools_out
        headers = {
            "x-api-key": provider.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        return url, headers, body

    def parse(self, data):
        blocks = data.get("content") or []
        text = "".join(
            str(b.get("text") or "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
        tool_calls = []
        for b in blocks:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            tool_calls.append(
                {
                    "id": str(b.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(b.get("name") or ""),
                        "arguments": json.dumps(
                            b.get("input") or {}, ensure_ascii=False
                        ),
                    },
                }
            )
        stop = str(data.get("stop_reason") or "")
        finish = {
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "end_turn": "stop",
            "stop_sequence": "stop",
        }.get(stop, "stop")
        usage = data.get("usage") or {}
        prompt = int(usage.get("input_tokens") or 0)
        completion = int(usage.get("output_tokens") or 0)
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "choices": [{"message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }


_PROTOCOLS = {"openai": OpenAIProtocol(), "anthropic": AnthropicProtocol()}


def get_protocol(name: str) -> Protocol:
    p = _PROTOCOLS.get((name or "openai").strip().lower())
    if p is None:
        raise ValueError(f"unsupported protocol: {name}")
    return p
