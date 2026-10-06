# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn-end hooks under the event-driven stream drain.

With the event-driven drain, ``_run_send_drain`` returns to the VCL loop
before the stream ends. The #1347 ``input_audio`` strip and the #1348
watchdog end (``update_activity_state('')``, which also clears Hung) must
wait for the deferred drain-done close and run once, on done, Stop and
error alike. #1348's ``note_activity`` must fire from each slice that
applies streamed text or thinking.
"""

from __future__ import annotations

import queue
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.panel import ChatSession, SendButtonListener
from plugin.framework.async_stream import StreamQueueKind, run_stream_drain_loop
from tests.chatbot.test_panel import _make_send_listener


class _Rearm:
    """Records slices. Nothing runs until the test pumps it."""

    def __init__(self) -> None:
        self.pending: list[Any] = []

    def post(self, fn: Any) -> None:
        self.pending.append(fn)

    def post_after(self, _delay: float, fn: Any) -> None:
        self.pending.append(fn)

    def pump(self) -> None:
        self.pending.pop(0)()


@pytest.fixture(autouse=True)
def _reset_drain_and_watchdog():
    import plugin.framework.logging as logging_mod
    from plugin.framework import async_stream as stream_mod
    from plugin.framework.async_drain_guard import reset_sentry_state

    yield
    session = stream_mod._event_drain
    if session is not None and not session.closed:
        session._finish()
    stream_mod._event_drain = None
    reset_sentry_state()
    with logging_mod._activity_lock:
        logging_mod._activity_state["phase"] = ""
        logging_mod._activity_state["status_control"] = None


def _audio_listener() -> SendButtonListener:
    listener = _make_send_listener()
    listener._terminal_status = ""
    listener.sidebar_state = MagicMock()
    listener.sidebar_state.send.is_recording = False
    text_part = {"type": "text", "text": "hello"}
    audio_part = {"type": "input_audio", "input_audio": {"data": "YmFzZTY0", "format": "wav"}}
    listener.session.messages = [{"role": "user", "content": [text_part, audio_part]}]
    return listener


def _has_audio(listener: SendButtonListener) -> bool:
    content = listener.session.messages[-1]["content"]
    return isinstance(content, list) and any(c.get("type") == "input_audio" for c in content)


@pytest.mark.parametrize("ending", ["done", "stop", "error"])
def test_turn_end_hooks_wait_for_the_drain_done_close(ending: str) -> None:
    import plugin.framework.logging as logging_mod

    listener = _audio_listener()
    status_ctrl = MagicMock()
    rearm = _Rearm()
    q: queue.Queue = queue.Queue()
    stop = [False]

    def _start_turn() -> None:
        logging_mod.update_activity_state("do_send", status_control=status_ctrl)
        run_stream_drain_loop(
            q,
            MagicMock(),
            [False],
            lambda _text, _thinking: None,
            on_stream_done=lambda _item: True,
            on_stopped=lambda: None,
            on_error=lambda _e: None,
            stop_checker=lambda: stop[0],
            rearm=rearm,
        )
        if ending == "error":
            raise RuntimeError("boom")

    with (
        patch.object(listener, "_do_send", side_effect=_start_turn),
        patch.object(listener, "_project_closing_line"),
        patch("plugin.chatbot.tool_loop_actions.session_for_turn", return_value=listener.session),
        patch("plugin.chatbot.tool_loop_actions.drop_turn") as drop_turn,
        patch("plugin.doc.peer_message.kick_pending_peer_starts"),
        patch("plugin.chatbot.panel.update_activity_state", wraps=logging_mod.update_activity_state) as activity,
        patch("plugin.framework.queue_executor.post_to_main_thread") as post_main,
    ):
        listener._run_send_drain()

        # The first slice has not even run: the turn is still streaming.
        assert _has_audio(listener)
        assert all(c.args[:1] != ("",) for c in activity.call_args_list)
        assert logging_mod._activity_state["phase"] == "do_send"
        assert logging_mod._activity_state["status_control"] is status_ctrl
        post_main.assert_not_called()
        drop_turn.assert_not_called()

        # A mid-stream slice applies text and returns; still not the end.
        q.put((StreamQueueKind.CHUNK, "partial"))
        rearm.pump()
        assert _has_audio(listener)
        post_main.assert_not_called()
        drop_turn.assert_not_called()

        if ending == "stop":
            stop[0] = True
        else:
            q.put((StreamQueueKind.STREAM_DONE, "end"))
        rearm.pump()

        assert not _has_audio(listener)
        assert listener.session.messages[-1]["content"] == [{"type": "text", "text": "hello"}]
        assert [c.args[:1] for c in activity.call_args_list].count(("",)) == 1
        assert logging_mod._activity_state["phase"] == ""
        assert logging_mod._activity_state["status_control"] is None
        post_main.assert_called_once_with(logging_mod._clear_hung_status, status_ctrl)
        drop_turn.assert_called_once()

        # Nothing left to run: the strip and the Hung clear happened once.
        assert rearm.pending == []

        # Validate cleanup asserts
        from plugin.framework.queue_executor import get_drain_owner, get_drain_depth, _AGENT_OPEN_SCOPES
        from plugin.framework import async_stream

        assert get_drain_owner() is None
        assert get_drain_depth() == 0
        assert _AGENT_OPEN_SCOPES == []
        assert async_stream._event_drain is None


def test_dispose_mid_stream_cleans_up_and_drops_turn() -> None:
    from plugin.framework.queue_executor import get_drain_owner, get_drain_depth, _AGENT_OPEN_SCOPES
    from plugin.framework import async_stream
    import plugin.framework.logging as logging_mod

    listener = _audio_listener()
    listener.sidebar_state.agent = MagicMock()
    listener.sidebar_state.agent.is_closing = False

    status_ctrl = MagicMock()
    rearm = _Rearm()
    q: queue.Queue = queue.Queue()

    def _start_turn() -> None:
        logging_mod.update_activity_state("do_send", status_control=status_ctrl)
        run_stream_drain_loop(
            q,
            MagicMock(),
            [False],
            lambda _text, _thinking: None,
            on_stream_done=lambda _item: True,
            on_stopped=lambda: None,
            on_error=lambda _e: None,
            rearm=rearm,
        )

    with (
        patch.object(listener, "_do_send", side_effect=_start_turn),
        patch.object(listener, "_project_closing_line"),
        patch("plugin.chatbot.tool_loop_actions.session_for_turn", return_value=listener.session),
        patch("plugin.chatbot.tool_loop_actions.drop_turn") as drop_turn,
        patch("plugin.doc.peer_message.kick_pending_peer_starts"),
        patch("plugin.chatbot.panel.update_activity_state", wraps=logging_mod.update_activity_state),
        patch("plugin.framework.queue_executor.post_to_main_thread"),
    ):
        listener._run_send_drain()

        # The first slice has not even run: the turn is still streaming.
        assert logging_mod._activity_state["phase"] == "do_send"

        # Dispose mid-stream (calls SendButtonListener.disposing() which sets _panel_teardown = True and cancels scope)
        listener.disposing()

        # Pump the stream next - it should dispatch STOPPED but wait to check the leak asserts
        q.put((StreamQueueKind.STREAM_DONE, "end"))

        rearm.pump()

        drop_turn.assert_called_once()

        assert get_drain_owner() is None
        assert get_drain_depth() == 0
        assert _AGENT_OPEN_SCOPES == []
        assert async_stream._event_drain is None


def test_do_send_raises_before_drain_cleans_up() -> None:
    from plugin.framework.queue_executor import get_drain_owner, get_drain_depth, _AGENT_OPEN_SCOPES
    from plugin.framework import async_stream
    import plugin.framework.logging as logging_mod

    listener = _audio_listener()

    def _start_turn() -> None:
        raise RuntimeError("boom before drain starts")

    with (
        patch.object(listener, "_do_send", side_effect=_start_turn),
        patch.object(listener, "_project_closing_line"),
        patch("plugin.chatbot.tool_loop_actions.session_for_turn", return_value=listener.session),
        patch("plugin.chatbot.tool_loop_actions.drop_turn"),
        patch("plugin.doc.peer_message.kick_pending_peer_starts"),
        patch("plugin.chatbot.panel.update_activity_state", wraps=logging_mod.update_activity_state),
        patch("plugin.framework.queue_executor.post_to_main_thread"),
    ):
        listener._run_send_drain()

        # It must immediately invoke error path
        assert get_drain_owner() is None
        assert get_drain_depth() == 0
        assert _AGENT_OPEN_SCOPES == []
        assert async_stream._event_drain is None


def test_each_slice_that_applies_chunks_notes_watchdog_activity() -> None:
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    listener = _make_send_listener()
    session = ChatSession("system prompt")
    session.add_user_message("question")
    listener.session = session
    begin_send_turn(listener, "chat")
    rearm = _Rearm()
    q: queue.Queue = queue.Queue()

    with (
        patch("plugin.framework.logging.note_activity") as note,
        patch.object(listener, "render_session_messages"),
    ):
        run_stream_drain_loop(
            q,
            MagicMock(),
            [False],
            listener._append_response,
            on_stream_done=lambda _item: True,
            on_stopped=lambda: None,
            on_error=lambda _e: None,
            rearm=rearm,
        )
        q.put((StreamQueueKind.THINKING, "pondering"))
        rearm.pump()
        after_thinking = note.call_count
        assert after_thinking >= 1

        # An idle slice applies nothing and must not count as activity.
        rearm.pump()
        assert note.call_count == after_thinking

        q.put((StreamQueueKind.CHUNK, "answer"))
        rearm.pump()
        assert note.call_count > after_thinking
