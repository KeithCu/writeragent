"""Unit tests for tests/harness/pytest_progress.py."""

from tests.harness.pytest_progress import format_idle_pytest_progress


def test_idle_progress_names_inflight_not_last_completed():
    """Idle progress message names the test still running, not the last completed one."""
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
