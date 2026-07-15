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
import re
from urllib.parse import urlparse

log = logging.getLogger("writeragent.mcp.cors")

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

_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")

PREFLIGHT_MAX_AGE = "86400"


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


def is_private_browser_origin(origin: str) -> bool:
    """True when Origin is http(s) with a LAN-style hostname or private/link-local IP."""
    normalized = normalize_cors_origin(origin)
    if not normalized:
        return False
    # Spoofed bracket hostnames (e.g. [::1].evil.net) must not crash the handler;
    # stdlib urlparse raises ValueError on invalid IPv6 URL syntax.
    try:
        parsed = urlparse(normalized)
    except ValueError:
        return False
    host = parsed.hostname
    if not host:
        return False
    h = host.lower()
    if h.endswith(_PRIVATE_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


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


def is_extra_allowed_origin(origin: str) -> bool:
    if not origin:
        return False
    normalized = normalize_cors_origin(origin)
    return bool(normalized and normalized in _extra_allowed_origins)


def reload_cors_policy_from_config(services) -> None:
    """Refresh CORS caches from mcp config (explicit list + private-origin JSON setting)."""
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


def reload_extra_allowed_origins_from_config(services) -> None:
    """Backward-compatible alias for reload_cors_policy_from_config."""
    reload_cors_policy_from_config(services)


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
