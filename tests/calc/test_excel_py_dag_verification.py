# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formal verification (Hypothesis + Deal) for Excel/Calc PY formula DAG translation."""

from __future__ import annotations

from typing import get_args, get_origin, get_type_hints

from hypothesis import given, strategies as st

from plugin.calc.excel_py_convert.to_dag import (
    _normalize_bindings,
    _normalize_excel_placeholders,
    _placeholder_to_data_index,
    _xl_binding_expr,
)
from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse


def test_xl_binding_expr_header_mode_annotation_is_str() -> None:
    """CrossHair cannot proxy Literal; HeaderMode must not appear in proxied params."""
    hints = get_type_hints(_xl_binding_expr)
    assert hints["header_mode"] is str
    bind_hints = get_type_hints(_normalize_bindings)
    header_modes = bind_hints["header_modes"]
    assert get_origin(header_modes) is dict
    _key, value = get_args(header_modes)
    assert value is str


@given(st.integers(min_value=2, max_value=10000))
def test_placeholder_to_data_index_invariant(p_num: int) -> None:
    idx = _placeholder_to_data_index(p_num)
    assert idx >= 0
    assert idx == p_num - 2


@given(st.integers(min_value=0, max_value=10000), st.sampled_from(["true", "false", "omit"]))
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


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=127)))
def test_normalize_excel_placeholders_length_invariant(src: str) -> None:
    normalized = _normalize_excel_placeholders(src)
    assert len(normalized) == len(src)
    # Check bare %P2% outside quotes gets replaced by _P2_
    if "%P2%" in src and '"' not in src and "'" not in src and "#" not in src:
        assert "%P2%" not in normalized
        assert "_P2_" in normalized


@given(st.text())
def test_normalize_lo_formula_for_parse_invariants(formula: str) -> None:
    result = normalize_lo_formula_for_parse(formula)
    assert isinstance(result, str)
    # Curly quotes should always be normalized away
    assert "\u201c" not in result
    assert "\u201d" not in result
