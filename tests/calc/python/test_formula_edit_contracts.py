# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for formula_edit contract predicates (no LibreOffice)."""

from __future__ import annotations

from plugin.calc.python.formula_edit import PythonFormulaParts
from plugin.calc.python.formula_edit_contracts import (
    _deal_data_args_ok,
    _deal_range_addr_ok,
    _parts_result_ok,
    _quoted_parse_result_ok,
)
from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, DEAL_MAX_TOKEN


def test_deal_range_addr_ok() -> None:
    assert _deal_range_addr_ok("A1:B2") is True
    assert _deal_range_addr_ok("'My Sheet'.A1") is True
    assert _deal_range_addr_ok("A" * (DEAL_MAX_TOKEN + 1)) is False
    assert _deal_range_addr_ok("A1\x00") is False
    assert _deal_range_addr_ok(None) is False


def test_deal_data_args_ok() -> None:
    assert _deal_data_args_ok(["A1:B2", "'My Sheet'.A1"]) is True
    assert _deal_data_args_ok(("A1",)) is False
    assert _deal_data_args_ok(["A1"] * (DEAL_MAX_SHAPE_DIM + 1)) is False
    assert _deal_data_args_ok(["A1\x00"]) is False


def test_quoted_parse_result_ok() -> None:
    assert _quoted_parse_result_ok('"x"', 0, None) is True
    assert _quoted_parse_result_ok('"x"', 0, ("x", 3)) is True
    assert _quoted_parse_result_ok('"x"', 0, ("x", 0)) is False
    assert _quoted_parse_result_ok('"x"', 0, ("x", 4)) is False


def test_parts_result_ok() -> None:
    assert _parts_result_ok(None) is True
    assert _parts_result_ok(PythonFormulaParts("=PY(", "x", ")")) is True
    assert _parts_result_ok(PythonFormulaParts("=PY", "x", ")")) is False
    assert _parts_result_ok(PythonFormulaParts("", "x", ")")) is False
    assert _parts_result_ok(PythonFormulaParts("=PY(", "x", "")) is False
    assert _parts_result_ok(object()) is False
