# WriterAgent - Python Compute Service Server
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lightweight HTTP server for sandboxed Python execution using standard wsgiref."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import math
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
from compute_service.config import DEFAULT_SETTINGS, ComputeSettings, ConfigError, clamp_timeout_sec, load_settings, ocr_path_is_allowed
from compute_service.http_server import (
    _HTTP_DRAIN_SEC,
    _REQUEST_READ_TIMEOUT_SEC,
    _REQUEST_WRITE_TIMEOUT_SEC,
    _accept_clock,
    _request_deadline,
    DeadlineRequestHandler,
    DualStackThreadPoolHTTPServer,
    ListenerBudget,
    WSGIDualStackServer,
    listener_budget,
    listener_thread_count,
    service_listener_threads,
    sticky_listener_slots,
)
from compute_service.json_forward import (
    WIRE_JSON_FORWARD,
    ExecuteRequestError,
    canonical_execute_mode,
    is_multipart_content_type,
    parse_execute_request,
    validate_session_id,
)
from plugin.vision.vision_common import IMPLEMENTED_HELPERS

log = logging.getLogger("compute_service")

__all__ = [
    "DeadlineRequestHandler",
    "DualStackThreadPoolHTTPServer",
    "ExecuteFn",
    "ListenerBudget",
    "ResetFn",
    "WSGIDualStackServer",
    "_HTTP_DRAIN_SEC",
    "_HTTP_STATUS_BY_CODE",
    "_REQUEST_READ_TIMEOUT_SEC",
    "_REQUEST_WRITE_TIMEOUT_SEC",
    "_ROUTE_ALLOW",
    "_accept_clock",
    "_authenticate_or_401",
    "_build_arg_parser",
    "_check_keyless_cors",
    "_drain_body_before_error",
    "_error",
    "_execute_admission_timeout",
    "_gated",
    "_handle_execute",
    "_handle_session_reset",
    "_handle_vision",
    "_infrastructure_status",
    "_inject_req_id",
    "_load_execute_request",
    "_parse_session_id",
    "_payload_status",
    "_read_optional_request_json",
    "_read_request_body",
    "_read_request_json",
    "_reject_chunked",
    "_request_deadline",
    "_run_with_logging_and_deadline",
    "_send_execution_result",
    "_set_write_deadline",
    "_start_json",
    "_start_raw_json",
    "_transfer_encoding_is_chunked",
    "_validate_source_text",
    "_vision_admission_timeout",
    "authenticate_request",
    "check_dependencies",
    "create_wsgi_app",
    "listener_budget",
    "listener_thread_count",
    "main",
    "run_server",
    "service_listener_threads",
    "setup_logging",
    "sticky_listener_slots",
]

def setup_logging(level_name: str = "INFO") -> None:
    """Configure standard logging format and level for the compute service."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", force=True)
    log.setLevel(level)


def check_dependencies(pool: Any) -> None:
    """Verify required dependencies are importable in worker; exit if missing."""
    ok, err = pool.check_dependencies(["numpy", "sympy"])
    if not ok:
        print(
            err or "Error: Required dependencies are not installed in the worker Python environment.\n"
            "Please start the server using './compute_service/start.sh' or activate the correct virtual environment.",
            file=sys.stderr,
        )
        from compute_service.formula_pool import shutdown_formula_pool

        shutdown_formula_pool()
        sys.exit(1)


# The cell never ran for the 503 codes: a proxy can retry. Eval errors and
# EXECUTION_TIMEOUT stay HTTP 200 so the sheet shows the error instead of #N/A.
# WORKER_CRASHED and EMPTY_RESPONSE are 500: the cell may have run (OOM,
# segfault, missing frame). 503 would invite a retry that kills another worker.
# VISION_WORKER_ERROR and a dict-shaped WORKER_EXECUTION_ERROR are the same
# class: the fault is the response body, not a forwarded cell. A formula
# WORKER_EXECUTION_ERROR inside result_json stays 200; see
# _send_execution_result.
# FILE_PATH_DENIED from the worker is the same client rejection as the route's
# pre-check. Leaving it out answered HTTP 200 after the symlink re-check.
# The other allowlist failures are the same class: the client named the path.
# FILE_TOO_LARGE matches the execute 413s (RESULT_TOO_LARGE / PAYLOAD_TOO_LARGE).
_HTTP_STATUS_BY_CODE = {
    "WORKER_POOL_BUSY": "503 Service Unavailable",
    "SERVICE_SHUTDOWN": "503 Service Unavailable",
    "WORKER_SPAWN_FAILED": "503 Service Unavailable",
    "WORKER_PIPE_BROKEN": "503 Service Unavailable",
    "QUEUE_TIMEOUT": "503 Service Unavailable",
    "VISION_POOL_BUSY": "503 Service Unavailable",
    "VISION_UNAVAILABLE": "503 Service Unavailable",
    "WORKER_CRASHED": "500 Internal Server Error",
    "EMPTY_RESPONSE": "500 Internal Server Error",
    "WORKER_EXECUTION_ERROR": "500 Internal Server Error",
    "VISION_WORKER_ERROR": "500 Internal Server Error",
    "INVALID_BASE64": "400 Bad Request",
    "INVALID_IMAGE": "400 Bad Request",
    "MISSING_IMAGE_SOURCE": "400 Bad Request",
    "FILE_PATH_DENIED": "400 Bad Request",
    "INVALID_FILE_PATH": "400 Bad Request",
    "FILE_NOT_FOUND": "400 Bad Request",
    "NOT_A_FILE": "400 Bad Request",
    "FILE_READ_ERROR": "400 Bad Request",
    "INVALID_REQUEST": "400 Bad Request",
    "RESULT_TOO_LARGE": "413 Payload Too Large",
    "PAYLOAD_TOO_LARGE": "413 Payload Too Large",
    "FILE_TOO_LARGE": "413 Payload Too Large",
    # OCR is configured off. Not a transient miss, so no Retry-After.
    "VISION_SERVICE_DISABLED": "501 Not Implemented",
}

# =============================================================================
# Response & Error Helpers
# =============================================================================

def _inject_req_id(body: dict[str, Any], req_id: Any) -> dict[str, Any]:
    """Attach correlation id to response body dict if present."""
    if req_id is not None:
        body["id"] = req_id
    return body


def _start_raw_json(
    start_response: Any,
    status: str,
    body: bytes,
    *,
    extra_headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    """Send already-encoded JSON bytes (worker result_json) without re-dumps."""
    headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def _start_json(
    start_response: Any,
    status: str,
    payload: dict[str, Any],
    *,
    extra_headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    """Serialize and send a JSON payload dict."""
    return _start_raw_json(
        start_response,
        status,
        json.dumps(payload, allow_nan=False).encode("utf-8"),
        extra_headers=extra_headers,
    )


def _error(
    start_response: Any,
    status: str,
    msg: str,
    code: str | None = None,
    req_id: Any = None,
    *,
    extra_headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    """Shared helper to construct and send a JSON error response."""
    payload: dict[str, Any] = {"status": "error", "error": msg}
    if code is not None:
        payload["code"] = code
    if req_id is not None:
        payload["id"] = req_id
    return _start_json(
        start_response,
        status,
        payload,
        extra_headers=extra_headers,
    )


def _payload_status(payload: Any) -> Any:
    """Status field when the worker returned a dict."""
    if isinstance(payload, dict):
        return payload.get("status")
    return None


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
    return _HTTP_STATUS_BY_CODE.get(code)


def _send_execution_result(
    start_response: Any,
    result_payload: Any,
    req_id: Any,
    *,
    error_status: str = "200 OK",
) -> list[bytes]:
    """Send worker result (raw JSON bytes, pool error status, or serialized dict).

    Eval errors stay HTTP 200 so the sheet shows them. Pass *error_status*
    when an unmapped ``status: error`` is a server fault (session reset).
    Mapped codes, including 413, come from ``_HTTP_STATUS_BY_CODE``.
    Every ``_start_json`` path shares one encode guard. Raw ``result_json``
    bytes are already encoded and stay outside it. Non-empty bytes are
    always 200. Empty bytes are ``EMPTY_RESPONSE`` at 500: they used to
    fall through into ``json.dumps`` of a dict that still held the bytes.
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
            # What was wrong: empty bytes are falsy, so this fell through and
            # json.dumps tried to encode a dict that still held those bytes.
            # Why this change: an empty frame is EMPTY_RESPONSE, not a second encode.
            return _error(
                start_response,
                "500 Internal Server Error",
                "Worker returned an empty result.",
                code="EMPTY_RESPONSE",
                req_id=req_id,
            )
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
        err_body: dict[str, Any] = {"status": "error", "error": f"JSON encode failed: {e}"}
        try:
            return _start_json(start_response, "500 Internal Server Error", _inject_req_id(dict(err_body), req_id))
        except (TypeError, ValueError):
            return _start_json(start_response, "500 Internal Server Error", err_body)


_ROUTE_ALLOW = {
    "/health": "GET",
    "/v1/execute": "POST",
    "/v1/session/reset": "POST",
    "/v1/vision": "POST",
}

ExecuteFn = Callable[..., dict[str, Any]]
ResetFn = Callable[..., dict[str, Any]]

# =============================================================================
# Request Parsing & Validation Helpers
# =============================================================================

def _drain_body_before_error(environ: dict[str, Any], max_bytes: int = 1024 * 1024) -> None:
    """Best-effort drain of remaining request body so client does not receive TCP RST."""
    # The body is already buffered. Reading Content-Length again on a
    # keep-alive connection would consume the next request.
    if isinstance(environ.get("compute.raw_body"), (bytes, bytearray)):
        return
    raw_len = environ.get("CONTENT_LENGTH")
    if not raw_len:
        return
    try:
        content_length = int(raw_len)
        if content_length <= 0:
            return
    except (TypeError, ValueError):
        return
    conn = environ.get("compute.connection")
    previous_timeout: float | None = None
    if conn is not None:
        try:
            raw_timeout = conn.gettimeout()
            previous_timeout = float(raw_timeout) if isinstance(raw_timeout, (int, float)) else None
            conn.settimeout(1.0)
        except OSError:
            previous_timeout = None
    try:
        to_drain = min(content_length, max_bytes)
        wsgi_input = environ.get("wsgi.input")
        if wsgi_input is not None:
            wsgi_input.read(to_drain)
    except OSError:
        pass
    finally:
        # The 1s drain budget must not become the write timeout for the error response.
        if conn is not None and previous_timeout is not None:
            try:
                conn.settimeout(previous_timeout)
            except OSError:
                pass


def _set_write_deadline(environ: dict[str, Any]) -> None:
    """Set a bounded socket write deadline once the body is buffered.

    The handler sets a read timeout so a client that accepts and then sends
    nothing cannot hold a worker thread indefinitely. Once the request body
    is buffered, reset that socket timeout to a bounded write deadline
    (_REQUEST_WRITE_TIMEOUT_SEC) so that in-progress execution is not prematurely
    aborted while preparing to stream or complete the calculation.
    """
    conn = environ.get("compute.connection")
    if conn is not None:
        try:
            conn.settimeout(_REQUEST_WRITE_TIMEOUT_SEC)
        except OSError:
            pass


def _validate_source_text(
    raw: Any,
    *,
    limit: int,
    label: str,
    required: bool,
    max_bytes: int | None = None,
) -> str | None:
    """Validate and return text from part, or raise ExecuteRequestError.

    Character length is checked after UTF-8 decode. *max_bytes* rejects a
    part before that decode; the HTTP body cap is the right bound.
    """
    if isinstance(raw, (bytes, bytearray)):
        if max_bytes is not None and len(raw) > max_bytes:
            raise ExecuteRequestError(f"{label} exceeds max body size ({max_bytes} bytes).", code="CODE_TOO_LARGE")
        if len(raw) == 0:
            if required:
                raise ExecuteRequestError(f"Missing '{label}' string parameter.")
            return None
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise ExecuteRequestError(f"Invalid UTF-8 in {label} part.")
        if len(text) > limit:
            raise ExecuteRequestError(f"{label} exceeds max_code_chars ({limit}).", code="CODE_TOO_LARGE")
        return text

    if isinstance(raw, str):
        if raw == "":
            if required:
                raise ExecuteRequestError(f"Missing '{label}' string parameter.")
            return None
        if len(raw) > limit:
            raise ExecuteRequestError(f"{label} exceeds max_code_chars ({limit}).", code="CODE_TOO_LARGE")
        return raw

    if raw is None:
        if required:
            raise ExecuteRequestError(f"Missing '{label}' string parameter.")
        return None

    raise ExecuteRequestError(f"{label} must be text.")


def _transfer_encoding_is_chunked(environ: dict[str, Any]) -> bool:
    """True when the client asked for a chunked body.

    This server is HTTP/1.0 and reads ``Content-Length``. A chunked body is
    not decoded. Reset used to treat a missing length as ``{}``, so a chunked
    ``POST /v1/session/reset`` succeeded and ignored the body.
    """
    raw = environ.get("HTTP_TRANSFER_ENCODING")
    if not isinstance(raw, str):
        return False
    return "chunked" in raw.lower()


def _reject_chunked(environ: dict[str, Any], start_response: Any) -> list[bytes] | None:
    """400 when the body is chunked. ``None`` when the request may continue."""
    if not _transfer_encoding_is_chunked(environ):
        return None
    return _error(start_response, "400 Bad Request", "Chunked transfer encoding is not supported")


def _read_request_body(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[bytes | None, list[bytes] | None]:
    """Read a bounded POST body with total read deadline enforcement. Returns ``(body, None)`` or ``(None, error_body)``."""
    cached = environ.get("compute.raw_body")
    if isinstance(cached, (bytes, bytearray)):
        return bytes(cached), None
    chunked = _reject_chunked(environ, start_response)
    if chunked is not None:
        return None, chunked
    raw_len = environ.get("CONTENT_LENGTH")
    if raw_len is None or raw_len == "":
        return None, _error(start_response, "400 Bad Request", "Missing Content-Length")
    try:
        content_length = int(raw_len)
    except (TypeError, ValueError):
        return None, _error(start_response, "400 Bad Request", "Invalid Content-Length")
    if content_length <= 0:
        return None, _error(start_response, "400 Bad Request", "Invalid Content-Length")
    if content_length > settings.max_body_bytes:
        _drain_body_before_error(environ, max_bytes=64 * 1024)
        return None, _error(start_response, "413 Payload Too Large", "Request body too large")

    wsgi_input = environ.get("wsgi.input")
    if wsgi_input is None:
        return None, _error(start_response, "500 Internal Server Error", "Missing wsgi.input stream")

    conn = environ.get("compute.connection")
    read_deadline = _accept_clock(environ.get("compute.accept_time")) + _REQUEST_READ_TIMEOUT_SEC

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
                _set_write_deadline(environ)
                return None, _error(start_response, "408 Request Timeout", "Request read timeout")
            remaining_time = read_deadline - now
            if conn is not None:
                try:
                    conn.settimeout(max(0.1, remaining_time))
                except Exception:
                    # What was wrong: a failed settimeout was ignored, so the
                    # next read kept the previous socket timeout and a slow
                    # body outlived _REQUEST_READ_TIMEOUT_SEC.
                    # Why this change: if the deadline cannot be armed, stop.
                    log.debug("failed to arm body-read deadline", exc_info=True)
                    _set_write_deadline(environ)
                    return None, _error(start_response, "408 Request Timeout", "Request read timeout")

            to_read = min(buf_size, content_length - bytes_read)
            raw_chunk: Any = read1(to_read) if callable(read1) else wsgi_input.read(to_read)
            if not isinstance(raw_chunk, (bytes, bytearray)) or not raw_chunk:
                break
            chunk = bytes(raw_chunk)
            chunks.append(chunk)
            bytes_read += len(chunk)
    except TimeoutError:
        _set_write_deadline(environ)
        return None, _error(start_response, "408 Request Timeout", "Request read timeout")
    except OSError as e:
        log.warning("Socket read error during body ingress: %s", e)
        _set_write_deadline(environ)
        return None, _error(start_response, "400 Bad Request", "Connection error reading request body")

    _set_write_deadline(environ)
    if bytes_read != content_length:
        return None, _error(start_response, "400 Bad Request", "Request body truncated")

    body = b"".join(chunks)
    # Admission reads the body before the permit wait. The handler must not
    # read the socket again: on a keep-alive connection that would consume
    # the next request.
    environ["compute.raw_body"] = body
    return body, None


def _read_request_json(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[dict[str, Any] | None, list[bytes] | None]:
    """Read request body and parse as top-level JSON dict."""
    cached_json = environ.get("compute.json_object")
    if isinstance(cached_json, dict):
        return cached_json, None
    body, err_resp = _read_request_body(environ, settings, start_response)
    if err_resp is not None:
        return None, err_resp
    assert body is not None

    def _reject_const(token: str) -> None:
        raise ValueError(token)

    try:
        data = json.loads(body.decode("utf-8"), parse_constant=_reject_const)
    except Exception:
        return None, _error(start_response, "400 Bad Request", "Invalid JSON")

    if not isinstance(data, dict):
        return None, _error(start_response, "400 Bad Request", "JSON body must be an object")

    req_id = data.get("id")
    if isinstance(req_id, float) and not math.isfinite(req_id):
        return None, _error(start_response, "400 Bad Request", "Invalid JSON")

    environ["compute.json_object"] = data
    return data, None


def _read_optional_request_json(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[dict[str, Any] | None, list[bytes] | None]:
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


def authenticate_request(environ: dict[str, Any], settings: ComputeSettings) -> tuple[str | None, str | None]:
    """Validate Authorization when an API key is configured.

    Returns ``(principal, error)``. *principal* is ``settings.default_principal``
    on success (today always ``"default"``); *error* is set on failure.
    """
    if not settings.auth_required:
        return settings.default_principal, None

    raw = environ.get("HTTP_AUTHORIZATION")
    if not isinstance(raw, str) or not raw:
        return None, "missing"

    if len(raw) < 7 or raw[:7].lower() != "bearer ":
        return None, "malformed"

    provided = raw[7:]
    expected = settings.api_key
    try:
        provided_bytes = provided.encode("utf-8")
        expected_bytes = expected.encode("utf-8")
    except UnicodeEncodeError:
        return None, "invalid"
    if not hmac.compare_digest(provided_bytes, expected_bytes):
        return None, "invalid"
    return settings.default_principal, None


def _check_keyless_cors(environ: dict[str, Any], settings: ComputeSettings, start_response: Any) -> list[bytes] | None:
    """In keyless mode, reject cross-origin requests and non-loopback hosts to prevent CSRF/SSRF."""
    if settings.auth_required:
        return None

    if environ.get("HTTP_ORIGIN"):
        return _error(
            start_response,
            "403 Forbidden",
            "Cross-origin requests are forbidden in keyless mode.",
            code="CROSS_ORIGIN_REFUSED",
        )

    # What was wrong: a missing Host skipped this check. HTTP/1.0 may omit
    # it, which bypassed the keyless DNS-rebinding defense.
    # Why this change: no Host is not a loopback Host.
    raw_host_value = environ.get("HTTP_HOST")
    if not isinstance(raw_host_value, str) or not raw_host_value.strip():
        return _error(
            start_response,
            "403 Forbidden",
            "Only loopback hosts are allowed in keyless mode.",
            code="CROSS_ORIGIN_REFUSED",
        )
    raw_host = raw_host_value.lower()
    if raw_host.startswith("["):
        host = raw_host.split("]")[0] + "]"
    else:
        host = raw_host.split(":")[0]

    if host not in ("localhost", "127.0.0.1", "[::1]"):
        return _error(
            start_response,
            "403 Forbidden",
            "Only loopback hosts are allowed in keyless mode.",
            code="CROSS_ORIGIN_REFUSED",
        )
    return None


def _authenticate_or_401(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> list[bytes] | None:
    """Authenticate request; return 401 response on failure or None on success."""
    _principal, auth_err = authenticate_request(environ, settings)
    if auth_err is not None:
        _drain_body_before_error(environ)
        return _error(
            start_response,
            "401 Unauthorized",
            "Unauthorized",
            extra_headers=[("WWW-Authenticate", "Bearer")],
        )
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

def _gated(
    environ: dict[str, Any],
    start_response: Any,
    settings: ComputeSettings,
    semaphore: threading.Semaphore | None,
    *,
    admission_timeout_sec: float,
    busy_code: str,
    busy_message: str,
    route_fn: Callable[[Any], list[bytes]],
    prepare: Callable[[Any], tuple[float, list[bytes] | None]] | None = None,
) -> list[bytes]:
    """Execute route_fn guarded by auth, optional concurrency permit, try/finally, and 500 fallback.

    ``route_fn`` receives the tracking ``start_response``. A second call is
    illegal in wsgiref, so a failure after headers are sent is logged and
    returns an empty body instead of another status line.

    ``prepare`` runs after auth and before the permit. It reads the body
    (cached for ``route_fn``) and returns the clamped request timeout.
    An error response from ``prepare`` does not take a permit.
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

    if prepare is not None:
        # What was wrong: prepare ran before the try that maps route_fn
        # failures to JSON INTERNAL_ERROR. A body-read bug escaped as an HTML 500.
        # Why this change: same JSON fallback, still before the permit. The
        # semaphore release below must not run for an error that never acquired.
        try:
            admission_timeout_sec, prep_err = prepare(_start)
        except Exception as e:
            path = environ.get("PATH_INFO", "")
            log.exception("fail %s prepare: %s", path, e)
            if started:
                return []
            return _error(_start, "500 Internal Server Error", "Internal server execution failure", code="INTERNAL_ERROR")
        if prep_err is not None:
            return prep_err

    if semaphore is not None:
        # One clock with the handler and the worker lease. Time spent queued
        # counts against the request; the lease does not start another timeout.
        # Execute and vision pass the clamped timeout_ms from prepare. Reset
        # has no timeout_ms and keeps default_timeout_sec.
        deadline = _request_deadline(environ.get("compute.accept_time"), admission_timeout_sec)
        remaining = deadline - time.monotonic()
        req_id = environ.get("compute.req_id")
        if remaining <= 0:
            _drain_body_before_error(environ)
            return _error(
                _start,
                "503 Service Unavailable",
                "Request deadline expired before execution.",
                code="QUEUE_TIMEOUT",
                req_id=req_id,
                extra_headers=[("Retry-After", "1")],
            )
        # This service runs at 200-400 rps. A worker busy for a few
        # milliseconds is the steady state and must queue. 503 means the
        # pool stayed busy until this request's deadline.
        if not semaphore.acquire(timeout=remaining):
            _drain_body_before_error(environ)
            return _error(
                _start,
                "503 Service Unavailable",
                busy_message,
                code=busy_code,
                req_id=req_id,
                extra_headers=[("Retry-After", "1")],
            )

    try:
        try:
            return route_fn(_start)
        except Exception as e:
            path = environ.get("PATH_INFO", "")
            log.exception("fail %s: %s", path, e)
            if started:
                return []
            return _error(_start, "500 Internal Server Error", "Internal server execution failure", code="INTERNAL_ERROR")
    finally:
        if semaphore is not None:
            semaphore.release()


def _run_with_logging_and_deadline(
    start_response: Any,
    label: str,
    req_id: Any,
    deadline: float,
    start_msg: str,
    action: Callable[[float], list[bytes]],
) -> list[bytes]:
    """Execute request action with deadline check, duration logging, and 500 error boundary."""
    if time.monotonic() >= deadline:
        return _error(
            start_response,
            "503 Service Unavailable",
            "Request deadline expired before execution.",
            code="QUEUE_TIMEOUT",
            req_id=req_id,
            extra_headers=[("Retry-After", "1")],
        )
    log.info("%s", start_msg)
    start_t = time.perf_counter()
    try:
        return action(start_t)
    except Exception as e:
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        log.exception("fail %s id=%r duration=%.2fms: %s", label, req_id, duration_ms, e)
        try:
            return _error(start_response, "500 Internal Server Error", "Internal server execution failure", code="INTERNAL_ERROR", req_id=req_id)
        except AssertionError:
            # What was wrong: action can call start_response and then raise.
            # This helper caught that before _gated's started flag, so a second
            # start_response hit wsgiref ("Headers already set!").
            # Why this change: the status line is already committed. Log it
            # and return an empty body.
            log.exception("error response after headers already started for %s id=%r", label, req_id)
            return []


def _load_execute_request(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[Any, list[bytes] | None]:
    """Read and peel an execute body. A second call returns the cached parts.

    Admission peels ``timeout_ms`` before taking a permit. The handler peels
    again through this cache so a 32 MiB grid is not walked twice and the
    socket is not read twice.
    """
    cached = environ.get("compute.execute_parts")
    if cached is not None:
        return cached, None
    raw_body, err_resp = _read_request_body(environ, settings, start_response)
    if err_resp is not None:
        return None, err_resp
    assert raw_body is not None
    content_type = environ.get("CONTENT_TYPE") or ""
    try:
        parts = parse_execute_request(raw_body, content_type)
    except ExecuteRequestError:
        err = "Invalid multipart execute body" if is_multipart_content_type(content_type) else "Invalid JSON"
        return None, _error(start_response, "400 Bad Request", err)
    environ["compute.execute_parts"] = parts
    return parts, None


def _execute_admission_timeout(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[float, list[bytes] | None]:
    """Clamped execute timeout from the body, or an error response."""
    parts, err_resp = _load_execute_request(environ, settings, start_response)
    if err_resp is not None:
        return 0.0, err_resp
    assert parts is not None
    if parts.req_id is not None:
        environ["compute.req_id"] = parts.req_id
    timeout_sec = clamp_timeout_sec(
        parts.timeout_ms,
        is_ms=True,
        default_timeout_sec=settings.default_timeout_sec,
        max_timeout_sec=settings.max_timeout_sec,
    )
    return float(timeout_sec), None


def _vision_admission_timeout(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[float, list[bytes] | None]:
    """Clamped vision timeout from the body, or an error response."""
    req_data, err_resp = _read_request_json(environ, settings, start_response)
    if err_resp is not None:
        return 0.0, err_resp
    assert req_data is not None
    req_id = req_data.get("id")
    if req_id is not None:
        environ["compute.req_id"] = req_id
    timeout_sec = clamp_timeout_sec(
        req_data.get("timeout_ms"),
        is_ms=True,
        default_timeout_sec=settings.ocr_timeout_sec,
        max_timeout_sec=settings.max_timeout_sec,
    )
    return float(timeout_sec), None


def _handle_execute(
    environ: dict[str, Any],
    start_response: Any,
    settings: ComputeSettings,
    run_execute: ExecuteFn,
) -> list[bytes]:
    parts, err_resp = _load_execute_request(environ, settings, start_response)
    if err_resp is not None:
        return err_resp
    assert parts is not None

    req_id = parts.req_id

    try:
        code = _validate_source_text(parts.code, limit=settings.max_code_chars, label="code", required=True, max_bytes=settings.max_body_bytes)
        assert code is not None

        if parts.has_session_id:
            raise ExecuteRequestError("session_id must be provided as a URL query parameter (?session_id=...), not in the request body.")

        session_id = _parse_session_id(environ)

        mode = canonical_execute_mode(parts.mode)
        if mode == "shared" and not session_id:
            raise ExecuteRequestError("mode='shared' requires a 'session_id' URL query parameter (?session_id=...).")
        # Bugfix: Return HTTP 400 when session_id is given with a non-shared mode (Bug 1).
        # What was wrong: An isolated request carrying ?session_id= bypassed the concurrency semaphore
        # at the WSGI router because has_session evaluated to True.
        # Why this change: Rejecting non-shared requests that specify session_id prevents concurrency bypass.
        if mode != "shared" and session_id:
            raise ExecuteRequestError("session_id URL query parameter is only permitted with mode='shared'.")

        init_script = _validate_source_text(parts.init_script, limit=settings.max_code_chars, label="init_script", required=False, max_bytes=settings.max_body_bytes)
    except ExecuteRequestError as exc:
        return _error(start_response, "400 Bad Request", str(exc), code=exc.code, req_id=req_id)

    timeout_sec = clamp_timeout_sec(parts.timeout_ms, is_ms=True, default_timeout_sec=settings.default_timeout_sec, max_timeout_sec=settings.max_timeout_sec)
    deadline = _request_deadline(environ.get("compute.accept_time"), float(timeout_sec))
    sid = session_id if mode == "shared" else None

    def _execute(start_t: float) -> list[bytes]:
        result_payload = run_execute(
            code=code,
            data_json=parts.data_json,
            session_id=sid,
            timeout_sec=timeout_sec,
            mode=mode,
            init_script=init_script,
            req_id=req_id,
            wire=WIRE_JSON_FORWARD,
            decode_result=False,
            deadline=deadline,
        )
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        status = _payload_status(result_payload)
        log.info("done /v1/execute id=%r status=%r duration=%.2fms", req_id, status, duration_ms)
        return _send_execution_result(start_response, result_payload, req_id)

    return _run_with_logging_and_deadline(
        start_response,
        label="/v1/execute",
        req_id=req_id,
        deadline=deadline,
        start_msg=f"exec /v1/execute id={req_id!r} mode={mode} session={sid!r} code_len={len(code)} timeout={timeout_sec}s",
        action=_execute,
    )


def _handle_session_reset(
    environ: dict[str, Any],
    start_response: Any,
    settings: ComputeSettings,
    run_reset: ResetFn,
) -> list[bytes]:
    req_data, err_resp = _read_optional_request_json(environ, settings, start_response)
    if err_resp is not None:
        return err_resp
    assert req_data is not None

    req_id = req_data.get("id")

    if "session_id" in req_data:
        return _error(start_response, "400 Bad Request", "session_id must be provided as a URL query parameter (?session_id=...), not in the JSON body.", req_id=req_id)

    try:
        session_id = _parse_session_id(environ)
    except ExecuteRequestError as exc:
        return _error(start_response, "400 Bad Request", str(exc), code=exc.code, req_id=req_id)

    if not session_id:
        return _error(start_response, "400 Bad Request", "Missing 'session_id' URL query parameter (?session_id=...).", req_id=req_id)

    deadline = _request_deadline(environ.get("compute.accept_time"), settings.default_timeout_sec)

    def _reset(start_t: float) -> list[bytes]:
        # reset_session defaults to 5s and used to ignore the accept-time
        # deadline. Cap at that default; floor so a just-expired clock still returns.
        remaining = deadline - time.monotonic()
        timeout_sec = min(5.0, max(0.01, remaining))
        result_payload = run_reset(session_id, timeout_sec=timeout_sec)
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        status = _payload_status(result_payload)
        log.info("done /v1/session/reset id=%r session=%r status=%r duration=%.2fms", req_id, session_id, status, duration_ms)

        if isinstance(result_payload, dict) and result_payload.get("status") == "error":
            # Unmapped reset failures are a server fault (500). Mapped codes
            # (503, 413, 400) stay on the shared table.
            return _send_execution_result(
                start_response,
                result_payload,
                req_id,
                error_status="500 Internal Server Error",
            )

        ok_resp: dict[str, Any] = {"status": "ok"}
        return _start_json(start_response, "200 OK", _inject_req_id(ok_resp, req_id))

    return _run_with_logging_and_deadline(
        start_response,
        label="/v1/session/reset",
        req_id=req_id,
        deadline=deadline,
        start_msg=f"reset /v1/session/reset id={req_id!r} session={session_id!r}",
        action=_reset,
    )


def _handle_vision(
    environ: dict[str, Any],
    start_response: Any,
    settings: ComputeSettings,
) -> list[bytes]:
    req_data, err_resp = _read_request_json(environ, settings, start_response)
    if err_resp is not None:
        return err_resp
    assert req_data is not None

    req_id = req_data.get("id")
    raw_helper = req_data.get("helper")
    # Missing or empty, including numeric 0, selects the default helper.
    helper = str(raw_helper).strip() if raw_helper else ""
    if not helper:
        helper = "extract_text"
    elif helper not in IMPLEMENTED_HELPERS:
        return _error(
            start_response,
            "400 Bad Request",
            f"Unknown vision helper: {helper!r}.",
            code="INVALID_REQUEST",
            req_id=req_id,
        )
    # Missing or empty image_b64, including numeric 0, falls through to image.
    image_input = req_data.get("image_b64") or req_data.get("image")
    file_path = req_data.get("file_path")

    image_str = image_input if isinstance(image_input, (str, bytes, bytearray)) and image_input else None
    path_str = file_path if isinstance(file_path, str) and file_path.strip() else None

    if image_str and path_str:
        return _error(start_response, "400 Bad Request", "Provide image_b64 or file_path, not both.", code="INVALID_REQUEST", req_id=req_id)

    if not image_str and not path_str:
        return _error(start_response, "400 Bad Request", "Missing image input: either 'image_b64'/'image' (base64 string buffer) or 'file_path' (server path) is required.", code="MISSING_IMAGE_SOURCE", req_id=req_id)

    if path_str and not ocr_path_is_allowed(path_str, settings.ocr_allow_paths):
        return _error(start_response, "400 Bad Request", "file_path is not under ocr.allow_paths (default deny).", code="FILE_PATH_DENIED", req_id=req_id)

    raw_params = req_data.get("params")
    if raw_params is None:
        params: dict[str, Any] = {}
    elif isinstance(raw_params, dict):
        params = raw_params
    else:
        return _error(start_response, "400 Bad Request", "params must be an object.", code="INVALID_REQUEST", req_id=req_id)
    vision_budget = float(clamp_timeout_sec(req_data.get("timeout_ms"), is_ms=True, default_timeout_sec=settings.ocr_timeout_sec, max_timeout_sec=settings.max_timeout_sec))
    vision_deadline = _request_deadline(environ.get("compute.accept_time"), vision_budget)

    from compute_service.vision_pool import get_vision_pool

    vision_pool = get_vision_pool(settings)

    def _vision(start_t: float) -> list[bytes]:
        result_payload = vision_pool.execute(
            helper=helper,
            image=image_str,
            file_path=path_str,
            params=params,
            timeout_sec=int(vision_budget),
            req_id=req_id,
            allow_paths=settings.ocr_allow_paths,
            deadline=vision_deadline,
        )
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        status = _payload_status(result_payload)
        log.info("done /v1/vision id=%r status=%r duration=%.2fms", req_id, status, duration_ms)
        return _send_execution_result(start_response, result_payload, req_id)

    return _run_with_logging_and_deadline(
        start_response,
        label="/v1/vision",
        req_id=req_id,
        deadline=vision_deadline,
        start_msg=f"vision /v1/vision id={req_id!r} helper={helper!r}",
        action=_vision,
    )


def create_wsgi_app(
    settings: ComputeSettings,
    *,
    execute_fn: ExecuteFn | None = None,
    reset_fn: ResetFn | None = None,
    worker_semaphore: threading.Semaphore | None = None,
    vision_semaphore: threading.Semaphore | None = None,
) -> Callable[[dict[str, Any], Any], list[bytes]]:
    """Build a WSGI app bound to *settings* (and optional test hooks).

    Executor / pool imports are deferred until the first ``/v1/execute`` or
    ``/v1/session/reset`` so config/auth startup does not pull WriterAgent
    ``plugin.framework.config``.

    Isolated ``/v1/execute`` takes a permit sized to ``settings.workers``.
    ``/v1/vision`` takes one sized to the vision pool. Sticky execute
    (``?session_id=``) and ``/v1/session/reset`` take a separate permit sized
    by ``sticky_listener_slots``. Execute and vision wait until the clamped
    ``timeout_ms`` (the pool default when it is omitted). Session reset waits
    until ``default_timeout_sec``. At least two listener threads stay free
    for ``GET /health``. ``worker_semaphore`` and ``vision_semaphore`` override
    those gates in tests.
    """
    run_execute = execute_fn
    run_reset = reset_fn
    if worker_semaphore is None:
        worker_semaphore = threading.Semaphore(settings.workers)
    if vision_semaphore is None:
        vision_semaphore = threading.Semaphore(max(1, settings.ocr_workers))
    sticky_semaphore = threading.Semaphore(sticky_listener_slots(settings))

    def _get_execute() -> ExecuteFn:
        nonlocal run_execute
        if run_execute is None:
            from compute_service.formula_pool import get_formula_pool

            run_execute = get_formula_pool(settings).execute
        return run_execute

    def _get_reset() -> ResetFn:
        nonlocal run_reset
        if run_reset is None:
            from compute_service.formula_pool import get_formula_pool

            run_reset = get_formula_pool(settings).reset_session
        return run_reset

    def wsgi_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET")

        if path == "/health" and method == "GET":
            return _start_json(start_response, "200 OK", {"status": "healthy", "service": "python-compute", "version": __version__})

        if path == "/v1/execute" and method == "POST":
            # Per Bug 5: Sticky requests (?session_id=...) wait per-worker without holding the
            # global isolated permit, avoiding head-of-line stalls on idle workers.
            try:
                has_session = bool(_parse_session_id(environ))
            except ExecuteRequestError:
                has_session = False
            # Sticky waits on one worker. Cap those waits separately so they
            # cannot occupy every listener the way a shared isolated permit did.
            route_sem = sticky_semaphore if has_session else worker_semaphore
            return _gated(
                environ,
                start_response,
                settings,
                route_sem,
                admission_timeout_sec=float(settings.default_timeout_sec),
                busy_code="WORKER_POOL_BUSY",
                busy_message="All compute workers are currently busy.",
                route_fn=lambda start: _handle_execute(environ, start, settings, _get_execute()),
                prepare=lambda start: _execute_admission_timeout(environ, settings, start),
            )

        if path == "/v1/session/reset" and method == "POST":
            # Resets wait on one session. Same listener cap as sticky execute,
            # not the isolated-worker permit.
            return _gated(
                environ,
                start_response,
                settings,
                sticky_semaphore,
                admission_timeout_sec=float(settings.default_timeout_sec),
                busy_code="WORKER_POOL_BUSY",
                busy_message="All compute workers are currently busy.",
                route_fn=lambda start: _handle_session_reset(environ, start, settings, _get_reset()),
            )

        if path == "/v1/vision" and method == "POST":
            return _gated(
                environ,
                start_response,
                settings,
                vision_semaphore,
                admission_timeout_sec=float(settings.ocr_timeout_sec),
                busy_code="VISION_POOL_BUSY",
                busy_message="All vision workers are currently busy.",
                route_fn=lambda start: _handle_vision(environ, start, settings),
                prepare=lambda start: _vision_admission_timeout(environ, settings, start),
            )

        allow = _ROUTE_ALLOW.get(path)
        if allow is not None:
            # A known path with the wrong verb used to be 404, so clients
            # retried the same method. Allow tells them which verb works.
            body = b"Method Not Allowed"
            start_response(
                "405 Method Not Allowed",
                [("Content-Type", "text/plain"), ("Content-Length", str(len(body))), ("Allow", allow)],
            )
            return [body]

        start_response("404 Not Found", [("Content-Type", "text/plain"), ("Content-Length", "9")])
        return [b"Not Found"]

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
    check_dependencies(formula_pool)

    if settings.ocr_workers > 0:
        from compute_service.vision_pool import get_vision_pool

        get_vision_pool(settings)

    try:
        server = WSGIDualStackServer(settings.host, settings.port, listener_threads=service_listener_threads(settings))
    except OSError as exc:
        print(f"Failed to bind {settings.host}:{settings.port}: {exc}", file=sys.stderr)
        raise
    server.set_app(create_wsgi_app(settings))

    shutdown_signals_received = 0

    def _handle_shutdown(signum: int, _frame: Any) -> None:
        nonlocal shutdown_signals_received
        shutdown_signals_received += 1
        try:
            sig_name = signal.Signals(signum).name
        except Exception:
            sig_name = str(signum)
        if shutdown_signals_received > 1:
            log.warning("Received repeated signal %s, aborting immediately...", sig_name)
            os._exit(1)
        log.info("Received signal %s, initiating graceful shutdown...", sig_name)
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
        server.drain_executor(_HTTP_DRAIN_SEC)
        from compute_service.formula_pool import shutdown_formula_pool
        from compute_service.vision_pool import shutdown_vision_pool

        shutdown_formula_pool(permanent=True)
        shutdown_vision_pool(permanent=True)
        server.server_close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone Python compute service for Collabora Online =PY()")
    parser.add_argument("--config", dest="config_path", default=None, help="Path to python-compute.json (or set PYTHON_COMPUTE_CONFIG)")
    parser.add_argument("--host", default=None, help="Bind host (overrides config/env)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (overrides config/env)")
    parser.add_argument("--workers", "--max-workers", dest="workers", type=int, default=None, help=f"Number of formula worker subprocesses (default: {DEFAULT_SETTINGS.workers})")
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
    try:
        settings = load_settings(
            config_path=args.config_path,
            host=args.host,
            port=args.port,
            workers=args.workers,
            worker_max_tasks=args.worker_max_tasks,
            ocr_workers=args.ocr_workers,
            ocr_timeout_sec=args.ocr_timeout_sec,
            ocr_max_tasks=args.ocr_max_tasks,
            api_key_file=args.api_key_file,
            log_level=args.log_level,
        )
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
