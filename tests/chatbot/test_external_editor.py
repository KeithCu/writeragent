# WriterAgent - tests for external editor launcher
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import pytest


def test_resolve_editor_argv_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "darwin")
    assert ee.resolve_editor_argv("/tmp/w.json") == ["open", "-t", "/tmp/w.json"]


def test_resolve_editor_argv_windows_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "win32")
    assert ee.resolve_editor_argv("C:\\a.json") is None


def test_resolve_editor_argv_linux_kate_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "linux")

    def _which(cmd: str) -> str | None:
        if cmd == "kate":
            return "/usr/bin/kate"
        return None

    monkeypatch.setattr(ee.shutil, "which", _which)
    assert ee.resolve_editor_argv("/p.json") == ["/usr/bin/kate", "/p.json"]


def test_resolve_editor_argv_linux_gedit_if_no_kate(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "linux")

    def _which(cmd: str) -> str | None:
        if cmd == "gedit":
            return "/usr/bin/gedit"
        return None

    monkeypatch.setattr(ee.shutil, "which", _which)
    assert ee.resolve_editor_argv("/p.json") == ["/usr/bin/gedit", "/p.json"]


def test_resolve_editor_argv_linux_editor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(ee.shutil, "which", lambda _cmd: None)
    monkeypatch.setenv("EDITOR", "nano")
    monkeypatch.delenv("VISUAL", raising=False)
    assert ee.resolve_editor_argv("/q.json") == ["nano", "/q.json"]


def test_resolve_editor_argv_linux_visual_when_editor_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(ee.shutil, "which", lambda _cmd: None)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("VISUAL", "vi")
    assert ee.resolve_editor_argv("/r.json") == ["vi", "/r.json"]
