# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import logging
import urllib.error
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from plugin.framework.constants import USER_AGENT
from plugin.framework.errors import NetworkError
from .ssl_helpers import get_verified_ssl_context, get_unverified_ssl_context, _is_certificate_verify_error
from .provider_detection import is_local_host
from plugin.framework.errors import format_error_message
from .errors import _format_http_error_response

log = logging.getLogger(__name__)


def sync_request(url, data=None, headers=None, parse_json=True, method=None, *, timeout):
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

    # Debug: log which headers we are actually sending (keys only)
    try:
        header_keys = list(req.headers.keys()) if hasattr(req, "headers") else []
        if not header_keys and hasattr(req, "get_full_url"):
            # If it's a urllib Request object, headers might be in .headers
            pass
        log.debug(f"Request to {getattr(req, 'full_url', url)} with header keys: {header_keys}")
    except Exception:
        pass

    full_url = getattr(req, "full_url", url)
    parsed = urlparse(str(full_url))
    host = parsed.hostname or ""
    is_local_https = parsed.scheme.lower() == "https" and is_local_host(host)

    def _read_with_context(context):
        log.debug(f"About to open URL: {getattr(req, 'full_url', url)}")
        with urlopen(req, timeout=timeout, context=context) as resp:
            log.debug(f"URL opened, status={resp.getcode()}. Heading to read...")
            raw = resp.read()
            log.debug(f"Read {len(raw)} bytes")
            if parse_json:
                return json.loads(raw.decode("utf-8"))
            return raw

    # Always verify first. Local self-signed hosts retry unverified below.
    ctx = get_verified_ssl_context()
    try:
        return _read_with_context(ctx)
    except urllib.error.HTTPError as e:
        status = e.code
        reason = e.reason
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""

        msg = _format_http_error_response(status, reason, err_body)
        log.exception("HTTP Error: %s", msg)
        raise NetworkError(msg, code="HTTP_ERROR", details={"url": url, "status": status}) from e
    except NetworkError:
        raise
    except Exception as e:
        if is_local_https and _is_certificate_verify_error(e):
            log.exception("Local HTTPS certificate verification failed for %s; retrying unverified.", host)
            try:
                return _read_with_context(get_unverified_ssl_context())
            except urllib.error.HTTPError as retry_http_e:
                status = retry_http_e.code
                reason = retry_http_e.reason
                try:
                    err_body = retry_http_e.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""
                msg = _format_http_error_response(status, reason, err_body)
                log.exception("HTTP Error: %s", msg)
                raise NetworkError(msg, code="HTTP_ERROR", details={"url": url, "status": status}) from retry_http_e
            except Exception as retry_e:
                log.exception("Request retry failed: %s", format_error_message(retry_e))
                raise NetworkError(format_error_message(retry_e), details={"url": url}) from retry_e
        log.exception("Request failed: %s", format_error_message(e))
        raise NetworkError(format_error_message(e), details={"url": url}) from e
