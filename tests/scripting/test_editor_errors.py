# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for editor failure detail and message formatting."""

from __future__ import annotations

import pytest

from plugin.framework.deal_shim import DEAL_MAX_SOURCE, UNDER_CROSSHAIR
from plugin.scripting.editor_errors import (
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

    with pytest.raises(deal.PreContractError):
        exception_traceback(None)  # type: ignore[arg-type]


def test_failure_message_accepts_long_summary():
    summary = "s" * (DEAL_MAX_SOURCE + 1)
    assert failure_message(summary) == summary
