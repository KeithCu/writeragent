from unittest.mock import MagicMock, patch
from plugin.chatbot.panel import SendButtonListener
from plugin.chatbot.tool_loop_actions import TurnController

def get_dummy_listener():
    class DummyListener:
        def __init__(self):
            self.ctx = MagicMock()
            self._panel_teardown = False
            self._last_mcp_turn = {}

        def _append_response(self, text):
            pass

        _on_mcp_result = SendButtonListener._on_mcp_result

    return DummyListener()

def test_on_mcp_result_drops_stale_turn():
    listener = get_dummy_listener()

    current_turn = TurnController(MagicMock(), MagicMock(), MagicMock())
    current_turn._alive = True

    old_turn = TurnController(MagicMock(), MagicMock(), MagicMock())
    listener._last_mcp_turn = {"": old_turn}

    with patch("plugin.chatbot.tool_loop_actions.current_turn", return_value=current_turn):
        with patch("plugin.framework.queue_executor.post_to_main_thread") as post_mock:
            listener._on_mcp_result(tool="test_tool", result_snippet="success")
            post_mock.assert_not_called()
            assert listener._last_mcp_turn == {}

def test_on_mcp_result_drops_dead_turn():
    listener = get_dummy_listener()

    current_turn = TurnController(MagicMock(), MagicMock(), MagicMock())
    current_turn._alive = False

    listener._last_mcp_turn = {"": current_turn}

    with patch("plugin.chatbot.tool_loop_actions.current_turn", return_value=current_turn):
        with patch("plugin.framework.queue_executor.post_to_main_thread") as post_mock:
            listener._on_mcp_result(tool="test_tool", result_snippet="success")
            post_mock.assert_not_called()
            assert listener._last_mcp_turn == {}

def test_on_mcp_result_posts_on_valid_turn():
    listener = get_dummy_listener()

    current_turn = TurnController(MagicMock(), MagicMock(), MagicMock())
    current_turn._alive = True

    listener._last_mcp_turn = {"": current_turn}

    with patch("plugin.chatbot.tool_loop_actions.current_turn", return_value=current_turn):
        with patch("plugin.framework.thread_guard.on_main_thread", return_value=False):
            with patch("plugin.framework.thread_guard.get_background_task_name", return_value="some_task"):
                with patch("plugin.framework.queue_executor.post_to_main_thread") as post_mock:
                    listener._on_mcp_result(tool="test_tool", result_snippet="success")
                    post_mock.assert_called_once()
