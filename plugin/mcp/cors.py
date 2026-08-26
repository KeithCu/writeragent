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
"""CORS for the MCP HTTP server: origin policy, config cache, and response headers."""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

log = logging.getLogger("writeragent.mcp.cors")

from plugin.framework.deal_shim import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_ORIGIN,
    ascii_bounded,
    str_bounded,
    deal,
    inverse_ensure,
)

MCP_CORS_ORIGINS_KEY = "mcp.cors_allowed_origins"

_PRIVATE_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".intern")

_extra_allowed_origins: frozenset[str] = frozenset()
_allow_private_origins: bool = True

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

# Loopback hosts formerly matched by ``_ORIGIN_RE``. No regex: CrossHair relib
# on that pattern ate 11:33 (check-all 32877875221).
_SAFE_LOOPBACK_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))

# URL-safe Origin alphabet (scheme/host/port, including IPv6 brackets).
# ascii_bounded(DEAL_MAX_ORIGIN=32) still let SMT wander through urlparse +
# ipaddress on punctuation junk (is_private_browser_origin 20:40 on the same run).
_ORIGIN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:/.-[]"
)
# Access-Control-Request-Headers: token chars plus comma/space separators.
_HEADER_LIST_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-, _"
)

PREFLIGHT_MAX_AGE = "86400"


def _deal_origin_ok(origin: object) -> bool:
    """Closed Origin domain: URL-safe alphabet, DEAL_MAX_ORIGIN length."""
    return isinstance(origin, str) and len(origin) <= DEAL_MAX_ORIGIN and all(c in _ORIGIN_CHARS for c in origin)


def _deal_allow_headers_ok(value: object) -> bool:
    """Preflight header-list domain: ascii tokens, few commas (not 32-char junk)."""
    if not isinstance(value, str) or not ascii_bounded(value, DEAL_MAX_ORIGIN):
        return False
    if value.count(",") > DEAL_MAX_CMD_ARGS:
        return False
    return all(c in _HEADER_LIST_CHARS for c in value)


@deal.pre(lambda value: value is None or str_bounded(value, DEAL_MAX_ORIGIN))
@deal.post(
    lambda result: result is None
    or (
        isinstance(result, str)
        and (result.lower().startswith("http://") or result.lower().startswith("https://"))
        and not result.endswith("/")
    )
)
def normalize_cors_origin(value: str | None) -> str | None:
    """Return a canonical origin URL or None if empty/invalid."""
    if value is None:
        return None
    origin = str(value).strip()
    if not origin:
        return None
    if origin.endswith("/"):
        origin = origin.rstrip("/")
    lower = origin.lower()
    if not (lower.startswith("http://") or lower.startswith("https://")):
        return None
    return origin


# Deep check-all run 32840960268 hung here at the 360-minute job wall (Prev 9:51
# on the unique-length post, then the runner was still on this FQN at cancel).
# Nested unique-length ensure is skipped under CrossHair; cheap list/str posts stay.
@deal.pre(
    lambda value: value is None
    or (isinstance(value, str) and str_bounded(value, DEAL_MAX_ORIGIN))
    or (
        isinstance(value, list)
        and len(value) <= DEAL_MAX_CMD_ARGS
        and all(not isinstance(item, str) or str_bounded(item, DEAL_MAX_ORIGIN) for item in value)
    )
    or not isinstance(value, (str, list))
)
@deal.post(lambda result: isinstance(result, list) and all(isinstance(x, str) for x in result))
@inverse_ensure(lambda value, result: len(result) == len(set(result)))
def normalize_origins_list(value) -> list[str]:
    """Coerce config value to a deduped list of normalized origin strings."""
    if value is None:
        return []
    if isinstance(value, str):
        one = normalize_cors_origin(value)
        return [one] if one else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        origin = normalize_cors_origin(item)
        if origin and origin not in out:
            out.append(origin)
    return out


@deal.pre(lambda origin: _deal_origin_ok(origin))
@deal.post(lambda result: isinstance(result, bool))
def is_private_browser_origin(origin: str) -> bool:
    """True when Origin is http(s) with a LAN-style hostname or private/link-local IP."""
    normalized = normalize_cors_origin(origin)
    if normalized is None:
        return False
    # Spoofed bracket hostnames (e.g. [::1].evil.net) must not crash the handler;
    # stdlib urlparse raises ValueError on invalid IPv6 URL syntax.
    try:
        parsed = urlparse(normalized)
    except ValueError:
        return False
    host = parsed.hostname
    if host is None:
        return False
    h = host.lower()
    if h.endswith(_PRIVATE_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Do not wrap in bool(): CrossHair SymbolicBool TypeError (same class as
    # bool(shape) on should_use_binary_envelope, check-all 32877875221 20:40).
    return ip.is_private or ip.is_loopback or ip.is_link_local


def set_extra_allowed_origins(origins) -> None:
    """Update explicit-origin cache used by is_safe_origin (HTTP threads, no ctx)."""
    global _extra_allowed_origins
    _extra_allowed_origins = frozenset(normalize_origins_list(origins))


def get_extra_allowed_origins() -> frozenset[str]:
    return _extra_allowed_origins


def get_allow_private_origins() -> bool:
    return _allow_private_origins


def set_allow_private_origins(allow: bool) -> None:
    global _allow_private_origins
    _allow_private_origins = bool(allow)


@deal.pre(lambda origin: _deal_origin_ok(origin))
@deal.post(lambda result: isinstance(result, bool))
def is_extra_allowed_origin(origin: str) -> bool:
    if len(origin) == 0:
        return False
    normalized = normalize_cors_origin(origin)
    return normalized is not None and normalized in _extra_allowed_origins


def _is_loopback_origin(origin: str) -> bool:
    """True for http(s) localhost / 127.0.0.1 / ::1 (optional port). No regex."""
    normalized = normalize_cors_origin(origin)
    if normalized is None:
        return False
    try:
        parsed = urlparse(normalized)
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = parsed.hostname
    if host is None:
        return False
    return host.lower() in _SAFE_LOOPBACK_HOSTS


def reload_cors_policy_from_config(services) -> None:
    """Refresh CORS caches from mcp config (explicit list + private-origin JSON setting)."""
    # crosshair: off
    try:
        cfg = services.config.proxy_for("mcp")
        raw = cfg.get("cors_allowed_origins")
        allow_private = cfg.get("cors_allow_private_origins")
    except Exception as e:
        log.warning("Could not load MCP CORS config: %s", e)
        raw = []
        allow_private = True
    origins = normalize_origins_list(raw)
    set_extra_allowed_origins(origins)
    set_allow_private_origins(allow_private if allow_private is not None else True)
    if origins:
        log.info("MCP CORS explicit allowed origins: %s", ", ".join(origins))
    log.debug("MCP CORS allow private/local browser origins: %s", _allow_private_origins)


@deal.pre(lambda origin: _deal_origin_ok(origin))
@deal.post(lambda result: isinstance(result, bool))
def is_safe_origin(origin: str) -> bool:
    """True when Origin may receive Access-Control-Allow-Origin reflection."""
    if len(origin) == 0:
        return False
    if _is_loopback_origin(origin):
        return True
    if is_extra_allowed_origin(origin):
        return True
    if get_allow_private_origins() and is_private_browser_origin(origin):
        return True
    return False


@deal.pre(
    lambda access_control_request_headers: access_control_request_headers is None
    or _deal_allow_headers_ok(access_control_request_headers)
)
@deal.post(lambda result: isinstance(result, str))
@inverse_ensure(lambda access_control_request_headers, result: "Content-Type" in result)
def merge_allow_headers(access_control_request_headers: str | None) -> str:
    """Build Access-Control-Allow-Headers: base list union preflight request list."""
    merged: dict[str, str] = {}
    for header in _BASE_ALLOW_HEADERS:
        merged[header.lower()] = header
    if access_control_request_headers is not None and len(access_control_request_headers) > 0:
        for header in (h.strip() for h in access_control_request_headers.split(",") if h.strip()):
            key = header.lower()
            if key not in merged:
                merged[key] = header
    return ", ".join(merged.values())


def send_cors_headers(handler, *, preflight: bool = False) -> None:
    """Apply CORS headers to an HTTP request handler (GenericRequestHandler or MCP raw handler)."""
    # crosshair: off
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
