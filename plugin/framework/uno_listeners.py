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
"""Unified and exception-safe base classes for UNO listeners to reduce boilerplate.

These base classes provide empty default implementations for standard event callbacks
and apply try/except logging blocks around execution to prevent Python exceptions from
leaking into PyUNO and causing LibreOffice to crash or segfault.

``ListenerBoundary`` is the one signal a generic ``except Exception`` must not
swallow. The listener base classifies the main-thread guard and real disposal
into that type, and re-raises a close or termination veto as the original UNO
exception. An ordinary runtime error stays a logged failure.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, NoReturn, TYPE_CHECKING

from plugin.framework.errors import is_real_disposal
from plugin.framework.thread_guard import THREAD_VIOLATION_MARK

log = logging.getLogger(__name__)

# Safe imports for optional PyUNO environment (ensures unit test compatibility outside of LO)
_unohelper: Any = None
_XEventListener: Any = None
_XActionListener: Any = None
_XItemListener: Any = None
_XTextListener: Any = None
_XKeyListener: Any = None
_XWindowListener: Any = None
_XDocumentEventListener: Any = None
_XActivationEventListener: Any = None
_XContainerListener: Any = None
_XMouseListener: Any = None
_XFocusListener: Any = None
_XMouseClickHandler: Any = None
_XTopWindowListener: Any = None
_XModifyListener: Any = None
_XTerminateListener: Any = None
_HAVE_UNO = False

try:
    import unohelper as _unohelper_impl
    from com.sun.star.lang import XEventListener as _XEventListener_impl
    from com.sun.star.awt import (
        XActionListener as _XActionListener_impl,
        XItemListener as _XItemListener_impl,
        XTextListener as _XTextListener_impl,
        XKeyListener as _XKeyListener_impl,
        XWindowListener as _XWindowListener_impl,
        XMouseListener as _XMouseListener_impl,
        XFocusListener as _XFocusListener_impl,
        XMouseClickHandler as _XMouseClickHandler_impl,
        XTopWindowListener as _XTopWindowListener_impl,
    )
    from com.sun.star.document import XDocumentEventListener as _XDocumentEventListener_impl
    from com.sun.star.sheet import XActivationEventListener as _XActivationEventListener_impl
    from com.sun.star.container import XContainerListener as _XContainerListener_impl
    from com.sun.star.util import XModifyListener as _XModifyListener_impl
    from com.sun.star.frame import XTerminateListener as _XTerminateListener_impl

    _unohelper = _unohelper_impl
    _XEventListener = _XEventListener_impl
    _XActionListener = _XActionListener_impl
    _XItemListener = _XItemListener_impl
    _XTextListener = _XTextListener_impl
    _XKeyListener = _XKeyListener_impl
    _XWindowListener = _XWindowListener_impl
    _XDocumentEventListener = _XDocumentEventListener_impl
    _XActivationEventListener = _XActivationEventListener_impl
    _XContainerListener = _XContainerListener_impl
    _XMouseListener = _XMouseListener_impl
    _XFocusListener = _XFocusListener_impl
    _XMouseClickHandler = _XMouseClickHandler_impl
    _XTopWindowListener = _XTopWindowListener_impl
    _XModifyListener = _XModifyListener_impl
    _XTerminateListener = _XTerminateListener_impl
    _HAVE_UNO = True
except ImportError:
    pass


if TYPE_CHECKING:

    class _BaseParent:
        pass

    class _XEventListenerParent:
        pass

    class _XActionListenerParent:
        pass

    class _XItemListenerParent:
        pass

    class _XTextListenerParent:
        pass

    class _XKeyListenerParent:
        pass

    class _XWindowListenerParent:
        pass

    class _XDocumentEventListenerParent:
        pass

    class _XActivationEventListenerParent:
        pass

    class _XContainerListenerParent:
        pass

    class _XMouseListenerParent:
        pass

    class _XFocusListenerParent:
        pass

    class _XMouseClickHandlerParent:
        pass

    class _XTopWindowListenerParent:
        pass

    class _XModifyListenerParent:
        pass

    class _XTerminateListenerParent:
        pass
else:

    class _DummyBase:
        pass

    class _DummyEventListener:
        pass

    class _DummyActionListener:
        pass

    class _DummyItemListener:
        pass

    class _DummyTextListener:
        pass

    class _DummyKeyListener:
        pass

    class _DummyWindowListener:
        pass

    class _DummyDocumentEventListener:
        pass

    class _DummyActivationListener:
        pass

    class _DummyContainerListener:
        pass

    class _DummyMouseListener:
        pass

    class _DummyFocusListener:
        pass

    class _DummyMouseClickHandler:
        pass

    class _DummyTopWindowListener:
        pass

    class _DummyModifyListener:
        pass

    class _DummyTerminateListener:
        pass

    _BaseParent = _unohelper.Base if _HAVE_UNO else _DummyBase
    _XEventListenerParent = _XEventListener if _HAVE_UNO else _DummyEventListener
    _XActionListenerParent = _XActionListener if _HAVE_UNO else _DummyActionListener
    _XItemListenerParent = _XItemListener if _HAVE_UNO else _DummyItemListener
    _XTextListenerParent = _XTextListener if _HAVE_UNO else _DummyTextListener
    _XKeyListenerParent = _XKeyListener if _HAVE_UNO else _DummyKeyListener
    _XWindowListenerParent = _XWindowListener if _HAVE_UNO else _DummyWindowListener
    _XDocumentEventListenerParent = _XDocumentEventListener if _HAVE_UNO else _DummyDocumentEventListener
    _XActivationEventListenerParent = _XActivationEventListener if _HAVE_UNO else _DummyActivationListener
    _XContainerListenerParent = _XContainerListener if _HAVE_UNO else _DummyContainerListener
    _XMouseListenerParent = _XMouseListener if _HAVE_UNO else _DummyMouseListener
    _XFocusListenerParent = _XFocusListener if _HAVE_UNO else _DummyFocusListener
    _XMouseClickHandlerParent = _XMouseClickHandler if _HAVE_UNO else _DummyMouseClickHandler
    _XTopWindowListenerParent = _XTopWindowListener if _HAVE_UNO else _DummyTopWindowListener
    _XModifyListenerParent = _XModifyListener if _HAVE_UNO else _DummyModifyListener
    _XTerminateListenerParent = _XTerminateListener if _HAVE_UNO else _DummyTerminateListener


# Marker on wrappers from ``_catch_and_log``. Prefer this over ``id()`` so a
# collected wrapper cannot suppress wrapping a later override.
_CATCH_LOGGED_ATTR = "_writeragent_catch_and_log"


class ListenerBoundary(BaseException):
    """Signal that must leave a UNO listener. ``except Exception`` cannot catch it.

    The listener base is the only classifier. Call sites do not keep a second
    list of CloseVeto / TerminationVeto / DisposedException / thread-guard
    names. ``kind`` keeps those cases from being mistaken for each other:

    - ``thread``: main-thread guard. Not an empty document and not disposal.
    - ``disposed``: the desktop or document is gone. Not an empty document.
    - ``veto``: close or app-quit veto. The original UNO exception is what
      the bridge must see; this wrapper only carries it across the classifier.

    A bare ``RuntimeException`` / ``RuntimeError`` is none of these. It stays
    a logged callback failure, not disposal and not "nothing is open".
    """

    kind: str
    original: BaseException

    def __init__(self, kind: str, original: BaseException) -> None:
        self.kind = kind
        self.original = original
        super().__init__(str(original))


def _is_thread_violation(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and THREAD_VIOLATION_MARK in str(exc)


def _is_bridge_veto(exc: BaseException) -> bool:
    name = type(exc).__name__
    return "CloseVetoException" in name or "TerminationVetoException" in name


def listener_boundary(exc: BaseException) -> ListenerBoundary | None:
    """The one boundary for *exc*, or None when the callback may fail soft.

    None is an ordinary Python or UNO runtime error. Real disposal uses
    :func:`plugin.framework.errors.is_real_disposal`, so a ``RuntimeException``
    name is not disposal.
    """
    if isinstance(exc, ListenerBoundary):
        return exc
    if _is_thread_violation(exc):
        return ListenerBoundary("thread", exc)
    if _is_bridge_veto(exc):
        return ListenerBoundary("veto", exc)
    if is_real_disposal(exc):
        return ListenerBoundary("disposed", exc)
    return None


def reraise_listener_boundary(exc: BaseException) -> NoReturn:
    """Re-raise *exc* so a generic ``except Exception`` cannot hide the boundary.

    A runtime error is re-raised as itself: not disposal, and not dropped as
    an empty document. A close or termination veto is re-raised as the
    original UNO exception so LibreOffice can still veto. The main-thread
    guard and real disposal leave as :class:`ListenerBoundary`.
    """
    if isinstance(exc, ListenerBoundary):
        if exc.kind == "veto":
            raise exc.original
        raise exc
    boundary = listener_boundary(exc)
    if boundary is None or boundary.kind == "veto":
        raise exc
    raise boundary from exc


_UNSET = object()


def _listener_failure_value(func: Any) -> Any:
    """``sal_Bool`` methods must not return None or the bridge type-errors.

    ``from __future__ import annotations`` stores the return annotation as
    the string ``bool`` rather than the bool type.
    """
    ret = getattr(func, "__annotations__", {}).get("return")
    if ret is bool or ret == "bool":
        return False
    return None


def _catch_and_log(func: Any, failure: Any = _UNSET) -> Any:
    """Decorator to catch and log exceptions in UNO listener callbacks."""
    if failure is _UNSET:
        failure = _listener_failure_value(func)

    @functools.wraps(func)
    def wrapper(self: Any, ev: Any = None, *args: Any, **kwargs: Any) -> Any:
        try:
            return func(self, ev, *args, **kwargs)
        except ListenerBoundary as boundary:
            # Already classified. A veto still has to be the UNO type.
            if boundary.kind == "veto":
                raise boundary.original
            raise
        except Exception as exc:
            # One boundary. Vetoes are re-raised as the original UNO exception
            # so the bridge can still veto. Thread violations and real
            # disposal leave as ListenerBoundary, which except Exception
            # cannot swallow. disposing() does not raise on disposal: a
            # throw there stops the broadcaster from notifying the remaining
            # listeners. Any other callback is a query, and a dead desktop
            # must not look like an empty success. A runtime error is logged
            # and returns the failure value; it is not disposal.
            # A type-name allow-list (CloseVeto, then TerminationVeto, with
            # DisposedException added and removed) and a bare except
            # Exception both miss this: the except swallows
            # assert_main_thread's RuntimeError, and a disposed desktop
            # becomes the failure return (None / False) — the same shape as
            # "nothing is open". A RuntimeException name is easy to treat
            # as that disposal, or the reverse. Every one of those is an
            # Exception, so each call site would re-check names.
            #
            # Classify first. TypeError and ValueError as their own except
            # clauses never reach listener_boundary. A disposal or veto that
            # subclasses either type would be logged and returned as a soft
            # failure. An ordinary TypeError or ValueError still logs under
            # its type name and does not enter the bridge.
            signal = listener_boundary(exc)
            if signal is not None and signal.kind == "disposed" and func.__name__ == "disposing":
                log.debug("%s disposing: source already disposed", self.__class__.__name__)
                return failure
            if signal is not None:
                reraise_listener_boundary(exc)
            if isinstance(exc, TypeError):
                log.exception(f"{self.__class__.__name__} TypeError in {func.__name__}")
                return failure
            if isinstance(exc, ValueError):
                log.exception(f"{self.__class__.__name__} ValueError in {func.__name__}")
                return failure
            log.exception(f"{self.__class__.__name__} unhandled exception in {func.__name__}")
            return failure

    setattr(wrapper, _CATCH_LOGGED_ATTR, True)
    return wrapper


# ---------------------------------------------------------
# Static Base Classes (100% clean MRO for all typecheckers)
# ---------------------------------------------------------


class BaseListener(_BaseParent, _XEventListenerParent):
    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # What was wrong: a hand-maintained frozenset missed new callbacks, so a
        # subclass override entered the bridge without _catch_and_log.
        # How it happened: adding or wrapping new listener methods required updating _UNO_CALLBACK_NAMES.
        # Why this change: discover callback names dynamically from base classes on the MRO with _CATCH_LOGGED_ATTR.
        callback_names: set[str] = set()
        for base in cls.__mro__:
            if base is cls:
                continue
            for name, attr in base.__dict__.items():
                if getattr(attr, _CATCH_LOGGED_ATTR, False):
                    callback_names.add(name)

        for name in callback_names:
            method = cls.__dict__.get(name)
            if method is None or getattr(method, _CATCH_LOGGED_ATTR, False):
                continue
            # __init_subclass__ wraps an override that replaced the base method,
            # and the override often has no return annotation. The bridge type
            # for XMouseClickHandler.mousePressed is sal_Bool. Taking None from
            # the override's missing annotation type-errors the bridge. The base
            # method's annotation is the contract.
            failure = None
            for base in cls.__mro__:
                base_method = base.__dict__.get(name)
                if base_method is not None:
                    ret = getattr(base_method, "__annotations__", {}).get("return")
                    if ret is bool or ret == "bool":
                        failure = False
                        break
            setattr(cls, name, _catch_and_log(method, failure=failure))

    @_catch_and_log
    def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_disposing(Source)

    def on_disposing(self, Source: Any) -> None:
        pass


class BaseContainerListener(BaseListener, _XContainerListenerParent):
    @_catch_and_log
    def elementInserted(self, Event: Any) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_element_inserted(Event)

    @_catch_and_log
    def elementRemoved(self, Event: Any) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_element_removed(Event)

    @_catch_and_log
    def elementReplaced(self, Event: Any) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_element_replaced(Event)

    def on_element_inserted(self, Event: Any) -> None:
        pass

    def on_element_removed(self, Event: Any) -> None:
        pass

    def on_element_replaced(self, Event: Any) -> None:
        pass


class BaseActionListener(BaseListener, _XActionListenerParent):
    @_catch_and_log
    def actionPerformed(self, rEvent: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_action_performed(rEvent)

    def on_action_performed(self, rEvent: Any) -> None:
        pass


class BaseItemListener(BaseListener, _XItemListenerParent):
    @_catch_and_log
    def itemStateChanged(self, rEvent: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_item_state_changed(rEvent)

    def on_item_state_changed(self, rEvent: Any) -> None:
        pass


class BaseTextListener(BaseListener, _XTextListenerParent):
    @_catch_and_log
    def textChanged(self, rEvent: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_text_changed(rEvent)

    def on_text_changed(self, rEvent: Any) -> None:
        pass


class BaseKeyListener(BaseListener, _XKeyListenerParent):
    @_catch_and_log
    def keyPressed(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_key_pressed(e)

    @_catch_and_log
    def keyReleased(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_key_released(e)

    def on_key_pressed(self, e: Any) -> None:
        pass

    def on_key_released(self, e: Any) -> None:
        pass


class BaseWindowListener(BaseListener, _XWindowListenerParent):
    @_catch_and_log
    def windowResized(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_window_resized(e)

    @_catch_and_log
    def windowMoved(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_window_moved(e)

    @_catch_and_log
    def windowShown(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_window_shown(e)

    @_catch_and_log
    def windowHidden(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_window_hidden(e)

    def on_window_resized(self, rEvent: Any) -> None:
        pass

    def on_window_moved(self, rEvent: Any) -> None:
        pass

    def on_window_shown(self, rEvent: Any) -> None:
        pass

    def on_window_hidden(self, rEvent: Any) -> None:
        pass


class BaseTopWindowListener(BaseListener, _XTopWindowListenerParent):
    @_catch_and_log
    def windowOpened(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_window_opened(e)

    @_catch_and_log
    def windowClosing(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_window_closing(e)

    @_catch_and_log
    def windowClosed(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_window_closed(e)

    @_catch_and_log
    def windowMinimized(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_window_minimized(e)

    @_catch_and_log
    def windowNormalized(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_window_normalized(e)

    @_catch_and_log
    def windowActivated(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_window_activated(e)

    @_catch_and_log
    def windowDeactivated(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_window_deactivated(e)

    def on_window_opened(self, rEvent: Any) -> None:
        pass

    def on_window_closing(self, rEvent: Any) -> None:
        pass

    def on_window_closed(self, rEvent: Any) -> None:
        pass

    def on_window_minimized(self, rEvent: Any) -> None:
        pass

    def on_window_normalized(self, rEvent: Any) -> None:
        pass

    def on_window_activated(self, rEvent: Any) -> None:
        pass

    def on_window_deactivated(self, rEvent: Any) -> None:
        pass


class BaseDocumentEventListener(BaseListener, _XDocumentEventListenerParent):
    @_catch_and_log
    def documentEventOccured(self, Event: Any) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_document_event(Event)

    def on_document_event(self, Event: Any) -> None:
        pass


class BaseActivationEventListener(BaseListener, _XActivationEventListenerParent):
    """Base listener for Calc sheet activation events (XActivationEventListener).

    Override on_active_spreadsheet_changed to react when the user switches sheets.
    The frame controller's addActivationEventListener / removeActivationEventListener
    methods accept instances of this class.
    """

    @_catch_and_log
    def activeSpreadsheetChanged(self, aEvent: Any) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_active_spreadsheet_changed(aEvent)

    def on_active_spreadsheet_changed(self, aEvent: Any) -> None:
        pass


class BaseMouseListener(BaseListener, _XMouseListenerParent):
    @_catch_and_log
    def mousePressed(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_mouse_pressed(e)

    @_catch_and_log
    def mouseReleased(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_mouse_released(e)

    @_catch_and_log
    def mouseEntered(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_mouse_entered(e)

    @_catch_and_log
    def mouseExited(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_mouse_exited(e)

    def on_mouse_pressed(self, e: Any) -> None:
        pass

    def on_mouse_released(self, e: Any) -> None:
        pass

    def on_mouse_entered(self, e: Any) -> None:
        pass

    def on_mouse_exited(self, e: Any) -> None:
        pass


class BaseFocusListener(BaseListener, _XFocusListenerParent):
    @_catch_and_log
    def focusGained(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_focus_gained(e)

    @_catch_and_log
    def focusLost(self, e: Any) -> None:  # noqa: N802 -- UNO signature
        self.on_focus_lost(e)

    def on_focus_gained(self, e: Any) -> None:
        pass

    def on_focus_lost(self, e: Any) -> None:
        pass


class BaseMouseClickHandler(BaseListener, _XMouseClickHandlerParent):
    @_catch_and_log
    def mousePressed(self, e: Any) -> bool:  # noqa: N802 -- UNO signature
        return bool(self.on_mouse_pressed(e))

    @_catch_and_log
    def mouseReleased(self, e: Any) -> bool:  # noqa: N802 -- UNO signature
        return bool(self.on_mouse_released(e))

    def on_mouse_pressed(self, e: Any) -> bool:
        return False

    def on_mouse_released(self, e: Any) -> bool:
        return False


class BaseModifyListener(BaseListener, _XModifyListenerParent):
    """Base listener for model/sheet/controller change notifications (XModifyListener).

    Subclasses can either override ``on_modified(aEvent)`` or ``modified(aEvent)``.
    Any unhandled exception is caught and logged via ``_catch_and_log``.
    """

    @_catch_and_log
    def modified(self, aEvent: Any) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_modified(aEvent)

    def on_modified(self, aEvent: Any) -> None:
        pass


class BaseTerminateListener(BaseListener, _XTerminateListenerParent):
    """Base listener for desktop termination events (XTerminateListener).

    Override ``on_query_termination`` (or raise TerminationVetoException) and
    ``on_notify_termination`` to clean up resources before LibreOffice quits.
    """

    @_catch_and_log
    def queryTermination(self, Event: Any) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_query_termination(Event)

    @_catch_and_log
    def notifyTermination(self, Event: Any) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_notify_termination(Event)

    def on_query_termination(self, Event: Any) -> None:
        pass

    def on_notify_termination(self, Event: Any) -> None:
        pass


def install_view_doc_listener(
    ctx: Any,
    register_controller: Callable[[Any], None],
    log_label: str,
) -> Any:
    """Attach a global listener that invokes register_controller on view/load events."""
    try:
        class _NewDocListener(BaseDocumentEventListener):  # type: ignore[misc, valid-type]
            def on_document_event(self, Event: Any) -> None:  # noqa: N803 -- UNO signature
                name = getattr(Event, "EventName", "") or ""
                if name not in ("OnViewCreated", "OnLoadFinished", "OnLoad", "OnNew"):
                    return
                # Prefer the event's own controller: it is guaranteed attached to the view
                # shell (OnViewCreated is broadcast after attach), while getCurrentController
                # can race and yield a controller whose registration is a silent no-op.
                controller = getattr(Event, "ViewController", None)
                if controller is not None:
                    register_controller(controller)
                    return
                source = getattr(Event, "Source", None)
                if source is not None and hasattr(source, "getCurrentController"):
                    register_controller(source.getCurrentController())

        smgr = ctx.getServiceManager()
        broadcaster = smgr.createInstanceWithContext("com.sun.star.frame.GlobalEventBroadcaster", ctx)
        listener = _NewDocListener()
        broadcaster.addDocumentEventListener(listener)
        log.debug("%s: global doc-event listener attached", log_label)
        return listener
    except Exception:
        log.warning("%s: doc-event listener install failed", log_label, exc_info=True)
        return None

