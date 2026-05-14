import json
from unittest.mock import MagicMock, patch
import pytest
from plugin.framework.client.llm_client import LlmClient
from plugin.tests.testing_utils import MockContext

@pytest.fixture
def mock_ctx():
    return MockContext()

def test_google_make_chat_request_tools(mock_ctx):
    config = {
        "endpoint": "https://generativelanguage.googleapis.com",
        "api_key": "test-key",
        "model": "gemini-1.5-pro",
    }
    client = LlmClient(config, mock_ctx)
    client._resolve_auth = MagicMock(return_value={"provider": "google", "api_key": "test-key"})

    messages = [
        {"role": "user", "content": "What is the weather?"},
        {"role": "assistant", "content": "Thinking...", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"location": "London"}'}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": '{"temp": 20}'}
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
    data = json.loads(body.decode("utf-8"))

    # Verify tools
    assert "tools" in data
    assert len(data["tools"]) == 1
    assert "function_declarations" in data["tools"][0]
    fd = data["tools"][0]["function_declarations"][0]
    assert fd["name"] == "get_weather"

    # Verify contents
    contents = data["contents"]
    assert len(contents) == 3
    assert contents[0]["role"] == "user"
    assert contents[0]["parts"][0]["text"] == "What is the weather?"

    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["text"] == "Thinking..."
    assert "functionCall" in contents[1]["parts"][1]
    assert contents[1]["parts"][1]["functionCall"]["name"] == "get_weather"
    assert contents[1]["parts"][1]["functionCall"]["args"] == {"location": "London"}

    assert contents[2]["role"] == "function"
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "get_weather"
    assert contents[2]["parts"][0]["functionResponse"]["response"] == {"temp": 20}

def test_google_extract_content_from_response(mock_ctx):
    client = LlmClient({"endpoint": "http://test"}, mock_ctx)
    client._resolve_auth = MagicMock(return_value={"provider": "google"})

    chunk = {
        "candidates": [{
            "content": {
                "parts": [
                    {"text": "I will check the weather. "},
                    {"functionCall": {"name": "get_weather", "args": {"location": "Paris"}}}
                ]
            },
            "finishReason": "STOP"
        }],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}
    }

    content, finish_reason, thinking, delta = client.extract_content_from_response(chunk)

    assert content == "I will check the weather. "
    assert finish_reason == "stop"
    assert thinking is None
    assert "tool_calls" in delta
    assert len(delta["tool_calls"]) == 1
    assert delta["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(delta["tool_calls"][0]["function"]["arguments"]) == {"location": "Paris"}
    assert delta["usage"]["promptTokenCount"] == 10


def test_google_image_completion(mock_ctx):
    config = {
        "endpoint": "https://generativelanguage.googleapis.com",
        "api_key": "test-key",
    }
    client = LlmClient(config, mock_ctx)
    client._resolve_auth = MagicMock(return_value={"provider": "google", "api_key": "test-key"})

    with patch("plugin.framework.client.llm_client.sync_request") as mock_sync:
        # 1. Test Imagen path (model name starts with imagen)
        mock_sync.return_value = {"predictions": [{"bytesBase64Encoded": "imagen_data"}]}
        client.image_completion("Draw a sunset", model="imagen-3.0-generate-002", width=1792, height=1024)
        
        args, kwargs = mock_sync.call_args
        assert ":predict" in args[0]
        body = json.loads(kwargs["data"])
        assert body["parameters"]["aspectRatio"] == "16:9"

        # 2. Test Multimodal path (other models)
        mock_sync.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{"inlineData": {"data": "multimodal_data", "mimeType": "image/png"}}]
                }
            }]
        }
        client.image_completion("Generate an image", model="gemini-2.0-flash-exp-image-generation")
        
        args, kwargs = mock_sync.call_args
        assert ":generateContent" in args[0]
        body = json.loads(kwargs["data"])
        assert "responseModalities" in body["generationConfig"]
        assert "IMAGE" in body["generationConfig"]["responseModalities"]

if __name__ == "__main__":
    pytest.main([__file__])
