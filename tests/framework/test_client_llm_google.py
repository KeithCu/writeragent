# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import pytest
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.client.google_shim import GoogleShim
from plugin.framework.client.openai_shim import OpenAIShim
from plugin.tests.testing_utils import MockContext

@pytest.fixture
def mock_ctx():
    return MockContext()


def test_google_get_shim_returns_google_shim(mock_ctx):
    """Verify that LlmClient with Google config resolves to GoogleShim (subclass of OpenAIShim)."""
    config = {
        "endpoint": "https://generativelanguage.googleapis.com",
        "api_key": "test-key",
        "model": "gemini-2.0-flash",
    }
    client = LlmClient(config, mock_ctx)
    shim = client._get_shim()
    assert isinstance(shim, GoogleShim)
    assert isinstance(shim, OpenAIShim)


def test_google_chat_request_openai_format(mock_ctx):
    """Verify that GoogleShim inherits OpenAIShim chat request building with /v1beta/openai/chat/completions."""
    config = {
        "endpoint": "https://generativelanguage.googleapis.com",
        "api_key": "test-gemini-key",
        "model": "gemini-2.0-flash",
    }
    client = LlmClient(config, mock_ctx)
    messages = [
        {"role": "user", "content": "What is the weather?"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"location": {"type": "string"}}}
            }
        }
    ]

    method, path, body, headers = client.make_chat_request(messages, tools=tools)
    assert method == "POST"
    assert path == "/v1beta/openai/chat/completions"
    assert headers["Authorization"] == "Bearer test-gemini-key"

    data = json.loads(body.decode("utf-8"))
    assert data["model"] == "gemini-2.0-flash"
    assert "messages" in data
    assert "tools" in data
    assert data["tools"][0]["function"]["name"] == "get_weather"


def test_google_image_completion(mock_ctx):
    """Verify that GoogleShim builds native Google image requests."""
    config = {
        "endpoint": "https://generativelanguage.googleapis.com",
        "api_key": "test-key",
    }
    client = LlmClient(config, mock_ctx)
    shim = client._get_shim()
    assert isinstance(shim, GoogleShim)

    # 1. Test Imagen path default model & aspect ratio 16:9
    method, path, body, headers = shim.build_image_request("Draw a sunset", model=None, width=1792, height=1024)
    assert method == "POST"
    assert path == "/v1beta/models/imagen-4.0-generate-001:predict?key=test-key"
    data = json.loads(body.decode("utf-8"))
    assert data["parameters"]["aspectRatio"] == "16:9"

    # 2. Test Imagen aspect ratio 4:3 and 3:4
    _, _, body_4_3, _ = shim.build_image_request("Draw a cat", model="imagen-4.0-generate-001", width=1024, height=768)
    assert json.loads(body_4_3.decode("utf-8"))["parameters"]["aspectRatio"] == "4:3"

    _, _, body_3_4, _ = shim.build_image_request("Draw a tower", model="imagen-4.0-generate-001", width=768, height=1024)
    assert json.loads(body_3_4.decode("utf-8"))["parameters"]["aspectRatio"] == "3:4"

    # 3. Test Multimodal path (other models)
    method, path, body, headers = shim.build_image_request("Generate an image", model="gemini-2.5-flash-image", width=1024, height=1024)
    assert method == "POST"
    assert path == "/v1beta/models/gemini-2.5-flash-image:generateContent?key=test-key"
    data = json.loads(body.decode("utf-8"))
    assert "responseModalities" in data["generationConfig"]
    assert "IMAGE" in data["generationConfig"]["responseModalities"]


def test_google_parse_image_responses(mock_ctx):
    """Verify parsing both Imagen predictions and Gemini candidates inlineData."""
    client = LlmClient({"endpoint": "https://generativelanguage.googleapis.com"}, mock_ctx)
    shim = GoogleShim(client)

    # Imagen predictions format
    data_imagen = {"predictions": [{"bytesBase64Encoded": "img_b64_1"}]}
    assert shim.parse_image_responses(data_imagen) == ["img_b64_1"]

    # Gemini multimodal format
    data_multimodal = {
        "candidates": [{
            "content": {"parts": [{"inlineData": {"data": "img_b64_2", "mimeType": "image/png"}}]}
        }]
    }
    assert shim.parse_image_responses(data_multimodal) == ["img_b64_2"]


def test_google_openai_auth_resolution():
    """Verify auth resolution for Google Gemini endpoints produces Bearer header."""
    from plugin.framework.client.auth import resolve_auth_for_config, build_auth_headers

    auth_info = resolve_auth_for_config({
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key": "test-key-123",
    })
    assert auth_info["provider"] == "google"
    assert auth_info["header_style"] == "bearer"

    headers = build_auth_headers(auth_info)
    assert headers["Authorization"] == "Bearer test-key-123"


if __name__ == "__main__":
    pytest.main([__file__])
