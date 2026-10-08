# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure unit tests for Calc formula fill-down / =PY span-peek (no soffice)."""

from __future__ import annotations

import pytest

from plugin.calc.formula_fill import (
    adjust_a1_formula,
    classify_sheet_ref_span,
    expand_single_formula,
    should_adjust_formula_fill,
)
from plugin.framework.errors import CalcError


@pytest.mark.parametrize(
    "value, value_2, value_3, value_4, value_5",
    [
        # J2:J5 + =H2 → H2…H5 (the AFC pin case).
        pytest.param("=H2", "=H2", "=H3", "=H4", "=H5", id="test_j2_j5_h2_fill_down"),
        pytest.param("=$H$2", "=$H$2", "=$H$2", "=$H$2", "=$H$2", id="test_absolute_dollar_stays_on_fill_down"),
    ],
)
def test_j2_j5_h2_fill_down(value, value_2, value_3, value_4, value_5):
    assert expand_single_formula(value, 4, 1) == [value_2, value_3, value_4, value_5]

@pytest.mark.parametrize(
    "value, value_2, value_3, value_4, value_5, value_6",
    [
        pytest.param("=$H2+H$2", 3, 1, "=$H2+H$2", "=$H3+H$2", "=$H4+H$2", id="test_mixed_absolute_fill_down"),
        pytest.param("=A1", 1, 3, "=A1", "=B1", "=C1", id="test_fill_across_relative"),
    ],
)
def test_mixed_absolute_fill_down(value, value_2, value_3, value_4, value_5, value_6):
    assert expand_single_formula(value, value_2, value_3) == [
        value_4,
        value_5,
        value_6,
    ]

@pytest.mark.parametrize(
    "value, value_2, value_3",
    [
        pytest.param('=PY("result = data.mean()"; A1:H10)', 4, 4, id="test_py_multirow_datarange_is_verbatim"),
        pytest.param('=PY("looks like A1:H10"; A1:H10)', 3, 3, id="test_py_quoted_block_plus_multirow_datarange_is_verbatim"),
        pytest.param('=PY("unterminated', 3, 3, id="test_py_unparseable_is_verbatim"),
    ],
)
def test_py_multirow_datarange_is_verbatim(value, value_2, value_3):
    formula = value
    assert should_adjust_formula_fill(formula) is False
    assert expand_single_formula(formula, value_2, 1) == [formula] * value_3


def test_py_samerow_datarange_adjusts_down_a_column():
    formula = '=PY("result = data.mean()"; A2:H2)'
    assert should_adjust_formula_fill(formula) is True
    assert expand_single_formula(formula, 3, 1) == [
        '=PY("result = data.mean()"; A2:H2)',
        '=PY("result = data.mean()"; A3:H3)',
        '=PY("result = data.mean()"; A4:H4)',
    ]


def test_twod_plus_one_formula_is_error():
    with pytest.raises(CalcError, match="2-D range"):
        expand_single_formula("=H2", 3, 2)


def test_py_quoted_a1_looking_text_is_not_a_sheet_ref():
    """A1:H10 inside the Python string must not force verbatim by itself."""
    formula = '=PY("block = \'A1:H10\'; result = 1")'
    assert should_adjust_formula_fill(formula) is True
    assert adjust_a1_formula(formula, 0, 1) == formula

def test_py_whole_column_datarange_is_verbatim():
    formula = '=PY("result = data"; A:A)'
    assert classify_sheet_ref_span("A:A") == "multi_row"
    assert should_adjust_formula_fill(formula) is False

def test_py_index_arg_is_ambiguous_verbatim():
    formula = '=PY("result = data"; 1)'
    assert should_adjust_formula_fill(formula) is False


def test_python_and_pythonfunction_aliases():
    same = '=PYTHON("result = data.mean()"; A2:H2)'
    block = "=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY(\"x\"; A1:H10)"
    assert should_adjust_formula_fill(same) is True
    assert should_adjust_formula_fill(block) is False


def test_ordinary_range_and_function_not_mangled():
    assert adjust_a1_formula("=SUM(A1:A3)+LOG10(B1)", 0, 1) == "=SUM(A2:A4)+LOG10(B2)"
    assert adjust_a1_formula('=IF(A1="H2"; B1; C1)', 0, 1) == '=IF(A2="H2"; B2; C2)'


def test_sheet_qualified_common_forms():
    assert adjust_a1_formula("=Sheet1.A1", 0, 1) == "=Sheet1.A2"
    assert adjust_a1_formula("='Data Sheet'!B2", 0, 1) == "='Data Sheet'!B3"
    assert adjust_a1_formula("=Sheet1.A1:B2", 0, 1) == "=Sheet1.A2:B3"


def test_out_of_bounds_becomes_ref():
    assert adjust_a1_formula("=A1", 0, -1) == "=#REF!"


def test_single_cell_expand_is_identity():
    assert expand_single_formula("=H2", 1, 1) == ["=H2"]


def test_span_classifier():
    assert classify_sheet_ref_span("H2") == "single_cell"
    assert classify_sheet_ref_span("A2:H2") == "same_row"
    assert classify_sheet_ref_span("A1:H10") == "multi_row"
    assert classify_sheet_ref_span("A:A") == "multi_row"
    assert classify_sheet_ref_span("1:1") == "same_row"
    assert classify_sheet_ref_span("SalesData") == "ambiguous"
