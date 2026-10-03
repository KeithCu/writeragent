# WriterAgent - Python Compute Service Vision Process Pool
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Process pool supervisor for heavy, isolated OCR and Vision workloads.

Maintains a bounded pool of warm subprocesses. Fast spreadsheet calculations
in the compute service remain unblocked in their thread pool, while heavy
Docling / PaddleOCR tasks run safely in isolated worker processes.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
from typing import Any

from compute_service.config import ComputeSettings
from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES
from compute_service.worker_base import BaseProcessPool, remaining_sec

log = logging.getLogger("compute_service.vision")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_WORKER_SCRIPT = os.path.join(_SCRIPT_DIR, "vision_worker.py")


class VisionProcessPool(BaseProcessPool):
    """Bounded pool of persistent worker subprocesses for Vision/OCR."""

    def __init__(self, settings: ComputeSettings | None = None, num_workers: int | None = None, default_timeout_sec: int | None = None, max_tasks: int | None = None, idle_worker_ttl_sec: float | None = None) -> None:
        cfg = settings or ComputeSettings()
        eff_num_workers = cfg.ocr_workers if num_workers is None else num_workers
        eff_timeout = cfg.ocr_timeout_sec if default_timeout_sec is None else default_timeout_sec
        eff_max_tasks = cfg.ocr_max_tasks if max_tasks is None else max_tasks
        eff_idle_ttl = cfg.idle_worker_ttl_sec if idle_worker_ttl_sec is None else idle_worker_ttl_sec

        # Formula workers already pass this. The 16 MiB IPC default rejected a
        # body the HTTP layer had accepted (32 MiB) as an uncaught ValueError.
        # A slow OCR call still writes one frame. recover_on_timeout drains
        # that frame and reuses the process instead of SIGKILL.
        super().__init__(script_path=_WORKER_SCRIPT, num_workers=eff_num_workers, default_timeout_sec=eff_timeout, max_tasks=eff_max_tasks, worker_name="Vision worker", idle_worker_ttl_sec=eff_idle_ttl, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES, recover_on_timeout=True)

    def execute(self, helper: str, image_b64: str | bytes | None = None, file_path: str | None = None, params: dict[str, Any] | None = None, timeout_sec: int | None = None, req_id: str | None = None, allow_paths: tuple[str, ...] | list[str] | None = None) -> dict[str, Any]:
        """Execute a vision task on an available worker process.

        The HTTP handler returns 400 for a denied path. The worker checks
        again before ``open``. A third check here used to answer 200 +
        ``FILE_PATH_DENIED`` when it fired.
        """
        if not self.is_enabled():
            return {"id": req_id, "status": "error", "code": "VISION_SERVICE_DISABLED", "error": "Vision / OCR service is not enabled on this instance (ocr_workers=0)."}

        eff_timeout = float(timeout_sec or self.default_timeout_sec)
        image_bytes = None
        if image_b64 is not None:
            if isinstance(image_b64, (bytes, bytearray)):
                image_bytes = bytes(image_b64)
            elif isinstance(image_b64, str):
                try:
                    image_bytes = base64.b64decode(image_b64, validate=True)
                except Exception as exc:
                    return {"id": req_id, "status": "error", "code": "INVALID_BASE64", "error": f"Base64 decode failed: {exc}"}
            else:
                return {"id": req_id, "status": "error", "code": "INVALID_IMAGE", "error": "image_b64 must be base64 string or raw bytes"}

        prefixes = () if allow_paths is None else tuple(str(p) for p in allow_paths)
        payload = {"id": req_id, "helper": helper, "image_bytes": image_bytes, "file_path": file_path, "params": params or {}, "allow_paths": prefixes}

        # Queue until a worker is free. The caller's timeout is the bound;
        # VISION_POOL_BUSY means that wait expired, not that the pool was busy
        # at the moment the request arrived.
        deadline = time.monotonic() + eff_timeout
        worker = self.lease_any(timeout_sec=remaining_sec(deadline))
        if worker is None:
            return {"id": req_id, "status": "error", "code": "VISION_POOL_BUSY", "error": "All vision workers are currently busy and request timed out waiting for worker lease."}

        try:
            res = worker.execute(payload, timeout_sec=remaining_sec(deadline))
            if req_id is not None and isinstance(res, dict):
                res["id"] = req_id
            return res
        finally:
            self.release_worker(worker)


# Global singleton per server process
_GLOBAL_VISION_POOL: VisionProcessPool | None = None
_GLOBAL_VISION_POOL_LOCK = threading.Lock()


def get_vision_pool(settings: ComputeSettings | None = None) -> VisionProcessPool:
    """Retrieve or initialize the global vision process pool."""
    global _GLOBAL_VISION_POOL
    with _GLOBAL_VISION_POOL_LOCK:
        if _GLOBAL_VISION_POOL is None:
            _GLOBAL_VISION_POOL = VisionProcessPool(settings=settings)
        return _GLOBAL_VISION_POOL


def shutdown_vision_pool() -> None:
    """Shut down the global vision process pool."""
    global _GLOBAL_VISION_POOL
    with _GLOBAL_VISION_POOL_LOCK:
        if _GLOBAL_VISION_POOL is not None:
            _GLOBAL_VISION_POOL.shutdown()
            _GLOBAL_VISION_POOL = None
