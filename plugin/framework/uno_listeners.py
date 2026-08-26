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
"""

from __future__ import annotations

import functools
import logging
from typing import Any, TYPE_CHECKING

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
_XCloseListener: Any = None
_XTerminateListener: Any = None
_XActivationEventListener: Any = None
_XContainerListener: Any = None
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
    )
    from com.sun.star.document import XDocumentEventListener as _XDocumentEventListener_impl
    from com.sun.star.util import XCloseListener as _XCloseListener_impl
    from com.sun.star.frame import XTerminateListener as _XTerminateListener_impl
    from com.sun.star.sheet import XActivationEventListener as _XActivationEventListener_impl
    from com.sun.star.container import XContainerListener as _XContainerListener_impl

    _unohelper = _unohelper_impl
    _XEventListener = _XEventListener_impl
    _XActionListener = _XActionListener_impl
    _XItemListener = _XItemListener_impl
    _XTextListener = _XTextListener_impl
    _XKeyListener = _XKeyListener_impl
    _XWindowListener = _XWindowListener_impl
    _XDocumentEventListener = _XDocumentEventListener_impl
    _XCloseListener = _XCloseListener_impl
    _XTerminateListener = _XTerminateListener_impl
    _XActivationEventListener = _XActivationEventListener_impl
    _XContainerListener = _XContainerListener_impl
    _HAVE_UNO = True
except ImportError:
    pass


if TYPE_CHECKING:
    class _BaseParent: pass
    class _XEventListenerParent: pass
    class _XActionListenerParent: pass
    class _XItemListenerParent: pass
    class _XTextListenerParent: pass
    class _XKeyListenerParent: pass
    class _XWindowListenerParent: pass
    class _XDocumentEventListenerParent: pass
    class _XCloseListenerParent: pass
    class _XTerminateListenerParent: pass
    class _XActivationEventListenerParent: pass
    class _XContainerListenerParent: pass
else:
    class _DummyBase: pass
    class _DummyEventListener: pass
    class _DummyActionListener: pass
    class _DummyItemListener: pass
    class _DummyTextListener: pass
    class _DummyKeyListener: pass
    class _DummyWindowListener: pass
    class _DummyDocumentEventListener: pass
    class _DummyCloseListener: pass
    class _DummyTerminateListener: pass
    class _DummyActivationListener: pass
    class _DummyContainerListener: pass

    _BaseParent = _unohelper.Base if _HAVE_UNO else _DummyBase
    _XEventListenerParent = _XEventListener if _HAVE_UNO else _DummyEventListener
    _XActionListenerParent = _XActionListener if _HAVE_UNO else _DummyActionListener
    _XItemListenerParent = _XItemListener if _HAVE_UNO else _DummyItemListener
    _XTextListenerParent = _XTextListener if _HAVE_UNO else _DummyTextListener
    _XKeyListenerParent = _XKeyListener if _HAVE_UNO else _DummyKeyListener
    _XWindowListenerParent = _XWindowListener if _HAVE_UNO else _DummyWindowListener
    _XDocumentEventListenerParent = _XDocumentEventListener if _HAVE_UNO else _DummyDocumentEventListener
    _XCloseListenerParent = _XCloseListener if _HAVE_UNO else _DummyCloseListener
    _XTerminateListenerParent = _XTerminateListener if _HAVE_UNO else _DummyTerminateListener
    _XActivationEventListenerParent = _XActivationEventListener if _HAVE_UNO else _DummyActivationListener
    _XContainerListenerParent = _XContainerListener if _HAVE_UNO else _DummyContainerListener


def _catch_and_log(func):
    """Decorator to catch and log exceptions in UNO listener callbacks."""

    @functools.wraps(func)
    def wrapper(self, ev, *args, **kwargs):
        try:
            return func(self, ev, *args, **kwargs)
        except TypeError:
            log.exception(f"{self.__class__.__name__} TypeError in {func.__name__}")
        except ValueError:
            log.exception(f"{self.__class__.__name__} ValueError in {func.__name__}")
        except Exception:
            # Base UNO listeners must not leak arbitrary Python exceptions into the C++ bridge.
            log.exception(f"{self.__class__.__name__} unhandled exception in {func.__name__}")

    return wrapper


# ---------------------------------------------------------
# Static Base Classes (100% clean MRO for all typecheckers)
# ---------------------------------------------------------

class BaseListener(_BaseParent, _XEventListenerParent):
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


class BaseDocumentEventListener(BaseListener, _XDocumentEventListenerParent):
    @_catch_and_log
    def documentEventOccured(self, Event: Any) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_document_event(Event)

    def on_document_event(self, Event: Any) -> None:
        pass


class BaseCloseListener(BaseListener, _XCloseListenerParent):
    @_catch_and_log
    def queryClosing(self, Source: Any, GetsOwnership: bool) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_query_closing(Source, GetsOwnership)

    @_catch_and_log
    def notifyClosing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        self.on_notify_closing(Source)

    def on_query_closing(self, Source: Any, GetsOwnership: bool) -> None:
        pass

    def on_notify_closing(self, Source: Any) -> None:
        pass


class BaseTerminateListener(BaseListener, _XTerminateListenerParent):
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
