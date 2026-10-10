#!/usr/bin/env python3
# WriterAgent - Python Compute Service Vision Pool & Worker
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Process pool supervisor and worker subprocess for heavy OCR and Vision tasks.

Maintains a bounded pool of warm subprocesses. Fast spreadsheet calculations
in the compute service remain unblocked in their thread pool, while heavy
Docling / PaddleOCR tasks run safely in isolated worker processes to isolate
ML dependencies (PyTorch, ONNX) and large memory buffers.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import time
from typing import Any, cast

# Ensure repo root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.config import ComputeSettings, MAX_BODY_BYTES, read_allowlisted_file
from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES
from compute_service.worker_base import BaseProcessPool, PoolSingleton, _Deadline, error_dict, resolve_override, run_compute_worker

log = logging.getLogger("compute_service.vision")

# Same cap as ComputeSettings.max_body_bytes. file_path does not pass through
# the HTTP check, and an unbounded read was pickled into the parent afterward.
_FILE_READ_MAX_BYTES = MAX_BODY_BYTES

_WORKER_SCRIPT = os.path.abspath(__file__)


def _decode_image_b64(image_input: str) -> bytes:
    """Decode a base64 image, including whitespace and a ``data:`` URL prefix.

    ``validate=True`` rejects whitespace, so it is stripped after the prefix.
    URL-safe producers use ``-`` and ``_`` and often omit padding. ``altchars``
    accepts that alphabet; padding is restored before the alphabet check.
    A character in neither alphabet still fails.
    """
    text = image_input.strip()
    if text.lower().startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    text = "".join(text.split())
    pad = (-len(text)) % 4
    if pad:
        text += "=" * pad
    return base64.b64decode(text, altchars=b"-_", validate=True)


class VisionProcessPool(BaseProcessPool):
    """Bounded pool of persistent worker subprocesses for Vision/OCR."""

    def __init__(
        self,
        settings: ComputeSettings | None = None,
        num_workers: int | None = None,
        default_timeout_sec: int | None = None,
        max_tasks: int | None = None,
        idle_worker_ttl_sec: float | None = None,
    ) -> None:
        cfg = settings or ComputeSettings()
        eff_num_workers = resolve_override(num_workers, cfg.ocr_workers)
        eff_timeout = resolve_override(default_timeout_sec, cfg.ocr_timeout_sec)
        eff_max_tasks = resolve_override(max_tasks, cfg.ocr_max_tasks)
        eff_idle_ttl = resolve_override(idle_worker_ttl_sec, cfg.idle_worker_ttl_sec)

        # Formula workers already pass this. The 16 MiB IPC default rejected a
        # body the HTTP layer had accepted (32 MiB) as an uncaught ValueError.
        # A read timeout kills the child. The next lease respawns it.
        super().__init__(
            script_path=_WORKER_SCRIPT,
            num_workers=eff_num_workers,
            default_timeout_sec=eff_timeout,
            max_tasks=eff_max_tasks,
            worker_name="Vision worker",
            idle_worker_ttl_sec=eff_idle_ttl,
            max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES,
        )

    def execute(
        self,
        helper: str,
        # bytes: for direct programmatic calls; HTTP passes str or bytes
        image: str | bytes | bytearray | None = None,
        image_b64: str | bytes | bytearray | None = None,
        file_path: str | None = None,
        params: dict[str, Any] | None = None,
        timeout_sec: int | None = None,
        req_id: str | None = None,
        allow_paths: tuple[str, ...] | list[str] | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Execute a vision task on an available worker process.

        The HTTP handler returns 400 for a denied path. The worker checks
        again before ``open``, so this layer does not add a third answer.
        """
        if not self.is_enabled():
            return error_dict("VISION_SERVICE_DISABLED", "Vision / OCR service is not enabled on this instance (ocr_workers=0).", req_id=req_id)

        # Only None means "use the default". Zero is an explicit timeout.
        eff_timeout = float(self.default_timeout_sec if timeout_sec is None else timeout_sec)
        image_input = image if image is not None else image_b64
        image_bytes = None
        if image_input is not None:
            if isinstance(image_input, (bytes, bytearray)):
                image_bytes = bytes(image_input)
            elif isinstance(image_input, str):
                try:
                    image_bytes = _decode_image_b64(image_input)
                except Exception as exc:
                    return error_dict("INVALID_BASE64", f"Base64 decode failed: {exc}", req_id=req_id)
            else:
                return error_dict("INVALID_IMAGE", "image must be base64 string or raw bytes", req_id=req_id)

        prefixes = () if allow_paths is None else tuple(str(p) for p in allow_paths)
        payload = {"id": req_id, "helper": helper, "image_bytes": image_bytes, "file_path": file_path, "params": params or {}, "allow_paths": prefixes}

        # Queue until a worker is free. The caller's timeout/deadline is the bound;
        # VISION_POOL_BUSY means that wait expired, not that the pool was busy
        # at the moment the request arrived.
        if deadline is None:
            deadline = time.monotonic() + eff_timeout

        # Under one second requested, or a longer budget that has already
        # fallen under one second, do not lease. A 0.01s floor used to start
        # OCR on a deadline that had already passed. A one-second request
        # still leases: the clock moves before this check.
        clock = _Deadline.from_absolute(eff_timeout, deadline)
        if clock.too_late_to_spawn():
            return error_dict("VISION_POOL_BUSY", "All vision workers are currently busy and request timed out waiting for worker lease.", req_id=req_id)
        lease_budget = max(deadline - time.monotonic(), 0.0)
        with self.leased(timeout_sec=lease_budget) as worker:
            # Time passes while waiting for the lease. Recheck the same clock.
            if worker is None or clock.too_late_to_spawn():
                return error_dict("VISION_POOL_BUSY", "All vision workers are currently busy and request timed out waiting for worker lease.", req_id=req_id)

            # child_run_seconds floors a positive remainder under one second
            # up to one second. A one-second OCR request still runs after the
            # lease. too_late_to_spawn already returned VISION_POOL_BUSY when
            # the clock is spent. Passing the raw remainder would make
            # execute answer EXECUTION_TIMEOUT.
            return worker.execute(payload, timeout_sec=clock.child_run_seconds(), req_id=req_id)


# Global singleton per server process
_POOL_SINGLETON: PoolSingleton[VisionProcessPool] = PoolSingleton()


def get_vision_pool(settings: ComputeSettings | None = None) -> VisionProcessPool:
    """Retrieve or initialize the global vision process pool."""
    return _POOL_SINGLETON.get(lambda: VisionProcessPool(settings=settings))


def shutdown_vision_pool(*, permanent: bool = False) -> None:
    """Shut down the global vision process pool.

    *permanent* is the server-exit path. A later ``get_vision_pool`` raises
    instead of spawning a new pool.
    """
    _POOL_SINGLETON.shutdown(permanent=permanent)


# ---------------------------------------------------------------------------
# Worker subprocess request handling
# ---------------------------------------------------------------------------


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
    file_path = req.get("file_path")
    image_bytes_raw = req.get("image_bytes")

    image_bytes: bytes
    has_bytes = isinstance(image_bytes_raw, (bytes, bytearray)) and len(image_bytes_raw) > 0
    if file_path and has_bytes:
        return {
            "id": req_id,
            "status": "error",
            "code": "INVALID_REQUEST",
            "error": "Provide image bytes or file_path, not both.",
        }
    if file_path:
        image_bytes_opt, err_body = _read_allowed_image(file_path, req.get("allow_paths"), req_id)
        if err_body is not None:
            return err_body
        image_bytes = cast("bytes", image_bytes_opt)
    elif isinstance(image_bytes_raw, (bytes, bytearray)):
        image_bytes = bytes(image_bytes_raw)
    else:
        return {"id": req_id, "status": "error", "code": "MISSING_IMAGE_SOURCE", "error": "Either image buffer or 'file_path' (server filesystem path) must be provided."}

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
    return run_compute_worker(_handle_request)


if __name__ == "__main__":
    raise SystemExit(main())
