#!/usr/bin/env python3
# WriterAgent - Python Compute Service Vision Worker
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Standalone worker subprocess for heavy OCR and Vision tasks.

Runs in an isolated process to isolate ML dependencies (Docling, PaddleOCR, PyTorch,
ONNX) and large memory buffers from the main compute service thread pool.
"""

from __future__ import annotations

import base64
import os
import sys
from typing import Any, cast

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

from compute_service.config import read_allowlisted_file
from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES
from compute_service.worker_base import run_worker_stdio_loop

# Default HTTP body cap. file_path does not pass through that check, and an
# unbounded read was pickled into the parent afterward.
_FILE_READ_MAX_BYTES = 32 * 1024 * 1024


def _read_allowed_image(file_path: str, allow_paths: Any, req_id: Any) -> tuple[bytes | None, dict[str, Any] | None]:
    """Return ``(bytes, None)`` or ``(None, error)``.

    The HTTP handler returns 400 for a path that is outside the allowlist
    before the pool is touched. The read itself is ``read_allowlisted_file``:
    the same prefix rule, applied to the opened path. Linux uses
    ``/proc/self/fd`` for that descriptor. Other platforms realpath the path
    that was opened, so a symlink swapped in before ``open`` returns cannot
    leave the prefix.
    """
    prefixes = allow_paths if isinstance(allow_paths, (list, tuple)) else ()
    data, err = read_allowlisted_file(file_path, prefixes, max_bytes=_FILE_READ_MAX_BYTES)
    if err is not None:
        body = dict(err)
        body["id"] = req_id
        return None, body
    return data, None


def _handle_request(req: dict[str, Any]) -> dict[str, Any]:
    req_id = req.get("id")
    helper = str(req.get("helper") or "extract_text").strip()
    params = req.get("params") or {}
    image_b64 = req.get("image_b64") or req.get("image")
    file_path = req.get("file_path")

    image_bytes: bytes
    if file_path:
        image_bytes_opt, err_body = _read_allowed_image(file_path, req.get("allow_paths"), req_id)
        if err_body is not None:
            return err_body
        image_bytes = cast("bytes", image_bytes_opt)
    elif isinstance(req.get("image_bytes"), (bytes, bytearray)):
        image_bytes = bytes(req["image_bytes"])
    elif image_b64:
        try:
            if isinstance(image_b64, str):
                image_bytes = base64.b64decode(image_b64)
            elif isinstance(image_b64, (bytes, bytearray)):
                image_bytes = bytes(image_b64)
            else:
                return {"id": req_id, "status": "error", "code": "INVALID_IMAGE", "error": "image_b64 must be base64 string or raw bytes"}
        except Exception as exc:
            return {"id": req_id, "status": "error", "code": "INVALID_BASE64", "error": f"Base64 decode failed: {exc}"}
    else:
        return {"id": req_id, "status": "error", "code": "MISSING_IMAGE_SOURCE", "error": "Either 'image_b64' (base64 string buffer), 'image_bytes', or 'file_path' (server filesystem path) must be provided."}

    try:
        from plugin.vision.venv.vision import run_vision
    except (ModuleNotFoundError, ImportError):
        return {"id": req_id, "status": "error", "code": "VISION_UNAVAILABLE", "error": "OCR is not installed in this server"}

    try:
        spec = {"helper": helper, "params": params}
        res = run_vision(spec=spec, image=image_bytes)
        if req_id is not None and isinstance(res, dict):
            res["id"] = req_id
        return res
    except Exception as exc:
        # Omit traceback — server paths on the kit wire; see formula_worker.py.
        return {"id": req_id, "status": "error", "code": "VISION_WORKER_ERROR", "error": str(exc)}


def main() -> int:
    # The parent pool reads and writes COMPUTE_MAX_PAYLOAD_BYTES (33 MiB).
    # The stdio default is 16 MiB, so a request the parent had accepted
    # failed in the child, and a result over 16 MiB broke this loop
    # (host saw EMPTY_RESPONSE). formula_worker already passes the cap.
    return run_worker_stdio_loop(_handle_request, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)


if __name__ == "__main__":
    raise SystemExit(main())
