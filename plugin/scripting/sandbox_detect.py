# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Detect Flatpak / Snap sandboxes and wrap subprocess commands to escape them."""

from __future__ import annotations

import os

_NOT_SET = "__not_set__"
_cached_sandbox: str | None = _NOT_SET  # type: ignore[assignment]  # sentinel


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
