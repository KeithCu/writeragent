# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""One HTTP transport for stream, sync chat, image, speech, and catalog.

Stop, connect-vs-read timeout, retry, and strict JSON live here. Secret
redaction, URL checks, and redirect policy live in ``http_security``.
Callers do not keep a second copy.

Concurrency: each ``LlmHttpTransport`` owns one keep-alive HTTP connection
(the stdlib ``http.client`` object). That object is not safe for two threads
to ``request`` / ``getresponse`` at once, so callers create a **new**
``LlmClient`` (and thus a new transport) per job — sidebar chat, grammar, a
Calc ``=PROMPT()`` cell, and smolagents each have their own. Catalog, speech,
and image-URL downloads use a short-lived transport around ``exchange``.
When the user hits Stop, another thread calls ``close()`` and shuts the
socket while the worker may still be blocked in ``getresponse``. That abort
is intentional. Do not put a lock around ``send()`` to make HTTP thread-safe:
Stop would then wait for the full network timeout. DNS is the exception:
``connect()`` blocks before ``sock`` exists, so Stop runs connect on a
dedicated worker and abandons it. Details: docs/framework/threading.md.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import logging
import socket
import threading
import time
from typing import Any, Callable, Literal

from plugin.framework.send_cancellation import SendCancellation, is_cancelled
from plugin.framework.worker_pool import run_in_background

from plugin.framework.constants import LLM_CONNECT_TIMEOUT_SEC, USER_AGENT
from plugin.framework.errors import NetworkError, format_error_message

from . import http_security
from .errors import RETRYABLE_HTTP_STATUS, format_http_error_response
from .provider_detection import is_openrouter_endpoint
from .request_controls import (
    RETRY_MAX_DELAY_SEC,
    RequestPacer,
    RetryBudget,
    StreamOverload,
    backoff_delay_sec,
    clear_host_gap,
    emit_retry_status,
    enable_local_unverified,
    mark_host_sent,
    over_cap_retry_message,
    pacing_key,
    parse_retry_after,
    parse_retry_after_ms,
    remember_host_gap,
    request_model_from_body,
    retry_after_exceeds_cap,
    ssl_mode_for,
    stopped_error,
    wait_abortable,
    wait_host_gap,
)
from .ssl_helpers import get_unverified_ssl_context, get_verified_ssl_context

log = logging.getLogger(__name__)

_MAX_REDIRECTS = 5

HttpObserve = Callable[[Any, str, str, Any], str]
HttpSender = Callable[..., http.client.HTTPResponse]


def parse_strict_json(raw: Any) -> Any:
    """Parse provider bytes with ``json.loads`` only.

    ``safe_json_loads`` repairs truncated model text, so a cut-off envelope
    such as ``{"choices":[{"message":{"content":"hel`` or a cut-off catalog
    ``{"data":[{"id":"gpt`` became a dict and looked finished. Provider
    envelopes and catalogs are not model text. A decode failure is
    ``BAD_RESPONSE``, not a reply.
    """
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise NetworkError("LLM response was not JSON", code="BAD_RESPONSE") from exc


@dataclass(eq=False)
class HttpResult:
    """Buffered HTTP result. ``parsed`` is set only for strict JSON."""

    status: int
    body: bytes
    content_type: str
    parsed: Any = None


class LlmHttpTransport:
    """Own persistent chat HTTP connections plus pacing, jittered retries, per-host cooldown, and local TLS fallback."""

    _endpoint_getter: Callable[[], str]
    _timeout_getter: Callable[[], int | float]
    _pacer: RequestPacer
    _openrouter_getter: Callable[[], bool] | None

    def __init__(
        self,
        endpoint_getter: Callable[[], str],
        timeout_getter: Callable[[], int | float],
        *,
        openrouter_getter: Callable[[], bool] | None = None,
    ) -> None:
        self._endpoint_getter = endpoint_getter
        self._timeout_getter = timeout_getter
        self._pacer = RequestPacer()
        self._openrouter_getter = openrouter_getter
        self._persistent_conn: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        self._conn_key: tuple[str, str, int, str] | None = None
        # Set while exchange follows a redirect onto a different origin.
        # Cleared before exchange returns so the next chat send uses the
        # configured endpoint again.
        self._redirect_origin: str | None = None

    def _current_endpoint(self) -> str:
        return self._redirect_origin if self._redirect_origin else self._endpoint_getter()

    def _endpoint_parts(self) -> tuple[str, str, int]:
        endpoint = self._current_endpoint()
        parsed = http_security._parse_url(endpoint)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        port = http_security._explicit_port(parsed) or (443 if scheme == "https" else 80)
        return scheme, host, port

    def pacing_key_for_model(self, model: str | None = None) -> str:
        _, host, port = self._endpoint_parts()
        endpoint = self._current_endpoint()
        explicit = bool(self._openrouter_getter and self._openrouter_getter())
        return pacing_key(host, model, port=port, openrouter=is_openrouter_endpoint(endpoint, explicit_is_openrouter=explicit))

    def _absolute_url(self, path: str) -> str:
        scheme, host, port = self._endpoint_parts()
        default = 443 if scheme == "https" else 80
        shown = http_security._bracket_ipv6_host(host)
        origin = f"{scheme}://{shown}" if port == default else f"{scheme}://{shown}:{port}"
        if not path.startswith("/"):
            path = "/" + path
        return origin + path

    def get_connection(self) -> http.client.HTTPConnection | http.client.HTTPSConnection:
        """Get or create a persistent ``http.client`` connection."""
        scheme, host, port = self._endpoint_parts()
        ssl_mode = ssl_mode_for(scheme, host)
        new_key = (scheme, host, port, ssl_mode)

        if self._persistent_conn:
            if self._conn_key != new_key:
                log.debug("Closing old connection to %s, opening new to %s" % (self._conn_key, new_key))
                self.close()
            else:
                return self._persistent_conn

        log.debug("Opening new connection to %s://%s:%s" % (scheme, host, port))
        self._conn_key = new_key
        # The constructor timeout is the connect budget, not request_timeout
        # (default 120). A dead host must not block DNS/TCP for the full stream
        # stall. send() raises the socket to the Settings read timeout after
        # connect returns.
        if scheme == "https":
            ssl_context = get_verified_ssl_context() if ssl_mode == "verified" else get_unverified_ssl_context()
            self._persistent_conn = http.client.HTTPSConnection(host, port, context=ssl_context, timeout=LLM_CONNECT_TIMEOUT_SEC)
        else:
            self._persistent_conn = http.client.HTTPConnection(host, port, timeout=LLM_CONNECT_TIMEOUT_SEC)

        # http.client defaults auto_open=1, so Stop's conn.close() between the
        # last stop check and conn.request() would silently reconnect and send.
        # auto_open = 0 makes a closed connection raise NotConnected instead.
        self._persistent_conn.auto_open = 0

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
                log.debug("persistent socket shutdown failed", exc_info=True)
            if self._persistent_conn is not None:
                self._persistent_conn.close()
        except Exception:
            log.debug("persistent connection close failed", exc_info=True)
        self._persistent_conn = None
        self._conn_key = None

    def _drop_stopped_connection(self, conn: http.client.HTTPConnection | http.client.HTTPSConnection) -> None:
        """Close a socket Stop won after connect and before the body is sent."""
        if conn is self._persistent_conn:
            self.close()
            return
        try:
            conn.close()
        except Exception:
            log.debug("stopped connection close failed", exc_info=True)

    def _raise_if_stopped(
        self,
        cancel: SendCancellation | None,
        conn: http.client.HTTPConnection | http.client.HTTPSConnection | None = None,
    ) -> None:
        if is_cancelled(cancel):
            if conn is not None:
                self._drop_stopped_connection(conn)
            else:
                self.close()
            raise stopped_error()

    def send(
        self,
        method: str,
        path: str,
        body: Any,
        headers: dict[str, str],
        *,
        cancel: SendCancellation | None = None,
        status_callback: Callable[[str], None] | None = None,
        _allow_stale_resend: bool = True,
    ) -> http.client.HTTPResponse:
        """Send one request on the persistent connection and return its response."""
        key = self.pacing_key_for_model(request_model_from_body(body))
        if not wait_host_gap(key, cancel, status_callback):
            raise stopped_error()
        conn = self.get_connection()
        # Refresh read timeout from Settings on each request for keep-alive connections.
        read_timeout = self._timeout_getter()
        sock = getattr(conn, "sock", None)
        # Captured before connect(). A socket http.client already holds is a
        # reused keep-alive connection. A fresh connect leaves this false even
        # after sock is assigned.
        reused_socket = isinstance(sock, socket.socket)
        if sock is None:
            # A reused keep-alive socket is already set; request() must not
            # be asked to connect again or it replaces that socket.
            # Stop already latched: do not start DNS.
            self._raise_if_stopped(cancel, conn)
            self._connect_abortable(conn, cancel)
            sock = getattr(conn, "sock", None)
        conn.timeout = read_timeout
        if sock is not None:
            sock.settimeout(read_timeout)
        self._raise_if_stopped(cancel, conn)
        self._pacer.wait_before_send()
        self._raise_if_stopped(cancel, conn)
        if not any(k.lower() == "user-agent" for k in headers):
            headers = dict(headers)
            headers["User-Agent"] = USER_AGENT
        allow_stale_resend = _allow_stale_resend
        try:
            conn.request(method, path, body=body, headers=headers)
            self._pacer.mark_sent()
            mark_host_sent(key)
            return conn.getresponse()
        except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
            # A keep-alive socket the peer already closed raises
            # RemoteDisconnected, BrokenPipeError, or ConnectionResetError
            # inside request() or getresponse(), before any response bytes.
            # That would spend a retry, call remember_host_gap, and show
            # "Provider busy". Only when this socket was already open: a
            # failure while connecting a new socket still uses the budget.
            # Close and send this same request once. Do not reconnect if Stop
            # was requested.
            if is_cancelled(cancel) or not reused_socket or not allow_stale_resend:
                raise
            log.debug("Keep-alive socket failed before a response; reconnecting once")
            self._drop_stopped_connection(conn)
            allow_stale_resend = False
            return self.send(method, path, body, headers, cancel=cancel, status_callback=status_callback, _allow_stale_resend=False)

    def _connect_abortable(self, conn: http.client.HTTPConnection | http.client.HTTPSConnection, cancel: SendCancellation | None) -> None:
        """Connect without waiting out the read budget, and without ignoring Stop.

        ``http.client.connect`` blocks inside ``getaddrinfo`` before it assigns
        ``sock``. The socket timeout does not bound ``getaddrinfo``, so
        ``close()`` sees ``sock is None`` and Stop during DNS is a no-op until
        the resolver finishes — often the whole 120s read budget. Run connect
        on a dedicated worker (the caller joins it; a pool slot would
        deadlock). Poll Stop and the connect deadline, then close whatever
        socket has appeared. Do not wait for DNS to finish.
        """
        outcome: dict[str, Any] = {}
        cancelled = threading.Event()
        conn.timeout = LLM_CONNECT_TIMEOUT_SEC

        def _do_connect() -> None:
            try:
                conn.connect()
            except Exception as exc:
                outcome["error"] = exc
            finally:
                outcome["done"] = True
                if cancelled.is_set():
                    self._drop_stopped_connection(conn)

        # dedicated: the caller joins this worker. A pooled job joined from
        # a pool thread deadlocks the two-worker background pool.
        handle = run_in_background(_do_connect, name="http-connect", dedicated=True)
        deadline = time.monotonic() + float(LLM_CONNECT_TIMEOUT_SEC)
        while not outcome.get("done"):
            if is_cancelled(cancel):
                cancelled.set()
                self._drop_stopped_connection(conn)
                raise stopped_error()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cancelled.set()
                self._drop_stopped_connection(conn)
                # TimeoutError is an OSError, so the shared retry budget can
                # try again. It must not be the Settings read/stall budget.
                raise TimeoutError("timed out")
            handle.join(min(0.05, remaining))
        error = outcome.get("error")
        if isinstance(error, Exception):
            raise error

    def handle_http_status(
        self,
        response: Any,
        *,
        request_body: Any,
        path: str,
        retries_left: int,
        emitted_any: bool,
        cancel: SendCancellation | None = None,
        status_callback: Callable[[str], None] | None = None,
        attempt: int = 1,
        secrets: list[str] | None = None,
        observe_http_error: HttpObserve | None = None,
    ) -> None:
        """On non-200: retry 429/503/529 while attempts remain; else raise.

        Reads the error body once. ``observe_http_error`` is the chat 500
        diagnostic hook; catalog and speech omit it and still get redaction.
        A normal return means the caller should retry.
        """
        if is_cancelled(cancel):
            self.close()
            raise stopped_error()
        raw_body = response.read()
        if isinstance(raw_body, (bytes, bytearray)):
            err_body = raw_body.decode("utf-8", errors="replace")
        elif isinstance(raw_body, str):
            err_body = raw_body
        else:
            err_body = str(raw_body or "")
        if not isinstance(err_body, str):
            err_body = str(err_body)
        wire_secrets = http_security.collect_secrets(secrets, http_security.secrets_from_target(path))
        if observe_http_error is not None:
            message = observe_http_error(response, err_body, path, request_body)
        else:
            try:
                st = int(response.status)
            except (TypeError, ValueError):
                st = 500
            message = format_http_error_response(st, str(getattr(response, "reason", "") or ""), err_body)
        message = http_security.redact_secrets(message, wire_secrets)
        if not isinstance(message, str):
            message = str(message)
        safe_target = http_security.public_target(path)
        if observe_http_error is None:
            log.error("HTTP %s for %s: %s", response.status, safe_target, message)
        self.close()
        if is_cancelled(cancel):
            raise stopped_error()
        try:
            status = int(response.status)
        except (TypeError, ValueError):
            status = 500
        model = request_model_from_body(request_body)
        retry_after = parse_retry_after(response.getheader("Retry-After"))
        if retry_after is None:
            retry_after = parse_retry_after_ms(response.getheader("retry-after-ms"))
        details: dict[str, Any] = {"url": safe_target, "status": status}
        if retry_after_exceeds_cap(retry_after):
            assert retry_after is not None
            remember_host_gap(self.pacing_key_for_model(model), RETRY_MAX_DELAY_SEC)
            message = over_cap_retry_message(message, retry_after)
            raise NetworkError(message, code="HTTP_ERROR", details=details)
        if status in RETRYABLE_HTTP_STATUS and retries_left > 0 and not emitted_any:
            delay = backoff_delay_sec(attempt=attempt, retry_after_sec=retry_after)
            remember_host_gap(self.pacing_key_for_model(model), delay)
            log.warning("Retrying HTTP %s after %.3fs (Retry-After=%s attempt=%s left=%s)", status, delay, retry_after, attempt, retries_left)
            emit_retry_status(status_callback, delay)
            if not wait_abortable(delay, cancel):
                clear_host_gap(self.pacing_key_for_model(model))
                raise stopped_error()
            return
        raise NetworkError(message, code="HTTP_ERROR", details=details)

    def exchange(
        self,
        method: str,
        path: str,
        body: Any,
        headers: dict[str, str] | None = None,
        *,
        cancel: SendCancellation | None = None,
        status_callback: Callable[[str], None] | None = None,
        parse_json: bool = False,
        sender: HttpSender | None = None,
        secrets: list[str] | None = None,
        on_retry: Callable[[], None] | None = None,
        after_read: Callable[[Any], None] | None = None,
        observe_http_error: HttpObserve | None = None,
        retry_log_message: str = "Retrying HTTP request on a fresh connection",
        max_bytes: int | None = None,
        on_bad_request: Callable[[bytes], bytes | None] | None = None,
    ) -> HttpResult:
        """Buffered request with the shared stop, timeout, retry, and redaction.

        ``parse_json`` uses :func:`parse_strict_json`. Truncated JSON raises
        ``BAD_RESPONSE`` instead of becoming a repaired object.

        301/302/303/307/308 are followed up to ``_MAX_REDIRECTS`` hops.
        301/302/303 switch a non-GET to GET and drop the body. 307/308 keep
        the method and body. A redirect onto another host drops secret
        headers and opens a new connection. That host is not kept as the
        persistent chat endpoint.

        This method owns redirect handling and response reads;
        :meth:`RetryBudget.run_with_retries` only sequences attempts.
        Streaming uses :meth:`stream` on the same transport with the same split.
        """
        budget = RetryBudget(
            self,
            cancel=cancel,
            status_callback=status_callback,
            on_bad_request=on_bad_request,
        )
        wire_headers = dict(headers or {})
        self._redirect_origin = None
        redirects_followed = 0
        current_model: str | None = None
        wire_secrets: list[str] | None = None

        def on_connection_error(err: Exception) -> None:
            budget.connection_outcome(
                err,
                fatal=budget.body_in_hand,
                attempt_charged=budget.attempt_charged,
                secrets=wire_secrets,
                path=path,
                model=current_model,
                retry_log_message=retry_log_message,
                close_on_fatal=False,
            )

        def dispatch(current_body: Any) -> tuple[Literal["again", "done"], Any]:
            nonlocal method, path, wire_headers, redirects_followed, current_model, wire_secrets
            current_model = request_model_from_body(current_body)
            wire_secrets = http_security.collect_secrets(
                secrets,
                http_security.secrets_from_headers(wire_headers),
                http_security.secrets_from_target(path),
            )
            budget.body_in_hand = False
            budget.attempt_charged = False

            self._raise_if_stopped(cancel)
            # A custom sender is bound to the caller's connection. After a
            # cross-origin redirect, send() on this transport uses the new host.
            if sender is not None and self._redirect_origin is None:
                response = sender(method, path, current_body, wire_headers, cancel=cancel, status_callback=status_callback)
            else:
                response = self.send(
                    method,
                    path,
                    current_body,
                    wire_headers,
                    cancel=cancel,
                    status_callback=status_callback,
                )
            status = int(getattr(response, "status", 0) or 0)
            if status in http_security.REDIRECT_STATUSES and redirects_followed < _MAX_REDIRECTS:
                # A CDN 301/302/307 is not a failure. Image URLs, TTS audio,
                # catalogs, and update checks follow Location. One hop
                # rewrites method/body, then loops. The retry budget is
                # for 429/503, not for a redirect the server asked for.
                current_url = self._absolute_url(path)
                hop = http_security.apply_redirect(
                    method,
                    current_body,
                    wire_headers,
                    current_url,
                    status,
                    (response.getheader("Location") or ""),
                )
                if hop is not None:
                    response.read()
                    new_method, new_body, new_headers, new_origin, new_path, cross_origin = hop
                    if cross_origin:
                        self._redirect_origin = new_origin
                        self.close()
                    log.debug("Following HTTP %s to %s", status, http_security.public_target(new_origin + new_path))
                    method = new_method
                    wire_headers = new_headers
                    path = new_path
                    redirects_followed += 1
                    return "again", new_body

            if status != 200:
                attempt = budget.charge_send(status=True)
                self.handle_http_status(
                    response,
                    request_body=current_body,
                    path=path,
                    retries_left=budget.sends_left,
                    emitted_any=False,
                    cancel=cancel,
                    status_callback=status_callback,
                    attempt=attempt,
                    secrets=wire_secrets,
                    observe_http_error=observe_http_error,
                )
                if on_retry is not None:
                    on_retry()
                return "again", current_body

            budget.clear_pacing_gap_when_unspent(current_model)
            if max_bytes is not None:
                raw = self._read_bounded_response(response, max_bytes=max_bytes, cancel=cancel)
            else:
                raw = response.read()
            budget.body_in_hand = True
            if after_read is not None:
                after_read(response)
            parsed = parse_strict_json(raw) if parse_json else None
            return "done", HttpResult(
                status=status,
                body=raw,
                content_type=response.getheader("Content-Type") or "",
                parsed=parsed,
            )

        try:
            return budget.run_with_retries(body, dispatch, on_connection_error)
        finally:
            if self._redirect_origin is not None:
                self._redirect_origin = None
                self.close()

    def stream(
        self,
        method: str,
        path: str,
        body: Any,
        headers: dict[str, str],
        consume: Callable[[Any, RetryBudget], str | None],
        *,
        cancel: SendCancellation | None = None,
        status_callback: Callable[[str], None] | None = None,
        wire_secrets: list[str] | None = None,
        on_bad_request: Callable[[bytes], bytes | None] | None = None,
    ) -> str | None:
        """SSE streaming with the shared retry loop; dispatch lives on the transport like ``exchange``.

        Builds the ``RetryBudget``. Stop raises ``stopped_error()``; ``_run_streaming_loop`` maps that to finish reason ``stop``.
        ``consume``'s return value passes through, including a model finish_reason of ``stop``.
        """
        budget = RetryBudget(
            self,
            cancel=cancel,
            status_callback=status_callback,
            on_bad_request=on_bad_request,
        )
        current_model: str | None = None

        def on_connection_error(err: Exception) -> None:
            budget.connection_outcome(
                err,
                fatal=budget.emitted_any,
                attempt_charged=budget.attempt_charged,
                secrets=None,
                path=path,
                model=current_model,
                retry_log_message="Retrying streaming request on fresh connection",
                close_on_fatal=True,
            )

        def dispatch(current_body: Any) -> tuple[Literal["again", "done"], Any]:
            nonlocal current_model
            if budget.stopped():
                self.close()
                raise stopped_error()

            current_model = request_model_from_body(current_body)
            budget.attempt_charged = False

            response = self.send(
                method,
                path,
                current_body,
                headers,
                cancel=cancel,
                status_callback=status_callback,
            )
            if response.status != 200:
                attempt = budget.charge_send(status=True)
                self.handle_http_status(
                    response,
                    request_body=current_body,
                    path=path,
                    retries_left=budget.sends_left,
                    emitted_any=budget.emitted_any,
                    cancel=cancel,
                    status_callback=status_callback,
                    attempt=attempt,
                    secrets=wire_secrets,
                    observe_http_error=None,
                )
                return "again", current_body

            budget.clear_pacing_gap_when_unspent(current_model)

            try:
                res = consume(response, budget)
                return "done", res
            except StreamOverload:
                if budget.emitted_any or budget.sends_left <= 0:
                    raise
                attempt = budget.charge_send(status=False)
                delay = backoff_delay_sec(attempt=attempt)
                remember_host_gap(
                    self.pacing_key_for_model(current_model),
                    delay,
                )
                emit_retry_status(status_callback, delay)
                if not wait_abortable(delay, cancel):
                    raise stopped_error()
                return "again", current_body

        return budget.run_with_retries(body, dispatch, on_connection_error)

    def handle_connection_error(
        self,
        err: Exception,
        *,
        path: str,
        retries_left: int,
        retry_log_message: str,
        cancel: SendCancellation | None = None,
        status_callback: Callable[[str], None] | None = None,
        attempt: int = 1,
        model: str | None = None,
        secrets: list[str] | None = None,
    ) -> None:
        """Close failed connections and decide whether a request should retry."""
        # Stop closes the socket, which looks like a broken pipe. Check
        # cancel before log.error so a user cancel is debug, not ERROR.
        if is_cancelled(cancel):
            log.debug("Connection closed by user stop; exiting streaming loop")
            self.close()
            raise stopped_error()
        log.error("Connection error, closing: %s" % http_security.redact_secrets(str(err), secrets))
        self.close()
        if retries_left > 0 and enable_local_unverified(self._endpoint_parts()[1], err):
            # Immediate reopen: TLS mode just changed; do not add backoff.
            self.close()
            return

        err_msg = http_security.redact_secrets(format_error_message(err), secrets)
        if retries_left > 0:
            log.warning(retry_log_message)
            delay = backoff_delay_sec(attempt=attempt)
            # Store the backoff under pacing_key, not the host alone.
            # An OpenRouter ``:free`` failure would otherwise slow paid traffic
            # on that host and leave ``:free`` unpaced. Chat already uses pacing_key.
            remember_host_gap(self.pacing_key_for_model(model), delay)
            emit_retry_status(status_callback, delay)
            if not wait_abortable(delay, cancel):
                clear_host_gap(self.pacing_key_for_model(model))
                raise stopped_error()
            return
        log.error("Connection retry failed: %s" % err_msg)
        raise NetworkError(err_msg, code="CONNECTION_ERROR", details={"url": path}) from err

    def _read_bounded_response(
        self,
        response: Any,
        *,
        max_bytes: int,
        cancel: SendCancellation | None = None,
    ) -> bytes:
        # Check Content-Length first when provided by server
        cl = response.getheader("Content-Length")
        if cl is not None:
            try:
                if int(str(cl)) > max_bytes:
                    self.close()
                    raise NetworkError(f"HTTP response exceeded {max_bytes} bytes", code="HTTP_ERROR")
            except ValueError:
                pass

        # 1s socket timeout during chunked read allows checking cancel
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            try:
                sock.settimeout(1.0)
            except Exception:
                log.debug("response socket settimeout failed", exc_info=True)

        chunks: list[bytes] = []
        total = 0
        read_timeout = self._timeout_getter()
        deadline = time.monotonic() + read_timeout
        while True:
            self._raise_if_stopped(cancel)
            try:
                chunk = response.read(65536)
            except (TimeoutError, socket.timeout):
                self._raise_if_stopped(cancel)
                if time.monotonic() > deadline:
                    raise TimeoutError("HTTP read timed out")
                continue

            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                self.close()
                raise NetworkError(f"HTTP response exceeded {max_bytes} bytes", code="HTTP_ERROR")
            chunks.append(chunk)
        return b"".join(chunks)
