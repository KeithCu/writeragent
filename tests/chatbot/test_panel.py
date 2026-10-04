# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for sidebar query Enter-to-send key classification and send dispose."""

import logging
import sys
from typing import Any
from unittest.mock import MagicMock, patch

from plugin.framework.config_schema import _get_schema_default
from plugin.chatbot.panel import (
    ClearButtonListener,
    QueryKeyListener,
    QueryTextListener,
    SendButtonListener,
    StopButtonListener,
    notify_record_mouse_pressed,
    notify_record_mouse_released,
    notify_stop_mouse_entered,
    notify_stop_mouse_pressed,
    query_enter_triggers_primary_send,
)
from plugin.chatbot.record_gesture import HANDS_FREE_STATUS, RecordGesture, hands_free_status_text
from plugin.chatbot.send_state import SendButtonState, SendEvent, SendEventKind
from plugin.chatbot.sidebar_state import SidebarCompositeState
from plugin.chatbot.audio_recorder_state import AudioRecorderState
from plugin.framework.queue_executor import SendCancellation


class TestQueryEnterSend:
    def test_enter_without_shift_triggers(self):
        assert (query_enter_triggers_primary_send(1280, 0))

    def test_shift_enter_does_not_trigger(self):
        assert not (query_enter_triggers_primary_send(1280, 1))

    def test_shift_with_other_modifiers(self):
        assert not (query_enter_triggers_primary_send(1280, 1 | 2))

    def test_non_return_key_ignored(self):
        assert not (query_enter_triggers_primary_send(1279, 0))

    def test_doc_yaml_default_enter_sends_true(self):
        assert (_get_schema_default("doc.chat_enter_key_sends_message")) is (True)


def _live_bus_callbacks(bus: Any, event: str) -> list[Any]:
    subs = bus._subscribers.get(event) or []
    live: list[Any] = []
    for callback, is_weak in subs:
        resolved = bus._resolve(callback, is_weak)
        if resolved is not None:
            live.append(resolved)
    return live


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


class TestGrammarStatusDocType:
    def test_non_writer_sidebar_ignores_grammar_bus(self) -> None:
        listener = _make_send_listener()
        listener.cached_doc_type = "calc"
        listener._on_grammar_status(phase="request", preview="Hello", length=5, result="LLM request")
        listener.status_control.setText.assert_not_called()

    def test_writer_sidebar_shows_grammar_status(self) -> None:
        listener = _make_send_listener()
        listener.cached_doc_type = "writer"
        listener._on_grammar_status(phase="failed", preview="sample", length=6, result="API error")
        listener.status_control.setText.assert_called_once_with("Grammar: failed 'sample' len 6: API error")


class TestSidebarModeFinishApplies:
    def _listener(self) -> MagicMock:
        from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

        listener = MagicMock()
        listener.sidebar_mode_flags = SidebarModeFlags(include_brainstorming=True, include_writing_plan=True, include_ppt_master=True)
        listener.chat_mode_selector = MagicMock()
        listener._apply_sidebar_mode_fn = MagicMock()
        listener._brainstorming_topic = "Kitchen remodel"
        listener._in_brainstorming_mode = True
        return listener

    def test_saved_spec_keeps_topic_and_applies_writing_plan(self) -> None:
        from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_WRITING_PLAN

        listener = self._listener()
        with patch("plugin.chatbot.chat_sidebar_mode.set_selector_mode_with_flags") as mock_set:
            SendButtonListener.on_brainstorming_session_finished(listener, spec_saved=True)
        assert listener._writing_plan_topic == "Implement the saved spec: Kitchen remodel"
        assert listener._brainstorming_topic == ""
        assert mock_set.call_args[0][1] == CHAT_MODE_WRITING_PLAN
        listener._apply_sidebar_mode_fn.assert_called_once_with(CHAT_MODE_WRITING_PLAN)

    def test_brainstorm_without_spec_applies_chat(self) -> None:
        from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_CHAT

        listener = self._listener()
        with patch("plugin.chatbot.chat_sidebar_mode.set_selector_mode_with_flags"):
            SendButtonListener.on_brainstorming_session_finished(listener, spec_saved=False)
        listener._apply_sidebar_mode_fn.assert_called_once_with(CHAT_MODE_CHAT)

    def test_writing_plan_and_ppt_finish_apply_chat(self) -> None:
        from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_CHAT

        for method in (
            SendButtonListener.on_writing_plan_session_finished,
            SendButtonListener.on_ppt_master_session_finished,
        ):
            listener = self._listener()
            with patch("plugin.chatbot.chat_sidebar_mode.set_selector_mode_with_flags"):
                method(listener)
            listener._apply_sidebar_mode_fn.assert_called_once_with(CHAT_MODE_CHAT)


class TestSendDispose:
    def setup_method(self) -> None:
        self._modules_patcher = patch.dict(sys.modules, {"plugin.main": MagicMock()}, clear=False)
        self._modules_patcher.start()

    def teardown_method(self) -> None:
        self._modules_patcher.stop()

    def test_disposing_cancels_in_flight_send(self) -> None:
        listener = _make_send_listener()
        scope = SendCancellation()
        listener._send_cancellation = scope
        checker = listener.resolve_stop_checker()
        assert not (checker())
        listener.disposing(None)
        assert (scope.is_cancelled())
        assert (checker())
        assert (listener._stop_requested_fallback)
        assert (listener.ctx) is None
        assert (listener.panel) is None

    def test_disposing_without_active_send_still_latches_stop(self) -> None:
        listener = _make_send_listener()
        listener._send_cancellation = None
        listener.disposing(None)
        assert (listener._stop_requested_fallback)
        assert (listener.ctx) is None
        assert (listener.panel) is None

    def test_disposing_stops_speech(self) -> None:
        listener = _make_send_listener()
        with patch("plugin.audio.tts_service.stop_speech") as mock_stop_speech:
            listener.disposing(None)
            mock_stop_speech.assert_called_once()

    def test_disposing_stops_the_microphone_and_leaves_stop_rec(self) -> None:
        listener = _make_send_listener()
        listener.audio_recorder = MagicMock()
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(False, True, False, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="recording"),
        )
        send_model = MagicMock()
        send_model.Label = "Stop Rec"
        listener.send_control.getModel.return_value = send_model
        listener.disposing(None)
        assert listener._panel_teardown is True
        listener.audio_recorder.cleanup.assert_called_once()
        assert listener.sidebar_state.send.is_recording is False
        assert send_model.Label != "Stop Rec"
        assert listener.ctx is None

    def test_drain_after_dispose_skips_status_tts_and_sticky(self) -> None:
        listener = _make_send_listener()
        listener._do_send = MagicMock()
        listener.session.messages = [{"role": "assistant", "content": "hello"}]
        listener._terminal_status = "Ready"
        listener._panel_teardown = True
        listener._sticky_restart_pending = True
        listener._record_gesture = RecordGesture(sticky=True)
        listener.audio_recorder = MagicMock()
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(False, False, False, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
        listener.status_control.setText.reset_mock()
        with (
            patch("plugin.audio.tts_service.speak_text_async") as speak,
            patch("plugin.audio.tts_service.is_speaking", return_value=False),
            patch("plugin.framework.config.get_config_bool_safe", return_value=True),
        ):
            listener._run_send_drain()
        speak.assert_not_called()
        listener.audio_recorder.start_recording.assert_not_called()
        listener.status_control.setText.assert_not_called()

    def test_peer_drain_after_dispose_skips_status(self) -> None:
        listener = _make_send_listener()
        listener._panel_teardown = True
        listener._do_send_extracted_peer = MagicMock()
        listener._extracted_peer_query = "hello"
        listener._sticky_restart_pending = True
        listener._record_gesture = RecordGesture(sticky=True)
        listener.audio_recorder = MagicMock()
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(False, False, False, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
        listener.status_control.setText.reset_mock()
        listener._run_extracted_peer_drain()
        listener.status_control.setText.assert_not_called()
        listener.audio_recorder.start_recording.assert_not_called()

    def test_disposing_unsubscribes_mcp_on_the_services_bus(self) -> None:
        from plugin.framework.event_bus import EventBus

        local_bus = EventBus()
        other_bus = EventBus()
        services = MagicMock()
        services.events = local_bus
        tools = MagicMock()
        tools._services = services
        with (
            patch("plugin.main.get_tools", return_value=tools),
            patch("plugin.framework.event_bus.global_event_bus", other_bus),
        ):
            listener = _make_send_listener()
            assert _live_bus_callbacks(local_bus, "mcp:request")
            assert _live_bus_callbacks(local_bus, "mcp:result")
            assert not _live_bus_callbacks(other_bus, "mcp:result")
            assert _live_bus_callbacks(other_bus, "grammar:status")
            listener.disposing(None)
            assert not _live_bus_callbacks(local_bus, "mcp:request")
            assert not _live_bus_callbacks(local_bus, "mcp:result")
            assert not _live_bus_callbacks(other_bus, "grammar:status")
        assert listener._mcp_event_bus is None

    def test_disposing_unsubscribes_when_mcp_and_grammar_share_a_bus(self) -> None:
        from plugin.framework.event_bus import EventBus

        shared = EventBus()
        services = MagicMock()
        services.events = shared
        tools = MagicMock()
        tools._services = services
        with (
            patch("plugin.main.get_tools", return_value=tools),
            patch("plugin.framework.event_bus.global_event_bus", shared),
        ):
            listener = _make_send_listener()
            listener.disposing(None)
            assert not _live_bus_callbacks(shared, "mcp:request")
            assert not _live_bus_callbacks(shared, "mcp:result")
            assert not _live_bus_callbacks(shared, "grammar:status")

    def test_disposing_clears_audio_auto_stop_callbacks(self) -> None:
        listener = _make_send_listener()
        listener.audio_recorder = MagicMock()
        listener.disposing(None)
        listener.audio_recorder.set_auto_stop_callbacks.assert_called_once_with(
            on_auto_stop=None,
            on_silence_progress=None,
            on_error=None,
        )

    def test_mcp_result_after_teardown_does_not_append(self) -> None:
        listener = _make_send_listener()
        listener._panel_teardown = True
        listener._append_response = MagicMock()
        with patch("plugin.framework.queue_executor.post_to_main_thread") as post:
            listener._on_mcp_result(tool="echo", result_snippet="hi")
        post.assert_not_called()
        listener._append_response.assert_not_called()

    def test_mcp_result_when_ctx_cleared_does_not_append(self) -> None:
        listener = _make_send_listener()
        listener.ctx = None
        listener._append_response = MagicMock()
        listener._on_mcp_result(tool="echo", result_snippet="hi")
        listener._append_response.assert_not_called()

    def test_mcp_result_queued_before_teardown_skips_ui(self) -> None:
        listener = _make_send_listener()
        listener._append_response = MagicMock()
        posted: list[Any] = []

        def _capture(fn: Any, *args: Any) -> None:
            posted.append(fn)

        with (
            patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
            patch("plugin.framework.thread_guard.get_background_task_name", return_value="mcp"),
            patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_capture),
        ):
            listener._on_mcp_result(tool="echo", result_snippet="hi")
        assert len(posted) == 1
        listener._panel_teardown = True
        posted[0]()
        listener._append_response.assert_not_called()

    def test_mcp_result_on_live_panel_appends(self) -> None:
        listener = _make_send_listener()
        listener._append_response = MagicMock()
        with (
            patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
            patch("plugin.framework.thread_guard.get_background_task_name", return_value=None),
        ):
            listener._on_mcp_result(tool="echo", result_snippet="hi")
        listener._append_response.assert_called_once()
        assert str(listener._append_response.call_args[0][0]).startswith("[MCP Result]")

    def test_audio_callbacks_after_teardown_do_not_touch_ui(self) -> None:
        listener = _make_send_listener()
        listener._panel_teardown = True
        listener.dispatch = MagicMock()
        listener._append_response = MagicMock()
        listener.status_control.setText.reset_mock()
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(False, True, False, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="recording"),
        )
        listener._on_audio_auto_stop()
        listener._on_audio_recorder_error("boom")
        listener._on_audio_silence_progress(100)
        listener.dispatch.assert_not_called()
        listener._append_response.assert_not_called()
        listener.status_control.setText.assert_not_called()

    def test_drain_during_dispose_keeps_cancelled_scope(self) -> None:
        listener = _make_send_listener()

        def _dispose_mid_send() -> None:
            listener.disposing(None)

        listener._do_send = _dispose_mid_send
        listener._run_send_drain()
        scope = listener._send_cancellation
        assert scope is not None
        assert scope.is_cancelled()

    def test_live_drain_clears_send_cancellation(self) -> None:
        listener = _make_send_listener()
        listener._do_send = MagicMock()
        listener._terminal_status = "Ready"
        listener._panel_teardown = False
        with (
            patch("plugin.audio.tts_service.speak_text_async"),
            patch("plugin.audio.tts_service.is_speaking", return_value=False),
            patch("plugin.framework.config.get_config_bool_safe", return_value=False),
            patch("plugin.chatbot.dialogs.get_control_text", return_value=""),
        ):
            listener._run_send_drain()
        assert listener._send_cancellation is None

    def test_send_completed_rereads_ask_box(self) -> None:
        listener = _make_send_listener()
        listener._do_send = MagicMock()
        listener._terminal_status = "Ready"
        listener._panel_teardown = False
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(True, False, True, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
        with (
            patch("plugin.audio.tts_service.speak_text_async"),
            patch("plugin.audio.tts_service.is_speaking", return_value=False),
            patch("plugin.framework.config.get_config_bool_safe", return_value=False),
            patch("plugin.chatbot.dialogs.get_control_text", return_value="still typing"),
        ):
            listener._run_send_drain()
        assert listener.sidebar_state.send.is_busy is False
        assert listener.sidebar_state.send.has_text is True

    def test_clear_while_busy_drops_the_in_flight_reply(self) -> None:
        from plugin.chatbot.panel import ChatSession
        from plugin.chatbot.tool_loop_actions import begin_send_turn, persist_assistant_on_turn

        listener = _make_send_listener()
        session = ChatSession("system prompt")
        session.add_user_message("question")
        listener.session = session
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(True, False, True, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
        begin_send_turn(listener, "chat")
        clear = ClearButtonListener(session, None, None, send_listener=listener)
        with patch("plugin.audio.tts_service.stop_speech"):
            clear.on_action_performed(MagicMock())
        assert listener._stop_requested_fallback is True
        persist_assistant_on_turn(listener, content="late reply")
        assert all(message.get("role") != "assistant" for message in session.messages)
        assert session.compaction is None
        with patch("plugin.chatbot.dialogs.set_control_text") as set_text:
            listener._append_response("painted after clear")
        set_text.assert_not_called()

    def test_swapped_session_does_not_receive_the_in_flight_reply(self) -> None:
        from plugin.chatbot.panel import ChatSession
        from plugin.chatbot.tool_loop_actions import begin_send_turn, persist_assistant_on_turn, session_for_turn

        listener = _make_send_listener()
        original = ChatSession("system prompt")
        original.add_user_message("question")
        other = ChatSession("other mode")
        listener.session = original
        begin_send_turn(listener, "chat")
        listener.set_session(other)
        assert session_for_turn(listener) is original
        persist_assistant_on_turn(listener, content="answer")
        assert any(message.get("content") == "answer" for message in original.messages)
        assert all(message.get("role") != "assistant" for message in other.messages)

    def test_clear_during_recording_stops_the_microphone(self) -> None:
        listener = _make_send_listener()
        listener.audio_recorder = MagicMock()
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(False, True, False, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="recording"),
        )
        send_model = MagicMock()
        send_model.Label = "Stop Rec"
        listener.send_control.getModel.return_value = send_model
        listener.response_control = None
        listener.rich_text_widget = None
        clear = ClearButtonListener(listener.session, None, listener.status_control, send_listener=listener)
        with patch("plugin.audio.tts_service.stop_speech"):
            clear.on_action_performed(MagicMock())
        listener.audio_recorder.cleanup.assert_called_once()
        assert listener.sidebar_state.send.is_recording is False
        assert send_model.Label != "Stop Rec"
        listener.session.clear.assert_called_once()

    def test_start_send_posts_drain_off_action_listener(self) -> None:
        """Send must return from actionPerformed before drain so GTK delivers Stop."""
        listener = _make_send_listener()
        posted: list = []
        listener.queue_executor.post = lambda fn, *a, **k: posted.append(fn)
        listener._do_send = MagicMock()
        listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
        listener.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
        assert (len(posted)) == (1)
        listener._do_send.assert_not_called()
        assert (listener.sidebar_state.send.is_busy)
        assert (listener._send_cancellation) is not None
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
        assert (listener._stop_requested_fallback)
        assert (listener._send_cancellation.is_cancelled())
        posted[0]()
        listener._do_send.assert_not_called()
        assert not (listener.sidebar_state.send.is_busy)

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
                assert (listener.sidebar_state.send.is_busy)
                assert not (listener.queue_executor._work_queue.empty())
                listener.dispatch(SendEvent(SendEventKind.STOP_CLICKED))
                assert not (listener.queue_executor._work_queue.empty()), "cancel_pending_work dropped _run_send_drain"
                listener.queue_executor.process_queue()
            listener._do_send.assert_not_called()
            assert not (listener.sidebar_state.send.is_busy)
        finally:
            qe.set_force_marshal_mode(False)
            while not listener.queue_executor._work_queue.empty():
                listener.queue_executor.process_queue()

    def test_do_send_errors_when_stop_rec_has_no_wav(self) -> None:
        """Stop Rec promises audio. A missing WAV must not return with an empty status."""
        listener = _make_send_listener()
        listener.cached_doc_type = "writer"
        listener.audio_wav_path = None
        listener.query_control = None
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(True, False, False, True, True),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
        listener._append_response = MagicMock()
        with patch("plugin.framework.uno_context.get_document_from_frame", return_value=MagicMock()):
            listener._do_send()
        assert listener._terminal_status == "Error"
        text = listener._append_response.call_args[0][0]
        assert "Audio error" in text
        assert "sound file" in text

    def test_do_send_empty_query_without_audio_stays_quiet(self) -> None:
        listener = _make_send_listener()
        listener.cached_doc_type = "writer"
        listener.audio_wav_path = None
        listener.query_control = None
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(True, False, False, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
        listener._append_response = MagicMock()
        with patch("plugin.framework.uno_context.get_document_from_frame", return_value=MagicMock()):
            listener._do_send()
        assert listener._terminal_status == ""
        listener._append_response.assert_not_called()

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
        assert not (listener.sidebar_state.send.is_recording)
        assert not (listener.sidebar_state.send.is_busy)
        assert (send_model.Label) != ("Stop Rec")

    def test_stop_mouse_pressed_cancels_busy_send(self) -> None:
        listener = _make_send_listener()
        listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
        listener.queue_executor.post = lambda fn, *a, **k: None
        listener.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
        assert (listener.sidebar_state.send.is_busy)
        assert not (listener._stop_requested_fallback)
        notify_stop_mouse_pressed(listener)
        assert (listener._stop_requested_fallback)
        assert (listener._send_cancellation.is_cancelled())

    def test_stop_mouse_pressed_idle_is_noop(self) -> None:
        listener = _make_send_listener()
        notify_stop_mouse_pressed(listener)
        assert not (listener._stop_requested_fallback)
        assert not (listener.sidebar_state.send.is_busy)

    def test_stop_mouse_pressed_skips_web_search_approval(self) -> None:
        listener = _make_send_listener()
        listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
        listener.queue_executor.post = lambda fn, *a, **k: None
        listener.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
        listener._approval_event = object()
        notify_stop_mouse_pressed(listener)
        assert not (listener._stop_requested_fallback)

    def test_stop_mouse_entered_stops_query_restore(self) -> None:
        from plugin.framework.frame_session import FrameSession

        listener = _make_send_listener()
        session = FrameSession(listener.frame, "doc-a")
        query = MagicMock()
        session.set_focus_pin(query)
        session.note_user_wants_query()
        listener.frame_session = session
        other = FrameSession(MagicMock(), "doc-b")
        other_query = MagicMock()
        other.set_focus_pin(other_query)
        other.note_user_wants_query()
        notify_stop_mouse_entered(listener)
        session.restore_focus()
        other.restore_focus()
        query.setFocus.assert_not_called()
        other_query.setFocus.assert_called_once()


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


def _query_text_event(text: str) -> Any:
    source = MagicMock()
    source.Model.Text = text
    event = MagicMock()
    event.Source = source
    return event


class TestSlashOverlayParked:
    """ENABLE_SLASH stays false: Ask listeners remain, overlay work does not run."""

    def test_text_change_skips_overlay_and_still_dispatches(self, caplog) -> None:
        send_listener = MagicMock()
        listener = QueryTextListener(send_listener)
        with patch("plugin.chatbot.slash_popup.ENABLE_SLASH", False), caplog.at_level(logging.DEBUG):
            listener.on_text_changed(_query_text_event("/he"))
        send_listener.slash_popup.on_query_text.assert_not_called()
        send_listener.dispatch.assert_called_once()
        event = send_listener.dispatch.call_args[0][0]
        assert event.kind == SendEventKind.TEXT_UPDATED
        assert event.data == {"has_text": True}
        assert not any("[SLASH-OV]" in r.message and r.levelno >= logging.INFO for r in caplog.records)
        assert not any("[SLASH-OV]" in r.message for r in caplog.records)

    def test_enabled_slash_prefix_calls_overlay_and_skips_dispatch(self, caplog) -> None:
        send_listener = MagicMock()
        listener = QueryTextListener(send_listener)
        with patch("plugin.chatbot.slash_popup.ENABLE_SLASH", True), \
             patch("plugin.chatbot.slash_popup.SLASH_OV_VERBOSE_DEBUG", False), \
             caplog.at_level(logging.DEBUG):
            listener.on_text_changed(_query_text_event("/he"))
        send_listener.slash_popup.on_query_text.assert_called_once_with("/he")
        send_listener.dispatch.assert_not_called()
        assert not any("[SLASH-OV]" in r.message and r.levelno >= logging.INFO for r in caplog.records)

    def test_enabled_plain_text_dispatches(self) -> None:
        send_listener = MagicMock()
        listener = QueryTextListener(send_listener)
        with patch("plugin.chatbot.slash_popup.ENABLE_SLASH", True):
            listener.on_text_changed(_query_text_event("hello"))
        send_listener.slash_popup.on_query_text.assert_called_once_with("hello")
        send_listener.dispatch.assert_called_once()
        event = send_listener.dispatch.call_args[0][0]
        assert event.kind == SendEventKind.TEXT_UPDATED
        assert event.data == {"has_text": True}

    def test_text_change_during_teardown_does_not_dispatch(self) -> None:
        send_listener = MagicMock()
        send_listener._panel_teardown = True
        listener = QueryTextListener(send_listener)
        listener.on_text_changed(_query_text_event("hello"))
        send_listener.dispatch.assert_not_called()

    def test_disabled_enter_skips_handle_key_and_still_sends(self, caplog) -> None:
        send_listener = MagicMock()
        send_listener.slash_popup.handle_key.return_value = True
        send_model = MagicMock()
        send_model.Enabled = True
        send_listener.send_control.getModel.return_value = send_model
        listener = QueryKeyListener(send_listener)
        event = type("KeyEvent", (), {"KeyCode": 1280, "Modifiers": 0, "Consume": False})()
        with patch("plugin.chatbot.slash_popup.ENABLE_SLASH", False), \
             patch("plugin.framework.config.get_config_bool", return_value=True), \
             caplog.at_level(logging.DEBUG):
            listener.on_key_pressed(event)
        send_listener.slash_popup.handle_key.assert_not_called()
        send_listener.on_action_performed.assert_called_once_with(event)
        assert not any("[SLASH-OV]" in r.message for r in caplog.records)


class TestQueryKeyListenerDispose:
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
        with patch("plugin.chatbot.slash_popup.ENABLE_SLASH", True):
            listener.on_key_pressed(event)
        send_listener.slash_popup.handle_key.assert_called_once_with(1280, 0)
        send_listener.on_action_performed.assert_not_called()
        assert (event.Consume)


class TestTtsStopInteraction:
    def test_stop_line_is_not_spoken(self) -> None:
        listener = _make_send_listener()
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(False, False, False, False, True),
            tool_loop=MagicMock(),
            audio=AudioRecorderState(status="idle"),
        )
        from plugin.chatbot.tool_loop_actions import _STOP_LINE
        listener.sidebar_state.tool_loop.messages = [{"role": "assistant", "content": _STOP_LINE}]
        with (
            patch("plugin.audio.tts_service.speak_text_async") as speak,
            patch("plugin.audio.tts_service.is_speaking", return_value=False),
            patch("plugin.framework.config.get_config_bool_safe", return_value=True),
        ):
            # simulate send completed event handling inside drain
            listener._terminal_status = "Ready"
            listener._send_cancellation = None
            listener._panel_teardown = False
            listener._sync_has_text_from_query = MagicMock()
            listener.dispatch = MagicMock()

            # The exact block for TTS in processEventsToIdle
            from plugin.chatbot.panel import SendEvent, SendEventKind
            listener.dispatch(SendEvent(SendEventKind.SEND_COMPLETED))
            listener._run_send_drain() # Wait, _run_send_drain doesn't do the TTS directly.

            # Actually, the TTS is triggered by resolving the block inside _run_send_drain.
            # We can mock session_for_turn to return the mock tool_loop.
            with patch("plugin.chatbot.tool_loop_actions.session_for_turn", return_value=listener.sidebar_state.tool_loop):
                listener._run_send_drain()
        speak.assert_not_called()

    def test_stop_button_stops_speech_when_speaking(self) -> None:
        send_listener = MagicMock()
        send_listener._approval_event = None
        send_listener._send_busy = False
        stop_model = MagicMock()
        stop_model.Enabled = True
        send_listener.stop_control.getModel.return_value = stop_model

        listener = StopButtonListener(send_listener)
        with patch("plugin.audio.tts_service.is_speaking", return_value=True):
            with patch("plugin.audio.tts_service.stop_speech") as mock_stop_speech:
                listener.on_action_performed(MagicMock())
                mock_stop_speech.assert_called_once()
                assert stop_model.Enabled is False
                send_listener.dispatch.assert_not_called()

    def test_notify_stop_mouse_pressed_stops_speech_when_speaking(self) -> None:
        send_listener = MagicMock()
        send_listener._approval_event = None
        send_listener._send_busy = False
        stop_model = MagicMock()
        stop_model.Enabled = True
        send_listener.stop_control.getModel.return_value = stop_model

        with patch("plugin.audio.tts_service.is_speaking", return_value=True):
            with patch("plugin.audio.tts_service.stop_speech") as mock_stop_speech:
                notify_stop_mouse_pressed(send_listener)
                mock_stop_speech.assert_called_once()
                assert stop_model.Enabled is False
                send_listener.dispatch.assert_not_called()

    def test_clear_stops_speech(self) -> None:
        session = MagicMock()
        listener = ClearButtonListener(session, MagicMock(), MagicMock(), "greeting")
        with patch("plugin.audio.tts_service.stop_speech") as mock_stop_speech:
            listener.on_action_performed(MagicMock())
            mock_stop_speech.assert_called_once()
            session.clear.assert_called_once()


def _hands_free_listener() -> tuple[SendButtonListener, Any]:
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
    listener.audio_recorder.state = AudioRecorderState(status="idle")
    listener._spawn_record_hold_wait = MagicMock()
    listener._spawn_sticky_tts_wait = MagicMock()
    return listener, send_model


class TestHandsFreeRecord:
    def setup_method(self) -> None:
        self._modules_patcher = patch.dict(sys.modules, {"plugin.main": MagicMock()}, clear=False)
        self._modules_patcher.start()

    def teardown_method(self) -> None:
        self._modules_patcher.stop()

    def test_short_click_records_once_and_ignores_following_action(self) -> None:
        listener, send_model = _hands_free_listener()
        notify_record_mouse_pressed(listener)
        notify_record_mouse_released(listener)
        assert listener.sidebar_state.send.is_recording
        assert listener._record_gesture.sticky is False
        assert listener.audio_recorder.start_recording.call_count == 1
        assert send_model.Label == "Stop Rec"
        listener.on_action_performed(MagicMock())
        assert listener.sidebar_state.send.is_recording
        assert listener.sidebar_state.send.is_busy is False
        assert listener.audio_recorder.start_recording.call_count == 1

    def test_long_press_sets_sticky_and_hands_free_status(self) -> None:
        listener, _send_model = _hands_free_listener()
        notify_record_mouse_pressed(listener)
        listener._spawn_record_hold_wait.assert_called_once()
        listener._on_record_hold_elapsed(listener._record_hold_gen)
        assert listener._record_gesture.sticky is True
        assert listener.sidebar_state.send.is_recording
        assert listener.audio_recorder.start_recording.call_count == 1
        listener.status_control.setText.assert_any_call(hands_free_status_text())
        notify_record_mouse_released(listener)
        listener.on_action_performed(MagicMock())
        assert listener.sidebar_state.send.is_recording
        assert listener._record_gesture.sticky is True
        assert listener.audio_recorder.start_recording.call_count == 1

    def test_stale_hold_timer_does_not_arm_sticky(self) -> None:
        listener, _send_model = _hands_free_listener()
        notify_record_mouse_pressed(listener)
        notify_record_mouse_released(listener)
        listener._on_record_hold_elapsed(listener._record_hold_gen - 1)
        assert listener._record_gesture.sticky is False
        assert listener.audio_recorder.start_recording.call_count == 1

    def test_keyboard_record_is_not_sticky(self) -> None:
        listener, _send_model = _hands_free_listener()
        listener.on_action_performed(MagicMock())
        assert listener.sidebar_state.send.is_recording
        assert listener._record_gesture.sticky is False

    def test_stop_rec_keeps_sticky_for_restart(self) -> None:
        listener, send_model = _hands_free_listener()
        notify_record_mouse_pressed(listener)
        listener._on_record_hold_elapsed(listener._record_hold_gen)
        notify_record_mouse_released(listener)
        listener.on_action_performed(MagicMock())
        assert send_model.Label == "Stop Rec"
        posted: list[Any] = []
        listener.queue_executor.post = lambda fn, *args, **kwargs: posted.append(fn)
        notify_record_mouse_pressed(listener)
        notify_record_mouse_released(listener)
        listener.on_action_performed(MagicMock())
        assert listener.sidebar_state.send.is_busy
        assert listener.sidebar_state.send.is_recording is False
        assert listener._record_gesture.sticky is True
        assert listener._sticky_restart_pending is False
        with patch("plugin.audio.tts_service.is_speaking", return_value=False):
            listener.dispatch(SendEvent(SendEventKind.SEND_COMPLETED))
            assert listener._sticky_restart_pending is True
            listener._flush_sticky_restart()
        assert listener._sticky_restart_pending is False
        assert listener.sidebar_state.send.is_recording
        assert listener._record_gesture.sticky is True
        assert not (listener.sidebar_state.send.is_busy and listener.sidebar_state.send.is_recording)

    def test_send_completed_without_sticky_does_not_restart(self) -> None:
        listener, _send_model = _hands_free_listener()
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(True, False, False, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
        with patch("plugin.audio.tts_service.is_speaking", return_value=False):
            listener.dispatch(SendEvent(SendEventKind.SEND_COMPLETED))
            listener._flush_sticky_restart()
        assert listener._sticky_restart_pending is False
        assert listener.sidebar_state.send.is_recording is False
        listener._spawn_sticky_tts_wait.assert_not_called()
        listener.audio_recorder.start_recording.assert_not_called()

    def test_send_completed_with_sticky_waits_for_tts_then_records(self) -> None:
        listener, _send_model = _hands_free_listener()
        listener._record_gesture = RecordGesture(sticky=True)
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(True, False, False, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
        listener.dispatch(SendEvent(SendEventKind.SEND_COMPLETED))
        assert listener._sticky_restart_pending is True
        with patch("plugin.audio.tts_service.is_speaking", return_value=True):
            listener._flush_sticky_restart()
        assert listener.sidebar_state.send.is_recording is False
        listener._spawn_sticky_tts_wait.assert_called_once()
        gen = listener._spawn_sticky_tts_wait.call_args.args[0]
        with patch("plugin.audio.tts_service.is_speaking", return_value=False):
            listener._poll_sticky_rerecord(gen)
        assert listener.sidebar_state.send.is_recording
        assert listener._record_gesture.sticky is True
        listener.status_control.setText.assert_any_call(HANDS_FREE_STATUS)

    def test_stop_during_busy_clears_sticky_so_completion_does_not_restart(self) -> None:
        listener, _send_model = _hands_free_listener()
        listener._record_gesture = RecordGesture(sticky=True)
        posted: list[Any] = []
        listener.queue_executor.post = lambda fn, *args, **kwargs: posted.append(fn)
        listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
        listener.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
        assert listener.sidebar_state.send.is_busy
        listener.dispatch(SendEvent(SendEventKind.STOP_CLICKED))
        assert listener._record_gesture.sticky is False
        with patch("plugin.audio.tts_service.is_speaking", return_value=False):
            listener.dispatch(SendEvent(SendEventKind.SEND_COMPLETED))
            listener._flush_sticky_restart()
        assert listener._sticky_restart_pending is False
        assert listener.sidebar_state.send.is_recording is False

    def test_error_clears_sticky(self) -> None:
        listener, _send_model = _hands_free_listener()
        listener._record_gesture = RecordGesture(sticky=True)
        listener.sidebar_state = SidebarCompositeState(
            send=SendButtonState(True, False, True, False, True),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )
        listener.dispatch(SendEvent(SendEventKind.ERROR_OCCURRED))
        assert listener._record_gesture.sticky is False
        assert listener.sidebar_state.send.is_busy is False
        listener._flush_sticky_restart()
        listener.audio_recorder.start_recording.assert_not_called()

    def test_record_start_failure_clears_sticky(self) -> None:
        listener, _send_model = _hands_free_listener()
        listener.audio_recorder.start_recording.side_effect = RuntimeError("no microphone")
        listener._append_response = MagicMock()
        notify_record_mouse_pressed(listener)
        listener._on_record_hold_elapsed(listener._record_hold_gen)
        assert listener._record_gesture.sticky is False
        assert listener.sidebar_state.send.is_recording is False

    def test_clear_exits_sticky(self) -> None:
        listener, _send_model = _hands_free_listener()
        listener._record_gesture = RecordGesture(sticky=True)
        listener.response_control = None
        listener.status_control = None
        clear = ClearButtonListener(listener.session, None, None, send_listener=listener)
        with patch("plugin.audio.tts_service.stop_speech"):
            clear.on_action_performed(MagicMock())
        assert listener._record_gesture.sticky is False

    def test_send_drain_restarts_record_when_sticky(self) -> None:
        """The drain, not the pure FSM, flushes sticky after the reply."""
        listener, _send_model = _hands_free_listener()
        listener._record_gesture = RecordGesture(sticky=True)
        listener._do_send = MagicMock()
        posted: list[Any] = []
        listener.queue_executor.post = lambda fn, *args, **kwargs: posted.append(fn)
        listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))
        listener.dispatch(SendEvent(SendEventKind.SEND_CLICKED))
        assert len(posted) == 1
        with patch("plugin.audio.tts_service.is_speaking", return_value=False):
            posted[0]()
        assert listener.sidebar_state.send.is_recording
        assert listener._record_gesture.sticky is True
        assert not (listener.sidebar_state.send.is_busy and listener.sidebar_state.send.is_recording)

    def test_stop_during_playback_clears_sticky_without_cancel(self) -> None:
        listener, _send_model = _hands_free_listener()
        listener._record_gesture = RecordGesture(sticky=True)
        listener._sticky_restart_gen = 3
        with patch("plugin.audio.tts_service.is_speaking", return_value=True):
            with patch("plugin.audio.tts_service.stop_speech"):
                notify_stop_mouse_pressed(listener)
        assert listener._record_gesture.sticky is False
        assert listener._stop_requested_fallback is False
        listener._poll_sticky_rerecord(3)
        assert listener.sidebar_state.send.is_recording is False


