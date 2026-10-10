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
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("writeragent.mcp.cors")

from plugin.framework.deal_shim import DEAL_MAX_CMD_ARGS, DEAL_MAX_ORIGIN, UNDER_CROSSHAIR, ascii_bounded, deal, inverse_ensure

MCP_CORS_ORIGINS_KEY = "mcp.cors_allowed_origins"

_PRIVATE_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".intern")

_extra_allowed_origins: frozenset[str] = frozenset()
_allow_private_origins: bool = True

# Streamable-HTTP MCP clients preflight with Mcp-Protocol-Version; SSE may use Last-Event-ID.
_BASE_ALLOW_HEADERS = ("Content-Type", "Authorization", "Mcp-Session-Id", "X-Document-URL", "Mcp-Protocol-Version", "Last-Event-ID", "Accept")

_EXPOSE_HEADERS = "Mcp-Session-Id, Mcp-Protocol-Version"

# Loopback hosts formerly matched by ``_ORIGIN_RE``. No regex: CrossHair relib
# on that pattern ate 11:33 (check-all 32877875221).
_SAFE_LOOPBACK_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))

# URL-safe Origin alphabet (scheme/host/port, including IPv6 brackets).
# ascii_bounded(DEAL_MAX_ORIGIN=32) still let SMT wander through urlparse +
# ipaddress on punctuation junk (is_private_browser_origin 20:40 on the same run).
_ORIGIN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:/.-[]")
# Access-Control-Request-Headers: token chars plus comma/space separators.
_HEADER_LIST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-, _")

PREFLIGHT_MAX_AGE = "86400"


def _deal_origin_ok_pytest(origin: object) -> bool:
    # Browser Origin headers are not limited to DEAL_MAX_ORIGIN or the
    # URL-safe alphabet. A junk or huge Origin used to raise
    # PreContractError out of send_cors_headers (HTTP 500). The body
    # returns False / None. CrossHair keeps the closed alphabet.
    return isinstance(origin, str)


def _deal_origin_ok_crosshair(origin: object) -> bool:
    """Closed Origin domain: URL-safe alphabet, DEAL_MAX_ORIGIN length."""
    return isinstance(origin, str) and len(origin) <= DEAL_MAX_ORIGIN and all(c in _ORIGIN_CHARS for c in origin)


_deal_origin_ok = _deal_origin_ok_crosshair if UNDER_CROSSHAIR else _deal_origin_ok_pytest


def _deal_allow_headers_ok_pytest(value: object) -> bool:
    # Access-Control-Request-Headers from a browser can be long and can
    # contain characters outside the token alphabet. The body unions them.
    return isinstance(value, str)


def _deal_allow_headers_ok_crosshair(value: object) -> bool:
    """Preflight header-list domain: ascii tokens, few commas (not 32-char junk)."""
    if not isinstance(value, str) or not ascii_bounded(value, DEAL_MAX_ORIGIN):
        return False
    if value.count(",") > DEAL_MAX_CMD_ARGS:
        return False
    return all(c in _HEADER_LIST_CHARS for c in value)


_deal_allow_headers_ok = (
    _deal_allow_headers_ok_crosshair if UNDER_CROSSHAIR else _deal_allow_headers_ok_pytest
)


def _deal_origins_config_ok_pytest(value: object) -> bool:
    # User config may list more than DEAL_MAX_CMD_ARGS origins. The body
    # drops entries it cannot normalize.
    return value is None or isinstance(value, (str, list))


def _deal_origins_config_ok_crosshair(value: object) -> bool:
    return (
        value is None
        or (isinstance(value, str) and _deal_origin_ok(value))
        or (
            isinstance(value, list)
            and len(value) <= DEAL_MAX_CMD_ARGS
            and all(_deal_origin_ok(item) for item in value)
        )
    )


_deal_origins_config_ok = (
    _deal_origins_config_ok_crosshair if UNDER_CROSSHAIR else _deal_origins_config_ok_pytest
)


@deal.pre(lambda value: value is None or _deal_origin_ok(value))
@deal.post(lambda result: result is None or (isinstance(result, str) and (result.lower().startswith("http://") or result.lower().startswith("https://")) and not result.endswith("/")))
def normalize_cors_origin(value: str | None) -> str | None:
    # crosshair: off
    # cover-all 33689813185 leftover (~32m / 1289 ex) despite _deal_origin_ok. Doable later: closed origin enum.
    """Return a canonical origin URL or None if empty/invalid."""
    if value is None:
        return None
    origin = str(value).strip()
    if not origin:
        return None
    # Origins are compared after parsing, not as exact strings.
    # https://App.Example.com and http://host:80 must match the canonical
    # scheme://host[:port] with scheme and host lowercased.
    try:
        parsed = urlparse(origin)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = parsed.hostname
    if not host:
        return None
    host = host.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    if port is not None:
        netloc = f"{host}:{port}"
    else:
        netloc = host
    return f"{scheme}://{netloc}"


# Deep check-all run 32840960268 hung here at the 360-minute job wall (Prev 9:51
# on the unique-length post, then the runner was still on this FQN at cancel).
# Nested unique-length ensure is skipped under CrossHair; cheap list/str posts stay.
@deal.pre(lambda value: _deal_origins_config_ok(value))
@deal.post(lambda result: isinstance(result, list) and all(isinstance(x, str) for x in result))
@inverse_ensure(lambda value, result: len(result) == len(set(result)))
def normalize_origins_list(value: Any) -> list[str]:
    # crosshair: off  # list/str Any + unique-length post still combinatoric (cover-all 33569420452: ~4040s est / 5594 ex despite _deal_origin_ok). Doable later: closed origin enum.
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
    # crosshair: off  # urlparse + ipaddress SMT wander (cover-all 33569420452: ~939s est / 1298 ex despite _deal_origin_ok). Doable later.
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


@deal.pre(lambda origins: _deal_origins_config_ok(origins))
def set_extra_allowed_origins(origins: Any) -> None:
    # crosshair: off  # frozenset(normalize_origins_list) leftover (cover-all 33569420452: ~4419s est / 6120 ex). Doable later.
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
    # crosshair: off  # frozenset membership under symbolic origin (cover-all 33569420452: ~966s est / 1335 ex). Doable later.
    if len(origin) == 0:
        return False
    normalized = normalize_cors_origin(origin)
    return normalized is not None and normalized in _extra_allowed_origins


def _is_loopback_origin(origin: str) -> bool:
    # crosshair: off  # urlparse hostname walk (cover-all 33569420452: ~939s est / 1298 ex). Doable later.
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


def reload_cors_policy_from_config(services: Any) -> None:
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
    # crosshair: off  # composes loopback/extra/private (cover-all 33569420452: ~966s est / 1335 ex). Doable later.
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


def origin_is_forbidden(handler: Any) -> bool:
    """True when Origin is present and not on the allow list.

    Missing Origin is allowed (CLI / curl / most MCP clients). Do not call
    ``is_safe_origin`` until the value passes ``_deal_origin_ok`` — junk or
    huge Origins would raise ``deal.PreContractError`` into a 500.
    """
    origin = handler.headers.get("Origin")
    if not origin:
        return False
    if not _deal_origin_ok(origin):
        return True
    return not is_safe_origin(origin)


def reject_forbidden_origin(handler: Any) -> bool:
    """If Origin is present and unsafe, write 403 and return True.

    Bug: CORS only omitted Access-Control-Allow-Origin for unsafe browser
    Origins; OPTIONS still returned 204 and POST still ran JSON-RPC. A
    non-preflighted or CORS-ignoring client could mutate. Nelson ca7c2d32
    (0.13.0) 403s any present-and-unsafe Origin with no CORS headers so
    the request never reaches a route. WriterAgent keeps loopback +
    private/LAN defaults; only the already-unsafe set is refused.
    """
    if not origin_is_forbidden(handler):
        return False
    from plugin.mcp.http_trace import log_forbidden_origin

    log_forbidden_origin(handler)
    # No Access-Control-* — a reflected ACAO would let the browser read the 403.
    handler._response_started = True
    handler.send_response(403)
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    return True


def get_configured_tunnel_host() -> str | None:
    """Hostname of the running MCP tunnel's public URL, if any."""
    try:
        from plugin.mcp import _shared_tunnel

        pub_url = _shared_tunnel.public_url if _shared_tunnel is not None else None
    except Exception:
        log.debug("tunnel host lookup for Host check failed", exc_info=True)
        return None
    if not pub_url:
        return None
    return urlparse(pub_url if "://" in pub_url else f"https://{pub_url}").hostname


def _is_specific_bind_host(bind_host: str | None) -> bool:
    """True when *bind_host* names one host (not empty and not a wildcard address)."""
    name = (bind_host or "").strip().strip("[]")
    if not name:
        return False
    try:
        return not ipaddress.ip_address(name).is_unspecified
    except ValueError:
        return True


def is_safe_host(host_header: str | None, tunnel_host: str | None = None, bind_host: str | None = None) -> bool:
    """DNS-rebinding protection: True when Host is localhost / 127.0.0.1 / [::1] (optional port) or tunnel host."""
    if not host_header:
        return False
    raw = host_header.strip()
    if not raw:
        return False
    if raw.startswith("["):
        closing = raw.find("]")
        if closing == -1:
            return False
        host = raw[: closing + 1].lower()
        rest = raw[closing + 1 :]
        if rest and (not rest.startswith(":") or not rest[1:].isdigit()):
            return False
    elif ":" in raw:
        host, rest = raw.split(":", 1)
        if not rest.isdigit():
            return False
        host = host.lower()
    else:
        host = raw.lower()

    if host in _SAFE_LOOPBACK_HOSTS or (host.startswith("[") and host[1:-1] in _SAFE_LOOPBACK_HOSTS):
        return True
    if _is_specific_bind_host(bind_host) and host.strip("[]") == str(bind_host).strip().strip("[]").lower():
        return True

    active_tunnel = tunnel_host or get_configured_tunnel_host()
    if active_tunnel:
        th = active_tunnel.strip()
        if "://" in th:
            th = urlparse(th).hostname or th
        elif ":" in th and not th.startswith("["):
            th = th.split(":", 1)[0]
        if host == th.lower():
            return True
    return False


def reject_forbidden_host(handler: Any) -> bool:
    """If Host header is missing or unsafe (DNS rebinding), write 403 and return True."""
    host_header = handler.headers.get("Host") if hasattr(handler, "headers") and handler.headers else None
    if is_safe_host(host_header, bind_host=getattr(getattr(handler, "server", None), "bind_host", None)):
        return False
    log.warning("Rejecting request with forbidden Host header: %r", host_header)
    handler._response_started = True
    handler.send_response(403)
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    return True


@deal.pre(lambda access_control_request_headers: access_control_request_headers is None or _deal_allow_headers_ok(access_control_request_headers))
@deal.post(lambda result: isinstance(result, str))
@inverse_ensure(lambda access_control_request_headers, result: "Content-Type" in result)
def merge_allow_headers(access_control_request_headers: str | None) -> str:
    # crosshair: off  # split/merge header list still SMT-heavy (cover-all 33569420452: ~1393s est / 1927 ex despite _deal_allow_headers_ok). Doable later.
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


def send_cors_headers(handler: Any, *, preflight: bool = False) -> None:
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
