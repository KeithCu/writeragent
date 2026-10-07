from unittest.mock import MagicMock, patch
from plugin.framework.frame_session import FrameSession
from plugin.chatbot.panel_factory import ChatPanelElement, _bind_close_hook, release_live_sidebar

def test_close_hook_prevents_double_release():
    with patch("plugin.chatbot.panel_factory.release_live_sidebar") as mock_release:
        ctx = MagicMock()
        frame = MagicMock()
        parent = MagicMock()
        session = FrameSession(frame)

        panel = ChatPanelElement(ctx, frame, parent, "url", session)
        session.bind_panel(panel)
        listener = MagicMock()
        query = MagicMock()

        _bind_close_hook(session, panel, listener, query)

        session.dispose()
        mock_release.assert_called_once_with(panel, query)
        assert session.panel is None

        mock_release.reset_mock()
        session.dispose()
        mock_release.assert_not_called()

def test_aborted_turn_prevents_reply_save():
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
    assert len(session.messages) == 1

    # Aborting without closed_by_document still saves (e.g. Stop partial)
    turn.abort()
    turn.persist_assistant(host, content="stopped partial")
    assert len(session.messages) == 2

    # Closed by document prevents saving
    turn.closed_by_document = True
    turn.persist_assistant(host, content="aborted reply")
    assert len(session.messages) == 2

def test_release_live_sidebar_called_twice_is_noop():
    panel = MagicMock()
    panel._live_panel_uid = "doc1"
    panel._released = False
    panel.frame_session = MagicMock()
    listener = MagicMock()
    panel.send_listener = listener

    release_live_sidebar(panel, None)
    listener.disposing.assert_called_once_with(None)
    panel.frame_session.release_panel.assert_called_once_with(panel, None)

    release_live_sidebar(panel, None)
    # A second release (frame close hook, then element dispose) must not raise.
    # It now short-circuits due to the _released flag.
    assert listener.disposing.call_count == 1
    assert panel.frame_session.release_panel.call_count == 1
