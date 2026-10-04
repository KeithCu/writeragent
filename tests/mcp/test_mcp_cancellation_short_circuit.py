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
