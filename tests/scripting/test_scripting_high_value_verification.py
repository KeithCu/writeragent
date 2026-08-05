# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for trusted_rpc, editor_ipc, and excel_xl."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.errors import ToolExecutionError
from plugin.scripting.trusted_rpc import parse_worker_dict_result
from plugin.scripting.editor_ipc import failure_detail, failure_message
from plugin.scripting.excel_xl import make_xl


def test_parse_worker_dict_result_ok() -> None:
    res = parse_worker_dict_result(
        {"status": "ok", "result": {"output": 42}},
        error_code="ERR",
        error_label="Test",
    )
    assert res == {"output": 42}


def test_parse_worker_dict_result_error_status() -> None:
    with pytest.raises(ToolExecutionError) as exc_info:
        parse_worker_dict_result(
            {"status": "error", "message": "Failed"},
            error_code="WORKER_FAIL",
            error_label="Test",
        )
    assert exc_info.value.code == "WORKER_FAIL"
    assert "Failed" in str(exc_info.value)


@given(summary=st.text(min_size=1, max_size=40), detail=st.text(max_size=40))
@settings(max_examples=50)
def test_failure_message_formatting(summary: str, detail: str) -> None:
    msg = failure_message(summary, detail=detail)
    assert isinstance(msg, str)
    assert len(msg) >= len(summary)


@given(idx=st.integers(min_value=2, max_value=10))
def test_excel_xl_binding_resolution(idx: int) -> None:
    ranges = tuple([f"range_{i}" for i in range(15)])
    xl_fn = make_xl(ranges)
    assert callable(xl_fn)

    # %P2% corresponds to 0th range in zero-indexed list (idx - 2)
    ref = f"%P{idx}%"
    bound_val = xl_fn(ref)
    assert bound_val == ranges[idx - 2]


def test_excel_xl_invalid_ref() -> None:
    xl_fn = make_xl(["r1", "r2"])
    with pytest.raises(ValueError):
        xl_fn("invalid_ref")

    with pytest.raises(ValueError):
        xl_fn("%P99%")  # out of bounds
