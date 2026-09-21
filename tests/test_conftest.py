"""Unit tests for pytest progress heartbeat cadence in tests/conftest.py."""

from tests.conftest import should_emit_pytest_progress_count


def test_progress_count_every_100():
    assert should_emit_pytest_progress_count(100) is True
    assert should_emit_pytest_progress_count(7900) is True
    assert should_emit_pytest_progress_count(99) is False
    assert should_emit_pytest_progress_count(0) is False


def test_progress_count_fail_always_emits():
    assert should_emit_pytest_progress_count(6226, failed=True) is True
    assert should_emit_pytest_progress_count(1, failed=True) is True


def test_progress_count_tail_every_10_after_7800():
    """Last 1% never reaches pytest: 8000 (~7988 items). Keep a line in the log."""
    assert should_emit_pytest_progress_count(7800) is True
    assert should_emit_pytest_progress_count(7810) is True
    assert should_emit_pytest_progress_count(7910) is True
    assert should_emit_pytest_progress_count(7980) is True
    assert should_emit_pytest_progress_count(7801) is False
    assert should_emit_pytest_progress_count(7901) is False
    # Before the tail, only the 100-cadence applies.
    assert should_emit_pytest_progress_count(7710) is False
