from typing import Any
import json
from plugin.framework.client.anthropic_shim import _parse_tool_input, AnthropicShim, _BAD_TOOL_INPUT

def test_anthropic_shim_tool_parsing():
    # Empty/whitespace args become {}
    assert _parse_tool_input("") == {}
    assert _parse_tool_input("   \n ") == {}

    # Bad JSON becomes __BAD_JSON__
    assert _parse_tool_input("{bad json") == _BAD_TOOL_INPUT
    assert _parse_tool_input("[\"not an object\"]") == _BAD_TOOL_INPUT

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

