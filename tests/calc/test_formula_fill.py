# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure unit tests for Calc formula fill-down / =PY span-peek (no soffice)."""

from __future__ import annotations

import pytest

from plugin.calc.formula_fill import (
    adjust_a1_formula,
    dot_sheet_refs,
    expand_single_formula,
    fill_repeated_formula_columns,
)


@pytest.mark.parametrize(
    "value, value_2, value_3, value_4, value_5",
    [
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
    assert expand_single_formula(formula, value_2, 1) == [formula] * value_3


def test_py_samerow_datarange_adjusts_down_a_column():
    formula = '=PY("result = data.mean()"; A2:H2)'
    assert expand_single_formula(formula, 3, 1) == [
        '=PY("result = data.mean()"; A2:H2)',
        '=PY("result = data.mean()"; A3:H3)',
        '=PY("result = data.mean()"; A4:H4)',
    ]


def test_py_quoted_a1_looking_text_is_not_a_sheet_ref():
    """A1:H10 inside the Python string must not force verbatim by itself."""
    formula = '=PY("block = \'A1:H10\'; result = 1")'
    assert adjust_a1_formula(formula, 0, 1) == formula
    assert expand_single_formula(formula, 2, 1) == [formula, formula]


def test_py_whole_column_datarange_is_verbatim():
    formula = '=PY("result = data"; A:A)'
    assert expand_single_formula(formula, 2, 1) == [formula, formula]


def test_py_index_arg_is_ambiguous_verbatim():
    formula = '=PY("result = data"; 1)'
    assert expand_single_formula(formula, 2, 1) == [formula, formula]


def test_python_and_pythonfunction_aliases():
    same = '=PYTHON("result = data.mean()"; A2:H2)'
    block = '=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("x"; A1:H10)'
    assert expand_single_formula(same, 2, 1) == [
        '=PYTHON("result = data.mean()"; A2:H2)',
        '=PYTHON("result = data.mean()"; A3:H3)',
    ]
    assert expand_single_formula(block, 2, 1) == [block, block]
    assert adjust_a1_formula(same, 0, 1, axis="rows") == '=PYTHON("result = data.mean()"; A3:H3)'
    assert adjust_a1_formula(block, 0, 1, axis="rows") == block


def test_quote_and_structured_reference_skipping():
    # Item 58: scanner skips '...' and [...]
    assert adjust_a1_formula("='Q1 Data'.Tax", 0, 1) == "='Q1 Data'.Tax"
    assert adjust_a1_formula("='A1:B2'.MyRange", 0, 1) == "='A1:B2'.MyRange"
    assert adjust_a1_formula("=SUM(Table1[Q1])", 0, 1) == "=SUM(Table1[Q1])"
    assert adjust_a1_formula("='Q1 Data'.A1", 0, 1) == "='Q1 Data'.A2"


def test_py_direction_aware_span_policy():
    # Item 59: per-ref direction-aware policy
    f_horizontal = '=PY("x"; A2:H2)'
    # Fill-down shifts single-row refs
    assert adjust_a1_formula(f_horizontal, 0, 1, axis="rows") == '=PY("x"; A3:H3)'
    # Fill-across leaves multi-col refs verbatim
    assert adjust_a1_formula(f_horizontal, 1, 0, axis="cols") == '=PY("x"; A2:H2)'

    f_vertical = '=PY("x"; A2:A4)'
    # Fill-down leaves multi-row refs verbatim
    assert adjust_a1_formula(f_vertical, 0, 1, axis="rows") == '=PY("x"; A2:A4)'
    # Fill-across shifts single-col refs
    assert adjust_a1_formula(f_vertical, 1, 0, axis="cols") == '=PY("x"; B2:B4)'


def test_twod_formula_expansion():
    # Item 62: 2-D fill allowed
    res = expand_single_formula("=A1", 2, 2)
    assert res == ["=A1", "=B1", "=A2", "=B2"]


def test_out_of_bounds_becomes_ref():
    assert adjust_a1_formula("=A1", 0, -1) == "=#REF!"


def test_out_of_bounds_ref_keeps_structure():
    # Item 61: partial out of bounds formats only bad end as #REF!
    assert adjust_a1_formula("=A1:B2", 0, -1) == "=#REF!:B1"
    assert adjust_a1_formula("=A1", 0, -1) == "=#REF!"


def test_ordinary_range_and_function_not_mangled():
    assert adjust_a1_formula("=SUM(A1:A3)+LOG10(B1)", 0, 1) == "=SUM(A2:A4)+LOG10(B2)"
    assert adjust_a1_formula('=IF(A1="H2"; B1; C1)', 0, 1) == '=IF(A2="H2"; B2; C2)'


def test_sheet_qualified_common_forms():
    assert adjust_a1_formula("=Sheet1.A1", 0, 1) == "=Sheet1.A2"
    assert adjust_a1_formula("='Data Sheet'!B2", 0, 1) == "='Data Sheet'!B3"
    assert adjust_a1_formula("=Sheet1.A1:B2", 0, 1) == "=Sheet1.A2:B3"


def test_single_cell_expand_is_identity():
    assert expand_single_formula("=H2", 1, 1) == ["=H2"]


@pytest.mark.parametrize(
    "formula, rows, cols, expected",
    [
        pytest.param("='My Sheet'.A1", 2, 1, ["='My Sheet'.A1", "='My Sheet'.A2"], id="quoted_sheet_fill_down"),
        pytest.param("=SUM(A:A)", 1, 3, ["=SUM(A:A)", "=SUM(B:B)", "=SUM(C:C)"], id="whole_col_fill_across"),
    ],
)
def test_quoted_sheet_and_whole_column_expand(formula, rows, cols, expected):
    assert expand_single_formula(formula, rows, cols) == expected


@pytest.mark.parametrize(
    "formula, expected",
    [
        pytest.param("=Orders!A1", "=Orders.A1", id="bare_sheet"),
        pytest.param("=SUM(orders!a1:A2)", "=SUM(Orders.A1:A2)", id="case_normalized_range"),
        pytest.param("='My Sheet'!A1", "='My Sheet'.A1", id="quoted_sheet"),
        pytest.param("='O''Brien'!A1", "='O''Brien'.A1", id="doubled_apostrophe"),
        pytest.param("=$Orders!$A$1", "=$Orders.$A$1", id="absolute_sheet_and_cell"),
        pytest.param("=Orders!A1:Orders!B2", "=Orders.A1:Orders.B2", id="both_ends_prefixed"),
        pytest.param('=PY("x"; Orders!A1:B5)', '=PY("x"; Orders.A1:B5)', id="py_data_range"),
        pytest.param('="Orders!A1"', '="Orders!A1"', id="string_literal_untouched"),
        pytest.param("=Missing!A1", "=Missing!A1", id="unknown_sheet_untouched"),
        pytest.param("=Orders.A1", "=Orders.A1", id="already_dotted"),
    ],
)
def test_dot_sheet_refs(formula: str, expected: str) -> None:
    sheets = {"orders": "Orders", "my sheet": "My Sheet", "o'brien": "O'Brien"}
    assert dot_sheet_refs(formula, lambda name: sheets.get(name.casefold())) == expected


def test_quoted_doubled_apostrophe_prefix_fill_down() -> None:
    assert adjust_a1_formula("='O''Brien'!A1", 0, 1) == "='O''Brien'!A2"


@pytest.mark.parametrize(
    "values, first_row, num_rows, num_cols, expected_values, expected_cols",
    [
        pytest.param(
            ["=B2*2", "x", "=B2*2", "y"],
            1,
            2,
            2,
            ["=B2*2", "x", "=B3*2", "y"],
            [0],
            id="row_aligned_col_fills_down",
        ),
        pytest.param(
            ["=SUM(B$2:B2)", "=SUM(B$2:B2)", "=SUM(B$2:B2)"],
            1,
            3,
            1,
            ["=SUM(B$2:B2)", "=SUM(B$2:B3)", "=SUM(B$2:B4)"],
            [0],
            id="running_total_mixed_abs_fills_down",
        ),
        pytest.param(["=B1", "=B1", "=B1"], 1, 3, 1, ["=B1", "=B1", "=B1"], [], id="header_ref_stays_pinned"),
        pytest.param(["=$B$2", "=$B$2", "=$B$2"], 1, 3, 1, ["=$B$2", "=$B$2", "=$B$2"], [], id="absolute_stays_pinned"),
        pytest.param(
            ["=SUM(B2:B10)", "=SUM(B2:B10)", "=SUM(B2:B10)"],
            1,
            3,
            1,
            ["=SUM(B2:B10)", "=SUM(B2:B10)", "=SUM(B2:B10)"],
            [],
            id="multi_row_span_stays_pinned",
        ),
        pytest.param(["=B2*2", "=B3*2"], 1, 2, 1, ["=B2*2", "=B3*2"], [], id="non_identical_untouched"),
        pytest.param(["=B2*2"], 1, 1, 1, ["=B2*2"], [], id="single_row_untouched"),
        pytest.param(
            ['=PY("r"; A1:H500)', '=PY("r"; A1:H500)'],
            1,
            2,
            1,
            ['=PY("r"; A1:H500)', '=PY("r"; A1:H500)'],
            [],
            id="py_block_untouched",
        ),
        pytest.param(["plain", "plain", "plain"], 1, 3, 1, ["plain", "plain", "plain"], [], id="plain_text_untouched"),
    ],
)
def test_fill_repeated_formula_columns(values, first_row, num_rows, num_cols, expected_values, expected_cols) -> None:
    got_values, got_cols = fill_repeated_formula_columns(values, first_row, num_rows, num_cols)
    assert got_values == expected_values
    assert got_cols == expected_cols
