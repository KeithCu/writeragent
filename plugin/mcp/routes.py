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
"""MCP route registry.

Stores route handlers keyed by (method, path). Modules register their
handlers during initialize() and the HTTP server dispatches to them.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, NamedTuple

log = logging.getLogger("writeragent.mcp.routes")


class Route(NamedTuple):
    handler: Callable[..., Any]


def json_route(fn: Callable[..., tuple[int, Any]]) -> Callable[..., None]:
    """Wrap ``(body, headers, query) -> (status, dict)`` for HTTP dispatch."""

    def wrapped(handler: Any) -> None:
        from plugin.framework.errors import format_error_payload
        from plugin.framework.url_utils import get_url_query_dict
        from plugin.mcp.server import read_json_body, write_http_json

        data, rejected = read_json_body(handler)
        if rejected is not None:
            status, err = rejected
            write_http_json(handler, status, format_error_payload(err))
            return
        query = get_url_query_dict(handler.path)
        status, payload = fn(data, handler.headers, query)
        write_http_json(handler, status, payload)

    return wrapped


class HttpRouteRegistry:
    """Registry of HTTP route handlers.

    HTTP worker threads read this table while MCP toggle registers and
    unregister routes. ``lock`` covers every read and write. ``GET /``
    copies the keys under that lock: without it, ``list(self._routes)``
    raises ``RuntimeError`` (dict changed size during iteration).

    Hold :attr:`lock` across a multi-route register or unregister so a
    concurrent read sees the set from before the batch or after it.
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Route] = {}  # (method, path) -> Route
        self.lock: threading.RLock = threading.RLock()

    def add(self, method: str, path: str, handler: Callable[..., Any]) -> None:
        """Register a route handler.

        Args:
            method:      HTTP method (GET, POST, DELETE, ...).
            path:        Exact path (e.g. "/health"). No path params.
            handler:     ``fn(http_handler) -> None`` (writes the response).
                         JSON routes use :func:`json_route` on ``(body, headers, query) -> (status, dict)``.
        """
        key = (method, path)
        with self.lock:
            self._routes[key] = Route(handler=handler)

    def remove(self, method: str, path: str) -> None:
        """Unregister a route."""
        key = (method, path)
        with self.lock:
            self._routes.pop(key, None)

    def match(self, method: str, path: str) -> Route | None:
        """Return Route(handler) or None."""
        with self.lock:
            return self._routes.get((method, path))

    def list_routes(self) -> list[tuple[str, str]]:
        """Return a snapshot of (method, path) tuples."""
        with self.lock:
            return list(self._routes.keys())
