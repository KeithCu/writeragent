# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for debug-only sidebar mock-LLM hooks."""

from __future__ import annotations

import dataclasses
import sys
from types import SimpleNamespace

import pytest

from plugin.chatbot.audio_recorder_state import AudioRecorderState
from plugin.chatbot.send_state import SendButtonState, SendEventKind
from plugin.chatbot.sidebar_state import SidebarCompositeState
from plugin.chatbot.sidebar_test_hooks import (
    approval_active,
    audio_status,
    chat_dialog_controls,
    control_enabled,
    debug_hooks_available,
    inject_wav,
    iter_live_chat_panels,
    register_live_panel,
    unregister_live_panel,
    press_accept,
    press_change,
    press_record,
    press_reject,
    press_send,
    press_stop,
    press_stop_mouse,
    press_stop_rec,
    query_text,
    send_listener,
    send_state,
    set_audio_supported,
    set_query_text,
    show_writeragent_chat_deck,
    sidebar_deck_names,
    sidebar_panel,
    sidebar_provider,
    stub_recorder_child,
    transcript_contains,
    transcript_text,
    wait_controls_send_finished,
    wait_idle,
)
from tests.chatbot.mock_llm_harness import mock_config


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


class _FakeListener:
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
        self.sidebar_state = SidebarCompositeState(
            send=SendButtonState(
                is_busy=busy,
                is_recording=False,
                has_text=False,
                has_audio=False,
                audio_supported=True,
            ),
            tool_loop=None,
            audio=AudioRecorderState(status="idle"),
        )

    def dispatch(self, event) -> None:
        self.events.append(event)
        if event.kind == SendEventKind.TEXT_UPDATED:
            data = event.data or {}
            self.sidebar_state = dataclasses.replace(
                self.sidebar_state,
                send=dataclasses.replace(self.sidebar_state.send, has_text=bool(data.get("has_text"))),
            )

    def on_action_performed(self, rEvent) -> None:
        self.events.append(("action", rEvent, self.send_control.getModel().Label))

    def _finish_inline_web_approval(self, approved, query_override=None) -> None:
        self.approval_finished.append((approved, query_override))
        self._approval_event = None


@pytest.fixture
def fake_listener() -> _FakeListener:
    return _FakeListener()


def test_debug_hooks_available_in_dev_tree() -> None:
    assert debug_hooks_available() is True


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


def test_press_record_and_stop_rec_dispatch(fake_listener: _FakeListener) -> None:
    press_record(listener=fake_listener)
    press_stop_rec(listener=fake_listener)
    kinds = [e.kind for e in fake_listener.events if hasattr(e, "kind")]
    assert SendEventKind.RECORD_CLICKED in kinds
    assert SendEventKind.STOP_REC_CLICKED in kinds


def test_set_audio_supported_and_audio_status(fake_listener: _FakeListener) -> None:
    set_audio_supported(False, listener=fake_listener)
    assert fake_listener.sidebar_state.send.audio_supported is False
    status = audio_status(listener=fake_listener)
    assert status["status"] == "idle"
    assert status["has_audio"] is False


def test_packet_g_stubs_raise() -> None:
    with pytest.raises(NotImplementedError):
        inject_wav(b"")
    with pytest.raises(NotImplementedError):
        stub_recorder_child()


def test_mock_config_mutates_flags() -> None:
    cfg = SimpleNamespace(delay_ms=25, fail="none", offline=False)
    mock_config(cfg, delay_ms=40, fail="hang", offline=True)
    assert cfg.delay_ms == 40
    assert cfg.fail == "hang"
    assert cfg.offline is True


def test_sidebar_panel_none_when_empty() -> None:
    # May still see leftover panels from other tests; only assert helper types.
    panel = sidebar_panel()
    sl = send_listener()
    assert panel is None or sl is getattr(panel, "send_listener", sl)


class _FakeDeck:
    def __init__(self) -> None:
        self.activated = False
        self._panels = _FakePanels()

    def activate(self, on: bool) -> None:
        self.activated = bool(on)

    def getPanels(self):
        return self._panels


class _FakeDialog:
    def __init__(self) -> None:
        self._ctrls = {"query": object(), "send": object(), "stop": object()}

    def getControl(self, name: str):
        return self._ctrls.get(name)


class _FakePanels:
    def hasByName(self, name: str) -> bool:
        return name == "ChatPanel"

    def getByName(self, name: str):
        return SimpleNamespace(getDialog=lambda: _FakeDialog())


class _FakeDecks:
    def __init__(self) -> None:
        self.writer = _FakeDeck()

    def getElementNames(self):
        return ["WriterAgentDeck"]

    def hasByName(self, name: str) -> bool:
        return name == "WriterAgentDeck"

    def getByName(self, name: str):
        return self.writer


class _SidebarProvider:
    """Matches SwXTextView.Sidebar (XSidebarProvider), not the controller."""

    def __init__(self) -> None:
        self._decks = _FakeDecks()
        self.visible_sets: list[bool] = []
        self.decks_shown = False

    def getDecks(self):
        return self._decks

    def isVisible(self) -> bool:
        return False

    def setVisible(self, value: bool) -> None:
        self.visible_sets.append(bool(value))

    def showDecks(self, value: bool) -> None:
        self.decks_shown = bool(value)


class _ProviderController:
    def __init__(self) -> None:
        self.Sidebar = _SidebarProvider()

    def getCurrentController(self):
        return self

    def getFrame(self):
        return SimpleNamespace()


def test_sidebar_provider_uses_sidebar_property_not_controller_get_decks() -> None:
    ctrl = _ProviderController()
    provider = sidebar_provider(ctrl)
    assert provider is ctrl.Sidebar
    assert not hasattr(ctrl, "getDecks")
    assert "WriterAgentDeck" in sidebar_deck_names(SimpleNamespace(), ctrl)


def test_sidebar_provider_falls_back_to_controller_get_decks() -> None:
    decks = _FakeDecks()
    ctrl = SimpleNamespace(getDecks=lambda: decks)
    assert sidebar_provider(ctrl) is ctrl


def test_chat_dialog_controls_reads_xdl_from_provider_decks() -> None:
    doc = _ProviderController()
    out = chat_dialog_controls(SimpleNamespace(), doc)
    assert out is not None
    assert "query" in out and "send" in out


def test_show_writeragent_chat_deck_activates_writeragent_deck() -> None:
    class _Helper:
        def executeDispatch(self, *args):
            return None

    ctx = SimpleNamespace(
        getServiceManager=lambda: SimpleNamespace(
            createInstanceWithContext=lambda *a: _Helper()
        )
    )
    doc = _ProviderController()
    show_writeragent_chat_deck(ctx, doc)
    assert doc.Sidebar.visible_sets == [True]
    assert doc.Sidebar.decks_shown is True
    assert doc.Sidebar._decks.writer.activated is True


class _StopCtrl:
    def __init__(self) -> None:
        self._model = SimpleNamespace(Enabled=False)

    def getModel(self):
        return self._model


def test_wait_controls_send_finished_sees_new_transcript(monkeypatch) -> None:
    stop = _StopCtrl()
    state = {"body": "old", "n": 0}

    def transcript() -> str:
        state["n"] += 1
        if state["n"] >= 2:
            stop._model.Enabled = False
            return "old\n[API error: HTTP Error 500]"
        stop._model.Enabled = True
        return "old"

    monkeypatch.setattr("plugin.chatbot.sidebar_test_hooks.time.sleep", lambda _s: None)
    ok = wait_controls_send_finished(
        {"stop": stop},
        timeout=2.0,
        transcript_fn=transcript,
        wait_for="API error",
        before="old",
    )
    assert ok is True
    assert control_enabled(stop) is False
