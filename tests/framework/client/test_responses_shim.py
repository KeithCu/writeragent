# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for OpenAI Responses API path, payload building, stream and sync parsing."""

from __future__ import annotations

import json
from typing import Any

import pytest

from plugin.framework.client.errors import stream_error_message
from plugin.framework.client.grok_shim import GrokShim
from plugin.framework.client.openai_shim import OpenAIShim, OpenRouterShim
from plugin.framework.client.responses_shim import (
    is_responses_body,
    parse_responses_chunk,
    parse_responses_sync,
)
from plugin.framework.client.stream_normalizer import (
    accumulate_streaming_thinking,
    extract_reasoning_replay_from_response,
    iterate_sse,
)
from plugin.framework.errors import NetworkError


class DummyClient:
    def __init__(self, config: dict[str, Any] | None = None, provider: str = "openai") -> None:
        self.config = config if config is not None else {}
        self._provider = provider

    def _get_provider(self) -> str:
        return self._provider

    def _endpoint(self) -> str:
        return "https://api.openai.com"

    def _api_path(self) -> str:
        return "/v1"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test"}


def test_conversion_messages_and_tools() -> None:
    client = DummyClient()
    shim = OpenAIShim(client)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "test_tool",
                "description": "desc",
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
            },
        }
    ]
    messages = [
        {"role": "system", "content": "sys 1"},
        {"role": "user", "content": "hello"},
        {"role": "developer", "content": "dev 1"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "image query"},
                {"type": "image_url", "image_url": "https://example.com/a.png"},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "dict query"},
                {"type": "image_url", "image_url": {"url": "https://example.com/b.png", "detail": "high"}},
            ],
        },
        {
            "role": "assistant",
            "content": "thinking answer",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "test_tool", "arguments": '{"x": "1"}'}}
            ],
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "blob1", "model": "gpt-6-astra"}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "tool output"},
    ]
    _method, path, body, _headers = shim.build_chat_request(
        messages, 100, None, tools, False, "gpt-6-astra", None
    )
    data = json.loads(body)
    assert path.endswith("/responses")
    assert data["instructions"] == "sys 1\n\ndev 1"
    items = data["input"]
    for item in items:
        if item.get("type") == "message":
            assert item.get("role") not in ("system", "developer")

    assert items[0] == {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    assert items[1] == {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "image query"},
            {"type": "input_image", "image_url": "https://example.com/a.png"},
        ],
    }
    assert items[2] == {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "dict query"},
            {"type": "input_image", "image_url": "https://example.com/b.png", "detail": "high"},
        ],
    }

    assert items[3] == {"type": "reasoning", "encrypted_content": "blob1", "summary": []}
    assert items[4] == {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "thinking answer"}],
    }
    assert items[5] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "test_tool",
        "arguments": '{"x": "1"}',
    }

    assert items[6] == {"type": "function_call_output", "call_id": "call_1", "output": "tool output"}


def test_reasoning_details_strips_id_and_assistant_message_has_no_id() -> None:
    client = DummyClient()
    shim = OpenAIShim(client)
    tools = [{"type": "function", "function": {"name": "fn", "parameters": {}}}]
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "id": "msg_123",
            "content": "reply",
            "reasoning_details": [
                {
                    "type": "reasoning.encrypted",
                    "data": "blob",
                    "format": "openai-responses-v1",
                    "id": "rs_should_not_leak",
                    "model": "gpt-6-astra",
                }
            ],
        },
    ]
    _method, _path, body, _headers = shim.build_chat_request(
        messages, 100, None, tools, False, "gpt-6-astra", None
    )
    data = json.loads(body)
    items = data["input"]
    r_item = items[1]
    assert r_item == {"type": "reasoning", "encrypted_content": "blob", "summary": []}
    assert "id" not in r_item
    msg_item = items[2]
    assert msg_item == {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "reply"}],
    }
    assert "id" not in msg_item


def test_encrypted_reasoning_dropped_on_model_mismatch_and_unstamped_kept() -> None:
    client = DummyClient()
    shim = OpenAIShim(client)
    tools = [{"type": "function", "function": {"name": "fn", "parameters": {}}}]
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "id": "msg_123",
            "content": "reply",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "fn", "arguments": "{}"}}],
            "reasoning_details": [
                {"type": "reasoning.encrypted", "data": "blob_luna", "format": "openai-responses-v1", "model": "gpt-6-luna"},
                {"type": "reasoning.encrypted", "data": "blob_nostamp", "format": "openai-responses-v1"},
            ],
        },
    ]
    _method, _path, body, _headers = shim.build_chat_request(
        messages, 100, None, tools, False, "gpt-6-astra", None
    )
    data = json.loads(body)
    items = data["input"]
    assert items[1] == {"type": "reasoning", "encrypted_content": "blob_nostamp", "summary": []}
    assert items[2] == {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "reply"}],
    }
    assert "id" not in items[2]
    assert items[3] == {"type": "function_call", "call_id": "c1", "name": "fn", "arguments": "{}"}


def test_duplicate_call_id_deduplication() -> None:
    client = DummyClient()
    shim = OpenAIShim(client)
    tools = [{"type": "function", "function": {"name": "fn", "parameters": {}}}]
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_abc", "type": "function", "function": {"name": "fn", "arguments": "{}"}},
                {"id": "call_abc", "type": "function", "function": {"name": "fn", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_abc", "content": "out 1"},
        {"role": "tool", "tool_call_id": "call_abc", "content": "out 2"},
    ]
    _method, _path, body, _headers = shim.build_chat_request(
        messages, 100, None, tools, False, "gpt-6-astra", None
    )
    data = json.loads(body)
    items = data["input"]
    assert items[1]["call_id"] == "call_abc"
    assert items[2]["call_id"] == "call_abc_dup1"
    assert items[3]["call_id"] == "call_abc"
    assert items[4]["call_id"] == "call_abc_dup1"


def test_tool_call_missing_id_or_name_is_omitted_and_tool_result_omitted() -> None:
    client = DummyClient()
    shim = OpenAIShim(client)
    tools = [{"type": "function", "function": {"name": "fn", "parameters": {}}}]
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "", "function": {"name": "fn", "arguments": "{}"}},
                {"id": "call_noname", "function": {"name": "", "arguments": "{}"}},
                {"id": "call_good", "function": {"name": "fn", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "", "content": "out 0"},
        {"role": "tool", "tool_call_id": "call_noname", "content": "out noname"},
        {"role": "tool", "tool_call_id": "call_good", "content": "out good"},
    ]
    _method, _path, body, _headers = shim.build_chat_request(
        messages, 100, None, tools, False, "gpt-6-astra", None
    )
    data = json.loads(body)
    items = data["input"]
    assert len(items) == 3
    assert items[1]["call_id"] == "call_good"
    assert items[2]["call_id"] == "call_good"


def test_payload_structure_and_format() -> None:
    client = DummyClient()
    shim = OpenAIShim(client)
    tools = [
        {
            "type": "function",
            "strict": True,
            "function": {"name": "fn", "description": "d", "parameters": {"type": "object"}},
        }
    ]
    resp_fmt_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "my_schema",
            "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
            "strict": True,
            "description": "desc",
        },
    }
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 500, None, tools, True, "gpt-6-astra", resp_fmt_schema
    )
    data = json.loads(body)
    assert data["store"] is False
    assert data["include"] == ["reasoning.encrypted_content"]
    assert data["max_output_tokens"] == 500
    assert "max_tokens" not in data
    assert "max_completion_tokens" not in data
    assert data["stream"] is True
    assert data["tool_choice"] == "auto"
    assert data["parallel_tool_calls"] is False
    assert data["tools"] == [
        {
            "type": "function",
            "name": "fn",
            "description": "d",
            "strict": False,
            "parameters": {"type": "object"},
        }
    ]
    assert data["text"] == {
        "format": {
            "type": "json_schema",
            "name": "my_schema",
            "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
            "strict": True,
            "description": "desc",
        }
    }

    _method, _path, body2, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 500, None, tools, False, "gpt-6-astra", {"type": "json_object"}
    )
    assert json.loads(body2)["text"] == {"format": {"type": "json_object"}}


def test_effort_and_temperature_handling() -> None:
    tools = [{"type": "function", "function": {"name": "fn", "parameters": {}}}]
    c1 = DummyClient(config={"reasoning_effort": "high"})
    shim1 = OpenAIShim(c1)
    _m, _p, b1, _h = shim1.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, 0.2, tools, False, "gpt-6-astra", None
    )
    d1 = json.loads(b1)
    assert d1["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "temperature" not in d1

    chat_extra = {"reasoning": {"effort": "minimal"}}
    _m, _p, b2, _h = shim1.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, 0.2, tools, False, "gpt-6-astra", None, chat_extra=chat_extra
    )
    d2 = json.loads(b2)
    assert d2["reasoning"] == {"effort": "minimal", "summary": "auto"}
    assert "temperature" not in d2
    assert chat_extra == {"reasoning": {"effort": "minimal"}}

    c0 = DummyClient(config={})
    shim0 = OpenAIShim(c0)
    _m, _p, b0, _h = shim0.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, 0.2, tools, False, "gpt-6-astra", None
    )
    d0 = json.loads(b0)
    assert "reasoning" not in d0
    assert "temperature" not in d0

    c_none = DummyClient(config={"reasoning_effort": "none"})
    shim_none = OpenAIShim(c_none)
    _m, _p, b_none, _h = shim_none.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, 0.2, tools, False, "gpt-6-astra", None
    )
    d_none = json.loads(b_none)
    assert d_none["reasoning"]["effort"] == "none"
    assert d_none["temperature"] == 0.2


@pytest.mark.parametrize(
    "model_name",
    [
        "gpt-6-astra",
        "gpt-6.1-sol",
        "gpt-6.1-terra",
        "gpt-6-luna",
        "gpt-6-sol",
        "openai/gpt-6-astra",
        "ft:gpt-6-astra:org:name:id",
    ],
)
def test_routing_gpt6_with_tools_uses_responses(model_name: str) -> None:
    client = DummyClient()
    shim = OpenAIShim(client)
    tools = [{"type": "function", "function": {"name": "fn", "parameters": {}}}]
    _m, path, body, _h = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, tools, False, model_name, None
    )
    assert path.endswith("/responses")
    data = json.loads(body)
    assert "input" in data


@pytest.mark.parametrize("tools", [None, []])
def test_routing_gpt6_without_tools_stays_on_chat(tools: Any) -> None:
    client = DummyClient()
    shim = OpenAIShim(client)
    _m, path, body, _h = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, tools, False, "gpt-6-astra", None
    )
    assert path.endswith("/chat/completions")
    data = json.loads(body)
    assert "messages" in data


@pytest.mark.parametrize("model_name", ["gpt-4o", "o3", "gpt-5.4"])
def test_routing_non_gpt6_with_tools_stays_on_chat(model_name: str) -> None:
    client = DummyClient()
    shim = OpenAIShim(client)
    tools = [{"type": "function", "function": {"name": "fn", "parameters": {}}}]
    _m, path, body, _h = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, tools, False, model_name, None
    )
    assert path.endswith("/chat/completions")
    data = json.loads(body)
    assert "messages" in data


def test_routing_other_providers_stay_on_chat() -> None:
    tools = [{"type": "function", "function": {"name": "fn", "parameters": {}}}]
    or_client = DummyClient(config={"is_openrouter": True}, provider="openrouter")
    or_shim = OpenRouterShim(or_client)
    _m, path_or, _b, _h = or_shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, tools, False, "openai/gpt-6-astra", None
    )
    assert path_or.endswith("/chat/completions")

    grok_client = DummyClient(provider="xai")
    grok_shim = GrokShim(grok_client)
    _m, path_grok, _b, _h = grok_shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, tools, False, "gpt-6-astra", None
    )
    assert path_grok.endswith("/chat/completions")


@pytest.mark.parametrize(
    ("flag", "model_name", "suffix"),
    [
        (False, "openai/gpt-6-astra", "/chat/completions"),
        (True, "openai/gpt-6-astra", "/responses"),
        (True, "OpenAI/gpt-6-astra", "/responses"),
        (True, "openai/gpt-oss-20b", "/chat/completions"),
        (True, "openai/gpt-6-luna", "/responses"),
        (True, "openai/gpt-5.4", "/responses"),
        (True, "openai/gpt-oss-120b:free", "/chat/completions"),
        (True, "anthropic/claude-sonnet-4", "/chat/completions"),
        (True, "gpt-6-astra", "/chat/completions"),
    ],
)
def test_openrouter_responses_flag_routing(monkeypatch: pytest.MonkeyPatch, flag: bool, model_name: str, suffix: str) -> None:
    from plugin.framework.client import openai_shim

    monkeypatch.setattr(openai_shim, "OPENROUTER_USE_RESPONSES", flag)
    tools = [{"type": "function", "function": {"name": "fn", "parameters": {}}}]
    shim = OpenRouterShim(DummyClient(config={"is_openrouter": True}, provider="openrouter"))
    _m, path, body, _h = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, tools, True, model_name, None
    )
    assert path.endswith(suffix)
    data = json.loads(body)
    assert ("input" in data) == (suffix == "/responses")
    _m, path, _b, _h = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, True, model_name, None
    )
    assert path.endswith("/chat/completions")
    content, *_rest = shim.parse_response_chunk({"type": "response.output_text.delta", "delta": "x"}, {})
    assert content == ("x" if flag else "")


def test_reasoning_details_stripped_for_chat_kept_for_responses() -> None:
    client = DummyClient()
    shim = OpenAIShim(client)
    tools = [{"type": "function", "function": {"name": "fn", "parameters": {}}}]
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "prev",
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "blob", "model": "gpt-6-astra"}],
        },
    ]
    _m, path_chat, body_chat, _h = shim.build_chat_request(
        messages, 100, None, None, False, "gpt-6-astra", None
    )
    assert path_chat.endswith("/chat/completions")
    data_chat = json.loads(body_chat)
    assert "reasoning_details" not in data_chat["messages"][1]

    _m, path_resp, body_resp, _h = shim.build_chat_request(
        messages, 100, None, tools, False, "gpt-6-astra", None
    )
    assert path_resp.endswith("/responses")
    data_resp = json.loads(body_resp)
    types = [it.get("type") for it in data_resp["input"]]
    assert "reasoning" in types


OPENAI_STREAM_EVENTS: list[dict[str, Any]] = [
    {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-6-astra-2026-03-01"}},
    {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
    {"type": "response.reasoning_summary_text.delta", "delta": "Thinking step 1. "},
    {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {"type": "reasoning", "id": "rs_1", "encrypted_content": "enc_blob_123"},
    },
    {"type": "response.output_text.delta", "delta": "Hello "},
    {"type": "response.output_text.delta", "delta": "world!"},
    {
        "type": "response.output_item.added",
        "output_index": 1,
        "item": {"type": "function_call", "id": "call_wire_1", "name": "weather", "arguments": ""},
    },
    {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"location": '},
    {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '"Boston"}'},
    {"type": "response.function_call_arguments.done", "output_index": 1, "arguments": '{"location": "Boston"}'},
    {
        "type": "response.completed",
        "response": {
            "id": "resp_1",
            "model": "gpt-6-astra-2026-03-01",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Hello world!"}]}],
            "usage": {
                "input_tokens": 15,
                "output_tokens": 25,
                "output_tokens_details": {"reasoning_tokens": 10},
            },
        },
    },
]


def test_openai_stream_parsing() -> None:
    stream_state: dict[str, Any] = {}
    content_acc = ""
    thinking_acc = ""
    last_finish = None
    last_delta: dict[str, Any] = {}
    tool_calls_acc: list[dict[str, Any]] = []
    encrypted_details: list[dict[str, Any]] = []

    for event in OPENAI_STREAM_EVENTS:
        content, finish, thinking, delta = parse_responses_chunk(
            event, stream_state, model_name="gpt-6-astra"
        )
        if content:
            content_acc += content
        if thinking:
            thinking_acc += thinking
        if finish:
            last_finish = finish
        if delta:
            last_delta.update(delta)
            if "tool_calls" in delta:
                tool_calls_acc.extend(delta["tool_calls"])
            if "reasoning_details" in delta:
                encrypted_details.extend(delta["reasoning_details"])

    assert content_acc == "Hello world!"
    assert thinking_acc == "Thinking step 1. "
    assert last_finish == "tool_calls"

    # Verify tool calls: added event, two arg deltas, done ignored
    assert len(tool_calls_acc) == 3
    assert tool_calls_acc[0] == {
        "index": 0,
        "id": "call_wire_1",
        "type": "function",
        "function": {"name": "weather", "arguments": ""},
    }
    assert tool_calls_acc[1] == {"index": 0, "function": {"arguments": '{"location": '}}
    assert tool_calls_acc[2] == {"index": 0, "function": {"arguments": '"Boston"}'}}

    # Verify usage normalized and attached to final delta
    assert last_delta["usage"] == {
        "input_tokens": 15,
        "prompt_tokens": 15,
        "output_tokens": 25,
        "completion_tokens": 25,
        "output_tokens_details": {"reasoning_tokens": 10},
    }
    assert last_delta["model"] == "gpt-6-astra-2026-03-01"

    # Verify encrypted reasoning details
    assert len(encrypted_details) == 1
    enc = encrypted_details[0]
    assert enc["type"] == "reasoning.encrypted"
    assert enc["data"] == "enc_blob_123"
    assert enc["format"] == "openai-responses-v1"
    assert enc["model"] == "gpt-6-astra-2026-03-01"
    assert "id" not in enc


def test_gap_in_output_indexes_allocates_sequential_tool_indexes() -> None:
    state: dict[str, Any] = {}
    _c1, _f1, _t1, d1 = parse_responses_chunk(
        {"type": "response.output_item.added", "output_index": 1, "item": {"type": "function_call", "name": "f1"}},
        state,
        model_name="gpt-6-astra",
    )
    _c2, _f2, _t2, d2 = parse_responses_chunk(
        {"type": "response.output_item.added", "output_index": 3, "item": {"type": "function_call", "name": "f2"}},
        state,
        model_name="gpt-6-astra",
    )
    assert d1["tool_calls"][0]["index"] == 0
    assert d2["tool_calls"][0]["index"] == 1


def test_zero_arg_tool_call_emits_empty_arguments() -> None:
    state: dict[str, Any] = {}
    _c1, _f1, _t1, d1 = parse_responses_chunk(
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "name": "zero_arg"}},
        state,
        model_name="gpt-6-astra",
    )
    _c2, _f2, _t2, d2 = parse_responses_chunk(
        {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": ""},
        state,
        model_name="gpt-6-astra",
    )
    assert d1["tool_calls"][0]["index"] == 0
    assert d2["tool_calls"][0]["function"]["arguments"] == ""


def test_done_only_after_added_emits_full_arguments_string() -> None:
    state: dict[str, Any] = {}
    _c1, _f1, _t1, d1 = parse_responses_chunk(
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "name": "f1"}},
        state,
        model_name="gpt-6-astra",
    )
    _c2, _f2, _t2, d2 = parse_responses_chunk(
        {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": '{"a": 1}'},
        state,
        model_name="gpt-6-astra",
    )
    assert d1["tool_calls"][0]["index"] == 0
    assert d2["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'


OPENROUTER_STREAM_EVENTS: list[dict[str, Any]] = [
    {"type": "response.content_part.delta", "delta": "First "},
    {"type": "response.content_part.delta", "part": {"text": "second"}},
    {"type": "response.reasoning.delta", "delta": "Thinking..."},
    {"type": "response.done", "response": {"usage": {"input_tokens": 5, "output_tokens": 10}}},
]


def test_openrouter_stream_parsing() -> None:
    state: dict[str, Any] = {}
    content_acc = ""
    thinking_acc = ""
    last_finish = None

    for event in OPENROUTER_STREAM_EVENTS:
        content, finish, thinking, _d = parse_responses_chunk(event, state, model_name="gpt-6-astra")
        if content:
            content_acc += content
        if thinking:
            thinking_acc += thinking
        if finish:
            last_finish = finish

    assert content_acc == "First second"
    assert thinking_acc == "Thinking..."
    assert last_finish == "stop"


def test_refusal_delta_yields_content_text_and_no_finish_reason() -> None:
    content, finish, thinking, delta = parse_responses_chunk(
        {"type": "response.refusal.delta", "delta": "I cannot help with that."},
        None,
        model_name="gpt-6-astra",
    )
    assert content == "I cannot help with that."
    assert finish is None
    assert thinking is None
    assert delta == {"content": "I cannot help with that."}


def test_response_incomplete_reasons() -> None:
    _c, f_len, _t, _d = parse_responses_chunk(
        {"type": "response.incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        None,
        model_name="gpt-6-astra",
    )
    assert f_len == "length"

    _c, f_filter, _t, _d = parse_responses_chunk(
        {"type": "response.incomplete", "incomplete_details": {"reason": "content_filter"}},
        None,
        model_name="gpt-6-astra",
    )
    assert f_filter == "content_filter"

    _c, f_other, _t, _d = parse_responses_chunk(
        {"type": "response.incomplete", "incomplete_details": {"reason": "cancelled"}},
        None,
        model_name="gpt-6-astra",
    )
    assert f_other == "cancelled"

    _c, f_def, _t, _d = parse_responses_chunk(
        {"type": "response.incomplete"},
        None,
        model_name="gpt-6-astra",
    )
    assert f_def == "length"


def test_response_failed_and_errors() -> None:
    event_failed = {"type": "response.failed", "response": {"error": {"message": "Invalid token"}}}
    with pytest.raises(NetworkError) as exc_info:
        parse_responses_chunk(event_failed, None, model_name="gpt-6-astra")
    assert "Invalid token" in str(exc_info.value)

    assert stream_error_message(event_failed) == "Invalid token"
    assert stream_error_message({"type": "error", "message": "boom", "code": "server_error"}) == "boom"
    assert stream_error_message({"type": "error"}) == "error"
    assert stream_error_message({"type": "response.incomplete"}) is None


def test_sync_response_parsing() -> None:
    sync_body = {
        "object": "response",
        "model": "gpt-6-astra-2026-03-01",
        "output": [
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Sync reasoning step"}],
                "encrypted_content": "enc_blob_sync",
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Sync message text"}],
            },
            {
                "type": "function_call",
                "call_id": "call_sync_1",
                "name": "lookup",
                "arguments": '{"q": "writeragent"}',
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    assert is_responses_body(sync_body) is True

    content, finish_reason, tool_calls, usage, images, message = parse_responses_sync(sync_body)
    assert content == "Sync message text"
    assert finish_reason == "tool_calls"
    assert tool_calls == [
        {"id": "call_sync_1", "type": "function", "function": {"name": "lookup", "arguments": '{"q": "writeragent"}'}}
    ]
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 20
    assert images == []
    assert message["content"] == "Sync message text"
    details = message["reasoning_details"]
    assert len(details) == 2
    assert details[0] == {"type": "reasoning.text", "text": "Sync reasoning step", "index": 0}
    assert details[1] == {
        "type": "reasoning.encrypted",
        "data": "enc_blob_sync",
        "format": "openai-responses-v1",
        "index": 1,
        "model": "gpt-6-astra-2026-03-01",
    }

    # status: failed raises NetworkError
    with pytest.raises(NetworkError):
        parse_responses_sync({"object": "response", "status": "failed", "error": {"message": "sync failed"}})

    # status: incomplete with max_output_tokens -> length
    _c, finish_inc, _tc, _u, _img, _msg = parse_responses_sync({
        "object": "response",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [],
    })
    assert finish_inc == "length"

    # Chat completions body still parses through OpenAIShim.parse_sync_response
    client = DummyClient()
    shim = OpenAIShim(client)
    chat_body = {"choices": [{"message": {"role": "assistant", "content": "normal chat content"}, "finish_reason": "stop"}]}
    c_chat, f_chat, _tc, _u, _img, _msg = shim.parse_sync_response(chat_body)
    assert c_chat == "normal chat content"
    assert f_chat == "stop"

    # Chat chunk still returns delta.content through OpenAIShim.parse_response_chunk
    chat_chunk = {"choices": [{"delta": {"content": "chunk text"}, "finish_reason": None}]}
    c_chunk, f_chunk, _th, d_chunk = shim.parse_response_chunk(chat_chunk)
    assert c_chunk == "chunk text"
    assert f_chunk is None
    assert d_chunk == {"content": "chunk text"}


def test_replay_round_trip() -> None:
    # 1. Parse a chunk that emits encrypted reasoning
    chunk = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {"type": "reasoning", "encrypted_content": "enc_round_trip"},
    }
    state: dict[str, Any] = {"_responses_model": "gpt-6-astra"}
    _c, _f, _t, delta = parse_responses_chunk(chunk, state, model_name="gpt-6-astra")
    assert "reasoning_details" in delta

    # 2. Feed into stream_normalizer
    thinking_parts: list[str] = []
    replay_meta: dict[str, Any] = {}
    accumulate_streaming_thinking(thinking_parts, replay_meta, delta)
    replay_dict = extract_reasoning_replay_from_response(
        streaming_text="".join(thinking_parts), streaming_meta=replay_meta
    )
    stored_details = replay_dict["reasoning_details"]
    assert len(stored_details) == 1
    assert stored_details[0]["data"] == "enc_round_trip"
    assert stored_details[0]["format"] == "openai-responses-v1"
    assert stored_details[0]["model"] == "gpt-6-astra"

    # 3. Build a subsequent chat request with this stored reasoning
    client = DummyClient()
    shim = OpenAIShim(client)
    tools = [{"type": "function", "function": {"name": "fn", "parameters": {}}}]
    next_messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "prior response",
            "reasoning_details": stored_details,
        },
        {"role": "user", "content": "followup"},
    ]
    _method, _path, body, _headers = shim.build_chat_request(
        next_messages, 100, None, tools, False, "gpt-6-astra", None
    )
    payload = json.loads(body)
    items = payload["input"]
    r_items = [it for it in items if it.get("type") == "reasoning"]
    assert len(r_items) == 1
    assert r_items[0] == {"type": "reasoning", "encrypted_content": "enc_round_trip", "summary": []}
    assert "id" not in r_items[0]


def test_iterate_sse_yields_events_with_or_without_done() -> None:
    lines_no_done = [
        b'data: {"type": "response.created"}\n\n',
        b'data: {"type": "response.output_text.delta", "delta": "hi"}\n\n',
        b'data: {"type": "response.completed"}\n\n',
    ]
    payloads1 = list(iterate_sse(lines_no_done))
    assert len(payloads1) == 3
    assert json.loads(payloads1[0])["type"] == "response.created"
    assert json.loads(payloads1[1])["type"] == "response.output_text.delta"
    assert json.loads(payloads1[2])["type"] == "response.completed"

    lines_with_done = [
        b'data: {"type": "response.created"}\n\n',
        b"data: [DONE]\n\n",
    ]
    payloads2 = list(iterate_sse(lines_with_done))
    assert len(payloads2) == 2
    assert json.loads(payloads2[0])["type"] == "response.created"
    assert payloads2[1] == "[DONE]"
