from unittest.mock import MagicMock
from plugin.mcp.mcp_protocol import MCPProtocolHandler, _PreparedMcpCall
from plugin.framework.tool import ToolContext

def test_mcp_protocol_short_circuits_if_stopped_after_gate():
    services_mock = MagicMock()
    protocol = MCPProtocolHandler(services_mock)

    ctx_mock = MagicMock(spec=ToolContext)
    # Stop checker evaluates to True immediately after wait
    ctx_mock.stop_checker = lambda: True

    prepared = _PreparedMcpCall(
        tool=MagicMock(),
        context=ctx_mock,
        doc=MagicMock(),
        doc_key="file:///test.odt",
        needs_gate=True,
        echo=None
    )

    result = protocol._run_prepared_mcp_execute(prepared, "test_tool", {})
    assert result == {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user"}


def test_invoke_prepared_short_circuits_when_stop_checker_is_latched():
    services_mock = MagicMock()
    protocol = MCPProtocolHandler(services_mock)
    protocol.tool_registry = MagicMock()

    ctx_mock = MagicMock(spec=ToolContext)
    ctx_mock.stop_checker = lambda: True

    prepared = _PreparedMcpCall(
        tool=MagicMock(),
        context=ctx_mock,
        doc=MagicMock(),
        doc_key=MagicMock(),
        needs_gate=False,
        echo=None,
    )

    result = protocol._invoke_prepared_mcp_tool(prepared, "test_tool", {"a": 1})
    assert result == {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user"}
    protocol.tool_registry.execute.assert_not_called()

