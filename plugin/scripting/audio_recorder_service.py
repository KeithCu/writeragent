# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side helpers for venv microphone recording subprocess."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any

from plugin.framework.config import get_config_str
from plugin.scripting.sandbox import resolve_venv_python, scrub_subprocess_env, wrap_command_for_sandbox

log = logging.getLogger(__name__)

_AUDIO_RECORD_MAIN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "venv", "audio_record_main.py"
)
_RECORDING_READY_TIMEOUT_SEC = 30
_RECORDING_STOP_TIMEOUT_SEC = 15

_VENV_NOT_CONFIGURED = (
    "Set the Python venv path in WriterAgent Settings → Python, then run "
    "'uv pip install sounddevice' in that venv."
)


def is_audio_recording_configured(ctx: Any) -> bool:
    """True when Settings → Python points at a venv with a python executable."""
    del ctx
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    return resolve_venv_python(venv_dir) is not None


def resolve_recording_python(ctx: Any) -> tuple[str | None, str]:
    """Return (venv python executable, error message)."""
    del ctx
    venv_dir = get_config_str("scripting.python_venv_path").strip()
    if not venv_dir:
        return None, _VENV_NOT_CONFIGURED
    exe = resolve_venv_python(venv_dir)
    if not exe:
        return (
            None,
            f"No python executable found under configured venv: {venv_dir!r} "
            "(expected bin/python, bin/python3, or bin/python3.x).",
        )
    return exe, ""


def _build_recording_env() -> dict[str, str]:
    env = scrub_subprocess_env(dict(os.environ))
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER"):
        if key in os.environ and key not in env:
            env[key] = os.environ[key]
    return env


def _popen_kwargs() -> dict[str, Any]:
    popen_kw: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": _build_recording_env(),
        "text": True,
        "bufsize": 1,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kw["preexec_fn"] = os.setsid
    return popen_kw


def spawn_recording_process(exe: str, output_path: str) -> subprocess.Popen[str]:
    """Start audio_record_main.py in the user venv."""
    cmd = wrap_command_for_sandbox([exe, _AUDIO_RECORD_MAIN, "--output", output_path])
    return subprocess.Popen(cmd, **_popen_kwargs())


def _read_json_line(proc: subprocess.Popen[str], timeout: float) -> dict[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("Recording subprocess stdout is not available.")
    try:
        line = proc.stdout.readline()
    except Exception as exc:
        raise RuntimeError(f"Failed to read from recording subprocess: {exc}") from exc
    if not line:
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        code = proc.poll()
        detail = stderr.strip() or f"exit code {code}"
        raise RuntimeError(f"Recording subprocess ended before responding ({detail}).")
    try:
        payload = json.loads(line.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid recording subprocess response: {line!r}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected recording subprocess payload: {payload!r}")
    return payload


def wait_for_recording_ready(proc: subprocess.Popen[str], *, timeout_sec: float = _RECORDING_READY_TIMEOUT_SEC) -> None:
    """Block until the child emits ``{\"status\": \"ready\"}`` or fails."""
    payload = _read_json_line(proc, timeout_sec)
    status = payload.get("status")
    if status == "ready":
        return
    if status == "error":
        raise RuntimeError(str(payload.get("message") or "Audio recording failed to start."))
    raise RuntimeError(f"Unexpected recording subprocess status: {status!r}")


def stop_recording_process(
    proc: subprocess.Popen[str],
    *,
    timeout_sec: float = _RECORDING_STOP_TIMEOUT_SEC,
) -> str:
    """Send stop, read final JSON line, terminate child, return WAV path."""
    if proc.stdin is None:
        raise RuntimeError("Recording subprocess stdin is not available.")
    try:
        proc.stdin.write("stop\n")
        proc.stdin.flush()
    except OSError as exc:
        raise RuntimeError(f"Failed to signal recording subprocess: {exc}") from exc

    payload = _read_json_line(proc, timeout_sec)
    status = payload.get("status")
    if status != "ok":
        message = payload.get("message") if status == "error" else f"Unexpected status {status!r}"
        raise RuntimeError(str(message or "Audio recording failed to stop."))
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        raise RuntimeError("Recording subprocess did not return a WAV path.")

    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.terminate()
    return path


def terminate_recording_process(proc: subprocess.Popen[str] | None) -> None:
    """Best-effort shutdown of a recording child."""
    if proc is None:
        return
    if proc.poll() is None:
        try:
            if proc.stdin is not None:
                proc.stdin.write("stop\n")
                proc.stdin.flush()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass


def make_temp_wav_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    return path
