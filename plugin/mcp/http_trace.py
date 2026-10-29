# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Structured MCP HTTP trace lines for writeragent_debug.log diagnosis."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.url_utils import get_url_path
from plugin.mcp.cors import get_allow_private_origins, is_extra_allowed_origin, is_private_browser_origin, is_safe_origin, merge_allow_headers

log = logging.getLogger("writeragent.mcp.http")


def _client(handler: Any) -> str:
    return "%s:%s" % handler.client_address[:2]


def _header(handler: Any, name: str) -> str | None:
    # Message.get is case-insensitive. Strip: the HTTP parser leaves trailing
    # whitespace, and %r / the User-Agent slice would change if it stayed.
    headers = getattr(handler, "headers", None)
    if not headers:
        return None
    value = headers.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def log_http_request(handler: Any, method: str, path: str) -> None:
    """Log every inbound HTTP hit before routing (confirms POST arrived vs OPTIONS-only)."""
    log.info("[MCP-HTTP] %s %s from %s origin=%r ua=%r", method, path, _client(handler), _header(handler, "Origin"), (_header(handler, "User-Agent") or "")[:120])


def log_cors_preflight(handler: Any, path: str) -> None:
    """Log OPTIONS preflight details — key for browser clients that stop after preflight."""
    if not log.isEnabledFor(logging.INFO):
        return
    origin = _header(handler, "Origin")
    requested_method = _header(handler, "Access-Control-Request-Method")
    requested_headers = _header(handler, "Access-Control-Request-Headers")
    allow_origin = bool(origin and is_safe_origin(origin))
    extra = bool(origin and is_extra_allowed_origin(origin))
    private = bool(origin and get_allow_private_origins() and is_private_browser_origin(origin) and not extra)
    log.info(
        "[MCP-CORS] OPTIONS %s from %s origin=%r safe=%s explicit_list=%s private_rule=%s request_method=%r request_headers=%r allow_origin=%s allow_headers=%r",
        path,
        _client(handler),
        origin,
        allow_origin,
        extra,
        private,
        requested_method,
        requested_headers,
        "reflect" if allow_origin else "omit",
        merge_allow_headers(requested_headers),
    )


def log_forbidden_origin(handler: Any) -> None:
    """Origin present and not on the allow list — request rejected with HTTP 403."""
    origin = _header(handler, "Origin")
    method = getattr(handler, "command", "?")
    path = get_url_path(getattr(handler, "path", "") or "")
    log.warning("[MCP-CORS] %s %r: Origin %r forbidden — HTTP 403 (no Access-Control-*). Enable mcp.cors_allow_private_origins or add the origin to mcp.cors_allowed_origins in writeragent.json.", method, path, origin)


def log_mcp_transport_entry(handler: Any, transport: str) -> None:
    """Log when POST/GET/DELETE reaches an MCP protocol handler (past routing)."""
    log.info(
        "[MCP-HTTP] %s /%s from %s origin=%r protocol_version=%r session=%r content_length=%s",
        getattr(handler, "command", "?"),
        transport,
        _client(handler),
        _header(handler, "Origin"),
        _header(handler, "Mcp-Protocol-Version"),
        _header(handler, "Mcp-Session-Id"),
        _header(handler, "Content-Length") or "0",
    )


def log_unsupported_protocol_version(handler: Any, requested: str) -> None:
    log.warning("[MCP-HTTP] rejected unsupported Mcp-Protocol-Version %r from %s origin=%r", requested, _client(handler), _header(handler, "Origin"))


def log_no_route(handler: Any, method: str, path: str) -> None:
    log.warning("[MCP-HTTP] no route for %s %s from %s", method, path, _client(handler))
