# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for Calc dependencies, filter criteria, and Excel ref resolution."""

from __future__ import annotations

import math

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
from plugin.framework.deal_shim import DEAL_MAX_TOKEN
from plugin.framework.errors import UnoObjectError
from tests.strip_bundle import deal_pre_present

import deal


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


def test_filter_string_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(resolve_filter_operator_code):
        pytest.skip("@deal.pre stripped in release bundle")
    too_long = "A" * (DEAL_MAX_TOKEN + 1)
    with pytest.raises(deal.PreContractError):
        resolve_filter_operator_code(too_long)
    with pytest.raises(deal.PreContractError):
        filter_connection_code(too_long)


def test_parse_sheet_filter_criterion_basic() -> None:
    raw = {"field": 0, "operator": "EQUAL", "value": "test"}
    field, op_code, conn, is_num, num_val, str_val = parse_sheet_filter_criterion(raw, is_first=True)
    assert field == 0
    assert conn == 0  # First row connection is AND (0)
    assert is_num is False
    assert str_val == "test"


def test_parse_sheet_filter_criterion_bad_field_raises_uno() -> None:
    with pytest.raises(UnoObjectError, match="Invalid filter 'field'"):
        parse_sheet_filter_criterion({"field": "", "operator": "EQUAL", "value": "x"}, is_first=True)
    with pytest.raises(UnoObjectError, match="Invalid filter 'field'"):
        parse_sheet_filter_criterion({"field": None, "operator": "EQUAL", "value": "x"}, is_first=True)


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


from plugin.calc.calc_addin_data import (
    _unwrap_cell,
    calc_addin_data_to_python,
)


@given(val=st.one_of(st.integers(), st.floats(), st.text(), st.booleans(), st.none()))
def test_unwrap_cell_invariants(val) -> None:
    res = _unwrap_cell(val)
    if val == "":
        assert res is None
    elif isinstance(val, float) and math.isnan(val):
        assert isinstance(res, float) and math.isnan(res)
    elif isinstance(val, (int, float, bool)) or val is None:
        assert res == val


@given(data=st.one_of(
    st.none(),
    st.integers(),
    st.text(),
    st.lists(st.integers()),
    st.lists(st.lists(st.integers())),
    st.tuples(st.tuples(st.integers())),
))
@settings(max_examples=100)
def test_calc_addin_data_to_python_rectangular_invariant(data) -> None:
    grid = calc_addin_data_to_python(data)
    if grid is not None:
        assert isinstance(grid, list)
        if grid:
            first_len = len(grid[0])
            for row in grid:
                assert isinstance(row, list)
                assert len(row) == first_len


def test_filter_operator2_code_to_name_is_off_cover_all() -> None:
    """cover-all 33797534946 leftover (~39m sheet_filter); code→name off, doable later."""
    from pathlib import Path

    from tests.strip_bundle import skip_if_release_build

    skip_if_release_build("scripts/ not in stripped release tree")
    from scripts.crosshair_stream import cover_fqns_for_module

    fqns = cover_fqns_for_module(Path("plugin/calc/sheet_filter_criteria.py"))
    assert not any(f.endswith(".filter_operator2_code_to_name") for f in fqns)

