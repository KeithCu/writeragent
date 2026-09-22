"""Unit tests for pytest progress heartbeat cadence in tests/conftest.py."""

from tests.conftest import format_idle_pytest_progress, should_emit_pytest_progress_count


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


def test_idle_progress_names_inflight_not_last_completed():
    """GHA 35669866244 blamed last=quoted_font after split_change had already finished."""
    msg = format_idle_pytest_progress(
        7975,
        "tests/writer/test_xhtml_style_postprocess.py::test_quoted_font_name_emits_well_formed_data_lo_para",
        [
            "tests/writer/test_foo.py::test_a",
            "tests/writer/test_foo.py::test_b",
        ],
    )
    assert "leftover=" in msg
    assert "test_a" in msg
    assert "test_quoted_font_name" not in msg


def test_idle_progress_caps_leftover_list():
    leftover = [f"t{i}" for i in range(8)]
    msg = format_idle_pytest_progress(7800, "last_one", leftover)
    assert "leftover=t0,t1,t2,t3,t4,t5 +2" in msg


def test_idle_progress_falls_back_to_last_when_none_inflight():
    msg = format_idle_pytest_progress(
        7975,
        "tests/writer/test_writer_diff_and_html_verification.py::test_hypothesis_split_change_fraction_bounds",
        [],
    )
    assert msg.endswith(
        "last=tests/writer/test_writer_diff_and_html_verification.py::test_hypothesis_split_change_fraction_bounds"
    )
