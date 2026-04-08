import pytest
from unittest.mock import MagicMock, patch
import sys

# Mock uno and dependencies for headless testing
class MockBase: pass
sys.modules['uno'] = MagicMock()
sys.modules['unohelper'] = MagicMock()
sys.modules['unohelper'].Base = MockBase
sys.modules['com.sun.star.text'] = MagicMock()
sys.modules['com.sun.star.awt'] = MagicMock()

from plugin.framework.tool_registry import ToolRegistry
from plugin.framework.tool_context import ToolContext
from plugin.modules.writer.base import ToolWriterSpecialBase, SpecializedWorkflowFinished
from plugin.modules.writer.specialized import DelegateToSpecializedWriter


class DummyTableTool(ToolWriterSpecialBase):
    name = "dummy_table_tool"
    description = "A dummy table tool."
    parameters = None
    specialized_domain = "tables"

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "message": "Table created"}


@pytest.fixture
def registry():
    r = ToolRegistry(services={})
    r.register(DummyTableTool())
    r.register(SpecializedWorkflowFinished())
    r.register(DelegateToSpecializedWriter())
    return r


@pytest.fixture
def mock_ctx(registry):
    ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.ctx = MagicMock()
    return ctx


def test_specialized_delegation_in_place_mode(registry, mock_ctx):
    # Enable in-place switching mode
    import plugin.modules.writer.specialized
    plugin.modules.writer.specialized.USE_SUB_AGENT = False

    active_domain = None

    def mock_set_active_domain(domain):
        nonlocal active_domain
        active_domain = domain

    mock_ctx.set_active_domain_callback = mock_set_active_domain

    gateway_tool = registry.get("delegate_to_specialized_writer_toolset")

    # Make sure stop_checker returns False so it doesn't abort immediately
    mock_ctx.stop_checker = lambda: False

    # Execute the gateway tool
    result = gateway_tool.execute_safe(
        mock_ctx,
        domain="tables",
        task="Create a 3x3 table"
    )

    # Verify callback was called
    assert active_domain == "tables"
    assert result["status"] == "ok"
    assert "Tool call switched to 'tables'" in result["message"]

    # Now simulate the next LLM turn using the active domain
    schemas = registry.get_schemas("openai", active_domain=active_domain)

    # It should only include the specialized domain tools and final_answer
    tool_names = [s["function"]["name"] for s in schemas]
    assert "dummy_table_tool" in tool_names
    assert "specialized_workflow_finished" in tool_names
    assert "delegate_to_specialized_writer_toolset" not in tool_names

    # Now execute final_answer to finish
    finish_tool = registry.get("specialized_workflow_finished")
    finish_result = finish_tool.execute_safe(mock_ctx, answer="Done")

    # Verify callback was called to clear the domain
    assert active_domain is None
    assert finish_result["status"] == "ok"
    assert finish_result["answer"] == "Done"


from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
from plugin.framework.config import get_config_int as _real_get_config_int

def _mock_get_config_int_for_sub_agent(ctx, key):
    if key == "chat_max_tokens":
        return 2048
    if key == "chat_max_tool_rounds":
        return 25
    return _real_get_config_int(ctx, key)


@patch(
    "plugin.framework.config.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.modules.writer.specialized.get_api_config", create=True)
@patch("plugin.contrib.smolagents.agents.ToolCallingAgent")
@patch("plugin.framework.smol_model.WriterAgentSmolModel")
@patch("plugin.modules.http.client.LlmClient")
def test_specialized_delegation_sub_agent_mode(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
    registry,
    mock_ctx,
):
    # Enable sub-agent mode
    import plugin.modules.writer.specialized
    plugin.modules.writer.specialized.USE_SUB_AGENT = True

    mock_ctx.ctx = MagicMock()
    mock_get_config.return_value = {}

    active_domain = "initial_value"

    def mock_set_active_domain(domain):
        nonlocal active_domain
        active_domain = domain

    mock_ctx.set_active_domain_callback = mock_set_active_domain

    # Setup the mocked smolagents agent to yield a FinalAnswerStep
    mock_agent_instance = MagicMock()

    from plugin.contrib.smolagents.memory import FinalAnswerStep
    dummy_final_step = FinalAnswerStep(output="Mocked agent summary")
    mock_agent_instance.run.return_value = [dummy_final_step]

    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_writer_toolset")

    mock_ctx.stop_checker = lambda: False

    # Execute the gateway tool
    result = gateway_tool.execute_safe(
        mock_ctx,
        domain="tables",
        task="Create a 3x3 table"
    )

    # Verify callback was NOT called (it remains initial_value)
    assert active_domain == "initial_value"

    # Output the result for debugging if it fails
    if result["status"] != "ok":
        print(result)

    # Verify smolagents was executed and returned the summary
    assert result["status"] == "ok"
    assert "completed" in result["message"]
    assert result["result"] == "Mocked agent summary"

    # Verify the tools passed to the sub-agent
    call_args = mock_agent_class.call_args
    assert call_args is not None
    smol_tools = call_args.kwargs.get("tools", [])

    # The sub-agent should only receive dummy_table_tool, NOT final_answer
    smol_tool_names = [t.name for t in smol_tools]
    assert "dummy_table_tool" in smol_tool_names
    assert "specialized_workflow_finished" not in smol_tool_names
