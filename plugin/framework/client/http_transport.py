# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persistent ``http.client`` transport for chat-completion requests.

Concurrency: each ``LlmHttpTransport`` owns one keep-alive HTTP connection
(the stdlib ``http.client`` object). That object is not safe for two threads
to ``request`` / ``getresponse`` at once, so callers create a **new**
``LlmClient`` (and thus a new transport) per job — sidebar chat, grammar, a
Calc ``=PROMPT()`` cell, and smolagents each have their own. When the user
hits Stop, another thread calls ``close()`` and shuts the socket while the
worker may still be blocked in ``getresponse``. That abort is intentional.
Do not put a lock around ``send()`` to “make HTTP thread-safe”: Stop would
then wait for the full network timeout. Details:
docs/framework/threading.md.
"""

import http.client
import logging
import socket
import urllib.parse
from typing import Any, Callable, Literal

from plugin.framework.errors import NetworkError
from plugin.framework.url_utils import get_url_hostname

from plugin.framework.errors import format_error_message
from .request_controls import (
    LocalHttpsCertificateFallback,
    RequestPacer,
    backoff_delay_sec,
    emit_retry_status,
    ensure_free_model_pacing,
    mark_host_sent,
    remember_host_gap,
    request_model_from_body,
    wait_abortable,
    wait_host_gap,
)
from .ssl_helpers import get_unverified_ssl_context, get_verified_ssl_context

log = logging.getLogger(__name__)

CONNECTION_ERRORS = (http.client.HTTPException, socket.error, OSError)
RetryAction = Literal["retry", "stop"]


class LlmHttpTransport:
    """Own persistent chat HTTP connections plus pacing, jittered retries, per-host cooldown, and local TLS fallback."""

    _endpoint_getter: Callable[[], str]
    _timeout_getter: Callable[[], int | float]
    _pacer: RequestPacer
    _cert_fallback: LocalHttpsCertificateFallback

    def __init__(
        self,
        endpoint_getter: Callable[[], str],
        timeout_getter: Callable[[], int | float],
        *,
        pacer: RequestPacer | None = None,
        cert_fallback: LocalHttpsCertificateFallback | None = None,
    ) -> None:
        self._endpoint_getter = endpoint_getter
        self._timeout_getter = timeout_getter
        self._pacer = pacer or RequestPacer()
        self._cert_fallback = cert_fallback or LocalHttpsCertificateFallback()
        self._persistent_conn: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        self._conn_key: tuple[str, str, int, str] | None = None

    @property
    def persistent_conn(self) -> http.client.HTTPConnection | http.client.HTTPSConnection | None:
        return self._persistent_conn

    @property
    def conn_key(self) -> tuple[str, str, int, str] | None:
        return self._conn_key

    def _endpoint_parts(self) -> tuple[str, str, int]:
        endpoint = self._endpoint_getter()
        parsed = urllib.parse.urlparse(endpoint)
        scheme = parsed.scheme.lower()
        host = get_url_hostname(endpoint)
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, host, port

    def current_host(self) -> str:
        return self._endpoint_parts()[1]

    def get_connection(self) -> http.client.HTTPConnection | http.client.HTTPSConnection:
        """Get or create a persistent ``http.client`` connection."""
        scheme, host, port = self._endpoint_parts()
        ssl_mode = self._cert_fallback.ssl_mode_for(scheme, host)
        new_key = (scheme, host, port, ssl_mode)

        if self._persistent_conn:
            if self._conn_key != new_key:
                log.debug("Closing old connection to %s, opening new to %s" % (self._conn_key, new_key))
                self.close()
            else:
                return self._persistent_conn

        log.debug("Opening new connection to %s://%s:%s" % (scheme, host, port))
        self._conn_key = new_key
        timeout = self._timeout_getter()

        if scheme == "https":
            ssl_context = get_verified_ssl_context() if ssl_mode == "verified" else get_unverified_ssl_context()
            self._persistent_conn = http.client.HTTPSConnection(host, port, context=ssl_context, timeout=timeout)
        else:
            self._persistent_conn = http.client.HTTPConnection(host, port, timeout=timeout)

        return self._persistent_conn

    def close(self) -> None:
        if not self._persistent_conn:
            return
        try:
            log.debug("Closing persistent connection to %s" % (self._conn_key,))
            try:
                sock = getattr(self._persistent_conn, "sock", None)
                if sock:
                    sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            self._persistent_conn.close()
        except Exception:
            pass
        self._persistent_conn = None
        self._conn_key = None

    def send(
        self,
        method: str,
        path: str,
        body: Any,
        headers: dict[str, str],
        *,
        connection_getter: Callable[[], http.client.HTTPConnection | http.client.HTTPSConnection] | None = None,
        stop_checker: Callable[[], bool] | None = None,
        status_callback: Callable[[str], None] | None = None,
    ) -> http.client.HTTPResponse:
        """Send one request on the persistent connection and return its response."""
        host = self.current_host()
        key = ensure_free_model_pacing(host, request_model_from_body(body))
        if not wait_host_gap(key, stop_checker, status_callback):
            raise NetworkError("LLM request aborted by Stop", code="STOPPED")
        conn = connection_getter() if connection_getter is not None else self.get_connection()
        self._pacer.wait_before_send()
        if not any(k.lower() == "user-agent" for k in headers):
            headers = dict(headers)
            from plugin.framework.constants import USER_AGENT
            headers["User-Agent"] = USER_AGENT
        conn.request(method, path, body=body, headers=headers)
        self._pacer.mark_sent()
        mark_host_sent(key)
        return conn.getresponse()

    def enable_local_ssl_fallback(self, err: Exception) -> bool:
        enabled = self._cert_fallback.enable_if_applicable(self.current_host(), err)
        if enabled:
            self.close()
        return enabled

    def handle_connection_error(
        self,
        err: Exception,
        *,
        path: str,
        retries_left: int,
        retry_log_message: str,
        stop_checker: Callable[[], bool] | None = None,
        status_callback: Callable[[str], None] | None = None,
        attempt: int = 1,
    ) -> RetryAction:
        """Close failed connections and decide whether a request should retry."""
        log.error("Connection error, closing: %s" % err)
        self.close()
        if stop_checker and stop_checker():
            log.error("Connection error during stop; exiting streaming loop")
            return "stop"
        if retries_left > 0 and self.enable_local_ssl_fallback(err):
            # Immediate reopen: TLS mode just changed; do not add backoff.
            return "retry"

        err_msg = format_error_message(err)
        if retries_left > 0:
            log.warning(retry_log_message)
            delay = backoff_delay_sec(attempt=attempt)
            remember_host_gap(self.current_host(), delay)
            emit_retry_status(status_callback, delay)
            if not wait_abortable(delay, stop_checker):
                return "stop"
            return "retry"
        log.error("Connection retry failed: %s" % err_msg)
        raise NetworkError(err_msg, code="CONNECTION_ERROR", details={"url": path}) from err
