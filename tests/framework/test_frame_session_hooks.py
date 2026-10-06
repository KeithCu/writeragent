import unittest
from unittest.mock import MagicMock, patch
from plugin.framework.frame_session import FrameSession
from plugin.chatbot.panel_factory import ChatPanelElement

class TestFrameSessionHooks(unittest.TestCase):
    @patch("plugin.chatbot.panel_factory.release_live_sidebar")
    def test_close_hook_prevents_double_release(self, mock_release):
        ctx = MagicMock()
        frame = MagicMock()
        parent = MagicMock()
        session = FrameSession(frame)

        panel = ChatPanelElement(ctx, frame, parent, "url", session)

        session.bind_panel(panel)
        def on_frame_close() -> None:
            if getattr(session, "panel", None) is panel:
                mock_release(panel)
        session.add_close_hook(on_frame_close)

        session.dispose()
        mock_release.assert_called_once_with(panel)

        self.assertIsNone(session.panel)

        mock_release.reset_mock()
        session.dispose()
        mock_release.assert_not_called()

    def test_aborted_turn_prevents_reply_save(self):
        from plugin.chatbot.tool_loop_actions import TurnController
        class FakeHost:
            def __init__(self):
                self._turn = None
        class FakeSession:
            def __init__(self):
                self.messages = []
            def add_assistant_message(self, **kwargs):
                self.messages.append(kwargs)

        session = FakeSession()
        turn = TurnController(session, mode="chat")
        host = FakeHost()
        host._turn = turn

        turn.persist_assistant(host, content="alive reply")
        self.assertEqual(len(session.messages), 1)

        # Aborting without closed_by_document still saves (e.g. Stop partial)
        turn.abort()
        turn.persist_assistant(host, content="stopped partial")
        self.assertEqual(len(session.messages), 2)

        # Closed by document prevents saving
        turn.closed_by_document = True
        turn.persist_assistant(host, content="aborted reply")
        self.assertEqual(len(session.messages), 2)

if __name__ == '__main__':
    unittest.main()
