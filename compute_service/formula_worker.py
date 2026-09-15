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
from compute_service.worker_base import run_worker_stdio_loop
from plugin.scripting.payload_codec import get_cython_status_info, load_cython_accelerator

# Large nested list-grid egress uses child_pack_result → host_pack_split_grid
# → Cython flatten when loaded. Ndarray egress stays child_pack_split_grid
# (NumPy tobytes). Ingress unpack is frombuffer. Load here so list-grid pack
# is not silent pure-Python. The HTTP host probes one child once for status;
# do not log Active/Inactive from every worker.
load_cython_accelerator()


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

    if action == "cython_status":
        cy_line = get_cython_status_info()[2]
        return {"id": req_id, "status": "ok", "cython_status": cy_line}

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

    data = req.get("data")
    session_id = req.get("session_id")
    mode = req.get("mode") or "isolated"
    timeout_sec = req.get("timeout_sec")
    init_script = req.get("init_script")

    try:
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
        return res
    except Exception as exc:
        return {
            "id": req_id,
            "status": "error",
            "code": "WORKER_EXECUTION_ERROR",
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def main() -> int:
    return run_worker_stdio_loop(_handle_request)


if __name__ == "__main__":
    raise SystemExit(main())
