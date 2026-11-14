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

import pytest

from plugin.chatbot.tool_loop_actions import finalize_sidebar_assistant_response, assign_send_outcome
from plugin.framework.deal_shim import DEAL_MAX_HTML_CHUNK
from plugin.framework.html_stripper import StreamingHTMLStripper


def _make_plain_send_listener():
    from plugin.chatbot.tool_loop_actions import begin_send_turn
    from tests.conftest import make_mock_send_button_listener

    send = make_mock_send_button_listener()
    send.rich_text_widget = None
    send.response_control = MagicMock()
    send.response_control.getModel.return_value = MagicMock()
    send.session = MagicMock()
    send.session.messages = []
    send._scroll_response_to_bottom = MagicMock()
    begin_send_turn(send)
    return send


def _turn(send):
    from plugin.chatbot.tool_loop_actions import current_turn

    return current_turn(send)


class TestPanelHTMLStripper:
    @pytest.mark.parametrize(
        "chunks, role, expected",
        [
            pytest.param(
                ["Hello <st", "rong>World</str", "ong>!"],
                "assistant",
                "Current: Hello World!",
                id="streaming_split_tags",
            ),
            pytest.param(
                ["<p>Hello</p>"],
                "user",
                "Current: Hello",
                id="direct_non_assistant_html",
            ),
            pytest.param(
                ["<p>" + ("y" * (DEAL_MAX_HTML_CHUNK + 1)) + "</p>"],
                "assistant",
                "Current: " + ("y" * (DEAL_MAX_HTML_CHUNK + 1)),
                id="long_assistant_chunk_over_deal_limit",
            ),
        ],
    )
    def test_append_response_plain_text_strips_tags(self, chunks, role, expected):
        send = _make_plain_send_listener()
        text_buf = ["Current: "]

        with (
            patch("plugin.chatbot.dialogs.get_control_text", side_effect=lambda _ctrl: text_buf[0]),
            patch("plugin.chatbot.dialogs.set_control_text", side_effect=lambda _ctrl, val: text_buf.__setitem__(0, val)),
            patch("plugin.chatbot.panel.threading.current_thread", return_value=threading.main_thread()),
        ):
            for chunk in chunks:
                send._append_response(chunk, role=role)

        assert text_buf[0] == expected

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
        assign_send_outcome(send, "error")
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
        assign_send_outcome(send, "error")
        send._append_response = MagicMock()
        _turn(send).stripper.feed("tail <i")
        finalize_sidebar_assistant_response(send)
        send.rerender_rich_text_session.assert_not_called()
        send._append_response.assert_called_once_with("<i", role="assistant")
        assert _turn(send).stripper is None

    def test_finalize_rerenders_when_stopped_status(self):
        send = _make_plain_send_listener()
        send.rerender_rich_text_session = MagicMock()
        assign_send_outcome(send, "stopped")
        _turn(send).stripper = None
        finalize_sidebar_assistant_response(send)
        send.rerender_rich_text_session.assert_called_once()
