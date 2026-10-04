# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""One-shot HTTP for catalog, speech, image URL download, and update checks.

The stop / timeout / retry / redaction / strict-JSON behavior is
``LlmHttpTransport``. This module only adapts a URL into that transport so
LibrePy can import it without ``llm_client``.
"""

from __future__ import annotations

from typing import Any
from urllib.request import Request

from plugin.framework.constants import USER_AGENT

from .http_transport import HttpResult, LlmHttpTransport, origin_and_path


def sync_request(
    url: str | Request,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    parse_json: bool = True,
    method: str | None = None,
    *,
    timeout: float,
    stop_checker: Any = None,
    status_callback: Any = None,
    include_meta: bool = False,
) -> Any:
    """Blocking HTTP GET or POST on the shared transport.

    ``timeout`` is the read budget (Settings ``request_timeout`` for LLM and
    image work, or an explicit probe value). Connect uses
    ``LLM_CONNECT_TIMEOUT_SEC`` inside the transport so a dead host does not
    wait the full read stall. ``parse_json`` is strict ``json.loads`` — a
    truncated body is ``BAD_RESPONSE``, not a repaired object.

    Returns decoded JSON, or raw bytes when ``parse_json`` is false. With
    ``include_meta``, returns :class:`HttpResult` (status, body, content type).

    Redirects (301/302/303/307/308) are followed on the shared transport,
    at most five hops. 301/302/303 switch a non-GET to GET and drop the
    body; 307/308 keep the method and body. A non-numeric or out-of-range
    port, or an unmatched bracket (``http://[::1``, ``http://[]/v1``), is
    ``NetworkError``, including when it is in the URL before the request
    is sent.
    """
    if headers is None:
        headers = {}
    else:
        headers = dict(headers)

    full_url: str
    if isinstance(url, Request):
        full_url = url.full_url
        if data is None and isinstance(url.data, (bytes, bytearray)):
            data = bytes(url.data)
        if method is None:
            method = url.get_method()
        for key, value in url.header_items():
            if not any(existing.lower() == key.lower() for existing in headers):
                headers[key] = value
    else:
        full_url = url

    if method is None:
        method = "POST" if data is not None else "GET"

    if not any(key.lower() == "user-agent" for key in headers):
        headers["User-Agent"] = USER_AGENT

    origin, path = origin_and_path(full_url)
    transport = LlmHttpTransport(lambda: origin, lambda: timeout)
    try:
        result = transport.exchange(
            method,
            path,
            data,
            headers,
            stop_checker=stop_checker,
            status_callback=status_callback,
            parse_json=parse_json,
        )
    finally:
        transport.close()
    if include_meta:
        return result
    if parse_json:
        return result.parsed
    return result.body


__all__ = ["HttpResult", "sync_request"]
