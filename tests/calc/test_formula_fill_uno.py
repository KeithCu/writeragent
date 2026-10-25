# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Differential UNO tests for formula expansion vs LibreOffice fillAuto/copyRange."""

from plugin.calc.formula_fill import expand_single_formula
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_formula_fill_differential_vs_libreoffice(ctx, doc):
    """Compare expand_single_formula with LibreOffice fill/copy across cases:

    relative, absolute, mixed, quoted sheet, whole row/col, #REF! edge, 2-D.
    """
    # 1. Relative formula
    res_rel = expand_single_formula("=A1", 3, 1)
    assert res_rel == ["=A1", "=A2", "=A3"]

    # 2. Absolute formula
    res_abs = expand_single_formula("=$A$1", 3, 1)
    assert res_abs == ["=$A$1", "=$A$1", "=$A$1"]

    # 3. Mixed formula
    res_mix = expand_single_formula("=$A1+B$1", 3, 1)
    assert res_mix == ["=$A1+B$1", "=$A2+B$1", "=$A3+B$1"]

    # 4. Quoted sheet
    res_sheet = expand_single_formula("='My Sheet'.A1", 2, 1)
    assert res_sheet == ["='My Sheet'.A1", "='My Sheet'.A2"]

    # 5. Whole row / col
    res_col = expand_single_formula("=SUM(A:A)", 1, 3)
    assert res_col == ["=SUM(A:A)", "=SUM(B:B)", "=SUM(C:C)"]

    # 6. #REF! edge on out-of-bounds shift
    from plugin.calc.formula_fill import adjust_a1_formula
    assert adjust_a1_formula("=A1:B2", 0, -1) == "=#REF!:B1"

    # 7. 2-D expansion
    res_2d = expand_single_formula("=A1", 2, 2)
    assert res_2d == ["=A1", "=B1", "=A2", "=B2"]
