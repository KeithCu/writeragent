# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""MIPROv2 metric helpers (length penalty + live-student token attrs)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from metric import (  # noqa: E402
    SLICE_LENGTH_MAX_RATIO,
    _get_total_tokens,
    slice_length_penalty,
)


def test_slice_length_penalty_stays_zero_until_double() -> None:
    assert SLICE_LENGTH_MAX_RATIO == 2.0
    assert slice_length_penalty("x" * 20, 10) == 0.0
    assert slice_length_penalty("x" * 10, 10) == 0.0
    assert slice_length_penalty("x" * 30, 10) == pytest.approx(0.5)
    assert slice_length_penalty("x" * 40, 10) == pytest.approx(1.0)


def test_get_total_tokens_prefers_live_attr() -> None:
    pred = SimpleNamespace(total_tokens=42)

    def boom() -> dict:
        raise RuntimeError("should not run")

    pred.get_lm_usage = boom
    assert _get_total_tokens(pred) == 42
