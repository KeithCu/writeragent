import json
from typing import Any

import pytest

from plugin.framework.client.anthropic_shim import AnthropicShim, _parse_tool_input
from plugin.framework.client.shim_helpers import snapshot_text
from plugin.framework.errors import NetworkError

def test_anthropic_shim_tool_parsing():
    # Empty/whitespace args become {}
    assert _parse_tool_input("") == {}
    assert _parse_tool_input("   \n ") == {}

    # Bad JSON is omitted. {} would look like a successful no-arg call.
    assert _parse_tool_input("{bad json") is None
    assert _parse_tool_input("[\"not an object\"]") is None

class _DummyClientBase:
    def _endpoint(self) -> str:
        return "http://test"

    def _headers(self) -> dict[str, str]:
        return {}

    def anthropic_previous_message_id(self, cache_session_id: str | None = None) -> str | None:
        return None


def test_anthropic_shim_build_chat():
    shim = AnthropicShim(_DummyClientBase())

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
    shim = AnthropicShim(_DummyClientBase())

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
    shim = AnthropicShim(_DummyClientBase())

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
    return AnthropicShim(_DummyClientBase())


def _message_blocks(body: bytes) -> list[dict[str, Any]]:
    data = json.loads(body)
    messages = data["messages"]
    assert isinstance(messages, list)
    return messages


def test_tool_result_name_is_not_a_tool_use_id():
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


def test_developer_joins_system_and_unknown_role_is_skipped():
    shim = _shim()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "developer", "content": "dev line"},
        {"role": "user", "content": "hi"},
        {"role": "function", "name": "old", "content": "do not send"},
    ]
    _method, _path, body, _headers = shim.build_chat_request(
        messages, 20, None, None, False, "claude-sonnet-4-5", None, None
    )
    data = json.loads(body)
    assert data["system"] == [{"type": "text", "text": "sys\n\ndev line", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    assert data["diagnostics"] == {"previous_message_id": None}
    assert "cache_control" not in data
    assert data["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert "do not send" not in body.decode()


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


def test_anthropic_passes_xhigh_and_omits_unknown() -> None:
    class DummyClientNoConfig(_DummyClientBase):
        pass

    shim = AnthropicShim(DummyClientNoConfig())
    _m, _p, body1, _h = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "claude-3-7-sonnet-20250219", None,
        chat_extra={"reasoning": {"effort": "xhigh"}}
    )
    assert json.loads(body1).get("output_config") == {"effort": "xhigh"}

    _m, _p, body2, _h = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "claude-3-7-sonnet-20250219", None,
        chat_extra={"reasoning": {"effort": "turbo"}}
    )
    assert "output_config" not in json.loads(body2)


def test_anthropic_setting_when_chat_extra_absent() -> None:
    class DummyClientWithConfig(_DummyClientBase):
        config = {"reasoning_effort": "high"}

    shim = AnthropicShim(DummyClientWithConfig())
    _m, _p, body, _h = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "claude-3-7-sonnet-20250219", None
    )
    assert json.loads(body).get("output_config") == {"effort": "high"}


def test_anthropic_grammar_beats_setting() -> None:
    class DummyClientWithConfig(_DummyClientBase):
        config = {"reasoning_effort": "high"}

    shim = AnthropicShim(DummyClientWithConfig())
    _m, _p, body, _h = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "claude-3-7-sonnet-20250219", None,
        chat_extra={"reasoning": {"effort": "minimal"}}
    )
    assert json.loads(body).get("output_config") == {"effort": "low"}


def test_anthropic_unset_omits_output_config() -> None:
    class DummyClientUnset(_DummyClientBase):
        config = {}

    shim = AnthropicShim(DummyClientUnset())
    _m, _p, body, _h = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "claude-3-7-sonnet-20250219", None
    )
    assert "output_config" not in json.loads(body)


def test_anthropic_tool_loop_prompt_caching_and_merge():
    from plugin.framework.client.shim_helpers import snapshot_text

    shim = AnthropicShim(_DummyClientBase())
    snap = snapshot_text("Current doc context")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "function": {"name": "func1", "arguments": "{}"}},
                {"id": "t2", "function": {"name": "func2", "arguments": "{}"}},
            ],
            "reasoning_content": "thinking text that must not become a block",
        },
        {"role": "tool", "tool_call_id": "t1", "content": "res1"},
        {"role": "tool", "tool_call_id": "t2", "content": f"res2\n\n{snap}"},
    ]

    _m, _p, body, _h = shim.build_chat_request(
        messages, 100, None, None, False, "claude-3-5-sonnet", None
    )
    data = json.loads(body)
    assert "cache_control" not in data  # No top-level cache_control

    converted_messages = data["messages"]
    assert len(converted_messages) == 2
    asst_msg = converted_messages[0]
    assert asst_msg["role"] == "assistant"
    # No thinking block
    assert not any(part.get("type") == "thinking" for part in asst_msg["content"])

    user_msg = converted_messages[1]
    assert user_msg["role"] == "user"
    parts = user_msg["content"]
    assert len(parts) == 3
    assert parts[0]["type"] == "tool_result"
    assert parts[0]["tool_use_id"] == "t1"
    assert "cache_control" not in parts[0]
    assert parts[0]["content"] == [{"type": "text", "text": "res1"}]

    assert parts[1]["type"] == "tool_result"
    assert parts[1]["tool_use_id"] == "t2"
    assert parts[1].get("cache_control") == {"type": "ephemeral"}
    assert parts[1]["content"] == [{"type": "text", "text": "res2"}]

    assert parts[2]["type"] == "text"
    assert parts[2]["text"] == snap
    assert "cache_control" not in parts[2]
    assert json.dumps(data).count("cache_control") == 1


def test_anthropic_user_send_prompt_caching():
    from plugin.framework.client.shim_helpers import snapshot_text

    shim = AnthropicShim(_DummyClientBase())
    snap = snapshot_text("Document snapshot")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Please summarize this document."},
                {"type": "text", "text": snap},
            ],
        }
    ]

    _m, _p, body, _h = shim.build_chat_request(
        messages, 100, None, None, False, "claude-3-5-sonnet", None
    )
    data = json.loads(body)
    assert "cache_control" not in data

    converted_messages = data["messages"]
    assert len(converted_messages) == 1
    user_msg = converted_messages[0]
    parts = user_msg["content"]
    assert len(parts) == 2
    assert parts[0]["text"] == "Please summarize this document."
    assert parts[0].get("cache_control") == {"type": "ephemeral"}

    assert parts[1]["text"] == snap
    assert "cache_control" not in parts[1]

    # The same user string on a later message list, with no tail, is that same first block without cache_control and without the tail.
    later_messages = [
        {"role": "user", "content": "Please summarize this document."},
        {"role": "assistant", "content": "Summary"},
    ]
    _m, _p, body_later, _h = shim.build_chat_request(
        later_messages, 100, None, None, False, "claude-3-5-sonnet", None
    )
    data_later = json.loads(body_later)
    asst_turn_messages = data_later["messages"]
    assert asst_turn_messages[0]["content"] == [{"type": "text", "text": "Please summarize this document."}]
    assert "cache_control" not in asst_turn_messages[0]["content"][0]


def test_anthropic_tool_choice():
    shim = AnthropicShim(_DummyClientBase())
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    messages = [{"role": "user", "content": "hi"}]

    # With tools and tool_choice=None, tool_choice is absent, matching today
    _m, _p, body1, _h = shim.build_chat_request(
        messages, 100, None, tools, False, "claude-3-5-sonnet", None, tool_choice=None
    )
    data1 = json.loads(body1)
    assert "tools" in data1
    assert "tool_choice" not in data1

    # With tools and tool_choice="none", body has tools and tool_choice == {"type": "none"}
    _m, _p, body2, _h = shim.build_chat_request(
        messages, 100, None, tools, False, "claude-3-5-sonnet", None, tool_choice="none"
    )
    data2 = json.loads(body2)
    assert "tools" in data2
    assert data2["tool_choice"] == {"type": "none"}


def test_anthropic_single_tool_round_cache_breakpoint():
    from plugin.framework.client.shim_helpers import snapshot_text

    shim = AnthropicShim(_DummyClientBase())
    snap = snapshot_text("Document text")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "function": {"name": "f1", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": f"res1\n\n{snap}"},
    ]

    _m, _p, body, _h = shim.build_chat_request(
        messages, 100, None, None, False, "claude-3-5-sonnet", None
    )
    data = json.loads(body)
    assert "cache_control" not in data
    converted = data["messages"]
    assert len(converted) == 2
    asst_msg = converted[0]
    assert asst_msg["role"] == "assistant"
    assert len(asst_msg["content"]) == 1
    assert asst_msg["content"][0]["type"] == "tool_use"
    assert asst_msg["content"][0]["id"] == "t1"
    assert "cache_control" not in asst_msg["content"][0]

    user_msg = converted[1]
    assert user_msg["role"] == "user"
    assert len(user_msg["content"]) == 2
    tool_res = user_msg["content"][0]
    assert tool_res["type"] == "tool_result"
    assert tool_res.get("cache_control") == {"type": "ephemeral"}
    assert tool_res["content"] == [{"type": "text", "text": "res1"}]

    snap_part = user_msg["content"][1]
    assert snap_part["type"] == "text"
    assert snap_part["text"] == snap
    assert "cache_control" not in snap_part
    assert json.dumps(data).count("cache_control") == 1


def test_anthropic_omits_cache_session_id():
    class DummyClient(_DummyClientBase):
        def _get_provider(self):
            return "anthropic"

    shim = AnthropicShim(DummyClient())
    messages = [{"role": "user", "content": "hi"}]
    _method, _path, body, headers = shim.build_chat_request(
        messages, 100, None, None, False, "claude-3-5-sonnet", None, cache_session_id="conv-1"
    )
    data = json.loads(body)
    assert "session_id" not in data
    assert "prompt_cache_key" not in data
    assert "x-grok-conv-id" not in headers


def test_anthropic_cache_breakpoints_system_tools_snapshot():
    shim = _shim()
    snap = snapshot_text("doc text")
    messages = [
        {"role": "system", "content": "You are a writing assistant."},
        {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": snap}]},
    ]
    tools = [
        {"type": "function", "function": {"name": "t1", "description": "tool 1", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "t2", "description": "tool 2", "parameters": {"type": "object"}}},
    ]
    _method, _path, body, _headers = shim.build_chat_request(
        messages, 100, None, tools, False, "claude-3-5-sonnet", None
    )
    data = json.loads(body)
    assert "cache_control" not in data
    assert data["system"] == [{"type": "text", "text": "You are a writing assistant.", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    # Tools remain unmarked because the system breakpoint covers tools and system prefix
    assert "cache_control" not in data["tools"][0]
    assert "cache_control" not in data["tools"][1]
    # User message: pre-snapshot block is marked, snapshot is unmarked
    u_content = data["messages"][0]["content"]
    assert u_content[0]["text"] == "hello"
    assert u_content[0]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in u_content[0]["cache_control"]
    assert u_content[1]["text"] == snap
    assert "cache_control" not in u_content[1]
    assert json.dumps(data).count("cache_control") == 2


def test_anthropic_cache_breakpoints_no_system_two_tools_snapshot():
    shim = _shim()
    snap = snapshot_text("doc text")
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": snap}]},
    ]
    tools = [
        {"type": "function", "function": {"name": "t1", "description": "tool 1", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "t2", "description": "tool 2", "parameters": {"type": "object"}}},
    ]
    _method, _path, body, _headers = shim.build_chat_request(
        messages, 100, None, tools, False, "claude-3-5-sonnet", None
    )
    data = json.loads(body)
    assert "system" not in data
    assert "cache_control" not in data
    assert "cache_control" not in data["tools"][0]
    assert data["tools"][1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    u_content = data["messages"][0]["content"]
    assert u_content[0]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in u_content[0]["cache_control"]
    assert "cache_control" not in u_content[1]
    assert json.dumps(data).count("cache_control") == 2


def test_anthropic_cache_breakpoints_long_transcript():
    shim = _shim()
    snap = snapshot_text("doc text")
    messages = [
        {"role": "system", "content": "Base instructions."},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "response 1"},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": "response 2"},
        {"role": "user", "content": [{"type": "text", "text": "turn 3"}, {"type": "text", "text": snap}]},
    ]
    tools = [
        {"type": "function", "function": {"name": "t1", "description": "tool 1", "parameters": {"type": "object"}}},
    ]
    _method, _path, body, _headers = shim.build_chat_request(
        messages, 100, None, tools, False, "claude-3-5-sonnet", None
    )
    data = json.loads(body)
    assert data["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in data["tools"][0]
    last_msg = data["messages"][-1]
    assert last_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in last_msg["content"][1]
    assert json.dumps(data).count("cache_control") == 2


def test_anthropic_diagnostics_previous_message_id_passthrough():
    class DummyClient(_DummyClientBase):
        def anthropic_previous_message_id(self, cache_session_id: str | None = None) -> str | None:
            return "msg_abc" if cache_session_id == "conv-1" else None

    shim = AnthropicShim(DummyClient())
    messages = [{"role": "user", "content": "hi"}]
    _method, _path, body, _headers = shim.build_chat_request(
        messages, 100, None, None, False, "claude-3-5-sonnet", None, cache_session_id="conv-1"
    )
    data = json.loads(body)
    assert data["diagnostics"] == {"previous_message_id": "msg_abc"}
    assert "session_id" not in data
    assert "prompt_cache_key" not in data

    _method2, _path2, body2, _headers2 = shim.build_chat_request(
        messages, 100, None, None, False, "claude-3-5-sonnet", None, cache_session_id=None
    )
    assert json.loads(body2)["diagnostics"] == {"previous_message_id": None}
