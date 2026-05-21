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
"""Shared CORS headers for the MCP HTTP server and protocol handler."""

import re

from plugin.mcp.cors_origins import get_allow_private_origins, is_extra_allowed_origin, is_private_browser_origin

# Streamable-HTTP MCP clients preflight with Mcp-Protocol-Version; SSE may use Last-Event-ID.
_BASE_ALLOW_HEADERS = (
    "Content-Type",
    "Authorization",
    "Mcp-Session-Id",
    "X-Document-URL",
    "Mcp-Protocol-Version",
    "Last-Event-ID",
    "Accept",
)

_EXPOSE_HEADERS = "Mcp-Session-Id, Mcp-Protocol-Version"

_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")

PREFLIGHT_MAX_AGE = "86400"


def is_safe_origin(origin: str) -> bool:
    """True when Origin may receive Access-Control-Allow-Origin reflection."""
    if not origin:
        return False
    if _ORIGIN_RE.match(origin):
        return True
    if is_extra_allowed_origin(origin):
        return True
    if get_allow_private_origins() and is_private_browser_origin(origin):
        return True
    return False


def merge_allow_headers(access_control_request_headers: str | None) -> str:
    """Build Access-Control-Allow-Headers: base list union preflight request list."""
    merged: dict[str, str] = {}
    for header in _BASE_ALLOW_HEADERS:
        merged[header.lower()] = header
    if access_control_request_headers:
        for header in (h.strip() for h in access_control_request_headers.split(",") if h.strip()):
            key = header.lower()
            if key not in merged:
                merged[key] = header
    return ", ".join(merged.values())


def send_cors_headers(handler, *, preflight: bool = False) -> None:
    """Apply CORS headers to an HTTP request handler (GenericRequestHandler or MCP raw handler)."""
    origin = handler.headers.get("Origin")
    if origin and is_safe_origin(origin):
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    requested = handler.headers.get("Access-Control-Request-Headers") if preflight else None
    handler.send_header("Access-Control-Allow-Headers", merge_allow_headers(requested))
    handler.send_header("Access-Control-Expose-Headers", _EXPOSE_HEADERS)
    if preflight:
        handler.send_header("Access-Control-Max-Age", PREFLIGHT_MAX_AGE)
