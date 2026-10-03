# WriterAgent - Python Compute Service Vision Pool tests
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from compute_service.config import ComputeSettings
from compute_service.server import WSGIDualStackServer, create_wsgi_app
from compute_service.vision_pool import (
    VisionProcessPool,
    get_vision_pool,
    shutdown_vision_pool,
)
from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES
from compute_service.vision_worker import _FILE_READ_MAX_BYTES, _handle_request, _read_allowed_image


from tests.compute_service.conftest import get_free_port


# Minimal 1x1 PNG base64 for testing
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def cleanup_vision_pool():
    yield
    shutdown_vision_pool()


def test_vision_pool_uses_compute_frame_cap() -> None:
    pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=0))
    assert pool.max_payload_bytes == COMPUTE_MAX_PAYLOAD_BYTES
    pool.shutdown()


def test_vision_file_read_is_capped(tmp_path) -> None:
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (_FILE_READ_MAX_BYTES + 1))
    data, err = _read_allowed_image(str(path), (str(tmp_path),), "ocr-big")
    assert data is None
    assert err is not None
    assert err["code"] == "FILE_TOO_LARGE"


class TestVisionPoolSupervisor:
    def test_default_pool_uses_config_defaults(self) -> None:
        pool = VisionProcessPool()
        try:
            assert not pool.is_enabled()
            assert len(pool.workers) == 0
        finally:
            pool.shutdown()

    def test_get_vision_pool_defaults(self) -> None:
        pool = get_vision_pool()
        assert not pool.is_enabled()
        assert len(pool.workers) == 0

    def test_get_vision_pool_singleton_and_reset(self) -> None:
        p1 = get_vision_pool()
        p2 = get_vision_pool()
        assert p1 is p2
        shutdown_vision_pool()
        p3 = get_vision_pool()
        assert p3 is not p1
        shutdown_vision_pool()

    def test_pool_rejects_malformed_base64(self) -> None:
        pool = VisionProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            res = pool.execute(helper="extract_text", image_b64="!!!not_valid_b64!!!", req_id="bad-b64")
            assert res.get("id") == "bad-b64"
            assert res.get("status") == "error"
            assert res.get("code") == "INVALID_BASE64"
            assert "Base64 decode failed" in res.get("error", "")
            # Worker was not leased, so tasks_executed remains 0
            assert pool.workers[0].tasks_executed == 0
        finally:
            pool.shutdown()

    def test_pool_lifecycle(self) -> None:
        pool = VisionProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            assert pool.is_enabled()
            assert len(pool.workers) == 1
            assert pool.workers[0].recover_on_timeout
            # Execute simple text extraction helper on tiny PNG
            res = pool.execute(helper="extract_text", image_b64=_TINY_PNG_B64, req_id="v-1")
            assert res.get("id") == "v-1"
            assert "status" in res
        finally:
            pool.shutdown()
            assert not pool.is_enabled()

    def test_pool_disabled(self) -> None:
        pool = VisionProcessPool(num_workers=0)
        assert not pool.is_enabled()
        res = pool.execute(helper="extract_text", image_b64=_TINY_PNG_B64, req_id="v-disabled")
        assert res.get("status") == "error"
        assert res.get("code") == "VISION_SERVICE_DISABLED"

    def test_pool_file_path_success(self, tmp_path) -> None:
        img_path = tmp_path / "test_image.png"
        img_path.write_bytes(base64.b64decode(_TINY_PNG_B64))

        pool = VisionProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            res = pool.execute(
                helper="extract_text",
                file_path=str(img_path),
                req_id="v-file-1",
                allow_paths=(str(tmp_path),),
            )
            assert res.get("id") == "v-file-1"
            assert "status" in res
        finally:
            pool.shutdown()

    def test_pool_file_path_not_found(self) -> None:
        pool = VisionProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            denied = pool.execute(
                helper="extract_text",
                file_path="/tmp/non_existent_12345.png",
                req_id="v-denied",
            )
            assert denied.get("status") == "error"
            assert denied.get("code") == "FILE_PATH_DENIED"
            missing = pool.execute(
                helper="extract_text",
                file_path="/tmp/non_existent_12345.png",
                req_id="v-missing",
                allow_paths=("/tmp",),
            )
            assert missing.get("status") == "error"
            assert missing.get("code") == "FILE_NOT_FOUND"
        finally:
            pool.shutdown()

    def test_busy_lease_waits_until_worker_is_free(self) -> None:
        pool = VisionProcessPool(num_workers=1, default_timeout_sec=15)
        held = pool.lease_any(timeout_sec=1.0)
        assert held is not None
        result: list[dict] = []

        def _run() -> None:
            result.append(pool.execute(helper="extract_text", image_b64=_TINY_PNG_B64, req_id="v-wait", timeout_sec=15))

        waiter = threading.Thread(target=_run)
        waiter.start()
        try:
            # Longer than an immediate busy return, so a fail-fast lease would already be done.
            time.sleep(0.3)
            assert result == []
            pool.release_worker(held)
            held = None
            waiter.join(timeout=20)
            assert not waiter.is_alive()
            assert result[0].get("code") != "VISION_POOL_BUSY"
            assert result[0].get("id") == "v-wait"
        finally:
            if held is not None:
                pool.release_worker(held)
            waiter.join(timeout=5)
            pool.shutdown()

    def test_worker_denies_symlink_outside_allow_paths(self, tmp_path) -> None:
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"secret-bytes")
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        link = allowed / "img.png"
        link.symlink_to(outside)
        denied = _handle_request({"id": "sym", "file_path": str(link), "allow_paths": [str(allowed)]})
        assert denied.get("code") == "FILE_PATH_DENIED"

        inside = allowed / "ok.png"
        inside.write_bytes(b"png-bytes")
        with patch("plugin.vision.venv.vision.run_vision", return_value={"status": "ok", "text": "x"}) as run:
            ok = _handle_request({"id": "ok", "helper": "extract_text", "file_path": str(inside), "allow_paths": [str(allowed)]})
        assert ok.get("status") == "ok"
        assert run.call_args.kwargs["image"] == b"png-bytes"

    def test_worker_crash_recovery(self) -> None:
        pool = VisionProcessPool(num_workers=1, default_timeout_sec=10)
        try:
            worker = pool.workers[0]
            # Kill worker externally
            worker.kill()
            assert not worker.is_alive()

            # Next request should automatically spawn a fresh worker and succeed
            res = pool.execute(helper="extract_text", image_b64=_TINY_PNG_B64, req_id="v-recovery")
            assert "status" in res
            assert worker.is_alive()
        finally:
            pool.shutdown()


class TestVisionHttpEndpoint:
    @pytest.fixture
    def vision_server(self):
        port = get_free_port()
        settings = ComputeSettings(
            host="127.0.0.1",
            port=port,
            api_key="vision-secret",
            ocr_workers=1,
        )
        app = create_wsgi_app(settings)
        server = WSGIDualStackServer("127.0.0.1", port, max_threads=4)
        server.set_app(app)

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)
        yield f"http://127.0.0.1:{port}"
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        shutdown_vision_pool()

    def _post(self, url: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{url}/v1/vision", data=data, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return resp.status, body
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode("utf-8"))
            return e.code, body

    def test_vision_auth_required(self, vision_server: str) -> None:
        status, body = self._post(vision_server, {"image_b64": _TINY_PNG_B64})
        assert status == 401
        assert body["status"] == "error"

    def test_vision_success_b64(self, vision_server: str) -> None:
        status, body = self._post(
            vision_server,
            {"id": "test-ocr-1", "helper": "extract_text", "image_b64": _TINY_PNG_B64},
            headers={"Authorization": "Bearer vision-secret"},
        )
        assert status == 200
        assert body.get("id") == "test-ocr-1"
        assert "status" in body

    def test_vision_file_path_denied_by_default(self, vision_server: str, tmp_path) -> None:
        img_path = tmp_path / "endpoint_img.png"
        img_path.write_bytes(base64.b64decode(_TINY_PNG_B64))

        status, body = self._post(
            vision_server,
            {"id": "test-ocr-file", "helper": "extract_text", "file_path": str(img_path)},
            headers={"Authorization": "Bearer vision-secret"},
        )
        assert status == 400
        assert body.get("code") == "FILE_PATH_DENIED"

    def test_vision_file_path_allowed_prefix(self, tmp_path) -> None:
        img_path = tmp_path / "allowed.png"
        img_path.write_bytes(base64.b64decode(_TINY_PNG_B64))
        port = get_free_port()
        settings = ComputeSettings(
            host="127.0.0.1",
            port=port,
            api_key="vision-secret",
            ocr_workers=1,
            ocr_allow_paths=(str(tmp_path),),
        )
        app = create_wsgi_app(settings)
        server = WSGIDualStackServer("127.0.0.1", port, max_threads=4)
        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)
        try:
            status, body = self._post(
                f"http://127.0.0.1:{port}",
                {"id": "test-ocr-ok", "helper": "extract_text", "file_path": str(img_path)},
                headers={"Authorization": "Bearer vision-secret"},
            )
            assert status == 200
            assert body.get("id") == "test-ocr-ok"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_vision_missing_image(self, vision_server: str) -> None:
        status, body = self._post(
            vision_server,
            {"id": "test-ocr-missing", "helper": "extract_text"},
            headers={"Authorization": "Bearer vision-secret"},
        )
        assert status == 400
        assert body["status"] == "error"
        assert "Missing image input" in body["error"]

    def test_vision_endpoint_disabled_when_zero_workers(self) -> None:
        """When ocr_workers=0 (the default), /v1/vision returns VISION_SERVICE_DISABLED without error."""
        port = get_free_port()
        settings = ComputeSettings(
            host="127.0.0.1",
            port=port,
            api_key="vision-secret",
            ocr_workers=0,
        )
        app = create_wsgi_app(settings)
        server = WSGIDualStackServer("127.0.0.1", port, max_threads=2)
        server.set_app(app)

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)
        try:
            url = f"http://127.0.0.1:{port}"
            status, body = self._post(
                url,
                {"id": "test-zero-workers", "helper": "extract_text", "image_b64": _TINY_PNG_B64},
                headers={"Authorization": "Bearer vision-secret"},
            )
            assert status == 200
            assert body.get("id") == "test-zero-workers"
            assert body.get("status") == "error"
            assert body.get("code") == "VISION_SERVICE_DISABLED"
            assert "ocr_workers=0" in body.get("error", "")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def _delay_worker(tmp_path):
    """Stdio worker that sleeps for ``delay`` seconds, then answers with its pid."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    script = tmp_path / "delay_worker.py"
    script.write_text(
        "\n".join(
            [
                "import os, sys, time",
                f"sys.path.insert(0, {str(repo)!r})",
                "from compute_service.worker_base import run_worker_stdio_loop",
                "def handle(req):",
                "    time.sleep(float(req.get('delay') or 0))",
                "    return {'status': 'ok', 'pid': os.getpid()}",
                "if __name__ == '__main__':",
                "    raise SystemExit(run_worker_stdio_loop(handle))",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return script


def test_vision_timeout_reuses_same_process(tmp_path) -> None:
    """A late frame is discarded and the same process serves the next call.

    Formula timeouts SIGKILL. Vision keeps the process because the model is
    already loaded; the pipe stays leased until that one frame arrives.
    """
    from compute_service.worker_base import BaseProcessPool

    pool = BaseProcessPool(str(_delay_worker(tmp_path)), num_workers=1, worker_name="Vision worker", recover_on_timeout=True)
    try:
        worker = pool.lease_any(2)
        assert worker is not None
        assert worker.process is not None
        pid = worker.process.pid
        res = worker.execute({"delay": 0.45}, timeout_sec=0.3)
        assert res.get("code") == "EXECUTION_TIMEOUT"
        assert worker.is_alive()
        assert worker.process is not None
        assert worker.process.pid == pid
        pool.release_worker(worker)

        again = pool.lease_any(2)
        assert again is worker
        nxt = again.execute({"delay": 0}, timeout_sec=2)
        assert nxt.get("status") == "ok"
        assert nxt.get("pid") == pid
        assert again.did_respawn is False
        pool.release_worker(again)
    finally:
        pool.shutdown()


def test_vision_timeout_kills_when_late_frame_never_arrives(tmp_path) -> None:
    """A second timeout still kills a call that never writes its frame."""
    from compute_service.worker_base import BaseProcessPool

    pool = BaseProcessPool(str(_delay_worker(tmp_path)), num_workers=1, worker_name="Vision worker", recover_on_timeout=True)
    try:
        worker = pool.lease_any(2)
        assert worker is not None
        assert worker.process is not None
        pid = worker.process.pid
        res = worker.execute({"delay": 30}, timeout_sec=0.2)
        assert res.get("code") == "EXECUTION_TIMEOUT"
        pool.release_worker(worker)

        again = pool.lease_any(2)
        assert again is worker
        nxt = again.execute({"delay": 0}, timeout_sec=2)
        assert nxt.get("status") == "ok"
        assert nxt.get("pid") != pid
        assert again.did_respawn is True
        pool.release_worker(again)
    finally:
        pool.shutdown()


def test_vision_worker_empty_bytes_not_missing_source() -> None:
    """An empty byte string must not be misclassified as a missing image source."""
    from compute_service.vision_worker import _handle_request

    res = _handle_request({"id": "empty-bytes", "image_bytes": b""})
    assert res.get("code") != "MISSING_IMAGE_SOURCE"


def test_vision_pool_execute_accepts_bytearray() -> None:
    """image_b64 may be passed as a bytearray and should be converted to bytes."""
    from unittest.mock import MagicMock

    pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=1))
    try:
        mock_worker = MagicMock()
        mock_worker.defer_release.return_value = False
        mock_worker.tasks_executed = 0
        payload_received = None

        def fake_exec(payload, timeout_sec):
            nonlocal payload_received
            payload_received = payload
            return {"status": "ok"}

        mock_worker.execute.side_effect = fake_exec
        with pool._cond:
            pool._idle = {mock_worker}

        data = bytearray(b"dummy image bytes")
        res = pool.execute(helper="test", image_b64=data, req_id="bytearray-test")
        assert res.get("status") == "ok"
        assert payload_received is not None
        assert payload_received["image_bytes"] == b"dummy image bytes"
        assert isinstance(payload_received["image_bytes"], bytes)
    finally:
        pool.shutdown()



