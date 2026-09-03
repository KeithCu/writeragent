# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for opt-in CI hang diagnostics (WRITERAGENT_CI_DEBUG)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import ci_debug


@pytest.fixture(autouse=True)
def _reset_ci_debug_handle():
    """Module-level file handle / hang-dump timer must not leak between tests."""
    ci_debug.stop_ci_debug()
    ci_debug.cancel_stderr_hang_dump()
    yield
    ci_debug.stop_ci_debug()
    ci_debug.cancel_stderr_hang_dump()


def test_ci_debug_disabled_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WRITERAGENT_CI_DEBUG", raising=False)
    monkeypatch.setenv("WRITERAGENT_CI_DEBUG_DIR", str(tmp_path))
    assert ci_debug.ci_debug_enabled() is False
    assert ci_debug.start_ci_debug() is None
    ci_debug.log_ci_debug("start should-not-write")
    assert list(tmp_path.iterdir()) == []


def test_ci_debug_trail_flushes_start_and_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WRITERAGENT_CI_DEBUG", "1")
    monkeypatch.setenv("WRITERAGENT_CI_DEBUG_DIR", str(tmp_path))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    assert ci_debug.ci_debug_enabled() is True
    assert ci_debug.ci_debug_worker_id() == "gw3"
    fh = ci_debug.start_ci_debug()
    assert fh is not None
    ci_debug.log_ci_debug("start tests/foo.py::test_hang")
    ci_debug.log_ci_debug("end tests/foo.py::test_hang")
    path = tmp_path / "gw3.log"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "worker=gw3" in text
    assert "start tests/foo.py::test_hang" in text
    assert "end tests/foo.py::test_hang" in text


def test_ci_debug_last_start_without_end_names_hung_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WRITERAGENT_CI_DEBUG", "1")
    monkeypatch.setenv("WRITERAGENT_CI_DEBUG_DIR", str(tmp_path))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    ci_debug.start_ci_debug()
    ci_debug.log_ci_debug("start tests/a.py::test_ok")
    ci_debug.log_ci_debug("end tests/a.py::test_ok")
    ci_debug.log_ci_debug("start tests/b.py::test_hung")
    ci_debug.stop_ci_debug()
    lines = (tmp_path / "controller.log").read_text(encoding="utf-8").splitlines()
    starts = [ln for ln in lines if ln.startswith("start ")]
    ends = [ln for ln in lines if ln.startswith("end ")]
    assert starts[-1] == "start tests/b.py::test_hung"
    assert "end tests/b.py::test_hung" not in ends


def test_arm_stderr_hang_dump_uses_faulthandler_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """GHA 33703959362: dump all Python threads to stderr at 90s, not a log file."""
    import sys

    seen: dict[str, object] = {}

    def fake_enable(*, file=None, all_threads=None):
        seen["enable"] = (file, all_threads)

    def fake_later(timeout, repeat=False, exit=False, file=None):
        seen["later"] = (timeout, repeat, exit, file)

    monkeypatch.setattr(ci_debug.faulthandler, "enable", fake_enable)
    monkeypatch.setattr(ci_debug.faulthandler, "dump_traceback_later", fake_later)
    ci_debug.arm_stderr_hang_dump(
        90, label="calc.test_rich_html_uno.test_insert_cell_html"
    )
    assert seen["later"] == (90, True, False, sys.stderr)
    assert seen["enable"] == (sys.stderr, True)
    err = capsys.readouterr().err
    assert (
        "hang dump armed timeout=90s test=calc.test_rich_html_uno.test_insert_cell_html"
        in err
    )
    assert "all_threads=True" in err


def test_cancel_stderr_hang_dump_prints_only_when_armed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ci_debug.faulthandler, "dump_traceback_later", lambda *a, **k: None)
    monkeypatch.setattr(ci_debug.faulthandler, "cancel_dump_traceback_later", lambda: None)
    monkeypatch.setattr(ci_debug.faulthandler, "enable", lambda **k: None)
    ci_debug.cancel_stderr_hang_dump()
    assert "hang dump disarmed" not in capsys.readouterr().err
    ci_debug.arm_stderr_hang_dump(90, label="unit")
    capsys.readouterr()
    ci_debug.cancel_stderr_hang_dump(label="unit")
    assert "hang dump disarmed test=unit" in capsys.readouterr().err


def test_stderr_hang_dump_default_timeout_is_under_gha_step() -> None:
    """Must fire before the 25m GHA step; 20 min is too late (33703959362)."""
    assert 90 <= ci_debug.STDERR_HANG_DUMP_SECONDS <= 120
