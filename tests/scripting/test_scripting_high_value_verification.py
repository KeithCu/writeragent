# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for editor_ipc and excel_xl."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.scripting.editor_errors import failure_message
from plugin.scripting.excel_xl import make_xl


@given(summary=st.text(min_size=1, max_size=40), detail=st.text(max_size=40))
@settings(max_examples=50)
def test_failure_message_formatting(summary: str, detail: str) -> None:
    msg = failure_message(summary, detail=detail)
    assert isinstance(msg, str)
    body = detail.strip()
    assert msg == (summary if not body else f"{summary}\n\n{body}")


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
