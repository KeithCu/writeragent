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
    prepare_chat_messages,
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
    assert usage["prompt_tokens"] == 20
    assert usage["completion_tokens"] == 10


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


def test_anthropic_null_content_is_empty_not_an_error():
    shim = AnthropicShim(MagicMock())
    content, _finish, _thinking, delta = shim.parse_response_chunk({"type": "message", "content": None, "stop_reason": "end_turn"})
    assert content == ""
    assert "tool_calls" not in delta
    content, _finish, _thinking, delta = shim.parse_response_chunk({"type": "message", "content": ["nope", {"type": "text", "text": "ok"}], "stop_reason": "end_turn"})
    assert content == "ok"


def test_anthropic_build_skips_missing_tool_arguments():
    import json

    client = MagicMock()
    client._endpoint.return_value = "https://api.anthropic.com"
    client._headers.return_value = {}
    shim = AnthropicShim(client)
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": None}, {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "function": {"name": "do_work"}}]}],
        32,
        None,
        None,
        False,
        "claude-test",
        None,
    )
    data = json.loads(body)
    assistant = data["messages"][-1]
    assert assistant["content"] == [] or all(part.get("type") != "tool_use" for part in assistant["content"])


def test_anthropic_thinking_delta_maps_onto_thinking():
    shim = AnthropicShim(MagicMock())
    content, _finish, thinking, delta = shim.parse_response_chunk({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}})
    assert content == ""
    assert thinking == "hmm"
    assert delta.get("thinking") == "hmm"
    content, _finish, thinking, delta = shim.parse_response_chunk({"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}})
    assert thinking in (None, "")
    assert delta == {}


def test_openai_shim_parse_sync_response_raises_on_error():
    import pytest
    from plugin.framework.errors import NetworkError

    shim = OpenAIShim(MagicMock())

    # Case 1: finish_reason == 'error' without choice.error
    with pytest.raises(NetworkError) as exc_info:
        shim.parse_sync_response({"choices": [{"message": {"content": ""}, "finish_reason": "error"}]})
    assert exc_info.value.code == "STREAM_ERROR"

    # Case 2: choice error dict (e.g. Groq 502 validation error)
    err_payload = {
        "choices": [{
            "message": {"content": ""},
            "finish_reason": "error",
            "error": {
                "message": "Groq: Tool call validation failed: /source_image expected string, but got null",
                "code": 502,
            },
        }]
    }
    with pytest.raises(NetworkError) as exc_info2:
        shim.parse_sync_response(err_payload)
    assert exc_info2.value.code == "STREAM_ERROR"
    assert "Groq: Tool call validation failed" in str(exc_info2.value)

    # Case 3: top-level error dict
    top_level_err = {
        "error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}
    }
    with pytest.raises(NetworkError) as exc_info3:
        shim.parse_sync_response(top_level_err)
    assert exc_info3.value.code == "STREAM_ERROR"
    assert "Rate limit exceeded" in str(exc_info3.value)


def test_data_uri_regex_preserves_trailing_prose():
    msg = {"role": "user", "content": "data:image/png;base64,AAAA\nNext line of text."}
    extracted = extract_and_strip_images_from_message(msg)
    assert len(extracted) == 1
    assert extracted[0]["data"] == "AAAA"
    assert msg["content"] == "[Image Ref]\nNext line of text."

    msg2 = {"role": "user", "content": "Hello data:image/png;base64,AAAA see you."}
    extract_and_strip_images_from_message(msg2)
    assert msg2["content"] == "Hello [Image Ref] see you."

    msg3 = {"role": "user", "content": 'src="data:image/png;base64,AAAA" more'}
    extract_and_strip_images_from_message(msg3)
    assert msg3["content"] == 'src="[Image Ref]" more'

    msg4 = {"role": "user", "content": '<img src="data:image/png;base64,AAAA">'}
    extract_and_strip_images_from_message(msg4)
    assert msg4["content"] == '<img src="[Image Ref]">'

    msg5 = {"role": "user", "content": 'url(data:image/png;base64,AAAA)'}
    extract_and_strip_images_from_message(msg5)
    assert msg5["content"] == 'url([Image Ref])'


def test_data_uri_regex_multiline_mime_wrapped_continuation():
    line1 = "A" * 76
    line2 = "B" * 76
    tail = "C" * 20 + "=="
    full_b64 = f"{line1}\n{line2}\n{tail}"
    msg = {"role": "user", "content": f"data:image/png;base64,{full_b64}\nFinal prose."}
    extracted = extract_and_strip_images_from_message(msg)
    assert len(extracted) == 1
    assert extracted[0]["data"] == line1 + line2 + ("C" * 20 + "==")
    assert msg["content"] == "[Image Ref]\nFinal prose."


def test_normalize_multimodal_moves_remote_image_url_on_openai():
    messages = [
        {"role": "user", "content": "look"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "here is an image"},
                {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
            ],
        },
    ]
    normalize_multimodal_messages(messages, "openai")
    # Assistant part replaced with [Image Ref]
    assert messages[1]["content"] == [
        {"type": "text", "text": "here is an image"},
        {"type": "text", "text": "[Image Ref]"},
    ]
    # URL moved to preceding user message
    assert isinstance(messages[0]["content"], list)
    assert messages[0]["content"][0]["text"] == "look"
    assert messages[0]["content"][1]["type"] == "image_url"
    assert messages[0]["content"][1]["image_url"]["url"] == "https://example.com/cat.png"


def test_normalize_multimodal_anthropic_keeps_tool_remote_image_in_place():
    block = {"type": "image_url", "image_url": {"url": "https://example.com/chart.png"}}
    messages = [
        {"role": "user", "content": "generate"},
        {"role": "tool", "content": [block]},
    ]
    normalize_multimodal_messages(messages, "anthropic")
    assert messages[1]["content"] == [block]


def test_extract_preserves_malformed_data_uri():
    msg = {
        "role": "assistant",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;notbase64!!!"}},
        ],
    }
    extracted = extract_and_strip_images_from_message(msg, strip_structured_image_blocks=True)
    assert extracted == []
    # Not replaced with [Image Ref]!
    assert msg["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;notbase64!!!"}},
    ]


def test_normalize_multimodal_all_system_synthetic_user_appends():
    messages = [
        {"role": "system", "content": "system 1"},
        {"role": "system", "content": "data:image/png;base64,abc123"},
    ]
    normalize_multimodal_messages(messages, "openai")
    assert [m.get("role") for m in messages] == ["system", "system", "user"]
    assert messages[0]["content"] == "system 1"
    assert messages[1]["content"] == "[Image Ref]"
    assert isinstance(messages[2]["content"], list)
    assert messages[2]["content"][0]["text"] == "[Image attached by tool/system]"
    assert messages[2]["content"][1]["image_url"]["url"] == "data:image/png;base64,abc123"


def test_normalize_multimodal_system_assistant_inserts_user_between():
    messages = [
        {"role": "system", "content": "system instructions"},
        {"role": "assistant", "content": "here data:image/png;base64,abc123"},
    ]
    normalize_multimodal_messages(messages, "openai")
    assert [m.get("role") for m in messages] == ["system", "user", "assistant"]
    assert messages[0]["content"] == "system instructions"
    assert isinstance(messages[1]["content"], list)
    assert messages[1]["content"][0]["text"] == "[Image attached by tool/system]"
    assert messages[2]["content"] == "here [Image Ref]"


def test_prepare_chat_messages_non_dict_passthrough():
    messages = ["non-dict-string", {"role": "user", "content": "hi"}]
    res = prepare_chat_messages(messages, "openai", prepend_dev_build_system_prefix=False)
    assert "non-dict-string" in res
    assert any(isinstance(m, dict) and m.get("role") == "user" and "hi" in str(m.get("content")) for m in res)

    # System dict following a non-dict still gets date line
    messages2 = ["nope", {"role": "system", "content": "prompt"}]
    res2 = prepare_chat_messages(messages2, "openai", prepend_dev_build_system_prefix=False)
    assert "nope" in res2
    sys_m = next(m for m in res2 if isinstance(m, dict) and m.get("role") == "system")
    assert "Today's date is " in sys_m["content"]


def test_prepare_chat_messages_null_text_in_system_list():
    messages = [{"role": "system", "content": [{"type": "text", "text": None}]}]
    res = prepare_chat_messages(messages, "openai", prepend_dev_build_system_prefix=False)
    assert isinstance(res[0]["content"], str)
    assert "Today's date is " in res[0]["content"]


def test_prepare_chat_messages_midstring_date_no_duplicate():
    messages = [{"role": "system", "content": "Context before. Today's date is Monday. More context."}]
    res = prepare_chat_messages(messages, "openai", prepend_dev_build_system_prefix=False)
    assert res[0]["content"].count("Today's date is ") == 1
