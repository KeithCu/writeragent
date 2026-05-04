"""Unit tests for shared smol ToolBase adapter and agent factory."""

from unittest.mock import MagicMock, patch

from plugin.framework.smol_agent_factory import build_toolcalling_agent
from plugin.framework.smol_tool_adapter import SmolToolAdapter, to_smol_inputs
from plugin.framework.tool_base import ToolBase
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def test_to_smol_inputs_librarian_nullable_from_required():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "description": "da"},
            "b": {"type": "integer", "description": "db"},
        },
        "required": ["a"],
    }
    inputs = to_smol_inputs(schema, style="librarian")
    assert inputs["a"]["nullable"] is False
    assert inputs["b"]["nullable"] is True
    assert inputs["a"]["type"] == "string"


def test_to_smol_inputs_specialized_preserves_enum_and_default_type():
    schema = {
        "type": "object",
        "properties": {
            "domain": {
                "enum": ["a", "b"],
                "description": "pick",
            },
        },
        "required": ["domain"],
    }
    inputs = to_smol_inputs(schema, style="specialized")
    assert inputs["domain"]["enum"] == ["a", "b"]
    assert inputs["domain"]["type"] == "any"
    assert inputs["domain"]["description"] == "pick"


class _StubTool(ToolBase):
    name = "stub"
    description = "desc"
    parameters = {
        "type": "object",
        "properties": {"p": {"type": "string", "description": "param"}},
        "required": ["p"],
    }

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "p": kwargs.get("p")}

    def is_async(self):
        return False


def test_smol_tool_adapter_unsafe_uses_execute():
    ctx = MagicMock()
    tool = _StubTool()
    tool.execute = MagicMock(return_value={"status": "ok"})
    adapter = SmolToolAdapter(tool, ctx, safe=False, inputs_style="librarian")
    out = adapter.forward(p="v")
    tool.execute.assert_called_once()
    assert out["status"] == "ok"


def test_smol_tool_adapter_safe_async_uses_execute_safe():
    ctx = MagicMock()

    class AsyncTool(_StubTool):
        def is_async(self):
            return True

    tool = AsyncTool()
    tool.execute_safe = MagicMock(return_value={"status": "ok"})
    adapter = SmolToolAdapter(tool, ctx, safe=True, main_thread_sync=True, inputs_style="specialized")
    adapter.forward(p="x")
    tool.execute_safe.assert_called_once()


@patch("plugin.framework.smol_agent_factory.ToolCallingAgent")
@patch("plugin.framework.smol_agent_factory.WriterAgentSmolModel")
@patch("plugin.framework.smol_agent_factory.LlmClient")
@patch("plugin.framework.smol_agent_factory.get_config_int")
@patch("plugin.framework.smol_agent_factory.get_api_config")
def test_build_toolcalling_agent_wires_max_tokens_and_steps(
    mock_get_api, mock_get_int, mock_llm, mock_wsm, mock_tca
):
    mock_get_api.return_value = {"model": "test/model"}

    def _int(_ctx, key: str) -> int:
        if key == "chat_max_tokens":
            return 512
        if key == "chat_max_tool_rounds":
            return 12
        raise AssertionError(key)

    mock_get_int.side_effect = _int

    from plugin.contrib.smolagents.tools import Tool

    class Tiny(Tool):
        name = "tiny"
        description = "d"
        inputs = {"a": {"type": "string", "description": "d", "nullable": True}}
        output_type = "string"

        def forward(self, a=""):
            return a

    ctx = MagicMock()
    ctx.ctx = MagicMock()
    build_toolcalling_agent(
        ctx,
        [Tiny()],
        instructions="inst",
        final_answer_tool_name="reply_to_user",
        examples_block="examples",
        status_callback=None,
    )
    mock_llm.assert_called_once_with({"model": "test/model"}, ctx.ctx)
    assert mock_wsm.call_args.kwargs["max_tokens"] == 512
    tca_kw = mock_tca.call_args.kwargs
    assert tca_kw["max_steps"] == 12
    assert tca_kw["instructions"] == "inst"
    assert tca_kw["final_answer_tool_name"] == "reply_to_user"
    assert tca_kw["system_prompt_examples"] == "examples"
