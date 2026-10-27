# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for editor failure detail and message formatting."""

from __future__ import annotations

import pytest

from plugin.framework.deal_shim import DEAL_MAX_SOURCE, UNDER_CROSSHAIR
from plugin.scripting.editor_errors import (
    editor_error_markers,
    exception_traceback,
    failure_detail,
    failure_message,
)


def test_exception_traceback_includes_frame():
    try:
        raise ValueError("probe failure")
    except ValueError as e:
        tb = exception_traceback(e)
    assert "ValueError: probe failure" in tb
    assert "test_exception_traceback_includes_frame" in tb


def test_failure_message_combines_summary_and_detail():
    msg = failure_message("Summary", detail="stderr line")
    assert msg.startswith("Summary\n\n")
    assert "stderr line" in msg


def test_failure_message_accepts_none_detail():
    msg = failure_message("Summary", detail=None)
    assert msg == "Summary"


def test_failure_detail_strips_blocks_and_keeps_one_blank_line():
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        tb = exception_traceback(e)
        detail = failure_detail(detail="  x \n\n y  ", exc=e)
        assert detail == f"x \n\n y\n\n{tb.rstrip()}"
        assert failure_detail(detail="   \n\n ", exc=e) == tb.rstrip()


def test_failure_detail_without_blocks_is_empty():
    assert failure_detail() == ""
    assert failure_detail(detail="  \n\t", exc=None) == ""


def test_failure_message_with_blank_detail_is_summary_only():
    assert failure_message("Summary", detail="  \n\t") == "Summary"


def test_failure_message_empty_summary():
    assert failure_message("", detail="", exc=None) == ""


def test_failure_detail_formats_exception_under_deal():
    """The body formats exc. Deal must allow that call; CrossHair still forces exc is None."""
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        if UNDER_CROSSHAIR:
            import deal

            with pytest.raises(deal.PreContractError):
                failure_detail(detail="stderr line", exc=e)
            return
        detail = failure_detail(detail="stderr line", exc=e)
        msg = failure_message("Summary", detail=None, exc=e)
        tb = exception_traceback(e)
    assert "stderr line" in detail
    assert "RuntimeError: boom" in detail
    assert detail == f"stderr line\n\n{tb.rstrip()}"
    assert msg.startswith("Summary\n\n")
    assert "RuntimeError: boom" in msg
    assert failure_detail(detail="stderr line", exc=None) == "stderr line"
    assert "ValueError: boom" in failure_detail(exc=ValueError("boom"))


def test_failure_detail_accepts_long_text():
    detail = failure_detail(detail="e" * (DEAL_MAX_SOURCE + 1))
    assert detail.startswith("e" * 20)
    assert len(detail) == DEAL_MAX_SOURCE + 1


def test_exception_traceback_rejects_none():
    import deal

    # Release bundles strip @deal.pre, so the body's AttributeError is the guard there.
    with pytest.raises((deal.PreContractError, AttributeError)):
        exception_traceback(None)  # type: ignore[arg-type]


def test_failure_message_accepts_long_summary():
    summary = "s" * (DEAL_MAX_SOURCE + 1)
    assert failure_message(summary) == summary


def test_editor_error_markers_syntax_line_with_prefix_and_suffix():
    msg = "Error: Code parsing failed on line 3 due to: SyntaxError: invalid syntax (took 12 ms)"
    markers = editor_error_markers(msg)
    assert markers == [
        {
            "line": 3,
            "message": "Error: Code parsing failed on line 3 due to: SyntaxError: invalid syntax",
            "severity": "error",
        }
    ]
    assert "col" not in markers[0]


def test_editor_error_markers_parsing_without_line_returns_empty():
    assert editor_error_markers("Code parsing failed due to: ValueError: null byte") == []
    assert editor_error_markers("Code parsing failed: expression is too deeply nested") == []


def test_editor_error_markers_runtime_segment():
    code = "x = 1\n1/0\n"
    msg = "Code execution failed at line '1/0' due to: ZeroDivisionError: division by zero"
    assert editor_error_markers(msg, code=code) == [
        {
            "line": 2,
            "message": "Code execution failed at line '1/0' due to: ZeroDivisionError: division by zero",
            "severity": "error",
        }
    ]

    multiline_code = "a = 1\nfor i in range(10):\n    print(i)\n"
    multiline_msg = "Code execution failed at line 'for i in range(10):\n    print(i)' due to: RuntimeError: stop"
    assert editor_error_markers(multiline_msg, code=multiline_code) == [
        {
            "line": 2,
            "message": "Code execution failed at line 'for i in range(10):",
            "severity": "error",
        }
    ]

    assert editor_error_markers("Code execution failed at line 'missing()' due to: NameError", code=code) == []
    assert editor_error_markers("Code execution failed at line 'None' due to: TypeError", code="None\n") == []


def test_editor_error_markers_runtime_segment_with_single_quote():
    code = "x = 1\nprint('hello')\n"
    msg = "Code execution failed at line 'print('hello')' due to: ValueError: boom"
    assert editor_error_markers(msg, code=code) == [
        {
            "line": 2,
            "message": "Code execution failed at line 'print('hello')' due to: ValueError: boom",
            "severity": "error",
        }
    ]


def test_editor_error_markers_sandbox_fallback_last_occurrence():
    tb = (
        'Traceback (most recent call last):\n'
        '  File "<sandbox>", line 2, in <module>\n'
        '  File "<sandbox>", line 4, in helper\n'
        'ValueError: fail'
    )
    assert editor_error_markers("Host error", traceback_text=tb) == [
        {
            "line": 4,
            "message": "Host error",
            "severity": "error",
        }
    ]


def test_editor_error_markers_ignores_cell_and_host_files():
    tb = (
        'Traceback (most recent call last):\n'
        '  File "<cell>", line 9, in <module>\n'
        '  File "/path/to/venv_sandbox.py", line 1527, in evaluate\n'
        'ValueError: error'
    )
    code = "".join(f"line {i}\n" for i in range(1, 2000))
    assert editor_error_markers("Host error", traceback_text=tb, code=code) == []


def test_editor_error_markers_syntax_wins_over_host_file():
    msg = "Code parsing failed on line 3 due to: SyntaxError: bad"
    tb = '  File "<sandbox>", line 10, in <module>\n'
    markers = editor_error_markers(msg, traceback_text=tb, code="a\nb\nc\nd\ne\n")
    assert markers == [
        {
            "line": 3,
            "message": "Code parsing failed on line 3 due to: SyntaxError: bad",
            "severity": "error",
        }
    ]


def test_editor_error_markers_syntax_past_end_of_code_returns_empty():
    msg = "Code parsing failed on line 5 due to: SyntaxError: bad"
    code = "x = 1\ny = 2\n"
    assert editor_error_markers(msg, code=code) == []
