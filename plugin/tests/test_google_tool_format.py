import json
from unittest.mock import MagicMock
from plugin.modules.http.client import LlmClient
from plugin.tests.testing_utils import MockContext

def test_google_tool_format():
    config = {
        "endpoint": "https://generativelanguage.googleapis.com",
        "api_key": "test-key",
        "model": "gemini-1.5-pro",
        "provider": "google"
    }
    # Mock resolve_auth to return provider="google"
    ctx = MockContext()
    client = LlmClient(config, ctx)
    client._resolve_auth = MagicMock(return_value={"provider": "google", "api_key": "test-key"})

    messages = [{"role": "user", "content": "What's the weather?"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"}
                    },
                    "required": ["location"]
                }
            }
        }
    ]

    method, path, body, headers = client.make_chat_request(messages, tools=tools)
    data = json.loads(body)

    assert "tools" in data
    assert len(data["tools"]) == 1
    assert "function_declarations" in data["tools"][0]
    fd = data["tools"][0]["function_declarations"][0]
    assert fd["name"] == "get_weather"
    assert fd["description"] == "Get the weather"
    assert fd["parameters"]["properties"]["location"]["type"] == "string"

if __name__ == "__main__":
    test_google_tool_format()
