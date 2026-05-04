from unittest.mock import MagicMock

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.framework.smol_model import WriterAgentSmolModel
from plugin.contrib.smolagents.models import ChatMessage, MessageRole
from plugin.contrib.smolagents.tools import Tool


class _ReplyTool(Tool):
    name = "reply_to_user"
    description = "Reply to the user"
    inputs = {"answer": {"type": "string", "description": "answer", "nullable": True}}
    output_type = "string"

    def forward(self, answer=""):
        return answer


def test_request_with_tools_receives_none_for_tools():
    """Smol path must not send OpenAI tool schemas; local servers can 500 on constrained parse."""
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
    assert client.request_with_tools.call_args.kwargs.get("tools") is None
    assert client.request_with_tools.call_args[0][0]  # non-empty messages
