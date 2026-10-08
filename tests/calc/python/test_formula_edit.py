# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for =PYTHON() formula parse/rebuild (no LibreOffice)."""

from __future__ import annotations

from plugin.calc.python.formula_edit import (
    build_data_suffix,
    build_new_python_formula,
    cell_looks_python_like,
    escape_code_for_excel_formula,
    escape_code_for_formula,
    format_data_binding_display,
    format_data_binding_text,
    format_excel_data_range,
    format_py_data_range,
    normalize_formula_string,
    parse_data_binding_text,
    parse_python_formula,
    py_call_open_end,
    py_code_arg_is_cell_ref,
    py_formula_has_unquoted_code_ref,
    rebuild_python_formula,
    rebuild_python_formula_with_data,
    sanitize_inline_py_code,
)
import pytest


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


def test_parse_with_comma_data_range():
    """getFormula() may use comma separators (e.g. en-US locale or XLSX import)."""
    parts = parse_python_formula('=PYTHON("np.mean(data)",A2:C2)')
    assert parts is not None
    assert parts.code == "np.mean(data)"
    assert "A2:C2" in parts.data_suffix
    assert format_data_binding_display(parts.data_suffix) == "A2:C2"


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param('=PYTHON("say ""hi""")', 'say "hi"', id="test_parse_escaped_quotes"),
        pytest.param('=PYTHON("a\nb")', "a\nb", id="test_parse_multiline"),
        pytest.param('=PYTHON("sp.prime(100)")', "sp.prime(100)", id="test_parse_sp_prime_quoted"),
        pytest.param("=PYTHON(sp.prime(100))", "sp.prime(100)", id="test_parse_unquoted_code"),
    ],
)
def test_parse_escaped_quotes(value, expected):
    parts = parse_python_formula(value)
    assert parts is not None
    assert parts.code == expected

def test_replace_preserves_data():
    old = '=PYTHON("result = 1"; Sheet1.A1:B2)'
    parts = parse_python_formula(old)
    assert parts is not None
    new = rebuild_python_formula(parts, "result = 2")
    assert 'result = 2' in new
    assert "Sheet1.A1:B2" in new
    reparsed = parse_python_formula(new)
    assert reparsed is not None
    assert reparsed.code == "result = 2"


def test_replace_escapes_quotes():
    old = '=PYTHON("x = 1")'
    parts = parse_python_formula(old)
    assert parts is not None
    new = rebuild_python_formula(parts, 'x = "a"')
    assert '""a""' in new or '""' in new
    assert parse_python_formula(new).code == 'x = "a"'


def test_non_python_returns_none():
    assert parse_python_formula("=SUM(A1)") is None

def test_py_code_arg_is_cell_ref():
    assert py_code_arg_is_cell_ref("A1") is True
    assert py_code_arg_is_cell_ref("$A$1") is True
    assert py_code_arg_is_cell_ref("$A1") is True
    assert py_code_arg_is_cell_ref("A$1") is True
    assert py_code_arg_is_cell_ref("Sheet1.A1") is True
    assert py_code_arg_is_cell_ref("Sheet1!$B$2") is True
    assert py_code_arg_is_cell_ref("'My Sheet'.$B$2") is True
    assert py_code_arg_is_cell_ref("A1:A10") is False
    assert py_code_arg_is_cell_ref("A1:B10") is False
    assert py_code_arg_is_cell_ref("A1+B1") is False
    assert py_code_arg_is_cell_ref("sp.prime(100)") is False
    assert py_code_arg_is_cell_ref('result = 1') is False
    assert py_code_arg_is_cell_ref("np.sum(data)") is False
    assert py_code_arg_is_cell_ref("") is False
    assert py_code_arg_is_cell_ref("Missing.A1") is True


def test_py_formula_has_unquoted_code_ref():
    assert py_formula_has_unquoted_code_ref("=PY($A$1; C1:C10)") is True
    assert py_formula_has_unquoted_code_ref("=PY(A1)") is True
    assert py_formula_has_unquoted_code_ref("=PY($A1)") is True
    assert py_formula_has_unquoted_code_ref("=PY(A$1)") is True
    assert py_formula_has_unquoted_code_ref("=PYTHON($A$1; C1:C10)") is True
    assert py_formula_has_unquoted_code_ref("=PY(Sheet1.$B$2)") is True
    assert py_formula_has_unquoted_code_ref("=PY('My Sheet'.A1)") is True
    assert py_formula_has_unquoted_code_ref("=PY($A$1; B1:B10; C1:C10)") is True
    assert py_formula_has_unquoted_code_ref('=PY("A1")') is False
    assert py_formula_has_unquoted_code_ref('=PY("A1"; C1:C10)') is False
    assert py_formula_has_unquoted_code_ref('=PY("result = 1"; A1)') is False
    assert py_formula_has_unquoted_code_ref("=PY(sp.prime(100))") is False
    assert py_formula_has_unquoted_code_ref("=PY(A1:A10)") is False
    assert py_formula_has_unquoted_code_ref("=PY(A1+B1)") is False


def test_parse_fully_qualified_addin_original_name():
    """LibreOffice getFormula() stores service.method, not the short PY token."""
    fq = "=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY($A$1; C1:C1)"
    parts = parse_python_formula(fq)
    assert parts is not None
    assert parts.code == "$A$1"
    assert py_code_arg_is_cell_ref(parts.code)
    assert "C1:C1" in parts.data_suffix
    assert py_formula_has_unquoted_code_ref(fq) is True
    librepy = '=ORG.EXTENSION.LIBREPY.PYTHONFUNCTION.PYTHON("result = 2")'
    lp = parse_python_formula(librepy)
    assert lp is not None
    assert lp.code == "result = 2"


def test_parse_sheet_qualified_and_two_range_code_refs():
    quoted = parse_python_formula("=PY('My Sheet'.$B$2; C1:C10)")
    assert quoted is not None
    assert quoted.code == "'My Sheet'.$B$2"
    assert py_code_arg_is_cell_ref(quoted.code)
    two = parse_python_formula("=PY($A$1; B1:B10; C1:C10)")
    assert two is not None
    assert parse_data_binding_text(format_data_binding_display(two.data_suffix)) == [
        "B1:B10",
        "C1:C10",
    ]


def test_normalize_array_and_no_equals():
    assert normalize_formula_string('{PYTHON("x")}') == '=PYTHON("x")'
    assert parse_python_formula('{PYTHON("x")}') is not None


def test_build_new_formula_empty():
    assert build_new_python_formula("") == '=PY("")'


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


def test_format_py_and_excel_data_range_real_sheets():
    """Product ranges must still format after the no-regex rewrite (run 32840960268)."""
    assert format_py_data_range("Sheet.A1") == "Sheet.A1"
    assert format_py_data_range("'My Sheet'.A1") == "'My Sheet'.A1"
    assert format_py_data_range("My Sheet.A1") == "'My Sheet'.A1"
    assert format_py_data_range("Sheet!A1") == "Sheet.A1"
    assert format_excel_data_range("Sheet!A1") == "Sheet!A1"
    assert format_excel_data_range("Sheet.A1") == "Sheet!A1"
    assert format_excel_data_range("'My Sheet'.A1") == "'My Sheet'!A1"
    assert format_excel_data_range("My Sheet.A1") == "'My Sheet'!A1"
    assert build_data_suffix(["Sheet.A1"]) == ";Sheet.A1)"
    assert build_data_suffix(["'My Sheet'.A1"]) == ";'My Sheet'.A1)"
    assert build_data_suffix(["Sheet!A1"], separator=",", excel_ranges=True) == ",Sheet!A1)"
    assert build_data_suffix(["My Sheet.A1"], separator=",") == ",'My Sheet'!A1)"


def test_rebuild_python_formula_with_data():
    formula = rebuild_python_formula_with_data("np.sum(data)", ["A1:A10"])
    assert formula == '=PY("np.sum(data)";A1:A10)'
    reparsed = parse_python_formula(formula)
    assert reparsed is not None
    assert reparsed.code == "np.sum(data)"


def test_parse_py_alias():
    parts = parse_python_formula('=PY("result = 1"; A1:B10)')
    assert parts is not None
    assert parts.code == "result = 1"
    assert parts.prefix.upper().startswith("=PY(")
    assert "A1:B10" in parts.data_suffix


def test_rebuild_preserves_python_prefix():
    parts = parse_python_formula('=PYTHON("x"; A1:B2)')
    assert parts is not None
    rebuilt = rebuild_python_formula(parts, "y = 1")
    assert rebuilt.startswith('=PYTHON("y = 1"')
    assert "A1:B2" in rebuilt


def test_format_data_binding_text_round_trip():
    args = ["A1:B10", "C1:C5"]
    text = format_data_binding_text(args)
    assert parse_data_binding_text(text) == args


def test_calc_escape_preserves_hand_written_code():
    """escape_code_for_formula quote-escapes only; hand-written code is not rewritten."""
    code = "x = float(1)"
    assert "+0.0" in sanitize_inline_py_code(code)
    assert escape_code_for_formula(code) == code
    assert escape_code_for_excel_formula(code) == code
    calc = rebuild_python_formula_with_data(code, [])
    xlsx = rebuild_python_formula_with_data(code, [], separator=",", excel_escape=True)
    assert "float(1)" in calc
    assert "float(1)" in xlsx
    assert xlsx.startswith('=PY("')


def test_normalize_curly_quotes_around_code_string():
    """Smart quotes around the PY code arg become ASCII before parse."""
    formula = '=PY(\u201cresult = 1\u201d)'
    normalized = normalize_formula_string(formula)
    assert '"' in normalized
    assert "\u201c" not in normalized and "\u201d" not in normalized
    parts = parse_python_formula(normalized)
    assert parts is not None
    assert parts.code == "result = 1"


def test_normalize_and_parse_bare_py_token():
    """CrossHair check raised PatternError on normalize/parse of ``'PY'``.

    Relib re-parses the PY|PYTHON head regex; a startswith scan must not throw.
    """
    assert normalize_formula_string("PY") == "PY"
    assert parse_python_formula("PY") is None
    assert normalize_formula_string("PY(") == "=PY("
    assert parse_python_formula("py(") is None  # still not a complete call
    assert parse_python_formula('=py("x")') is not None
    assert parse_python_formula('PYTHON("x")') is not None


def test_sanitize_dtype_float_with_control_chars():
    """``dtype=float`` + NUL/SOH grew past nested ``_rewrite_token_calls`` pre.

    Machine-generated sanitize path rewrites; escape_code_for_formula only quote-doubles.
    """
    nul = "dtype=float\x00"
    soh = ".dtype=float\x01"
    assert sanitize_inline_py_code(nul) == "dtype=np.float64\x00"
    assert escape_code_for_formula(nul) == nul
    assert escape_code_for_formula(soh) == soh
    parts = parse_python_formula('=PY("x")')
    assert parts is not None
    rebuilt = rebuild_python_formula(parts, soh)
    assert soh in rebuilt


def test_monaco_save_round_trip_preserves_user_code():
    """Hand-written float("3.5"), int(-3.7), ax.text, np.float survive Monaco save roundtrip."""
    from plugin.calc.python.editor import build_editor_formula_save

    code = 'x = float("3.5")\ny = int(-3.7)\nax.text(1, 2, "hi")\nnp.float64(1)'
    parts = parse_python_formula(f'=PY("{escape_code_for_formula(code)}")')
    saved = build_editor_formula_save(parsed_parts=parts, new_code=code, cell_has_unparsed_python=False)
    assert isinstance(saved, str)
    parsed_after = parse_python_formula(saved)
    assert parsed_after is not None
    assert parsed_after.code == code


def test_dollar_sign_preserved_in_data_args():
    """$ is preserved in emitted data range tokens on rebuild and Monaco save."""
    from plugin.calc.python.editor import build_editor_formula_save

    assert format_py_data_range("$A$1:$B$5") == "$A$1:$B$5"
    assert format_excel_data_range("$A$1:$B$5") == "$A$1:$B$5"
    assert format_py_data_range("Sheet1.$A$1:$B$5") == "Sheet1.$A$1:$B$5"
    assert format_py_data_range("'My Sheet'.$A$1:$B$5") == "'My Sheet'.$A$1:$B$5"

    rebuilt = rebuild_python_formula_with_data("x = 1", ["$A$1:$B$5"])
    assert rebuilt == '=PY("x = 1";$A$1:$B$5)'

    parts = parse_python_formula('=PY("x = 1";$A$1:$B$5)')
    assert parts is not None
    saved = build_editor_formula_save(
        parsed_parts=parts,
        new_code="x = 2",
        cell_has_unparsed_python=False,
        data_binding_text="$A$1:$B$5",
    )
    assert saved == '=PY("x = 2";$A$1:$B$5)'


def test_rebuild_preserves_prefix():
    """Existing PYTHON, lowercase py, or OriginalName prefixes are kept."""
    cases = (
        '=PYTHON("x = 1")',
        '=py("x = 1")',
        '=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON("x = 1")',
    )
    for orig in cases:
        parts = parse_python_formula(orig)
        assert parts is not None
        rebuilt = rebuild_python_formula(parts, "x = 2")
        assert rebuilt.startswith(f'{parts.prefix}"x = 2"')
        rebuilt_data = rebuild_python_formula_with_data("x = 2", ["A1"], parts=parts)
        assert rebuilt_data.startswith(f'{parts.prefix}"x = 2"')


def test_long_multi_range_suffix_parses_under_deal():
    """_is_data_arg_separator must accept data suffixes longer than DEAL_MAX_TOKEN (64)."""
    ranges = [f"Sheet{i}!$A${i}:$B${i+1}" for i in range(10)]
    suffix = "; " + "; ".join(ranges) + ")"
    formula = f'=PY("x = 1"{suffix}'
    assert len(suffix) > 64
    parts = parse_python_formula(formula)
    assert parts is not None
    assert parts.code == "x = 1"
    assert parts.data_suffix == suffix


def test_cell_looks_python_like_and_py_call_open_end():
    assert cell_looks_python_like('=PY("x = 1")')
    assert cell_looks_python_like('=PYTHON("x = 1")')
    assert cell_looks_python_like('=py("x = 1")')
    assert not cell_looks_python_like("=SUM(A1:B10)")
    assert not cell_looks_python_like("")

    assert py_call_open_end("=PY(", require_equals=True) == 4
    assert py_call_open_end("PY(", require_equals=False) == 3
    assert py_call_open_end("PY(", require_equals=True) is None

