# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Argument validation tests for InsertMathDraw."""

from __future__ import annotations

from plugin.draw.math_insert import InsertMathDraw


def test_insert_math_validate_accepts_latex_payload() -> None:
    ok, err = InsertMathDraw().validate(
        doc_type="impress",
        formula_type="latex",
        formula="E = mc^2",
        page=0,
        x=2000,
        y=2000,
    )
    assert (ok, err) == (True, None)


def test_insert_math_validate_accepts_mathml_payload() -> None:
    mml = '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>'
    ok, err = InsertMathDraw().validate(
        doc_type="impress",
        formula_type="mathml",
        formula=mml,
        page=0,
        x=100,
        y=100,
    )
    assert (ok, err) == (True, None)


def test_insert_math_validate_rejects_missing_page() -> None:
    ok, err = InsertMathDraw().validate(
        doc_type="impress",
        formula_type="latex",
        formula="a",
        x=0,
        y=0,
    )
    assert (ok, err) == (False, "Missing required parameter: page")
