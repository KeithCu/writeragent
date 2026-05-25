# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Persistent venv subprocess with a fresh namespace on every execute (see worker_harness.py)."""

from __future__ import annotations

import pickle
import struct
import logging
import os
import select
import signal
import subprocess
import threading
import time
import uuid
from typing import Any, IO

from plugin.scripting.payload_codec import host_unpack_data
from plugin.scripting.timeout_limits import python_exec_timeout_default

log = logging.getLogger(__name__)

_HARNESS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker_harness.py")
_instances: dict[str, PythonWorkerManager] = {}
_registry_lock = threading.Lock()


class PythonWorkerManager:
    """One warm child process per resolved Python executable path."""

    def __init__(self, exe: str, env: dict[str, str]) -> None:
        self.exe = exe
        self.env = dict(env)
        self._proc: subprocess.Popen[Any] | None = None
        self._io_lock = threading.Lock()

    @classmethod
    def get(cls, exe: str, env: dict[str, str]) -> PythonWorkerManager:
        """Return the singleton worker for *exe* (caller should pass a scrubbed env dict)."""
        with _registry_lock:
            mgr = _instances.get(exe)
            if mgr is None:
                mgr = cls(exe, dict(env))
                _instances[exe] = mgr
            return mgr

    @classmethod
    def shutdown_all(cls) -> None:
        """Terminate all workers (tests / extension teardown)."""
        with _registry_lock:
            for mgr in list(_instances.values()):
                mgr._terminate_worker()
            _instances.clear()

    def warm(self) -> None:
        """Spawn the worker and trigger auto-imports (numpy etc.) so the next real execute is instant."""
        self.execute("result = None", timeout_sec=30)

    def execute(self, code: str, *, data: Any = None, timeout_sec: int | None = None) -> dict[str, Any]:
        """Run *code* in the warm worker; state from prior calls is not visible."""
        if timeout_sec is None:
            timeout_sec = python_exec_timeout_default()
        request: dict[str, Any] = {"id": str(uuid.uuid4()), "code": code}
        if data is not None:
            request["data"] = data

        payload = pickle.dumps(request, protocol=5)
        header = struct.pack("!I", len(payload))

        with self._io_lock:
            for attempt in range(2):
                try:
                    self._ensure_running()
                    assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None
                    stdin = self._proc.stdin
                    stdout = self._proc.stdout
                    stdin.write(header)
                    stdin.write(payload)
                    stdin.flush()

                    response_bytes = self._read_response_bytes(stdout, timeout_sec)
                    if not response_bytes:
                        raise RuntimeError("Worker closed stdout without a response")
                    # Trusted IPC: bytes from our own worker_harness child over a private pipe.
                    response = pickle.loads(response_bytes)  # nosec B301
                    return self._normalize_response(response)
                except (BrokenPipeError, pickle.UnpicklingError, RuntimeError, subprocess.TimeoutExpired) as e:
                    log.warning("Python worker failed (attempt %s): %s", attempt + 1, e)
                    self._terminate_worker()
                    if attempt == 1:
                        return {"status": "error", "message": f"Python worker failed: {e}"}
            return {"status": "error", "message": "Python worker failed"}

    def _normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        if response.get("status") == "ok":
            result = response.get("result")
            if result is not None:
                result = host_unpack_data(result, as_nested_list=True)
            return {
                "status": "ok",
                "result": result,
                "stdout": (response.get("stdout") or "").strip(),
                "stderr": "",
            }
        msg = response.get("message") or response.get("error") or "Unknown worker error"
        tb = response.get("traceback")
        if tb and isinstance(tb, str):
            msg = f"{msg}\n{tb.strip()}"
        out: dict[str, Any] = {
            "status": "error",
            "message": str(msg),
            "stdout": (response.get("stdout") or "").strip(),
        }
        return out

    def _ensure_running(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._terminate_worker()
        popen_kw: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": self.env,
            "text": False,
            "bufsize": 0,
        }
        if os.name != "nt":
            popen_kw["preexec_fn"] = os.setsid
        self._proc = subprocess.Popen([self.exe, _HARNESS_PATH], **popen_kw)
        log.debug("Started Python worker pid=%s exe=%s", self._proc.pid, self.exe)

    def _read_response_bytes(self, stdout: IO[bytes], timeout_sec: int) -> bytes:
        assert self._proc is not None
        end = time.time() + timeout_sec

        def _read_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                if time.time() >= end:
                    raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec)
                remaining = end - time.time()
                ready, _, _ = select.select([stdout], [], [], min(1.0, remaining))
                if ready:
                    chunk = stdout.read(n - len(buf))
                    if not chunk:
                        return bytes()
                    buf.extend(chunk)
                if self._proc is not None and self._proc.poll() is not None and not ready:
                    break
            return bytes(buf)

        header = _read_exact(4)
        if len(header) < 4:
            return b""

        size = struct.unpack("!I", header)[0]
        payload = _read_exact(size)
        if len(payload) < size:
            return b""

        return payload

    def _terminate_worker(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if os.name == "nt":
                    proc.kill()
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        proc.kill()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

