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

import logging
import os
import sys
import time
from typing import Any

# Ensure repo root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.config import ComputeSettings, MAX_BODY_BYTES, read_allowlisted_file
from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES
from compute_service.worker_base import MIN_REQUEST_SEC, BaseProcessPool, Deadline, PoolSingleton, error_dict, resolve_override, run_compute_worker

log = logging.getLogger("compute_service.vision")

_WORKER_SCRIPT = os.path.abspath(__file__)


class VisionProcessPool(BaseProcessPool):
    """Bounded pool of persistent worker subprocesses for Vision/OCR."""

    def __init__(self, settings: ComputeSettings | None = None, num_workers: int | None = None, default_timeout_sec: int | None = None, max_tasks: int | None = None, idle_worker_ttl_sec: float | None = None) -> None:
        cfg = settings or ComputeSettings()
        eff_num_workers = resolve_override(num_workers, cfg.ocr_workers)
        eff_timeout = resolve_override(default_timeout_sec, cfg.ocr_timeout_sec)
        eff_max_tasks = resolve_override(max_tasks, cfg.ocr_max_tasks)
        eff_idle_ttl = resolve_override(idle_worker_ttl_sec, cfg.idle_worker_ttl_sec)

        # Formula workers already pass this. The 16 MiB IPC default rejected a
        # body the HTTP layer had accepted (32 MiB) as an uncaught ValueError.
        # A read timeout kills the child. The next lease respawns it.
        super().__init__(script_path=_WORKER_SCRIPT, num_workers=eff_num_workers, default_timeout_sec=eff_timeout, max_tasks=eff_max_tasks, worker_name="Vision worker", idle_worker_ttl_sec=eff_idle_ttl, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)

    def execute(
        self,
        helper: str,
        # Raw image bytes. HTTP passes the multipart image part as is.
        image: bytes | bytearray | None = None,
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
        prefixes = () if allow_paths is None else tuple(str(p) for p in allow_paths)
        payload = {"id": req_id, "helper": helper, "image_bytes": image, "file_path": file_path, "params": params or {}, "allow_paths": prefixes}

        # Queue until a worker is free. The caller's timeout/deadline is the bound;
        # VISION_POOL_BUSY means that wait expired, not that the pool was busy
        # at the moment the request arrived.
        if deadline is None:
            deadline = time.monotonic() + eff_timeout

        # Under one second requested, or a longer budget that has already
        # fallen under one second, do not lease. A 0.01s floor used to start
        # OCR on a deadline that had already passed. A one-second request
        # still leases: the clock moves before this check.
        clock = Deadline.from_absolute(eff_timeout, deadline)
        if clock.usable() is None:
            return error_dict("VISION_POOL_BUSY", "All vision workers are currently busy and request timed out waiting for worker lease.", req_id=req_id)
        lease_budget = max(deadline - time.monotonic(), 0.0)
        with self.leased(timeout_sec=lease_budget) as worker:
            # Time passes while waiting for the lease. Recheck the same clock.
            run_for = None if worker is None else clock.usable()
            if worker is None or run_for is None:
                return error_dict("VISION_POOL_BUSY", "All vision workers are currently busy and request timed out waiting for worker lease.", req_id=req_id)

            # A one-second OCR request can already be under a second.
            # execute builds a new clock from this timeout, and a budget
            # under one second is refused. The lift stays here.
            execute_for = run_for if run_for >= MIN_REQUEST_SEC else MIN_REQUEST_SEC
            return worker.execute(payload, timeout_sec=execute_for, req_id=req_id)


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


def _handle_request(req: dict[str, Any]) -> dict[str, Any]:
    req_id = req.get("id")
    helper = str(req.get("helper") or "extract_text").strip()
    params = req.get("params") or {}
    file_path = req.get("file_path")
    image_bytes_raw = req.get("image_bytes")

    if file_path:
        prefixes = req.get("allow_paths")
        if not isinstance(prefixes, (list, tuple)):
            prefixes = ()
        data, err = read_allowlisted_file(file_path, prefixes, max_bytes=MAX_BODY_BYTES)
        if err is not None:
            return error_dict(err["code"], err["error"], req_id=req_id)
        image_bytes = data
    else:
        image_bytes = image_bytes_raw

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
