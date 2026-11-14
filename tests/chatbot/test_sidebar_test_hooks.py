# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for debug-only sidebar mock-LLM hooks."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import plugin.chatbot.sidebar_test_hooks as sidebar_test_hooks

pytest.importorskip("plugin.chatbot.sidebar_test_hooks")

pytestmark = pytest.mark.xdist_group("audio_recorder_control")

from plugin.chatbot.audio_recorder_state import AudioRecorderState, AudioStatus
from plugin.chatbot.send_state import ButtonAction, SendButtonState, SendEventKind, SendMode
from plugin.chatbot.sidebar_state import SidebarCompositeState
from plugin.chatbot.sidebar_test_hooks import (
    approval_active,
    ensure_slash_popup,
    iter_live_chat_panels,
    register_live_panel,
    unregister_live_panel,
    press_accept,
    press_change,
    press_query_key,
    press_record,
    press_reject,
    press_send,
    press_stop,
    press_stop_mouse,
    press_stop_rec,
    query_text,
    send_state,
    set_query_text,
    clear_sidebar_chat,
    slash_popup_state,
    stub_recorder_child,
    transcript_contains,
    transcript_text,
    wait_controls_send_finished,
    wait_idle,
    adopt_chat_sidebar,
    handle_debug_sidebar_command,
    inflate_sidebar_history,
    send_listener_for_uid,
)


class _QueryModel:
    def __init__(self) -> None:
        self.Text = ""


class _QueryControl:
    def __init__(self) -> None:
        self._model = _QueryModel()
        self._text = ""

    def getModel(self) -> _QueryModel:
        return self._model

    def setText(self, text: str) -> None:
        self._text = text
        self._model.Text = text

    def getText(self) -> str:
        return self._text or self._model.Text


class _BtnModel:
    def __init__(self, label: str) -> None:
        self.Label = label
        self.Enabled = True


class _Btn:
    def __init__(self, label: str) -> None:
        self._model = _BtnModel(label)

    def getModel(self) -> _BtnModel:
        return self._model


class _FakeAudioRecorder:
    def __init__(self) -> None:
        self.state = AudioRecorderState(status=AudioStatus.IDLE)
        self.temp_filename: str | None = None
        self._write_injected_wav = lambda: None
        self._notify_auto_stop = lambda path: None


class _FakeListener:
    session: Any
    slash_popup: Any
    _apply_sidebar_mode_fn: Any
    _record_gesture: Any
    _auto_stop_path: Any
    _last_compact_reason: Any
    model_selector: Any
    clear_listener: Any

    def __init__(self, *, busy: bool = False, approval: object | None = None) -> None:
        self.events: list = []
        self.query_control = _QueryControl()
        self.response_control = _QueryControl()
        self.send_control = _Btn("Send")
        self.stop_control = _Btn("Stop")
        self.rich_text_widget = None
        self._approval_event = approval
        self._approval_query_for_engine = "cats"
        self.approval_finished: list[tuple] = []
        self._button_action = ButtonAction.RECORD
        self._stop_button_action = ButtonAction.IGNORE
        self._voice = SimpleNamespace(_apply_record_gesture=lambda step: None, stop_during_take=lambda: False)
        self._events = SimpleNamespace(_finish_inline_web_approval=self._finish_inline_web_approval)
        self.sidebar_state = SidebarCompositeState(
            send=SendButtonState(
                mode=SendMode.BUSY if busy else SendMode.IDLE,
                has_text=False,
                has_audio=False,
                audio_supported=True,
            ),
            tool_loop=None,
        )

        self.audio_recorder = _FakeAudioRecorder()
        self.audio_recorder._notify_auto_stop = lambda path: setattr(self, "_auto_stop_path", path)

    def dispatch(self, event) -> None:
        self.events.append(event)
        if event.kind == SendEventKind.TEXT_UPDATED:
            self.sidebar_state = dataclasses.replace(
                self.sidebar_state,
                send=dataclasses.replace(self.sidebar_state.send, has_text=bool(event.has_text)),
            )

    def on_action_performed(self, rEvent) -> None:
        self.events.append(("action", rEvent, self.send_control.getModel().Label))

    def _finish_inline_web_approval(self, approved, query_override=None) -> None:
        self.approval_finished.append((approved, query_override))
        self._approval_event = None


@pytest.fixture
def fake_listener() -> _FakeListener:
    return _FakeListener()


class _Panel:
    def __init__(self) -> None:
        self.send_listener = "sl"
        self.xFrame = "frame-a"


def test_registry_register_and_unregister() -> None:
    panel = _Panel()
    register_live_panel(panel)
    try:
        assert panel in iter_live_chat_panels()
        from plugin.chatbot import sidebar_test_hooks as hooks

        assert hooks.sidebar_panel(frame="frame-a") is panel
        assert hooks.send_listener(frame="frame-a") == "sl"
    finally:
        unregister_live_panel(panel)
    assert panel not in iter_live_chat_panels()


def test_factory_debug_registry_works_without_hooks_module() -> None:
    import plugin.chatbot.panel_factory as pf

    panel = _Panel()
    saved = sys.modules.pop("plugin.chatbot.sidebar_test_hooks", None)
    try:
        pf.register_debug_live_panel(panel)
        assert panel in pf.iter_debug_live_chat_panels()
    finally:
        pf.unregister_debug_live_panel(panel)
        if saved is not None:
            sys.modules["plugin.chatbot.sidebar_test_hooks"] = saved


def test_factory_debug_registry_visible_to_hooks() -> None:
    import plugin.chatbot.panel_factory as pf

    panel = _Panel()
    pf.register_debug_live_panel(panel)
    try:
        assert panel in iter_live_chat_panels()
        from plugin.chatbot.sidebar_test_hooks import send_listener as sl_fn

        assert sl_fn(frame="frame-a") == "sl"
    finally:
        pf.unregister_debug_live_panel(panel)


def test_set_query_text_dispatches_text_updated(fake_listener: _FakeListener) -> None:
    set_query_text("  hello  ", listener=fake_listener)
    assert query_text(listener=fake_listener).strip() == "hello"
    kinds = [e.kind for e in fake_listener.events if hasattr(e, "kind")]
    assert SendEventKind.TEXT_UPDATED in kinds
    assert fake_listener.sidebar_state.send.has_text is True


def test_slash_popup_hooks_read_state_and_consume_enter(fake_listener: _FakeListener) -> None:
    fake_listener.slash_popup = SimpleNamespace(
        is_open=True,
        visible_names=["help", "clear"],
        selected_name="help",
        handle_key=lambda *a, **k: True,
    )
    state = slash_popup_state(listener=fake_listener)
    assert state["visible"] is True
    assert state["items"] == ["help", "clear"]
    assert state["selected"] == "help"
    assert state["available"] is True
    # Match panel.py QueryKeyListener: open popup consumes Enter (no Send).
    press_query_key(1280, listener=fake_listener)
    assert not any(isinstance(e, tuple) and e and e[0] == "action" for e in fake_listener.events)


def test_ensure_slash_popup_none_without_ctx_or_listener(monkeypatch) -> None:
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks._HOOK_CTX", None)
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.send_listener", lambda frame=None: None)
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks._URP_SLASH_POPUP", None)
    assert ensure_slash_popup() is None


def test_press_send_uses_on_action_performed(fake_listener: _FakeListener) -> None:
    press_send(listener=fake_listener)
    assert fake_listener.events[-1][0] == "action"


def test_press_stop_dispatches_stop_clicked(fake_listener: _FakeListener) -> None:
    press_stop(listener=fake_listener)
    kinds = [e.kind for e in fake_listener.events if hasattr(e, "kind")]
    assert SendEventKind.STOP_CLICKED in kinds


def test_press_stop_mouse_cancels_when_busy() -> None:
    listener = _FakeListener(busy=True)
    press_stop_mouse(listener=listener)
    kinds = [e.kind for e in listener.events if hasattr(e, "kind")]
    assert SendEventKind.STOP_CLICKED in kinds


def test_press_stop_mouse_noop_when_approval_active() -> None:
    listener = _FakeListener(busy=True, approval=object())
    press_stop_mouse(listener=listener)
    kinds = [e.kind for e in listener.events if hasattr(e, "kind")]
    assert SendEventKind.STOP_CLICKED not in kinds
    assert approval_active(listener=listener) is True


def test_press_accept_is_send_action_not_stop(fake_listener: _FakeListener) -> None:
    fake_listener.send_control.getModel().Label = "Accept"
    press_accept(listener=fake_listener)
    kinds = [e.kind for e in fake_listener.events if hasattr(e, "kind")]
    assert SendEventKind.STOP_CLICKED not in kinds
    assert fake_listener.events[-1][0] == "action"


def test_press_change_uses_override_helper(fake_listener: _FakeListener) -> None:
    fake_listener._approval_event = object()
    press_change("edited cats", listener=fake_listener)
    assert fake_listener.approval_finished == [(True, "edited cats")]
    kinds = [e.kind for e in fake_listener.events if hasattr(e, "kind")]
    assert SendEventKind.STOP_CLICKED not in kinds


def test_press_reject_does_not_stop_stream(fake_listener: _FakeListener) -> None:
    fake_listener._approval_event = object()
    press_reject(listener=fake_listener)
    assert fake_listener.approval_finished == [(False, None)]
    kinds = [e.kind for e in fake_listener.events if hasattr(e, "kind")]
    assert SendEventKind.STOP_CLICKED not in kinds


def test_transcript_contains(fake_listener: _FakeListener) -> None:
    fake_listener.response_control.setText("You: hi\nAssistant: hello")
    assert transcript_contains("hello", listener=fake_listener)
    assert transcript_text(listener=fake_listener).endswith("hello")


def test_wait_idle_true_when_not_busy(fake_listener: _FakeListener) -> None:
    assert wait_idle(listener=fake_listener, timeout=0.2) is True


def test_send_state_labels(fake_listener: _FakeListener) -> None:
    view = send_state(listener=fake_listener)
    assert view.is_busy is False
    assert view.send_label == "Send"
    assert view.stop_label == "Stop"


def test_handle_debug_sidebar_record_and_snapshot(fake_listener: _FakeListener, monkeypatch) -> None:
    from plugin.chatbot.sidebar_test_hooks import debug_sidebar_snapshot_path

    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.adopt_runtime_send_listeners", lambda: 0)
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.send_listener", lambda frame=None: fake_listener)
    handle_debug_sidebar_command("chatbot.debug_sidebar.RECORD_CLICKED")
    kinds = [e.kind for e in fake_listener.events if hasattr(e, "kind")]
    assert SendEventKind.RECORD_CLICKED in kinds
    handle_debug_sidebar_command("chatbot.debug_sidebar.SNAPSHOT")
    path = debug_sidebar_snapshot_path()
    assert os.path.isfile(path)
    os.remove(path)


def test_handle_debug_sidebar_slash_ops(fake_listener: _FakeListener, monkeypatch) -> None:
    popup = SimpleNamespace(
        is_open=True,
        visible_names=["help", "clear"],
        selected_name="help",
        on_query_text=lambda text: setattr(popup, "last_text", text),
        handle_key=lambda key, mods: setattr(popup, "last_key", (key, mods)),
        last_text="",
        last_key=None,
    )
    fake_listener.slash_popup = popup
    fake_listener.query_control.setText("/he")
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.adopt_runtime_send_listeners", lambda: 0)
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.send_listener", lambda frame=None: fake_listener)
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks._slash_lru_names", lambda: ["help"])

    handle_debug_sidebar_command("chatbot.debug_sidebar.SLASH_REFRESH")
    assert popup.last_text == "/he"
    handle_debug_sidebar_command("chatbot.debug_sidebar.SLASH_ENTER")
    assert popup.last_key == (1280, 0)
    handle_debug_sidebar_command("chatbot.debug_sidebar.SLASH_ESC")
    assert popup.last_key == (1281, 0)
    handle_debug_sidebar_command("chatbot.debug_sidebar.SNAPSHOT")
    from plugin.chatbot.sidebar_test_hooks import debug_sidebar_snapshot_path

    path = debug_sidebar_snapshot_path()
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["slash_available"] is True
    assert data["slash_visible"] is True
    assert data["slash_items"] == ["help", "clear"]
    assert data["slash_selected"] == "help"
    os.remove(path)


def test_handle_debug_sidebar_text_and_mode_commands(fake_listener: _FakeListener, monkeypatch) -> None:
    applied_modes = []
    fake_listener._apply_sidebar_mode_fn = lambda mode: applied_modes.append(mode)
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.adopt_runtime_send_listeners", lambda: 0)
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.send_listener", lambda frame=None: fake_listener)

    handle_debug_sidebar_command("chatbot.debug_sidebar.SET_TEXT_EMPTY")
    assert any(e.kind == SendEventKind.TEXT_UPDATED and e.has_text is False for e in fake_listener.events)

    handle_debug_sidebar_command("chatbot.debug_sidebar.SET_TEXT_NONEMPTY")
    assert any(e.kind == SendEventKind.TEXT_UPDATED and e.has_text is True for e in fake_listener.events)

    handle_debug_sidebar_command("chatbot.debug_sidebar.SET_CHAT_MODE")
    assert "chat" in applied_modes


def test_press_record_and_stop_rec_follow_the_button(fake_listener: _FakeListener) -> None:
    """Short Record is the mouse gesture. Stop Rec is the button action."""
    from plugin.chatbot.record_gesture import RecordGesture
    from plugin.chatbot.send_state import SendEvent
    from plugin.framework.i18n import _

    fake_listener._record_gesture = RecordGesture()

    def _apply(step) -> None:
        fake_listener._record_gesture = step.gesture
        if step.dispatch_record:
            fake_listener.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))

    fake_listener._voice = SimpleNamespace(_apply_record_gesture=_apply)
    fake_listener._button_action = ButtonAction.SEND
    fake_listener.send_control.getModel().Label = _("Send")
    press_record(listener=fake_listener)
    press_stop_rec(listener=fake_listener)
    assert fake_listener.events == []

    fake_listener._button_action = ButtonAction.RECORD
    fake_listener.send_control.getModel().Label = _("Record")
    press_record(listener=fake_listener)
    assert [e.kind for e in fake_listener.events] == [SendEventKind.RECORD_CLICKED]

    fake_listener.events.clear()
    fake_listener._button_action = ButtonAction.SEND
    fake_listener.send_control.getModel().Label = _("Stop Rec")
    press_stop_rec(listener=fake_listener)
    assert ("action", None, _("Stop Rec")) in fake_listener.events
    assert not any(getattr(e, "kind", None) == SendEventKind.RECORD_CLICKED for e in fake_listener.events)


def test_real_take_drops_stale_stub_flags(fake_listener: _FakeListener) -> None:
    # S6: G flags outlived the control file, so Stop took the stub path
    # for a real child and never reaped it.
    from plugin.chatbot.sidebar_test_hooks import (
        clear_stub_recorder_control,
        try_stub_initialize,
        try_stub_stop,
    )

    stub_recorder_child(listener=fake_listener)
    clear_stub_recorder_control()
    rec = fake_listener.audio_recorder
    assert try_stub_initialize(rec) is False
    assert try_stub_stop(rec) is False


def test_handle_debug_sidebar_inflate_history(fake_listener: _FakeListener, monkeypatch) -> None:
    from plugin.chatbot.sidebar_test_hooks import debug_sidebar_snapshot_path

    class _Session:
        def __init__(self) -> None:
            self.messages = [{"role": "system", "content": "sys"}]
            self.compaction = None

    fake_listener.session = _Session()
    fake_listener._last_compact_reason = None
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.adopt_runtime_send_listeners", lambda: 0)
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks._listener_for_current_doc", lambda: fake_listener)
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.send_listener", lambda frame=None: fake_listener)
    handle_debug_sidebar_command("chatbot.debug_sidebar.INFLATE_HISTORY")
    path = debug_sidebar_snapshot_path()
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    os.remove(path)
    assert data["session_n_messages"] >= 5
    assert data["has_compaction"] is False
    assert sum(data["session_content_chars"]) >= 20000


def test_inflate_sidebar_history_in_process(fake_listener: _FakeListener, monkeypatch) -> None:
    from plugin.chatbot.compaction import estimate_tokens

    class _Session:
        def __init__(self) -> None:
            self.messages = [{"role": "system", "content": "sys"}]
            self.compaction = None

    fake_listener.session = _Session()
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.send_listener", lambda frame=None: fake_listener)
    snap = inflate_sidebar_history()
    assert snap["session_n_messages"] >= 5
    assert int(snap.get("inflate_pairs") or 0) >= 1
    # Mock catalog window is 32768; proactive compact fires at 75%.
    assert estimate_tokens(fake_listener.session.messages) >= int(32768 * 0.75)
    assert fake_listener.session.messages[0]["role"] == "system"
    assert fake_listener.session.messages[-1]["role"] == "assistant"


def test_sidebar_panel_prefers_current_doc_not_weakset_first(monkeypatch) -> None:
    """Packet K: leftover Calc must not win inflate / send_listener()."""
    from plugin.chatbot import sidebar_test_hooks as hooks

    class _Session:
        def __init__(self, name: str) -> None:
            self.messages = [{"role": "system", "content": name}]
            self.compaction = None

    writer_frame = object()
    calc_frame = object()
    writer_sl = SimpleNamespace(session=_Session("writer"), slash_popup=None, name="writer")
    calc_sl = SimpleNamespace(session=_Session("calc"), slash_popup="stolen", name="calc")
    writer = SimpleNamespace(xFrame=writer_frame, Frame=writer_frame, send_listener=writer_sl)
    calc = SimpleNamespace(xFrame=calc_frame, Frame=calc_frame, send_listener=calc_sl)
    monkeypatch.setattr(hooks, "iter_live_chat_panels", lambda: [calc, writer])
    monkeypatch.setattr(hooks, "_current_frame", lambda: writer_frame)
    assert hooks.sidebar_panel() is writer
    assert hooks.sidebar_panel(writer_frame) is writer
    assert hooks.sidebar_panel(calc_frame) is calc
    assert hooks.send_listener() is writer_sl
    assert hooks.send_listener() is not calc_sl


def test_sidebar_panel_none_when_several_panels_miss(monkeypatch) -> None:
    """A frame miss with two live decks must not return panels[0]."""
    from plugin.chatbot import sidebar_test_hooks as hooks

    calc = SimpleNamespace(xFrame=object(), Frame=object(), _live_panel_uid="calc", send_listener=object())
    writer = SimpleNamespace(xFrame=object(), Frame=object(), _live_panel_uid="writer", send_listener=object())
    monkeypatch.setattr(hooks, "iter_live_chat_panels", lambda: [calc, writer])
    monkeypatch.setattr(hooks, "_current_frame", lambda: object())
    assert hooks.sidebar_panel() is None
    assert hooks.sidebar_panel(uid="writer") is writer
    assert hooks.sidebar_panel(uid="missing") is None
    assert hooks.send_listener() is None


def test_sidebar_panel_named_frame_does_not_fall_through_to_only_panel(monkeypatch) -> None:
    """A frame the caller already has must not resolve via panels[0]."""
    from plugin.chatbot import sidebar_test_hooks as hooks

    only = SimpleNamespace(xFrame=object(), send_listener="sl")
    monkeypatch.setattr(hooks, "iter_live_chat_panels", lambda: [only])
    other = object()
    assert hooks.sidebar_panel(frame=other) is None
    assert hooks.send_listener(frame=other) is None


def test_sidebar_panel_off_thread_wrapper_does_not_call_uno_same(monkeypatch) -> None:
    """uno_same is main-thread-only. A miss must not become panels[0]."""
    from plugin.chatbot import sidebar_test_hooks as hooks

    writer_frame = object()
    only = SimpleNamespace(xFrame=writer_frame, send_listener="sl")
    monkeypatch.setattr(hooks, "iter_live_chat_panels", lambda: [only])
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)

    def uno_same(left: object, right: object) -> bool:
        raise AssertionError("uno_same off the main thread")

    monkeypatch.setattr("plugin.framework.uno_context.uno_same", uno_same)
    assert hooks.sidebar_panel(frame=object()) is None
    assert hooks.send_listener(frame=object()) is None
    assert hooks.sidebar_panel(frame=writer_frame) is only


def test_sidebar_panel_returns_the_only_live_panel(monkeypatch) -> None:
    from plugin.chatbot import sidebar_test_hooks as hooks

    only = SimpleNamespace(xFrame=object(), _live_panel_uid="only", send_listener="sl")
    monkeypatch.setattr(hooks, "iter_live_chat_panels", lambda: [only])
    monkeypatch.setattr(hooks, "_current_frame", lambda: object())
    assert hooks.sidebar_panel() is only
    assert hooks.send_listener() == "sl"


def test_frames_match_does_not_call_uno_same_off_main_thread(monkeypatch) -> None:
    from plugin.chatbot import sidebar_test_hooks as hooks

    called: list[object] = []
    writer_frame = object()
    writer = SimpleNamespace(xFrame=writer_frame, _live_panel_uid="writer")
    calc = SimpleNamespace(xFrame=object(), _live_panel_uid="calc")
    monkeypatch.setattr(hooks, "iter_live_chat_panels", lambda: [calc, writer])
    monkeypatch.setattr(hooks, "_current_frame", lambda: object())

    def uno_same(left: object, right: object) -> bool:
        called.append((left, right))
        return left is writer_frame

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)
    monkeypatch.setattr("plugin.framework.uno_context.uno_same", uno_same)
    assert hooks.sidebar_panel() is None
    assert called == []


def test_send_listener_for_uid_matches_registered_panel_uid(monkeypatch) -> None:
    from plugin.chatbot import sidebar_test_hooks as hooks

    writer_sl = SimpleNamespace(_panel_teardown=False)
    writer = SimpleNamespace(_live_panel_uid="writer-uid", send_listener=writer_sl)
    calc_sl = SimpleNamespace(_panel_teardown=False)
    calc = SimpleNamespace(_live_panel_uid="calc-uid", send_listener=calc_sl)
    monkeypatch.setattr(hooks, "iter_live_chat_panels", lambda: [calc, writer])
    monkeypatch.setattr("plugin.doc.live_panels.get_live_panel", lambda uid: None)
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)
    assert hooks.send_listener_for_uid("writer-uid") is writer_sl
    writer_sl._panel_teardown = True
    assert hooks.send_listener_for_uid("writer-uid") is None


def test_adopt_and_iter_skip_torn_down_listeners(monkeypatch) -> None:
    from plugin.chatbot import sidebar_test_hooks as hooks

    class SendButtonListener:
        def __init__(self, teardown: bool) -> None:
            self._panel_teardown = teardown
            self.dispatch = lambda event: None
            self.query_control = object()

    live = SendButtonListener(False)
    dead = SendButtonListener(True)
    panel_live = SimpleNamespace(send_listener=live)
    panel_dead = SimpleNamespace(send_listener=dead)
    monkeypatch.setattr(
        "plugin.chatbot.panel_factory.iter_debug_live_chat_panels",
        lambda: [panel_live, panel_dead],
    )
    saved = list(hooks._LIVE_SEND_LISTENERS)
    hooks._LIVE_SEND_LISTENERS.clear()
    try:
        assert hooks.adopt_runtime_send_listeners() == 1
        assert hooks._LIVE_SEND_LISTENERS == [live]
        assert hooks.iter_send_listeners() == [live]
        live._panel_teardown = True
        assert hooks.iter_send_listeners() == []
        assert live not in hooks._LIVE_SEND_LISTENERS
    finally:
        hooks._LIVE_SEND_LISTENERS[:] = saved


def test_send_listener_fallback_skips_torn_down(monkeypatch) -> None:
    from plugin.chatbot import sidebar_test_hooks as hooks

    live = SimpleNamespace(_panel_teardown=False, slash_popup=None)
    dead = SimpleNamespace(_panel_teardown=True, slash_popup="closed")
    monkeypatch.setattr(hooks, "iter_live_chat_panels", lambda: [])
    saved = list(hooks._LIVE_SEND_LISTENERS)
    hooks._LIVE_SEND_LISTENERS[:] = [live, dead]
    try:
        assert hooks.send_listener() is live
        assert dead not in hooks._LIVE_SEND_LISTENERS
    finally:
        hooks._LIVE_SEND_LISTENERS[:] = saved


def test_inflate_history_pads_current_doc_not_leftover_calc(
    fake_listener: _FakeListener, monkeypatch
) -> None:
    """INFLATE_HISTORY must grow the current Writer session, not WeakSet[0]."""
    from plugin.chatbot import sidebar_test_hooks as hooks
    from plugin.chatbot.sidebar_test_hooks import debug_sidebar_snapshot_path

    class _Session:
        def __init__(self, name: str) -> None:
            self.messages = [{"role": "system", "content": name}]
            self.compaction = None

    writer_frame = object()
    calc_frame = object()
    writer_session = _Session("writer")
    calc_session = _Session("calc")
    writer_sl = fake_listener
    writer_sl.session = writer_session
    writer_sl.model_selector = None
    writer_sl._last_compact_reason = None
    calc_sl = _FakeListener()
    calc_sl.session = calc_session
    calc_sl.slash_popup = "leftover"
    calc_sl.model_selector = None
    writer = SimpleNamespace(xFrame=writer_frame, Frame=writer_frame, send_listener=writer_sl)
    calc = SimpleNamespace(xFrame=calc_frame, Frame=calc_frame, send_listener=calc_sl)
    monkeypatch.setattr(hooks, "adopt_runtime_send_listeners", lambda: 0)
    monkeypatch.setattr(hooks, "iter_live_chat_panels", lambda: [calc, writer])
    monkeypatch.setattr(hooks, "_current_frame", lambda: writer_frame)
    monkeypatch.setattr(hooks, "_HOOK_CTX", object())
    monkeypatch.setattr(hooks, "current_component", lambda ctx: SimpleNamespace(_frame=writer_frame))
    monkeypatch.setattr(
        hooks,
        "send_listener_for_doc",
        lambda doc: writer_sl if getattr(doc, "_frame", None) is writer_frame else calc_sl,
    )
    handle_debug_sidebar_command("chatbot.debug_sidebar.INFLATE_HISTORY")
    path = debug_sidebar_snapshot_path()
    assert os.path.isfile(path)
    os.remove(path)
    assert sum(len(str(m.get("content") or "")) for m in writer_session.messages) >= 20000
    assert calc_session.messages == [{"role": "system", "content": "calc"}]


def test_parse_debug_sidebar_command_strips_uid() -> None:
    from plugin.chatbot.sidebar_test_hooks import (
        _debug_sidebar_query,
        _parse_debug_sidebar_command,
    )

    parse_op = _parse_debug_sidebar_command
    assert parse_op("chatbot.debug_sidebar.INFLATE_HISTORY") == ("INFLATE_HISTORY", "")
    assert parse_op("chatbot.debug_sidebar?INFLATE_HISTORY&uid=34") == ("INFLATE_HISTORY", "34")
    assert parse_op("chatbot.debug_sidebar.SNAPSHOT&uid=writer-uid") == ("SNAPSHOT", "writer-uid")
    assert parse_op("chatbot.debug_sidebar.OPEN_CALC") == ("OPEN_CALC", "")
    assert _debug_sidebar_query("INFLATE_HISTORY", "34") == "INFLATE_HISTORY&uid=34"
    assert _debug_sidebar_query("SNAPSHOT", "") == "SNAPSHOT"


def test_inflate_history_uid_pads_writer_when_soffice_current_is_calc(
    fake_listener: _FakeListener, monkeypatch
) -> None:
    """URP ``&uid=`` must win over leftover Calc as soffice current component.

    #802 bound INFLATE to getCurrentComponent(); CI K1 still saw hello
    n_messages=2 because soffice current stayed on leftover Calc after P/E12
    while URP Send clicked Writer.
    """
    from plugin.chatbot import sidebar_test_hooks as hooks
    from plugin.chatbot.sidebar_test_hooks import debug_sidebar_snapshot_path
    import plugin.doc.live_panels as live_panels
    from plugin.doc.live_panels import register_live_panel

    class _Session:
        def __init__(self, name: str) -> None:
            self.messages = [{"role": "system", "content": name}]
            self.compaction = None

    class _Panel:
        # live_panels is a WeakValueDictionary — SimpleNamespace is not weakref-able.
        def __init__(self, sl) -> None:
            self.send_listener = sl

    writer_session = _Session("writer")
    calc_session = _Session("calc")
    writer_sl = fake_listener
    writer_sl.session = writer_session
    writer_sl.model_selector = None
    writer_sl._last_compact_reason = None
    calc_sl = _FakeListener()
    calc_sl.session = calc_session
    calc_sl.slash_popup = "leftover"
    calc_sl.model_selector = None
    writer = _Panel(writer_sl)
    monkeypatch.setattr(live_panels, "_PANELS", {})
    register_live_panel("writer-uid", writer)
    monkeypatch.setattr(hooks, "adopt_runtime_send_listeners", lambda: 0)
    # Soffice current is leftover Calc — the #802 path would pad this session.
    monkeypatch.setattr(hooks, "_listener_for_current_doc", lambda: calc_sl)
    monkeypatch.setattr(hooks, "send_listener", lambda frame=None: calc_sl)
    monkeypatch.setattr(hooks, "_listener_with_slash_popup", lambda sl: sl)
    handle_debug_sidebar_command("chatbot.debug_sidebar.INFLATE_HISTORY&uid=writer-uid")
    path = debug_sidebar_snapshot_path()
    assert os.path.isfile(path)
    os.remove(path)
    assert sum(len(str(m.get("content") or "")) for m in writer_session.messages) >= 20000
    assert calc_session.messages == [{"role": "system", "content": "calc"}]
    assert send_listener_for_uid("writer-uid") is writer_sl
    assert send_listener_for_uid("missing") is None


def test_clear_sidebar_chat_resets_session_and_widget(fake_listener: _FakeListener) -> None:
    """Packet G must wipe leftover E/F transcript before canned-string asserts."""
    cleared: list[str] = []

    class _Session:
        def clear(self) -> None:
            cleared.append("session")

    class _Widget:
        def clear_and_greeting(self, greeting: str = "") -> None:
            cleared.append("widget:%s" % greeting)

    fake_listener.session = _Session()
    fake_listener.rich_text_widget = _Widget()
    fake_listener.response_control.setText("You: look up cats\nAssistant: leftover")
    clear_sidebar_chat(listener=fake_listener)
    assert cleared == ["session", "widget:"]


def test_clear_sidebar_chat_uses_clear_listener(fake_listener: _FakeListener) -> None:
    """In-process Clear is the button listener, which latches Stop when busy."""
    cleared: list[str] = []
    clicked: list[object] = []

    class _Session:
        def clear(self) -> None:
            cleared.append("session")

    fake_listener.session = _Session()
    fake_listener.clear_listener = SimpleNamespace(on_action_performed=lambda event: clicked.append(event))
    clear_sidebar_chat(listener=fake_listener)
    assert clicked == [None]
    assert cleared == []


def test_clear_sidebar_chat_falls_back_to_response_control(fake_listener: _FakeListener) -> None:
    fake_listener.session = None
    fake_listener.rich_text_widget = None
    fake_listener.response_control.setText("You: hello\nAssistant: leftover")
    clear_sidebar_chat(listener=fake_listener)
    assert fake_listener.response_control.getText() == ""


def test_clear_sidebar_chat_urp_clicks_clear(monkeypatch) -> None:
    import plugin.chatbot.sidebar_test_hooks as hooks

    clicked: list[object] = []
    clear_btn = object()
    monkeypatch.setattr(hooks, "send_listener", lambda frame=None: None)
    monkeypatch.setattr(hooks, "chat_dialog_controls", lambda ctx, doc: {"clear": clear_btn})
    monkeypatch.setattr(hooks, "current_component", lambda ctx: object())
    monkeypatch.setattr(hooks, "uno_click", lambda ctrl: clicked.append(ctrl))
    saved_ctx = hooks._HOOK_CTX
    hooks._HOOK_CTX = object()
    try:
        clear_sidebar_chat(listener=None)
    finally:
        hooks._HOOK_CTX = saved_ctx
    assert clicked == [clear_btn]


class _StopCtrl:
    def __init__(self) -> None:
        self._model = SimpleNamespace(Enabled=False)

    def getModel(self):
        return self._model


def test_wait_controls_send_finished_wait_for_ignores_prior_turns(monkeypatch) -> None:
    """Packet C: ``ran out of tokens`` in an earlier turn must not finish a later send."""
    stop = _StopCtrl()
    stop._model.Enabled = False
    prior = "Assistant: [Response truncated -- the model ran out of tokens...]\n"

    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.time.sleep", lambda _s: None)
    ok = wait_controls_send_finished(
        {"stop": stop},
        timeout=0.4,
        transcript_fn=lambda: prior + "You: round one\nAssistant: Mock notes\nTopic: hello.\n",
        wait_for="ran out of tokens",
        before=prior,
    )
    assert ok is False


def test_post_to_soffice_vcl_inits_async_callback(monkeypatch) -> None:
    inited: list[bool] = []
    posted: list = []
    qe = SimpleNamespace(
        _get_async_callback=lambda: inited.append(True),
        post=lambda fn, *a, **k: posted.append(fn),
    )
    sl = SimpleNamespace(queue_executor=qe)
    from plugin.chatbot.sidebar_test_hooks import _post_to_soffice_vcl

    _post_to_soffice_vcl(lambda: None, sl=sl)
    assert inited == [True]
    assert posted


def test_handle_debug_sidebar_fsm_op_inits_async_callback_before_post(fake_listener, monkeypatch) -> None:
    """Packet P: STOP_CLICKED on a fresh panel must not sit queued until KICK_PEERS.

    Force-marshal post skips AsyncCallback init. Without the init the poke was
    a no-op, and the stale Stop later cancelled Calc's peer turn mid-flight.
    """
    order: list[str] = []
    posted: list = []

    def _post(fn, *a, **k) -> None:
        order.append("post")
        posted.append(fn)

    fake_listener.queue_executor = SimpleNamespace(
        _get_async_callback=lambda: order.append("init"),
        post=_post,
    )
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.adopt_runtime_send_listeners", lambda: 0)
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.send_listener", lambda frame=None: fake_listener)
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks._write_debug_snapshot", lambda sl: {})
    handle_debug_sidebar_command("chatbot.debug_sidebar.STOP_CLICKED")
    assert order == ["init", "post"]
    posted[0]()
    kinds = [e.kind for e in fake_listener.events if hasattr(e, "kind")]
    assert SendEventKind.STOP_CLICKED in kinds


def test_adopt_chat_sidebar_shows_deck_on_doc(monkeypatch) -> None:
    doc = SimpleNamespace(
        getCurrentController=lambda: SimpleNamespace(getFrame=lambda: "calc-frame")
    )
    shown: list = []
    monkeypatch.setattr(
        "plugin.chatbot.sidebar_test_hooks.wait_for_chat_dialog_controls",
        lambda ctx, timeout=20.0, doc=None: shown.append(doc) or {"query": 1, "send": 1},
    )
    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.adopt_runtime_send_listeners", lambda: 0)
    monkeypatch.setattr(
        "plugin.chatbot.sidebar_test_hooks.send_listener",
        lambda frame=None: "sl-%s" % frame,
    )
    controls, sl = adopt_chat_sidebar(object(), doc)
    assert shown == [doc]
    assert controls == {"query": 1, "send": 1}
    assert sl == "sl-calc-frame"


def test_load_visible_calc_factory_uses_blank_target(monkeypatch) -> None:
    """Dual-peer needs Writer to stay open — factory load must use ``_blank``."""
    calls: list[tuple] = []

    class _Desktop:
        def loadComponentFromURL(self, url, target, _flags, _props):
            calls.append((url, target))
            return "calc"

    monkeypatch.setattr(
        "plugin.framework.uno_context.get_ctx", lambda: object()
    )
    monkeypatch.setattr(
        "plugin.framework.uno_context.get_desktop", lambda _ctx: _Desktop()
    )
    from plugin.chatbot.sidebar_test_hooks import _load_visible_calc_factory

    _load_visible_calc_factory()
    assert calls == [("private:factory/scalc", "_blank")]


class _TextCtl:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def getText(self) -> str:
        return self.text

    def setText(self, text: str) -> None:
        self.text = text


def test_urp_slash_host_help_appends_output() -> None:
    # CI 40839406030: /help passed role= to the real _UrpSlashHost.append_output
    # (TypeError). MagicMock hosts accept any kwargs, so drive the real class.
    from plugin.chatbot.slash_commands import SLASH_COMMANDS, SlashResult, run_slash_command
    from plugin.chatbot.sidebar_test_hooks import _UrpSlashHost

    help_cmd = next(c for c in SLASH_COMMANDS if c.name == "help")

    query = _TextCtl("/help")
    response = _TextCtl("earlier\n")
    host = _UrpSlashHost(query, response, None, None)
    with patch("plugin.chatbot.slash_commands.record_slash_lru"):
        assert run_slash_command(help_cmd, host) is SlashResult.HANDLED
    assert query.text == ""
    assert response.text.startswith("earlier\n")
    assert "/help" in response.text and "/clear" in response.text


def test_ensure_slash_popup_registers_mock_commands(monkeypatch) -> None:
    import plugin.chatbot.slash_commands as slash

    monkeypatch.setattr(slash, "_mock_commands_enabled", False)
    monkeypatch.setattr(sidebar_test_hooks, "_require_debug", lambda: None)
    monkeypatch.setattr(sidebar_test_hooks, "send_listener", lambda: None)
    monkeypatch.setattr(sidebar_test_hooks, "_urp_slash_controller", lambda: None)
    sidebar_test_hooks.ensure_slash_popup()
    assert "mock-bravo" in [c.name for c in slash.registered_slash_commands()]
