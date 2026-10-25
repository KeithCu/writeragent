# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure HTTP redaction, URL validation, and redirect policy helpers.

These helpers carry no connection state, I/O, threads, or logging.
``http_transport`` calls them without re-exporting them.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from plugin.framework.errors import NetworkError


# Header names whose values are credentials. Matched case-insensitively.
# Cookie and proxy-authorization are credentials too. They are stripped on
# cross-origin redirect hops and redacted from logs, along with authorization
# and API-key variants.
_SECRET_HEADER_NAMES = frozenset({"authorization", "x-api-key", "api-key", "x-goog-api-key", "cookie", "proxy-authorization"})
# Query keys that carry credentials. ``output_modalities`` is not one of these.
_SECRET_QUERY_KEYS = frozenset({"api_key", "apikey", "api-key", "key", "token", "access_token"})
# A one-character key would punch holes through ordinary error text ("k", "1").
_MIN_SECRET_LEN = 4
# urllib followed these. 304/300 are not automatic hops.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Browsers and urllib turn these into GET and drop the body. 307/308 keep both.
_REDIRECT_SWITCH_TO_GET = frozenset({301, 302, 303})


def redact_secrets(text: str, secrets: list[str] | None) -> str:
    """Replace credential strings with ``<redacted>``. Longer values first."""
    if not text or not secrets:
        return text
    ordered = sorted({secret for secret in secrets if secret and len(secret) >= _MIN_SECRET_LEN}, key=len, reverse=True)
    for secret in ordered:
        text = text.replace(secret, "<redacted>")
    return text


def secrets_from_headers(headers: dict[str, str] | None) -> list[str]:
    """Bearer tokens and API-key header values that must not appear in errors."""
    if not headers:
        return []
    secrets: list[str] = []
    for key, value in headers.items():
        if not value:
            continue
        low = str(key).lower()
        if low in ("x-api-key", "api-key", "x-goog-api-key"):
            token = str(value).strip()
        elif low == "authorization":
            parts = str(value).split(None, 1)
            token = parts[1].strip() if len(parts) == 2 else str(value).strip()
        elif low not in _SECRET_HEADER_NAMES:
            continue
        else:
            token = str(value).strip()
        if token and token not in secrets:
            secrets.append(token)
    return secrets


def secrets_from_target(url_or_path: str) -> list[str]:
    """Credential query values and URL userinfo passwords."""
    raw = url_or_path or ""
    if "://" not in raw:
        raw = "http://placeholder.invalid" + (raw if raw.startswith("/") else "/" + raw)
    try:
        parsed = urllib.parse.urlparse(raw)
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    except (ValueError, TypeError):
        return []
    secrets: list[str] = []
    for key, value in pairs:
        if key.lower() in _SECRET_QUERY_KEYS and value and value not in secrets:
            secrets.append(value)
    password = parsed.password
    if password:
        revealed = urllib.parse.unquote(password)
        if revealed and revealed not in secrets:
            secrets.append(revealed)
    return secrets


def collect_secrets(*groups: list[str] | None) -> list[str]:
    """Merge secret lists, dropping empties and duplicates."""
    found: list[str] = []
    for group in groups:
        if not group:
            continue
        for secret in group:
            if secret and secret not in found:
                found.append(secret)
    return found


def _bracket_ipv6_host(host: str) -> str:
    """Put brackets back around an IPv6 literal so the origin can be parsed again.

    ``ParseResult.hostname`` strips the brackets RFC 3986 requires. Rebuilding
    ``http://[::1]:11434`` as ``http://::1:11434`` makes the next parse read
    ``:1:11434`` as the port, and ``_explicit_port`` raises ``INVALID_URL``.
    A colon in the host is an IPv6 literal; wrap it once.
    """
    if ":" not in host or host.startswith("["):
        return host
    return f"[{host}]"


def _invalid_url_error(url: str) -> NetworkError:
    """``INVALID_URL`` for a URL that ``urlparse`` / ``urljoin`` reject.

    Query strings stay out of the details. The same code as a bad port.
    """
    shown = (url or "").split("?", 1)[0]
    return NetworkError("Invalid URL", code="INVALID_URL", details={"url": shown})


def _parse_url(url: str) -> urllib.parse.ParseResult:
    """``urlparse``, with an invalid bracket URL as ``NetworkError``.

    ``urlparse`` raises ``ValueError`` ("Invalid IPv6 URL", or an empty or
    illegal address inside ``[]``) for ``http://[::1``, ``http://[::1]extra``,
    and ``http://[]/v1`` before ``_explicit_port`` or ``_bracket_ipv6_host``
    run. A bad bracket URL is ``INVALID_URL``, the same contract as a bad port.
    """
    try:
        return urllib.parse.urlparse(url)
    except ValueError as exc:
        raise _invalid_url_error(url) from exc


def _explicit_port(parsed: urllib.parse.ParseResult) -> int | None:
    """Explicit URL port, or None when the URL omits one.

    ``ParseResult.port`` raises ``ValueError`` for ``localhost:1a34`` and for
    ports outside 0–65535. urllib checks the port only when ``.port`` is read,
    and ``sync_request`` calls ``origin_and_path`` before its try. A bad port
    is ``INVALID_URL``; callers already handle ``NetworkError``.
    """
    try:
        return parsed.port
    except ValueError as exc:
        host = ""
        try:
            host = parsed.hostname or ""
        except ValueError:
            host = ""
        scheme = parsed.scheme or "http"
        path = parsed.path or "/"
        raise NetworkError("Invalid URL port", code="INVALID_URL", details={"url": f"{scheme}://{host}{path}"}) from exc


def _origin_key(url: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` with the default port filled in."""
    parsed = _parse_url(url)
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    port = _explicit_port(parsed)
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port



def _apply_redirect(  # pyright: ignore[reportUnusedFunction]
    method: str,
    body: Any,
    headers: dict[str, str],
    current_url: str,
    status: int,
    location: str,
) -> tuple[str, Any, dict[str, str], str, str] | None:
    """Next hop for one redirect, or None when it must not be followed.

    301/302/303 turn a non-GET/HEAD into GET and drop the body. 307/308 keep
    the method and body. Secret headers are dropped when the host changes.
    Only http and https targets are followed. A bad port or an unmatched
    bracket URL is ``NetworkError``.
    """
    if status not in _REDIRECT_STATUSES:
        return None
    loc = (location or "").strip()
    if not loc or any(ch in loc for ch in "\r\n\x00"):
        return None
    # ``urljoin`` splits the URL the same way ``urlparse`` does and raises
    # ``ValueError`` for ``Location: http://[::1`` before ``_explicit_port``
    # runs. That would skip ``exchange``'s NetworkError catch. Same
    # invalid-URL contract as a bad port.
    try:
        joined = urllib.parse.urljoin(current_url, loc.replace(" ", "%20"))
    except ValueError as exc:
        # Relative Location: the bad brackets are on the current URL.
        blame = loc if "://" in loc else current_url
        raise _invalid_url_error(blame) from exc
    parsed = _parse_url(joined)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    host = _bracket_ipv6_host(parsed.hostname or "")
    if not host:
        return None
    port = _explicit_port(parsed)
    origin = f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    new_method = method
    new_body = body
    new_headers = dict(headers)
    if status in _REDIRECT_SWITCH_TO_GET and method.upper() not in ("GET", "HEAD"):
        new_method = "GET"
        new_body = None
        new_headers = {key: value for key, value in new_headers.items() if key.lower() not in ("content-length", "content-type")}
    if _origin_key(current_url) != _origin_key(origin):
        new_headers = {key: value for key, value in new_headers.items() if key.lower() not in _SECRET_HEADER_NAMES}
    return new_method, new_body, new_headers, origin, path


def public_target(url_or_path: str) -> str:
    """Host and path for logs and error details. Query and userinfo are dropped."""
    raw = url_or_path or ""
    if "://" not in raw:
        path = raw.split("?", 1)[0]
        return path or "/"
    parsed = _parse_url(raw)
    host = parsed.hostname or ""
    port_num = _explicit_port(parsed)
    port = f":{port_num}" if port_num else ""
    path = parsed.path or "/"
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}{port}{path}"



def origin_and_path(url: str) -> tuple[str, str]:
    """Split an absolute URL into the transport origin and the request target."""
    parsed = _parse_url(url)
    scheme = (parsed.scheme or "https").lower()
    host = _bracket_ipv6_host(parsed.hostname or "")
    port = _explicit_port(parsed)
    origin = f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return origin, path
