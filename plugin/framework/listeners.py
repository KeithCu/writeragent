# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
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
"""Base classes for UNO listeners to reduce boilerplate.

These base classes provide empty `disposing` implementations and standard
try/except logging blocks around the main event callbacks.
"""

import logging
import functools
import unohelper
from com.sun.star.awt import XActionListener, XItemListener, XTextListener, XWindowListener
from com.sun.star.lang import XEventListener

log = logging.getLogger(__name__)


def _catch_and_log(func):
    """Decorator to catch and log exceptions in UNO listener callbacks."""
    @functools.wraps(func)
    def wrapper(self, ev, *args, **kwargs):
        try:
            return func(self, ev, *args, **kwargs)
        except TypeError as e:
            log.error(f"{self.__class__.__name__} TypeError in {func.__name__}: %s", e)
        except ValueError as e:
            log.error(f"{self.__class__.__name__} ValueError in {func.__name__}: %s", e)
        except Exception as e:
            # Base UNO listeners must not leak arbitrary Python exceptions into the C++ bridge.
            log.error(f"{self.__class__.__name__} unhandled exception in {func.__name__}: %s", e, exc_info=True)
    return wrapper


class BaseListener(unohelper.Base, XEventListener):
    """Base UNO listener providing empty disposing()."""

    def disposing(self, Source):
        """Required by XEventListener interface."""
        pass


class BaseActionListener(BaseListener, XActionListener):
    """Base class for XActionListener that catches and logs exceptions."""

    @_catch_and_log
    def actionPerformed(self, rEvent):
        self.on_action_performed(rEvent)

    def on_action_performed(self, rEvent):
        """Override this method to handle the action event."""
        pass


class BaseItemListener(BaseListener, XItemListener):
    """Base class for XItemListener that catches and logs exceptions."""

    @_catch_and_log
    def itemStateChanged(self, rEvent):
        self.on_item_state_changed(rEvent)

    def on_item_state_changed(self, rEvent):
        """Override this method to handle the item state change event."""
        pass


class BaseTextListener(BaseListener, XTextListener):
    """Base class for XTextListener that catches and logs exceptions."""

    @_catch_and_log
    def textChanged(self, rEvent):
        self.on_text_changed(rEvent)

    def on_text_changed(self, rEvent):
        """Override this method to handle the text changed event."""
        pass


class BaseWindowListener(BaseListener, XWindowListener):
    """Base class for XWindowListener providing empty defaults and catching exceptions."""

    @_catch_and_log
    def windowResized(self, rEvent):
        self.on_window_resized(rEvent)

    @_catch_and_log
    def windowMoved(self, rEvent):
        self.on_window_moved(rEvent)

    @_catch_and_log
    def windowShown(self, rEvent):
        self.on_window_shown(rEvent)

    @_catch_and_log
    def windowHidden(self, rEvent):
        self.on_window_hidden(rEvent)

    def on_window_resized(self, rEvent):
        pass

    def on_window_moved(self, rEvent):
        pass

    def on_window_shown(self, rEvent):
        pass

    def on_window_hidden(self, rEvent):
        pass
