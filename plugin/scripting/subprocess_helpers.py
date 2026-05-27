# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Subprocess spawn helpers: env scrubbing and Flatpak/Snap sandbox escape."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

_BLOCKED_ENV_SUBSTR = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")
# LibreOffice sets PYTHONHOME/PYTHONPATH to its bundled stdlib; letting these
# leak into a venv subprocess causes SRE module mismatch and import failures.
_BLOCKED_ENV_EXACT = {"PYTHONHOME", "PYTHONPATH"}

_NOT_SET = "__not_set__"
_cached_sandbox: str | None = _NOT_SET  # type: ignore[assignment]  # sentinel


def scrub_subprocess_env(base: dict[str, str] | None) -> dict[str, str]:
    """Drop likely-secret vars and LO Python overrides from the environment passed to venv Python."""
    if not base:
        return {}
    out: dict[str, str] = {}
    for k, v in base.items():
        ku = k.upper()
        if ku in _BLOCKED_ENV_EXACT:
            continue
        if any(s in ku for s in _BLOCKED_ENV_SUBSTR):
            continue
        out[k] = v
    out.setdefault("PYTHONIOENCODING", "utf-8")
    out.setdefault("PYTHONUTF8", "1")
    out.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return out


def detect_sandbox() -> str | None:
    """Return ``'flatpak'``, ``'snap'``, or ``None``.

    The result is cached because sandbox status cannot change at runtime.
    """
    global _cached_sandbox
    if _cached_sandbox is not _NOT_SET:
        return _cached_sandbox

    if os.path.exists("/.flatpak-info") or os.environ.get("FLATPAK_ID"):
        _cached_sandbox = "flatpak"
    elif os.environ.get("SNAP_NAME"):
        _cached_sandbox = "snap"
    else:
        _cached_sandbox = None
    return _cached_sandbox


_PIPE_BUF_TARGET = 1024 * 1024


def optimize_pipe(pipe_fd: int) -> None:
    """Raise venv-worker pipe capacity toward 1 MiB on Linux (default ~64 KiB).

    Large pickle IPC (split-grid / NumPy) can exceed the default pipe buffer;
    F_SETPIPE_SZ requests a larger kernel ring buffer so host and child block less.
    No-op on macOS/Windows (no supported API). Silently no-ops when caps deny resize.
    """
    if sys.platform != "linux":
        return
    import fcntl

    try:
        fcntl.fcntl(pipe_fd, fcntl.F_SETPIPE_SZ, _PIPE_BUF_TARGET)
    except OSError:
        pass


def optimize_popen_pipes(proc: subprocess.Popen[Any]) -> None:
    """Apply :func:`optimize_pipe` to stdin/stdout/stderr of a piped child process."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            optimize_pipe(stream.fileno())
        except (OSError, ValueError):
            pass


def wrap_command_for_sandbox(cmd: list[str]) -> list[str]:
    """Prepend ``flatpak-spawn --host`` when running inside a Flatpak sandbox.

    Snap confinement with ``classic``/``home`` plugs typically allows direct
    subprocess access, so Snap commands are returned unchanged.
    """
    sandbox = detect_sandbox()
    if sandbox == "flatpak":
        return ["flatpak-spawn", "--host"] + cmd
    return cmd


def _reset_cache() -> None:
    """Reset the cached detection result (for tests only)."""
    global _cached_sandbox
    _cached_sandbox = _NOT_SET  # type: ignore[assignment]
