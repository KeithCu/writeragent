# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import logging
import urllib.error
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from plugin.framework.constants import USER_AGENT
from plugin.framework.errors import NetworkError
from .request_controls import RETRY_MAX_ATTEMPTS, RETRYABLE_HTTP_STATUS, LocalHttpsCertificateFallback, backoff_delay_sec, parse_retry_after, wait_abortable
from .ssl_helpers import _is_certificate_verify_error, get_verified_ssl_context, get_unverified_ssl_context
from plugin.framework.errors import format_error_message
from .errors import _format_http_error_response

log = logging.getLogger(__name__)


def _log_request_target(url: Any) -> str:
    """Host and path for logs. Query strings can carry API keys."""
    raw = getattr(url, "full_url", url)
    parsed = urlparse(str(raw))
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/"
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}{port}{path}"


def _header_secrets(headers: dict[str, str] | None, req: Any) -> list[str]:
    """Bearer tokens and x-api-key values that must not appear in an error string."""
    items: list[tuple[str, str]] = []
    if headers:
        items.extend((str(key), str(value)) for key, value in headers.items() if value)
    req_headers = getattr(req, "headers", None)
    if req_headers is not None:
        try:
            items.extend((str(key), str(value)) for key, value in dict(req_headers).items() if value)
        except (TypeError, ValueError):
            pass
    secrets: list[str] = []
    for key, value in items:
        low = key.lower()
        if low in ("x-api-key", "api-key"):
            token = value.strip()
        elif low == "authorization":
            parts = value.split(None, 1)
            token = parts[1].strip() if len(parts) == 2 else value.strip()
        else:
            continue
        if token and token not in secrets:
            secrets.append(token)
    return secrets


def _redact_secrets(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def sync_request(url: str | Request, data: bytes | None = None, headers: dict[str, str] | None = None, parse_json: bool = True, method: str | None = None, *, timeout: float) -> Any:
    """
    Blocking HTTP GET or POST. Shared by LLM client and other code.
    url: str or urllib.request.Request. If Request, headers/data come from it.
    data: optional bytes for POST. headers: optional dict (used only if url is str).
    timeout: required seconds for connect+read (no silent default — callers must
    pass Settings ``request_timeout`` / ``LlmClient._timeout()`` for LLM and
    image work, or an explicit short probe value at the call site).
    Returns response data: decoded JSON if parse_json else raw bytes. Raises on error.
    """
    if headers is None:
        headers = {}

    # Add default User-Agent header to identify WriterAgent
    has_ua = any(k.lower() == "user-agent" for k in headers.keys())
    if not has_ua:
        headers["User-Agent"] = USER_AGENT

    if isinstance(url, str):
        req = Request(url, data=data, headers=headers, method=method)
    else:
        req = url

    full_url = getattr(req, "full_url", url)
    # What was wrong: details['url'] kept the query, and format_error_payload
    # copies details into the UI payload, so ``?api_key=`` left the process.
    # Stash the same host-plus-path string the debug line already logs.
    logged_target = _log_request_target(full_url)
    parsed = urlparse(str(full_url))
    host = parsed.hostname or ""
    is_https = parsed.scheme.lower() == "https"

    # Debug: log which headers we are actually sending (keys only)
    try:
        header_keys = list(req.headers.keys()) if hasattr(req, "headers") else []
        if not header_keys and hasattr(req, "get_full_url"):
            # If it's a urllib Request object, headers might be in .headers
            pass
        log.debug("Request to %s with header keys: %s", logged_target, header_keys)
    except Exception:
        pass

    def _read_with_context(context: Any) -> Any:
        log.debug("About to open URL: %s", logged_target)
        with urlopen(req, timeout=timeout, context=context) as resp:
            log.debug(f"URL opened, status={resp.getcode()}. Heading to read...")
            raw = resp.read()
            log.debug(f"Read {len(raw)} bytes")
            if parse_json:
                return json.loads(raw.decode("utf-8"))
            return raw

    # Always verify first unless this process already recorded a local cert failure.
    cert_fallback = LocalHttpsCertificateFallback()
    ctx = get_unverified_ssl_context() if is_https and cert_fallback.ssl_mode_for("https", host) == "unverified" else get_verified_ssl_context()
    sends_left = RETRY_MAX_ATTEMPTS
    attempt = 0

    header_secrets = _header_secrets(headers, req)

    def _http_error(e: urllib.error.HTTPError) -> NetworkError:
        status = e.code
        reason = e.reason
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        msg = _format_http_error_response(status, reason, err_body)
        # What was wrong: the NetworkError kept the raw provider body. Catalog
        # fetch logs ``str(e)``, so a 401 that echoed Authorization / x-api-key
        # landed in writeragent_debug.log. Chat HTTP errors already redact.
        msg = _redact_secrets(msg, header_secrets)
        log.exception("HTTP Error %s %s for %s", status, reason, logged_target)
        return NetworkError(msg, code="HTTP_ERROR", details={"url": logged_target, "status": status})

    while True:
        try:
            return _read_with_context(ctx)
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP_STATUS and sends_left > 1:
                sends_left -= 1
                attempt += 1
                retry_after = None
                headers_obj = getattr(e, "headers", None)
                if headers_obj is not None:
                    retry_after = parse_retry_after(headers_obj.get("Retry-After"))
                delay = backoff_delay_sec(attempt=attempt, retry_after_sec=retry_after)
                # Catalog and update checks have no Stop callback. Chat stays on LlmHttpTransport.
                wait_abortable(delay, None)
                continue
            raise _http_error(e) from e
        except NetworkError:
            raise
        except Exception as e:
            if is_https and cert_fallback.enable_if_applicable(host, e):
                ctx = get_unverified_ssl_context()
                continue
            # A public cert failure must not be retried as a generic connection
            # error, and must not switch this URL to unverified TLS.
            if _is_certificate_verify_error(e):
                log.exception("Request failed: %s", format_error_message(e))
                raise NetworkError(format_error_message(e), details={"url": logged_target}) from e
            retryable = isinstance(e, (TimeoutError, ConnectionError, OSError)) or (isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError))
            if retryable and sends_left > 1:
                sends_left -= 1
                attempt += 1
                delay = backoff_delay_sec(attempt=attempt)
                wait_abortable(delay, None)
                continue
            log.exception("Request failed: %s", format_error_message(e))
            raise NetworkError(format_error_message(e), details={"url": logged_target}) from e
