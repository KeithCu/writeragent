# WriterAgent - Python Compute Service Server
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lightweight HTTP server for sandboxed Python execution using standard wsgiref."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hmac
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.parse
from typing import Any, Callable

# Ensure repo root is on sys.path to resolve plugin.* / compute_service imports
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service import __version__
from compute_service import error_codes as ec
from compute_service.config import DEFAULT_SETTINGS, ComputeSettings, ConfigError, clamp_timeout_sec, load_settings, ocr_path_is_allowed
from compute_service.wsgi_server import HTTP_DRAIN_SEC, REQUEST_READ_TIMEOUT_SEC, REQUEST_WRITE_TIMEOUT_SEC, WSGIDualStackServer, admission_permits, listener_thread_count, request_conn
from plugin.framework.child_runtime import Deadline
from compute_service.json_forward import (
    ExecuteRequestError,
    ExecuteRequestParts,
    VisionRequestParts,
    _reject_nonfinite_number,
    canonical_execute_mode,
    is_multipart_content_type,
    parse_execute_request,
    parse_multipart_vision,
    validate_session_id,
)
from plugin.vision.vision_common import IMPLEMENTED_HELPERS

log = logging.getLogger("compute_service")


def _accept_clock(accept_time: float | None) -> float:
    """Accept monotonic timestamp from the listener, or now when missing."""
    if accept_time is not None:
        return accept_time
    return time.monotonic()


def setup_logging(level_name: str = "INFO") -> None:
    """Configure standard logging format and level for the compute service."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", force=True)
    log.setLevel(level)


def check_dependencies(pool: Any) -> None:
    """Verify required dependencies are importable in worker; exit if missing."""
    ok, err = pool.check_dependencies()
    if not ok:
        print(err or "Error: Required dependencies are not installed in the worker Python environment.\nPlease start the server using './compute_service/start.sh' or activate the correct virtual environment.", file=sys.stderr)
        from compute_service.formula_pool import shutdown_formula_pool

        shutdown_formula_pool()
        sys.exit(1)


# =============================================================================
# Response & Error Helpers
# =============================================================================


def _inject_req_id(body: dict[str, Any], req_id: Any) -> dict[str, Any]:
    """Attach correlation id to response body dict if present."""
    if req_id is not None:
        body["id"] = req_id
    return body


def _start_raw_json(start_response: Any, status: str, body: bytes, *, extra_headers: list[tuple[str, str]] | None = None) -> list[bytes]:
    """Send already-encoded JSON bytes (worker result_json) without re-dumps."""
    headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def _start_json(start_response: Any, status: str, payload: dict[str, Any], *, extra_headers: list[tuple[str, str]] | None = None) -> list[bytes]:
    """Serialize and send a JSON payload dict."""
    return _start_raw_json(start_response, status, json.dumps(payload, allow_nan=False).encode("utf-8"), extra_headers=extra_headers)


def _error(start_response: Any, code: str, msg: str, req_id: Any = None, *, extra_headers: list[tuple[str, str]] | None = None) -> list[bytes]:
    """Send a coded JSON error; HTTP status comes from ``error_codes.HTTP_STATUS``."""
    status = ec.HTTP_STATUS[code]
    payload: dict[str, Any] = {"status": "error", "error": msg, "code": code}
    if req_id is not None:
        payload["id"] = req_id
    return _start_json(start_response, status, payload, extra_headers=extra_headers)


def _infrastructure_status(payload: dict[str, Any]) -> str | None:
    """Map a worker payload to an HTTP status, or None to keep 200.

    503 is only the miss-and-retry set. A crash, an empty frame, or a
    dict-shaped worker fault (``WORKER_EXECUTION_ERROR``,
    ``VISION_WORKER_ERROR``) is 500. Bad vision input and a denied OCR
    path are 400. OCR disabled is 501. Raw ``result_json`` never reaches
    this map.
    """
    if payload.get("status") != "error":
        return None
    code = payload.get("code")
    if not isinstance(code, str):
        return None
    status = ec.HTTP_STATUS.get(code)
    return None if status == "200 OK" else status


def _send_execution_result(start_response: Any, result_payload: Any, req_id: Any, *, error_status: str = "200 OK") -> list[bytes]:
    """Send worker result (raw JSON bytes, pool error status, or serialized dict).

    Eval errors stay HTTP 200 so the sheet shows them. Pass *error_status*
    when an unmapped ``status: error`` is a server fault (session reset).
    Mapped codes, including 413, come from ``error_codes.HTTP_STATUS``.
    Every ``_start_json`` path shares one encode guard. Raw ``result_json``
    bytes are already encoded and stay outside it. Non-empty bytes are
    always 200. Empty bytes are ``EMPTY_RESPONSE`` at 500: they are falsy
    and would fall through into ``json.dumps`` of a dict that still held them.
    *error_status* applies only to a dict with no ``result_json``.
    ``reset_session`` does not return ``result_json``, so its 500 override
    is not skipped. A formula ``WORKER_EXECUTION_ERROR`` is inside those
    bytes on purpose; re-statusing them would hide the cell text behind #N/A.
    """
    if isinstance(result_payload, dict):
        raw_out = result_payload.get("result_json")
        if isinstance(raw_out, (bytes, bytearray)):
            if raw_out:
                return _start_raw_json(start_response, "200 OK", bytes(raw_out))
            # Empty bytes are falsy. An empty frame is EMPTY_RESPONSE, not a second encode.
            return _error(start_response, ec.EMPTY_RESPONSE, "Worker returned an empty result.", req_id=req_id)
        infra = _infrastructure_status(result_payload)
        _inject_req_id(result_payload, req_id)
        if infra is not None:
            http_status = infra
        elif result_payload.get("status") == "error" and error_status != "200 OK":
            http_status = error_status
        else:
            http_status = "200 OK"
    else:
        http_status = "200 OK"

    try:
        return _start_json(start_response, http_status, result_payload)
    except (TypeError, ValueError) as e:
        return _error(start_response, ec.INTERNAL_ERROR, f"JSON encode failed: {e}")


ExecuteFn = Callable[..., dict[str, Any]]
ResetFn = Callable[..., dict[str, Any]]

# =============================================================================
# Request Parsing & Validation Helpers
# =============================================================================


def _drain_body_before_error(environ: dict[str, Any], max_bytes: int = 1024 * 1024) -> bool:
    """Read the unread request body before an error reply so the client does not get a TCP RST.

    Reads stop after one second (each read's socket timeout is the time left,
    and the previous timeout is put back) or *max_bytes*. Returns whether the
    declared body was fully consumed; when it was not (chunked, a bad length,
    the cap, EOF or a timeout) the connection is marked to close so leftover
    bytes never parse as the next request.
    """
    rc = request_conn(environ)
    if _transfer_encoding_is_chunked(environ):
        rc.close_connection = True
        return False
    # Already buffered: reading Content-Length again would eat the next request.
    if rc.raw_body is not None:
        return True
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        rc.close_connection = True
        return False
    stream = environ.get("wsgi.input")
    cap = min(length, max_bytes)
    done = 0
    if cap > 0 and stream is not None:
        conn = rc.connection
        saved: float | None = None
        try:
            saved = conn.gettimeout() if conn is not None else None
        except OSError:
            conn = None  # A timeout that cannot be read cannot be restored, so it is left alone.
        read = getattr(stream, "read1", None) or stream.read
        deadline = min(time.monotonic() + 1.0, _accept_clock(rc.accept_time) + REQUEST_READ_TIMEOUT_SEC)
        try:
            while done < cap and (budget := deadline - time.monotonic()) > 0:
                if conn is not None:
                    conn.settimeout(max(0.1, budget))
                chunk = read(min(64 * 1024, cap - done))
                if not chunk:
                    break
                done += len(chunk)
        except OSError:
            pass
        finally:
            if conn is not None:
                try:
                    conn.settimeout(saved)
                except OSError:
                    pass
    if done < length:
        rc.close_connection = True
        return False
    return True


def _has_declared_body(environ: dict[str, Any]) -> bool:
    """True when client declared a non-empty request body or chunked encoding."""
    if _transfer_encoding_is_chunked(environ):
        return True
    raw_len = environ.get("CONTENT_LENGTH")
    if raw_len:
        try:
            return int(raw_len) > 0
        except (ValueError, TypeError):
            return True
    return False


def _set_write_deadline(environ: dict[str, Any]) -> None:
    """Set a bounded socket write deadline once the body is buffered.

    The handler sets a read timeout so a client that accepts and then sends
    nothing cannot hold a worker thread indefinitely. Once the request body
    is buffered, reset that socket timeout to a bounded write deadline
    (REQUEST_WRITE_TIMEOUT_SEC) so that in-progress execution is not prematurely
    aborted while preparing to stream or complete the calculation.
    """
    conn = request_conn(environ).connection
    if conn is not None:
        try:
            conn.settimeout(REQUEST_WRITE_TIMEOUT_SEC)
        except OSError:
            pass


def _validate_source_text(raw: Any, *, limit: int, label: str, required: bool, max_bytes: int | None = None) -> str | None:
    """Validate and return text from part, or raise ExecuteRequestError.

    Character length is checked after UTF-8 decode. *max_bytes* rejects a
    part before that decode; the HTTP body cap is the right bound.
    """
    if isinstance(raw, (bytes, bytearray)):
        if max_bytes is not None and len(raw) > max_bytes:
            raise ExecuteRequestError(f"{label} exceeds max body size ({max_bytes} bytes).", code=ec.CODE_TOO_LARGE)
        if len(raw) == 0:
            text: str | None = None
        else:
            try:
                text = bytes(raw).decode("utf-8")
            except UnicodeDecodeError:
                raise ExecuteRequestError(f"Invalid UTF-8 in {label} part.")
    elif isinstance(raw, str):
        text = raw
    elif raw is None:
        text = None
    else:
        raise ExecuteRequestError(f"{label} must be text.")

    if text is None or text == "":
        if required:
            raise ExecuteRequestError(f"Missing '{label}' string parameter.")
        return None
    if len(text) > limit:
        raise ExecuteRequestError(f"{label} exceeds max_code_chars ({limit}).", code=ec.CODE_TOO_LARGE)
    return text


def _transfer_encoding_is_chunked(environ: dict[str, Any]) -> bool:
    """True when the client asked for a chunked body.

    This server speaks HTTP/1.1 and still reads ``Content-Length``. A chunked
    body is not decoded. A missing length is not ``{}``: a chunked reset would
    otherwise succeed and ignore the body.
    """
    raw = environ.get("HTTP_TRANSFER_ENCODING")
    if not isinstance(raw, str):
        return False
    return "chunked" in raw.lower()


def _reject_chunked(environ: dict[str, Any], start_response: Any) -> list[bytes] | None:
    """400 when the body is chunked. ``None`` when the request may continue."""
    if not _transfer_encoding_is_chunked(environ):
        return None
    request_conn(environ).close_connection = True
    return _error(start_response, ec.BAD_FRAMING, "Chunked transfer encoding is not supported")


def _read_request_body(environ: dict[str, Any], settings: ComputeSettings, start_response: Any) -> tuple[bytes | None, list[bytes] | None]:
    """Read a bounded POST body with total read deadline enforcement. Returns ``(body, None)`` or ``(None, error_body)``."""
    rc = request_conn(environ)
    cached = rc.raw_body
    if cached is not None:
        return cached, None
    chunked = _reject_chunked(environ, start_response)
    if chunked is not None:
        return None, chunked
    raw_len = environ.get("CONTENT_LENGTH")
    if raw_len is None or raw_len == "":
        rc.close_connection = True
        return None, _error(start_response, ec.BAD_FRAMING, "Missing Content-Length")
    try:
        content_length = int(raw_len)
    except (TypeError, ValueError):
        rc.close_connection = True
        return None, _error(start_response, ec.BAD_FRAMING, "Invalid Content-Length")
    if content_length <= 0:
        rc.close_connection = True
        return None, _error(start_response, ec.BAD_FRAMING, "Invalid Content-Length")
    if content_length > settings.max_body_bytes:
        _drain_body_before_error(environ, max_bytes=64 * 1024)
        rc.close_connection = True
        return None, _error(start_response, ec.PAYLOAD_TOO_LARGE, "Request body too large")

    wsgi_input = environ.get("wsgi.input")
    if wsgi_input is None:
        rc.close_connection = True
        return None, _error(start_response, ec.INTERNAL_ERROR, "Missing wsgi.input stream")

    def fail(code: str, msg: str) -> tuple[None, list[bytes]]:
        rc.close_connection = True
        _set_write_deadline(environ)
        return None, _error(start_response, code, msg)

    conn = rc.connection
    read_deadline = _accept_clock(rc.accept_time) + REQUEST_READ_TIMEOUT_SEC

    chunks: list[bytes] = []
    bytes_read = 0
    buf_size = 64 * 1024
    # read() can loop many recv()s under one timeout, so a slow drip outlives
    # the deadline. read1() is one raw read; the timeout is then the time left.
    read1 = getattr(wsgi_input, "read1", None)

    try:
        while bytes_read < content_length:
            now = time.monotonic()
            if now >= read_deadline:
                return fail(ec.READ_TIMEOUT, "Request read timeout")
            remaining_time = read_deadline - now
            if conn is not None:
                try:
                    conn.settimeout(max(0.1, remaining_time))
                except Exception:
                    # If the deadline cannot be armed, stop. The next read would
                    # keep the previous socket timeout.
                    log.debug("failed to arm body-read deadline", exc_info=True)
                    return fail(ec.READ_TIMEOUT, "Request read timeout")

            to_read = min(buf_size, content_length - bytes_read)
            raw_chunk: Any = read1(to_read) if callable(read1) else wsgi_input.read(to_read)
            if not isinstance(raw_chunk, (bytes, bytearray)) or not raw_chunk:
                break
            chunk = bytes(raw_chunk)
            chunks.append(chunk)
            bytes_read += len(chunk)
    except TimeoutError:
        return fail(ec.READ_TIMEOUT, "Request read timeout")
    except OSError as e:
        log.warning("Socket read error during body ingress: %s", e)
        return fail(ec.BAD_FRAMING, "Connection error reading request body")

    _set_write_deadline(environ)
    if bytes_read != content_length:
        rc.close_connection = True
        return None, _error(start_response, ec.BAD_FRAMING, "Request body truncated")

    body = b"".join(chunks)
    # Admission reads the body before the permit wait. The handler must not
    # read the socket again: on a keep-alive connection that would consume
    # the next request.
    rc.raw_body = body
    return body, None


def _read_request_json(environ: dict[str, Any], settings: ComputeSettings, start_response: Any) -> tuple[dict[str, Any] | None, list[bytes] | None]:
    """Read request body and parse as top-level JSON dict."""
    body, err_resp = _read_request_body(environ, settings, start_response)
    if err_resp is not None:
        return None, err_resp
    assert body is not None

    def _reject_const(token: str) -> None:
        raise ValueError(token)

    try:
        data = json.loads(body.decode("utf-8"), parse_constant=_reject_const)
    except Exception:
        return None, _error(start_response, ec.BAD_REQUEST_BODY, "Invalid JSON")

    if not isinstance(data, dict):
        return None, _error(start_response, ec.BAD_REQUEST_BODY, "JSON body must be an object")

    # The id is echoed on every reply, so it must be a finite scalar before it is cached.
    req_id = data.get("id")
    try:
        if req_id is not None and (not isinstance(req_id, (str, int)) or isinstance(req_id, bool)):
            raise ExecuteRequestError("id must be a scalar")
        _reject_nonfinite_number(req_id)
    except ExecuteRequestError:
        return None, _error(start_response, ec.BAD_REQUEST_BODY, "Invalid JSON")

    return data, None


def _read_optional_request_json(environ: dict[str, Any], settings: ComputeSettings, start_response: Any) -> tuple[dict[str, Any] | None, list[bytes] | None]:
    """Parse an optional POST JSON object. Missing or empty body is ``{}``.

    ``/v1/session/reset`` allows an empty body (correlation ``id`` only).
    A missing or zero ``Content-Length`` is that empty body. Chunked
    transfer encoding is 400 here, same as ``/v1/execute`` and ``/v1/vision``.
    """
    chunked = _reject_chunked(environ, start_response)
    if chunked is not None:
        return None, chunked
    raw_len = environ.get("CONTENT_LENGTH")
    if not raw_len:
        return {}, None
    try:
        if int(raw_len) == 0:
            return {}, None
    except (TypeError, ValueError):
        pass
    return _read_request_json(environ, settings, start_response)


def authenticate_request(environ: dict[str, Any], settings: ComputeSettings) -> str | None:
    """Validate Authorization when an API key is configured.

    Returns the error reason string on failure, or None on success.
    """
    if not settings.auth_required:
        return None

    raw = environ.get("HTTP_AUTHORIZATION")
    if not isinstance(raw, str) or not raw:
        return "missing"

    if len(raw) < 7 or raw[:7].lower() != "bearer ":
        return "malformed"

    provided = raw[7:]
    expected = settings.api_key
    try:
        provided_bytes = provided.encode("utf-8")
        expected_bytes = expected.encode("utf-8")
    except UnicodeEncodeError:
        return "invalid"
    if not hmac.compare_digest(provided_bytes, expected_bytes):
        return "invalid"
    return None


def _check_keyless_cors(environ: dict[str, Any], settings: ComputeSettings, start_response: Any) -> list[bytes] | None:
    """In keyless mode, reject cross-origin requests and non-loopback hosts to prevent CSRF/SSRF."""
    if settings.auth_required:
        return None

    if environ.get("HTTP_ORIGIN"):
        if _has_declared_body(environ):
            request_conn(environ).close_connection = True
        return _error(start_response, ec.CROSS_ORIGIN_REFUSED, "Cross-origin requests are forbidden in keyless mode.")

    # HTTP/1.0 may omit Host. No Host is not a loopback Host, so keyless
    # mode still refuses it.
    raw_host_value = environ.get("HTTP_HOST")
    if not isinstance(raw_host_value, str) or not raw_host_value.strip():
        if _has_declared_body(environ):
            request_conn(environ).close_connection = True
        return _error(start_response, ec.CROSS_ORIGIN_REFUSED, "Only loopback hosts are allowed in keyless mode.")
    raw_host = raw_host_value.lower()
    if raw_host.startswith("["):
        host = raw_host.split("]")[0] + "]"
    else:
        host = raw_host.split(":")[0]

    if host not in ("localhost", "127.0.0.1", "[::1]"):
        if _has_declared_body(environ):
            request_conn(environ).close_connection = True
        return _error(start_response, ec.CROSS_ORIGIN_REFUSED, "Only loopback hosts are allowed in keyless mode.")
    return None


# Fixed in code on purpose, not a setting: one warning per client IP and reason per interval.
_AUTH_WARN_INTERVAL_SEC = 60.0
_auth_warn_lock = threading.Lock()
_auth_warn_history: dict[tuple[str, str], float] = {}


def _log_auth_warning(remote_addr: str, reason: str) -> None:
    now = time.monotonic()
    should_warn = False
    with _auth_warn_lock:
        cutoff = now - _AUTH_WARN_INTERVAL_SEC
        expired = [k for k, t in _auth_warn_history.items() if t < cutoff]
        for k in expired:
            _auth_warn_history.pop(k, None)
        last_t = _auth_warn_history.get((remote_addr, reason))
        if last_t is None or (now - last_t) >= _AUTH_WARN_INTERVAL_SEC:
            _auth_warn_history[(remote_addr, reason)] = now
            should_warn = True
    if should_warn:
        log.warning("Authentication failed: remote_addr=%s reason=%s", remote_addr, reason)


def _authenticate_or_401(environ: dict[str, Any], settings: ComputeSettings, start_response: Any) -> list[bytes] | None:
    """Authenticate request; return 401 response on failure or None on success."""
    auth_err = authenticate_request(environ, settings)
    if auth_err is not None:
        remote_addr = str(environ.get("REMOTE_ADDR") or "")
        _log_auth_warning(remote_addr, auth_err)
        _drain_body_before_error(environ)
        return _error(start_response, ec.UNAUTHORIZED, "Unauthorized", extra_headers=[("WWW-Authenticate", "Bearer")])
    return _check_keyless_cors(environ, settings, start_response)


def _parse_session_id(environ: dict[str, Any]) -> str | None:
    """Extract and validate the session_id URL query parameter, if present."""
    query_string = environ.get("QUERY_STRING", "")
    query_params = urllib.parse.parse_qs(query_string, keep_blank_values=False)
    session_ids = query_params.get("session_id")
    if session_ids and session_ids[0].strip():
        return validate_session_id(session_ids[0])
    return None


# =============================================================================
# Route Handlers & WSGI Application Router
# =============================================================================


@dataclass
class _Admission:
    """Admitted request: its one deadline and the parsed body.

    ``deadline`` starts at the accept stamp (kept-alive resubmits are
    re-stamped; without one, now). The semaphore wait, the pool lease, and
    the child all spend this one clock.
    """

    deadline: Deadline
    parsed: Any = None
    req_id: Any = None


def _admission(environ: dict[str, Any], timeout_sec: float, parsed: Any, req_id: Any) -> _Admission:
    end = _accept_clock(request_conn(environ).accept_time) + timeout_sec
    return _Admission(deadline=Deadline.from_absolute(timeout_sec, end), parsed=parsed, req_id=req_id)


def _admit_body(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
    *,
    load: Callable[[dict[str, Any], ComputeSettings, Any], tuple[Any, list[bytes] | None]],
    default_timeout_sec: int | None = None,
) -> tuple[_Admission | None, list[bytes] | None]:
    """Admit and validate request body, returning an _Admission or error response."""
    parts, err = load(environ, settings, start_response)
    if err is not None:
        return None, err
    assert parts is not None
    timeout_sec = float(
        clamp_timeout_sec(
            parts.timeout_ms,
            is_ms=True,
            default_timeout_sec=settings.default_timeout_sec if default_timeout_sec is None else default_timeout_sec,
            max_timeout_sec=settings.max_timeout_sec,
        )
    )
    return _admission(environ, timeout_sec, parts, parts.req_id), None


def _admit_reset(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[_Admission | None, list[bytes] | None]:
    """Admit and validate optional reset request body, returning an _Admission or error response."""
    data, err = _read_optional_request_json(environ, settings, start_response)
    if err is not None:
        return None, err
    assert data is not None
    return _admission(environ, float(settings.default_timeout_sec), data, data.get("id")), None


def _admit_vision(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[_Admission | None, list[bytes] | None]:
    """Admit vision request, returning 501 early if vision workers are disabled."""
    if settings.ocr_workers <= 0:
        if _has_declared_body(environ):
            request_conn(environ).close_connection = True
        return None, _error(start_response, ec.VISION_SERVICE_DISABLED, "Vision / OCR service is not enabled on this instance (ocr_workers=0).")
    return _admit_body(
        environ,
        settings,
        start_response,
        default_timeout_sec=settings.ocr_timeout_sec,
        load=_load_vision,
    )


Prepare = Callable[[dict[str, Any], Any], tuple[_Admission | None, list[bytes] | None]]
Handle = Callable[[dict[str, Any], Any, _Admission], list[bytes]]


@dataclass(frozen=True, slots=True)
class _Route:
    method: str
    is_body_route: bool
    prepare: Prepare
    handle: Handle
    busy_code: str = ec.WORKER_POOL_BUSY
    busy_message: str = "All compute workers are currently busy."


def _gated(
    environ: dict[str, Any],
    start_response: Any,
    settings: ComputeSettings,
    semaphore: threading.Semaphore,
    route: _Route,
) -> list[bytes]:
    """Execute route.handle guarded by auth, concurrency permit, try/finally, and 500 fallback.

    ``route.handle`` receives environ, the tracking ``start_response``, and the admission record.
    A second call is illegal in wsgiref, so a failure after headers are sent is logged
    and returns an empty body instead of another status line.

    ``route.prepare`` runs after auth and before the permit. It reads the body
    and returns the admission record. An error response from ``route.prepare`` does
    not take a permit.
    """
    started = False

    def _start(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
        nonlocal started
        started = True
        # Pass exc_info only when set. Test doubles and some WSGI callers
        # take (status, headers) and reject a trailing None.
        if exc_info is not None:
            return start_response(status, headers, exc_info)
        return start_response(status, headers)

    auth_resp = _authenticate_or_401(environ, settings, _start)
    if auth_resp is not None:
        return auth_resp

    admission: _Admission | None = None
    acquired = False
    path = environ.get("PATH_INFO", "")
    try:
        admission, prep_err = route.prepare(environ, _start)
        if prep_err is not None:
            return prep_err
        assert admission is not None

        req_id = admission.req_id

        remaining = admission.deadline.left()
        if remaining <= 0:
            return _error(_start, ec.QUEUE_TIMEOUT, "Request deadline expired before execution.", req_id=req_id, extra_headers=[("Retry-After", "1")])

        if not semaphore.acquire(timeout=remaining):
            return _error(_start, route.busy_code, route.busy_message, req_id=req_id, extra_headers=[("Retry-After", "1")])
        acquired = True

        log.debug("exec %s id=%r", path, req_id)
        start_t = time.perf_counter()
        res = route.handle(environ, _start, admission)
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        log.info("done %s id=%r duration=%.2fms", path, req_id, duration_ms)
        return res
    except Exception as e:
        log.exception("fail %s: %s", path, e)
        request_conn(environ).close_connection = True
        if started:
            return []
        req_id = None if admission is None else admission.req_id
        return _error(_start, ec.INTERNAL_ERROR, "Internal server execution failure", req_id=req_id)
    finally:
        if acquired:
            semaphore.release()


def _load_parts(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
    parse: Callable[[bytes, str], Any],
    *,
    multipart_error: str,
    other_error: str,
) -> tuple[Any, list[bytes] | None]:
    raw_body, err_resp = _read_request_body(environ, settings, start_response)
    if err_resp is not None:
        return None, err_resp
    assert raw_body is not None
    content_type = environ.get("CONTENT_TYPE") or ""
    try:
        parts = parse(raw_body, content_type)
    except ExecuteRequestError as exc:
        if exc.code is not None:
            return None, _error(start_response, exc.code, str(exc))
        err = multipart_error if is_multipart_content_type(content_type) else other_error
        return None, _error(start_response, ec.BAD_REQUEST_BODY, err)
    return parts, None


def _health(start_response: Any) -> list[bytes]:
    return _start_json(start_response, "200 OK", {"status": "healthy", "service": "python-compute", "version": __version__})


def _load_execute(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[Any, list[bytes] | None]:
    return _load_parts(
        environ,
        settings,
        start_response,
        parse_execute_request,
        multipart_error="Invalid multipart execute body",
        other_error="Invalid JSON",
    )


def _load_vision(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[Any, list[bytes] | None]:
    return _load_parts(
        environ,
        settings,
        start_response,
        parse_multipart_vision,
        multipart_error="Invalid multipart vision body",
        other_error="Invalid vision body",
    )


def _handle_execute(environ: dict[str, Any], start_response: Any, settings: ComputeSettings, run_execute: ExecuteFn, admission: _Admission) -> list[bytes]:
    parts = admission.parsed
    assert isinstance(parts, ExecuteRequestParts)

    req_id = parts.req_id

    try:
        code = _validate_source_text(parts.code, limit=settings.max_code_chars, label="code", required=True, max_bytes=settings.max_body_bytes)
        assert code is not None

        if parts.has_session_id:
            raise ExecuteRequestError("session_id must be provided as a URL query parameter (?session_id=...), not in the request body.")

        # Parsed here, inside the 400 mapping, so a bad session_id is a client error.
        session_id = _parse_session_id(environ)

        mode = canonical_execute_mode(parts.mode)
        if mode == "shared" and not session_id:
            raise ExecuteRequestError("mode='shared' requires a 'session_id' URL query parameter (?session_id=...).")
        # session_id is only valid for shared mode. An isolated request
        # with ?session_id= would skip the concurrency semaphore.
        if mode != "shared" and session_id:
            raise ExecuteRequestError("session_id URL query parameter is only permitted with mode='shared'.")

        init_script = _validate_source_text(parts.init_script, limit=settings.max_code_chars, label="init_script", required=False, max_bytes=settings.max_body_bytes)
    except ExecuteRequestError as exc:
        code = exc.code if exc.code is not None else ec.BAD_REQUEST_BODY
        return _error(start_response, code, str(exc), req_id=req_id)

    sid = session_id if mode == "shared" else None

    result_payload = run_execute(code=code, data_json=parts.data_json, session_id=sid, mode=mode, init_script=init_script, req_id=req_id, decode_result=False, deadline=admission.deadline)
    return _send_execution_result(start_response, result_payload, req_id)


def _handle_session_reset(environ: dict[str, Any], start_response: Any, settings: ComputeSettings, run_reset: ResetFn, admission: _Admission) -> list[bytes]:
    req_data = admission.parsed
    assert isinstance(req_data, dict)
    req_id = req_data.get("id")

    if "session_id" in req_data:
        return _error(start_response, ec.BAD_REQUEST_BODY, "session_id must be provided as a URL query parameter (?session_id=...), not in the JSON body.", req_id=req_id)

    try:
        session_id = _parse_session_id(environ)
    except ExecuteRequestError as exc:
        code = exc.code if exc.code is not None else ec.BAD_REQUEST_BODY
        return _error(start_response, code, str(exc), req_id=req_id)

    if not session_id:
        return _error(start_response, ec.BAD_REQUEST_BODY, "Missing 'session_id' URL query parameter (?session_id=...).", req_id=req_id)

    from compute_service.formula_pool import SESSION_RESET_TIMEOUT_SEC

    # Reset leases within the admission deadline, capped at SESSION_RESET_TIMEOUT_SEC.
    # The floor lets a just-expired clock still return.
    timeout_sec = min(SESSION_RESET_TIMEOUT_SEC, max(0.01, admission.deadline.left()))
    result_payload = run_reset(session_id, timeout_sec=timeout_sec)

    if isinstance(result_payload, dict) and result_payload.get("status") == "error":
        # Unmapped reset failures are a server fault (500). Mapped codes
        # (503, 413, 400) stay on the shared table.
        return _send_execution_result(start_response, result_payload, req_id, error_status="500 Internal Server Error")

    ok_resp: dict[str, Any] = {"status": "ok"}
    return _start_json(start_response, "200 OK", _inject_req_id(ok_resp, req_id))


def _handle_vision(environ: dict[str, Any], start_response: Any, settings: ComputeSettings, admission: _Admission) -> list[bytes]:
    parts = admission.parsed
    assert isinstance(parts, VisionRequestParts)

    req_id = parts.req_id
    raw_helper = parts.helper
    # Missing or empty, including numeric 0, selects the default helper.
    helper = str(raw_helper).strip() if raw_helper else ""
    if not helper:
        helper = "extract_text"
    elif helper not in IMPLEMENTED_HELPERS:
        return _error(start_response, ec.INVALID_REQUEST, f"Unknown vision helper: {helper!r}.", req_id=req_id)

    # An empty image part counts as missing.
    image_bytes = parts.image or None
    file_path = parts.file_path
    path_str = file_path if isinstance(file_path, str) and file_path.strip() else None

    if image_bytes and path_str:
        return _error(start_response, ec.INVALID_REQUEST, "Provide image or file_path, not both.", req_id=req_id)

    if not image_bytes and not path_str:
        return _error(start_response, ec.MISSING_IMAGE_SOURCE, "Missing image input: either 'image' (raw binary part) or 'file_path' (server path) is required.", req_id=req_id)

    if path_str and not ocr_path_is_allowed(path_str, settings.ocr_allow_paths):
        return _error(start_response, ec.FILE_PATH_DENIED, "file_path is not under ocr.allow_paths (default deny).", req_id=req_id)

    raw_params = parts.params
    if raw_params is None:
        params: dict[str, Any] = {}
    elif isinstance(raw_params, dict):
        params = raw_params
    else:
        return _error(start_response, ec.INVALID_REQUEST, "params must be an object.", req_id=req_id)

    from compute_service.vision import get_vision_pool

    vision_pool = get_vision_pool(settings)
    result_payload = vision_pool.execute(
        helper=helper,
        image=image_bytes,
        file_path=path_str,
        params=params,
        req_id=req_id,
        allow_paths=settings.ocr_allow_paths,
        deadline=admission.deadline,
    )
    return _send_execution_result(start_response, result_payload, req_id)


def create_wsgi_app(settings: ComputeSettings, *, execute_fn: ExecuteFn | None = None, reset_fn: ResetFn | None = None, semaphore: threading.Semaphore | None = None) -> Callable[[dict[str, Any], Any], list[bytes]]:
    """Build a WSGI app bound to *settings* (and optional test hooks).

    The formula pool is imported on the request path so config/auth startup
    does not pull WriterAgent ``plugin.framework.config``. Each execute and
    reset resolves the current pool.

    Execute (isolated or sticky), session reset and vision share one
    semaphore of ``admission_permits`` (listeners minus two, so ``GET /health``
    always has a listener). Execute and vision wait until the clamped
    ``timeout_ms`` (the pool default when it is omitted); session reset waits
    until ``default_timeout_sec``. The pools queue for their own workers past
    the permit. Header and body reads occupy a listener before the permit.
    ``semaphore`` overrides the gate in tests.
    """
    permits = semaphore if semaphore is not None else threading.Semaphore(admission_permits(settings))

    def _current_formula_pool() -> Any:
        # Resolve the pool on each call. shutdown_formula_pool() can replace
        # it, and a cached bound method would keep talking to the dead pool.
        from compute_service.formula_pool import get_formula_pool

        return get_formula_pool(settings)

    def _get_execute() -> ExecuteFn:
        if execute_fn is not None:
            return execute_fn
        return _current_formula_pool().execute

    def _get_reset() -> ResetFn:
        if reset_fn is not None:
            return reset_fn
        return _current_formula_pool().reset_session

    def _prepare_execute(environ: dict[str, Any], start: Any) -> tuple[_Admission | None, list[bytes] | None]:
        return _admit_body(environ, settings, start, load=_load_execute)

    def _dispatch_execute(environ: dict[str, Any], start: Any, admission: _Admission) -> list[bytes]:
        return _handle_execute(environ, start, settings, _get_execute(), admission)

    def _prepare_reset(environ: dict[str, Any], start: Any) -> tuple[_Admission | None, list[bytes] | None]:
        return _admit_reset(environ, settings, start)

    def _dispatch_reset(environ: dict[str, Any], start: Any, admission: _Admission) -> list[bytes]:
        return _handle_session_reset(environ, start, settings, _get_reset(), admission)

    def _prepare_vision(environ: dict[str, Any], start: Any) -> tuple[_Admission | None, list[bytes] | None]:
        return _admit_vision(environ, settings, start)

    def _dispatch_vision(environ: dict[str, Any], start: Any, admission: _Admission) -> list[bytes]:
        return _handle_vision(environ, start, settings, admission)

    routes: dict[str, _Route] = {
        "/v1/execute": _Route("POST", True, _prepare_execute, _dispatch_execute),
        "/v1/session/reset": _Route("POST", True, _prepare_reset, _dispatch_reset),
        "/v1/vision": _Route("POST", True, _prepare_vision, _dispatch_vision, busy_code=ec.VISION_POOL_BUSY, busy_message="All vision workers are currently busy."),
    }

    def wsgi_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET")

        if path == "/health":
            if method != "GET":
                if _has_declared_body(environ):
                    request_conn(environ).close_connection = True
                body = b"Method Not Allowed"
                start_response(
                    "405 Method Not Allowed",
                    [("Content-Type", "text/plain"), ("Content-Length", str(len(body))), ("Allow", "GET")],
                )
                return [body]
            if _has_declared_body(environ):
                request_conn(environ).close_connection = True
            return _health(start_response)

        route = routes.get(path)
        if route is None:
            if _has_declared_body(environ):
                request_conn(environ).close_connection = True
            start_response("404 Not Found", [("Content-Type", "text/plain"), ("Content-Length", "9")])
            return [b"Not Found"]

        if method != route.method:
            if _has_declared_body(environ):
                request_conn(environ).close_connection = True
            body = b"Method Not Allowed"
            start_response(
                "405 Method Not Allowed",
                [("Content-Type", "text/plain"), ("Content-Length", str(len(body))), ("Allow", route.method)],
            )
            return [body]

        if not route.is_body_route and _has_declared_body(environ):
            request_conn(environ).close_connection = True

        return _gated(environ, start_response, settings, permits, route)

    return wsgi_app


# =============================================================================
# Server Execution & CLI
# =============================================================================


def run_server(settings: ComputeSettings) -> None:
    setup_logging(settings.log_level)
    auth_note = "auth=yes" if settings.auth_required else "auth=no (insecure)"
    log.info("Starting Python Compute Service on %s:%d (%s, workers=%d, ocr_workers=%d)...", settings.host, settings.port, auth_note, settings.workers, settings.ocr_workers)

    from compute_service.formula_pool import get_formula_pool

    formula_pool = get_formula_pool(settings)
    # Shut the pool down on bind failure and on the normal exit. The HTTP
    # drain stays on serve_forever, which is the only path that has a server.
    try:
        check_dependencies(formula_pool)

        if settings.ocr_workers > 0:
            from compute_service.vision import get_vision_pool

            get_vision_pool(settings)

        try:
            server = WSGIDualStackServer(settings.host, settings.port, listener_threads=listener_thread_count(settings.threads))
        except OSError as exc:
            print(f"Failed to bind {settings.host}:{settings.port}: {exc}", file=sys.stderr)
            raise
        server.max_body_bytes = settings.max_body_bytes
        server.set_app(create_wsgi_app(settings))

        shutdown_signals_received = 0

        def _handle_shutdown(signum: int, _frame: Any) -> None:
            nonlocal shutdown_signals_received
            shutdown_signals_received += 1
            # Only SIGINT and SIGTERM are registered, and both are Signals members.
            sig_name = signal.Signals(signum).name
            if shutdown_signals_received > 1:
                log.warning("Received repeated signal %s, aborting immediately...", sig_name)
                os._exit(1)
            log.info("Received signal %s, initiating graceful shutdown...", sig_name)
            # shutdown() waits until serve_forever returns (socketserver.TCPServer.shutdown),
            # so the signal handler must not call it on this thread.
            threading.Thread(target=server.shutdown, daemon=True).start()

        try:
            signal.signal(signal.SIGTERM, _handle_shutdown)
            signal.signal(signal.SIGINT, _handle_shutdown)
        except (ValueError, AttributeError):
            pass

        try:
            server.serve_forever()
        finally:
            log.info("Stopping Python Compute Service...")
            server.close_sockets()
            server.drain_executor(HTTP_DRAIN_SEC)
            server.server_close()
    finally:
        from compute_service.formula_pool import shutdown_formula_pool
        from compute_service.vision import shutdown_vision_pool

        shutdown_formula_pool(permanent=True)
        shutdown_vision_pool(permanent=True)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone Python compute service for Collabora Online =PY()")
    parser.add_argument("--config", dest="config_path", default=None, help="Path to python-compute.json (or set PYTHON_COMPUTE_CONFIG)")
    parser.add_argument("--host", default=None, help="Bind host (overrides config/env)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (overrides config/env)")
    parser.add_argument("--workers", dest="workers", type=int, default=None, help=f"Number of formula worker subprocesses (default: {DEFAULT_SETTINGS.workers})")
    parser.add_argument("--worker-max-tasks", dest="worker_max_tasks", type=int, default=None, help=f"Recycle formula worker process after N tasks (default: {DEFAULT_SETTINGS.worker_max_tasks})")
    parser.add_argument("--ocr-workers", dest="ocr_workers", type=int, default=None, help=f"Dedicated OCR/Vision worker subprocesses (default: {DEFAULT_SETTINGS.ocr_workers}, 0 to disable)")
    parser.add_argument("--ocr-timeout", dest="ocr_timeout_sec", type=int, default=None, help=f"Execution timeout for vision tasks in seconds (default: {DEFAULT_SETTINGS.ocr_timeout_sec})")
    parser.add_argument("--ocr-max-tasks", dest="ocr_max_tasks", type=int, default=None, help=f"Recycle OCR worker process after N tasks (default: {DEFAULT_SETTINGS.ocr_max_tasks})")
    parser.add_argument("--api-key-file", dest="api_key_file", default=None, help="Read Bearer shared secret from this file (preferred over argv secrets)")
    parser.add_argument("--log-level", dest="log_level", default=None, help=f"Logging level: DEBUG, INFO, WARNING, ERROR (default: {DEFAULT_SETTINGS.log_level})")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    overrides = {key: value for key, value in vars(args).items() if key != "config_path"}
    try:
        settings = load_settings(overrides=overrides, config_path=args.config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        run_server(settings)
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
