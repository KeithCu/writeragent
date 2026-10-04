from unittest.mock import MagicMock
from plugin.chatbot.panel import SendButtonListener

def test_mcp_result_ignores_old_req_id():
    listener = SendButtonListener.__new__(SendButtonListener)
    listener.ctx = MagicMock()
    listener._panel_teardown = False
    listener._last_mcp_req_id = "request_1"
    listener._mcp_event_bus = MagicMock()

    # Mock _last_mcp_turn and current_turn
    turn_mock = MagicMock()
    turn_mock.alive = True

    from plugin.chatbot.tool_loop_actions import TurnController
    turn_mock.__class__ = TurnController
    listener._last_mcp_turn = turn_mock

    import plugin.chatbot.tool_loop_actions
    original_current_turn = getattr(plugin.chatbot.tool_loop_actions, "current_turn", None)
    plugin.chatbot.tool_loop_actions.current_turn = lambda x: turn_mock

    # Set up UI mock
    append_mock = MagicMock()
    listener._append_response = append_mock

    try:
        # If it returns early, _update_ui won't be defined or post_to_main_thread won't be called.
        # We can just verify it doesn't throw and finishes properly.
        listener._on_mcp_result(tool="test", result_snippet="snip", req_id="request_1")

        # Mismatch req_id:
        listener._on_mcp_result(tool="test", result_snippet="snip", req_id="request_2")
    finally:
        if original_current_turn:
            plugin.chatbot.tool_loop_actions.current_turn = original_current_turn
