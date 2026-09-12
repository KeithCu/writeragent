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

# Ensure repo root is on sys.path to resolve plugin.* / compute_service imports
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service import __version__
from compute_service.config import ComputeSettings, ConfigError, load_settings, ocr_path_is_allowed

log = logging.getLogger("compute_service")

ExecuteFn = Callable[..., dict[str, Any]]


def setup_logging(level_name: str = "INFO") -> None:
    """Configure standard logging format and level for the compute service."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    log.setLevel(level)


def check_dependencies(pool: Any = None) -> None:
    """Verify required dependencies are importable in worker; exit if missing."""
    if pool is None:
        from compute_service.formula_pool import get_formula_pool

        pool = get_formula_pool()
    ok, err = pool.check_dependencies(["numpy", "sympy"])
    if not ok:
        print(
            err
            or "Error: Required dependencies are not installed in the worker Python environment.\n"
            "Please start the server using './compute_service/start.sh' or activate the correct virtual environment.",
            file=sys.stderr,
        )
        from compute_service.formula_pool import shutdown_formula_pool

        shutdown_formula_pool()
        sys.exit(1)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, allow_nan=False).encode("utf-8")


def _start_json(
    start_response: Any,
    status: str,
    payload: dict[str, Any],
    *,
    extra_headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    body = _json_bytes(payload)
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def _read_request_json(
    environ: dict[str, Any],
    settings: ComputeSettings,
    start_response: Any,
) -> tuple[dict[str, Any] | None, list[bytes] | None]:
    """Parse a POST JSON object. Returns ``(payload, None)`` or ``(None, error_body)``."""
    raw_len = environ.get("CONTENT_LENGTH")
    if raw_len is None or raw_len == "":
        return None, _start_json(
            start_response,
            "400 Bad Request",
            {"status": "error", "error": "Missing Content-Length"},
        )
    try:
        content_length = int(raw_len)
    except (TypeError, ValueError):
        return None, _start_json(
            start_response,
            "400 Bad Request",
            {"status": "error", "error": "Invalid Content-Length"},
        )
    if content_length <= 0:
        return None, _start_json(
            start_response,
            "400 Bad Request",
            {"status": "error", "error": "Invalid Content-Length"},
        )
    if content_length > settings.max_body_bytes:
        return None, _start_json(
            start_response,
            "413 Payload Too Large",
            {"status": "error", "error": "Request body too large"},
        )

    try:
        body = environ["wsgi.input"].read(content_length)
    except Exception:
        return None, _start_json(
            start_response,
            "400 Bad Request",
            {"status": "error", "error": "Failed to read request body"},
        )
    # Guard against partial reads (non-wsgiref WSGI servers may return fewer bytes).
    if len(body) != content_length:
        return None, _start_json(
            start_response,
            "400 Bad Request",
            {"status": "error", "error": "Request body truncated"},
        )
    try:
        req_data = json.loads(body.decode("utf-8"))
    except Exception:
        return None, _start_json(
            start_response,
            "400 Bad Request",
            {"status": "error", "error": "Invalid JSON"},
        )

    if not isinstance(req_data, dict):
        return None, _start_json(
            start_response,
            "400 Bad Request",
            {"status": "error", "error": "JSON body must be an object"},
        )
    return req_data, None


def authenticate_request(
    environ: dict[str, Any],
    settings: ComputeSettings,
) -> tuple[str | None, str | None]:
    """Validate Authorization when an API key is configured.

    Returns ``(principal, error)``. *principal* is ``settings.default_principal``
    on success (today always ``\"default\"``); *error* is set on failure.
    """
    if not settings.auth_required:
        return settings.default_principal, None

    raw = environ.get("HTTP_AUTHORIZATION")
    if not isinstance(raw, str) or not raw:
        return None, "missing"

    # Exact ``Bearer <token>`` — single space, case-sensitive scheme per coolwsd.
    prefix = "Bearer "
    if not raw.startswith(prefix):
        return None, "malformed"

    provided = raw[len(prefix) :]
    expected = settings.api_key
    # Use compare_digest alone — the len() pre-check would short-circuit before
    # compare_digest runs, leaking expected key length via timing side-channel.
    if not hmac.compare_digest(provided, expected):
        return None, "invalid"
    return settings.default_principal, None


def create_wsgi_app(
    settings: ComputeSettings,
    *,
    execute_fn: ExecuteFn | None = None,
) -> Callable[[dict[str, Any], Any], list[bytes]]:
    """Build a WSGI app bound to *settings* (and optional test *execute_fn*).

    Executor imports are deferred until the first ``/v1/execute`` so config/auth
    startup does not pull WriterAgent ``plugin.framework.config``.
    """
    run_execute = execute_fn
    inflight_sema = threading.BoundedSemaphore(settings.max_inflight)
    session_inflight: dict[str, int] = {}
    session_inflight_lock = threading.Lock()

    def _acquire_inflight(session_id: str | None) -> str | None:
        """Return an error code if rejected, else None. Caller must _release_inflight."""
        if not inflight_sema.acquire(blocking=False):
            return "INFLIGHT_LIMIT"
        if session_id:
            with session_inflight_lock:
                used = session_inflight.get(session_id, 0)
                if used >= settings.max_inflight_per_session:
                    inflight_sema.release()
                    return "SESSION_INFLIGHT_LIMIT"
                session_inflight[session_id] = used + 1
        return None

    def _release_inflight(session_id: str | None) -> None:
        if session_id:
            with session_inflight_lock:
                used = session_inflight.get(session_id, 0)
                if used <= 1:
                    session_inflight.pop(session_id, None)
                else:
                    session_inflight[session_id] = used - 1
        inflight_sema.release()

    def wsgi_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        nonlocal run_execute
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET")

        if path == "/health" and method == "GET":
            return _start_json(
                start_response,
                "200 OK",
                {"status": "healthy", "service": "python-compute", "version": __version__},
            )

        if path == "/v1/execute" and method == "POST":
            _principal, auth_err = authenticate_request(environ, settings)
            if auth_err is not None:
                # Generic body — do not reveal whether the key was missing vs wrong.
                return _start_json(
                    start_response,
                    "401 Unauthorized",
                    {"status": "error", "error": "Unauthorized"},
                    extra_headers=[("WWW-Authenticate", "Bearer")],
                )

            req_data, err_resp = _read_request_json(environ, settings, start_response)
            if err_resp is not None:
                return err_resp
            assert req_data is not None

            req_id = req_data.get("id")

            code = req_data.get("code")
            if not code or not isinstance(code, str):
                err_body: dict[str, Any] = {"status": "error", "error": "Missing 'code' string parameter."}
                if req_id is not None:
                    err_body["id"] = req_id
                return _start_json(
                    start_response,
                    "400 Bad Request",
                    err_body,
                )
            if len(code) > settings.max_code_chars:
                err_body = {
                    "status": "error",
                    "code": "CODE_TOO_LARGE",
                    "error": f"code exceeds max_code_chars ({settings.max_code_chars}).",
                }
                if req_id is not None:
                    err_body["id"] = req_id
                return _start_json(start_response, "400 Bad Request", err_body)

            if "session_id" in req_data:
                err_body = {
                    "status": "error",
                    "error": "session_id must be provided as a URL query parameter (?session_id=...), not in the JSON body.",
                }
                if req_id is not None:
                    err_body["id"] = req_id
                return _start_json(start_response, "400 Bad Request", err_body)

            query_string = environ.get("QUERY_STRING", "")
            query_params = urllib.parse.parse_qs(query_string, keep_blank_values=False)
            session_ids = query_params.get("session_id")
            session_id = session_ids[0].strip() if session_ids and session_ids[0].strip() else None

            data = req_data.get("data")
            mode = req_data.get("mode") or "isolated"
            if mode not in ("isolated", "shared"):
                mode = "isolated"

            if mode == "shared" and not session_id:
                err_body = {
                    "status": "error",
                    "error": "mode='shared' requires a 'session_id' URL query parameter (?session_id=...).",
                }
                if req_id is not None:
                    err_body["id"] = req_id
                return _start_json(start_response, "400 Bad Request", err_body)

            init_script = req_data.get("init_script")
            if init_script is not None and not isinstance(init_script, str):
                init_script = None

            # Lazy: auth/config layer stays free of plugin.framework.config.
            from compute_service.executor import timeout_ms_to_sec

            if run_execute is None:
                from compute_service.formula_pool import get_formula_pool

                formula_pool = get_formula_pool(settings)
                run_execute = lambda **kw: formula_pool.execute(**kw)

            timeout_sec = timeout_ms_to_sec(
                req_data.get("timeout_ms"),
                default_timeout_sec=settings.default_timeout_sec,
                max_timeout_sec=settings.max_timeout_sec,
            )
            sid = session_id
            inflight_sid = sid if mode == "shared" else None
            limit_code = _acquire_inflight(inflight_sid)
            if limit_code is not None:
                err_body = {
                    "status": "error",
                    "code": limit_code,
                    "error": "Too many in-flight compute requests.",
                }
                if req_id is not None:
                    err_body["id"] = req_id
                return _start_json(start_response, "503 Service Unavailable", err_body)

            log.info(
                "exec /v1/execute id=%r mode=%s session=%r code_len=%d timeout=%ds",
                req_id,
                mode,
                sid,
                len(code),
                timeout_sec,
            )

            start_t = time.perf_counter()
            try:
                result_payload = run_execute(
                    code=code,
                    data=data,
                    session_id=sid,
                    timeout_sec=timeout_sec,
                    mode=mode,
                    init_script=init_script,
                    req_id=req_id,
                )
                duration_ms = (time.perf_counter() - start_t) * 1000.0
                status = result_payload.get("status") if isinstance(result_payload, dict) else None
                log.info(
                    "done /v1/execute id=%r status=%r duration=%.2fms",
                    req_id,
                    status,
                    duration_ms,
                )

                if req_id is not None and isinstance(result_payload, dict):
                    result_payload["id"] = req_id

                try:
                    return _start_json(start_response, "200 OK", result_payload)
                except (TypeError, ValueError) as e:
                    err_body = {"status": "error", "error": f"JSON encode failed: {e}"}
                    if req_id is not None:
                        err_body["id"] = req_id
                    return _start_json(
                        start_response,
                        "500 Internal Server Error",
                        err_body,
                    )
            except Exception as e:
                duration_ms = (time.perf_counter() - start_t) * 1000.0
                log.exception("fail /v1/execute id=%r duration=%.2fms: %s", req_id, duration_ms, e)
                err_body = {"status": "error", "error": f"Server execution failure: {e}"}
                if req_id is not None:
                    err_body["id"] = req_id
                return _start_json(
                    start_response,
                    "500 Internal Server Error",
                    err_body,
                )
            finally:
                _release_inflight(inflight_sid)

        if path == "/v1/vision" and method == "POST":
            _principal, auth_err = authenticate_request(environ, settings)
            if auth_err is not None:
                return _start_json(
                    start_response,
                    "401 Unauthorized",
                    {"status": "error", "error": "Unauthorized"},
                    extra_headers=[("WWW-Authenticate", "Bearer")],
                )

            req_data, err_resp = _read_request_json(environ, settings, start_response)
            if err_resp is not None:
                return err_resp
            assert req_data is not None

            req_id = req_data.get("id")
            helper = str(req_data.get("helper") or "extract_text").strip()
            image_b64 = req_data.get("image_b64") or req_data.get("image")
            file_path = req_data.get("file_path")

            b64_str = image_b64 if isinstance(image_b64, str) and image_b64.strip() else None
            path_str = file_path if isinstance(file_path, str) and file_path.strip() else None

            if not b64_str and not path_str:
                vision_err_body: dict[str, Any] = {
                    "status": "error",
                    "error": "Missing image input: either 'image_b64' (base64 string buffer) or 'file_path' (server path) is required.",
                }
                if req_id is not None:
                    vision_err_body["id"] = req_id
                return _start_json(start_response, "400 Bad Request", vision_err_body)

            if path_str and not ocr_path_is_allowed(path_str, settings.ocr_allow_paths):
                denied: dict[str, Any] = {
                    "status": "error",
                    "code": "FILE_PATH_DENIED",
                    "error": "file_path is not under ocr.allow_paths (default deny).",
                }
                if req_id is not None:
                    denied["id"] = req_id
                return _start_json(start_response, "400 Bad Request", denied)

            params = req_data.get("params") or {}
            timeout_ms = req_data.get("timeout_ms")
            timeout_sec_opt: int | None = None
            if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
                timeout_sec_opt = max(1, min(settings.max_timeout_sec, (int(timeout_ms) + 999) // 1000))

            from compute_service.vision_pool import get_vision_pool

            vision_pool = get_vision_pool(settings)
            try:
                result_payload = vision_pool.execute(
                    helper=helper,
                    image_b64=b64_str,
                    file_path=path_str,
                    params=params if isinstance(params, dict) else {},
                    timeout_sec=timeout_sec_opt,
                    req_id=req_id,
                    allow_paths=settings.ocr_allow_paths,
                )
                if req_id is not None and isinstance(result_payload, dict):
                    result_payload["id"] = req_id
                try:
                    return _start_json(start_response, "200 OK", result_payload)
                except (TypeError, ValueError) as e:
                    err_body = {"status": "error", "error": f"JSON encode failed: {e}"}
                    if req_id is not None:
                        err_body["id"] = req_id
                    return _start_json(
                        start_response,
                        "500 Internal Server Error",
                        err_body,
                    )
            except Exception as e:
                log.exception("fail /v1/vision id=%r: %s", req_id, e)
                err_body = {"status": "error", "error": f"Server execution failure: {e}"}
                if req_id is not None:
                    err_body["id"] = req_id
                return _start_json(
                    start_response,
                    "500 Internal Server Error",
                    err_body,
                )

        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"Not Found"]

    return wsgi_app


# TEST-ONLY back-compat alias — no auth configured, keyless loopback defaults.
# Do NOT use this in production; call create_wsgi_app(settings) with real settings instead.
wsgi_app = create_wsgi_app(ComputeSettings())


class DualStackThreadPoolHTTPServer(HTTPServer):
    """HTTPServer that listens on both IPv4 and IPv6 loopback (or a single host) using a ThreadPoolExecutor."""

    request_queue_size = 128

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: Any,
        bind_and_activate: bool = True,
        max_threads: int | None = None,
    ) -> None:
        self.sockets: list[socket.socket] = []
        # Own shutdown state: BaseServer uses name-mangled ``__is_shut_down`` / ``__shutdown_request``
        # that type checkers cannot see; our multi-socket ``serve_forever`` must pair with ``shutdown``.
        self._dual_is_shut_down = threading.Event()
        self._dual_shutdown_request = False
        self.executor = ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="compute-worker")
        super().__init__(server_address, RequestHandlerClass, bind_and_activate=False)

        host, port = server_address

        bind_addresses: list[tuple[socket.AddressFamily, str]] = []
        if host in ("", "127.0.0.1", "::1", "localhost"):
            # Secure default: bind only to local loopback interface.
            bind_addresses = [
                (socket.AF_INET, "127.0.0.1"),
                (socket.AF_INET6, "::1"),
            ]
        elif host in ("0.0.0.0", "::"):
            # Wildcard binds (e.g. for Docker/container networking) allowed only when explicitly requested via HOST env.
            bind_addresses = [
                (socket.AF_INET, "0.0.0.0"),
                (socket.AF_INET6, "::"),
            ]
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
                self.sockets.append(sock)
            except OSError as e:
                print(f"Warning: Failed to bind to {ip}:{port} ({family}): {e}", file=sys.stderr)

        if not self.sockets:
            raise OSError(f"Could not bind to any address for {host}:{port}")

        self.socket = self.sockets[0]
        self.address_family = self.socket.family
        actual_port = self.socket.getsockname()[1]
        self.server_address = (host, actual_port)

        if bind_and_activate:
            try:
                self.server_activate()
            except Exception:
                self.server_close()
                raise

    def server_activate(self) -> None:
        for sock in self.sockets:
            sock.listen(self.request_queue_size)

    def server_close(self) -> None:
        for sock in self.sockets:
            try:
                sock.close()
            except Exception:
                pass
        self.executor.shutdown(wait=False, cancel_futures=True)

    def fileno(self) -> int:
        return self.socket.fileno()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self._dual_is_shut_down.clear()
        try:
            with selectors.DefaultSelector() as selector:
                for sock in self.sockets:
                    selector.register(sock, selectors.EVENT_READ)

                while not self._dual_shutdown_request:
                    ready = selector.select(poll_interval)
                    if self._dual_shutdown_request:
                        break
                    if ready:
                        for key, _unused in ready:
                            ready_sock = key.fileobj
                            if isinstance(ready_sock, socket.socket):
                                self._handle_request_noblock_for_socket(ready_sock)
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
        self.executor.submit(self.process_request_thread, request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        """Process incoming request inside a pooled worker thread."""
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def _handle_request_noblock_for_socket(self, sock: socket.socket) -> None:
        try:
            request, client_address = sock.accept()
        except OSError:
            return
        if self.verify_request(request, client_address):
            try:
                self.process_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
                self.shutdown_request(request)
            except:  # noqa: E722 — match stdlib BaseServer
                self.shutdown_request(request)
                raise
        else:
            self.shutdown_request(request)


# Backwards-compatibility alias
DualStackThreadingHTTPServer = DualStackThreadPoolHTTPServer


class WSGIDualStackServer:
    """Wrapper that mixes DualStackThreadPoolHTTPServer with wsgiref.simple_server.WSGIServer."""

    def __init__(self, host: str, port: int, max_threads: int | None = None) -> None:
        from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

        class _WSGIDualStackServer(DualStackThreadPoolHTTPServer, WSGIServer):
            def __init__(
                self,
                server_address: tuple[str, int],
                RequestHandlerClass: Any,
                bind_and_activate: bool = True,
            ) -> None:
                DualStackThreadPoolHTTPServer.__init__(
                    self,
                    server_address,
                    RequestHandlerClass,
                    bind_and_activate,
                    max_threads=max_threads,
                )
                self.server_name = socket.getfqdn(str(self.server_address[0]))
                self.server_port = self.server_address[1]
                self.setup_environ()

        self.srv = _WSGIDualStackServer((host, port), WSGIRequestHandler)

    def set_app(self, app: Any) -> None:
        self.srv.set_app(app)

    def serve_forever(self) -> None:
        self.srv.serve_forever()

    def shutdown(self) -> None:
        self.srv.shutdown()

    def server_close(self) -> None:
        self.srv.server_close()


def run_server(settings: ComputeSettings) -> None:
    setup_logging(settings.log_level)
    auth_note = "auth=yes" if settings.auth_required else "auth=no (insecure)"
    log.info(
        "Starting Python Compute Service on %s:%d (%s, workers=%d, ocr_workers=%d)...",
        settings.host,
        settings.port,
        auth_note,
        settings.workers,
        settings.ocr_workers,
    )
    # Initialize Cython accelerator and log status on startup
    from plugin.scripting.payload_codec import get_cython_status_info, load_cython_accelerator

    load_cython_accelerator()
    _cy_active, _cy_loc, cy_status = get_cython_status_info()
    log.info("%s", cy_status)

    # Warm up formula worker pool and verify dependencies in worker environment
    from compute_service.formula_pool import get_formula_pool

    formula_pool = get_formula_pool(settings)
    check_dependencies(formula_pool)

    if settings.ocr_workers > 0:
        from compute_service.vision_pool import get_vision_pool

        get_vision_pool(settings)

    server = WSGIDualStackServer(settings.host, settings.port, max_threads=settings.threads)
    server.set_app(create_wsgi_app(settings))

    def _handle_shutdown(signum: int, _frame: Any) -> None:
        try:
            sig_name = signal.Signals(signum).name
        except Exception:
            sig_name = str(signum)
        log.info("Received signal %s, initiating graceful shutdown...", sig_name)
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)
    except (ValueError, AttributeError):
        # Non-main thread execution or platforms where signal handlers cannot be registered.
        pass

    try:
        server.serve_forever()
    finally:
        log.info("Stopping Python Compute Service...")
        from compute_service.formula_pool import shutdown_formula_pool
        from compute_service.vision_pool import shutdown_vision_pool

        shutdown_formula_pool()
        shutdown_vision_pool()
        server.server_close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Standalone Python compute service for Collabora Online =PY()",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Path to python-compute.json (or set PYTHON_COMPUTE_CONFIG)",
    )
    parser.add_argument("--host", default=None, help="Bind host (overrides config/env)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (overrides config/env)")
    parser.add_argument(
        "--threads",
        "--max-threads",
        dest="threads",
        type=int,
        default=None,
        help="Number of HTTP server listener threads (default: 2)",
    )
    parser.add_argument(
        "--workers",
        "--max-workers",
        dest="workers",
        type=int,
        default=None,
        help="Number of formula worker subprocesses (default: 1)",
    )
    parser.add_argument(
        "--worker-max-tasks",
        dest="worker_max_tasks",
        type=int,
        default=None,
        help="Recycle formula worker process after N tasks (default: 500)",
    )
    parser.add_argument(
        "--ocr-workers",
        dest="ocr_workers",
        type=int,
        default=None,
        help="Dedicated OCR/Vision worker subprocesses (default: 0, 0 to disable)",
    )
    parser.add_argument(
        "--ocr-timeout",
        dest="ocr_timeout_sec",
        type=int,
        default=None,
        help="Execution timeout for vision tasks in seconds (default: 60)",
    )
    parser.add_argument(
        "--ocr-max-tasks",
        dest="ocr_max_tasks",
        type=int,
        default=None,
        help="Recycle OCR worker process after N tasks (default: 100)",
    )
    parser.add_argument(
        "--api-key-file",
        dest="api_key_file",
        default=None,
        help="Read Bearer shared secret from this file (preferred over argv secrets)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(
            config_path=args.config_path,
            host=args.host,
            port=args.port,
            threads=args.threads,
            workers=args.workers,
            worker_max_tasks=args.worker_max_tasks,
            ocr_workers=args.ocr_workers,
            ocr_timeout_sec=args.ocr_timeout_sec,
            ocr_max_tasks=args.ocr_max_tasks,
            api_key_file=args.api_key_file,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    run_server(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
