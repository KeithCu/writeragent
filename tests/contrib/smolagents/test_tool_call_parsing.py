# WriterAgent - smolagents tool-call text parsing tests

from plugin.contrib.smolagents.memory import ActionStep, ToolCall
from plugin.contrib.smolagents.models import MessageRole, get_tool_call_from_text
from plugin.contrib.smolagents.monitoring import Timing
from plugin.contrib.smolagents.utils import (
    content_looks_like_tool_call,
    coerce_final_answer_value,
    parse_json_blob,
    try_parse_implicit_final_answer_tool_call,
)
import pytest

USER_LOG_SNIPPET = (
    "Calling tools:\n"
    "[{'id': 'call_019e8619a1967b11b7fccbbf', 'type': 'function', "
    "'function': {'name': 'web_search', 'arguments': {'query': "
    "'Cottage Inn Pizza Madison Heights Michigan 505 W Eleven Mile Rd phone number reviews'}}}]"
)


def test_parse_json_blob_python_repr_calling_tools_prefix():
    data, prefix = parse_json_blob(USER_LOG_SNIPPET)
    assert prefix.startswith("Calling tools:")
    assert data["function"]["name"] == "web_search"
    assert "Cottage Inn Pizza" in data["function"]["arguments"]["query"]


def test_get_tool_call_from_text_openai_nested_python_repr():
    tc = get_tool_call_from_text(USER_LOG_SNIPPET, "name", "arguments")
    assert tc.function.name == "web_search"
    assert tc.function.arguments == {
        "query": "Cottage Inn Pizza Madison Heights Michigan 505 W Eleven Mile Rd phone number reviews"
    }


def test_get_tool_call_from_text_standard_action_json():
    text = 'Action:\n{"name": "web_search", "arguments": "Population Shanghai"}'
    tc = get_tool_call_from_text(text, "name", "arguments")
    assert tc.function.name == "web_search"
    assert tc.function.arguments == "Population Shanghai"


def test_get_tool_call_from_text_openai_nested_double_quoted_json():
    text = (
        '{"id": "call_1", "type": "function", '
        '"function": {"name": "web_search", "arguments": {"query": "test"}}}'
    )
    tc = get_tool_call_from_text(text, "name", "arguments")
    assert tc.function.name == "web_search"
    assert tc.function.arguments == {"query": "test"}


def test_content_looks_like_tool_call_for_mimicked_output():
    known = {"web_search", "final_answer", "visit_webpage"}
    assert content_looks_like_tool_call(USER_LOG_SNIPPET, known_tool_names=known)


def test_content_looks_like_tool_call_false_for_plain_answer():
    known = {"web_search", "final_answer"}
    assert not content_looks_like_tool_call("Shanghai has the larger population.", known_tool_names=known)


def test_coerce_final_answer_value_joins_html_array():
    assert coerce_final_answer_value(["<p>a</p>", "<p>b</p>"]) == "<p>a</p>\n<p>b</p>"


def test_try_parse_implicit_final_answer_mercury_style_blob():
    text = '{"answer": ["<h1>Title</h1>", "<p>Body with \\\\(4.3\\\\) stars.</p>"]}'
    tc = try_parse_implicit_final_answer_tool_call(text, "final_answer")
    assert tc is not None
    assert tc.function.name == "final_answer"
    assert tc.function.arguments == {
        "answer": "<h1>Title</h1>\n<p>Body with \\(4.3\\) stars.</p>"
    }


@pytest.mark.parametrize(
    "value",
    [
        pytest.param('{"name": "web_search", "arguments": {"query": "pizza"}}', id="test_try_parse_implicit_final_answer_none_for_web_search_shape"),
        pytest.param('{"name": "final_answer", "arguments": "Done."}', id="test_try_parse_implicit_final_answer_none_for_proper_tool_call"),
    ],
)
def test_try_parse_implicit_final_answer_none_for_web_search_shape(value):
    text = value
    assert try_parse_implicit_final_answer_tool_call(text, "final_answer") is None

def test_content_looks_like_tool_call_false_for_answer_only_json():
    known = {"web_search", "final_answer"}
    blob = '{"answer": ["<p>hi</p>"]}'
    assert not content_looks_like_tool_call(blob, known_tool_names=known)


def test_action_step_to_messages_uses_action_json_not_calling_tools_repr():
    step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0),
        tool_calls=[
            ToolCall(
                name="web_search",
                arguments={"query": "Population Shanghai"},
                id="call_1",
            )
        ],
    )
    messages = step.to_messages()
    tool_msg = next(m for m in messages if m.role == MessageRole.TOOL_CALL)
    text = tool_msg.content[0]["text"]
    assert text.startswith("Action:\n")
    assert "Calling tools:" not in text
    assert '"name": "web_search"' in text
    assert "Population Shanghai" in text


def test_get_tool_json_schema_normalizes_nullable_scalars():
    """Verify smolagents get_tool_json_schema normalizes optional parameters to ['type', 'null']."""
    from plugin.contrib.smolagents.models import get_tool_json_schema
    from plugin.contrib.smolagents.tools import Tool

    class DummyImageTool(Tool):
        name = "image_generate"
        description = "Generate an image"
        inputs = {
            "prompt": {"type": "string", "description": "The prompt", "nullable": False},
            "source_image": {"type": "string", "description": "Optional image", "nullable": True},
        }
        output_type = "string"
        skip_forward_signature_validation = True

        def forward(self, prompt: str, source_image: str | None = None) -> str:
            return "done"

    schema = get_tool_json_schema(DummyImageTool())
    fn = schema["function"]
    params = fn["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["prompt"]
    assert params["properties"]["prompt"]["type"] == "string"
    assert params["properties"]["source_image"]["type"] == ["string", "null"]
    assert "nullable" not in params["properties"]["source_image"]
