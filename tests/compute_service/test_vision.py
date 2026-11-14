# WriterAgent - Python Compute Service Vision Pool tests
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import base64
import contextlib
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

from compute_service.config import ComputeSettings, MAX_BODY_BYTES, read_allowlisted_file
from compute_service.server import WSGIDualStackServer, create_wsgi_app
from plugin.framework.child_runtime import Deadline
from compute_service.vision import VisionProcessPool, _handle_request, get_vision_pool, shutdown_vision_pool
from compute_service.config import COMPUTE_MAX_PAYLOAD_BYTES
from tests.compute_service.multipart_encode import encode_multipart_vision


from tests.compute_service.conftest import get_free_port


# Minimal 1x1 PNG base64 for testing
_TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


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

    Otherwise a result over 16 MiB breaks the child loop and the host sees
    EMPTY_RESPONSE.
    """
    import io

    from plugin.framework.ipc import DEFAULT_MAX_PAYLOAD_BYTES, read_pickle_frame, write_pickle_frame
    from compute_service.vision import main

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

    monkeypatch.setattr("compute_service.vision._handle_request", handle)
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
    path.write_bytes(b"x" * (MAX_BODY_BYTES + 1))
    data, err = read_allowlisted_file(str(path), (str(tmp_path),), max_bytes=MAX_BODY_BYTES)
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

    def test_zero_timeout_is_pool_busy(self) -> None:
        """timeout_sec=0 expires immediately instead of falling back to the OCR default."""
        pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=1, ocr_timeout_sec=30))
        try:
            res = pool.execute(helper="extract_text", image=b"\x89PNG", timeout_sec=0, req_id="zero")
            assert res.get("id") == "zero"
            assert res.get("code") == "VISION_POOL_BUSY"
        finally:
            pool.shutdown()

    def test_pool_disabled(self) -> None:
        pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=0))
        assert not pool.is_enabled()
        res = pool.execute(helper="extract_text", image=base64.b64decode(_TINY_PNG_B64), req_id="v-disabled")
        assert res.get("status") == "error"
        assert res.get("code") == "VISION_SERVICE_DISABLED"

    def test_pool_file_path_not_found(self) -> None:
        pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=1, ocr_timeout_sec=15))
        try:
            denied = pool.execute(helper="extract_text", file_path="/tmp/non_existent_12345.png", req_id="v-denied")
            assert denied.get("status") == "error"
            assert denied.get("code") == "FILE_PATH_DENIED"
            missing = pool.execute(helper="extract_text", file_path="/tmp/non_existent_12345.png", req_id="v-missing", allow_paths=("/tmp",))
            assert missing.get("status") == "error"
            assert missing.get("code") == "FILE_NOT_FOUND"
        finally:
            pool.shutdown()

    def test_busy_lease_waits_until_worker_is_free(self) -> None:
        pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=1, ocr_timeout_sec=15))
        held = pool.lease_any(timeout_sec=1.0)
        assert held is not None
        result: list[dict] = []

        def _run() -> None:
            result.append(pool.execute(helper="extract_text", image=base64.b64decode(_TINY_PNG_B64), req_id="v-wait", timeout_sec=15))

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

    def test_worker_missing_ocr_backend_module(self) -> None:
        docling = "plugin.vision.venv.vision_docling"
        swallowed = {"status": "error", "code": "VISION_ERROR", "message": f"No module named '{docling}'", "helper": "extract_text", "details": {"missing_module": docling}}
        with patch("plugin.vision.venv.vision.run_vision", return_value=swallowed):
            out2 = _handle_request({"id": "miss-dict", "helper": "extract_text", "image_bytes": b"x"})
        assert out2.get("code") == "VISION_BACKEND_MISSING"
        assert docling in str(out2.get("error"))

        boom = {"status": "error", "code": "VISION_ERROR", "message": "boom", "helper": "extract_text"}
        with patch("plugin.vision.venv.vision.run_vision", return_value=boom):
            out3 = _handle_request({"id": "vision-err", "helper": "extract_text", "image_bytes": b"x"})
        assert out3.get("code") == "VISION_ERROR"

        with patch("plugin.vision.venv.vision.run_vision", side_effect=RuntimeError("boom")):
            out4 = _handle_request({"id": "worker-boom", "helper": "extract_text", "image_bytes": b"x"})
        assert out4.get("code") == "VISION_WORKER_ERROR"

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
            data, err = read_allowlisted_file(str(inside), [str(allowed)], max_bytes=MAX_BODY_BYTES)
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


class TestVisionHttpEndpoint:
    @staticmethod
    @contextlib.contextmanager
    def _running_server(**setting_overrides):
        port = get_free_port()
        settings = ComputeSettings(
            **{
                "host": "127.0.0.1",
                "port": port,
                "api_key": "vision-secret",
                "ocr_workers": 1,
                **setting_overrides,
            }
        )
        app = create_wsgi_app(settings)
        server = WSGIDualStackServer("127.0.0.1", port, listener_threads=8)
        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            shutdown_vision_pool()

    @pytest.fixture(scope="class")
    def vision_server(self):
        with self._running_server() as url:
            yield url

    def _post(self, url: str, meta: dict, image: bytes | None = None, headers: dict | None = None) -> tuple[int, dict]:
        content_type, data = encode_multipart_vision(meta, image)
        req_headers = {"Content-Type": content_type}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(f"{url}/v1/vision", data=data, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return resp.status, body
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode("utf-8"))
            return e.code, body

    def test_vision_auth_required(self, vision_server: str) -> None:
        status, body = self._post(vision_server, {"id": "auth-test"}, image=base64.b64decode(_TINY_PNG_B64))
        assert status == 401
        assert body["status"] == "error"

    def test_vision_round_trip_pool_and_http(self, monkeypatch, tmp_path) -> None:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        stub_worker = tmp_path / "stub_vision_worker.py"
        stub_worker.write_text(
            f"import sys, types\n"
            f"sys.path.insert(0, {repo_root!r})\n"
            f"mod = types.ModuleType('plugin.vision.venv.vision')\n"
            f"mod.run_vision = lambda spec, image: {{'status': 'ok', 'text': f\"{{spec['helper']}}:{{len(image)}}\"}}\n"
            f"sys.modules['plugin.vision.venv.vision'] = mod\n"
            f"from compute_service.vision import main\n"
            f"raise SystemExit(main())\n",
            encoding="utf-8",
        )
        # Stub only run_vision so the test exercises pool, stdio framing, and HTTP without OCR dependencies.
        monkeypatch.setattr("compute_service.vision._WORKER_SCRIPT", str(stub_worker))

        img = base64.b64decode(_TINY_PNG_B64)
        img_path = tmp_path / "allowed.png"
        img_path.write_bytes(img)
        pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=1, ocr_timeout_sec=15))
        try:
            res = pool.execute(helper="extract_text", image=img, req_id="v-1")
            assert res == {"status": "ok", "text": f"extract_text:{len(img)}", "id": "v-1"}
        finally:
            pool.shutdown()

        with self._running_server(ocr_allow_paths=(str(tmp_path),)) as url:
            status, body = self._post(
                url,
                {"id": "test-ocr-1", "helper": "extract_text"},
                image=img,
                headers={"Authorization": "Bearer vision-secret"},
            )
            assert status == 200
            assert body == {"status": "ok", "text": f"extract_text:{len(img)}", "id": "test-ocr-1"}

            status_file, body_file = self._post(
                url,
                {"id": "test-ocr-file-ok", "helper": "extract_text", "file_path": str(img_path)},
                headers={"Authorization": "Bearer vision-secret"},
            )
            assert status_file == 200
            assert body_file == {"status": "ok", "text": f"extract_text:{len(img)}", "id": "test-ocr-file-ok"}

    def test_vision_file_path_denied_by_default(self, vision_server: str, tmp_path) -> None:
        img_path = tmp_path / "endpoint_img.png"
        img_path.write_bytes(base64.b64decode(_TINY_PNG_B64))

        status, body = self._post(vision_server, {"id": "test-ocr-file", "helper": "extract_text", "file_path": str(img_path)}, headers={"Authorization": "Bearer vision-secret"})
        assert status == 400
        assert body.get("code") == "FILE_PATH_DENIED"

    def test_vision_missing_image(self, vision_server: str) -> None:
        status, body = self._post(vision_server, {"id": "test-ocr-missing", "helper": "extract_text"}, headers={"Authorization": "Bearer vision-secret"})
        assert status == 400
        assert body["status"] == "error"
        assert "Missing image input" in body["error"]

    def test_vision_endpoint_disabled_when_zero_workers(self) -> None:
        """When ocr_workers=0 (the default), /v1/vision returns HTTP 501 VISION_SERVICE_DISABLED."""
        with self._running_server(ocr_workers=0) as url:
            status, body = self._post(url, {"id": "test-zero-workers", "helper": "extract_text"}, image=base64.b64decode(_TINY_PNG_B64), headers={"Authorization": "Bearer vision-secret"})
            assert status == 501
            assert body.get("status") == "error"
            assert body.get("code") == "VISION_SERVICE_DISABLED"
            assert "ocr_workers=0" in body.get("error", "")


def test_vision_pool_execute_accepts_bytearray() -> None:
    """image_b64 may be passed as a bytearray."""
    from unittest.mock import MagicMock

    pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=1))
    try:
        mock_worker = MagicMock()
        mock_worker.tasks_executed = 0
        payload_received = None

        def fake_exec(payload, timeout_sec, **kwargs):
            nonlocal payload_received
            payload_received = payload
            return {"status": "ok"}

        mock_worker.execute.side_effect = fake_exec
        with pool._cond:
            pool._idle = OrderedDict([(mock_worker, time.monotonic())])

        data = bytearray(b"dummy image bytes")
        res = pool.execute(helper="test", image=data, req_id="bytearray-test")
        assert res.get("status") == "ok"
        assert payload_received is not None
        assert payload_received["image_bytes"] == b"dummy image bytes"
        assert isinstance(payload_received["image_bytes"], bytearray)
    finally:
        pool.shutdown()


def test_vision_expired_deadline_does_not_execute() -> None:
    from unittest.mock import MagicMock

    pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=1))
    try:
        mock_worker = MagicMock()
        mock_worker.tasks_executed = 0
        mock_worker.execute.return_value = {"status": "ok"}
        with pool._cond:
            pool._idle = OrderedDict([(mock_worker, time.monotonic())])
        res = pool.execute(helper="test", image=base64.b64decode(_TINY_PNG_B64), deadline=Deadline.from_absolute(30.0, time.monotonic() - 1))
        assert res.get("code") == "VISION_POOL_BUSY"
        mock_worker.execute.assert_not_called()
    finally:
        pool.shutdown()


def test_one_second_budget_with_time_left_still_runs() -> None:
    """A 1s request with 0.4s left still runs. That is not VISION_POOL_BUSY.

    usable() stays set for a one-second budget. run_for() lifts that
    remainder to 1s so execute is not given a sub-second clock.
    """
    from unittest.mock import MagicMock

    pool = VisionProcessPool(settings=ComputeSettings(ocr_workers=0, idle_worker_ttl_sec=0))
    pool.num_workers = 1
    mock_worker = MagicMock()
    mock_worker.tasks_executed = 0
    mock_worker.is_alive.return_value = True
    mock_worker.execute.return_value = {"status": "ok", "id": "slip"}
    try:
        with pool._cond:
            pool._idle[mock_worker] = time.monotonic()
        res = pool.execute(helper="extract_text", image=b"\x89PNG", deadline=Deadline.from_absolute(1.0, time.monotonic() + 0.4), req_id="slip")
        assert res.get("code") != "VISION_POOL_BUSY"
        assert res.get("code") != "EXECUTION_TIMEOUT"
        assert res.get("id") == "slip"
        mock_worker.execute.assert_called_once()
        assert mock_worker.execute.call_args.kwargs["timeout_sec"] >= 1.0
        assert mock_worker.execute.call_args.kwargs["req_id"] == "slip"
    finally:
        pool.shutdown()
