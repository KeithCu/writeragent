# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""One session per open frame.

What was wrong: focus pins, click handlers, and the chatbot sidebar lived in
process globals (``_default_focus_restore``, ``_stream_focus_trackers``,
``panels[0]``) and were aimed with ``Desktop.getCurrentComponent()`` or
``get_active_document()``. How: a handler installed for whichever document
was current stayed for the process; a second sidebar skipped its listener
because the first was still set; dispose cleared the other window's pin;
``uno_same`` off the main thread fell through to ``panels[0]``. Why: the
frame the sidebar was given already identifies the window. This object is
created when that frame is opened and destroyed when it closes. It owns the
listeners, the focus pin, and the panel. Dispose removes only this session.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("writeragent.frame_session")

_OPEN: list[FrameSession] = []


def _same_frame(left: Any, right: Any) -> bool:
    """True when *left* and *right* are the same frame.

    Off the main thread ``uno_same`` raises. Treating that as a miss used to
    fall through to ``panels[0]``. Identity here is ``is`` off the main
    thread, and ``uno_same`` only on it.
    """
    if left is None or right is None:
        return False
    if left is right:
        return True
    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        return False
    try:
        from plugin.framework.uno_context import uno_same

        return bool(uno_same(left, right))
    except Exception:
        log.debug("frame identity", exc_info=True)
        return False


def document_id_for_frame(frame: Any) -> str:
    """RuntimeUID of the model on *frame*, or ``""``.

    The id comes from the frame's controller. ``getCurrentComponent()`` is a
    different window when focus has moved.
    """
    if frame is None:
        return ""
    try:
        from plugin.framework.thread_guard import on_main_thread

        if not on_main_thread():
            return ""
        from plugin.framework.uno_context import get_document_from_frame, get_runtime_uid

        model = get_document_from_frame(frame)
        if model is None:
            return ""
        return get_runtime_uid(model)
    except Exception:
        log.debug("document id for frame", exc_info=True)
        return ""


def session_for_frame(frame: Any) -> FrameSession | None:
    """The open session for *frame*, or None."""
    if frame is None:
        return None
    for session in list(_OPEN):
        if _same_frame(session.frame, frame):
            return session
    return None


def open_frame_session(frame: Any, doc_uid: str = "") -> FrameSession:
    """Return the session for *frame*, creating it the first time the frame is seen.

    A missing frame gets a private session that is not stored. Two windows
    must not share that object.
    """
    if frame is not None:
        found = session_for_frame(frame)
        if found is not None:
            if doc_uid and not found.doc_uid:
                found.doc_uid = str(doc_uid)
            return found
    session = FrameSession(frame, doc_uid)
    if frame is not None:
        _OPEN.append(session)
        session.watch_frame()
    return session


def reset_frame_sessions() -> None:
    """Test hook: drop every open session without touching UNO."""
    for session in list(_OPEN):
        session._closed = True
    _OPEN.clear()


def _forget(session: FrameSession) -> None:
    try:
        _OPEN.remove(session)
    except ValueError:
        pass


class FrameSession:
    """Listeners, focus pin, and sidebar for a single frame."""

    frame: Any
    doc_uid: str
    panel: Any
    focus_pin: Any
    _restore_query: bool
    _closed: bool

    def __init__(self, frame: Any, doc_uid: str = "") -> None:
        self.frame = frame
        self.doc_uid = str(doc_uid or "")
        self.panel = None
        self.focus_pin = None
        self._restore_query = True
        self._query_control: Any = None
        self._query_listener: Any = None
        self._leave: list[tuple[Any, Any, Any]] = []
        self._click_controller: Any = None
        self._click_handler: Any = None
        self._trackers: list[Any] = []
        self._frame_listener: Any = None
        self._closed = False

    def bind_panel(self, panel: Any) -> None:
        """This sidebar is the one the session owns."""
        self.panel = panel

    def set_focus_pin(self, control: Any) -> None:
        """Ask field restored after a stream scroll on this frame only."""
        self.focus_pin = control

    def clear_focus_pin_if(self, control: Any) -> None:
        """Clear the pin only when it is still *control*.

        What was wrong: every sidebar dispose set the process pin to None,
        including a second window closing while the first window's query
        field was the pin.
        """
        if control is not None and self.focus_pin is control:
            self.focus_pin = None

    def note_user_wants_query(self) -> None:
        """This frame's Ask field should take the caret after a stream scroll."""
        self._restore_query = True

    def note_user_left_query(self) -> None:
        """Stop restoring this frame's Ask field. Other frames are unchanged.

        Stream chunks call :meth:`restore_focus`. A process-wide flag meant a
        click in window B aborted window A's caret restore, and the reverse.
        """
        if not self._restore_query:
            return
        self._restore_query = False
        log.debug("stream focus: left query")

    def restore_focus(self) -> None:
        """Put the caret back in this frame's Ask field.

        Closes over this session. Callers do not pass a frame or look up the
        current component.
        """
        if self._closed or not self._restore_query:
            return
        query = self.focus_pin
        if query is None or not hasattr(query, "setFocus"):
            return
        try:
            query.setFocus()
            log.debug("FrameSession.restore_focus")
        except Exception as exc:
            log.debug("FrameSession.restore_focus: %s", exc)

    def watch_frame(self) -> None:
        """Destroy this session when the frame itself is disposed.

        ``disposing`` runs while the broadcaster drops listeners. Do not
        ``removeListener`` from that callback.
        """
        frame = self.frame
        if frame is None or not hasattr(frame, "addEventListener"):
            return
        try:
            import unohelper
            from com.sun.star.lang import XEventListener
        except ImportError:
            return
        if unohelper is None or XEventListener is None:
            return

        session = self

        class _FrameClose(unohelper.Base, XEventListener):  # type: ignore[misc]
            def disposing(self, Source: Any) -> None:  # noqa: N803 -- UNO signature
                session.dispose(from_frame_disposing=True)

        try:
            listener = _FrameClose()
            frame.addEventListener(listener)
            self._frame_listener = listener
        except Exception:
            log.debug("frame close listener", exc_info=True)

    def install(self, ctx: Any, query: Any = None, leave_query_controls: Any = None) -> None:
        """Attach this frame's focus and click listeners.

        *ctx* is the extension context the panel already holds. The document
        controller comes from ``frame.getController()``, not from *ctx* via
        ``getCurrentComponent()``.
        """
        del ctx  # the frame, not Desktop, names the document
        self._attach_query_listener(query)
        for control in leave_query_controls or ():
            if control is not None and control is not query:
                self._attach_leave_query(control)
        self._attach_click_handler()
        log.debug("frame session listeners n=%d", len(self._trackers))

    def forget_listener(self, listener: Any) -> None:
        """Drop *listener* from this session. Do not call ``remove*``.

        What was wrong: ``on_disposing`` called ``removeMouseListener`` /
        ``removeFocusListener`` / ``removeMouseClickHandler`` while UNO was
        already disposing the broadcaster. How: disposing walks the listener
        list and notifies each one. Why: only the Python lists change here.
        ``remove*`` stays on :meth:`release_listeners`, which runs from our
        own teardown before the broadcaster is disposing.
        """
        if listener is None:
            return
        if self._query_listener is listener:
            self._query_listener = None
            self._query_control = None
        self._leave = [row for row in self._leave if row[1] is not listener and row[2] is not listener]
        if self._click_handler is listener:
            self._click_handler = None
            self._click_controller = None
        self._drop_tracker(listener)

    def release_listeners(self) -> None:
        """Remove this session's listeners from controls that are still alive."""
        if self._query_listener is not None:
            self._remove(self._query_control, "removeFocusListener", self._query_listener)
        for control, mouse, focus in list(self._leave):
            self._remove(control, "removeMouseListener", mouse)
            self._remove(control, "removeFocusListener", focus)
        if self._click_handler is not None:
            self._remove(self._click_controller, "removeMouseClickHandler", self._click_handler)
        self._query_listener = None
        self._query_control = None
        self._leave.clear()
        self._click_handler = None
        self._click_controller = None
        self._trackers.clear()

    def release_panel(self, panel: Any, query_control: Any = None) -> None:
        """Sidebar gone. Drop this panel's pin and listeners only.

        The session stays until the frame closes so a reopened deck on the
        same frame is the same object. Another frame's session is not in
        this method.
        """
        if self._closed:
            return
        if self.panel is panel:
            self.panel = None
        self.clear_focus_pin_if(query_control)
        self.release_listeners()

    def dispose(self, *, from_frame_disposing: bool = False) -> None:
        """Frame closed. Forget this session and no other."""
        if self._closed:
            return
        self._closed = True
        if from_frame_disposing:
            self._query_listener = None
            self._query_control = None
            self._leave.clear()
            self._click_handler = None
            self._click_controller = None
            self._trackers.clear()
            self._frame_listener = None
        else:
            self.release_listeners()
            self._remove_frame_listener()
        self.focus_pin = None
        self.panel = None
        _forget(self)

    def _controller(self) -> Any:
        """Controller for this frame. Never ``Desktop.getCurrentComponent()``.

        What was wrong: the page-click handler subscribed whichever controller
        was current when the first sidebar installed. Clicks in this window
        never cleared stream focus, and later chunks called ``setFocus`` on
        the other window's Ask field.
        """
        frame = self.frame
        if frame is None:
            return None
        try:
            return frame.getController()
        except Exception as exc:
            log.debug("frame controller: %s", exc)
            return None

    def _same_controller(self, left: Any, right: Any) -> bool:
        if left is None or right is None:
            return False
        if left is right:
            return True
        return _same_frame(left, right)

    def _drop_tracker(self, listener: Any) -> None:
        try:
            self._trackers.remove(listener)
        except ValueError:
            pass

    def _remove(self, control: Any, method: str, listener: Any) -> None:
        if control is None or listener is None:
            return
        try:
            remover = getattr(control, method, None)
            if callable(remover):
                remover(listener)
        except Exception:
            log.debug("frame session %s", method, exc_info=True)

    def _remove_frame_listener(self) -> None:
        listener = self._frame_listener
        self._frame_listener = None
        frame = self.frame
        if listener is None or frame is None:
            return
        try:
            if hasattr(frame, "removeEventListener"):
                frame.removeEventListener(listener)
        except Exception:
            log.debug("remove frame listener", exc_info=True)

    def _attach_query_listener(self, query: Any) -> None:
        if query is None or not hasattr(query, "addFocusListener"):
            return
        if self._query_control is query and self._query_listener is not None:
            return
        if self._query_listener is not None:
            self._remove(self._query_control, "removeFocusListener", self._query_listener)
            self.forget_listener(self._query_listener)
        try:
            import unohelper
            from com.sun.star.awt import XFocusListener
        except ImportError:
            return
        if unohelper is None or XFocusListener is None:
            return

        from plugin.framework.uno_listeners import BaseFocusListener

        session = self

        class _QueryFocus(BaseFocusListener):
            def on_disposing(self, Source: Any) -> None:  # noqa: N803 -- UNO signature
                session.forget_listener(self)

            def on_focus_gained(self, e: Any) -> None:
                session.note_user_wants_query()
                log.debug("stream focus: query")

        try:
            listener = _QueryFocus()
            query.addFocusListener(listener)
            self._trackers.append(listener)
            self._query_control = query
            self._query_listener = listener
        except Exception:
            log.debug("query focus listener", exc_info=True)

    def _attach_leave_query(self, control: Any) -> None:
        """Stop restoring Ask when the pointer is on this sidebar control.

        What was wrong: ``addFocusListener`` threw after ``addMouseListener``
        succeeded, and the mouse listener was not recorded, so dispose never
        removed it. Why: roll back whichever half attached.
        """
        if control is None:
            return
        for existing, _mouse, _focus in self._leave:
            if existing is control:
                return
        try:
            import unohelper
            from com.sun.star.awt import XFocusListener, XMouseListener
        except ImportError:
            return
        if unohelper is None or XFocusListener is None or XMouseListener is None:
            return

        from plugin.framework.uno_listeners import BaseFocusListener, BaseMouseListener

        session = self

        class _LeaveFocus(BaseFocusListener):
            def on_disposing(self, Source: Any) -> None:  # noqa: N803 -- UNO signature
                session.forget_listener(self)

            def on_focus_gained(self, e: Any) -> None:
                session.note_user_left_query()
                log.debug("stream focus: sidebar control")

        class _LeaveMouse(BaseMouseListener):
            def on_disposing(self, Source: Any) -> None:  # noqa: N803 -- UNO signature
                session.forget_listener(self)

            def on_mouse_pressed(self, e: Any) -> None:
                session.note_user_left_query()

            def on_mouse_entered(self, e: Any) -> None:
                session.note_user_left_query()

        mouse_track: Any = None
        focus_track: Any = None
        try:
            if hasattr(control, "addMouseListener"):
                mouse_track = _LeaveMouse()
                control.addMouseListener(mouse_track)
                self._trackers.append(mouse_track)
            if hasattr(control, "addFocusListener"):
                focus_track = _LeaveFocus()
                control.addFocusListener(focus_track)
                self._trackers.append(focus_track)
            if mouse_track is not None or focus_track is not None:
                self._leave.append((control, mouse_track, focus_track))
        except Exception:
            log.debug("leave-query listeners", exc_info=True)
            self._rollback_leave(control, mouse_track, focus_track)

    def _rollback_leave(self, control: Any, mouse_track: Any, focus_track: Any) -> None:
        self._remove(control, "removeFocusListener", focus_track)
        self._remove(control, "removeMouseListener", mouse_track)
        self._drop_tracker(focus_track)
        self._drop_tracker(mouse_track)
        self._leave = [row for row in self._leave if row[1] is not mouse_track and row[2] is not focus_track]

    def _attach_click_handler(self) -> None:
        """Page click on this frame's controller calls :meth:`note_user_left_query`.

        What was wrong: the handler was added once, to whichever controller
        ``getCurrentComponent()`` returned, and never removed. A later window
        never subscribed, so its clicks did not stop stream ``setFocus``.
        """
        try:
            import unohelper
            from com.sun.star.awt import XMouseClickHandler
        except ImportError:
            return
        if unohelper is None or XMouseClickHandler is None:
            return

        controller = self._controller()
        if controller is None or not hasattr(controller, "addMouseClickHandler"):
            return
        if self._click_handler is not None and self._same_controller(self._click_controller, controller):
            return
        if self._click_handler is not None:
            self._remove(self._click_controller, "removeMouseClickHandler", self._click_handler)
            self.forget_listener(self._click_handler)

        from plugin.framework.uno_listeners import BaseMouseClickHandler

        session = self

        class _DocClick(BaseMouseClickHandler):
            def on_disposing(self, Source: Any) -> None:  # noqa: N803 -- UNO signature
                session.forget_listener(self)

            def on_mouse_pressed(self, e: Any) -> bool:
                session.note_user_left_query()
                log.debug("stream focus: document click")
                return False

        handler = _DocClick()
        try:
            controller.addMouseClickHandler(handler)
            self._click_controller = controller
            self._click_handler = handler
            self._trackers.append(handler)
        except Exception:
            log.debug("document click handler", exc_info=True)
            self._remove(controller, "removeMouseClickHandler", handler)
            self.forget_listener(handler)
