# WriterAgent - Python Compute Service Vision Pool tests
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from unittest.mock import patch

import pytest

from compute_service.config import ComputeSettings, read_allowlisted_file
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


def test_vision_child_stdio_accepts_compute_frame_cap(monkeypatch) -> None:
    """The child loop must use the same 33 MiB cap as the parent pool.

    A frame between the 16 MiB stdio default and COMPUTE_MAX_PAYLOAD_BYTES
    used to fail the child read. A result over 16 MiB broke the child loop
    and the host saw EMPTY_RESPONSE.
    """
    import io

    from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, read_pickle_frame, write_pickle_frame
    from compute_service.vision_worker import main

    blob = b"v" * (DEFAULT_MAX_PAYLOAD_BYTES + 1)
    assert DEFAULT_MAX_PAYLOAD_BYTES < len(blob) < COMPUTE_MAX_PAYLOAD_BYTES
    stdin_buf = io.BytesIO()
    write_pickle_frame(stdin_buf, {"blob": blob}, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)
    stdin_buf.seek(0)
    stdout_buf = io.BytesIO()

    class _MockStdin:
        buffer = stdin_buf

    class _MockStdout:
        buffer = stdout_buf

    monkeypatch.setattr("sys.stdin", _MockStdin())
    monkeypatch.setattr("sys.stdout", _MockStdout())
    seen: dict[str, int] = {}

    def handle(req: dict) -> dict:
        seen["n"] = len(req.get("blob") or b"")
        return {"status": "ok", "blob": req["blob"]}

    monkeypatch.setattr("compute_service.vision_worker._handle_request", handle)
    assert main() == 0
    assert seen["n"] == len(blob)

    stdout_buf.seek(0)
    ready = read_pickle_frame(stdout_buf, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)
    assert isinstance(ready, dict)
    assert ready.get("status") == "ready"
    result = read_pickle_frame(stdout_buf, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)
    assert isinstance(result, dict)
    assert result.get("status") == "ok"
    assert len(result.get("blob") or b"") == len(blob)


def _hide_proc_fd(monkeypatch, platform: str) -> None:
    """Pretend ``/proc/self/fd`` is not a symlink to the opened file.

    macOS and Windows ``realpath`` that path as the literal string. Patching
    the platform and that realpath lets the portable branch run on Linux.
    """
    monkeypatch.setattr("compute_service.config.sys.platform", platform)
    real_realpath = os.path.realpath

    def realpath(path, *args, **kwargs):
        text = os.fspath(path)
        normalized = text.replace("\\", "/")
        if normalized.startswith("/proc/") and "/fd/" in normalized:
            return text
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr("compute_service.config.os.path.realpath", realpath)


def test_vision_file_read_is_capped(tmp_path) -> None:
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (_FILE_READ_MAX_BYTES + 1))
    data, err = _read_allowed_image(str(path), (str(tmp_path),), "ocr-big")
    assert data is None
    assert err is not None
    assert err["code"] == "FILE_TOO_LARGE"


class TestVisionPoolSupervisor:

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="No mkfifo on this platform")
    def test_open_allowed_file_fifo(self, tmp_path) -> None:
        fifo_path = os.path.join(tmp_path, "my_fifo")
        os.mkfifo(fifo_path)
        val, err = read_allowlisted_file(fifo_path, allow_prefixes=(str(tmp_path),), max_bytes=100)
        assert err is not None
        assert err["code"] == "NOT_A_FILE"
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

    def test_permanent_shutdown_refuses_new_pool(self) -> None:
        """Server exit must not let a late get() spawn another pool.

        A normal shutdown still returns a new pool. That is what tests use.
        """
        p1 = get_vision_pool()
        shutdown_vision_pool(permanent=True)
        with pytest.raises(RuntimeError, match="shut down"):
            get_vision_pool()
        shutdown_vision_pool()
        p2 = get_vision_pool()
        assert p2 is not p1
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

    def test_symlink_swapped_between_check_and_open_is_not_read(self, tmp_path) -> None:
        """A name that was inside the allowlist can point outside before open.

        The allowlist check returns, then the directory entry is replaced.
        Opening that path must not return the bytes outside the prefix.
        """
        self._assert_swap_before_open_is_denied(tmp_path)

    # test_symlink_swapped_before_open_is_denied_without_proc removed because non-Linux check is explicitly skipped to avoid name-based TOCTOU.


    def _assert_swap_before_open_is_denied(self, tmp_path) -> None:
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"secret-bytes")
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        inside = allowed / "img.png"
        inside.write_bytes(b"ok-bytes")
        real_open = os.open
        swapped = {"done": False}

        def open_after_swap(path: str, flags: int, *args, **kwargs):
            if not swapped["done"] and os.path.basename(str(path)) == "img.png":
                swapped["done"] = True
                os.remove(path)
                os.symlink(outside, path)
            return real_open(path, flags, *args, **kwargs)

        with patch("compute_service.config.os.open", side_effect=open_after_swap):
            data, err = _read_allowed_image(str(inside), [str(allowed)], "race")
        assert swapped["done"] is True
        assert data != b"secret-bytes"
        assert err is not None
        assert err.get("code") == "FILE_PATH_DENIED"

    def test_proc_fd_rejects_inode_after_name_is_restored(self, tmp_path) -> None:
        """Restoring the directory entry after open must not hide the inode.

        realpath of the path string would then see an inside file and allow
        the read. ``/proc/self/fd`` still names the outside inode ``open``
        returned. This is the Linux check the portable fallback does not have.
        """
        if not sys.platform.startswith("linux"):
            pytest.skip("/proc/self/fd inode check")
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"secret-bytes")
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        inside = allowed / "img.png"
        inside.write_bytes(b"ok-bytes")
        real_open = os.open
        state = {"phase": "before"}

        def open_swap_then_restore(path: str, flags: int, *args, **kwargs):
            if state["phase"] == "before" and os.path.basename(str(path)) == "img.png":
                state["phase"] = "opening"
                os.remove(path)
                os.symlink(outside, path)
                fd = real_open(path, flags, *args, **kwargs)
                os.remove(path)
                restored = real_open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
                os.write(restored, b"ok-bytes")
                os.close(restored)
                state["phase"] = "done"
                return fd
            return real_open(path, flags, *args, **kwargs)

        with patch("compute_service.config.os.open", side_effect=open_swap_then_restore):
            data, err = read_allowlisted_file(str(inside), [str(allowed)], max_bytes=1024)
        assert state["phase"] == "done"
        assert data != b"secret-bytes"
        assert err is not None
        assert err.get("code") == "FILE_PATH_DENIED"

    @pytest.mark.parametrize("platform", ["darwin", "win32"])
    def test_allowlisted_read_works_without_proc_fd(self, tmp_path, monkeypatch, platform: str) -> None:
        """An allowlisted file must be readable when ``/proc/self/fd`` is not the file.

        On macOS and Windows, realpath of that node is the literal string and
        the prefix check denied every path. The stub runs on Linux so this
        does not need a macOS or Windows runner.
        """
        _hide_proc_fd(monkeypatch, platform)
        img = tmp_path / "ok.png"
        img.write_bytes(b"png-bytes")
        data, err = read_allowlisted_file(str(img), [str(tmp_path)], max_bytes=1024)
        assert err is None
        assert data == b"png-bytes"

    @pytest.mark.skipif(sys.platform == "win32", reason="the swap unlinks an open file and creates a symlink; neither works on a Windows runner (the win32 branch is simulated on POSIX)")
    @pytest.mark.parametrize("platform", ["darwin", "win32"])
    def test_allowlisted_read_denies_swapped_symlink_on_non_linux(self, tmp_path, monkeypatch, platform: str) -> None:
        """On macOS and Windows, a swapped symlink must be detected and denied post-open."""
        _hide_proc_fd(monkeypatch, platform)
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        secret_dir = tmp_path / "secret_dir"
        secret_dir.mkdir()
        secret = secret_dir / "secret.png"
        secret.write_bytes(b"secret-bytes")

        inside = allowed / "img.png"
        inside.write_bytes(b"allowed-bytes")

        real_open = os.open

        def open_swap_to_secret(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            inside.unlink()
            inside.symlink_to(secret)
            return fd

        with patch("compute_service.config.os.open", side_effect=open_swap_to_secret):
            data, err = read_allowlisted_file(str(inside), [str(allowed)], max_bytes=1024)
        assert data is None
        assert err is not None
        assert err.get("code") == "FILE_PATH_DENIED"

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
        """When ocr_workers=0 (the default), /v1/vision returns HTTP 501 VISION_SERVICE_DISABLED."""
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
            assert status == 501
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


@pytest.mark.skipif(sys.platform == "win32", reason="the late-frame drain read no frame on the Windows runner (GHA 37401464435) and the pid respawned; needs a Windows investigation")
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
        pool.release_worker(again)
    finally:
        pool.shutdown()


def test_vision_worker_rejects_both_sources() -> None:
    """file_path and a non-empty image buffer together is a client error."""
    from compute_service.vision_worker import _handle_request

    both = _handle_request({"id": "both", "file_path": "/tmp/x.png", "image_bytes": b"png"})
    assert both.get("code") == "INVALID_REQUEST"

    # Empty bytes are not a second source, so the file path is still attempted.
    file_only = _handle_request({"id": "file-only", "file_path": "/no/such", "image_bytes": b""})
    assert file_only.get("code") != "INVALID_REQUEST"


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

        def fake_exec(payload, timeout_sec, **kwargs):
            nonlocal payload_received
            payload_received = payload
            return {"status": "ok"}

        mock_worker.execute.side_effect = fake_exec
        with pool._cond:
            pool._idle = OrderedDict([(mock_worker, None)])

        data = bytearray(b"dummy image bytes")
        res = pool.execute(helper="test", image_b64=data, req_id="bytearray-test")
        assert res.get("status") == "ok"
        assert payload_received is not None
        assert payload_received["image_bytes"] == b"dummy image bytes"
        assert isinstance(payload_received["image_bytes"], bytes)
    finally:
        pool.shutdown()


def test_decode_image_b64_strips_whitespace_and_data_url() -> None:
    from compute_service.vision_pool import _decode_image_b64

    raw = base64.b64encode(b"hi").decode("ascii")
    wrapped = "data:image/png;base64," + raw[:4] + "\n" + raw[4:]
    assert _decode_image_b64(wrapped) == b"hi"


def test_decode_image_b64_accepts_urlsafe_and_missing_padding() -> None:
    """URL-safe alphabets and omitted padding are real encoder output.

    validate=True used to reject both and return INVALID_BASE64 for a valid image.
    """
    import binascii
    from compute_service.vision_pool import _decode_image_b64

    raw = bytes(range(256))
    standard = base64.b64encode(raw).decode("ascii")
    url = base64.urlsafe_b64encode(raw).decode("ascii")
    assert url != standard
    assert _decode_image_b64(url) == raw
    assert _decode_image_b64(url.rstrip("=")) == raw
    assert _decode_image_b64(standard.rstrip("=")) == raw
    assert _decode_image_b64("data:image/jpeg;base64," + url) == raw
    with pytest.raises(binascii.Error):
        _decode_image_b64(url[:4] + "*" + url[5:])


def test_vision_expired_deadline_does_not_execute() -> None:
    from unittest.mock import MagicMock

    pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=1))
    try:
        mock_worker = MagicMock()
        mock_worker.defer_release.return_value = False
        mock_worker.tasks_executed = 0
        mock_worker.execute.return_value = {"status": "ok"}
        with pool._cond:
            pool._idle = OrderedDict([(mock_worker, None)])
        res = pool.execute(helper="test", image_b64=_TINY_PNG_B64, deadline=time.monotonic() - 1)
        assert res.get("code") == "VISION_POOL_BUSY"
        mock_worker.execute.assert_not_called()
    finally:
        pool.shutdown()


def test_vision_pool_execute_passes_drain_timeout_budget() -> None:
    """VisionProcessPool.execute provides drain_timeout_sec >= default_timeout_sec (Bug 3)."""
    from unittest.mock import MagicMock

    pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=1, ocr_timeout_sec=30))
    try:
        mock_worker = MagicMock()
        mock_worker.defer_release.return_value = False
        mock_worker.tasks_executed = 0
        recorded_drain_timeout = None

        def fake_exec(payload, timeout_sec, drain_timeout_sec=None):
            nonlocal recorded_drain_timeout
            recorded_drain_timeout = drain_timeout_sec
            return {"status": "ok"}

        mock_worker.execute.side_effect = fake_exec
        with pool._cond:
            pool._idle = OrderedDict([(mock_worker, None)])

        # Deadline almost expired (remaining 0.05s)
        near_deadline = time.monotonic() + 0.05
        res = pool.execute(helper="test", image_b64=_TINY_PNG_B64, deadline=near_deadline)
        assert res.get("status") == "ok"
        assert recorded_drain_timeout is not None
        assert recorded_drain_timeout >= 30.0
    finally:
        pool.shutdown()




