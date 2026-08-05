# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for Calc dependencies, filter criteria, and Excel ref resolution."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.calc.formula_dep_chain import _resolve_sheet_and_cell
from plugin.calc.sheet_filter_criteria import (
    filter_connection_code,
    resolve_filter_operator_code,
    parse_sheet_filter_criterion,
)
from plugin.calc.excel_py_convert.resolve_refs import (
    ResolvedDep,
    resolve_dep,
)
from plugin.framework.errors import UnoObjectError


def test_resolve_sheet_and_cell_parse() -> None:
    res = _resolve_sheet_and_cell(None, "Sheet1.B10")
    # doc is None, so function returns None after parsing cell part B10 (col 1, row 9)
    assert res is None

    res2 = _resolve_sheet_and_cell(None, "INVALID_CELL_1234567")
    assert res2 is None


@given(conn=st.sampled_from(["AND", "and", "OR", "or", None]))
def test_filter_connection_code_valid(conn: str | None) -> None:
    code = filter_connection_code(conn)
    assert code in (0, 1)


def test_filter_connection_code_invalid() -> None:
    with pytest.raises(UnoObjectError):
        filter_connection_code("INVALID_CONN")


@given(op=st.sampled_from(["EQUAL", "NOT_EQUAL", "GREATER", "LESS", "CONTAINS", "BEGINS_WITH"]))
def test_resolve_filter_operator_code_valid(op: str) -> None:
    code = resolve_filter_operator_code(op)
    assert isinstance(code, int)
    assert code >= 0


def test_resolve_filter_operator_code_invalid() -> None:
    with pytest.raises(UnoObjectError):
        resolve_filter_operator_code("UNKNOWN_OPERATOR_123")


def test_parse_sheet_filter_criterion_basic() -> None:
    raw = {"field": 0, "operator": "EQUAL", "value": "test"}
    field, op_code, conn, is_num, num_val, str_val = parse_sheet_filter_criterion(raw, is_first=True)
    assert field == 0
    assert conn == 0  # First row connection is AND (0)
    assert is_num is False
    assert str_val == "test"


class DummyModel:
    def __init__(self) -> None:
        self.anchor_snapshots = {"A6": "A6:C10"}
        self.tables = {"Table1": "A1:D50"}


def test_resolve_dep_range_and_table() -> None:
    model = DummyModel()  # type: ignore[assignment]
    dep1 = resolve_dep("A1:B10", model)
    assert isinstance(dep1, ResolvedDep)
    assert dep1.kind == "range"
    assert dep1.a1 == "A1:B10"

    dep2 = resolve_dep("Table1[#All]", model)
    assert dep2.kind == "table_snapshot"
    assert dep2.a1 == "A1:D50"

    dep3 = resolve_dep("_xlfn.ANCHORARRAY(A6)", model)
    assert dep3.kind == "anchor_snapshot"
    assert dep3.a1 == "A6:C10"
