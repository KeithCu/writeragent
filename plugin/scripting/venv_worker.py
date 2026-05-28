# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Host-side venv worker: path resolution, warm subprocess, and run_code_in_user_venv."""

from __future__ import annotations

import logging
import os
import pickle
import select
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Dict, IO, Optional, Tuple

from plugin.framework.config import get_config_str
from plugin.scripting.config_limits import (
    configured_python_exec_timeout,
    python_exec_timeout_default,
    resolve_python_exec_timeout,
)
from plugin.scripting.payload_codec import host_unpack_data
from plugin.scripting.subprocess_helpers import optimize_popen_pipes, scrub_subprocess_env, wrap_command_for_sandbox

log = logging.getLogger(__name__)

_TIMEOUT_AFTER = " timed out after "


def _worker_error_message(exc: BaseException) -> str:
    """Build a short user-facing worker error without subprocess command paths."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"Python worker failed: timed out after {exc.timeout} seconds"
    text = str(exc)
    if text.startswith("Command ") and _TIMEOUT_AFTER in text:
        return f"Python worker failed:{text[text.index(_TIMEOUT_AFTER):]}"
    return f"Python worker failed: {text}"


_HARNESS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker_harness.py")
_instances: dict[str, PythonWorkerManager] = {}
_registry_lock = threading.Lock()


# --- Path resolution ---


def resolve_libreoffice_python() -> Optional[str]:
    """Return ``sys.executable`` if it names a real file (no other heuristics).

    Under PyUNO this is normally the office-bundled Python; on broken installs it
    may be wrong or missing — callers surface an error and the user can set a venv.
    """
    exe = (getattr(sys, "executable", None) or "").strip()
    if not exe or not os.path.isfile(exe):
        return None
    if os.name != "nt" and not os.access(exe, os.X_OK):
        return None
    # Reject LibreOffice binaries (soffice, libreoffice, oosplash) that are not Python
    basename = os.path.basename(exe).lower()
    if not basename.startswith("python"):
        return None
    return exe


def resolve_venv_python(venv_dir: str) -> Optional[str]:
    """Return the python executable inside *venv_dir*, or None if missing or not a file."""
    if not venv_dir or not venv_dir.strip():
        return None
    expanded = os.path.expanduser(os.path.expandvars(venv_dir.strip()))
    candidates: list[str] = []
    if os.name == "nt":
        candidates.append(os.path.join(expanded, "Scripts", "python.exe"))
    else:
        bin_dir = os.path.join(expanded, "bin")
        if os.path.isdir(bin_dir):
            for name in ("python", "python3"):
                candidates.append(os.path.join(bin_dir, name))
            for entry in sorted(os.listdir(bin_dir)):
                if entry.startswith("python3."):
                    candidates.append(os.path.join(bin_dir, entry))
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# NOTE for AI agents: The diagnostic script below runs in a sandboxed LocalPythonExecutor.
# Do NOT use dynamic execution primitives like __import__(), eval(), or exec(), as they are
# forbidden by the sandbox and will cause an InterpreterError. Use explicit try/except import blocks.
_DIAGNOSTIC_SCRIPT = """
import platform
res = {'v': platform.python_version(), 'arch': platform.machine(), 'p': {}}
sci = ['numpy', 'pandas', 'scipy', 'sklearn', 'matplotlib', 'sympy']
ui = ['webview', 'jedi', 'PyQt6', 'PyQt6.QtWebEngineWidgets', 'qtpy']
res['sci'] = sci
res['ui'] = ui

# Check for Cython accelerator
try:
    from plugin.scripting.payload_codec import fast_flatten_grid_2d
    res['cython'] = 'optimized' if fast_flatten_grid_2d is not None else 'python'
except ImportError:
    res['cython'] = 'missing'

# Explicit try/except blocks for each package (forbidden to use __import__ loop in sandbox)
try:
    import numpy
    res['p']['numpy'] = 'present'
except ImportError:
    res['p']['numpy'] = None

try:
    import pandas
    res['p']['pandas'] = 'present'
except ImportError:
    res['p']['pandas'] = None

try:
    import scipy
    res['p']['scipy'] = 'present'
except ImportError:
    res['p']['scipy'] = None

try:
    import sklearn
    res['p']['sklearn'] = 'present'
except ImportError:
    res['p']['sklearn'] = None

try:
    import matplotlib
    res['p']['matplotlib'] = 'present'
except ImportError:
    res['p']['matplotlib'] = None

try:
    import sympy
    res['p']['sympy'] = 'present'
except ImportError:
    res['p']['sympy'] = None

try:
    import webview
    res['p']['webview'] = 'present'
except ImportError:
    res['p']['webview'] = None

try:
    import jedi
    res['p']['jedi'] = 'present'
except ImportError:
    res['p']['jedi'] = None

try:
    import PyQt6
    res['p']['PyQt6'] = 'present'
except ImportError:
    res['p']['PyQt6'] = None

try:
    import PyQt6.QtWebEngineWidgets
    res['p']['PyQt6.QtWebEngineWidgets'] = 'present'
except ImportError:
    res['p']['PyQt6.QtWebEngineWidgets'] = None

try:
    import qtpy
    res['p']['qtpy'] = 'present'
except ImportError:
    res['p']['qtpy'] = None

result = res
"""


def _format_self_check_success(data: dict[str, Any]) -> str:
    version = data.get("v", "unknown")
    arch = data.get("arch", "")
    packages = data.get("p", {})
    sci_list = data.get("sci", [])
    ui_list = data.get("ui", [])

    header = f"Python {version} ({arch})" if arch else f"Python {version}"
    msg_lines = [f"{header} responds OK."]

    def format_group(title, keys):
        found = []
        missing = []
        for k in keys:
            if packages.get(k) == "present":
                found.append(k)
            else:
                missing.append(k)

        lines = [f"\n{title}:"]
        if found:
            lines.append(f"  Present: {', '.join(found)}")
        if missing:
            lines.append(f"  Missing: {', '.join(missing)}")
        return lines

    if sci_list:
        msg_lines.extend(format_group("Scientific Libraries", sci_list))
    if ui_list:
        msg_lines.extend(format_group("UI / Monaco Libraries", ui_list))

    return "\n".join(msg_lines)


def run_venv_self_check(python_exe: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """Run a diagnostic script via the warm worker; return (success, user-facing message)."""
    timeout_sec = max(1, int(timeout))
    try:
        manager = PythonWorkerManager.get(python_exe, scrub_subprocess_env(dict(os.environ)))
        response = manager.execute(_DIAGNOSTIC_SCRIPT, timeout_sec=timeout_sec)
    except OSError as e:
        return False, f"Could not run Python: {e}"

    if response.get("status") != "ok":
        msg = str(response.get("message", "Unknown error"))
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            return False, "Timed out waiting for Python (check venv and try again)."
        return False, msg

    data = response.get("result")
    if not isinstance(data, dict):
        return False, f"Unexpected output from test run: {data!r}"

    try:
        return True, _format_self_check_success(data)
    except Exception as e:
        return False, f"Failed to parse diagnostic output: {e}\nRaw output: {data!r}"


def probe_venv_path(venv_dir: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """Resolve *venv_dir* and run a self-check; single entry for UI and tests."""
    if not venv_dir or not str(venv_dir).strip():
        exe = resolve_libreoffice_python()
        if not exe:
            return False, "No process interpreter: sys.executable is missing, not a file, or not executable. Set a venv path in Settings → Python, or fix the LibreOffice install."
        ok, msg = run_venv_self_check(exe, timeout=timeout)
        if ok:
            return True, f"LibreOffice process Python ({exe}) responds OK."
        return ok, msg
    expanded = os.path.expanduser(os.path.expandvars(str(venv_dir).strip()))
    if not os.path.isdir(expanded):
        return False, f"Not a directory: {expanded}"

    exe = resolve_venv_python(expanded)
    if not exe:
        return False, "No python found (expected bin/python or Scripts\\python.exe under that path)."
    return run_venv_self_check(exe, timeout=timeout)


# --- Warm worker ---


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
                        stderr_out = self._drain_stderr()
                        raise RuntimeError(f"Worker closed stdout without a response{stderr_out}")
                    # Trusted IPC: bytes from our own worker_harness child over a private pipe.
                    response = pickle.loads(response_bytes)  # nosec B301
                    return self._normalize_response(response)
                except (BrokenPipeError, pickle.UnpicklingError, RuntimeError, subprocess.TimeoutExpired, OSError) as e:
                    log.warning("Python worker failed (attempt %s): %s", attempt + 1, e)
                    self._terminate_worker()
                    if attempt == 1:
                        return {"status": "error", "message": _worker_error_message(e)}
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
        if sys.platform == "win32":
            popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            popen_kw["preexec_fn"] = os.setsid
        self._proc = subprocess.Popen(wrap_command_for_sandbox([self.exe, _HARNESS_PATH]), **popen_kw)
        optimize_popen_pipes(self._proc)
        log.debug("Started Python worker pid=%s exe=%s", self._proc.pid, self.exe)

    def _read_response_bytes(self, stdout: IO[bytes], timeout_sec: int) -> bytes:
        assert self._proc is not None
        # Windows select.select() only supports sockets, not pipes (raises
        # WinError 10038).  Use a thread-based blocking read there instead.
        if sys.platform == "win32":
            return self._read_response_bytes_threaded(stdout, timeout_sec)
        return self._read_response_bytes_select(stdout, timeout_sec)

    def _read_response_bytes_select(self, stdout: IO[bytes], timeout_sec: int) -> bytes:
        """POSIX path: use select() to poll the pipe with a timeout."""
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

    def _read_response_bytes_threaded(self, stdout: IO[bytes], timeout_sec: int) -> bytes:
        """Windows path: blocking read in a daemon thread with join-timeout."""
        result: list[bytes] = [b""]
        error: list[BaseException | None] = [None]

        def _reader() -> None:
            try:
                header = stdout.read(4)
                if not header or len(header) < 4:
                    return
                size = struct.unpack("!I", header)[0]
                payload = stdout.read(size)
                if len(payload) < size:
                    return
                result[0] = payload
            except Exception as exc:
                error[0] = exc

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout_sec)
        if t.is_alive():
            raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec)
        if error[0] is not None:
            raise error[0]
        return result[0]

    def _drain_stderr(self) -> str:
        """Read any pending stderr from the crashed worker for diagnostics."""
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            # Worker already exited (stdout closed); wait briefly then read stderr.
            self._proc.wait(timeout=2)
        except Exception:
            pass
        try:
            stderr_bytes = self._proc.stderr.read()
        except Exception:
            return ""
        if not stderr_bytes:
            return ""
        text = stderr_bytes.decode("utf-8", errors="replace").strip()
        return f"\nWorker stderr:\n{text}"

    def _terminate_worker(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if sys.platform == "win32":
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


# --- Public entrypoints ---


def run_code_in_user_venv(
    uno_ctx: Any,
    code: str,
    *,
    data: Any = None,
    timeout_sec: int | None = None,
    active_domain: str | None = None,
    python_tool_domain: str | None = None,
) -> Dict[str, Any]:
    """Execute *code* via :class:`PythonWorkerManager` (warm process, isolated namespace per call).

    *active_domain* / *python_tool_domain* are reserved for future venv→LO tool RPC (not wired yet).
    """
    del active_domain, python_tool_domain  # deferred — see docs/enabling_numpy_in_libreoffice.md §7
    if not (code or "").strip():
        return {"status": "error", "message": "No code provided."}

    venv_dir = get_config_str(uno_ctx, "scripting.python_venv_path").strip()
    if venv_dir:
        exe = resolve_venv_python(venv_dir)
        if not exe:
            return {
                "status": "error",
                "message": f"No python executable found under configured venv: {venv_dir!r}",
            }
        log.debug("run_venv_code: using venv interpreter under %s", venv_dir)
    else:
        exe = resolve_libreoffice_python()
        if not exe:
            return {
                "status": "error",
                "message": (
                    "Could not resolve a Python interpreter (sys.executable missing, not a file, or not executable). "
                    "Set scripting.python_venv_path in Settings → Python for a dedicated venv, or fix the LibreOffice install."
                ),
            }
        log.debug("run_venv_code: using process interpreter %s (no venv path set)", exe)

    configured = configured_python_exec_timeout(uno_ctx)
    timeout_sec = resolve_python_exec_timeout(timeout_sec, configured=configured)

    child_env = scrub_subprocess_env(dict(os.environ))
    manager = PythonWorkerManager.get(exe, child_env)
    return manager.execute(code, data=data, timeout_sec=timeout_sec)


def warm_venv_worker(uno_ctx: Any) -> None:
    """Pre-warm the venv subprocess (spawn + trigger auto-imports). Safe to call from a background thread."""
    venv_dir = get_config_str(uno_ctx, "scripting.python_venv_path").strip()
    if venv_dir:
        exe = resolve_venv_python(venv_dir)
    else:
        exe = resolve_libreoffice_python()
    if not exe:
        return
    child_env = scrub_subprocess_env(dict(os.environ))
    manager = PythonWorkerManager.get(exe, child_env)
    manager.warm()
