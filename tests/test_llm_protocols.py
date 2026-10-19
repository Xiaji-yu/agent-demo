"""协议适配：OpenAI Chat 原样；Anthropic Messages 的工具调用双向映射。"""

import json

from agentcore.llm import protocols as proto
from agentcore.llm.providers import Provider


def _p(protocol="openai", base="http://api", model="m"):
    return Provider("p", "P", protocol, base, model, "sk-x", temperature=0.2)


class TestOpenAI:
    def test_build(self):
        url, headers, body = proto.get_protocol("openai").build(
            _p(),
            [{"role": "user", "content": "hi"}],
            [{"type": "function", "function": {"name": "f"}}],
            128,
            default_temperature=0.7,
            default_max_tokens=1024,
        )
        assert url == "http://api/chat/completions"
        assert headers["Authorization"] == "Bearer sk-x"
        assert body["max_tokens"] == 128 and body["temperature"] == 0.2
        assert body["tools"] and body["tool_choice"] == "auto"

    def test_parse_is_identity(self):
        data = {"choices": [{"message": {"content": "ok"}}]}
        assert proto.get_protocol("openai").parse(data) is data


class TestAnthropicBuild:
    def test_url_and_headers(self):
        url, headers, body = proto.get_protocol("anthropic").build(
            _p("anthropic", "https://api.anthropic.com"),
            [{"role": "user", "content": "hi"}],
            None,
            64,
            default_temperature=0.7,
            default_max_tokens=1024,
        )
        assert url == "https://api.anthropic.com/v1/messages"
        assert headers["x-api-key"] == "sk-x"
        assert headers["anthropic-version"] == proto.ANTHROPIC_VERSION
        assert "Authorization" not in headers
        assert body["max_tokens"] == 64

    def test_v1_base_does_not_double(self):
        url, _h, _b = proto.get_protocol("anthropic").build(
            _p("anthropic", "https://h/v1"),
            [{"role": "user", "content": "x"}],
            None,
            16,
            default_temperature=0,
            default_max_tokens=16,
        )
        assert url == "https://h/v1/messages"

    def test_system_hoisted(self):
        _u, _h, body = proto.get_protocol("anthropic").build(
            _p("anthropic"),
            [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "hi"},
            ],
            None,
            64,
            default_temperature=0,
            default_max_tokens=64,
        )
        assert body["system"] == "你是助手"
        assert all(m["role"] != "system" for m in body["messages"])

    def test_tool_roundtrip_blocks(self):
        """assistant tool_calls → tool_use；role=tool → tool_result 合并进一条 user。"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "查天气"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": json.dumps({"city": "北京"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "晴 25℃"},
            {"role": "tool", "tool_call_id": "call_2", "content": "多云"},
        ]
        _u, _h, body = proto.get_protocol("anthropic").build(
            _p("anthropic"),
            messages,
            None,
            64,
            default_temperature=0,
            default_max_tokens=64,
        )
        msgs = body["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        use = msgs[1]["content"][0]
        assert use["type"] == "tool_use" and use["id"] == "call_1"
        assert use["input"] == {"city": "北京"}
        # 两条连续 tool 结果合并进同一条 user
        results = msgs[2]["content"]
        assert [r["type"] for r in results] == ["tool_result", "tool_result"]
        assert results[0]["tool_use_id"] == "call_1"

    def test_tools_schema_mapping(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "f",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        _u, _h, body = proto.get_protocol("anthropic").build(
            _p("anthropic"),
            [{"role": "user", "content": "x"}],
            tools,
            64,
            default_temperature=0,
            default_max_tokens=64,
        )
        assert body["tools"][0] == {
            "name": "f",
            "description": "d",
            "input_schema": {"type": "object", "properties": {}},
        }


class TestAnthropicParse:
    def test_text_and_usage(self):
        out = proto.get_protocol("anthropic").parse(
            {
                "content": [{"type": "text", "text": "你好"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 4},
            }
        )
        msg = out["choices"][0]["message"]
        assert msg["content"] == "你好"
        assert "tool_calls" not in msg
        assert out["choices"][0]["finish_reason"] == "stop"
        assert out["usage"] == {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
        }

    def test_tool_use_normalized_to_openai(self):
        out = proto.get_protocol("anthropic").parse(
            {
                "content": [
                    {"type": "text", "text": "我查一下"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "weather",
                        "input": {"city": "北京"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            }
        )
        msg = out["choices"][0]["message"]
        assert msg["content"] == "我查一下"
        tc = msg["tool_calls"][0]
        assert tc["id"] == "toolu_1"
        assert tc["function"]["name"] == "weather"
        assert json.loads(tc["function"]["arguments"]) == {"city": "北京"}
        assert out["choices"][0]["finish_reason"] == "tool_calls"

    def test_empty_content(self):
        out = proto.get_protocol("anthropic").parse(
            {"content": [], "stop_reason": "end_turn"}
        )
        assert out["choices"][0]["message"]["content"] == ""

    def test_unknown_protocol_raises(self):
        try:
            proto.get_protocol("grpc")
        except ValueError as e:
            assert "unsupported" in str(e)
        else:  # pragma: no cover
            raise AssertionError("应当拒绝未知协议")
