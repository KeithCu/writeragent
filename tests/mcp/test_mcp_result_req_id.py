from unittest.mock import MagicMock, patch
from plugin.chatbot.panel import SendButtonListener
from plugin.chatbot.tool_loop_actions import TurnController


def test_mcp_result_preserves_concurrent_requests():
    """Verify that earlier concurrent MCP requests are not dropped by later request IDs."""
    listener = SendButtonListener.__new__(SendButtonListener)
    listener.ctx = MagicMock()
    listener._panel_teardown = False
    listener._mcp_event_bus = MagicMock()
    listener._last_mcp_turn = {}

    turn_mock = MagicMock(spec=TurnController)
    turn_mock.alive = True

    append_mock = MagicMock()
    listener._append_response = append_mock

    with patch("plugin.chatbot.tool_loop_actions.current_turn", return_value=turn_mock):
        # Two requests arrive concurrently
        listener._on_mcp_request(tool="test1", req_id="request_1")
        listener._on_mcp_request(tool="test2", req_id="request_2")

        # Result for request_1 arrives after request_2 was registered
        listener._on_mcp_result(tool="test1", result_snippet="snip1", req_id="request_1")
        # Result for request_2 arrives
        listener._on_mcp_result(tool="test2", result_snippet="snip2", req_id="request_2")

    # Both results must be appended
    assert append_mock.call_count == 2
    assert "test1" in append_mock.call_args_list[0][0][0]
    assert "test2" in append_mock.call_args_list[1][0][0]

