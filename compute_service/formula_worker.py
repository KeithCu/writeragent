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
import traceback
from typing import Any

# Ensure repo root is on sys.path
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.executor import execute_code
from compute_service.json_forward import (
    COMPUTE_MAX_PAYLOAD_BYTES,
    WIRE_JSON_FORWARD,
    dumps_response,
)
from compute_service.worker_base import run_worker_stdio_loop

# Do not load the Cython accelerator here. Default compute wire is JSON-forward
# (worker json.loads data_json / dumps result_json once). The optional pickle +
# split_grid fallback still unpacks via NumPy frombuffer. Cython flatten is
# host-only (LibrePy / wire="pickle"). Importing payload_codec unpack helpers
# must not load or claim Cython Active.


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
            return {
                "id": req_id,
                "status": "error",
                "code": "MISSING_DEPENDENCIES",
                "missing": missing,
                "error": f"Missing required dependencies in worker environment: {', '.join(missing)}",
            }
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

    code = req.get("code")
    if not code or not isinstance(code, str):
        return {
            "id": req_id,
            "status": "error",
            "code": "MISSING_CODE",
            "error": "Missing or invalid 'code' parameter",
        }

    session_id = req.get("session_id")
    mode = req.get("mode") or "isolated"
    timeout_sec = req.get("timeout_sec")
    init_script = req.get("init_script")
    json_forward = _is_json_forward(req)

    try:
        data = _load_request_data(req)
        res = execute_code(
            code=code,
            data=data,
            session_id=session_id,
            timeout_sec=timeout_sec,
            mode=mode,
            init_script=init_script,
        )
        if req_id is not None and isinstance(res, dict):
            res["id"] = req_id
        if json_forward:
            return _json_forward_envelope(res, req_id=req_id)
        return res
    except Exception as exc:
        err = {
            "id": req_id,
            "status": "error",
            "code": "WORKER_EXECUTION_ERROR",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        if json_forward:
            return _json_forward_envelope(err, req_id=req_id)
        return err


def _is_json_forward(req: dict[str, Any]) -> bool:
    if req.get("wire") == WIRE_JSON_FORWARD:
        return True
    return isinstance(req.get("data_json"), (bytes, bytearray))


def _load_request_data(req: dict[str, Any]) -> Any:
    """One deserialize of the data blob on the worker (never on the HTTP host)."""
    raw = req.get("data_json")
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(bytes(raw).decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid data_json: {exc}") from exc
    return req.get("data")


def _json_forward_envelope(res: dict[str, Any], *, req_id: Any) -> dict[str, Any]:
    """Pickle envelope: small status for host logs + result_json bytes to forward."""
    try:
        result_json = dumps_response(res)
    except (TypeError, ValueError) as exc:
        fallback = {
            "status": "error",
            "error": f"JSON encode failed: {exc}",
        }
        if req_id is not None:
            fallback["id"] = req_id
        result_json = dumps_response(fallback)
        return {
            "id": req_id,
            "status": "error",
            "result_json": result_json,
        }
    return {
        "id": req_id,
        "status": res.get("status"),
        "result_json": result_json,
    }


def main() -> int:
    return run_worker_stdio_loop(_handle_request, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)


if __name__ == "__main__":
    raise SystemExit(main())
