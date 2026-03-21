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
"""HTTP route registry for the framework.

Stores route handlers keyed by (method, path). Modules register their
handlers during initialize() and the HTTP server dispatches to them.
"""

import logging
from collections import namedtuple

log = logging.getLogger("writeragent.framework.http_routes")

Route = namedtuple("Route", ["handler", "raw", "main_thread"])


class HttpRouteRegistry:
    """Registry of HTTP route handlers.

    Usage::

        routes = HttpRouteRegistry()

        # Simple handler — receives (body, headers, query), returns (status, dict)
        routes.add("GET", "/health", health_handler)

        # Raw handler — receives the BaseHTTPRequestHandler, writes directly
        routes.add("POST", "/mcp", mcp_handler, raw=True)

        # Main-thread handler — wrapped in execute_on_main_thread()
        routes.add("GET", "/doc-info", doc_handler, main_thread=True)
    """

    def __init__(self):
        self._routes = {}  # (method, path) -> Route

    def add(self, method, path, handler, raw=False, main_thread=False):
        """Register a route handler.

        Args:
            method:      HTTP method (GET, POST, DELETE, ...).
            path:        Exact path (e.g. "/health"). No path params.
            handler:     Callable. See ``raw`` for signature.
            raw:         If False (default): fn(body, headers, query) -> (status, dict).
                         If True: fn(http_handler) -> None (writes directly).
            main_thread: If True, handler is wrapped in execute_on_main_thread().
        """
        key = (method.upper(), path)
        if key in self._routes:
            log.warning("Route %s %s already registered — overwriting", method, path)
        self._routes[key] = Route(handler=handler, raw=raw, main_thread=main_thread)
        log.debug("Route registered: %s %s (raw=%s, main_thread=%s)",
                  method, path, raw, main_thread)

    def remove(self, method, path):
        """Unregister a route."""
        key = (method.upper(), path)
        removed = self._routes.pop(key, None)
        if removed:
            log.debug("Route removed: %s %s", method, path)
        return removed is not None

    def match(self, method, path):
        """Return Route(handler, raw, main_thread) or None."""
        return self._routes.get((method.upper(), path))

    @property
    def route_count(self):
        return len(self._routes)

    def list_routes(self):
        """Return a list of (method, path) tuples."""
        return list(self._routes.keys())
