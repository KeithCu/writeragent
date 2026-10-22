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
"""Process-wide MCP session management.

One session id for the whole soffice process, shared by every MCP client.
Minted on first successful initialize; never rotated; never cleared on DELETE.
Stale session ids return HTTP 404.
"""

from __future__ import annotations

import logging
import threading
from typing import Any
import uuid

from plugin.mcp import wire_types
from plugin.mcp.cors import send_cors_headers
from plugin.mcp.server import MCP_HTTP_MAX_BODY_BYTES, write_http_json

MCP_PROTOCOL_VERSION = wire_types.MCP_PROTOCOL_VERSION

log = logging.getLogger("writeragent.mcp.protocol")

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "_SESSION_EXPIRED_MSG",
    "_get_request_session_id",
    "_mcp_session_id",
    "_mcp_session_lock",
    "_mint_session_id_once",
    "_reject_stale_session",
    "_send_mcp_response_headers",
]


def _get_request_session_id(handler: Any) -> str | None:
    value = handler.headers.get("Mcp-Session-Id")
    if value:
        return value.strip()
    return None


def _send_mcp_response_headers(handler: Any, *, session_id: str | None = None) -> None:
    """CORS plus streamable-HTTP MCP headers on every MCP transport response."""
    send_cors_headers(handler, preflight=False)
    handler.send_header("Mcp-Protocol-Version", MCP_PROTOCOL_VERSION)
    if session_id:
        handler.send_header("Mcp-Session-Id", session_id)


# One session id for the whole soffice process, shared by every MCP client.
# Minted on first successful initialize; never rotated; never cleared on DELETE.
_mcp_session_id = None
_mcp_session_lock = threading.Lock()
_SESSION_EXPIRED_MSG = "Session expired (server restarted). Call initialize again."


def _mint_session_id_once() -> str:
    """Assign uuid4 on first successful initialize; later calls keep that id."""
    global _mcp_session_id
    with _mcp_session_lock:
        if _mcp_session_id is None:
            _mcp_session_id = str(uuid.uuid4())
        return _mcp_session_id


def _reject_stale_session(handler: Any, msg: Any = None) -> bool:
    """Write HTTP 404 when Mcp-Session-Id is present and not the process id.

    Spec clients re-initialize on 404, not 409 or silent success. No header is
    allowed (CLI / first contact). A single ``initialize`` is always allowed —
    that is recovery after restart. A stale header on a batch 404s the whole
    request so we never process some items.
    """
    incoming = _get_request_session_id(handler)
    if incoming is None:
        return False
    if isinstance(msg, dict) and msg.get("method") == "initialize":
        return False
    if incoming == _mcp_session_id:
        return False
    req_id = msg.get("id") if isinstance(msg, dict) else None
    log.info("[MCP] stale session id %r (current=%r) — 404", incoming, _mcp_session_id)
    # Drain unread body and send Connection: close so the kernel does
    # not emit TCP RST on close, which manifests as Errno 104 on the client.
    if msg is None and not getattr(handler, "_body_read", False):
        raw_length = handler.headers.get("Content-Length") if hasattr(handler, "headers") and handler.headers else None
        if raw_length:
            try:
                content_length = int(raw_length)
                if 0 < content_length <= MCP_HTTP_MAX_BODY_BYTES:
                    handler.rfile.read(content_length)
                    setattr(handler, "_body_read", True)
            except (TypeError, ValueError, OSError):
                pass
    handler.close_connection = True

    def _headers(h: Any) -> None:
        _send_mcp_response_headers(h, session_id=_mcp_session_id)
        h.send_header("Connection", "close")

    write_http_json(
        handler,
        404,
        wire_types.jsonrpc_failure(req_id, wire_types.INVALID_REQUEST, _SESSION_EXPIRED_MSG),
        extra_headers=_headers,
    )
    flush = getattr(handler.wfile, "flush", None)
    if callable(flush):
        try:
            flush()
        except OSError:
            pass
    return True
