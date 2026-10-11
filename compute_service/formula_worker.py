#!/usr/bin/env python3
# WriterAgent - Python Compute Service Formula Worker
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Standalone worker subprocess for formula and general sandboxed Python execution.

Runs in an isolated process to isolate memory, GIL, and allow hard SIGKILL
termination on hangs/timeouts without affecting the master HTTP server.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from typing import Any


# Ensure repo root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.config import DEFAULT_SETTINGS, clamp_timeout_sec
from compute_service.json_egress import normalize_execute_response
from compute_service.json_forward import ExecuteRequestError, canonical_execute_mode, dumps_response, require_execute_wire, validate_execute_response, validate_session_id
from compute_service.worker_base import run_compute_worker

# execute_code pulls in the sandbox lazily on the first execution so
# the ready handshake is not spent on that graph with an empty stderr.

# Do not load the Cython accelerator here. The compute payload is JSON-forward
# (worker json.loads data_json / dumps result_json once). There is no
# split_grid field on this pipe. Cython flatten stays on the LibrePy host.


def execute_code(code: str, data: Any = None, session_id: str | None = None, timeout_sec: int | None = None, *, mode: str = "isolated", init_script: str | None = None, default_timeout_sec: int = DEFAULT_SETTINGS.default_timeout_sec, max_timeout_sec: int | None = None) -> dict[str, Any]:
    """Execute *code* under AST sandboxing; return a JSON object of status, result, stdout, and error.

    The host already clamps request timeouts to configured bounds (e.g. 1800s);
    the worker does not impose a second 600s clamp when max_timeout_sec is None.
    """
    # Lazy import to avoid loading the heavy sandbox graph at module load time.
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    # Always pass an explicit timeout so the sandbox never consults WriterAgent defaults.
    timeout_sec = clamp_timeout_sec(timeout_sec, default_timeout_sec=default_timeout_sec, max_timeout_sec=max_timeout_sec)

    # Shared kernel only when explicitly requested *and* a session id is provided.
    use_session: str | None = None
    if mode == "shared" and isinstance(session_id, str) and session_id.strip():
        use_session = validate_session_id(session_id)

    # Stable init_session_id so run_sandboxed_code runs init once per worker and
    # seeds later cells from that namespace (hash change replaces the snapshot).
    init_sid: str | None = None
    init_code = init_script if isinstance(init_script, str) and init_script.strip() else None
    init_hash: str | None = None
    if init_code is not None:
        init_hash = hashlib.sha256(init_code.encode("utf-8")).hexdigest()
        if use_session is not None:
            init_sid = f"{use_session}:init"
        else:
            init_sid = f"isolated:{init_hash}:init"

    raw = run_sandboxed_code(code=code, data=data, session_id=use_session, timeout_sec=timeout_sec, init_script=init_code, init_session_id=init_sid, init_script_hash=init_hash)

    return normalize_execute_response(raw)


def _handle_request(req: dict[str, Any]) -> dict[str, Any]:
    req_id = req.get("id")
    action = req.get("action")

    if action == "check_dependencies":
        packages = req.get("packages") or ["numpy", "sympy"]
        missing: list[str] = []
        for pkg in packages:
            try:
                importlib.import_module(str(pkg))
            except Exception:
                missing.append(str(pkg))
        if missing:
            return {"id": req_id, "status": "error", "code": "MISSING_DEPENDENCIES", "missing": missing, "error": f"Missing required dependencies in worker environment: {', '.join(missing)}"}
        return {"id": req_id, "status": "ok"}

    if action == "reset_session":
        session_id = req.get("session_id")
        if session_id and isinstance(session_id, str):
            from plugin.scripting.venv.venv_sandbox import reset_sandbox_session

            res = reset_sandbox_session(session_id)
            if req_id is not None and isinstance(res, dict):
                res["id"] = req_id
            return res
        return {"id": req_id, "status": "ok"}

    session_reset = bool(req.get("session_reset"))
    code = req.get("code")
    if not code or not isinstance(code, str):
        err = {"id": req_id, "status": "error", "code": "MISSING_CODE", "error": "Missing or invalid 'code' parameter", "stdout": ""}
        return _json_forward_envelope(err, req_id=req_id, session_reset=session_reset)

    session_id = req.get("session_id")
    # Same mode rule as the HTTP handler. Missing is isolated; anything else
    # is rejected, so a typo is not rewritten on one path only.
    raw_mode = req.get("mode", None)
    try:
        mode = canonical_execute_mode(raw_mode)
        raw_wire = req.get("wire")
        if raw_wire is not None:
            require_execute_wire(raw_wire)
    except ExecuteRequestError as exc:
        err = {"id": req_id, "status": "error", "code": "INVALID_REQUEST", "error": str(exc), "stdout": ""}
        return _json_forward_envelope(err, req_id=req_id, session_reset=session_reset)
    timeout_sec = req.get("timeout_sec")
    init_script = req.get("init_script")

    try:
        data = _load_request_data(req)
        res = validate_execute_response(execute_code(code=code, data=data, session_id=session_id, timeout_sec=timeout_sec, mode=mode, init_script=init_script))
        if req_id is not None and isinstance(res, dict):
            res["id"] = req_id
        return _json_forward_envelope(res, req_id=req_id, session_reset=session_reset)
    except Exception as exc:
        # Traceback included server paths on the kit wire. Eval errors from
        # json_egress are only status/error/stdout; this path must match.
        err = {"id": req_id, "status": "error", "code": "WORKER_EXECUTION_ERROR", "error": str(exc), "stdout": ""}
        return _json_forward_envelope(err, req_id=req_id, session_reset=session_reset)


def _load_request_data(req: dict[str, Any]) -> Any:
    """One deserialize of the data blob on the worker (never on the HTTP host).

    The stdio dict carries ``data_json`` only. A ``data`` object is not a
    second wire: absent ``data_json`` means no data.
    """
    raw = req.get("data_json")
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(bytes(raw).decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid data_json: {exc}") from exc
    raise ValueError("data_json must be bytes")


def _json_forward_envelope(res: dict[str, Any], *, req_id: Any, session_reset: bool = False) -> dict[str, Any]:
    """Pickle envelope: small status for host logs + result_json bytes to forward."""
    if session_reset:
        res["session_reset"] = True
    try:
        result_json = dumps_response(res)
    except (TypeError, ValueError) as exc:
        fallback: dict[str, Any] = {"status": "error", "error": f"JSON encode failed: {exc}"}
        if req_id is not None:
            fallback["id"] = req_id
        if session_reset:
            fallback["session_reset"] = True
        result_json = dumps_response(fallback)
        out: dict[str, Any] = {"id": req_id, "status": "error", "result_json": result_json}
        if session_reset:
            out["session_reset"] = True
        return out
    out = {"id": req_id, "status": res.get("status"), "result_json": result_json}
    if session_reset:
        out["session_reset"] = True
    return out


def main() -> int:
    return run_compute_worker(_handle_request)


if __name__ == "__main__":
    raise SystemExit(main())
