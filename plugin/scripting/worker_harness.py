#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Long-lived venv worker: length-prefixed pickle requests on stdin, responses on stdout.

Each execute request runs user code in LocalPythonExecutor. Without ``session_id`` the
namespace is fresh per call; with ``session_id`` the same executor is reused (shared kernel).
"""
from __future__ import annotations

import os
import sys
from typing import Any

# Standalone entry (venv python worker_harness.py): repo root must be on sys.path for plugin.* imports.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from plugin.scripting.venv_sandbox import reset_sandbox_session, run_sandboxed_code, serialize_result


def _execute_request(
    code: str,
    data: Any | None,
    *,
    session_id: str | None = None,
    init_script: str | None = None,
    init_session_id: str | None = None,
    init_script_hash: str | None = None,
) -> dict[str, Any]:
    return run_sandboxed_code(
        code,
        data=data,
        session_id=session_id,
        init_script=init_script,
        init_session_id=init_session_id,
        init_script_hash=init_script_hash,
    )


def _handle_request(request: dict[str, Any]) -> dict[str, Any]:
    action = request.get("action")
    if action == "reset_session":
        session_id = request.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return {"status": "error", "message": "No session_id provided."}
        return reset_sandbox_session(session_id)

    code = request.get("code")
    if not isinstance(code, str) or not code.strip():
        return {"status": "error", "message": "No code provided."}
    session_id = request.get("session_id")
    sid = session_id if isinstance(session_id, str) and session_id.strip() else None
    init_script = request.get("init_script")
    init_session_id = request.get("init_session_id")
    init_hash = request.get("init_script_hash")
    return _execute_request(
        code,
        request.get("data"),
        session_id=sid,
        init_script=init_script if isinstance(init_script, str) else None,
        init_session_id=init_session_id if isinstance(init_session_id, str) else None,
        init_script_hash=init_hash if isinstance(init_hash, str) else None,
    )


# Back-compat for tests: from plugin.scripting.worker_harness import _serialize
def _serialize(obj: Any) -> Any:
    return serialize_result(obj)


def main() -> None:
    import pickle
    import struct

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        header = stdin.read(4)
        if not header or len(header) < 4:
            break

        size = struct.unpack("!I", header)[0]
        payload = stdin.read(size)
        if len(payload) < size:
            break

        req_id = ""
        try:
            # Trusted IPC: bytes from WriterAgent host that spawned this harness process.
            request = pickle.loads(payload)  # nosec B301
            req_id = str(request.get("id", ""))
            response = _handle_request(request)
        except pickle.UnpicklingError as e:
            response = {"status": "error", "message": f"Invalid pickle request: {e}"}
        except Exception as e:
            response = {"status": "error", "message": str(e)}

        response["id"] = req_id
        try:
            out_payload = pickle.dumps(response, protocol=5)
        except Exception as e:
            err_response = {"id": req_id, "status": "error", "message": f"Pickle serialization failed: {e}"}
            out_payload = pickle.dumps(err_response, protocol=5)

        stdout.write(struct.pack("!I", len(out_payload)))
        stdout.write(out_payload)
        stdout.flush()



if __name__ == "__main__":
    main()
