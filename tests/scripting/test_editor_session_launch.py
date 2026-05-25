# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared Monaco editor session launch."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting import editor_session_launch as launch_mod


def test_launch_monaco_editor_reuses_running_process():
    ctx = MagicMock()
    sent_messages: list[dict] = []
    mock_proc = MagicMock()

    def fake_send(msg: dict) -> None:
        sent_messages.append(msg)

    with patch.object(launch_mod, "_PERSISTENT_EDITOR") as mock_persistent:
        mock_persistent.is_running = True
        mock_persistent.proc = mock_proc
        with patch.object(launch_mod, "EditorSession") as mock_session_cls:
            session = MagicMock()
            session.is_running = True
            session.send = fake_send
            mock_session_cls.return_value = session

            ok = launch_mod.launch_monaco_editor(
                ctx,
                exe="/venv/bin/python",
                load_message={"type": "load", "code": "print(1)"},
                on_save=MagicMock(),
            )

    assert ok is True
    assert sent_messages == [{"type": "load", "code": "print(1)"}]
    mock_session_cls.assert_called_once()


def test_launch_monaco_editor_spawns_when_not_running():
    ctx = MagicMock()
    mock_proc = MagicMock()

    with patch.object(launch_mod, "_PERSISTENT_EDITOR") as mock_persistent:
        mock_persistent.is_running = False
        with patch.object(launch_mod, "spawn_editor_process", return_value=mock_proc):
            with patch.object(launch_mod, "EditorSession") as mock_session_cls:
                session = MagicMock()
                session.is_running = True
                session.wait_for_ready.return_value = True
                mock_session_cls.return_value = session

                ok = launch_mod.launch_monaco_editor(
                    ctx,
                    exe="/venv/bin/python",
                    load_message={"type": "load", "mode": "run_script"},
                    on_save=MagicMock(),
                )

    assert ok is True
    session.start_reader.assert_called_once()
    session.send.assert_called_once_with({"type": "load", "mode": "run_script"})


def test_monaco_editor_available_false_without_venv():
    ctx = MagicMock()
    with patch.object(launch_mod, "resolve_editor_python", return_value=(None, "missing venv")):
        exe, ok = launch_mod.monaco_editor_available(ctx)
    assert exe is None
    assert ok is False


def test_monaco_editor_available_false_when_webview_missing():
    ctx = MagicMock()
    with patch.object(launch_mod, "resolve_editor_python", return_value=("/venv/bin/python", "")):
        with patch.object(launch_mod, "probe_webview_import", return_value=(False, "no webview")):
            exe, ok = launch_mod.monaco_editor_available(ctx)
    assert exe == "/venv/bin/python"
    assert ok is False
