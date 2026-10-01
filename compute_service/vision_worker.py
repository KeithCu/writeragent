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
import traceback
from typing import Any

# Ensure repo root is on sys.path
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.worker_base import run_worker_stdio_loop


def _handle_request(req: dict[str, Any]) -> dict[str, Any]:
    req_id = req.get("id")
    helper = str(req.get("helper") or "extract_text").strip()
    params = req.get("params") or {}
    image_b64 = req.get("image_b64") or req.get("image")
    file_path = req.get("file_path")

    image_bytes: bytes
    if file_path:
        if not isinstance(file_path, str) or not file_path.strip():
            return {"id": req_id, "status": "error", "code": "INVALID_FILE_PATH", "error": "file_path must be a non-empty string path"}
        p = os.path.expanduser(file_path.strip())
        if not os.path.exists(p):
            return {"id": req_id, "status": "error", "code": "FILE_NOT_FOUND", "error": f"Image file not found: {file_path}"}
        if not os.path.isfile(p):
            return {"id": req_id, "status": "error", "code": "NOT_A_FILE", "error": f"Path is not a regular file: {file_path}"}
        try:
            with open(p, "rb") as f:
                image_bytes = f.read()
        except Exception as exc:
            return {"id": req_id, "status": "error", "code": "FILE_READ_ERROR", "error": f"Failed to read image file {file_path}: {exc}"}
    elif req.get("image_bytes") and isinstance(req["image_bytes"], (bytes, bytearray)):
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

    from plugin.vision.venv.vision import run_vision

    try:
        spec = {"helper": helper, "params": params}
        res = run_vision(spec=spec, image=image_bytes)
        if req_id is not None and isinstance(res, dict):
            res["id"] = req_id
        return res
    except Exception as exc:
        return {"id": req_id, "status": "error", "code": "VISION_WORKER_ERROR", "error": str(exc), "traceback": traceback.format_exc()}


def main() -> int:
    return run_worker_stdio_loop(_handle_request)


if __name__ == "__main__":
    raise SystemExit(main())
