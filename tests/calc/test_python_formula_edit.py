# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for =PYTHON() formula parse/rebuild (no LibreOffice)."""

from __future__ import annotations

from plugin.calc.python_formula_edit import (
    build_data_suffix,
    build_new_python_formula,
    format_data_binding_display,
    format_data_binding_text,
    normalize_formula_string,
    parse_data_binding_text,
    parse_python_formula,
    rebuild_python_formula,
    rebuild_python_formula_with_data,
    replace_python_code,
)


def test_parse_simple():
    parts = parse_python_formula('=PYTHON("result = 1")')
    assert parts is not None
    assert parts.code == "result = 1"
    assert parts.data_suffix == ")"


def test_parse_with_data_range():
    parts = parse_python_formula('=PYTHON("result = 1"; A1:B10)')
    assert parts is not None
    assert parts.code == "result = 1"
    assert "A1:B10" in parts.data_suffix


def test_parse_escaped_quotes():
    parts = parse_python_formula('=PYTHON("say ""hi""")')
    assert parts is not None
    assert parts.code == 'say "hi"'


def test_parse_multiline():
    parts = parse_python_formula('=PYTHON("a\nb")')
    assert parts is not None
    assert parts.code == "a\nb"


def test_replace_preserves_data():
    old = '=PYTHON("result = 1"; Sheet1.A1:B2)'
    new = replace_python_code(old, "result = 2")
    assert new is not None
    assert 'result = 2' in new
    assert "Sheet1.A1:B2" in new
    reparsed = parse_python_formula(new)
    assert reparsed is not None
    assert reparsed.code == "result = 2"


def test_replace_escapes_quotes():
    old = '=PYTHON("x = 1")'
    new = replace_python_code(old, 'x = "a"')
    assert new is not None
    assert '""a""' in new or '""' in new
    assert parse_python_formula(new).code == 'x = "a"'


def test_non_python_returns_none():
    assert parse_python_formula("=SUM(A1)") is None
    assert replace_python_code("=SUM(A1)", "x") is None


def test_parse_sp_prime_quoted():
    parts = parse_python_formula('=PYTHON("sp.prime(100)")')
    assert parts is not None
    assert parts.code == "sp.prime(100)"


def test_parse_unquoted_code():
    parts = parse_python_formula("=PYTHON(sp.prime(100))")
    assert parts is not None
    assert parts.code == "sp.prime(100)"


def test_normalize_array_and_no_equals():
    assert normalize_formula_string('{PYTHON("x")}') == '=PYTHON("x")'
    assert parse_python_formula('{PYTHON("x")}') is not None


def test_build_new_formula_empty():
    assert build_new_python_formula("") == '=PYTHON("")'


def test_build_new_formula_escapes():
    assert '""' in build_new_python_formula('say "hi"')


def test_rebuild_preserves_data_suffix_from_parts():
    parts = parse_python_formula('=PYTHON("x"; A1:B2; C3)')
    assert parts is not None
    rebuilt = rebuild_python_formula(parts, "y = 1")
    assert "A1:B2" in rebuilt
    assert "C3" in rebuilt
    assert parse_python_formula(rebuilt) is not None
    assert parse_python_formula(rebuilt).code == "y = 1"


def test_format_data_binding_display():
    assert format_data_binding_display(")") == ""
    assert format_data_binding_display(";A1:B10)") == "A1:B10"
    assert format_data_binding_display(";A1; C1:C5)") == "A1; C1:C5"


def test_parse_data_binding_text_single():
    assert parse_data_binding_text("A1:C1") == ["A1:C1"]
    assert parse_data_binding_text("  Sheet1.A1:B2  ") == ["Sheet1.A1:B2"]


def test_parse_data_binding_text_multi():
    assert parse_data_binding_text("A1:C1, C1:C5") == ["A1:C1", "C1:C5"]
    assert parse_data_binding_text("A1; C1:C5") == ["A1", "C1:C5"]
    assert parse_data_binding_text("[A1:C1, C1:C5]") == ["A1:C1", "C1:C5"]


def test_parse_data_binding_text_empty():
    assert parse_data_binding_text("") == []
    assert parse_data_binding_text("   ") == []


def test_build_data_suffix():
    assert build_data_suffix([]) == ")"
    assert build_data_suffix(["A1:B10"]) == ";A1:B10)"
    assert build_data_suffix(["A1:B10", "C1:C5"]) == ";A1:B10;C1:C5)"


def test_rebuild_python_formula_with_data():
    formula = rebuild_python_formula_with_data("np.sum(data)", ["A1:A10"])
    assert formula == '=PYTHON("np.sum(data)";A1:A10)'
    reparsed = parse_python_formula(formula)
    assert reparsed is not None
    assert reparsed.code == "np.sum(data)"


def test_format_data_binding_text_round_trip():
    args = ["A1:B10", "C1:C5"]
    text = format_data_binding_text(args)
    assert parse_data_binding_text(text) == args

