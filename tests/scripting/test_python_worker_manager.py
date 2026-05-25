# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

import io
import os
import pickle
import struct
import subprocess
import threading
import time

import pytest

from plugin.scripting.python_worker_manager import PythonWorkerManager


@pytest.fixture(autouse=True)
def _shutdown_python_workers():
    yield
    PythonWorkerManager.shutdown_all()


def _pack_response(obj: dict) -> bytes:
    """Encode a response the same way worker_harness.py does."""
    payload = pickle.dumps(obj, protocol=5)
    return struct.pack("!I", len(payload)) + payload


class TestReadResponseBytesThreaded:
    """Tests for the Windows-safe threaded reader."""

    def test_reads_valid_response(self):
        response = {"status": "ok", "result": 42, "id": "test"}
        raw = _pack_response(response)
        stdout = io.BytesIO(raw)
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True  # just needs to be non-None for the assert
        got = mgr._read_response_bytes_threaded(stdout, timeout_sec=5)
        assert got
        decoded = pickle.loads(got)
        assert decoded["status"] == "ok"
        assert decoded["result"] == 42

    def test_returns_empty_on_eof(self):
        stdout = io.BytesIO(b"")
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True
        got = mgr._read_response_bytes_threaded(stdout, timeout_sec=2)
        assert got == b""

    def test_returns_empty_on_short_header(self):
        stdout = io.BytesIO(b"\x00\x00")
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True
        got = mgr._read_response_bytes_threaded(stdout, timeout_sec=2)
        assert got == b""

    def test_returns_empty_on_truncated_payload(self):
        header = struct.pack("!I", 100)
        stdout = io.BytesIO(header + b"short")
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True
        got = mgr._read_response_bytes_threaded(stdout, timeout_sec=2)
        assert got == b""

    def test_timeout_raises(self):
        """A blocking read that never yields data should raise TimeoutExpired."""
        class SlowIO(io.RawIOBase):
            def readable(self):
                return True
            def readinto(self, b):
                time.sleep(10)
                return 0
        slow = io.BufferedReader(SlowIO())
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True
        with pytest.raises(subprocess.TimeoutExpired):
            mgr._read_response_bytes_threaded(slow, timeout_sec=1)

    def test_propagates_read_error(self):
        class ErrorIO(io.RawIOBase):
            def readable(self):
                return True
            def readinto(self, b):
                raise IOError("pipe broken")
        broken = io.BufferedReader(ErrorIO())
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True
        with pytest.raises(IOError, match="pipe broken"):
            mgr._read_response_bytes_threaded(broken, timeout_sec=2)


@pytest.mark.skipif(os.name == "nt", reason="select.select() does not support pipes/BytesIO on Windows")
class TestReadResponseBytesSelect:
    """Tests for the POSIX select-based reader."""

    def test_reads_valid_response(self):
        response = {"status": "ok", "result": "hello", "id": "test"}
        raw = _pack_response(response)
        stdout = io.BytesIO(raw)
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        # _read_response_bytes_select needs _proc with a poll() method
        class FakeProc:
            def poll(self):
                return None
        mgr._proc = FakeProc()
        got = mgr._read_response_bytes_select(stdout, timeout_sec=5)
        assert got
        decoded = pickle.loads(got)
        assert decoded["result"] == "hello"


class TestExecuteOSErrorRetry:
    """Verify that OSError in the execute loop triggers retry instead of propagation."""

    def test_oserror_retried(self):
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = None
        mgr._io_lock = threading.Lock()
        mgr.env = {}

        call_count = [0]
        original_ensure = mgr._ensure_running

        def fake_ensure():
            call_count[0] += 1
            raise OSError("[WinError 10038] not a socket")

        mgr._ensure_running = fake_ensure
        mgr._terminate_worker = lambda: None
        result = mgr.execute("result = 1", timeout_sec=1)
        assert result["status"] == "error"
        assert "10038" in result["message"]
        assert call_count[0] == 2  # retried once
