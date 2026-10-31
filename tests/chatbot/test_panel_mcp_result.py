from unittest.mock import MagicMock, patch
from plugin.chatbot.tool_loop_actions import TurnController

def get_dummy_listener():
    from tests.conftest import make_mock_send_button_listener

    listener = make_mock_send_button_listener()
    listener._append_response = MagicMock()
    return listener

def test_on_mcp_result_drops_stale_turn():
    listener = get_dummy_listener()

    current_turn = TurnController(MagicMock(), MagicMock(), MagicMock())
    current_turn.alive = True

    old_turn = TurnController(MagicMock(), MagicMock(), MagicMock())
    listener._last_mcp_turn = {"": old_turn}

    with patch("plugin.chatbot.tool_loop_actions.current_turn", return_value=current_turn):
        with patch("plugin.framework.queue_executor.post_to_main_thread") as post_mock:
            listener._events._on_mcp_result(tool="test_tool", result_snippet="success")
            post_mock.assert_not_called()
            assert listener._last_mcp_turn == {}

def test_on_mcp_result_drops_dead_turn():
    listener = get_dummy_listener()

    current_turn = TurnController(MagicMock(), MagicMock(), MagicMock())
    current_turn.alive = False

    listener._last_mcp_turn = {"": current_turn}

    with patch("plugin.chatbot.tool_loop_actions.current_turn", return_value=current_turn):
        with patch("plugin.framework.queue_executor.post_to_main_thread") as post_mock:
            listener._events._on_mcp_result(tool="test_tool", result_snippet="success")
            post_mock.assert_not_called()
            assert listener._last_mcp_turn == {}

def test_on_mcp_result_posts_on_valid_turn():
    listener = get_dummy_listener()

    current_turn = TurnController(MagicMock(), MagicMock(), MagicMock())
    current_turn.alive = True

    listener._last_mcp_turn = {"": current_turn}

    with patch("plugin.chatbot.tool_loop_actions.current_turn", return_value=current_turn):
        with patch("plugin.framework.thread_guard.on_main_thread", return_value=False):
            with patch("plugin.framework.thread_guard.get_background_task_name", return_value="some_task"):
                with patch("plugin.framework.queue_executor.post_to_main_thread") as post_mock:
                    listener._events._on_mcp_result(tool="test_tool", result_snippet="success")
                    post_mock.assert_called_once()
