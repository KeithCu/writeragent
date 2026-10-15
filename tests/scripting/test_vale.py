# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Vale style linter helpers and sandbox parity."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest

from plugin.framework.worker_pool import get_subprocess_creationflags
from plugin.scripting.sandbox import detect_sandbox, wrap_command_for_sandbox
from plugin.scripting.venv.vale import _subprocess_creationflags, _wrap_vale_command


def test_vale_spawn_matches_sandbox_outside_flatpak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    real_exists = os.path.exists

    def fake_exists(path: Any) -> bool:
        if path == "/.flatpak-info":
            return False
        return real_exists(path)

    monkeypatch.setattr(os.path, "exists", fake_exists)

    detect_sandbox.cache_clear()
    cmd = ["vale", "--output", "JSON", "note.txt"]
    try:
        assert _wrap_vale_command(cmd) == cmd
        assert _wrap_vale_command(cmd) == wrap_command_for_sandbox(cmd)
    finally:
        detect_sandbox.cache_clear()


def test_vale_spawn_matches_sandbox_inside_flatpak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLATPAK_ID", "org.example.App")
    cmd = ["vale", "--output", "JSON", "note.txt"]
    detect_sandbox.cache_clear()
    try:
        assert _wrap_vale_command(cmd) == ["flatpak-spawn", "--host", *cmd]
        assert _wrap_vale_command(cmd) == wrap_command_for_sandbox(cmd)
    finally:
        detect_sandbox.cache_clear()


def test_vale_creationflags_match_worker_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert _subprocess_creationflags() == get_subprocess_creationflags()

    monkeypatch.setattr(sys, "platform", "linux")
    assert _subprocess_creationflags() == {}
    assert get_subprocess_creationflags() == {}
