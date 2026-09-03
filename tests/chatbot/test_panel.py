# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for sidebar query Enter-to-send key classification and send dispose."""

import sys
import unittest
from unittest.mock import MagicMock, patch

from plugin.framework.config_schema import _get_schema_default
from plugin.chatbot.panel import (
    QueryKeyListener,
    SendButtonListener,
    notify_stop_mouse_entered,
    notify_stop_mouse_pressed,
    query_enter_triggers_primary_send,
)
from plugin.chatbot.send_state import SendButtonState, SendEvent, SendEventKind
from plugin.chatbot.sidebar_state import SidebarCompositeState
from plugin.chatbot.audio_recorder_state import AudioRecorderState
from plugin.framework.queue_executor import SendCancellation


class QueryEnterSendTests(unittest.TestCase):
    def test_enter_without_shift_triggers(self):
        self.assertTrue(query_enter_triggers_primary_send(1280, 0))

    def test_shift_enter_does_not_trigger(self):
        self.assertFalse(query_enter_triggers_primary_send(1280, 1))

    def test_shift_with_other_modifiers(self):
        self.assertFalse(query_enter_triggers_primary_send(1280, 1 | 2))

    def test_non_return_key_ignored(self):
        self.assertFalse(query_enter_triggers_primary_send(1279, 0))

    def test_doc_yaml_default_enter_sends_true(self):
        self.assertIs(_get_schema_default("doc.chat_enter_key_sends_message"), True)


def _make_send_listener() -> SendButtonListener:
    session = MagicMock()
    session.messages = [{"role": "system", "content": "test"}]
    return SendButtonListener(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        session,
    )


class SendDisposeTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(sys.modules, {"plugin.main": MagicMock()}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disposing_cancels_in_flight_send(self) -> None:
        listener = _make_send_listener()
        scope = SendCancellation()
        listener._send_cancellation = scope
        checker = listener.resolve_stop_checker()
        self.assertFalse(checker())
        listener.disposing(None)
        self.assertTrue(scope.is_cancelled())
        self.assertTrue(checker())
        self.assertTrue(listener._stop_requested_fallback)
        self.assertIsNone(listener.ctx)
        self.assertIsNone(listener.panel)

    def test_disposing_without_active_send_still_latches_stop(self) -> None:
        listener = _make_send_listener()
        listener._send_cancellation = None
        listener.disposing(None)
        self.assertTrue(listener._stop_requested_fallback)
        self.assertIsNone(listener.ctx)
        self.assertIsNone(listener.panel)

    def test_start_send_posts_drain_off_action_listener(self) -> None:
        """Send must return from actionPerformed before drain so GTK delivers Stop."""
        listener = _make_send_listener()
        posted: list = []
        listener.queue_executor.post = lambda fn, *a, **k: posted.append(fn)
        listener._do_send = MagicMock()
        listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
        listener.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
        self.assertEqual(len(posted), 1)
        listener._do_send.assert_not_called()
        self.assertTrue(listener.sidebar_state.send.is_busy)
        self.assertIsNotNone(listener._send_cancellation)
        posted[0]()
        listener._do_send.assert_called_once()

    def test_stop_before_deferred_drain_skips_do_send(self) -> None:
        listener = _make_send_listener()
        posted: list = []
        listener.queue_executor.post = lambda fn, *a, **k: posted.append(fn)
        listener._do_send = MagicMock()
        listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
        listener.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
        listener.dispatch(SendEvent(SendEventKind.STOP_CLICKED))
        self.assertTrue(listener._stop_requested_fallback)
        self.assertTrue(listener._send_cancellation.is_cancelled())
        posted[0]()
        listener._do_send.assert_not_called()
        self.assertFalse(listener.sidebar_state.send.is_busy)

    def test_stop_before_drain_does_not_drop_posted_closer(self) -> None:
        """Stop must not cancel_pending_work the posted drain (Send would stay busy)."""
        from plugin.framework import queue_executor as qe

        listener = _make_send_listener()
        listener._do_send = MagicMock()
        qe.set_force_marshal_mode(True)
        try:
            with patch.object(listener.queue_executor, "_poke_main_thread", lambda: None):
                listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
                listener.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
                self.assertTrue(listener.sidebar_state.send.is_busy)
                self.assertFalse(listener.queue_executor._work_queue.empty())
                listener.dispatch(SendEvent(SendEventKind.STOP_CLICKED))
                self.assertFalse(
                    listener.queue_executor._work_queue.empty(),
                    "cancel_pending_work dropped _run_send_drain",
                )
                listener.queue_executor.process_queue()
            listener._do_send.assert_not_called()
            self.assertFalse(listener.sidebar_state.send.is_busy)
        finally:
            qe.set_force_marshal_mode(False)
            while not listener.queue_executor._work_queue.empty():
                listener.queue_executor.process_queue()

    def test_record_start_failure_does_not_leave_stop_rec(self) -> None:
        """Nested ERROR during RECORD_CLICKED used to restore Stop Rec after resetting is_recording."""
        listener = _make_send_listener()
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(False, False, False, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
        send_model = MagicMock()
        send_model.Label = "Record"
        listener.send_control.getModel.return_value = send_model
        listener.stop_control.getModel.return_value = MagicMock()
        listener.audio_recorder = MagicMock()
        listener.audio_recorder.start_recording.side_effect = RuntimeError("no microphone")
        listener._append_response = MagicMock()
        listener.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))
        self.assertFalse(listener.sidebar_state.send.is_recording)
        self.assertFalse(listener.sidebar_state.send.is_busy)
        self.assertNotEqual(send_model.Label, "Stop Rec")

    def test_stop_mouse_pressed_cancels_busy_send(self) -> None:
        listener = _make_send_listener()
        listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
        listener.queue_executor.post = lambda fn, *a, **k: None
        listener.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
        self.assertTrue(listener.sidebar_state.send.is_busy)
        self.assertFalse(listener._stop_requested_fallback)
        notify_stop_mouse_pressed(listener)
        self.assertTrue(listener._stop_requested_fallback)
        self.assertTrue(listener._send_cancellation.is_cancelled())

    def test_stop_mouse_pressed_idle_is_noop(self) -> None:
        listener = _make_send_listener()
        notify_stop_mouse_pressed(listener)
        self.assertFalse(listener._stop_requested_fallback)
        self.assertFalse(listener.sidebar_state.send.is_busy)

    def test_stop_mouse_pressed_skips_web_search_approval(self) -> None:
        listener = _make_send_listener()
        listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
        listener.queue_executor.post = lambda fn, *a, **k: None
        listener.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
        listener._approval_event = object()
        notify_stop_mouse_pressed(listener)
        self.assertFalse(listener._stop_requested_fallback)

    def test_stop_mouse_entered_stops_query_restore(self) -> None:
        from plugin.framework import uno_context as uc

        query = MagicMock()
        uc.set_default_focus_restore(query)
        uc.note_user_wants_query()
        try:
            notify_stop_mouse_entered()
            uc.restore_query_if_user_still_there()
            query.setFocus.assert_not_called()
        finally:
            uc.set_default_focus_restore(None)
            uc._restore_query_after_scroll = True


class _MockDisposedException(Exception):
    """Name must include DisposedException so is_disposed_exception matches."""


class _ConsumeEvent:
    def __init__(self) -> None:
        object.__setattr__(self, "KeyCode", 1280)
        object.__setattr__(self, "Modifiers", 0)
        object.__setattr__(self, "Consume", False)

    def __setattr__(self, name, value):
        if name == "Consume":
            raise _MockDisposedException("event disposed")
        object.__setattr__(self, name, value)


class QueryKeyListenerDisposeTests(unittest.TestCase):
    def test_consume_disposed_still_sends(self) -> None:
        send_listener = MagicMock()
        send_model = MagicMock()
        send_model.Enabled = True
        send_listener.send_control.getModel.return_value = send_model
        listener = QueryKeyListener(send_listener)
        event = _ConsumeEvent()
        with patch("plugin.framework.config.get_config_bool", return_value=True):
            listener.on_key_pressed(event)
        send_listener.on_action_performed.assert_called_once_with(event)

    def test_open_slash_popup_enter_does_not_send(self) -> None:
        send_listener = MagicMock()
        send_listener.slash_popup.handle_key.return_value = True
        listener = QueryKeyListener(send_listener)
        event = type("KeyEvent", (), {"KeyCode": 1280, "Modifiers": 0, "Consume": False})()
        listener.on_key_pressed(event)
        send_listener.on_action_performed.assert_not_called()
        self.assertTrue(event.Consume)


if __name__ == "__main__":
    unittest.main()
