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
import importlib.util
import json
import os
import sys
from typing import Any


# Ensure repo root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.config import DEFAULT_SETTINGS
from compute_service.json_egress import normalize_execute_response
from compute_service.json_forward import json_forward_envelope
from compute_service.worker_base import configure_compute_worker_env, run_compute_worker

# Default math/OpenMP thread pools to 1 thread early before any heavy math imports.
configure_compute_worker_env()

# ast sandboxing is loaded lazily on the first execution so the ready
# handshake is not spent on that graph with an empty stderr.

# Do not load the Cython accelerator here. The compute payload is JSON-forward
# (worker json.loads data_json / dumps result_json once). There is no
# split_grid field on this pipe. Cython flatten stays on the LibrePy host.


def _handle_request(req: dict[str, Any]) -> dict[str, Any]:
    req_id = req.get("id")
    action = req.get("action")

    if action == "check_dependencies":
        packages = req.get("packages") or ["numpy", "sympy", "pandas"]
        missing: list[str] = []
        # find_spec locates a package without importing it. Importing
        # scipy and pandas here cost startup time for a check that
        # only needs to know they are installed. A dotted name imports its
        # parent and raises when that parent is missing, so keep the except.
        for pkg in packages:
            try:
                if importlib.util.find_spec(str(pkg)) is None:
                    missing.append(str(pkg))
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

    code = req.get("code")
    if not code or not isinstance(code, str):
        err = {"id": req_id, "status": "error", "code": "MISSING_CODE", "error": "Missing or invalid 'code' parameter", "stdout": ""}
        return json_forward_envelope(err, req_id=req_id, session_reset=False, namespace_created=False)

    session_id = req.get("session_id")
    # Pool execute() already rejected a bad mode. The stdio dict
    # carries that canonical mode; checking again duplicated the gate.
    mode = req.get("mode", "isolated")
    timeout_sec = req.get("timeout_sec")
    init_script = req.get("init_script")

    try:
        raw_data = req.get("data_json")
        data = None if raw_data is None else json.loads(raw_data)

        # Lazy import to avoid loading the heavy sandbox graph at module load time.
        from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

        eff_timeout = DEFAULT_SETTINGS.default_timeout_sec if timeout_sec is None else timeout_sec

        # Shared kernel only when explicitly requested *and* a session id is provided.
        use_session: str | None = None
        if mode == "shared" and isinstance(session_id, str) and session_id.strip():
            use_session = session_id.strip()

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

        # What was wrong: Large arrays / grids were packed into binary float64 split_grid
        # envelopes by default, which cast ints to float64 and turned ints > 2^53 into text,
        # only for json_egress to immediately decode them back into lists.
        # How it happened: run_sandboxed_code defaulted to force="auto", packing grids
        # >= BINARY_MIN_CELLS into split_grid buffers.
        # Why this change: The compute service output is JSON and never needed the binary
        # buffer across a process boundary; force="never" keeps results as native lists
        # so integers stay exact.
        raw = run_sandboxed_code(
            code=code,
            data=data,
            force="never",
            session_id=use_session,
            timeout_sec=eff_timeout,
            init_script=init_code,
            init_session_id=init_sid,
            init_script_hash=init_hash,
        )

        namespace_created = raw.pop("namespace_created", None)
        res = normalize_execute_response(raw)
        created = bool(namespace_created)
        if req_id is not None and isinstance(res, dict):
            res["id"] = req_id
        session_reset = bool(req.get("expect_namespace")) and created
        return json_forward_envelope(res, req_id=req_id, session_reset=session_reset, namespace_created=created)
    except Exception as exc:
        # Traceback included server paths on the kit wire. Eval errors from
        # json_egress are only status/error/stdout; this path must match.
        err = {"id": req_id, "status": "error", "code": "WORKER_EXECUTION_ERROR", "error": str(exc), "stdout": ""}
        return json_forward_envelope(err, req_id=req_id, session_reset=False, namespace_created=False)


def main() -> int:
    return run_compute_worker(_handle_request)


if __name__ == "__main__":
    raise SystemExit(main())
