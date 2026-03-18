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
"""Global UNO component context provider.

Services are singletons that outlive the UNO component that created them.
The ctx passed during bootstrap (from MainJob.__init__) can become stale.

``uno.getComponentContext()`` always returns the current, valid global
context — this is the same call the fallback autostart thread uses.

All services that need UNO access should call ``get_ctx()`` rather than
storing a ctx reference from ``initialize()``.
"""

import logging

log = logging.getLogger("writeragent.context")

_fallback_ctx = None


def set_fallback_ctx(ctx):
    """Store a fallback ctx for use when uno module is not available."""
    global _fallback_ctx
    _fallback_ctx = ctx


def get_ctx():
    """Return the current valid UNO component context.

    Prefers ``uno.getComponentContext()`` (always fresh).
    Falls back to the stored bootstrap ctx if uno is not importable.
    """
    try:
        import uno
        if hasattr(uno, "getComponentContext"):
            ctx = uno.getComponentContext()
            if ctx is not None:
                return ctx
    except ImportError:
        pass
    return _fallback_ctx
