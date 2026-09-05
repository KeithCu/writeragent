# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formal verification (Hypothesis + Deal) for Excel/Calc PY formula DAG translation."""

from __future__ import annotations

from typing import get_args, get_origin, get_type_hints

import pytest
from hypothesis import given, strategies as st

import deal
from plugin.calc.excel_py_convert.models import ExcelPyCell, ExcelWorkbookModel
from plugin.calc.excel_py_convert.to_dag import (
    _deal_convert_scripts_ok,
    _deal_excel_src_ok,
    _find_xl_calls,
    _normalize_bindings,
    _normalize_excel_placeholders,
    _placeholder_to_data_index,
    _skip_string,
    _xl_binding_expr,
    convert_cell_to_dag,
    convert_model_to_dag,
    rewrite_excel_code,
)
from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse
from plugin.framework.deal_shim import DEAL_MAX_CMD_ARGS, DEAL_MAX_PLACEHOLDER_INDEX, DEAL_MAX_SOURCE, DEAL_MAX_XL_EXPR
from tests.strip_bundle import deal_pre_present


def test_xl_binding_expr_header_mode_annotation_is_str() -> None:
    """CrossHair cannot proxy Literal; HeaderMode must not appear in proxied params."""
    hints = get_type_hints(_xl_binding_expr)
    assert hints["header_mode"] is str
    bind_hints = get_type_hints(_normalize_bindings)
    header_modes = bind_hints["header_modes"]
    assert get_origin(header_modes) is dict
    _key, value = get_args(header_modes)
    assert value is str


@given(st.integers(min_value=2, max_value=2 + DEAL_MAX_PLACEHOLDER_INDEX))
def test_placeholder_to_data_index_invariant(p_num: int) -> None:
    idx = _placeholder_to_data_index(p_num)
    assert idx >= 0
    assert idx == p_num - 2


@given(st.integers(min_value=0, max_value=DEAL_MAX_PLACEHOLDER_INDEX), st.sampled_from(["true", "false", "omit"]))
def test_xl_binding_expr_invariants(idx: int, header_mode: str) -> None:
    expr = _xl_binding_expr(idx, header_mode)
    assert expr.startswith("xl(")
    assert expr.endswith(")")
    p_str = f'"%P{idx + 2}%"'
    assert p_str in expr
    if header_mode == "true":
        assert "headers=True" in expr
    elif header_mode == "false":
        assert "headers=False" in expr
    assert len(expr) <= DEAL_MAX_XL_EXPR


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=127), max_size=64))
def test_normalize_excel_placeholders_length_invariant(src: str) -> None:
    normalized = _normalize_excel_placeholders(src)
    assert len(normalized) == len(src)
    # Check bare %P2% outside quotes gets replaced by _P2_
    if "%P2%" in src and '"' not in src and "'" not in src and "#" not in src:
        assert "%P2%" not in normalized
        assert "_P2_" in normalized


@given(st.text(max_size=64))
def test_normalize_lo_formula_for_parse_invariants(formula: str) -> None:
    result = normalize_lo_formula_for_parse(formula)
    assert isinstance(result, str)
    # Curly quotes should always be normalized away
    assert "\u201c" not in result
    assert "\u201d" not in result


def test_placeholder_index_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(_placeholder_to_data_index):
        pytest.skip("@deal.pre stripped in release bundle")
    with pytest.raises(deal.PreContractError):
        _placeholder_to_data_index(2 + DEAL_MAX_PLACEHOLDER_INDEX + 1)
    with pytest.raises(deal.PreContractError):
        _xl_binding_expr(DEAL_MAX_PLACEHOLDER_INDEX + 1, "omit")
    assert _placeholder_to_data_index(2 + DEAL_MAX_PLACEHOLDER_INDEX) == DEAL_MAX_PLACEHOLDER_INDEX
    assert '"%P' in _xl_binding_expr(DEAL_MAX_PLACEHOLDER_INDEX, "omit")


def test_dag_wrapper_overflow_pre_fails_closed() -> None:
    """Callee bounds do not stop CrossHair covering wrappers; wrappers must pre themselves."""
    if not deal_pre_present(_find_xl_calls):
        pytest.skip("@deal.pre stripped in release bundle")
    too_long = "x" * (DEAL_MAX_SOURCE + 1)
    with pytest.raises(deal.PreContractError):
        _find_xl_calls(too_long)
    with pytest.raises(deal.PreContractError):
        rewrite_excel_code(too_long, num_deps=0)
    with pytest.raises(deal.PreContractError):
        rewrite_excel_code("x", num_deps=DEAL_MAX_CMD_ARGS + 1)
    with pytest.raises(deal.PreContractError):
        _skip_string("ab", -1)
    with pytest.raises(deal.PreContractError):
        _skip_string("ab", 2)
    with pytest.raises(deal.PreContractError):
        convert_model_to_dag(
            ExcelWorkbookModel(scripts=["x"] * (DEAL_MAX_CMD_ARGS + 1), cells=[])
        )
    with pytest.raises(deal.PreContractError):
        convert_model_to_dag(
            ExcelWorkbookModel(
                scripts=[],
                cells=[
                    ExcelPyCell(
                        sheet="",
                        cell="",
                        script_index=0,
                        return_type=0,
                        deps=["x"] * (DEAL_MAX_CMD_ARGS + 1),
                    )
                ],
            )
        )


def test_find_xl_calls_accepts_tab_then_quote() -> None:
    """Pytest domain is DEAL_MAX_SOURCE; a two-char quote walk is a legal script."""
    calls, issues = _find_xl_calls('\t"')
    assert calls == []
    assert isinstance(issues, list)


def test_deal_convert_scripts_ok_matches_excel_src_ok() -> None:
    """Wrapper scripts pre must share rewrite_excel_code's _deal_excel_src_ok.

    Pytest keeps str_bounded (NUL allowed); CrossHair alphabet rejects NUL
    (check-all 33935176527: convert_model_to_dag(scripts=['\\x00'])).
    """
    assert _deal_excel_src_ok("\x00")  # pytest: str_bounded; CrossHair pre rejects
    assert _deal_convert_scripts_ok(ExcelWorkbookModel(scripts=["\x00"], cells=[]))
    assert _deal_convert_scripts_ok(ExcelWorkbookModel(scripts=["x"], cells=[]))


def test_convert_cell_to_dag_script_index_oor_fail_closed() -> None:
    """Pre must not require in-range script_index; body returns unconverted cell."""
    model = ExcelWorkbookModel(scripts=["df = 1"], cells=[])
    cell = ExcelPyCell(sheet="S", cell="A1", script_index=9, return_type=0, deps=[])
    converted = convert_cell_to_dag(model, cell)
    assert converted.converted is False
    assert "out of range" in (converted.issues or [""])[0]
    cell_neg = ExcelPyCell(sheet="S", cell="A1", script_index=-1, return_type=0, deps=[])
    converted_neg = convert_cell_to_dag(model, cell_neg)
    assert converted_neg.converted is False
    assert "out of range" in (converted_neg.issues or [""])[0]
