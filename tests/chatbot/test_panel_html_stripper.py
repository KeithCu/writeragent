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
from plugin.framework.html_stripper import StreamingHTMLStripper


def _make_plain_send_listener():
    with patch.object(SendButtonListener, "__init__", lambda self, *a, **k: None):
        send = SendButtonListener.__new__(SendButtonListener)
        send.rich_text_widget = None
        send.response_control = MagicMock()
        send.response_control.getModel.return_value = MagicMock()
        send._plain_text_stripper = StreamingHTMLStripper()
        send._should_auto_scroll = MagicMock(return_value=True)
        send._scroll_response_to_bottom = MagicMock()
        send.queue_executor = MagicMock()
        return send


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
