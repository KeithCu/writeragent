import pytest
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.framework.tool import ToolRegistry, _is_specialized_domain_tool
from plugin.modules.writer.base import ToolWriterSpecialBase, SpecializedWorkflowFinished
from plugin.modules.writer.specialized import DelegateToSpecializedWriter
from plugin.modules.calc.specialized import DelegateToSpecializedCalc
from plugin.modules.draw.specialized import DelegateToSpecializedDraw
from plugin.modules.calc.base import ToolCalcSpecialBase
from plugin.modules.draw.base import ToolDrawSpecialBase


class DummyTableTool(ToolWriterSpecialBase):
    name = "dummy_table_tool"
    description = "A dummy table tool."
    parameters = None
    specialized_domain = "tables"

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "message": "Table created"}


class DummyShapeTool(ToolWriterSpecialBase):
    name = "dummy_shape_tool"
    description = "A dummy shape tool."
    parameters = {"type": "object", "properties": {}, "required": []}
    specialized_domain = "shapes"

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "message": "ok"}


class DummyCalcSpecialTool(ToolCalcSpecialBase):
    name = "dummy_calc_images_tool"
    description = "A dummy Calc specialized tool."
    parameters = {"type": "object", "properties": {}, "required": []}
    specialized_domain = "images"

    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "message": "ok"}


class DummyDrawSpecialTool(ToolDrawSpecialBase):
    name = "dummy_draw_special_tool"
    description = "A dummy Draw specialized tool."
    parameters = {"type": "object", "properties": {}, "required": []}
    specialized_domain = "draw_test_domain"

    uno_services = ["com.sun.star.drawing.DrawingDocument"]

    def execute(self, ctx, **kwargs):
        return {"status": "ok", "message": "ok"}


@pytest.fixture
def registry():
    r = ToolRegistry(services={})
    r.register(DummyTableTool())
    r.register(DummyShapeTool())
    r.register(SpecializedWorkflowFinished())
    r.register(DelegateToSpecializedWriter())
    return r


@pytest.fixture
def mock_ctx(registry):
    ctx = MagicMock()
    ctx.services = {"tools": registry}
    ctx.ctx = MagicMock()
    return ctx


from plugin.contrib.smolagents.memory import FinalAnswerStep
from plugin.framework.config import get_config_int as _real_get_config_int

def _mock_get_config_int_for_sub_agent(ctx, key):
    if key == "chat_max_tokens":
        return 2048
    if key == "chat_max_tool_rounds":
        return 25
    return _real_get_config_int(ctx, key)


@patch(
    "plugin.framework.smol_agent.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.framework.smol_agent.get_api_config", create=True)
@patch("plugin.framework.smol_agent.ToolCallingAgent")
@patch("plugin.framework.smol_agent.WriterAgentSmolModel")
@patch("plugin.framework.smol_agent.LlmClient")
def test_specialized_delegation_sub_agent_mode(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
    registry,
    mock_ctx,
):
    # Sub-agent path (default USE_SUB_AGENT=True in writer/specialized.py)
    mock_ctx.ctx = MagicMock()
    mock_get_config.return_value = {}

    active_domain = "initial_value"

    def mock_set_active_domain(domain):
        nonlocal active_domain
        active_domain = domain

    mock_ctx.set_active_domain_callback = mock_set_active_domain

    # Setup the mocked smolagents agent to yield a FinalAnswerStep
    mock_agent_instance = MagicMock()

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


@patch(
    "plugin.framework.smol_agent.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.framework.smol_agent.get_api_config", create=True)
@patch("plugin.framework.smol_agent.ToolCallingAgent")
@patch("plugin.framework.smol_agent.WriterAgentSmolModel")
@patch("plugin.framework.smol_agent.LlmClient")
def test_shapes_delegation_includes_canvas_in_instructions(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
    registry,
    mock_ctx,
):
    mock_ctx.ctx = MagicMock()
    mock_get_config.return_value = {}

    doc = MagicMock()

    def supports(svc: str) -> bool:
        return svc == "com.sun.star.text.TextDocument"

    doc.supportsService = supports
    doc.getCurrentController.return_value = None
    style = MagicMock()

    def gv(name: str):
        return {
            "Width": 21_000,
            "Height": 29_700,
            "LeftMargin": 2_000,
            "RightMargin": 2_000,
            "TopMargin": 2_500,
            "BottomMargin": 2_500,
            "IsLandscape": False,
        }[name]

    style.getPropertyValue = gv
    page_styles = MagicMock()
    page_styles.hasByName = lambda n: n == "Standard"
    page_styles.getByName = lambda n: style if n == "Standard" else MagicMock()
    families = MagicMock()
    families.getByName = lambda n: page_styles if n == "PageStyles" else MagicMock()
    doc.getStyleFamilies.return_value = families
    mock_ctx.doc = doc

    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="Shapes done")]
    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_writer_toolset")
    mock_ctx.stop_checker = lambda: False

    result = gateway_tool.execute_safe(
        mock_ctx,
        domain="shapes",
        task="Add a rectangle",
    )
    assert result["status"] == "ok"
    instructions = mock_agent_class.call_args.kwargs["instructions"]
    assert "Document canvas (Writer)" in instructions
    assert "210.0" in instructions
    smol_tools = mock_agent_class.call_args.kwargs.get("tools", [])
    assert any(t.name == "dummy_shape_tool" for t in smol_tools)


def test_is_specialized_domain_tool_helper():
    t = DummyTableTool()
    assert _is_specialized_domain_tool(t, "tables") is True
    assert _is_specialized_domain_tool(t, "images") is False
    c = DummyCalcSpecialTool()
    assert _is_specialized_domain_tool(c, "images") is True
    d = DummyDrawSpecialTool()
    assert _is_specialized_domain_tool(d, "draw_test_domain") is True


def test_active_domain_schemas_include_calc_and_draw(registry):
    """Calc/Draw specialized tools must appear when active_domain matches (in-place mode)."""
    registry.register(DummyCalcSpecialTool())
    registry.register(DummyDrawSpecialTool())

    mock_sheet = MagicMock()

    def supports(svc):
        return svc == "com.sun.star.sheet.SpreadsheetDocument"

    mock_sheet.supportsService = supports

    schemas = registry.get_schemas("openai", doc=mock_sheet, active_domain="images")
    names = [s["function"]["name"] for s in schemas]
    assert "dummy_calc_images_tool" in names
    assert "specialized_workflow_finished" in names

    mock_draw = MagicMock()

    def supports_draw(svc):
        return svc == "com.sun.star.drawing.DrawingDocument"

    mock_draw.supportsService = supports_draw

    schemas_d = registry.get_schemas("openai", doc=mock_draw, active_domain="draw_test_domain")
    names_d = [s["function"]["name"] for s in schemas_d]
    assert "dummy_draw_special_tool" in names_d
    assert "specialized_workflow_finished" in names_d


@patch(
    "plugin.framework.smol_agent.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.framework.smol_agent.get_api_config", create=True)
@patch("plugin.framework.smol_agent.ToolCallingAgent")
@patch("plugin.framework.smol_agent.WriterAgentSmolModel")
@patch("plugin.framework.smol_agent.LlmClient")
def test_calc_specialized_delegation_sub_agent(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
    registry,
    mock_ctx,
):
    registry.register(DummyCalcSpecialTool())
    registry.register(DelegateToSpecializedCalc())
    
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="Calc done")]
    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_calc_toolset")
    mock_ctx.stop_checker = lambda: False
    
    result = gateway_tool.execute_safe(mock_ctx, domain="images", task="Insert image")
    
    assert result["status"] == "ok"
    assert result["result"] == "Calc done"
    
    # Verify the domain tools passed
    call_args = mock_agent_class.call_args
    smol_tools = call_args.kwargs.get("tools", [])
    smol_tool_names = [t.name for t in smol_tools]
    assert "dummy_calc_images_tool" in smol_tool_names


@patch(
    "plugin.framework.smol_agent.get_config_int",
    side_effect=_mock_get_config_int_for_sub_agent,
)
@patch("plugin.framework.smol_agent.get_api_config", create=True)
@patch("plugin.framework.smol_agent.ToolCallingAgent")
@patch("plugin.framework.smol_agent.WriterAgentSmolModel")
@patch("plugin.framework.smol_agent.LlmClient")
def test_draw_specialized_delegation_sub_agent(
    mock_llm,
    mock_smol_model,
    mock_agent_class,
    mock_get_config,
    _mock_get_config_int,
    registry,
    mock_ctx,
):
    registry.register(DummyDrawSpecialTool())
    registry.register(DelegateToSpecializedDraw())
    
    mock_agent_instance = MagicMock()
    mock_agent_instance.run.return_value = [FinalAnswerStep(output="Draw done")]
    mock_agent_class.return_value = mock_agent_instance

    gateway_tool = registry.get("delegate_to_specialized_draw_toolset")
    mock_ctx.stop_checker = lambda: False
    
    result = gateway_tool.execute_safe(mock_ctx, domain="draw_test_domain", task="Create shape")
    
    assert result["status"] == "ok"
    assert result["result"] == "Draw done"
    
    # Verify the domain tools passed
    call_args = mock_agent_class.call_args
    smol_tools = call_args.kwargs.get("tools", [])
    smol_tool_names = [t.name for t in smol_tools]
    assert "dummy_draw_special_tool" in smol_tool_names
