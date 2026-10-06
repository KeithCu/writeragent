# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for HTML tag stripping integration in SendButtonListener/panel.py."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from plugin.chatbot.panel import SendButtonListener
from plugin.chatbot.rich_text import finalize_sidebar_assistant_response
from plugin.framework.deal_shim import DEAL_MAX_HTML_CHUNK
from plugin.framework.html_stripper import StreamingHTMLStripper


def _make_plain_send_listener():
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    with patch.object(SendButtonListener, "__init__", lambda self, *a, **k: None):
        send = SendButtonListener.__new__(SendButtonListener)
        send.ctx = MagicMock()
        send.rich_text_widget = None
        send.response_control = MagicMock()
        send.response_control.getModel.return_value = MagicMock()
        send.session = None
        send._should_auto_scroll = MagicMock(return_value=True)
        send._scroll_response_to_bottom = MagicMock()
        send.queue_executor = MagicMock()
        begin_send_turn(send, "chat")
        return send


def _turn(send):
    from plugin.chatbot.tool_loop_actions import current_turn

    return current_turn(send)


class TestPanelHTMLStripper:
    @patch("plugin.chatbot.dialogs.get_control_text", return_value="Current: ")
    @patch("plugin.chatbot.dialogs.set_control_text")
    def test_append_response_plain_text_strips_streaming_tags(self, mock_set_text, mock_get_text):
        send = _make_plain_send_listener()
        
        # Stream chunks with tags split
        chunk1 = "Hello <st"
        chunk2 = "rong>World</str"
        chunk3 = "ong>!"
        
        # Chunk 1: `<st` is buffered, returns "Hello "
        with patch("plugin.chatbot.panel.threading.current_thread", return_value=threading.main_thread()):
            send._append_response(chunk1, role="assistant")
            mock_set_text.assert_called_with(send.response_control, "Current: Hello ")
            
            # Reset mocks for chunk 2
            mock_get_text.return_value = "Current: Hello "
            send._append_response(chunk2, role="assistant")
            # `<strong` is discarded, `World` is returned, `<str` is buffered.
            mock_set_text.assert_called_with(send.response_control, "Current: Hello World")
            
            # Reset mocks for chunk 3
            mock_get_text.return_value = "Current: Hello World"
            send._append_response(chunk3, role="assistant")
            # `ong>` closes and is discarded, `!` is returned
            mock_set_text.assert_called_with(send.response_control, "Current: Hello World!")

    @patch("plugin.chatbot.dialogs.get_control_text", return_value="Current: ")
    @patch("plugin.chatbot.dialogs.set_control_text")
    def test_append_response_plain_text_direct_strips_html(self, mock_set_text, mock_get_text):
        send = _make_plain_send_listener()
        # Direct non-assistant messages (e.g. user message with HTML tags saved in history)
        with patch("plugin.chatbot.panel.threading.current_thread", return_value=threading.main_thread()):
            send._append_response("<p>Hello</p>", role="user")
            mock_set_text.assert_called_with(send.response_control, "Current: Hello")

    @patch("plugin.chatbot.dialogs.get_control_text", return_value="Current: ")
    @patch("plugin.chatbot.dialogs.set_control_text")
    def test_append_response_long_assistant_chunk_does_not_precontract(
        self, mock_set_text, mock_get_text
    ):
        """One assistant chunk > DEAL_MAX_HTML_CHUNK must still append (debug deal)."""
        send = _make_plain_send_listener()
        long_text = "<p>" + ("y" * (DEAL_MAX_HTML_CHUNK + 1)) + "</p>"
        with patch("plugin.chatbot.panel.threading.current_thread", return_value=threading.main_thread()):
            send._append_response(long_text, role="assistant")
        mock_set_text.assert_called_with(
            send.response_control, "Current: " + ("y" * (DEAL_MAX_HTML_CHUNK + 1))
        )

    @patch("plugin.chatbot.dialogs.get_control_text", return_value="Current: ")
    @patch("plugin.chatbot.dialogs.set_control_text")
    def test_finalize_sidebar_assistant_response_flushes_stripper(self, mock_set_text, mock_get_text):
        send = _make_plain_send_listener()
        send.rerender_rich_text_session = MagicMock()
        
        # Stream incomplete non-tag math like "a <b"
        with patch("plugin.chatbot.panel.threading.current_thread", return_value=threading.main_thread()):
            send._append_response("a <b", role="assistant")
            # returns "a " since "<b" is buffered as potential tag
            mock_set_text.assert_called_with(send.response_control, "Current: a ")
            
            # Finalize response: it should flush the stripper buffer "<b"
            mock_get_text.return_value = "Current: a "
            finalize_sidebar_assistant_response(send)
            
            # Should have appended "<b" to response
            mock_set_text.assert_called_with(send.response_control, "Current: a <b")

    def test_finalize_skips_rerender_after_stop(self):
        """Packet B1: Stop must keep streamed text + [Stopped by user], not paste No response."""
        send = _make_plain_send_listener()
        send.rerender_rich_text_session = MagicMock()
        _turn(send).stripper = None
        finalize_sidebar_assistant_response(send, allow_rerender=False)
        send.rerender_rich_text_session.assert_not_called()
        finalize_sidebar_assistant_response(send, allow_rerender=True)
        send.rerender_rich_text_session.assert_called_once()

    def test_finalize_skips_rerender_on_api_error(self):
        """Packet F: HTTP 429/500 must not be replaced by the previous HTML reply."""
        send = _make_plain_send_listener()
        send.rerender_rich_text_session = MagicMock()
        send._terminal_status = "Error"
        _turn(send).stripper = StreamingHTMLStripper()
        finalize_sidebar_assistant_response(send)
        send.rerender_rich_text_session.assert_not_called()
        assert _turn(send).stripper is None

    def test_finalize_appends_leftover_when_rerender_does_not_replace(self):
        send = _make_plain_send_listener()
        send.rich_text_widget = MagicMock()
        send.rerender_rich_text_session = MagicMock(return_value=False)
        send._append_response = MagicMock()
        _turn(send).stripper.feed("a <b")
        finalize_sidebar_assistant_response(send)
        send._append_response.assert_called_once_with("<b", role="assistant")

    def test_finalize_skips_leftover_when_rerender_replaces_tail(self):
        send = _make_plain_send_listener()
        send.rich_text_widget = MagicMock()
        send.rerender_rich_text_session = MagicMock(return_value=True)
        send._append_response = MagicMock()
        _turn(send).stripper.feed("a <b")
        finalize_sidebar_assistant_response(send)
        send.rerender_rich_text_session.assert_called_once()
        send._append_response.assert_not_called()

    def test_finalize_on_error_flushes_leftover_without_rerender(self):
        send = _make_plain_send_listener()
        send.rerender_rich_text_session = MagicMock(return_value=True)
        send._terminal_status = "Error"
        send._append_response = MagicMock()
        _turn(send).stripper.feed("tail <i")
        finalize_sidebar_assistant_response(send)
        send.rerender_rich_text_session.assert_not_called()
        send._append_response.assert_called_once_with("<i", role="assistant")
        assert _turn(send).stripper is None

    def test_finalize_rerenders_when_stopped_status(self):
        send = _make_plain_send_listener()
        send.rerender_rich_text_session = MagicMock()
        send._terminal_status = "Stopped"
        _turn(send).stripper = None
        finalize_sidebar_assistant_response(send)
        send.rerender_rich_text_session.assert_called_once()

    def test_finalize_stopped_calls_render_session_messages(self):
        """When stopped, finalize must call render_session_messages to paint the closed turn."""
        send = _make_plain_send_listener()
        send.session = MagicMock()
        msgs: list[dict[str, str]] = []
        send.session.messages = msgs
        _turn(send).session = send.session
        _turn(send).messages = msgs
        send.render_session_messages = MagicMock()
        send.stop_requested = True
        _turn(send).stripper = None
        finalize_sidebar_assistant_response(send, allow_rerender=False)
        send.render_session_messages.assert_called_once_with(send.session)
