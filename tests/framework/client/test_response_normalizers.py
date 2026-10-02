# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock

from plugin.framework.client.anthropic_shim import AnthropicShim
from plugin.framework.client.google_shim import GoogleShim
from plugin.framework.client.openai_shim import OpenAIShim
from plugin.framework.client.response_normalizers import (
    extract_and_strip_images_from_message,
    normalize_multimodal_messages,
    strip_leaked_chat_template_control_tokens,
)


def test_strip_leaked_chat_template_control_tokens():
    assert strip_leaked_chat_template_control_tokens("<|channel|>Hello") == "Hello"
    assert strip_leaked_chat_template_control_tokens("Normal text") == "Normal text"
    assert strip_leaked_chat_template_control_tokens("") == ""


def test_extract_and_strip_images_from_message():
    msg = {"role": "user", "content": "Hello data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"}
    extracted = extract_and_strip_images_from_message(msg)
    assert len(extracted) == 1
    assert extracted[0]["mime_type"] == "image/png"
    assert msg["content"] == "Hello [Image Ref]"


def test_normalize_multimodal_messages():
    messages = [
        {"role": "user", "content": "Hello data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"},
        {"role": "assistant", "content": "Hi"}
    ]
    normalize_multimodal_messages(messages, "openai")
    # Image should be attached to the user message as image_url structured block
    assert isinstance(messages[0]["content"], list)
    assert messages[0]["content"][0]["text"] == "Hello [Image Ref]"
    assert messages[0]["content"][1]["type"] == "image_url"


def test_extract_string_or_null_image_url():
    string_part = {"role": "user", "content": [{"type": "image_url", "image_url": "data:image/png;base64,abc123"}]}
    extracted = extract_and_strip_images_from_message(string_part)
    assert extracted == [{"mime_type": "image/png", "data": "abc123"}]
    assert string_part["content"] == [{"type": "text", "text": "[Image Ref]"}]

    null_part = {"role": "user", "content": [{"type": "image_url", "image_url": None}]}
    assert extract_and_strip_images_from_message(null_part) == []
    assert null_part["content"] == [{"type": "text", "text": "[Image Ref]"}]


def test_normalize_duplicate_messages_keep_their_own_images():
    # messages.index() used equality, so the second identical assistant
    # message attached its image to the first user turn.
    blob = "data:image/png;base64,abc123"
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "same " + blob},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "same " + blob},
    ]
    normalize_multimodal_messages(messages, "openai")

    def image_count(message: dict) -> int:
        content = message["content"]
        if not isinstance(content, list):
            return 0
        return sum(1 for part in content if isinstance(part, dict) and part.get("type") == "image_url")

    assert image_count(messages[0]) == 1
    assert image_count(messages[2]) == 1


def test_openai_shim_skips_non_dict_image_data():
    shim = OpenAIShim(MagicMock())
    assert shim.parse_image_responses({"data": [{"b64_json": "ghi"}, None, "nope"]}) == ["ghi"]
    assert shim.parse_image_responses({"data": None}) == []


def test_openai_shim_parse_sync_response():
    client_mock = MagicMock()
    shim = OpenAIShim(client_mock)
    
    mock_payload = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "Hello from OpenAI",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "test", "arguments": "{}"}}]
            },
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}
    }
    
    content, finish_reason, tool_calls, usage, images, message = shim.parse_sync_response(mock_payload)
    assert content == "Hello from OpenAI"
    assert finish_reason == "stop"
    assert tool_calls == [{"id": "c1", "type": "function", "function": {"name": "test", "arguments": "{}"}}]
    assert usage["prompt_tokens"] == 10


def test_anthropic_shim_parse_sync_response():
    client_mock = MagicMock()
    shim = AnthropicShim(client_mock)
    
    mock_payload = {
        "type": "message",
        "content": [
            {"type": "text", "text": "Hello from Anthropic"},
            {"type": "tool_use", "id": "t1", "name": "do_work", "input": {"x": 1}}
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 20, "output_tokens": 10}
    }
    
    content, finish_reason, tool_calls, usage, images, message = shim.parse_sync_response(mock_payload)
    assert content == "Hello from Anthropic"
    assert finish_reason == "tool_calls"
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "do_work"
    assert usage["input_tokens"] == 20


def test_anthropic_shim_skips_tool_block_without_name():
    shim = AnthropicShim(MagicMock())
    payload = {
        "type": "message",
        "content": [
            {"type": "tool_use", "id": "t0"},
            {"type": "tool_use", "id": "t1", "name": "do_work", "input": {"x": 1}},
        ],
        "stop_reason": "tool_use",
    }
    parsed = shim.parse_sync_response(payload)
    tool_calls = parsed[2]
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "do_work"
    assert tool_calls[0]["id"] == "t1"


def test_google_shim_parse_sync_response():
    client_mock = MagicMock()
    shim = GoogleShim(client_mock)

    mock_payload = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "Hello from Google",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": '{"q": "libreoffice"}'}}
                ]
            },
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 15, "completion_tokens": 8}
    }

    content, finish_reason, tool_calls, usage, images, message = shim.parse_sync_response(mock_payload)
    assert content == "Hello from Google"
    assert finish_reason == "stop"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "web_search"
    assert usage["prompt_tokens"] == 15


def test_anthropic_json_object_is_a_system_hint_not_a_request_field():
    import json

    client = MagicMock()
    client._endpoint.return_value = "https://api.anthropic.com"
    client._headers.return_value = {}
    shim = AnthropicShim(client)
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}],
        32,
        None,
        None,
        False,
        "claude-test",
        {"type": "json_object"},
    )
    data = json.loads(body)
    assert "response_format" not in data
    assert data["system"] == "Be brief.\n\nRespond with a single JSON object and no other text."


def test_anthropic_thinking_delta_maps_onto_thinking():
    shim = AnthropicShim(MagicMock())
    content, _finish, thinking, delta = shim.parse_response_chunk({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}})
    assert content == ""
    assert thinking == "hmm"
    assert delta.get("thinking") == "hmm"
    content, _finish, thinking, delta = shim.parse_response_chunk({"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}})
    assert thinking in (None, "")
    assert delta == {}
