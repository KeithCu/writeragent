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

import importlib
import json
import os
import sys
from typing import Any

# Before any plugin import. writeragent_api treats a missing
# WRITERAGENT_IS_WORKER as the LibreOffice host and calls execute_tool
# → get_ctx(). This process has no office and no tool-call pipe.
# WRITERAGENT_COMPUTE_WORKER makes that call fail before either path.
os.environ["WRITERAGENT_IS_WORKER"] = "1"
os.environ["WRITERAGENT_COMPUTE_WORKER"] = "1"

# Ensure repo root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES, ExecuteRequestError, canonical_execute_mode, dumps_response, require_execute_wire, validate_execute_response
from compute_service.worker_base import run_worker_stdio_loop

# execute_code pulls in the sandbox. Import it on the first real request so
# run_worker_stdio_loop can write {"status": "ready"} before that graph loads.
# A cold import used to consume the 15s handshake with an empty stderr.

# Do not load the Cython accelerator here. The compute payload is JSON-forward
# (worker json.loads data_json / dumps result_json once). There is no
# split_grid field on this pipe. Cython flatten stays on the LibrePy host.


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
    # Same mode rule as the HTTP handler. Missing is isolated. false / 0 / a
    # typo used to be rewritten on one path and rejected on the other.
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
        from compute_service.executor import execute_code

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

    The stdio dict used to also carry a ``data`` object for the pickle /
    split_grid wire. Reading it here ran a payload the JSON path had not
    accepted. Absent ``data_json`` is no data, not a second wire.
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
    return run_worker_stdio_loop(_handle_request, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)


if __name__ == "__main__":
    raise SystemExit(main())
