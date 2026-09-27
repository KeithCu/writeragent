# WriterAgent - Python Compute Service Vision Pool tests
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import base64
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from compute_service.config import ComputeSettings
from compute_service.server import WSGIDualStackServer, create_wsgi_app
from compute_service.vision_pool import (
    VisionProcessPool,
    get_vision_pool,
    shutdown_vision_pool,
)


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Minimal 1x1 PNG base64 for testing
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def cleanup_vision_pool():
    yield
    shutdown_vision_pool()


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

    def test_pool_lifecycle(self) -> None:
        pool = VisionProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            assert pool.is_enabled()
            assert len(pool.workers) == 1
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


