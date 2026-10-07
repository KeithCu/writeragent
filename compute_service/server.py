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
import selectors
import signal
import socket
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer
from typing import Any, Callable
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

# Ensure repo root is on sys.path to resolve plugin.* / compute_service imports
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service import __version__
from compute_service.config import (
    DEFAULT_SETTINGS,
    ComputeSettings,
    ConfigError,
    clamp_timeout_sec,
    load_settings,
    ocr_path_is_allowed,
)
from compute_service.json_forward import (
    WIRE_JSON_FORWARD,
    ExecuteRequestError,
    canonical_execute_mode,
    is_multipart_content_type,
    parse_execute_request,
    validate_session_id,
)

log = logging.getLogger("compute_service")

# Bound the post-accept drain. Matches README 30s termination grace.
_HTTP_DRAIN_SEC = 30.0
# Header and body read. Reset to _REQUEST_WRITE_TIMEOUT_SEC once the body is
# buffered so long calculations do not trip it during execution.
_REQUEST_READ_TIMEOUT_SEC = 30.0
# Socket write deadline for sending the response once computation completes.
_REQUEST_WRITE_TIMEOUT_SEC = 30.0
# The cell never ran. A proxy can retry. Eval errors and EXECUTION_TIMEOUT
# stay HTTP 200 so the sheet shows the error instead of #N/A.
# QUEUE_TIMEOUT is the same miss as the handler's pre-check. The pool
# returns it when the accept deadline expires after that check; leaving it
# out of this set answered HTTP 200 for a request that never leased a worker.
# VISION_POOL_BUSY is that miss on /v1/vision (lease wait expired, no
# worker). The route's accept-deadline pre-check is already 503; this code
# was not in the set, so the same miss came back HTTP 200.
_POOL_UNAVAILABLE = frozenset({
    "WORKER_POOL_BUSY",
    "SERVICE_SHUTDOWN",
    "WORKER_CRASHED",
    "WORKER_SPAWN_FAILED",
    "WORKER_PIPE_BROKEN",
    "EMPTY_RESPONSE",
    "QUEUE_TIMEOUT",
    "VISION_POOL_BUSY",
    "VISION_UNAVAILABLE",
})

ExecuteFn = Callable[..., dict[str, Any]]
ResetFn = Callable[..., dict[str, Any]]


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


def _request_deadline(accept_time: Any, timeout_sec: float) -> float:
    """One clock from TCP accept through the worker lease and the child.

    Without an accept timestamp (direct WSGI tests) the clock starts now.
    Queue wait is subtracted from *timeout_sec*; it is not a second 30s timer.
    """
    if isinstance(accept_time, (int, float)) and not isinstance(accept_time, bool):
        return float(accept_time) + float(timeout_sec)
    return time.monotonic() + float(timeout_sec)


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


def _infrastructure_status(payload: dict[str, Any]) -> str | None:
    """Map compute/worker infrastructure failure codes to 503 Service Unavailable."""
    if payload.get("status") == "error":
        code = payload.get("code")
        if code in _POOL_UNAVAILABLE:
            return "503 Service Unavailable"
    return None


def _send_execution_result(start_response: Any, result_payload: Any, req_id: Any) -> list[bytes]:
    """Send worker result (raw JSON bytes, pool error status, or serialized dict)."""
    if isinstance(result_payload, dict):
        raw_out = result_payload.get("result_json")
        if isinstance(raw_out, (bytes, bytearray)) and raw_out:
            return _start_raw_json(start_response, "200 OK", bytes(raw_out))
        # RESULT_TOO_LARGE / PAYLOAD_TOO_LARGE -> 413 Payload Too Large (non-retryable)
        code = result_payload.get("code")
        if code in ("RESULT_TOO_LARGE", "PAYLOAD_TOO_LARGE"):
            _inject_req_id(result_payload, req_id)
            return _start_json(start_response, "413 Payload Too Large", result_payload)
        infra = _infrastructure_status(result_payload)
        _inject_req_id(result_payload, req_id)
        if infra is not None:
            return _start_json(start_response, infra, result_payload)

    try:
        if isinstance(result_payload, dict):
            _inject_req_id(result_payload, req_id)
        return _start_json(start_response, "200 OK", result_payload)
    except (TypeError, ValueError) as e:
        err_body: dict[str, Any] = {"status": "error", "error": f"JSON encode failed: {e}"}
        try:
            return _start_json(start_response, "500 Internal Server Error", _inject_req_id(dict(err_body), req_id))
        except (TypeError, ValueError):
            return _start_json(start_response, "500 Internal Server Error", err_body)


# =============================================================================
# Request Parsing & Validation Helpers
# =============================================================================

def _drain_body_before_error(environ: dict[str, Any], max_bytes: int = 1024 * 1024) -> None:
    """Best-effort drain of remaining request body so client does not receive TCP RST."""
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
    if conn is not None:
        try:
            conn.settimeout(1.0)
        except Exception:
            pass
    try:
        to_drain = min(content_length, max_bytes)
        wsgi_input = environ.get("wsgi.input")
        if wsgi_input is not None:
            wsgi_input.read(to_drain)
    except Exception:
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
        except Exception:
            pass


def _validate_source_text(
    raw: Any,
    *,
    limit: int,
    label: str,
    required: bool,
) -> str | None:
    """Validate and return text from part, or raise ExecuteRequestError."""
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > limit * 4:
            raise ExecuteRequestError(f"{label} exceeds max_code_chars ({limit}).", code="CODE_TOO_LARGE")
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


def _read_request_body(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[bytes | None, list[bytes] | None]:
    """Read a bounded POST body with total read deadline enforcement. Returns ``(body, None)`` or ``(None, error_body)``."""
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
    accept_time = environ.get("compute.accept_time")
    read_deadline = float(accept_time) + _REQUEST_READ_TIMEOUT_SEC if accept_time is not None else time.monotonic() + _REQUEST_READ_TIMEOUT_SEC

    chunks: list[bytes] = []
    bytes_read = 0
    buf_size = 64 * 1024

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
                    pass

            to_read = min(buf_size, content_length - bytes_read)
            chunk = wsgi_input.read(to_read)
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)
    except (TimeoutError, socket.timeout):
        _set_write_deadline(environ)
        return None, _error(start_response, "408 Request Timeout", "Request read timeout")
    except OSError as e:
        log.warning("Socket read error during body ingress: %s", e)
        _set_write_deadline(environ)
        return None, _error(start_response, "400 Bad Request", "Connection error reading request body")

    _set_write_deadline(environ)
    if bytes_read != content_length:
        return None, _error(start_response, "400 Bad Request", "Request body truncated")

    return b"".join(chunks), None


def _read_request_json(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[dict[str, Any] | None, list[bytes] | None]:
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
        return None, _error(start_response, "400 Bad Request", "Invalid JSON")

    if not isinstance(data, dict):
        return None, _error(start_response, "400 Bad Request", "JSON body must be an object")

    req_id = data.get("id")
    if isinstance(req_id, float) and not math.isfinite(req_id):
        return None, _error(start_response, "400 Bad Request", "Invalid JSON")

    return data, None


def _read_optional_request_json(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[dict[str, Any] | None, list[bytes] | None]:
    """Parse an optional POST JSON object. Missing or empty body is ``{}``.

    ``/v1/session/reset`` allows an empty body (correlation ``id`` only).
    ``/v1/execute`` still requires Content-Length > 0 via ``_read_request_body``.
    """
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

    if "HTTP_HOST" in environ:
        raw_host = environ["HTTP_HOST"].lower()
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
    busy_code: str,
    busy_message: str,
    route_fn: Callable[[], list[bytes]],
) -> list[bytes]:
    """Execute route_fn guarded by auth, optional concurrency permit, try/finally, and 500 fallback."""
    auth_resp = _authenticate_or_401(environ, settings, start_response)
    if auth_resp is not None:
        return auth_resp

    if semaphore is not None and not semaphore.acquire(blocking=False):
        _drain_body_before_error(environ)
        return _error(
            start_response,
            "503 Service Unavailable",
            busy_message,
            code=busy_code,
            extra_headers=[("Retry-After", "1")],
        )

    try:
        try:
            return route_fn()
        except Exception as e:
            path = environ.get("PATH_INFO", "")
            log.exception("fail %s: %s", path, e)
            return _error(start_response, "500 Internal Server Error", "Internal server execution failure", code="INTERNAL_ERROR")
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
        return _error(start_response, "500 Internal Server Error", "Internal server execution failure", code="INTERNAL_ERROR", req_id=req_id)


def _handle_execute(
    environ: dict[str, Any],
    start_response: Any,
    settings: ComputeSettings,
    run_execute: ExecuteFn,
) -> list[bytes]:
    raw_body, err_resp = _read_request_body(environ, settings, start_response)
    if err_resp is not None:
        return err_resp
    assert raw_body is not None

    content_type = environ.get("CONTENT_TYPE") or ""
    try:
        parts = parse_execute_request(raw_body, content_type)
    except ExecuteRequestError:
        err = "Invalid multipart execute body" if is_multipart_content_type(content_type) else "Invalid JSON"
        return _error(start_response, "400 Bad Request", err)

    req_id = parts.req_id

    try:
        code = _validate_source_text(parts.code, limit=settings.max_code_chars, label="code", required=True)
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

        init_script = _validate_source_text(parts.init_script, limit=settings.max_code_chars, label="init_script", required=False)
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
        status = result_payload.get("status") if isinstance(result_payload, dict) else None
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
        result_payload = run_reset(session_id)
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        status = result_payload.get("status") if isinstance(result_payload, dict) else None
        log.info("done /v1/session/reset id=%r session=%r status=%r duration=%.2fms", req_id, session_id, status, duration_ms)

        if isinstance(result_payload, dict) and result_payload.get("status") == "error":
            infra = _infrastructure_status(result_payload)
            # Worker reset failure is 500 Internal Server Error (server fault) unless an infra code
            http_status = infra if infra is not None else "500 Internal Server Error"
            _inject_req_id(result_payload, req_id)
            return _start_json(start_response, http_status, result_payload)

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
    helper = str(req_data.get("helper") or "extract_text").strip()
    image_input = req_data.get("image_b64") or req_data.get("image")
    file_path = req_data.get("file_path")

    image_str = image_input if isinstance(image_input, (str, bytes, bytearray)) and image_input else None
    path_str = file_path if isinstance(file_path, str) and file_path.strip() else None

    if not image_str and not path_str:
        return _error(start_response, "400 Bad Request", "Missing image input: either 'image_b64'/'image' (base64 string buffer) or 'file_path' (server path) is required.", req_id=req_id)

    if path_str and not ocr_path_is_allowed(path_str, settings.ocr_allow_paths):
        return _error(start_response, "400 Bad Request", "file_path is not under ocr.allow_paths (default deny).", code="FILE_PATH_DENIED", req_id=req_id)

    params = req_data.get("params") or {}
    vision_budget = float(clamp_timeout_sec(req_data.get("timeout_ms"), is_ms=True, default_timeout_sec=settings.ocr_timeout_sec, max_timeout_sec=settings.max_timeout_sec))
    vision_deadline = _request_deadline(environ.get("compute.accept_time"), vision_budget)

    from compute_service.vision_pool import get_vision_pool

    vision_pool = get_vision_pool(settings)

    def _vision(start_t: float) -> list[bytes]:
        result_payload = vision_pool.execute(
            helper=helper,
            image=image_str,
            file_path=path_str,
            params=params if isinstance(params, dict) else {},
            timeout_sec=int(vision_budget),
            req_id=req_id,
            allow_paths=settings.ocr_allow_paths,
            deadline=vision_deadline,
        )
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        status = result_payload.get("status") if isinstance(result_payload, dict) else None
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

    ``/v1/execute`` and ``/v1/session/reset`` share a non-blocking permit
    count sized to allow sticky session queues without starving idle workers,
    while reserving at least 2 listener threads for immediate ``GET /health``.
    ``worker_semaphore`` and ``vision_semaphore`` override those gates in tests.
    """
    run_execute = execute_fn
    run_reset = reset_fn
    if worker_semaphore is None:
        worker_semaphore = threading.Semaphore(settings.workers)
    if vision_semaphore is None:
        vision_semaphore = threading.Semaphore(max(1, settings.ocr_workers))

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
            route_sem = None if has_session else worker_semaphore
            return _gated(
                environ,
                start_response,
                settings,
                route_sem,
                busy_code="WORKER_POOL_BUSY",
                busy_message="All compute workers are currently busy.",
                route_fn=lambda: _handle_execute(environ, start_response, settings, _get_execute()),
            )

        if path == "/v1/session/reset" and method == "POST":
            # Session resets are per-session; do not hold global isolated permit
            return _gated(
                environ,
                start_response,
                settings,
                None,
                busy_code="WORKER_POOL_BUSY",
                busy_message="All compute workers are currently busy.",
                route_fn=lambda: _handle_session_reset(environ, start_response, settings, _get_reset()),
            )

        if path == "/v1/vision" and method == "POST":
            return _gated(
                environ,
                start_response,
                settings,
                vision_semaphore,
                busy_code="VISION_POOL_BUSY",
                busy_message="All vision workers are currently busy.",
                route_fn=lambda: _handle_vision(environ, start_response, settings),
            )

        start_response("404 Not Found", [("Content-Type", "text/plain"), ("Content-Length", "9")])
        return [b"Not Found"]

    return wsgi_app


# =============================================================================
# HTTP Server Plumbing
# =============================================================================

class DualStackThreadPoolHTTPServer(HTTPServer):
    """HTTPServer that listens on both IPv4 and IPv6 loopback (or a single host) using a ThreadPoolExecutor.

    The thread pool capacity is sized larger than the worker count (at least ``max(8, W + 4)`` threads).
    ``/v1/execute`` and ``/v1/session/reset`` share a non-blocking semaphore sized to the
    formula pool; ``/v1/vision`` has its own sized to the vision pool. Both gates run
    before the request body is read. A miss is 503 Service Unavailable, so a full pool
    does not hold a listener thread and does not consume the other pool's permits.
    At least two listener threads stay available for immediate ``GET /health``.
    """

    request_queue_size: int = 128
    _dual_is_shut_down: threading.Event
    _dual_shutdown_request: bool
    executor: ThreadPoolExecutor
    address_family: int
    server_address: tuple[str | bytes | bytearray, int] | tuple[str | bytes | bytearray, int, int, int]

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: Any,
        bind_and_activate: bool = True,
        max_threads: int | None = None,
    ) -> None:
        self.sockets: list[socket.socket] = []
        self._dual_is_shut_down = threading.Event()
        self._dual_shutdown_request = False
        self._accept_times: dict[int, float] = {}
        self.executor = ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="compute-worker")

        super().__init__(server_address, RequestHandlerClass, bind_and_activate=False)
        inherited = self.socket
        try:
            inherited.close()
        except OSError:
            pass

        host, port = server_address

        bind_addresses: list[tuple[socket.AddressFamily, str]] = []
        if host in ("", "127.0.0.1", "::1", "localhost"):
            bind_addresses = [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]
        elif host in ("0.0.0.0", "::"):
            bind_addresses = [(socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")]
        else:
            try:
                infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
                seen_families = set()
                for family, _unused, _unused2, _unused3, sockaddr in infos:
                    if family not in seen_families:
                        seen_families.add(family)
                        bind_addresses.append((family, str(sockaddr[0])))
            except Exception:
                bind_addresses = [(socket.AF_INET, host)]

        for family, ip in bind_addresses:
            try:
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    try:
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    except OSError:
                        pass
                sock.bind((ip, port))
                if port == 0:
                    port = sock.getsockname()[1]
                self.sockets.append(sock)
            except OSError as e:
                print(f"Warning: Failed to bind to {ip}:{port} ({family}): {e}", file=sys.stderr)

        if not self.sockets:
            raise OSError(f"Could not bind to any address for {host}:{port}")

        self.socket: socket.socket = self.sockets[0]
        self.address_family = self.socket.family
        actual_port = self.socket.getsockname()[1]
        self.server_address = (host, actual_port)

        if bind_and_activate:
            try:
                self.server_activate()
            except Exception:
                self.server_close()
                raise

    def close_sockets(self) -> None:
        """Close listening sockets so new incoming connections are refused immediately."""
        for sock in self.sockets:
            try:
                sock.close()
            except Exception:
                pass
        self.sockets.clear()

    def server_activate(self) -> None:
        for sock in self.sockets:
            sock.listen(self.request_queue_size)

    def server_close(self) -> None:
        self.close_sockets()
        self.executor.shutdown(wait=False, cancel_futures=False)

    def drain_executor(self, timeout: float) -> None:
        """Wait until accepted requests finish, then return."""
        done = threading.Event()

        def _wait() -> None:
            self.executor.shutdown(wait=True, cancel_futures=False)
            done.set()

        threading.Thread(target=_wait, name="http-drain", daemon=True).start()
        if not done.wait(timeout):
            log.warning("HTTP request drain exceeded %.0fs; abandoning in-flight handlers", timeout)

    def fileno(self) -> int:
        return self.socket.fileno()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self._dual_is_shut_down.clear()
        listen = set(self.sockets)
        try:
            with selectors.DefaultSelector() as selector:
                for sock in self.sockets:
                    selector.register(sock, selectors.EVENT_READ)

                while not self._dual_shutdown_request:
                    ready = selector.select(poll_interval)
                    if self._dual_shutdown_request:
                        break
                    for key, _unused in ready:
                        ready_sock = key.fileobj
                        if not isinstance(ready_sock, socket.socket):
                            continue
                        if ready_sock in listen:
                            try:
                                conn, client_address = ready_sock.accept()
                            except OSError as e:
                                log.warning("Accept error: %s; backing off", e)
                                time.sleep(0.05)
                                continue
                            if not self.verify_request(conn, client_address):
                                self.shutdown_request(conn)
                                continue
                            self._accept_times[id(conn)] = time.monotonic()
                            self.process_request(conn, client_address)
                    self.service_actions()
        finally:
            self._dual_shutdown_request = False
            self._dual_is_shut_down.set()

    def shutdown(self) -> None:
        """Stop ``serve_forever`` (must be called from another thread while it is running)."""
        self._dual_shutdown_request = True
        self._dual_is_shut_down.wait()

    def process_request(self, request: Any, client_address: Any) -> None:
        """Submit incoming request to the thread pool executor."""
        try:
            self.executor.submit(self.process_request_thread, request, client_address)
        except Exception:
            try:
                request.close()
            except Exception:
                pass
            raise

    def shutdown_request(self, request: Any) -> None:
        self._accept_times.pop(id(request), None)
        super().shutdown_request(request)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        """Process incoming request inside a pooled worker thread."""
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


class DeadlineRequestHandler(WSGIRequestHandler):
    """WSGI request handler with total header read deadline and compute context logging."""

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.settimeout(_REQUEST_READ_TIMEOUT_SEC)
        except Exception:
            pass

    def handle_one_request(self) -> None:
        """Handle request with a total header-read deadline."""
        accept_time = getattr(self.server, "_accept_times", {}).get(id(self.connection))
        header_deadline = (float(accept_time) if accept_time is not None else time.monotonic()) + _REQUEST_READ_TIMEOUT_SEC
        rfile_raw: Any = getattr(self.rfile, "raw", None)
        orig_readinto = getattr(rfile_raw, "readinto", None) if rfile_raw is not None else None

        def _deadline_readinto(b: Any) -> int:
            now = time.monotonic()
            remaining = header_deadline - now
            if remaining <= 0:
                raise socket.timeout("Header read deadline expired")
            self.connection.settimeout(max(0.01, remaining))
            if callable(orig_readinto):
                res = orig_readinto(b)
                if isinstance(res, int):
                    return res
            return 0

        if rfile_raw is not None and callable(orig_readinto):
            rfile_raw.readinto = _deadline_readinto
        try:
            super().handle_one_request()
        finally:
            if rfile_raw is not None and orig_readinto is not None:
                rfile_raw.readinto = orig_readinto
            try:
                self.connection.settimeout(_REQUEST_READ_TIMEOUT_SEC)
            except Exception:
                pass

    def address_string(self) -> str:
        return str(self.client_address[0])

    def log_message(self, format: str, *args: Any) -> None:
        log.debug(format, *args)

    def get_environ(self) -> dict[str, Any]:
        environ = super().get_environ()
        environ["compute.connection"] = self.connection
        server: Any = self.server
        environ["compute.accept_time"] = server._accept_times.get(id(self.connection), None)
        return environ


class WSGIDualStackServer(DualStackThreadPoolHTTPServer, WSGIServer):
    """Dual-stack thread-pooled WSGI server."""

    server_name: str
    server_port: int
    srv: Any

    def __init__(self, host: str, port: int, max_threads: int | None = None) -> None:
        effective_threads = max(8, (max_threads or 2) + 4) if max_threads is not None else 16
        DualStackThreadPoolHTTPServer.__init__(
            self,
            (host, port),
            DeadlineRequestHandler,
            bind_and_activate=True,
            max_threads=effective_threads,
        )
        raw_host = str(self.server_address[0])
        self.server_name = raw_host if raw_host and raw_host not in ("", "0.0.0.0", "::") else "localhost"
        self.server_port = self.server_address[1]
        self.setup_environ()
        self.srv = self


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
        server = WSGIDualStackServer(settings.host, settings.port, max_threads=settings.threads)
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

        shutdown_formula_pool()
        shutdown_vision_pool()
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
