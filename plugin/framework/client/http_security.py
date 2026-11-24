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
from plugin.framework.log_redaction import is_secret_key


# urllib followed these. 304/300 are not automatic hops.
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


# UUIDs, hex keys, base64 and nanoid links are longer than this.
_OPAQUE_SEGMENT_MIN_CHARS = 16
# A model id follows these segments and errors must show it.
_MODEL_ID_PARENTS = frozenset({"models", "deployments"})


def is_credential_name(name: str) -> bool:
    """True when an HTTP header called *name* carries a credential.

    Also the base of ``_is_credential_query_name``. Broader than ``is_secret_key``, which also
    judges tool arguments, where names such as ``sort_key`` are data. ``max_tokens`` and
    ``output_modalities`` are not credentials.
    """
    return is_secret_key(name) or name.lower().endswith(("key", "token"))


def _is_credential_query_name(name: str) -> bool:
    """True when a URL query parameter called *name* carries a credential.

    Azure Functions passes its key as ``code`` and signed URLs use ``sig``, ``signature`` or
    ``X-Amz-Signature``. Headers with those names are ordinary data.
    """
    low = name.lower()
    return is_credential_name(name) or low in ("code", "sig") or low.endswith("signature")


def redact_secrets(text: str, secrets: list[str] | None) -> str:
    """Replace credential strings with ``<redacted>``. Longer values first."""
    if not text or not secrets:
        return text
    # A one-character key would punch holes through ordinary error text ("k", "1").
    ordered = sorted({secret for secret in secrets if secret and len(secret) >= 4}, key=len, reverse=True)
    for secret in ordered:
        text = text.replace(secret, "<redacted>")
    return text


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
        if raw_value and _is_credential_query_name(urllib.parse.unquote_plus(raw_key)):
            secrets.extend((raw_value, urllib.parse.unquote_plus(raw_value)))
    password = parsed.password
    if password:
        secrets.extend((password, urllib.parse.unquote(password)))
    secrets.extend(_request_path_secrets(parsed.path))
    return secrets


def _opaque_segments(segments: list[str]) -> list[str]:
    secrets: list[str] = []
    for segment in segments:
        decoded = urllib.parse.unquote(segment)
        # All-lowercase segments without digits are words ("model-context-protocol").
        if len(decoded) >= _OPAQUE_SEGMENT_MIN_CHARS and any(ch.isdigit() or ch.isupper() for ch in decoded):
            secrets.extend((segment, decoded))
    return secrets


def _request_path_secrets(path: str) -> list[str]:
    segments = path.split("/")
    for idx, segment in enumerate(segments):
        if segment in _MODEL_ID_PARENTS:
            segments = segments[:idx]
            break
    return _opaque_segments(segments)


def _public_path(path: str) -> str:
    return redact_secrets(path, _request_path_secrets(path))


def secrets_from_url_path(url: str) -> list[str]:
    """Path segments of *url* that look like an opaque credential, as written and percent-decoded.

    Scans the whole path for MCP links (``/s/<secret>/mcp``); ``secrets_from_target``
    stops at model ids.
    """
    return _opaque_segments(_parse_url(url).path.split("/"))
def collect_secrets(*groups: list[str] | None) -> list[str]:
    """Merge secret lists, dropping empties."""
    return [s for g in groups if g for s in g if s]


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


def _invalid_url_error(url: str, message: str = "Invalid URL") -> NetworkError:
    """``INVALID_URL`` for a URL that ``urlparse`` / ``urljoin`` reject or has a bad port.

    Userinfo, query strings and fragments stay out of the details, and opaque
    path segments are redacted.
    """
    shown = re.split(r"[?#]", url or "", maxsplit=1)[0]
    scheme, sep, rest = shown.partition("://")
    if not sep:
        scheme, rest = "", shown
    # The URL did not parse, so a raw "/" may be part of the password. Keep only what follows the last "@".
    rest = rest.rpartition("@")[2]
    rest = redact_secrets(rest, _request_path_secrets(rest.partition("/")[2]))
    return NetworkError(message, code="INVALID_URL", details={"url": f"{scheme}{sep}{rest}"})


def _parse_url(url: str) -> urllib.parse.ParseResult:
    """``urlparse``, with an invalid bracket URL as ``NetworkError``.

    ``urlparse`` raises ``ValueError`` ("Invalid IPv6 URL", or an empty or
    illegal address inside ``[]``) for ``http://[::1``, ``http://[::1]extra``,
    and ``http://[]/v1`` before ``_explicit_port`` or ``_bracket_ipv6_host``
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
        host = _bracket_ipv6_host(parsed.hostname or "")
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
    """Host and path for logs and error details. Query, userinfo and opaque path segments are dropped or redacted."""
    raw = url_or_path or ""
    if "://" not in raw:
        path = raw.split("?", 1)[0]
        return _public_path(path or "/")
    parsed = _parse_url(raw)
    host = _bracket_ipv6_host(parsed.hostname or "")
    port_num = _explicit_port(parsed)
    port = f":{port_num}" if port_num else ""
    path = _public_path(parsed.path or "/")
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}{port}{path}"


def log_target(url_or_path: str) -> str:
    """``public_target`` for log lines and error text. A URL that cannot be parsed gives its sanitized spelling instead of raising."""
    try:
        return public_target(url_or_path)
    except NetworkError as exc:
        return exc.details["url"]



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
