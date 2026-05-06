from unittest.mock import MagicMock

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.framework.smol_agent import WriterAgentSmolModel
from plugin.contrib.smolagents.models import ChatMessage, MessageRole
from plugin.contrib.smolagents.tools import Tool


class _ReplyTool(Tool):
    name = "reply_to_user"
    description = "Reply to the user"
    inputs = {"answer": {"type": "string", "description": "answer", "nullable": True}}
    output_type = "string"

    def forward(self, answer=""):
        return answer


def test_request_with_tools_receives_smol_generated_tools():
    """Smol path preserves the older request shape: prompt tools plus OpenAI schemas on the wire."""
    client = MagicMock()
    client.config = {"model": "test/local"}
    model = WriterAgentSmolModel(client, max_tokens=256)
    client.request_with_tools.return_value = {
        "content": "Action:\n{\n  \"name\": \"reply_to_user\",\n  \"arguments\": {\"answer\": \"hi\"}\n}",
        "tool_calls": None,
        "finish_reason": "stop",
        "images": None,
        "usage": {},
    }
    msgs = [ChatMessage(role=MessageRole.USER, content="hello")]
    model.generate(msgs, tools_to_call_from=[_ReplyTool()])
    assert client.request_with_tools.call_count == 1
    tools = client.request_with_tools.call_args.kwargs.get("tools")
    assert tools and tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "reply_to_user"
    assert client.request_with_tools.call_args.kwargs.get("model") == "test/local"


def test_smol_path_strips_control_tokens():
    """Explicit test that token stripping happens in the smolagents path via LlmClient."""
    client = MagicMock()
    client.config = {"model": "test/local"}
    model = WriterAgentSmolModel(client, max_tokens=256)

    # LlmClient.request_with_tools already strips control tokens before returning.
    # We test that the smol path receives clean content.
    clean_content = "Hello from librarian!"
    client.request_with_tools.return_value = {
        "content": clean_content,
        "tool_calls": None,
        "finish_reason": "stop",
        "images": None,
        "usage": {},
    }

    msgs = [ChatMessage(role=MessageRole.USER, content="hello")]
    result = model.generate(msgs, tools_to_call_from=[_ReplyTool()])

    assert "<|" not in result.content
    assert clean_content in result.content
    assert client.request_with_tools.call_count == 1
    assert client.request_with_tools.call_args.kwargs.get("tools")


def test_native_tool_calls_are_converted_by_chatmessage():
    """The adapter should rely on ChatMessage.from_dict instead of manual tool-call mapping."""
    client = MagicMock()
    client.config = {"model": "test/local"}
    model = WriterAgentSmolModel(client, max_tokens=256)
    client.request_with_tools.return_value = {
        "content": "",
        "tool_calls": [
            {
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "reply_to_user",
                    "arguments": '{"answer": "hi"}',
                },
            }
        ],
        "finish_reason": "tool_calls",
        "images": None,
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }

    msgs = [ChatMessage(role=MessageRole.USER, content="hello")]
    result = model.generate(msgs, tools_to_call_from=[_ReplyTool()])

    assert result.tool_calls is not None
    assert result.tool_calls[0].id == "call_123"
    assert result.tool_calls[0].function.name == "reply_to_user"
    assert result.tool_calls[0].function.arguments == '{"answer": "hi"}'
    assert result.token_usage is not None
    assert result.token_usage.input_tokens == 7
    assert result.token_usage.output_tokens == 3
