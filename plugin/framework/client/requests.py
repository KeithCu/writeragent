# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""One-shot HTTP for catalog, speech, image URL download, update checks, and web search.

The stop / timeout / retry / redaction / strict-JSON behavior is
``LlmHttpTransport``. This module adapts a URL into a shared persistent
transport per ``(origin, connect_timeout)`` so repeated requests to the same
host (such as web search bursts) reuse one keep-alive connection, while
keeping LibrePy free of ``llm_client``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, final

from plugin.framework.send_cancellation import (
    SendCancellation,
    acquire_unless_cancelled,
    is_cancelled,
)

from .http_security import origin_and_path
from .http_transport import LlmHttpTransport
from .request_controls import stopped_error

log = logging.getLogger(__name__)


@final
class _SharedTransport:
    """Serialized persistent transport for one ``(origin, connect_timeout)``."""

    lock: threading.Lock
    timeout: float
    transport: LlmHttpTransport

    def __init__(self, origin: str, timeout: float, connect_timeout: float | None) -> None:
        self.lock = threading.Lock()
        self.timeout = timeout
        self.transport = LlmHttpTransport(
            lambda: origin,
            lambda: self.timeout,
            connect_timeout=connect_timeout,
        )


_SYNC_TRANSPORTS_LOCK = threading.Lock()
_SYNC_TRANSPORTS: dict[tuple[str, float | None], _SharedTransport] = {}


def _get_shared_transport(
    origin: str,
    timeout: float,
    connect_timeout: float | None,
) -> _SharedTransport:
    key = (origin, None if connect_timeout is None else float(connect_timeout))
    with _SYNC_TRANSPORTS_LOCK:
        entry = _SYNC_TRANSPORTS.get(key)
        if entry is None:
            # A single http.client connection cannot carry two requests at once,
            # so concurrent callers (such as deep research threads) serialize on
            # entry.lock and share one long-lived connection per origin.
            entry = _SharedTransport(origin, timeout, connect_timeout)
            _SYNC_TRANSPORTS[key] = entry
        return entry


def close_sync_transports() -> None:
    """Close and clear all cached persistent transports for ``sync_request``."""
    with _SYNC_TRANSPORTS_LOCK:
        entries = list(_SYNC_TRANSPORTS.values())
        _SYNC_TRANSPORTS.clear()
    for entry in entries:
        try:
            entry.transport.close()
        except Exception:
            log.debug("Closing sync transport failed", exc_info=True)


def _register_shutdown_hook() -> None:
    try:
        from plugin.framework.quit_hooks import register_quit_hook
    except ImportError:
        # LibrePy bundles requests.py without quit_hooks.py.
        return
    register_quit_hook(close_sync_transports)


_register_shutdown_hook()


def sync_request(
    url: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    parse_json: bool = True,
    *,
    timeout: float,
    connect_timeout: float | None = None,
    cancel: SendCancellation | None = None,
    status_callback: Any = None,
    include_meta: bool = False,
    max_bytes: int | None = None,
    ok_statuses: frozenset[int] = frozenset({200}),
    fresh_connection: bool = False,
) -> Any:
    """Blocking HTTP GET or POST on the shared transport.

    ``timeout`` is the read budget (Settings ``request_timeout`` for LLM and
    image work, or an explicit probe value). Connect uses
    ``LLM_CONNECT_TIMEOUT_SEC`` inside the transport so a dead host does not
    wait the full read stall; ``connect_timeout`` replaces
    ``LLM_CONNECT_TIMEOUT_SEC`` for callers whose host should answer quickly.
    ``parse_json`` is strict ``json.loads`` — a truncated body is
    ``BAD_RESPONSE``, not a repaired object.

    Returns decoded JSON, or raw bytes when ``parse_json`` is false. With
    ``include_meta``, returns ``http_transport.HttpResult`` (status, body, content type).
    Statuses in ``ok_statuses`` are returned without raising ``NetworkError``;
    callers passing extra statuses need ``include_meta=True`` to tell them apart.
    With ``fresh_connection=True``, any pooled keep-alive socket for that
    origin is closed under the transport lock before sending.
    HTTP errors are labelled with the origin rather than "AI Provider".

    Redirects follow ``LlmHttpTransport.exchange``.
    A non-numeric or out-of-range port, or an unmatched bracket
    (``http://[::1``, ``http://[]/v1``), is ``NetworkError``, including
    when it is in the URL before the request is sent.
    """
    if is_cancelled(cancel):
        raise stopped_error()
    method = "POST" if data is not None else "GET"

    origin, path = origin_and_path(url)
    entry = _get_shared_transport(origin, timeout, connect_timeout)
    if not acquire_unless_cancelled(entry.lock, cancel):
        raise stopped_error()
    try:
        if is_cancelled(cancel):
            raise stopped_error()
        entry.timeout = timeout
        if fresh_connection:
            # Drop the pooled keep-alive socket so this request opens a new connection.
            entry.transport.close()
        if cancel is not None:
            cancel.register_on_cancel(entry.transport.close)
        try:
            result = entry.transport.exchange(
                method,
                path,
                data,
                headers,
                cancel=cancel,
                status_callback=status_callback,
                parse_json=parse_json,
                max_bytes=max_bytes,
                ok_statuses=ok_statuses,
                error_source=origin,
            )
        finally:
            if cancel is not None:
                cancel.unregister_on_cancel(entry.transport.close)
    finally:
        entry.lock.release()
    if include_meta:
        return result
    if parse_json:
        return result.parsed
    return result.body
