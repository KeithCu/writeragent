# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persistent ``http.client`` transport for chat-completion requests."""

import http.client
import logging
import socket
import urllib.parse
from collections.abc import Callable
from typing import Any, Literal

from plugin.framework.errors import NetworkError
from plugin.framework.url_utils import get_url_hostname

from .errors import format_error_message
from .request_controls import LocalHttpsCertificateFallback, RequestPacer
from .ssl_helpers import get_unverified_ssl_context, get_verified_ssl_context

log = logging.getLogger(__name__)

CONNECTION_ERRORS = (http.client.HTTPException, socket.error, OSError)
RetryAction = Literal["retry", "stop"]


class LlmHttpTransport:
    """Own persistent chat HTTP connections plus pacing, retry, and local TLS fallback."""

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

    def send(self, method: str, path: str, body: Any, headers: dict[str, str], *, connection_getter: Callable[[], http.client.HTTPConnection | http.client.HTTPSConnection] | None = None) -> http.client.HTTPResponse:
        """Send one request on the persistent connection and return its response."""
        conn = connection_getter() if connection_getter is not None else self.get_connection()
        self._pacer.wait_before_send()
        conn.request(method, path, body=body, headers=headers)
        self._pacer.mark_sent()
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
        retry_available: bool,
        retry_log_message: str,
        stop_checker: Callable[[], bool] | None = None,
    ) -> RetryAction:
        """Close failed connections and decide whether a request should retry."""
        log.error("Connection error, closing: %s" % err)
        self.close()
        if stop_checker and stop_checker():
            log.error("Connection error during stop; exiting streaming loop")
            return "stop"
        if retry_available and self.enable_local_ssl_fallback(err):
            return "retry"

        err_msg = format_error_message(err)
        if retry_available:
            log.warning(retry_log_message)
            return "retry"
        log.error("Connection retry failed: %s" % err_msg)
        raise NetworkError(err_msg, code="CONNECTION_ERROR", context={"url": path}) from err
