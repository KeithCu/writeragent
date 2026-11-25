# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure HTTP redaction, URL validation, and redirect policy helpers.

These helpers carry no connection state, I/O, threads, or logging.
``http_transport`` calls them without re-exporting them.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from plugin.framework.errors import NetworkError
from plugin.framework.log_redaction import (
    bracket_ipv6_host,
    is_credential_name,
    is_credential_query_name,
    opaque_segments,
    public_path,
    redact_secrets,
    request_path_secrets,
    without_userinfo,
)


# urllib followed these. 304/300 are not automatic hops.
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def secrets_from_headers(headers: dict[str, str] | None) -> list[str]:
    """Bearer tokens and credential header values that must not appear in errors."""
    secrets: list[str] = []
    for key, value in (headers or {}).items():
        if not value or not is_credential_name(str(key)):
            continue
        low = str(key).lower()
        raw = str(value).strip()
        parts = raw.split(None, 1)
        token = parts[1].strip() if low == "authorization" and len(parts) == 2 else raw
        if token:
            secrets.append(token)
    return secrets


def secrets_from_target(url_or_path: str) -> list[str]:
    """Credential query values, URL userinfo password, and opaque path segments before model ids, as written and decoded."""
    raw = url_or_path or ""
    if "://" not in raw:
        raw = "http://placeholder.invalid" + (raw if raw.startswith("/") else "/" + raw)
    try:
        parsed = urllib.parse.urlparse(raw)
    except (ValueError, TypeError):
        return []
    secrets: list[str] = []
    for pair in parsed.query.split("&"):
        raw_key, _sep, raw_value = pair.partition("=")
        if raw_value and is_credential_query_name(urllib.parse.unquote_plus(raw_key)):
            secrets.extend((raw_value, urllib.parse.unquote_plus(raw_value)))
    password = parsed.password
    if password:
        secrets.extend((password, urllib.parse.unquote(password)))
    secrets.extend(request_path_secrets(parsed.path))
    return secrets


def secrets_from_url_path(url: str) -> list[str]:
    """Path segments of *url* that look like an opaque credential, as written and percent-decoded.

    Scans the whole path for MCP links (``/s/<secret>/mcp``); ``secrets_from_target``
    stops at model ids.
    """
    return opaque_segments(_parse_url(url).path.split("/"))


def collect_secrets(*groups: list[str] | None) -> list[str]:
    """Merge secret lists, dropping empties."""
    return [s for g in groups if g for s in g if s]


def _invalid_url_error(url: str, message: str = "Invalid URL") -> NetworkError:
    """``INVALID_URL`` for a URL that ``urlparse`` / ``urljoin`` reject or has a bad port.

    Userinfo, query strings and fragments stay out of the details, and opaque
    path segments are redacted.
    """
    parts = re.split(r"[?#]", url or "", maxsplit=1)
    shown = parts[0]
    tail = parts[1] if len(parts) > 1 else ""
    scheme, sep, rest = shown.partition("://")
    if not sep:
        scheme, rest = "", shown
    rest = without_userinfo(rest, tail)
    rest = redact_secrets(rest, request_path_secrets(rest.partition("/")[2]))
    return NetworkError(message, code="INVALID_URL", details={"url": f"{scheme}{sep}{rest}"})


def _parse_url(url: str) -> urllib.parse.ParseResult:
    """``urlparse``, with an invalid bracket URL as ``NetworkError``.

    ``urlparse`` raises ``ValueError`` ("Invalid IPv6 URL", or an empty or
    illegal address inside ``[]``) for ``http://[::1``, ``http://[::1]extra``,
    and ``http://[]/v1`` before ``_explicit_port`` or ``bracket_ipv6_host``
    run. A bad bracket URL is ``INVALID_URL``, the same contract as a bad port.
    """
    try:
        return urllib.parse.urlparse(url)
    except ValueError:
        # ValueError text can quote the whole netloc, password included, and log.exception prints causes.
        raise _invalid_url_error(url) from None


def _explicit_port(parsed: urllib.parse.ParseResult) -> int | None:
    """Explicit URL port, or None when the URL omits one.

    ``ParseResult.port`` raises ``ValueError`` for ``localhost:1a34`` and for
    ports outside 0–65535. urllib checks the port only when ``.port`` is read,
    and ``sync_request`` calls ``origin_and_path`` before its try. A bad port
    is ``INVALID_URL``; callers already handle ``NetworkError``.
    """
    try:
        return parsed.port
    except ValueError:
        host = bracket_ipv6_host(parsed.hostname or "")
        scheme = parsed.scheme or "http"
        raise _invalid_url_error(f"{scheme}://{host}{parsed.path or '/'}", "Invalid URL port") from None


def _origin_key(url: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` with the default port filled in."""
    parsed = _parse_url(url)
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    port = _explicit_port(parsed)
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port



def apply_redirect(
    method: str,
    body: Any,
    headers: dict[str, str],
    current_url: str,
    status: int,
    location: str,
) -> tuple[str, Any, dict[str, str], str, str, bool] | None:
    """Next hop for one redirect, or None when it must not be followed.

    301/302/303 turn a non-GET/HEAD into GET and drop the body. 307/308 keep
    the method and body. Secret headers are dropped when the host changes.
    Only http and https targets are followed. A bad port or an unmatched
    bracket URL is ``NetworkError``.
    """
    loc = (location or "").strip()
    if not loc or any(ch in loc for ch in "\r\n\x00"):
        return None
    # ``urljoin`` splits the URL the same way ``urlparse`` does and raises
    # ``ValueError`` for ``Location: http://[::1`` before ``_explicit_port``
    # runs. That would skip ``exchange``'s NetworkError catch. Same
    # invalid-URL contract as a bad port.
    try:
        joined = urllib.parse.urljoin(current_url, loc.replace(" ", "%20"))
    except ValueError:
        # Relative Location: the bad brackets are on the current URL.
        blame = loc if "://" in loc else current_url
        raise _invalid_url_error(blame) from None
    parsed = _parse_url(joined)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    if not parsed.hostname:
        return None
    origin, path = origin_and_path(joined)
    new_method = method
    new_body = body
    new_headers = dict(headers)
    # Browsers and urllib turn these into GET and drop the body. 307/308 keep both.
    if status in (301, 302, 303) and method.upper() not in ("GET", "HEAD"):
        new_method = "GET"
        new_body = None
        new_headers = {key: value for key, value in new_headers.items() if key.lower() not in ("content-length", "content-type")}
    cross_origin = _origin_key(current_url) != _origin_key(origin)
    if cross_origin:
        new_headers = {key: value for key, value in new_headers.items() if not is_credential_name(key)}
    return new_method, new_body, new_headers, origin, path, cross_origin


def public_target(url_or_path: str) -> str:
    """Host and path for logs and error details. Query, fragment, userinfo and opaque path segments are dropped or redacted."""
    raw = url_or_path or ""
    shown, *cut = re.split(r"[?#]", raw, maxsplit=1)
    # One leading "/" is a request path, where "@" is data ("claude@20240620"). Other text without a
    # scheme is a host or a network-path reference, which can start with userinfo.
    if shown.startswith("/") and not shown.startswith("//"):
        return public_path(shown)
    if "://" not in shown:
        return public_path(without_userinfo(shown, "".join(cut)) or "/")
    parsed = _parse_url(raw)
    host = bracket_ipv6_host(parsed.hostname or "")
    port_num = _explicit_port(parsed)
    port = f":{port_num}" if port_num else ""
    path = public_path(parsed.path or "/")
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}{port}{path}"


def log_target(url_or_path: str) -> str:
    """``public_target`` for error text, bare request paths, and raw schemeless endpoints; never raises.

    ``RedactingFormatter`` cannot identify bare paths or schemeless ``user:pw@host`` tokens in prose,
    so those callers keep ``log_target``.
    """
    try:
        return public_target(url_or_path)
    except NetworkError as exc:
        return exc.details["url"]



def origin_and_path(url: str) -> tuple[str, str]:
    """Split an absolute URL into the transport origin and the request target."""
    parsed = _parse_url(url)
    scheme = (parsed.scheme or "https").lower()
    host = bracket_ipv6_host(parsed.hostname or "")
    port = _explicit_port(parsed)
    origin = f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return origin, path
