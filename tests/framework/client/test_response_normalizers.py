# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import pytest
from unittest.mock import MagicMock

from plugin.framework.client.response_normalizers import (
    strip_leaked_chat_template_control_tokens,
    extract_and_strip_images_from_message,
    normalize_multimodal_messages,
    OpenAIShim,
)
from plugin.framework.client.anthropic_shim import AnthropicShim
from plugin.framework.client.google_shim import GoogleShim


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
    assert finish_reason == "tool_use"
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "do_work"
    assert usage["input_tokens"] == 20


def test_google_shim_parse_sync_response():
    client_mock = MagicMock()
    shim = GoogleShim(client_mock)
    
    mock_payload = {
        "candidates": [{
            "content": {
                "parts": [
                    {"text": "Hello from Google"},
                    {"functionCall": {"name": "web_search", "args": {"q": "libreoffice"}}}
                ]
            },
            "finishReason": "STOP"
        }],
        "usageMetadata": {"promptTokenCount": 15, "candidatesTokenCount": 8}
    }
    
    content, finish_reason, tool_calls, usage, images, message = shim.parse_sync_response(mock_payload)
    assert content == "Hello from Google"
    assert finish_reason == "stop"
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "web_search"
    assert usage["promptTokenCount"] == 15
