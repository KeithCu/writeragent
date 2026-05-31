# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for RichTextControl append path in SendButtonListener."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch


def _make_send_listener():
    from plugin.chatbot.panel import SendButtonListener

    with patch.object(SendButtonListener, "__init__", lambda self, *a, **k: None):
        send = SendButtonListener.__new__(SendButtonListener)
        send.rich_text_widget = MagicMock()
        send.rich_text_widget.get_text_length.return_value = 500
        send.queue_executor = MagicMock()
        send._record_assistant_start = False
        send._assistant_stream_start_len = None
        send.session = MagicMock()
        return send


class TestRichAppendResponse:
    def test_record_assistant_start_sets_stream_start_len(self):
        send = _make_send_listener()
        send._record_assistant_start = True
        with patch("plugin.chatbot.panel.threading.current_thread", return_value=threading.main_thread()):
            send._append_response("<p>Report</p>", role="assistant")

        assert send._assistant_stream_start_len == 500
        send.rich_text_widget.get_text_length.assert_called_once()
        send.rich_text_widget.append_assistant_stream_chunk.assert_called_once_with(
            "Report",
            auto_scroll=True,
        )

    def test_main_thread_calls_widget_directly(self):
        send = _make_send_listener()
        with patch("plugin.chatbot.panel.threading.current_thread", return_value=threading.main_thread()):
            send._append_response("search step", role="assistant")

        send.queue_executor.post.assert_not_called()
        send.rich_text_widget.append_assistant_stream_chunk.assert_called_once()

    def test_worker_thread_posts_to_queue_executor(self):
        send = _make_send_listener()
        worker = threading.Thread(target=lambda: None)
        with patch("plugin.chatbot.panel.threading.current_thread", return_value=worker):
            send._append_response("search step", role="assistant")

        send.queue_executor.post.assert_called_once()
        send.rich_text_widget.append_assistant_stream_chunk.assert_not_called()

    def test_web_research_final_answer_start_len_after_search_steps(self):
        """Final answer must re-mark stream start after search chunks, not after user message."""
        send = _make_send_listener()
        send._assistant_stream_start_len = 100  # after user message (from on_after_insert)
        send.rich_text_widget.get_text_length.return_value = 500  # after search steps
        send._record_assistant_start = True

        with patch("plugin.chatbot.panel.threading.current_thread", return_value=threading.main_thread()):
            send._append_response("<p>Report</p>", role="assistant")

        assert send._assistant_stream_start_len == 500

        send.rich_text_widget.rerender_last_assistant_if_html = MagicMock()
        send.session.messages = [{"role": "assistant", "content": "<p>Report</p>"}]
        send.rich_text_widget.rerender_last_assistant_if_html(
            send.session,
            send._assistant_stream_start_len,
        )
        send.rich_text_widget.rerender_last_assistant_if_html.assert_called_once_with(
            send.session,
            500,
        )
