# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Structured MCP HTTP trace lines for writeragent_debug.log diagnosis."""

import logging

from plugin.mcp.cors import is_safe_origin, merge_allow_headers
from plugin.mcp.cors_origins import is_extra_allowed_origin

log = logging.getLogger("writeragent.mcp.http")


def _client(handler) -> str:
    try:
        return "%s:%s" % handler.client_address[:2]
    except Exception:
        return "?"


def _header(handler, name: str) -> str | None:
    value = handler.headers.get(name)
    return value.strip() if value else None


def log_http_request(handler, method: str, path: str) -> None:
    """Log every inbound HTTP hit before routing (confirms POST arrived vs OPTIONS-only)."""
    log.info(
        "[MCP-HTTP] %s %s from %s origin=%r ua=%r",
        method,
        path,
        _client(handler),
        _header(handler, "Origin"),
        (_header(handler, "User-Agent") or "")[:120],
    )


def log_cors_preflight(handler, path: str) -> None:
    """Log OPTIONS preflight details — key for browser clients that stop after preflight."""
    origin = _header(handler, "Origin")
    requested_method = _header(handler, "Access-Control-Request-Method")
    requested_headers = _header(handler, "Access-Control-Request-Headers")
    allow_origin = bool(origin and is_safe_origin(origin))
    extra = bool(origin and is_extra_allowed_origin(origin))
    log.info(
        "[MCP-CORS] OPTIONS %s from %s origin=%r safe=%s extra_allowed=%s request_method=%r request_headers=%r allow_origin=%s allow_headers=%r",
        path,
        _client(handler),
        origin,
        allow_origin,
        extra,
        requested_method,
        requested_headers,
        "reflect" if allow_origin else "omit",
        merge_allow_headers(requested_headers),
    )
    if origin and not allow_origin:
        log.warning(
            "[MCP-CORS] OPTIONS %s: Origin %r is not allowed — browser will block POST (no Access-Control-Allow-Origin). "
            "Add it under mcp.cors_allowed_origins in Settings or writeragent.json.",
            path,
            origin,
        )


def log_mcp_transport_entry(handler, transport: str) -> None:
    """Log when POST/GET/DELETE reaches an MCP protocol handler (past routing)."""
    log.info(
        "[MCP-HTTP] %s /%s from %s origin=%r protocol_version=%r session=%r content_length=%s",
        handler.command if hasattr(handler, "command") else "?",
        transport,
        _client(handler),
        _header(handler, "Origin"),
        _protocol_version(handler),
        _header(handler, "Mcp-Session-Id"),
        handler.headers.get("Content-Length", "0"),
    )


def log_unsupported_protocol_version(handler, requested: str) -> None:
    log.warning(
        "[MCP-HTTP] rejected unsupported Mcp-Protocol-Version %r from %s origin=%r",
        requested,
        _client(handler),
        _header(handler, "Origin"),
    )


def log_no_route(handler, method: str, path: str) -> None:
    log.warning("[MCP-HTTP] no route for %s %s from %s", method, path, _client(handler))


def _protocol_version(handler) -> str | None:
    for name in ("Mcp-Protocol-Version", "mcp-protocol-version", "MCP-Protocol-Version"):
        value = handler.headers.get(name)
        if value:
            return value.strip()
    return None
