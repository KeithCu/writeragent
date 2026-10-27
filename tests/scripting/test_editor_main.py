# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for editor child lifecycle, reader loop, and WSGI static server."""

from __future__ import annotations

import builtins
import io
import logging
import queue
import struct
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugin.scripting.editor_ipc import PARENT_TO_CHILD_TYPES, write_message
from plugin.scripting.venv import editor_main as em


def _reset_state() -> None:
    em._closed_sent = False
    em._closed_session_id = ""
    em._shutting_down = False
    em._gui_loop_entered = False
    em._current_session_id = ""
    em._current_mode = ""
    em._current_target = {}
    em._window = None
    while True:
        try:
            em._ui_queue.get_nowait()
        except queue.Empty:
            break


def test_reader_eof_before_gui_loop_skips_destroy_and_claim():
    _reset_state()
    mock_window = MagicMock()
    em._window = mock_window
    em._gui_loop_entered = False
    em._pipe_reader_loop(io.BytesIO())
    mock_window.destroy.assert_not_called()
    assert em._shutting_down is True
    assert em._claim_gui_loop() is False
    mock_window.destroy.assert_not_called()


def test_reader_eof_after_claim_destroys_the_window():
    _reset_state()
    mock_window = MagicMock()
    em._window = mock_window
    assert em._claim_gui_loop() is True
    em._pipe_reader_loop(io.BytesIO())
    mock_window.destroy.assert_called_once()


def test_reader_queues_parent_types_and_drops_unknown():
    _reset_state()
    for name in PARENT_TO_CHILD_TYPES:
        if name == "load":
            continue
        _reset_state()
        buf = io.BytesIO()
        write_message(buf, {"type": name})
        buf.seek(0)
        em._pipe_reader_loop(buf)
        items = []
        while True:
            try:
                items.append(em._ui_queue.get_nowait())
            except queue.Empty:
                break
        assert len(items) == 1
        assert items[0]["type"] == name

    # One buffer with {"type": "validate_result"} followed by {"type": "saved"}
    _reset_state()
    buf = io.BytesIO()
    write_message(buf, {"type": "validate_result"})
    write_message(buf, {"type": "saved"})
    buf.seek(0)
    em._pipe_reader_loop(buf)
    items = []
    while True:
        try:
            items.append(em._ui_queue.get_nowait())
        except queue.Empty:
            break
    assert len(items) == 1
    assert items[0]["type"] == "saved"
    assert em._shutting_down is True

    # Buffer with {"type": "closed"} followed by {"type": "saved"}
    _reset_state()
    buf = io.BytesIO()
    write_message(buf, {"type": "closed"})
    write_message(buf, {"type": "saved"})
    buf.seek(0)
    em._pipe_reader_loop(buf)
    items = []
    while True:
        try:
            items.append(em._ui_queue.get_nowait())
        except queue.Empty:
            break
    assert len(items) == 0


def test_reader_load_publishes_identity_and_clears_closed():
    _reset_state()
    em._closed_sent = True
    em._current_session_id = "old"

    buf = io.BytesIO()
    write_message(buf, {
        "type": "load",
        "session_id": "s9",
        "mode": "calc_cell",
        "target": {"cell_address": "C3"},
    })
    buf.seek(0)
    em._pipe_reader_loop(buf)

    items = []
    while True:
        try:
            items.append(em._ui_queue.get_nowait())
        except queue.Empty:
            break

    assert len(items) == 1
    assert items[0]["type"] == "load"
    assert em._current_session_id == "s9"
    assert em._current_mode == "calc_cell"
    assert em._current_target["cell_address"] == "C3"
    assert em._closed_sent is False


def test_reader_bad_pickle_finishes_and_destroys_only_after_claim():
    _reset_state()
    bad_bytes = struct.pack("!I", 8) + b"notpickl"

    mock_window = MagicMock()
    em._window = mock_window
    em._gui_loop_entered = False
    em._pipe_reader_loop(io.BytesIO(bad_bytes))
    mock_window.destroy.assert_not_called()
    assert em._claim_gui_loop() is False

    _reset_state()
    mock_window2 = MagicMock()
    em._window = mock_window2
    assert em._claim_gui_loop() is True
    em._pipe_reader_loop(io.BytesIO(bad_bytes))
    mock_window2.destroy.assert_called_once()
    assert em._shutting_down is True


def test_wsgi_index_200_missing_404_and_read_500(tmp_path, monkeypatch, caplog):
    _reset_state()
    index_file = tmp_path / "index.html"
    index_file.write_bytes(b"hi")
    monkeypatch.setenv("WRITERAGENT_EDITOR_ASSETS", str(tmp_path))

    recorded: dict[str, str | dict[str, str]] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        recorded["status"] = status
        recorded["headers"] = dict(headers)

    # PATH_INFO / -> 200 OK, body [b"hi"], Content-Type text/html*
    body = em.wsgi_app({"PATH_INFO": "/"}, start_response)
    assert recorded["status"] == "200 OK"
    assert body == [b"hi"]
    headers = recorded["headers"]
    assert isinstance(headers, dict)
    assert headers["Content-Type"].startswith("text/html")

    # PATH_INFO /nope -> 404 Not Found, body [b"Not Found"]
    recorded.clear()
    body = em.wsgi_app({"PATH_INFO": "/nope"}, start_response)
    assert recorded["status"] == "404 Not Found"
    assert body == [b"Not Found"]

    # Patch builtins.open so index.html rb open raises OSError("disk")
    orig_open = builtins.open

    def fake_open(file, mode="r", *args, **kwargs):
        if str(file) == str(index_file) and "rb" in mode:
            raise OSError("disk")
        return orig_open(file, mode, *args, **kwargs)

    recorded.clear()
    monkeypatch.setattr(builtins, "open", fake_open)
    caplog.set_level(logging.ERROR)
    body = em.wsgi_app({"PATH_INFO": "/"}, start_response)
    assert "failed to serve" in caplog.text
    status = recorded["status"]
    assert isinstance(status, str)
    assert status.startswith("500")
    assert b"disk" in b"".join(body)


def test_main_fails_fast_when_monaco_loader_is_missing(tmp_path, monkeypatch, capsys):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    monkeypatch.setenv("WRITERAGENT_EDITOR_ASSETS", str(tmp_path))
    monkeypatch.setattr(em, "_ROCHER_ROOT", "")
    # Keep main() from claiming the real stdout fd when pytest runs with -s.
    monkeypatch.setattr("plugin.scripting.ipc.claim_ipc_channel", lambda: io.BytesIO())
    with pytest.raises(SystemExit) as exc_info:
        em.main()
    assert exc_info.value.code == 1
    assert "Monaco assets not found" in capsys.readouterr().err


def test_wsgi_traversal_and_vs_root_are_404(tmp_path, monkeypatch):
    _reset_state()
    rocher_dir = tmp_path / "rocher"
    rocher_dir.mkdir()
    (rocher_dir / "loader.js").write_text("loader", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    monkeypatch.setattr(em, "_ROCHER_ROOT", str(rocher_dir))

    recorded: dict[str, str | dict[str, str]] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        recorded["status"] = status
        recorded["headers"] = dict(headers)

    body = em.wsgi_app({"PATH_INFO": "/vs/../../secret.txt"}, start_response)
    assert recorded["status"] == "404 Not Found"
    assert body == [b"Not Found"]

    recorded.clear()
    body = em.wsgi_app({"PATH_INFO": "/vs/"}, start_response)
    assert recorded["status"] == "404 Not Found"
    assert body == [b"Not Found"]


def test_wsgi_editor_config_output_per_env(monkeypatch):
    _reset_state()

    def call_config(env_val: str | None) -> tuple[str, dict[str, str], bytes]:
        if env_val is None:
            monkeypatch.delenv("WRITERAGENT_EDITOR_BACKEND", raising=False)
        else:
            monkeypatch.setenv("WRITERAGENT_EDITOR_BACKEND", env_val)
        recorded: dict[str, Any] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            recorded["status"] = status
            recorded["headers"] = dict(headers)

        body = b"".join(em.wsgi_app({"PATH_INFO": "/editor_config.js"}, start_response))
        return recorded["status"], recorded["headers"], body

    # Default / unset -> monaco
    status, headers, body = call_config(None)
    assert status == "200 OK"
    assert headers["Content-Type"] == "application/javascript"
    assert b'window.WA_EDITOR_BACKEND = "monaco";' in body

    # codemirror -> codemirror
    status, headers, body = call_config("codemirror")
    assert status == "200 OK"
    assert headers["Content-Type"] == "application/javascript"
    assert b'window.WA_EDITOR_BACKEND = "codemirror";' in body

    # Garbage -> monaco
    status, headers, body = call_config("sublime_text")
    assert status == "200 OK"
    assert b'window.WA_EDITOR_BACKEND = "monaco";' in body


def test_monaco_static_path_serves_cm6_files(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    cm6_dir = assets / "cm6"
    cm6_dir.mkdir()
    (assets / "backend_cm6.js").write_text("// cm6", encoding="utf-8")
    (cm6_dir / "cm6.bundle.js").write_text("// bundle", encoding="utf-8")

    served_backend = em.monaco_static_path("/backend_cm6.js", str(assets), "")
    assert served_backend == str(assets / "backend_cm6.js")

    served_bundle = em.monaco_static_path("/cm6/cm6.bundle.js", str(assets), "")
    assert served_bundle == str(cm6_dir / "cm6.bundle.js")


def test_main_codemirror_backend_does_not_fatal_on_missing_rocher(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("x", encoding="utf-8")
    (assets / "backend_cm6.js").write_text("x", encoding="utf-8")
    cm6_dir = assets / "cm6"
    cm6_dir.mkdir()
    (cm6_dir / "cm6.bundle.js").write_text("x", encoding="utf-8")

    monkeypatch.setenv("WRITERAGENT_EDITOR_ASSETS", str(assets))
    monkeypatch.setenv("WRITERAGENT_EDITOR_BACKEND", "codemirror")
    monkeypatch.setattr(em, "_ROCHER_ROOT", "")
    monkeypatch.setattr("plugin.scripting.ipc.claim_ipc_channel", lambda: io.BytesIO())

    mock_webview = MagicMock()
    mock_webview.create_window.return_value = MagicMock()
    mock_webview.start.side_effect = lambda **kw: None
    monkeypatch.setitem(sys.modules, "webview", mock_webview)

    # Should not raise SystemExit on missing rocher
    em.main()
    mock_webview.create_window.assert_called_once()


def test_main_codemirror_backend_fatals_when_cm6_assets_missing(tmp_path, monkeypatch, capsys):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("x", encoding="utf-8")
    # Missing backend_cm6.js and cm6.bundle.js

    monkeypatch.setenv("WRITERAGENT_EDITOR_ASSETS", str(assets))
    monkeypatch.setenv("WRITERAGENT_EDITOR_BACKEND", "codemirror")
    monkeypatch.setattr("plugin.scripting.ipc.claim_ipc_channel", lambda: io.BytesIO())

    with pytest.raises(SystemExit) as exc_info:
        em.main()
    assert exc_info.value.code == 1
    assert "CodeMirror" in capsys.readouterr().err
