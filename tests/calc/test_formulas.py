# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc formula evaluation messages (no soffice)."""

from plugin.calc.formulas import formula_evaluation_error_message


def test_error_503_is_num_not_div_zero():
    # 503 and 532 used to share a #DIV/0! string that always said code 532.
    message = formula_evaluation_error_message(503)
    assert message == "Formula evaluation error: #NUM! (Invalid numeric value, code 503)"
    assert "#DIV/0!" not in message
    assert "532" not in message


def test_error_532_is_div_zero():
    message = formula_evaluation_error_message(532)
    assert "#DIV/0!" in message
    assert "code 532" in message
    assert "#NUM!" not in message


def test_other_formula_error_codes_keep_their_labels():
    assert "Pair missing bracket (code 508)" in formula_evaluation_error_message(508)
    assert "#REF!" in formula_evaluation_error_message(524)
    assert formula_evaluation_error_message(519) == "Formula evaluation error: code 519"
