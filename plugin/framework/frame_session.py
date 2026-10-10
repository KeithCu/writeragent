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

Focus pins, click handlers, and the chatbot sidebar belong to the frame
the sidebar was given, not to process globals (``_default_focus_restore``,
``_stream_focus_trackers``, ``panels[0]``) aimed with
``Desktop.getCurrentComponent()`` or ``get_active_document()``. A handler
installed for whichever document was current would stay for the process; a
second sidebar would skip its listener because the first was still set;
dispose would clear the other window's pin; ``uno_same`` off the main
thread would fall through to ``panels[0]``. This object is created when
that frame is opened and destroyed when it closes. It owns the listeners,
the focus pin, and the panel. Dispose removes only this session.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

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
    must not share that object. A frame whose close listener did not attach
    is also not stored: ``dispose`` runs only from that listener, so an
    unwatched session in ``_OPEN`` would leak until process exit.
    """
    if frame is not None:
        found = session_for_frame(frame)
        if found is not None:
            if doc_uid and not found.doc_uid:
                found.doc_uid = str(doc_uid)
            return found
    session = FrameSession(frame, doc_uid)
    if frame is not None:
        # Append first so a disposing callback inside addEventListener can
        # forget this session. Drop it again when the listener never stuck.
        _OPEN.append(session)
        try:
            attached = session.watch_frame()
        except Exception:
            _forget(session)
            raise
        if not attached:
            _forget(session)
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


def _is_attach_thread_violation(exc: BaseException) -> bool:
    """True when *exc* is the main-thread guard, not a missing UNO method.

    Off the main thread, ``getController``, ``addFocusListener``,
    ``addMouseListener``, and ``addMouseClickHandler`` raise ``RuntimeError``
    (``UNO thread violation``). That is the thread guard: callers re-raise it.
    Catching it and logging at debug would leave the session with no listener
    while ``install`` looked successful. Any other attach error stays a debug log.
    """
    from plugin.framework.uno_listeners import listener_boundary

    boundary = listener_boundary(exc)
    return boundary is not None and boundary.kind == "thread"


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
        self._close_hooks: list[Callable[[], None]] = []

    def add_close_hook(self, hook: Callable[[], None]) -> None:
        if self._closed:
            hook()
            return
        self._close_hooks.append(hook)

    def remove_close_hook(self, hook: Callable[[], None]) -> None:
        """Drop *hook* from close callbacks (no-op if absent)."""
        if hook in self._close_hooks:
            self._close_hooks.remove(hook)

    def bind_panel(self, panel: Any) -> None:
        """This sidebar is the one the session owns."""
        self.panel = panel

    def set_focus_pin(self, control: Any) -> None:
        """Ask field restored after a stream scroll on this frame only."""
        self.focus_pin = control

    def clear_focus_pin_if(self, control: Any) -> None:
        """Clear the pin only when it is still *control*.

        Only this session's pin. A second window closing must not clear the
        first window's query field.
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

    def frame_is_active_window(self) -> bool:
        """True when this frame's window is LibreOffice's active top-level window.

        Main thread only: off it this returns False, so callers skip
        ``setFocus`` rather than touch UNO from a worker.

        ``getActiveTopWindow()`` is None when another application has the
        focus, and a dialog of this office (Options, a message box) is its own
        top window. Both count as "not this frame", so a stream never pulls
        focus out of them either. Any UNO failure also counts as "not active":
        losing one caret restore is cheap, stealing focus is the bug.
        """
        from plugin.framework.thread_guard import on_main_thread

        if self._closed or self.frame is None or not on_main_thread():
            return False
        try:
            container = self.frame.getContainerWindow()
            if container is None:
                return False
            # The window peer hands out its toolkit, so no component context
            # is needed here (XToolkit2 includes XExtendedToolkit).
            toolkit = container.getToolkit()
            top = toolkit.getActiveTopWindow() if toolkit is not None else None
        except Exception as exc:
            log.debug("stream focus: active window probe failed doc=%s: %s", self.doc_uid, exc)
            return False
        # _same_frame is the main-thread uno_same identity test; PyUNO hands
        # out a new wrapper per call, so ``is`` alone would always miss.
        return _same_frame(top, container)

    def restore_focus(self) -> None:
        """Put the caret back in this frame's Ask field.

        Closes over this session. Callers do not pass a frame or look up the
        current component.

        Restore only while this frame's container window is the active top
        window. While doc B streamed, every chunk called ``query.setFocus()``
        here about 3 times with no check that B was the window the user was
        in. VCL's GrabFocus pulls keyboard focus into a background top window,
        so a window the user switched to was raised but never activated, and
        its Window menu would not open until B finished. Do not clear the
        restore flag: when the user comes back to B, chunks should keep the
        caret in Ask again without a fresh click there.
        """
        if self._closed or not self._restore_query:
            return
        query = self.focus_pin
        if query is None or not hasattr(query, "setFocus"):
            return
        if not self.frame_is_active_window():
            log.debug("stream focus: skip restore, frame not active doc=%s", self.doc_uid)
            return
        try:
            query.setFocus()
            log.debug("FrameSession.restore_focus doc=%s", self.doc_uid)
        except Exception as exc:
            log.debug("FrameSession.restore_focus: %s", exc)

    def watch_frame(self) -> bool:
        """Destroy this session when the frame itself is disposed.

        Returns True only when the close listener is attached. False means
        the caller must not leave this session in ``_OPEN``.

        ``disposing`` runs while this broadcaster drops listeners. Do not
        ``removeEventListener`` on the frame from that callback. Sidebar
        listeners are on other controls; :meth:`dispose` releases those.
        """
        frame = self.frame
        if frame is None or not hasattr(frame, "addEventListener"):
            return False
        try:
            import unohelper
            from com.sun.star.lang import XEventListener
        except ImportError:
            return False
        if unohelper is None or XEventListener is None:
            return False

        session = self

        from plugin.framework.uno_listeners import _catch_and_log

        # Other listeners use Base*Listener, which wraps callbacks in
        # _catch_and_log. A bare disposing() lets a listener-removal or
        # thread-violation error into the UNO bridge and aborts teardown
        # of the remaining listeners.
        class _FrameClose(unohelper.Base, XEventListener):  # type: ignore[misc]
            @_catch_and_log
            def disposing(self, Source: Any) -> None:  # noqa: N803 -- UNO signature
                session.dispose()

        try:
            listener = _FrameClose()
            frame.addEventListener(listener)
            if self._closed:
                return False
            self._frame_listener = listener
        except Exception as exc:
            # Same guard as the focus/click attaches: a thread violation is
            # not "this frame has no close listener".
            if _is_attach_thread_violation(exc):
                raise
            log.debug("frame close listener", exc_info=True)
            return False
        return True

    def install(self, ctx: Any, query: Any = None, leave_query_controls: Any = None) -> None:
        """Attach this frame's focus and click listeners.

        *ctx* is the extension context the panel already holds. The document
        controller comes from ``frame.getController()``, not from *ctx* via
        ``getCurrentComponent()``. A ``UNO thread violation`` from
        ``getController`` / ``addFocusListener`` / ``addMouseListener`` /
        ``addMouseClickHandler`` propagates; this method does not return as
        if those listeners were installed. In-tree callers run on the main
        thread, where that guard does not fire.
        """
        if self._closed:
            return
        del ctx  # the frame, not Desktop, names the document
        self._attach_query_listener(query)
        for control in leave_query_controls or ():
            if control is not None and control is not query:
                self._attach_leave_query(control)
        self._attach_click_handler()
        log.debug("frame session listeners n=%d", len(self._trackers))

    def forget_listener(self, listener: Any) -> None:
        """Drop *listener* from this session. Do not call ``remove*``.

        Disposing walks the listener list and notifies each one. Calling
        ``removeMouseListener`` / ``removeFocusListener`` /
        ``removeMouseClickHandler`` from ``on_disposing`` hits a broadcaster
        that is already tearing those listeners down. Only the Python lists
        change here. ``remove*`` stays on :meth:`release_listeners`. Panel
        teardown calls it while the controls are alive. Frame :meth:`dispose`
        calls it too; :meth:`_remove` ignores a control that is already dead.
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
        first_exc = None

        def _safe_remove(control: Any, method: str, listener: Any) -> None:
            nonlocal first_exc
            try:
                self._remove(control, method, listener)
            except Exception as exc:
                if first_exc is None:
                    first_exc = exc

        if self._query_listener is not None:
            _safe_remove(self._query_control, "removeFocusListener", self._query_listener)
        for control, mouse, focus in list(self._leave):
            _safe_remove(control, "removeMouseListener", mouse)
            _safe_remove(control, "removeFocusListener", focus)
        if self._click_handler is not None:
            _safe_remove(self._click_controller, "removeMouseClickHandler", self._click_handler)
        self._query_listener = None
        self._query_control = None
        self._leave.clear()
        self._click_handler = None
        self._click_controller = None
        self._trackers.clear()

        if first_exc is not None:
            raise first_exc

    def release_panel(self, panel: Any, query_control: Any = None) -> None:
        """Sidebar gone. Drop this panel's pin and listeners only.

        The session stays until the frame closes so a reopened deck on the
        same frame is the same object. Another frame's session is not in
        this method.

        ``open_frame_session`` returns the existing session for the frame, so
        the old element's dispose and the new element's listeners share one
        object. A late dispose of the previous sidebar must not call
        ``release_listeners`` after ``bind_panel`` has pointed this session
        at the new sidebar: the ``self.panel is panel`` check keeps the new
        panel, and those listeners belong to it. Only that panel's dispose
        removes them. ``clear_focus_pin_if`` stays scoped to the control
        that is still the pin.
        """
        if self._closed:
            return
        self.clear_focus_pin_if(query_control)
        # Each sidebar build appends an on_frame_close closure. Drop this
        # panel's hook or the old panel and send_listener stay alive across
        # deck reopens.
        hook = getattr(panel, "_frame_close_hook", None)
        if hook is not None:
            self.remove_close_hook(hook)
            panel._frame_close_hook = None
        if self.panel is not panel:
            return
        self.panel = None
        self.release_listeners()

    def dispose(self) -> None:
        """Frame closed. Forget this session and no other.

        The only caller is the frame ``disposing`` callback. That walk is
        already dropping the frame listener, so this method does not call
        ``removeEventListener`` on the frame. Sidebar listeners are a
        different broadcaster.

        Frame ``disposing`` often runs before sidebar teardown. Setting
        ``_closed`` and clearing the Python lists without
        :meth:`release_listeners` makes :meth:`release_panel` return
        immediately, so the query, leave, and click listeners stay
        registered. Those listeners close over this session and keep it
        alive after the frame is gone. Release them here, the same way
        :meth:`release_panel` does. :meth:`_remove` swallows a dead control,
        so a control that is already disposing does not raise, and a later
        :meth:`release_panel` does not remove them again. ``_closed`` is set
        first so a ``remove*`` that re-enters :meth:`release_panel` cannot
        start a second remove.
        """
        if self._closed:
            return
        self._closed = True
        for hook in list(self._close_hooks):
            try:
                hook()
            except Exception:
                log.debug("Error in FrameSession close hook", exc_info=True)
        self._close_hooks.clear()
        try:
            self.release_listeners()
        finally:
            self._frame_listener = None
            self.focus_pin = None
            self.panel = None
            _forget(self)

    def _controller(self) -> Any:
        """Controller for this frame. Never ``Desktop.getCurrentComponent()``.

        This frame's controller, not whichever one was current when the first
        sidebar installed. Subscribing that controller means clicks in this
        window never clear stream focus, and later chunks call ``setFocus``
        on the other window's Ask field.
        """
        frame = self.frame
        if frame is None:
            return None
        try:
            return frame.getController()
        except Exception as exc:
            if _is_attach_thread_violation(exc):
                raise
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
        except Exception as exc:
            if _is_attach_thread_violation(exc):
                raise
            log.debug("frame session %s", method, exc_info=True)

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
        except Exception as exc:
            if _is_attach_thread_violation(exc):
                raise
            log.debug("query focus listener", exc_info=True)

    def _attach_leave_query(self, control: Any) -> None:
        """Stop restoring Ask when the pointer is on this sidebar control.

        Roll back whichever half attached. If ``addFocusListener`` throws
        after ``addMouseListener`` succeeded and the mouse listener is not
        recorded, dispose never removes it.
        """
        if control is None:
            return
        for existing, _mouse, _focus in self._leave:
            if self._same_controller(existing, control):
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
        except Exception as exc:
            # Roll back a half-attached pair first, then surface a thread
            # violation. A normal failure stays a debug log after the rollback.
            self._rollback_leave(control, mouse_track, focus_track)
            if _is_attach_thread_violation(exc):
                raise
            log.debug("leave-query listeners", exc_info=True)

    def _rollback_leave(self, control: Any, mouse_track: Any, focus_track: Any) -> None:
        first_exc = None

        def _safe_remove(ctrl: Any, method: str, listener: Any) -> None:
            nonlocal first_exc
            try:
                self._remove(ctrl, method, listener)
            except Exception as exc:
                if first_exc is None:
                    first_exc = exc

        _safe_remove(control, "removeFocusListener", focus_track)
        _safe_remove(control, "removeMouseListener", mouse_track)
        self._drop_tracker(focus_track)
        self._drop_tracker(mouse_track)
        self._leave = [row for row in self._leave if not (row[0] is control and row[1] is mouse_track and row[2] is focus_track)]

        if first_exc is not None:
            raise first_exc

    def _attach_click_handler(self) -> None:
        """Page click on this frame's controller calls :meth:`note_user_left_query`.

        Subscribe this frame's controller, and remove the handler with the
        session. Adding it once to whichever controller
        ``getCurrentComponent()`` returned leaves a later window unsubscribed,
        so its clicks do not stop stream ``setFocus``.
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
        except Exception as exc:
            self._remove(controller, "removeMouseClickHandler", handler)
            self.forget_listener(handler)
            if _is_attach_thread_violation(exc):
                raise
            log.debug("document click handler", exc_info=True)
