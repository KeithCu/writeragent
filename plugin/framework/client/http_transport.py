# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""One HTTP transport for stream, sync chat, image, speech, and catalog.

Stop, connect-vs-read timeout, retry, secret redaction, and strict JSON live
here. Callers do not keep a second copy.

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

import http.client
import json
import logging
import socket
import threading
import time
import urllib.parse
from typing import Any, Callable, Literal

from plugin.framework.errors import NetworkError
from plugin.framework.url_utils import get_url_hostname

from plugin.framework.errors import format_error_message
from .errors import _format_http_error_response
from .request_controls import RETRY_MAX_ATTEMPTS, RETRYABLE_HTTP_STATUS, LocalHttpsCertificateFallback, RequestPacer, backoff_delay_sec, clear_host_gap, emit_retry_status, ensure_free_model_pacing, mark_host_sent, pacing_key, parse_retry_after, remember_host_gap, request_model_from_body, wait_abortable, wait_host_gap
from .ssl_helpers import get_unverified_ssl_context, get_verified_ssl_context

log = logging.getLogger(__name__)

CONNECTION_ERRORS = (http.client.HTTPException, socket.error, OSError)
RetryAction = Literal["retry", "stop"]
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
_MAX_REDIRECTS = 5

HttpObserve = Callable[[Any, str, str, Any], str]
HttpSender = Callable[..., http.client.HTTPResponse]


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


def _response_header(response: Any, name: str) -> str:
    getter = getattr(response, "getheader", None)
    if not callable(getter):
        return ""
    value = getter(name)
    return value.strip() if isinstance(value, str) else ""


def _apply_redirect(
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


def parse_strict_json(raw: Any) -> Any:
    """Parse provider bytes with ``json.loads`` only.

    ``safe_json_loads`` repairs truncated model text, so a cut-off envelope
    such as ``{"choices":[{"message":{"content":"hel`` or a cut-off catalog
    ``{"data":[{"id":"gpt`` became a dict and looked finished. Provider
    envelopes and catalogs are not model text. A decode failure is
    ``BAD_RESPONSE``, not a reply.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NetworkError("LLM response was not JSON", code="BAD_RESPONSE") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise NetworkError("LLM response was not JSON", code="BAD_RESPONSE")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise NetworkError("LLM response was not JSON", code="BAD_RESPONSE") from exc


class HttpResult:
    """Buffered HTTP result. ``parsed`` is set only for strict JSON."""

    status: int
    body: bytes
    content_type: str
    parsed: Any

    def __init__(self, status: int, body: bytes, content_type: str, parsed: Any = None) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.parsed = parsed


def _response_content_type(response: Any) -> str:
    getter = getattr(response, "getheader", None)
    if not callable(getter):
        return ""
    value = getter("Content-Type")
    return value if isinstance(value, str) else ""


def _response_bytes(response: Any) -> bytes:
    raw = response.read()
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return raw.encode("utf-8")
    return b""


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


class LlmHttpTransport:
    """Own persistent chat HTTP connections plus pacing, jittered retries, per-host cooldown, and local TLS fallback."""

    _endpoint_getter: Callable[[], str]
    _timeout_getter: Callable[[], int | float]
    _pacer: RequestPacer
    _cert_fallback: LocalHttpsCertificateFallback

    def __init__(self, endpoint_getter: Callable[[], str], timeout_getter: Callable[[], int | float], *, pacer: RequestPacer | None = None, cert_fallback: LocalHttpsCertificateFallback | None = None) -> None:
        self._endpoint_getter = endpoint_getter
        self._timeout_getter = timeout_getter
        self._pacer = pacer or RequestPacer()
        self._cert_fallback = cert_fallback or LocalHttpsCertificateFallback()
        self._persistent_conn: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        self._conn_key: tuple[str, str, int, str] | None = None
        # Set while exchange follows a redirect onto a different origin.
        # Cleared before exchange returns so the next chat send uses the
        # configured endpoint again.
        self._redirect_origin: str | None = None

    @property
    def persistent_conn(self) -> http.client.HTTPConnection | http.client.HTTPSConnection | None:
        return self._persistent_conn

    @property
    def conn_key(self) -> tuple[str, str, int, str] | None:
        return self._conn_key

    def _endpoint_parts(self) -> tuple[str, str, int]:
        endpoint = self._redirect_origin if self._redirect_origin else self._endpoint_getter()
        parsed = _parse_url(endpoint)
        scheme = parsed.scheme.lower()
        host = get_url_hostname(endpoint)
        port = _explicit_port(parsed) or (443 if scheme == "https" else 80)
        return scheme, host, port

    def _absolute_url(self, path: str) -> str:
        scheme, host, port = self._endpoint_parts()
        default = 443 if scheme == "https" else 80
        shown = _bracket_ipv6_host(host)
        origin = f"{scheme}://{shown}" if port == default else f"{scheme}://{shown}:{port}"
        if not path.startswith("/"):
            path = "/" + path
        return origin + path

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
        # The constructor timeout is the connect budget, not request_timeout
        # (default 120). A dead host must not block DNS/TCP for the full stream
        # stall. send() raises the socket to the Settings read timeout after
        # connect returns.
        from plugin.framework.constants import LLM_CONNECT_TIMEOUT_SEC

        connect_timeout = LLM_CONNECT_TIMEOUT_SEC

        if scheme == "https":
            ssl_context = get_verified_ssl_context() if ssl_mode == "verified" else get_unverified_ssl_context()
            self._persistent_conn = http.client.HTTPSConnection(host, port, context=ssl_context, timeout=connect_timeout)
        else:
            self._persistent_conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)

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
                pass
            self._persistent_conn.close()
        except Exception:
            pass
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
            pass

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
        _stale_resend: bool = True,
    ) -> http.client.HTTPResponse:
        """Send one request on the persistent connection and return its response."""
        host = self.current_host()
        key = ensure_free_model_pacing(host, request_model_from_body(body))
        if not wait_host_gap(key, stop_checker, status_callback):
            raise NetworkError("LLM request aborted by Stop", code="STOPPED")
        conn = connection_getter() if connection_getter is not None else self.get_connection()
        conn.auto_open = 0
        # The timeout used to be stored only when the socket was opened.
        # ``LlmClient._timeout`` reads ``request_timeout`` on every call, so a
        # later change never reached a keep-alive connection. Connect still
        # uses the short connect budget; read uses Settings request_timeout.
        from plugin.framework.constants import LLM_CONNECT_TIMEOUT_SEC

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
            if stop_checker is not None and stop_checker():
                self._drop_stopped_connection(conn)
                raise NetworkError("LLM request aborted by Stop", code="STOPPED")
            self._connect_abortable(conn, stop_checker, connect_timeout=LLM_CONNECT_TIMEOUT_SEC)
            sock = getattr(conn, "sock", None)
        conn.timeout = read_timeout
        if sock is not None:
            sock.settimeout(read_timeout)
        if stop_checker is not None and stop_checker():
            self._drop_stopped_connection(conn)
            raise NetworkError("LLM request aborted by Stop", code="STOPPED")
        self._pacer.wait_before_send()
        if stop_checker is not None and stop_checker():
            self._drop_stopped_connection(conn)
            raise NetworkError("LLM request aborted by Stop", code="STOPPED")
        if not any(k.lower() == "user-agent" for k in headers):
            headers = dict(headers)
            from plugin.framework.constants import USER_AGENT

            headers["User-Agent"] = USER_AGENT
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
            if (stop_checker is not None and stop_checker()) or not reused_socket or not _stale_resend:
                raise
            log.debug("Keep-alive socket failed before a response; reconnecting once")
            self._drop_stopped_connection(conn)
            return self.send(method, path, body, headers, connection_getter=connection_getter, stop_checker=stop_checker, status_callback=status_callback, _stale_resend=False)

    def enable_local_ssl_fallback(self, err: Exception) -> bool:
        enabled = self._cert_fallback.enable_if_applicable(self.current_host(), err)
        if enabled:
            self.close()
        return enabled

    def _connect_abortable(self, conn: http.client.HTTPConnection | http.client.HTTPSConnection, stop_checker: Callable[[], bool] | None, *, connect_timeout: float) -> None:
        """Connect without waiting out the read budget, and without ignoring Stop.

        ``http.client.connect`` blocks inside ``getaddrinfo`` before it assigns
        ``sock``. The socket timeout does not bound ``getaddrinfo``, so
        ``close()`` sees ``sock is None`` and Stop during DNS is a no-op until
        the resolver finishes — often the whole 120s read budget. Run connect
        on a dedicated worker (the caller joins it; a pool slot would
        deadlock). Poll Stop and the connect deadline, then close whatever
        socket has appeared. Do not wait for DNS to finish.
        """
        from plugin.framework.worker_pool import run_in_background

        outcome: dict[str, Any] = {}
        cancelled = threading.Event()
        conn.timeout = connect_timeout

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
        deadline = time.monotonic() + float(connect_timeout)
        while not outcome.get("done"):
            if stop_checker is not None and stop_checker():
                cancelled.set()
                self._drop_stopped_connection(conn)
                raise NetworkError("LLM request aborted by Stop", code="STOPPED")
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
        stop_checker: Callable[[], bool] | None = None,
        status_callback: Callable[[str], None] | None = None,
        attempt: int = 1,
        secrets: list[str] | None = None,
        observe_http_error: HttpObserve | None = None,
    ) -> RetryAction:
        """On non-200: retry 429/503/529 while attempts remain; else raise.

        Reads the error body once. ``observe_http_error`` is the chat 500
        diagnostic hook; catalog and speech omit it and still get redaction.
        """
        err_body = _response_bytes(response).decode("utf-8", errors="replace")
        wire_secrets = collect_secrets(secrets, secrets_from_target(path))
        if observe_http_error is not None:
            message = observe_http_error(response, err_body, path, request_body)
        else:
            message = _format_http_error_response(int(response.status), str(getattr(response, "reason", "") or ""), err_body)
        message = redact_secrets(message, wire_secrets)
        safe_target = public_target(path)
        if observe_http_error is None:
            log.error("HTTP %s for %s: %s", response.status, safe_target, message)
        self.close()
        status = int(response.status)
        if status in RETRYABLE_HTTP_STATUS and retries_left > 0 and not emitted_any:
            retry_after = None
            getter = getattr(response, "getheader", None)
            if callable(getter):
                header = getter("Retry-After")
                retry_after = parse_retry_after(header if isinstance(header, str) else None)
            delay = backoff_delay_sec(attempt=attempt, retry_after_sec=retry_after)
            model = request_model_from_body(request_body)
            remember_host_gap(pacing_key(self.current_host(), model), delay)
            log.warning("Retrying HTTP %s after %.3fs (Retry-After=%s attempt=%s left=%s)", status, delay, retry_after, attempt, retries_left)
            emit_retry_status(status_callback, delay)
            if not wait_abortable(delay, stop_checker):
                return "stop"
            return "retry"
        details: dict[str, Any] = {"url": safe_target, "status": status}
        raise NetworkError(message, code="HTTP_ERROR", details=details)

    def exchange(
        self,
        method: str,
        path: str,
        body: Any,
        headers: dict[str, str] | None = None,
        *,
        stop_checker: Callable[[], bool] | None = None,
        status_callback: Callable[[str], None] | None = None,
        parse_json: bool = False,
        sender: HttpSender | None = None,
        secrets: list[str] | None = None,
        on_retry: Callable[[], None] | None = None,
        after_read: Callable[[Any], None] | None = None,
        observe_http_error: HttpObserve | None = None,
        retry_log_message: str = "Retrying HTTP request on a fresh connection",
    ) -> HttpResult:
        """Buffered request with the shared stop, timeout, retry, and redaction.

        ``parse_json`` uses :func:`parse_strict_json`. Truncated JSON raises
        ``BAD_RESPONSE`` instead of becoming a repaired object.

        301/302/303/307/308 are followed up to ``_MAX_REDIRECTS`` hops.
        301/302/303 switch a non-GET to GET and drop the body. 307/308 keep
        the method and body. A redirect onto another host drops secret
        headers and opens a new connection. That host is not kept as the
        persistent chat endpoint.
        """
        wire_headers = dict(headers or {})
        wire_secrets = collect_secrets(secrets, secrets_from_headers(wire_headers), secrets_from_target(path))
        self._redirect_origin = None

        def _send(send_method: str, send_path: str, send_body: Any, send_headers: dict[str, str], *, stop_checker: Callable[[], bool] | None = None, status_callback: Callable[[str], None] | None = None) -> http.client.HTTPResponse:
            # A custom sender is bound to the caller's connection. After a
            # cross-origin redirect, send() on this transport uses the new host.
            if sender is not None and self._redirect_origin is None:
                return sender(send_method, send_path, send_body, send_headers, stop_checker=stop_checker, status_callback=status_callback)
            return self.send(send_method, send_path, send_body, send_headers, stop_checker=stop_checker, status_callback=status_callback)

        sends_left = RETRY_MAX_ATTEMPTS
        attempt = 0
        redirects_followed = 0
        model = request_model_from_body(body)
        try:
            while True:
                body_in_hand = False
                # True once this iteration has already spent one attempt on a non-200.
                attempt_charged = False
                try:
                    if stop_checker is not None and stop_checker():
                        self.close()
                        raise NetworkError("LLM request aborted by Stop", code="STOPPED")
                    response = _send(method, path, body, wire_headers, stop_checker=stop_checker, status_callback=status_callback)
                    status = int(getattr(response, "status", 0) or 0)
                    if status in _REDIRECT_STATUSES and redirects_followed < _MAX_REDIRECTS:
                        # A CDN 301/302/307 is not a failure. Image URLs, TTS audio,
                        # catalogs, and update checks follow Location. One hop
                        # rewrites method/body, then loops. The retry budget is
                        # for 429/503, not for a redirect the server asked for.
                        current_url = self._absolute_url(path)
                        hop = _apply_redirect(method, body, wire_headers, current_url, status, _response_header(response, "Location"))
                        if hop is not None:
                            _response_bytes(response)
                            new_method, new_body, new_headers, new_origin, new_path = hop
                            if _origin_key(new_origin) != _origin_key(current_url):
                                self._redirect_origin = new_origin
                                self.close()
                            log.debug("Following HTTP %s to %s", status, public_target(new_origin + new_path))
                            method = new_method
                            body = new_body
                            wire_headers = new_headers
                            path = new_path
                            redirects_followed += 1
                            wire_secrets = collect_secrets(secrets, secrets_from_headers(wire_headers), secrets_from_target(path))
                            continue
                    if status != 200:
                        sends_left -= 1
                        attempt += 1
                        attempt_charged = True
                        action = self.handle_http_status(
                            response,
                            request_body=body,
                            path=path,
                            retries_left=sends_left,
                            emitted_any=False,
                            stop_checker=stop_checker,
                            status_callback=status_callback,
                            attempt=attempt,
                            secrets=wire_secrets,
                            observe_http_error=observe_http_error,
                        )
                        if action == "stop":
                            raise NetworkError("LLM request aborted by Stop", code="STOPPED")
                        if on_retry is not None:
                            on_retry()
                        continue
                    if attempt == 0:
                        clear_host_gap(pacing_key(self.current_host(), model))
                    raw = _response_bytes(response)
                    body_in_hand = True
                    if after_read is not None:
                        after_read(response)
                    parsed = parse_strict_json(raw) if parse_json else None
                    return HttpResult(status=status, body=raw, content_type=_response_content_type(response), parsed=parsed)
                except NetworkError:
                    raise
                except CONNECTION_ERRORS as exc:
                    if body_in_hand:
                        raise NetworkError(redact_secrets(format_error_message(exc), wire_secrets), code="CONNECTION_LOST", details={"url": public_target(path)}) from exc
                    # handle_http_status reads the error body, and that read can
                    # raise IncompleteRead, a reset, or a timeout. The non-200
                    # branch above already decremented sends_left. Charging
                    # again would consume two of the three attempts for one
                    # failed read, and the last try would never run.
                    if not attempt_charged:
                        sends_left -= 1
                        attempt += 1
                    action = self.handle_connection_error(
                        exc,
                        path=public_target(path),
                        retries_left=sends_left,
                        retry_log_message=retry_log_message,
                        stop_checker=stop_checker,
                        status_callback=status_callback,
                        attempt=attempt,
                        model=model,
                        secrets=wire_secrets,
                    )
                    if action == "stop":
                        raise NetworkError("LLM request aborted by Stop", code="STOPPED")
                    continue
        finally:
            if self._redirect_origin is not None:
                self._redirect_origin = None
                self.close()

    def handle_connection_error(self, err: Exception, *, path: str, retries_left: int, retry_log_message: str, stop_checker: Callable[[], bool] | None = None, status_callback: Callable[[str], None] | None = None, attempt: int = 1, model: str | None = None, secrets: list[str] | None = None) -> RetryAction:
        """Close failed connections and decide whether a request should retry."""
        # Stop closes the socket, which looks like a broken pipe. Check
        # stop_checker before log.error so a user cancel is debug, not ERROR.
        if stop_checker and stop_checker():
            log.debug("Connection closed by user stop; exiting streaming loop")
            self.close()
            return "stop"
        log.error("Connection error, closing: %s" % redact_secrets(str(err), secrets))
        self.close()
        if retries_left > 0 and self.enable_local_ssl_fallback(err):
            # Immediate reopen: TLS mode just changed; do not add backoff.
            return "retry"

        err_msg = redact_secrets(format_error_message(err), secrets)
        if retries_left > 0:
            log.warning(retry_log_message)
            delay = backoff_delay_sec(attempt=attempt)
            # Store the backoff under pacing_key, not current_host() alone.
            # An OpenRouter ``:free`` failure would otherwise slow paid traffic
            # on that host and leave ``:free`` unpaced. Chat already uses pacing_key.
            remember_host_gap(pacing_key(self.current_host(), model), delay)
            emit_retry_status(status_callback, delay)
            if not wait_abortable(delay, stop_checker):
                return "stop"
            return "retry"
        log.error("Connection retry failed: %s" % err_msg)
        raise NetworkError(err_msg, code="CONNECTION_ERROR", details={"url": path}) from err
