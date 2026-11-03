# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Vale style linter helpers and sandbox parity."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugin.framework.worker_pool import get_subprocess_creationflags
from plugin.scripting.sandbox import detect_sandbox, wrap_command_for_sandbox
from plugin.scripting.venv import vale
from plugin.scripting.venv.vale import (
    _CHECK_TIMEOUT_SEC,
    _ensure_vale_config,
    _map_alert,
    _subprocess_creationflags,
    _wrap_vale_command,
    run_vale_check,
)


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


def test_map_alert_line_and_span_cases():
    # Line 1
    t1 = "This is was done."
    a1 = {"Line": 1, "Span": [6, 11], "Match": "is was", "Check": "RepeatedWords", "Severity": "warning"}
    m1 = _map_alert(t1, a1)
    assert m1 is not None
    assert m1["wrong"] == "is was"
    assert m1["n_error_start"] == 5
    assert m1["n_error_length"] == 6

    # Line 2
    t2 = "Header\nSecond has  bad  words."
    a2 = {"Line": 2, "Span": [13, 15], "Match": "bad"}
    m2 = _map_alert(t2, a2)
    assert m2 is not None
    assert m2["n_error_start"] == 19
    assert m2["wrong"] == "bad"
    assert t2[19:22] == "bad"

    # Non-BMP before the match
    t3 = "\U0001f642 is bad"
    a3 = {"Line": 1, "Span": [3, 4], "Match": "is"}
    m3 = _map_alert(t3, a3)
    assert m3 is not None
    assert m3["n_error_start"] == 2
    assert m3["wrong"] == "is"

    # Match disagreeing with the slice
    t4 = "hello world"
    a4 = {"Line": 1, "Span": [1, 5], "Match": "HELLO"}
    m4 = _map_alert(t4, a4)
    assert m4 is not None
    assert m4["wrong"] == "HELLO"
    assert m4["n_error_start"] == 0

    # Line 3 column equals index
    t5 = "a\nb\nc bad\n"
    a5 = {"Line": 3, "Span": [3, 5], "Match": "bad"}
    m5 = _map_alert(t5, a5)
    assert m5 is not None
    assert m5["n_error_start"] == t5.index("bad")


def test_map_alert_skipped_cases():
    text = "line one\nline two"
    # Document-scope alert (Match is empty)
    assert _map_alert(text, {"Line": 1, "Span": [1, 1], "Match": ""}) is None
    # Invalid Span
    assert _map_alert(text, {"Line": 1, "Span": [0, 0], "Match": "foo"}) is None
    assert _map_alert(text, {"Line": 1, "Span": [100, 105], "Match": "foo"}) is None
    assert _map_alert(text, {"Line": 1, "Span": [True, 2], "Match": "foo"}) is None
    # Missing Line or invalid Line
    assert _map_alert(text, {"Span": [1, 2], "Match": "li"}) is None
    # Line 2 on a single-line text
    assert _map_alert("single line", {"Line": 2, "Span": [1, 2], "Match": "li"}) is None
    # Non-dict alert
    assert _map_alert(text, ["not", "dict"]) is None


def test_map_alert_action_and_check_fallback():
    # Null Action, scalar Params, no Check
    alert = {"Line": 1, "Span": [1, 4], "Match": "line", "Action": None, "Params": "x"}
    mapped = _map_alert("line one", alert)
    assert mapped is not None
    assert mapped["rule_identifier"] == "vale||Style"
    assert mapped["suggestions"] == []
    assert mapped["correct"] == ""


def test_ensure_vale_config_caching_and_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ini_path = tmp_path / ".vale.ini"
    styles_path = tmp_path / "vale_styles"
    styles_list = ["Microsoft", "Google"]

    call_log: list[list[str]] = []

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        call_log.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # First call: writes ini, runs sync once, writes marker
    _ensure_vale_config("/dummy/vale", ini_path, styles_path, styles_list)
    assert len(call_log) == 1
    assert call_log[0][-1] == "sync"
    assert "--no-global" in call_log[0]
    marker_path = ini_path.with_name(".vale-sync-ok")
    assert marker_path.exists()

    # Second call with same styles: hit marker, no second run
    _ensure_vale_config("/dummy/vale", ini_path, styles_path, styles_list)
    assert len(call_log) == 1

    # Changing styles rewrites ini and syncs again
    _ensure_vale_config("/dummy/vale", ini_path, styles_path, ["Microsoft", "Google", "write-good"])
    assert len(call_log) == 2


def test_ensure_vale_config_sync_failure_and_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ini_path = tmp_path / ".vale.ini"
    styles_path = tmp_path / "vale_styles"
    styles_list = ["Microsoft"]

    calls = 0

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="boom"):
        _ensure_vale_config("/dummy/vale", ini_path, styles_path, styles_list)

    marker_path = ini_path.with_name(".vale-sync-ok")
    assert not marker_path.exists()
    assert ini_path.exists()

    # Next call retries and succeeds, then writes marker
    _ensure_vale_config("/dummy/vale", ini_path, styles_path, styles_list)
    assert calls == 2
    assert marker_path.exists()


def test_ensure_vale_config_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ini_path = tmp_path / ".vale.ini"
    styles_path = tmp_path / "vale_styles"

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 20)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="timed out"):
        _ensure_vale_config("/dummy/vale", ini_path, styles_path, ["Microsoft"])

    marker_path = ini_path.with_name(".vale-sync-ok")
    assert not marker_path.exists()


def test_run_vale_check_stdin_invocation_and_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(vale, "_get_vale_binary", lambda: "/venv/bin/vale")

    mock_run = MagicMock()
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout='{"stdin.txt": []}', stderr="")
    monkeypatch.setattr(subprocess, "run", mock_run)

    user_dir = str(tmp_path)
    text = "Hello world."
    # First call provisions (sync) + checks
    res1 = run_vale_check(text, user_dir, "Microsoft")
    assert res1 == {"errors": []}

    mock_run.reset_mock()
    res2 = run_vale_check(text, user_dir, "Microsoft")
    assert res2 == {"errors": []}
    assert mock_run.call_count == 1
    call_args, call_kwargs = mock_run.call_args
    assert call_kwargs["input"] == text
    assert call_kwargs["timeout"] == _CHECK_TIMEOUT_SEC
    expected_cmd = ["/venv/bin/vale", "--config", str(tmp_path / ".vale.ini"), "--output=JSON", "--ext=.txt", "--no-global"]
    assert call_args[0] == expected_cmd


def test_run_vale_check_fallback_key_and_error_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(vale, "_get_vale_binary", lambda: "/venv/bin/vale")

    # Mark config as already synced to avoid sync call
    ini_path = tmp_path / ".vale.ini"
    marker_path = tmp_path / ".vale-sync-ok"
    content = f"""StylesPath = {(tmp_path / "vale_styles").as_posix()}
MinAlertLevel = suggestion
Packages = Microsoft, Google, write-good

[*]
BasedOnStyles = Microsoft
Microsoft.Headings = NO
"""
    ini_path.write_text(content, encoding="utf-8")
    import hashlib
    marker_path.write_text(hashlib.sha256(content.encode("utf-8")).hexdigest(), encoding="utf-8")

    # Fallback key: sole key in dictionary
    alert = {"Line": 1, "Span": [1, 5], "Match": "Hello"}
    fake_json = '{"C:/tmp/whatever.txt": [' + str(alert).replace("'", '"') + ']}'

    def fake_run_success(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=fake_json, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_success)
    res = run_vale_check("Hello world.", str(tmp_path), "Microsoft")
    assert len(res["errors"]) == 1
    assert res["errors"][0]["wrong"] == "Hello"

    # Returncode 2 with stderr="boom" raises RuntimeError
    def fake_run_fail(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run_fail)
    with pytest.raises(RuntimeError, match="boom"):
        run_vale_check("Hello world.", str(tmp_path), "Microsoft")
