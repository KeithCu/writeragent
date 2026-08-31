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
    """Module-level file handle must not leak between tests."""
    ci_debug.stop_ci_debug()
    yield
    ci_debug.stop_ci_debug()


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
