import json
import logging
from typing import Any

import pytest

from plugin.framework.client.anthropic_shim import AnthropicShim, _parse_tool_input
from plugin.framework.errors import NetworkError

def test_anthropic_shim_tool_parsing():
    # Empty/whitespace args become {}
    assert _parse_tool_input("") == {}
    assert _parse_tool_input("   \n ") == {}

    # Bad JSON is omitted. {} would look like a successful no-arg call.
    assert _parse_tool_input("{bad json") is None
    assert _parse_tool_input("[\"not an object\"]") is None

def test_anthropic_shim_build_chat():
    class DummyClient:
        def _endpoint(self): return "http://test"
        def _headers(self): return {}
    shim = AnthropicShim(DummyClient())

    # temperature clamping & output_config
    messages = [{"role": "user", "content": "hi"}]
    method, path, body, headers = shim.build_chat_request(
        messages, 100, 1.5, None, False, "claude-3-7-sonnet-20250219", None,
        {"reasoning": {"effort": "minimal"}}
    )
    data = json.loads(body)
    assert data["temperature"] == 1.0  # clamped from 1.5
    assert data["output_config"] == {"effort": "low"}

    # dropped tools and orphan result
    messages2 = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "good1", "function": {"name": "f1", "arguments": "{}"}},
            {"id": "bad1", "function": {"name": "f2", "arguments": "{bad"}},
        ]},
        {"role": "tool", "tool_call_id": "good1", "content": "good result"},
        {"role": "tool", "tool_call_id": "bad1", "content": "orphan result"}
    ]
    method, path, body, headers = shim.build_chat_request(
        messages2, 100, None, None, False, "claude-3-5-sonnet", None, None
    )
    data = json.loads(body)
    msg_list = data["messages"]

    # Check that the assistant message only has the good tool_use
    assistant_msg = msg_list[0]
    assert assistant_msg["role"] == "assistant"
    assert len(assistant_msg["content"]) == 1
    assert assistant_msg["content"][0]["id"] == "good1"

    # Check that the user message only has the good tool_result
    user_msg = msg_list[1]
    assert user_msg["role"] == "user"
    assert len(user_msg["content"]) == 1
    assert user_msg["content"][0]["tool_use_id"] == "good1"

def test_anthropic_shim_stream_state():
    class DummyClient:
        def _endpoint(self): return "http://test"
        def _headers(self): return {}
    shim = AnthropicShim(DummyClient())

    state1: dict[str, Any] = {}
    state2: dict[str, Any] = {}

    # Stream 1 block start
    chunk1 = {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "f1"}}
    _, _, _, d1 = shim.parse_response_chunk(chunk1, stream_state=state1)

    # Stream 2 block start (should get index 0 in its own state, not 1)
    chunk2 = {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": "f2"}}
    _, _, _, d2 = shim.parse_response_chunk(chunk2, stream_state=state2)

    assert d1["tool_calls"][0]["index"] == 0
    assert d2["tool_calls"][0]["index"] == 0
    assert state1["_stream_tool_indexes"][0] == 0
    assert state2["_stream_tool_indexes"][0] == 0


def test_anthropic_shim_skipped_tool_calls():
    class DummyClient:
        def _endpoint(self): return "http://test"
        def _headers(self): return {}
    shim = AnthropicShim(DummyClient())

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "good1", "function": {"name": "func_good", "arguments": '{"x": 1}'}},
            {"id": "bad_no_args", "function": {"name": "func_no_args"}},
            {"id": "bad_json", "function": {"name": "func_bad_json", "arguments": "{invalid json"}},
            {"id": "bad_name_none", "function": {"name": None, "arguments": "{}"}},
            {"id": "bad_name_empty", "function": {"name": "", "arguments": "{}"}},
            {"id": "", "function": {"name": "func_empty_id", "arguments": "{}"}},
            "not-a-dict",
        ]},
        {"role": "tool", "tool_call_id": "good1", "content": "result good"},
        {"role": "tool", "tool_call_id": "bad_no_args", "content": "result no args"},
        {"role": "tool", "tool_call_id": "bad_json", "content": "result bad json"},
        {"role": "tool", "tool_call_id": "bad_name_none", "content": "result name none"},
        {"role": "tool", "tool_call_id": "bad_name_empty", "content": "result name empty"},
    ]
    _, _, body, _ = shim.build_chat_request(
        messages, 100, None, None, False, "claude-3-5-sonnet", None, None
    )
    data = json.loads(body)
    msg_list = data["messages"]

    emitted_tool_use_ids = {
        block["id"]
        for msg in msg_list
        if msg.get("role") == "assistant"
        for block in msg.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }
    emitted_tool_result_ids = [
        block["tool_use_id"]
        for msg in msg_list
        if msg.get("role") == "user"
        for block in msg.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]

    # Only valid tool call is emitted
    assert emitted_tool_use_ids == {"good1"}
    # Assert no tool_result is emitted for skipped ids
    for skipped_id in ("bad_no_args", "bad_json", "bad_name_none", "bad_name_empty"):
        assert skipped_id not in emitted_tool_result_ids
    # Assert every tool_result matches an emitted tool_use id
    for result_id in emitted_tool_result_ids:
        assert result_id in emitted_tool_use_ids
    assert emitted_tool_result_ids == ["good1"]


def _shim() -> AnthropicShim:
    class DummyClient:
        def _endpoint(self) -> str:
            return "http://test"

        def _headers(self) -> dict[str, str]:
            return {}

    return AnthropicShim(DummyClient())


def _message_blocks(body: bytes) -> list[dict[str, Any]]:
    data = json.loads(body)
    messages = data["messages"]
    assert isinstance(messages, list)
    return messages


def test_tool_result_name_is_not_a_tool_use_id(caplog: pytest.LogCaptureFixture):
    """A tool name used to be sent as tool_use_id and Anthropic returned 400."""
    shim = _shim()
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "search", "arguments": "{}"}},
            {"id": "good1", "function": {"name": "f1", "arguments": "{}"}},
        ]},
        {"role": "tool", "name": "search", "content": "named result"},
        {"role": "tool", "content": "no id"},
        {"role": "tool", "tool_call_id": "good1", "content": "ok"},
    ]
    with caplog.at_level(logging.WARNING):
        _method, _path, body, _headers = shim.build_chat_request(
            messages, 100, None, None, False, "claude-sonnet-4-5", None, None
        )
    msg_list = _message_blocks(body)
    result_ids = [
        block["tool_use_id"]
        for msg in msg_list
        if msg.get("role") == "user"
        for block in msg.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert result_ids == ["good1"]
    assert "search" not in result_ids
    assert "no tool_call_id" in caplog.text


def test_developer_joins_system_and_unknown_role_is_skipped(caplog: pytest.LogCaptureFixture):
    shim = _shim()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "developer", "content": "dev line"},
        {"role": "user", "content": "hi"},
        {"role": "function", "name": "old", "content": "do not send"},
    ]
    with caplog.at_level(logging.WARNING):
        _method, _path, body, _headers = shim.build_chat_request(
            messages, 20, None, None, False, "claude-sonnet-4-5", None, None
        )
    data = json.loads(body)
    assert data["system"] == "sys\n\ndev line"
    assert data["messages"] == [{"role": "user", "content": "hi"}]
    assert "do not send" not in body.decode()
    assert "function" in caplog.text


def test_https_image_url_becomes_anthropic_url_source():
    shim = _shim()
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            {"type": "image_url", "image_url": "http://example.com/b.png"},
        ]},
    ]
    _method, _path, body, _headers = shim.build_chat_request(
        messages, 20, None, None, False, "claude-sonnet-4-5", None, None
    )
    content = _message_blocks(body)[0]["content"]
    assert content[1] == {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}
    assert content[2] == {"type": "image", "source": {"type": "url", "url": "http://example.com/b.png"}}


def test_blank_anthropic_model_raises():
    shim = _shim()
    with pytest.raises(NetworkError, match="model is not set"):
        shim.build_chat_request(
            [{"role": "user", "content": "hi"}], 20, None, None, False, "", None, None
        )
    with pytest.raises(NetworkError, match="model is not set"):
        shim.build_chat_request(
            [{"role": "user", "content": "hi"}], 20, None, None, False, "   ", None, None
        )


def test_sync_tool_use_skips_empty_id_and_non_object_input():
    shim = _shim()
    payload = {
        "type": "message",
        "content": [
            {"type": "tool_use", "id": "", "name": "a", "input": {}},
            {"type": "tool_use", "name": "b", "input": {}},
            {"type": "tool_use", "id": "listy", "name": "c", "input": [1, 2]},
            {"type": "tool_use", "id": "ok", "name": "d", "input": {"x": 1}},
            {"type": "tool_use", "id": "empty", "name": "e"},
            {"type": "thinking", "thinking": "first"},
            {"type": "thinking", "thinking": "second"},
            {"type": "text", "text": "done"},
        ],
        "stop_reason": "tool_use",
    }
    content, finish_reason, thinking, delta = shim.parse_response_chunk(payload, {})
    assert content == "done"
    assert finish_reason == "tool_calls"
    assert thinking == "first\n\nsecond"
    assert delta["reasoning"] == "first\n\nsecond"
    calls = delta["tool_calls"]
    assert [call["id"] for call in calls] == ["ok", "empty"]
    assert json.loads(calls[1]["function"]["arguments"]) == {}

    _content, _reason, tool_calls, _usage, _images, message = shim.parse_sync_response(payload)
    assert message["reasoning"] == "first\n\nsecond"
    assert tool_calls is not None
    assert [call["id"] for call in tool_calls] == ["ok", "empty"]


def test_stream_usage_is_normalized_without_summing():
    """message_start nests input tokens. message_delta output_tokens replaces, it does not add."""
    shim = _shim()
    state: dict[str, Any] = {}
    _content, _reason, _thinking, start = shim.parse_response_chunk(
        {"type": "message_start", "message": {"usage": {"input_tokens": 25, "output_tokens": 1, "cache_read_input_tokens": 3}}},
        state,
    )
    assert start["usage"]["prompt_tokens"] == 25
    assert start["usage"]["completion_tokens"] == 1
    assert start["usage"]["cache_read_input_tokens"] == 3

    _content, finish, _thinking, end = shim.parse_response_chunk(
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 15}},
        state,
    )
    assert finish == "stop"
    assert end["usage"]["output_tokens"] == 15
    assert end["usage"]["completion_tokens"] == 15
    assert "prompt_tokens" not in end["usage"]

