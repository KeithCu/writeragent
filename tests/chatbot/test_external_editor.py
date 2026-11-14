# WriterAgent - tests for external editor launcher
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


def test_resolve_editor_argv_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "darwin")
    assert ee.resolve_editor_argv("/tmp/w.json") == ["open", "-t", "/tmp/w.json"]


@pytest.mark.parametrize(
    ("which_cmd", "which_path", "editor_env", "visual_env", "target_path", "expected_argv"),
    [
        ("kate", "/usr/bin/kate", None, None, "/p.json", ["/usr/bin/kate", "/p.json"]),
        ("gedit", "/usr/bin/gedit", None, None, "/p.json", ["/usr/bin/gedit", "/p.json"]),
        (None, None, "nano", None, "/q.json", ["nano", "/q.json"]),
        (None, None, None, "vi", "/r.json", ["vi", "/r.json"]),
    ],
)
def test_resolve_editor_argv_linux(
    monkeypatch: pytest.MonkeyPatch,
    which_cmd: str | None,
    which_path: str | None,
    editor_env: str | None,
    visual_env: str | None,
    target_path: str,
    expected_argv: list[str],
) -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(ee.shutil, "which", lambda cmd: which_path if cmd == which_cmd else None)
    if editor_env is not None:
        monkeypatch.setenv("EDITOR", editor_env)
    else:
        monkeypatch.delenv("EDITOR", raising=False)
    if visual_env is not None:
        monkeypatch.setenv("VISUAL", visual_env)
    else:
        monkeypatch.delenv("VISUAL", raising=False)
    assert ee.resolve_editor_argv(target_path) == expected_argv


def test_ensure_config_file_creates_missing_file_private(tmp_path: Path) -> None:
    from plugin.chatbot.external_editor import _ensure_config_file

    path = tmp_path / "sub" / "writeragent.json"
    _ensure_config_file(str(path))
    assert path.parent.is_dir()
    assert path.read_text(encoding="utf-8") == "{}\n"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_ensure_config_file_keeps_an_existing_file(tmp_path: Path) -> None:
    from plugin.chatbot.external_editor import _ensure_config_file

    path = tmp_path / "writeragent.json"
    path.write_text('{"sentinel": true}\n', encoding="utf-8")
    _ensure_config_file(str(path))
    assert path.read_text(encoding="utf-8") == '{"sentinel": true}\n'


def test_popen_detached_redirects_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.chatbot import external_editor as ee

    monkeypatch.setattr(sys, "platform", "linux")
    recorded_kwargs: dict[str, Any] = {}

    def _fake_popen(argv: list[str], **kwargs: Any) -> None:
        recorded_kwargs.update(kwargs)

    monkeypatch.setattr(ee.subprocess, "Popen", _fake_popen)
    ee._popen_detached(["/usr/bin/kate", "/p.json"])

    assert recorded_kwargs["stdin"] == subprocess.DEVNULL
    assert recorded_kwargs["stdout"] == subprocess.DEVNULL
    assert recorded_kwargs["stderr"] == subprocess.DEVNULL
    assert recorded_kwargs["start_new_session"] is True


def test_open_writeragent_json_in_editor_spawns_resolved_editor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from plugin.chatbot import external_editor as ee

    config_path = str(tmp_path / "writeragent.json")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ee, "init_config", MagicMock())
    monkeypatch.setattr(ee, "get_config_file_path", lambda: config_path)
    monkeypatch.setattr(ee, "resolve_editor_argv", lambda _path: ["/usr/bin/kate", "/p.json"])
    mock_spawn = MagicMock()
    monkeypatch.setattr(ee, "_popen_detached", mock_spawn)
    mock_msgbox = MagicMock()
    monkeypatch.setattr(ee, "msgbox", mock_msgbox)

    ee.open_writeragent_json_in_editor(MagicMock())

    mock_spawn.assert_called_once_with(["/usr/bin/kate", "/p.json"])
    mock_msgbox.assert_not_called()


def test_open_writeragent_json_in_editor_reports_spawn_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from plugin.chatbot import external_editor as ee

    config_path = str(tmp_path / "writeragent.json")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ee, "init_config", MagicMock())
    monkeypatch.setattr(ee, "get_config_file_path", lambda: config_path)
    monkeypatch.setattr(ee, "resolve_editor_argv", lambda _path: ["/usr/bin/kate", "/p.json"])
    monkeypatch.setattr(ee, "_popen_detached", MagicMock(side_effect=OSError("nope")))
    mock_msgbox = MagicMock()
    monkeypatch.setattr(ee, "msgbox", mock_msgbox)

    ee.open_writeragent_json_in_editor(MagicMock())

    mock_msgbox.assert_called_once()

