import json
from unittest.mock import MagicMock, patch
import pytest
from plugin.modules.http.client import LlmClient
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

if __name__ == "__main__":
    pytest.main([__file__])
