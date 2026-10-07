# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-frame session: a second frame must not touch the first frame's listeners or panel."""

from __future__ import annotations

import logging
import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.frame_session import (
    FrameSession,
    document_id_for_frame,
    open_frame_session,
    reset_frame_sessions,
    session_for_frame,
)


@pytest.fixture(autouse=True)
def _reset_sessions():
    reset_frame_sessions()
    yield
    reset_frame_sessions()


@contextmanager
def _fake_listeners():
    class _Base:
        pass

    class XFocusListener:
        pass

    class XMouseListener:
        pass

    class XMouseClickHandler:
        pass

    fake_awt = types.SimpleNamespace(
        XFocusListener=XFocusListener,
        XMouseListener=XMouseListener,
        XMouseClickHandler=XMouseClickHandler,
    )
    fake_unohelper = types.SimpleNamespace(Base=_Base)
    with patch.dict(sys.modules, {"unohelper": fake_unohelper, "com.sun.star.awt": fake_awt}):
        yield


class _Controller:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.removed: list[object] = []

    def addMouseClickHandler(self, handler: object) -> None:
        self.added.append(handler)

    def removeMouseClickHandler(self, handler: object) -> None:
        self.removed.append(handler)


def _set_active_top(frame: MagicMock, top: object) -> None:
    """Make the toolkit report *top* as the active top window for *frame*."""
    container = frame.getContainerWindow.return_value
    container.getToolkit.return_value.getActiveTopWindow.return_value = top


def _frame(controller: _Controller) -> MagicMock:
    frame = MagicMock()
    frame.getController.return_value = controller
    # Default: this frame's window is the active one, so restore_focus runs.
    _set_active_top(frame, frame.getContainerWindow.return_value)
    return frame


def _install(session: FrameSession, query: MagicMock, leave: MagicMock | None = None) -> None:
    with _fake_listeners():
        session.install(MagicMock(), query=query, leave_query_controls=(leave,) if leave is not None else ())


def test_each_frame_gets_its_own_session():
    frame_a = MagicMock(name="frame-a")
    frame_b = MagicMock(name="frame-b")
    first = open_frame_session(frame_a, "doc-a")
    again = open_frame_session(frame_a, "doc-a")
    second = open_frame_session(frame_b, "doc-b")
    assert first is again
    assert first is not second
    assert session_for_frame(frame_a) is first
    assert session_for_frame(frame_b) is second
    assert first.doc_uid == "doc-a"
    assert second.doc_uid == "doc-b"


def test_document_id_comes_from_the_frame_not_the_current_component():
    frame = MagicMock()
    model = MagicMock()
    with (
        patch("plugin.framework.uno_context.get_document_from_frame", return_value=model) as from_frame,
        patch("plugin.framework.uno_context.get_runtime_uid", return_value="uid-from-frame"),
        patch("plugin.framework.uno_context.get_active_document", side_effect=AssertionError("current component")),
    ):
        assert document_id_for_frame(frame) == "uid-from-frame"
    from_frame.assert_called_once_with(frame)


def test_panel_is_constructed_with_the_session_document_id():
    from plugin.chatbot.panel_factory import ChatPanelElement

    frame = MagicMock()
    session = open_frame_session(frame, "runtime-uid-a")
    element = ChatPanelElement(MagicMock(), frame, MagicMock(), "private:resource/ChatPanel", session)
    assert element._live_panel_uid == "runtime-uid-a"
    assert element.frame_session is session
    assert session.panel is element


def test_factory_opens_one_session_per_frame():
    from types import SimpleNamespace

    from plugin.chatbot.panel_factory import ChatPanelFactory

    factory = ChatPanelFactory(MagicMock())
    frame_a = MagicMock(name="frame-a")
    frame_b = MagicMock(name="frame-b")

    def _args(frame: object) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(Name="Frame", Value=frame),
            SimpleNamespace(Name="ParentWindow", Value=MagicMock()),
        ]

    def _doc_id(frame: object) -> str:
        return "uid-a" if frame is frame_a else "uid-b"

    with (
        patch("plugin.framework.frame_session.document_id_for_frame", side_effect=_doc_id),
        patch("plugin.framework.uno_context.get_active_document", side_effect=AssertionError("current component")),
    ):
        element_a = factory.createUIElement("private:resource/toolpanel/WriterAgent/ChatPanel", _args(frame_a))
        element_b = factory.createUIElement("private:resource/toolpanel/WriterAgent/ChatPanel", _args(frame_b))
        element_a_again = factory.createUIElement("private:resource/toolpanel/WriterAgent/ChatPanel", _args(frame_a))
    assert element_a._live_panel_uid == "uid-a"
    assert element_b._live_panel_uid == "uid-b"
    assert element_a.frame_session is not element_b.frame_session
    assert element_a_again.frame_session is element_a.frame_session
    assert session_for_frame(frame_a) is element_a.frame_session
    assert session_for_frame(frame_b) is element_b.frame_session


def test_second_frame_dispose_does_not_touch_the_first_listeners():
    controller_a = _Controller()
    controller_b = _Controller()
    frame_a = _frame(controller_a)
    frame_b = _frame(controller_b)
    session_a = open_frame_session(frame_a, "doc-a")
    session_b = open_frame_session(frame_b, "doc-b")
    query_a = MagicMock(name="query-a")
    query_b = MagicMock(name="query-b")
    leave_a = MagicMock(name="leave-a")
    leave_b = MagicMock(name="leave-b")
    panel_a = MagicMock(name="panel-a")
    panel_b = MagicMock(name="panel-b")
    session_a.bind_panel(panel_a)
    session_b.bind_panel(panel_b)
    session_a.set_focus_pin(query_a)
    session_b.set_focus_pin(query_b)
    with patch("plugin.framework.uno_context.get_desktop", side_effect=AssertionError("desktop")):
        _install(session_a, query_a, leave_a)
        _install(session_b, query_b, leave_b)
    assert len(controller_a.added) == 1
    assert len(controller_b.added) == 1
    assert controller_a.added[0] is not controller_b.added[0]
    query_a.addFocusListener.assert_called_once()
    query_b.addFocusListener.assert_called_once()

    session_b.release_panel(panel_b, query_b)

    assert controller_a.removed == []
    assert controller_b.removed == [controller_b.added[0]]
    query_a.removeFocusListener.assert_not_called()
    leave_a.removeMouseListener.assert_not_called()
    assert session_a.focus_pin is query_a
    assert session_a.panel is panel_a
    assert session_b.focus_pin is None
    assert session_b.panel is None
    assert session_for_frame(frame_a) is session_a
    assert session_for_frame(frame_b) is session_b


def test_second_frame_click_does_not_restore_or_clear_the_first_panel():
    controller_a = _Controller()
    controller_b = _Controller()
    session_a = open_frame_session(_frame(controller_a), "doc-a")
    session_b = open_frame_session(_frame(controller_b), "doc-b")
    query_a = MagicMock(name="query-a")
    query_b = MagicMock(name="query-b")
    panel_a = MagicMock(name="panel-a")
    panel_b = MagicMock(name="panel-b")
    session_a.bind_panel(panel_a)
    session_b.bind_panel(panel_b)
    session_a.set_focus_pin(query_a)
    session_b.set_focus_pin(query_b)
    session_a.note_user_wants_query()
    session_b.note_user_wants_query()
    _install(session_a, query_a)
    _install(session_b, query_b)

    handler_b = controller_b.added[0]
    handler_b.mousePressed(None)

    session_a.restore_focus()
    session_b.restore_focus()
    query_a.setFocus.assert_called_once()
    query_b.setFocus.assert_not_called()
    assert session_a.panel is panel_a
    assert session_b.panel is panel_b
    assert handler_b not in controller_a.added


def test_listener_disposing_does_not_remove_listener():
    controller = _Controller()
    session = open_frame_session(_frame(controller), "doc-a")
    query = MagicMock()
    leave = MagicMock()
    _install(session, query, leave)
    click = controller.added[0]
    mouse = leave.addMouseListener.call_args[0][0]
    click.disposing(None)
    mouse.disposing(None)
    assert controller.removed == []
    leave.removeMouseListener.assert_not_called()
    query.removeFocusListener.assert_not_called()


def test_stale_panel_dispose_does_not_release_the_bound_panels_listeners():
    """A late dispose of the previous sidebar must not detach the rebound session.

    What was wrong: release_panel kept self.panel when the argument was not
    the bound panel, then still called release_listeners. How: the same frame
    reuses the session and bind_panel points it at the new element; the old
    element's dispose still runs. Why: listeners belong to the bound panel.
    """
    controller = _Controller()
    session = open_frame_session(_frame(controller), "doc-a")
    query_old = MagicMock(name="query-old")
    query_new = MagicMock(name="query-new")
    leave_new = MagicMock(name="leave-new")
    panel_old = object()
    panel_new = object()
    session.bind_panel(panel_old)
    session.set_focus_pin(query_old)
    _install(session, query_old)
    session.bind_panel(panel_new)
    session.set_focus_pin(query_new)
    _install(session, query_new, leave_new)
    click = controller.added[-1]

    session.release_panel(panel_old, query_old)

    assert session.panel is panel_new
    assert session.focus_pin is query_new
    assert session._query_control is query_new
    assert session._query_listener is not None
    assert session._click_handler is click
    assert session._leave
    assert controller.removed == []
    query_new.removeFocusListener.assert_not_called()
    leave_new.removeMouseListener.assert_not_called()
    leave_new.removeFocusListener.assert_not_called()


class _HoldingControl:
    """Keeps listeners until remove* drops them. A dead control raises instead."""

    def __init__(self, *, dead: bool = False) -> None:
        self.dead = dead
        self.listeners: list[object] = []
        self.removed: list[object] = []

    def addFocusListener(self, listener: object) -> None:
        self.listeners.append(listener)

    def removeFocusListener(self, listener: object) -> None:
        self._drop(listener)

    def addMouseListener(self, listener: object) -> None:
        self.listeners.append(listener)

    def removeMouseListener(self, listener: object) -> None:
        self._drop(listener)

    def _drop(self, listener: object) -> None:
        if self.dead:
            raise RuntimeError("disposed")
        self.removed.append(listener)
        self.listeners = [item for item in self.listeners if item is not listener]


class _HoldingController:
    def __init__(self, *, dead: bool = False) -> None:
        self.dead = dead
        self.handlers: list[object] = []
        self.removed: list[object] = []

    def addMouseClickHandler(self, handler: object) -> None:
        self.handlers.append(handler)

    def removeMouseClickHandler(self, handler: object) -> None:
        if self.dead:
            raise RuntimeError("disposed")
        self.removed.append(handler)
        self.handlers = [item for item in self.handlers if item is not handler]


def test_frame_disposing_releases_listeners_before_sidebar_teardown():
    """Frame disposing before sidebar teardown must detach listeners.

    What was wrong: dispose set _closed and cleared the Python lists without
    remove*. release_panel then returned immediately, and the listeners still
    closed over the session. Why: dispose releases them, and a later
    release_panel does not remove them a second time.
    """
    controller = _HoldingController()
    frame = _frame(controller)
    session = open_frame_session(frame, "doc-a")
    query = _HoldingControl()
    leave = _HoldingControl()
    panel = object()
    session.bind_panel(panel)
    session.set_focus_pin(query)
    _install(session, query, leave)
    assert query.listeners
    assert leave.listeners
    assert controller.handlers

    frame.addEventListener.call_args[0][0].disposing(None)

    assert session_for_frame(frame) is None
    assert query.listeners == []
    assert leave.listeners == []
    assert controller.handlers == []
    assert session._query_listener is None
    assert session._leave == []
    assert session._click_handler is None
    assert session._trackers == []
    assert session._closed is True
    frame.removeEventListener.assert_not_called()

    query_removed = list(query.removed)
    leave_removed = list(leave.removed)
    click_removed = list(controller.removed)
    session.release_panel(panel, query)
    assert query.removed == query_removed
    assert leave.removed == leave_removed
    assert controller.removed == click_removed


def test_frame_disposing_clears_listeners_when_controls_are_already_dead():
    """A control that is already disposed must not crash frame close.

    remove* raises. The session still drops its lists so a later
    release_panel does not call remove* again.
    """
    controller = _HoldingController(dead=True)
    frame = _frame(controller)
    session = open_frame_session(frame, "doc-a")
    query = _HoldingControl(dead=True)
    leave = _HoldingControl(dead=True)
    panel = object()
    session.bind_panel(panel)
    _install(session, query, leave)
    assert query.listeners
    assert leave.listeners
    assert controller.handlers

    frame.addEventListener.call_args[0][0].disposing(None)

    assert session_for_frame(frame) is None
    assert session._query_listener is None
    assert session._query_control is None
    assert session._leave == []
    assert session._click_handler is None
    assert session._click_controller is None
    assert session._trackers == []
    assert session.panel is None
    assert session.focus_pin is None
    # The dead controls still hold the listeners; we must not try again.
    session.release_panel(panel, query)
    frame.addEventListener.call_args[0][0].disposing(None)
    assert query.removed == []
    assert leave.removed == []
    assert controller.removed == []


def test_explicit_release_removes_only_that_sessions_listeners():
    controller = _Controller()
    session = open_frame_session(_frame(controller), "doc-a")
    query = MagicMock()
    leave = MagicMock()
    panel = object()
    session.bind_panel(panel)
    session.set_focus_pin(query)
    _install(session, query, leave)
    click = controller.added[0]
    mouse = leave.addMouseListener.call_args[0][0]
    focus = leave.addFocusListener.call_args[0][0]
    session.release_panel(panel, query)
    assert controller.removed == [click]
    leave.removeMouseListener.assert_called_once_with(mouse)
    leave.removeFocusListener.assert_called_once_with(focus)
    query.removeFocusListener.assert_called_once()


def test_leave_query_rolls_back_when_focus_attach_throws():
    session = FrameSession(MagicMock(), "doc-a")
    query = MagicMock()
    leave = MagicMock()
    leave.addFocusListener.side_effect = RuntimeError("focus attach failed")
    _install(session, query, leave)
    mouse = leave.addMouseListener.call_args[0][0]
    leave.removeMouseListener.assert_called_once_with(mouse)
    assert session._leave == []
    assert mouse not in session._trackers


def test_frame_disposing_drops_only_that_session():
    controller_a = _Controller()
    controller_b = _Controller()
    frame_a = _frame(controller_a)
    frame_b = _frame(controller_b)
    session_a = open_frame_session(frame_a, "doc-a")
    session_b = open_frame_session(frame_b, "doc-b")
    query_a = MagicMock()
    query_b = MagicMock()
    session_a.set_focus_pin(query_a)
    session_b.set_focus_pin(query_b)
    _install(session_a, query_a)
    _install(session_b, query_b)
    listener_a = frame_a.addEventListener.call_args[0][0]
    listener_a.disposing(None)
    assert session_for_frame(frame_a) is None
    assert session_for_frame(frame_b) is session_b
    # Frame close releases this session's listeners. The other frame stays.
    assert controller_a.removed == [controller_a.added[0]]
    assert controller_b.removed == []
    query_a.removeFocusListener.assert_called_once()
    query_b.removeFocusListener.assert_not_called()
    frame_a.removeEventListener.assert_not_called()
    assert session_b.focus_pin is query_b
    session_b.restore_focus()
    query_b.setFocus.assert_called_once()


def _thread_violation(where: str) -> RuntimeError:
    return RuntimeError(f"UNO thread violation: {where}")


def test_thread_violation_from_get_controller_is_not_a_silent_install():
    """Off-thread getController used to log at debug and skip the click handler."""
    frame = MagicMock()
    frame.getController.side_effect = _thread_violation("getController")
    session = FrameSession(frame, "doc-a")
    query = MagicMock()
    with pytest.raises(RuntimeError, match="UNO thread violation"):
        _install(session, query)
    assert session._click_handler is None


def test_thread_violation_from_add_focus_listener_is_not_a_silent_install():
    session = FrameSession(MagicMock(), "doc-a")
    query = MagicMock()
    query.addFocusListener.side_effect = _thread_violation("addFocusListener")
    with pytest.raises(RuntimeError, match="UNO thread violation"):
        _install(session, query)
    assert session._query_listener is None
    assert session._trackers == []


def test_thread_violation_on_leave_query_rolls_back_then_raises():
    """A thread violation after addMouseListener must not leave that listener on."""
    session = FrameSession(MagicMock(), "doc-a")
    query = MagicMock()
    leave = MagicMock()
    leave.addFocusListener.side_effect = _thread_violation("addFocusListener")
    with pytest.raises(RuntimeError, match="UNO thread violation"):
        _install(session, query, leave)
    mouse = leave.addMouseListener.call_args[0][0]
    leave.removeMouseListener.assert_called_once_with(mouse)
    assert session._leave == []
    assert mouse not in session._trackers


def test_thread_violation_from_add_mouse_listener_is_not_a_silent_install():
    session = FrameSession(MagicMock(), "doc-a")
    query = MagicMock()
    leave = MagicMock()
    leave.addMouseListener.side_effect = _thread_violation("addMouseListener")
    with pytest.raises(RuntimeError, match="UNO thread violation"):
        _install(session, query, leave)
    assert session._leave == []
    leave.addFocusListener.assert_not_called()


def test_thread_violation_from_add_mouse_click_handler_is_not_a_silent_install():
    class _Boom(_Controller):
        def addMouseClickHandler(self, handler: object) -> None:
            raise _thread_violation("addMouseClickHandler")

    controller = _Boom()
    session = FrameSession(_frame(controller), "doc-a")
    query = MagicMock()
    with pytest.raises(RuntimeError, match="UNO thread violation"):
        _install(session, query)
    assert session._click_handler is None
    assert session._click_controller is None


def test_ordinary_attach_error_stays_a_debug_log(caplog):
    """A non-guard RuntimeError is still not a failed install."""
    # What was wrong: logger.debug calls are stripped in release builds.
    # Why: skip when running against stripped release bundle.
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("log.debug stripped in release bundle")
    import logging

    session = FrameSession(MagicMock(), "doc-a")
    query = MagicMock()
    query.addFocusListener.side_effect = RuntimeError("focus attach failed")
    with caplog.at_level(logging.DEBUG, logger="writeragent.frame_session"):
        _install(session, query)
    assert session._query_listener is None
    assert any(record.levelno == logging.DEBUG and "query focus listener" in record.message for record in caplog.records)


def test_close_listener_failure_is_not_left_open():
    frame = MagicMock()
    frame.addEventListener.side_effect = RuntimeError("add failed")
    session = open_frame_session(frame, "doc-a")
    frame.addEventListener.assert_called_once()
    assert session_for_frame(frame) is None
    assert session._frame_listener is None
    assert session._closed is False


def test_frame_without_close_api_is_not_tracked():
    class _Bare:
        pass

    frame = _Bare()
    session = open_frame_session(frame, "doc-a")
    assert session.frame is frame
    assert session_for_frame(frame) is None


def test_close_listener_import_failure_is_not_tracked():
    frame = MagicMock()
    with patch.dict(sys.modules, {"unohelper": None}):
        session = open_frame_session(frame, "doc-a")
    frame.addEventListener.assert_not_called()
    assert session.frame is frame
    assert session_for_frame(frame) is None


def test_thread_violation_on_close_listener_is_not_left_open():
    frame = MagicMock()
    frame.addEventListener.side_effect = _thread_violation("addEventListener")
    with pytest.raises(RuntimeError, match="UNO thread violation"):
        open_frame_session(frame, "doc-a")
    assert session_for_frame(frame) is None


def test_second_sidebar_still_attaches_when_the_first_listener_is_live():
    session_a = open_frame_session(_frame(_Controller()), "doc-a")
    session_b = open_frame_session(_frame(_Controller()), "doc-b")
    query_a = MagicMock()
    query_b = MagicMock()
    _install(session_a, query_a)
    _install(session_b, query_b)
    query_a.addFocusListener.assert_called_once()
    query_b.addFocusListener.assert_called_once()
    _install(session_b, query_b)
    query_b.addFocusListener.assert_called_once()


def _active_session(doc: str = "doc") -> tuple[FrameSession, MagicMock, MagicMock]:
    frame = _frame(_Controller())
    session = open_frame_session(frame, doc)
    query = MagicMock(name="query-" + doc)
    session.set_focus_pin(query)
    session.note_user_wants_query()
    return session, frame, query


def test_restore_focus_skips_when_another_window_is_active():
    """BUG A: a streaming background doc must not GrabFocus its Ask field."""
    session, frame, query = _active_session()
    _set_active_top(frame, MagicMock(name="other-doc-window"))
    session.restore_focus()
    query.setFocus.assert_not_called()


def test_restore_focus_skips_when_office_is_not_the_focused_app():
    session, frame, query = _active_session()
    _set_active_top(frame, None)
    session.restore_focus()
    query.setFocus.assert_not_called()


def test_restore_focus_skips_when_the_window_probe_fails():
    session, frame, query = _active_session()
    frame.getContainerWindow.side_effect = RuntimeError("disposed")
    session.restore_focus()
    query.setFocus.assert_not_called()


def test_restore_focus_off_main_thread_makes_no_uno_calls():
    session, frame, query = _active_session()
    frame.getContainerWindow.reset_mock()
    with patch("plugin.framework.thread_guard.on_main_thread", return_value=False):
        session.restore_focus()
    frame.getContainerWindow.assert_not_called()
    query.setFocus.assert_not_called()


def test_restore_focus_resumes_when_the_user_returns_to_the_frame():
    """Skipping while inactive keeps the restore flag, so no new Ask click is needed."""
    session, frame, query = _active_session()
    container = frame.getContainerWindow.return_value
    _set_active_top(frame, MagicMock(name="other-doc-window"))
    session.restore_focus()
    _set_active_top(frame, container)
    session.restore_focus()
    query.setFocus.assert_called_once()


def test_only_the_active_frame_restores_focus():
    session_a, frame_a, query_a = _active_session("doc-a")
    session_b, frame_b, query_b = _active_session("doc-b")
    # The user switched to doc A while doc B streams.
    _set_active_top(frame_b, frame_a.getContainerWindow.return_value)
    session_a.restore_focus()
    session_b.restore_focus()
    query_a.setFocus.assert_called_once()
    query_b.setFocus.assert_not_called()


def test_frame_is_active_window_uses_uno_identity_for_new_wrappers():
    """PyUNO returns a fresh wrapper per call; identity goes through uno_same."""
    session, frame, _query = _active_session()
    wrapper = MagicMock(name="same-window-new-wrapper")
    _set_active_top(frame, wrapper)
    with patch("plugin.framework.uno_context.uno_same", return_value=True) as mock_same:
        assert session.frame_is_active_window() is True
    mock_same.assert_called_once_with(wrapper, frame.getContainerWindow.return_value)


def test_watch_frame_returns_false_if_disposing_fires_immediately():
    frame = MagicMock()
    def _add_listener(listener):
        listener.disposing(None)
    frame.addEventListener.side_effect = _add_listener
    session = open_frame_session(frame, "doc-a")
    assert session_for_frame(frame) is None
    assert session._frame_listener is None
    assert session._closed is True

def test_install_bails_out_if_session_closed():
    session = FrameSession(MagicMock(), "doc-a")
    session.dispose()
    query = MagicMock()
    _install(session, query)
    query.addFocusListener.assert_not_called()

def test_rollback_leave_does_not_drop_unrelated_listeners_with_none_slots():
    session = FrameSession(MagicMock(), "doc-a")
    control1 = MagicMock()
    focus_track1 = MagicMock()
    session._leave.append((control1, None, focus_track1))

    control2 = MagicMock()
    mouse_track2 = MagicMock()
    focus_track2 = MagicMock()
    session._leave.append((control2, mouse_track2, focus_track2))

    session._rollback_leave(control2, mouse_track2, focus_track2)
    assert len(session._leave) == 1
    assert session._leave[0][0] is control1

def test_attach_leave_query_deduplicates_using_uno_identity():
    session = FrameSession(MagicMock(), "doc-a")
    leave1 = MagicMock(name="wrapper1")
    leave2 = MagicMock(name="wrapper2")
    query = MagicMock()

    with patch("plugin.framework.uno_context.uno_same", return_value=True):
        _install(session, query, leave1)
        _install(session, query, leave2)

    # First attach adds mouse listener and focus listener
    leave1.addMouseListener.assert_called_once()
    leave1.addFocusListener.assert_called_once()

    # Second attach should not be called because uno_same returns True
    leave2.addMouseListener.assert_not_called()
    leave2.addFocusListener.assert_not_called()

def test_dispose_handles_thread_violation_in_release_listeners():
    # Setup
    session, frame, query = _active_session("doc-a")
    leave_ctrl = MagicMock()
    leave_mouse = MagicMock()
    leave_focus = MagicMock()
    session._leave.append((leave_ctrl, leave_mouse, leave_focus))

    click_ctrl = MagicMock()
    click_handler = MagicMock()
    session._click_controller = click_ctrl
    session._click_handler = click_handler

    session._query_listener = MagicMock()
    session._query_control = MagicMock()
    session._query_control.removeFocusListener.side_effect = _thread_violation("removeFocusListener")

    with pytest.raises(RuntimeError, match="UNO thread violation"):
        session.dispose()

    leave_ctrl.removeMouseListener.assert_called_once_with(leave_mouse)
    leave_ctrl.removeFocusListener.assert_called_once_with(leave_focus)
    click_ctrl.removeMouseClickHandler.assert_called_once_with(click_handler)

    assert session._frame_listener is None
    assert session.panel is None
    assert session_for_frame(frame) is None


def test_remove_close_hook_drops_registered_hook():
    session = FrameSession(MagicMock(), "doc-a")
    hook1 = MagicMock()
    hook2 = MagicMock()
    session.add_close_hook(hook1)
    session.add_close_hook(hook2)
    assert len(session._close_hooks) == 2

    session.remove_close_hook(hook1)
    assert session._close_hooks == [hook2]

    # Removing a hook not present is a no-op
    session.remove_close_hook(hook1)
    assert session._close_hooks == [hook2]


def test_release_panel_removes_registered_close_hook():
    session = FrameSession(MagicMock(), "doc-a")
    panel = MagicMock()
    hook = MagicMock()
    panel._frame_close_hook = hook
    session.add_close_hook(hook)
    assert len(session._close_hooks) == 1

    session.release_panel(panel)
    assert len(session._close_hooks) == 0
    assert getattr(panel, "_frame_close_hook", None) is None


def test_frame_close_disposing_swallows_dispose_failure(caplog):
    frame = MagicMock()
    session = open_frame_session(frame, "doc-a")
    assert session._frame_listener is not None

    with patch.object(session, "dispose", side_effect=RuntimeError("simulated teardown error")):
        # Must not raise into UNO caller
        session._frame_listener.disposing(None)

    assert any(
        record.levelno == logging.ERROR and "unhandled exception in disposing" in record.message
        for record in caplog.records
    )
