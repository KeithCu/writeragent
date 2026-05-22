#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Long-lived venv worker: one JSON request per line on stdin, one response per line on stdout.

Each request runs user code in a **fresh** LocalPythonExecutor namespace. The process stays
warm; only interpreter state is discarded between requests.
"""
from __future__ import annotations

import pickle
import struct
import os
import sys
from typing import Any

# Standalone entry (venv python worker_harness.py): repo root must be on sys.path for plugin.* imports.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from plugin.scripting.venv_sandbox import run_sandboxed_code, serialize_result


def _execute_request(code: str, data: Any | None) -> dict[str, Any]:
    return run_sandboxed_code(code, data=data)


# Back-compat for tests: from plugin.scripting.worker_harness import _serialize
def _serialize(obj: Any) -> Any:
    return serialize_result(obj)


def main() -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        header = stdin.read(4)
        if not header:
            break
        if len(header) < 4:
            break

        size = struct.unpack("!I", header)[0]
        payload = stdin.read(size)
        if len(payload) < size:
            break

        req_id = ""
        try:
            request = pickle.loads(payload)
            req_id = str(request.get("id", ""))
            code = request.get("code")
            if not isinstance(code, str) or not code.strip():
                response: dict[str, Any] = {"status": "error", "message": "No code provided."}
            else:
                response = _execute_request(code, request.get("data"))
        except pickle.UnpicklingError as e:
            response = {"status": "error", "message": f"Invalid pickle request: {e}"}
        except Exception as e:
            response = {"status": "error", "message": str(e)}

        response["id"] = req_id
        try:
            out_payload = pickle.dumps(response, protocol=5)
        except Exception as e:
            # Fallback for unpicklable results
            err_response = {"id": req_id, "status": "error", "message": f"Pickle serialization failed: {e}"}
            out_payload = pickle.dumps(err_response, protocol=5)

        stdout.write(struct.pack("!I", len(out_payload)))
        stdout.write(out_payload)
        stdout.flush()


if __name__ == "__main__":
    main()
