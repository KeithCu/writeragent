# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for =PYTHON() formula parse/rebuild (no LibreOffice)."""

from __future__ import annotations

import io
import re
import shutil
from pathlib import Path

import deal
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.calc.python.formula_edit import (
    PyFormula,
    _parse_quoted_string,
    _parse_unquoted_code_arg,
    assemble_python_formula,
    build_data_suffix,
    escape_code_for_formula,
    format_data_binding_display,
    format_data_binding_text,
    format_excel_data_range,
    format_py_data_range,
    normalize_formula_string,
    parse_data_binding_text,
    py_code_arg_is_cell_ref,
)
from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse
from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, DEAL_MAX_SOURCE, DEAL_MAX_TOKEN
from tests.harness.strip_bundle import deal_pre_present, expect_pre_or_body
from tests.harness.vhs_budget import vhs_max_examples


def test_parse_simple():
    parts = PyFormula.parse('=PYTHON("result = 1")')
    assert parts is not None
    assert parts.code == "result = 1"
    assert parts.data_suffix == ")"


def test_parse_with_data_range():
    parts = PyFormula.parse('=PYTHON("result = 1"; A1:B10)')
    assert parts is not None
    assert parts.code == "result = 1"
    assert "A1:B10" in parts.data_suffix


def test_parse_with_comma_data_range():
    """getFormula() may use comma separators (e.g. en-US locale or XLSX import)."""
    parts = PyFormula.parse('=PYTHON("np.mean(data)",A2:C2)')
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
    parts = PyFormula.parse(value)
    assert parts is not None
    assert parts.code == expected


def test_replace_preserves_data():
    old = '=PYTHON("result = 1"; Sheet1.A1:B2)'
    parts = PyFormula.parse(old)
    assert parts is not None
    new = parts.render("result = 2")
    assert new.startswith('=PYTHON("result = 2"')
    assert "Sheet1.A1:B2" in new
    reparsed = PyFormula.parse(new)
    assert reparsed is not None
    assert reparsed.code == "result = 2"


def test_replace_escapes_quotes():
    old = '=PYTHON("x = 1")'
    parts = PyFormula.parse(old)
    assert parts is not None
    new = parts.render('x = "a"')
    assert '""a""' in new or '""' in new
    parsed_new = PyFormula.parse(new)
    assert parsed_new is not None
    assert parsed_new.code == 'x = "a"'


def test_non_python_returns_none():
    assert PyFormula.parse("=SUM(A1)") is None


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
    assert PyFormula.parse("=PY($A$1; C1:C10)").code_is_ref is True
    assert PyFormula.parse("=PY(A1)").code_is_ref is True
    assert PyFormula.parse("=PY($A1)").code_is_ref is True
    assert PyFormula.parse("=PY(A$1)").code_is_ref is True
    assert PyFormula.parse("=PYTHON($A$1; C1:C10)").code_is_ref is True
    assert PyFormula.parse("=PY(Sheet1.$B$2)").code_is_ref is True
    assert PyFormula.parse("=PY('My Sheet'.A1)").code_is_ref is True
    assert PyFormula.parse("=PY($A$1; B1:B10; C1:C10)").code_is_ref is True
    assert PyFormula.parse('=PY("A1")').code_is_ref is False
    assert PyFormula.parse('=PY("A1"; C1:C10)').code_is_ref is False
    assert PyFormula.parse('=PY("result = 1"; A1)').code_is_ref is False
    assert PyFormula.parse("=PY(sp.prime(100))").code_is_ref is False
    assert PyFormula.parse("=PY(A1:A10)").code_is_ref is False
    assert PyFormula.parse("=PY(A1+B1)").code_is_ref is False


def test_unquoted_code_arg_skips_string_literals():
    parts = PyFormula.parse('=PY(foo(")"); A1)')
    assert parts is not None and parts.code == 'foo(")")'


def test_parse_fully_qualified_addin_original_name():
    """LibreOffice getFormula() stores service.method, not the short PY token."""
    fq = "=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY($A$1; C1:C1)"
    parts = PyFormula.parse(fq)
    assert parts is not None
    assert parts.code == "$A$1"
    assert py_code_arg_is_cell_ref(parts.code)
    assert "C1:C1" in parts.data_suffix
    assert parts.code_is_ref is True
    librepy = '=ORG.EXTENSION.LIBREPY.PYTHONFUNCTION.PYTHON("result = 2")'
    lp = PyFormula.parse(librepy)
    assert lp is not None
    assert lp.code == "result = 2"


def test_parse_sheet_qualified_and_two_range_code_refs():
    quoted = PyFormula.parse("=PY('My Sheet'.$B$2; C1:C10)")
    assert quoted is not None
    assert quoted.code == "'My Sheet'.$B$2"
    assert py_code_arg_is_cell_ref(quoted.code)
    two = PyFormula.parse("=PY($A$1; B1:B10; C1:C10)")
    assert two is not None
    assert parse_data_binding_text(format_data_binding_display(two.data_suffix)) == [
        "B1:B10",
        "C1:C10",
    ]


def test_normalize_array_and_no_equals():
    assert normalize_formula_string('{PYTHON("x")}') == '=PYTHON("x")'
    assert PyFormula.parse('{PYTHON("x")}') is not None


def test_build_new_formula_empty():
    assert PyFormula.compose("", []) == '=PY("")'


def test_build_new_formula_escapes():
    assert '""' in PyFormula.compose('say "hi"', [])


def test_rebuild_preserves_data_suffix_from_parts():
    parts = PyFormula.parse('=PYTHON("x"; A1:B2; C3)')
    assert parts is not None
    rebuilt = parts.render("y = 1")
    assert "A1:B2" in rebuilt
    assert "C3" in rebuilt
    reparsed = PyFormula.parse(rebuilt)
    assert reparsed is not None
    assert reparsed.code == "y = 1"


def test_format_data_binding_display():
    assert format_data_binding_display(")") == ""
    assert format_data_binding_display("; A1:B10)") == "A1:B10"
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


def test_parse_data_binding_text_quoted_sheet_names():
    from unittest.mock import patch
    import plugin.calc.python.formula_edit as fe

    assert parse_data_binding_text("'My,Sheet'.A1, B2") == ["'My,Sheet'.A1", "B2"]
    assert parse_data_binding_text("'My;Sheet'!A1") == ["'My;Sheet'!A1"]
    assert parse_data_binding_text("'O''Brien'.A1; B2") == ["'O''Brien'.A1", "B2"]
    with patch.object(fe, "_deal_data_args_ok", return_value=True), patch.object(
        fe, "_deal_range_addr_ok", return_value=True
    ):
        built = PyFormula.compose("x", ["'My,Sheet'.A1", "B2"])
    parsed = PyFormula.parse(built)
    assert parsed is not None
    assert parse_data_binding_text(format_data_binding_display(parsed.data_suffix)) == ["'My,Sheet'.A1", "B2"]


def test_build_data_suffix():
    assert build_data_suffix([]) == ")"
    assert build_data_suffix(["A1:B10"]) == ";A1:B10)"
    assert build_data_suffix(["A1:B10", "C1:C5"]) == ";A1:B10;C1:C5)"


def test_format_py_and_excel_data_range_real_sheets():
    """Product ranges format sheet-qualified cell and range references correctly."""
    assert format_py_data_range("Sheet.A1") == "Sheet.A1"
    assert format_py_data_range("'My Sheet'.A1") == "'My Sheet'.A1"
    assert format_py_data_range("My Sheet.A1") == "'My Sheet'.A1"
    assert format_py_data_range("Sheet!A1") == "Sheet.A1"
    assert format_py_data_range("O'Brien.A1") == "'O''Brien'.A1"
    assert format_py_data_range("'O''Brien'.A1") == "'O''Brien'.A1"
    assert format_excel_data_range("Sheet!A1") == "Sheet!A1"
    assert format_excel_data_range("Sheet.A1") == "Sheet!A1"
    assert format_excel_data_range("'My Sheet'.A1") == "'My Sheet'!A1"
    assert format_excel_data_range("My Sheet.A1") == "'My Sheet'!A1"
    assert format_excel_data_range("O'Brien.A1") == "'O''Brien'!A1"
    assert format_excel_data_range("'O''Brien'.A1") == "'O''Brien'!A1"
    assert format_py_data_range("'My.Sheet'.A1:B2") == "'My.Sheet'.A1:B2"
    assert format_py_data_range("'My.Sheet'!A1:B2") == "'My.Sheet'.A1:B2"
    assert format_excel_data_range("'My.Sheet'.A1:B2") == "'My.Sheet'!A1:B2"
    assert format_excel_data_range("'My.Sheet'!A1:B2") == "'My.Sheet'!A1:B2"
    assert format_py_data_range("'My!Sheet'.A1") == "'My!Sheet'.A1"
    assert format_excel_data_range("'My!Sheet'!A1") == "'My!Sheet'!A1"
    assert format_py_data_range("'My!Sheet'!A1") == "'My!Sheet'.A1"
    assert format_py_data_range("'My.Sheet' . A1") == "'My.Sheet'.A1"
    assert format_py_data_range("A1.B5") == "A1.B5"
    assert format_excel_data_range("A1.B5") == "A1.B5"
    assert format_py_data_range("$Sheet1.$A$1") == "$Sheet1.$A$1"
    assert format_excel_data_range("$Sheet1.$A$1") == "$Sheet1!$A$1"
    assert format_excel_data_range("Sheet1.A1") == "Sheet1!A1"
    assert format_excel_data_range("Sheet1.A1:B10") == "Sheet1!A1:B10"
    assert format_excel_data_range("Data2.A1") == "Data2!A1"
    assert format_excel_data_range("Q1.A1") == "Q1.A1"
    assert format_py_data_range("Sheet1.A1") == "Sheet1.A1"
    assert format_excel_data_range("!A1") == "!A1"
    assert format_py_data_range("!A1") == "''.A1"
    assert build_data_suffix(["Sheet.A1"]) == ";Sheet.A1)"
    assert build_data_suffix(["'My Sheet'.A1"]) == ";'My Sheet'.A1)"
    assert build_data_suffix(["Sheet!A1"], separator=",") == ",Sheet!A1)"
    assert build_data_suffix(["My Sheet.A1"], separator=",") == ",'My Sheet'!A1)"


def test_rebuild_python_formula_with_data():
    formula = PyFormula.compose("np.sum(data)", ["A1:A10"])
    assert formula == '=PY("np.sum(data)";A1:A10)'
    reparsed = PyFormula.parse(formula)
    assert reparsed is not None
    assert reparsed.code == "np.sum(data)"


def test_parse_py_alias():
    parts = PyFormula.parse('=PY("result = 1"; A1:B10)')
    assert parts is not None
    assert parts.code == "result = 1"
    assert parts.prefix.upper().startswith("=PY(")
    assert "A1:B10" in parts.data_suffix


def test_format_data_binding_text_round_trip():
    args = ["A1:B10", "C1:C5"]
    text = format_data_binding_text(args)
    assert parse_data_binding_text(text) == args


def test_calc_escape_preserves_hand_written_code():
    """escape_code_for_formula quote-escapes only; hand-written code is not rewritten."""
    code = "x = float(1)"
    assert escape_code_for_formula(code) == code
    calc = PyFormula.compose(code, [])
    xlsx = PyFormula.compose(code, [], separator=",")
    assert "float(1)" in calc
    assert "float(1)" in xlsx
    assert xlsx.startswith('=PY("')


def test_normalize_curly_quotes_around_code_string():
    """Smart quotes around the PY code arg become ASCII before parse."""
    formula = '=PY(\u201cresult = 1\u201d)'
    normalized = normalize_formula_string(formula)
    assert '"' in normalized
    assert "\u201c" not in normalized and "\u201d" not in normalized
    parts = PyFormula.parse(normalized)
    assert parts is not None
    assert parts.code == "result = 1"


def test_normalize_keeps_curly_quotes_in_code_payload():
    formula = '=PY("print(\u201chi\u201d)")'
    assert normalize_formula_string(formula) == formula
    parts = PyFormula.parse(formula)
    assert parts is not None and parts.code == 'print(\u201chi\u201d)'
    parts2 = PyFormula.parse('=PY("x = \u2018a\u2019")')
    assert parts2 is not None and parts2.code == "x = \u2018a\u2019"
    parts3 = PyFormula.parse('=PY(\u201cx\u201d; A1)')
    assert parts3 is not None and parts3.code == "x" and "A1" in parts3.data_suffix


def test_normalize_and_parse_bare_py_token():
    """Cluster A: CrossHair check raised PatternError on normalize/parse of ``'PY'``.

    Relib re-parses the PY|PYTHON head regex; a startswith scan must not throw.
    """
    assert normalize_formula_string("PY") == "PY"
    assert PyFormula.parse("PY") is None
    assert normalize_formula_string("PY(") == "=PY("
    assert PyFormula.parse("py(") is None  # still not a complete call
    assert PyFormula.parse('=py("x")') is not None
    assert PyFormula.parse('PYTHON("x")') is not None


def test_sanitize_dtype_float_with_control_chars():
    """Cluster B: escape_code_for_formula and rebuild preserve ``dtype=float`` control characters."""
    nul = "dtype=float\x00"
    soh = ".dtype=float\x01"
    assert escape_code_for_formula(nul) == nul
    assert escape_code_for_formula(soh) == soh
    parts = PyFormula.parse('=PY("x")')
    assert parts is not None
    rebuilt = parts.render(soh)
    assert soh in rebuilt


def test_monaco_save_round_trip_preserves_user_code():
    """Hand-written float("3.5"), int(-3.7), ax.text, np.float survive Monaco save roundtrip."""
    from plugin.calc.python.formula_edit import build_editor_formula_save

    code = 'x = float("3.5")\ny = int(-3.7)\nax.text(1, 2, "hi")\nnp.float64(1)'
    parts = PyFormula.parse(f'=PY("{escape_code_for_formula(code)}")')
    assert parts is not None
    saved = build_editor_formula_save(parsed_parts=parts, new_code=code)
    assert isinstance(saved, str)
    parsed_after = PyFormula.parse(saved)
    assert parsed_after is not None
    assert parsed_after.code == code


def test_assemble_python_formula():
    assert assemble_python_formula("=PY(", "x = 1", ")", quoted=True) == '=PY("x = 1")'
    assert assemble_python_formula("=PY(", "A1", ";B1)", quoted=False) == "=PY(A1;B1)"


def test_dollar_sign_preserved_in_data_args():
    """$ is preserved in emitted data range tokens on rebuild and Monaco save."""
    from plugin.calc.python.formula_edit import build_editor_formula_save

    assert format_py_data_range("$A$1:$B$5") == "$A$1:$B$5"
    assert format_excel_data_range("$A$1:$B$5") == "$A$1:$B$5"
    assert format_py_data_range("Sheet1.$A$1:$B$5") == "Sheet1.$A$1:$B$5"
    assert format_py_data_range("'My Sheet'.$A$1:$B$5") == "'My Sheet'.$A$1:$B$5"

    rebuilt = PyFormula.compose("x = 1", ["$A$1:$B$5"])
    assert rebuilt == '=PY("x = 1";$A$1:$B$5)'

    parts = PyFormula.parse('=PY("x = 1";$A$1:$B$5)')
    assert parts is not None
    saved = build_editor_formula_save(
        parsed_parts=parts,
        new_code="x = 2",
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
        parts = PyFormula.parse(orig)
        assert parts is not None
        rebuilt = parts.render("x = 2")
        assert rebuilt.startswith(f'{parts.prefix}"x = 2"')
        rebuilt_data = parts.render("x = 2", data_args=["A1"])
        assert rebuilt_data.startswith(f'{parts.prefix}"x = 2"')


def test_long_multi_range_suffix_parses_under_deal():
    """Data suffix parsing must accept data suffixes longer than DEAL_MAX_TOKEN (64)."""
    ranges = [f"Sheet{i}!$A${i}:$B${i+1}" for i in range(10)]
    suffix = "; " + "; ".join(ranges) + ")"
    formula = f'=PY("x = 1"{suffix}'
    assert len(suffix) > 64
    parts = PyFormula.parse(formula)
    assert parts is not None
    assert parts.code == "x = 1"
    assert parts.data_suffix == suffix


def test_open_end_and_parse_pins():
    assert PyFormula.open_end("=PY(", require_equals=True) == 4
    assert PyFormula.open_end("PY(", require_equals=False) == 3
    assert PyFormula.open_end("PY(", require_equals=True) is None

    assert PyFormula.parse('=PY("x"+1)') is None
    assert PyFormula.parse('=PY("x"; A1)') is not None


_CROSSHAIR_ERROR_RE = re.compile(r": error:")
# Rewrite wrappers (sanitize/escape/rebuild) are ``# crosshair: off`` — check-all
# drops those FQNs. Data-binding display/parse are also off.
# Keep parse/normalize: nested ensures are inverse_ensure-skipped. Range
# formatters stay on (closed A1/sheet alphabet, DEAL_MAX_TOKEN length).
_CROSSHAIR_TARGETS = (
    "plugin.calc.python.formula_edit._parse_quoted_string",
    "plugin.calc.python.formula_edit.normalize_formula_string",
    "plugin.calc.python.formula_edit.PyFormula.parse",
    "plugin.calc.spreadsheet_import.preprocess.normalize_lo_formula_for_parse",
)

# Avoid Hypothesis inventing NULs / unpaired surrogates that confuse quote lexers.
_CODE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=127, blacklist_characters="\x00"),
    max_size=40,
)
# Data-binding helpers are ascii_bounded (A1 / range tokens, including ``\x1c``).
_ASCII_BINDING = st.text(
    alphabet=st.characters(min_codepoint=0, max_codepoint=127),
    max_size=30,
)


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


@pytest.mark.verification
@given(code=_CODE_TEXT)
@settings(max_examples=vhs_max_examples(60, 600), deadline=None)
def test_hypothesis_escape_embed_parse_round_trip(code: str) -> None:
    """escape → embed in =PY("…") → parse recovers code."""
    escaped = escape_code_for_formula(code)
    formula = f'=PY("{escaped}")'
    parts = PyFormula.parse(formula)
    assert parts is not None
    assert parts.code == code
    assert parts.data_suffix == ")"


@pytest.mark.verification
@given(code=_CODE_TEXT)
@settings(max_examples=vhs_max_examples(50, 500), deadline=None)
def test_hypothesis_rebuild_parse_round_trip(code: str) -> None:
    """rebuild(parse(=PY(\"…\"))) preserves sanitized code (Phase 8 #1)."""
    escaped = escape_code_for_formula(code)
    formula = f'=PY("{escaped}")'
    parts = PyFormula.parse(formula)
    assert parts is not None
    rebuilt = parts.render(parts.code)
    again = PyFormula.parse(rebuilt)
    assert again is not None
    assert again.code == parts.code
    assert again.data_suffix == parts.data_suffix
    assert rebuilt.startswith('=PY("')


@pytest.mark.verification
@given(formula=_CODE_TEXT)
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_normalize_idempotent_after_first(formula: str) -> None:
    once = normalize_formula_string(formula)
    assert normalize_formula_string(once) == once
    assert "\u201c" not in once and "\u201d" not in once


@pytest.mark.verification
@given(inner=_CODE_TEXT.filter(lambda s: not s.startswith('"')))
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_unquoted_code_never_starts_with_quote(inner: str) -> None:
    result = _parse_unquoted_code_arg(inner)
    assert result is None or not result.startswith('"')


@pytest.mark.verification
@given(body=_CODE_TEXT)
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_quoted_string_bounds(body: str) -> None:
    # Wrap as a Calc string; doubled quotes inside body via escape.
    escaped = body.replace('"', '""')
    s = f'"{escaped}"'
    parsed = _parse_quoted_string(s, 0)
    assert parsed is not None
    code, end = parsed
    assert code == body
    assert 0 < end <= len(s)


@pytest.mark.verification
def test_parse_quoted_string_rejects_negative_start() -> None:
    """CrossHair found IndexError on start=-1; pre + body guard now reject negatives.

    With deal installed (dev venv), pre raises; under LibreOffice deal_shim the body
    returns None. Either way we must not IndexError.
    """
    expect_pre_or_body(lambda: _parse_quoted_string('""', -1), body_result=None)
    assert _parse_quoted_string('"x"', 0) == ("x", 3)
    if deal_pre_present(_parse_quoted_string):
        with pytest.raises(deal.PreContractError):
            _parse_quoted_string('"x"', DEAL_MAX_SOURCE + 1)


@pytest.mark.verification
def test_normalize_lo_preserves_semicolon_inside_quotes() -> None:
    raw = '=SUM("a;b"; C1)'
    out = normalize_lo_formula_for_parse(raw)
    assert '"a;b"' in out
    assert out.count(";") == 1  # only the one inside quotes
    assert ", C1)" in out or ",C1)" in out.replace(" ", "")


@pytest.mark.verification
def test_normalize_lo_curly_and_semicolon() -> None:
    formula = '=PY(\u201cx=1\u201d; A1)'
    out = normalize_lo_formula_for_parse(formula)
    assert "\u201c" not in out and "\u201d" not in out
    assert "; A1)" not in out
    assert ", A1)" in out or ",A1)" in out.replace(" ", "")


@pytest.mark.verification
def test_formula_edit_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(PyFormula.compose):
        pytest.skip("@deal.pre stripped in release bundle")
    too_long = "x" * (DEAL_MAX_SOURCE + 1)
    # rebuild_* / sanitize / escape allow DEAL_MAX_SOURCE+256 slack (dtype=float growth).
    rebuild_too_long = "x" * (DEAL_MAX_SOURCE + 256 + 1)
    with pytest.raises(deal.PreContractError):
        PyFormula.compose(rebuild_too_long, [])
    with pytest.raises(deal.PreContractError):
        escape_code_for_formula(rebuild_too_long)
    with pytest.raises(deal.PreContractError):
        PyFormula("=PY(", "x", data_suffix=")").render(rebuild_too_long)
    with pytest.raises(deal.PreContractError):
        format_data_binding_display(too_long)
    with pytest.raises(deal.PreContractError):
        normalize_formula_string(too_long)
    with pytest.raises(deal.PreContractError):
        PyFormula.parse(too_long)
    with pytest.raises(deal.PreContractError):
        normalize_lo_formula_for_parse(too_long)
    with pytest.raises(deal.PreContractError):
        parse_data_binding_text(too_long)
    with pytest.raises(deal.PreContractError):
        format_data_binding_display("A1\u00a0")
    with pytest.raises(deal.PreContractError):
        parse_data_binding_text("A1\u00a0")
    range_too_long = "A" * (DEAL_MAX_TOKEN + 1)
    with pytest.raises(deal.PreContractError):
        format_py_data_range(range_too_long)
    with pytest.raises(deal.PreContractError):
        format_excel_data_range(range_too_long)
    with pytest.raises(deal.PreContractError):
        format_py_data_range("A1\x00")
    with pytest.raises(deal.PreContractError):
        format_excel_data_range("A1\x00")
    too_many = ["A1"] * (DEAL_MAX_SHAPE_DIM + 1)
    with pytest.raises(deal.PreContractError):
        format_data_binding_text(too_many)
    with pytest.raises(deal.PreContractError):
        build_data_suffix(too_many)


@pytest.mark.verification
@given(suffix=_ASCII_BINDING)
@settings(max_examples=vhs_max_examples(50, 500), deadline=None)
def test_hypothesis_format_data_binding_display_invariants(suffix: str) -> None:
    res = format_data_binding_display(suffix)
    assert isinstance(res, str)
    if res:
        assert not res.startswith(";") and not res.startswith(",") and not res.endswith(")")


# CrossHair marked slow (excluded from default make test).
@pytest.mark.verification
@pytest.mark.slow
@pytest.mark.parametrize("target", _CROSSHAIR_TARGETS)
def test_crosshair_formula_edit_fqn_if_available(target: str) -> None:
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")
    from scripts.crosshair_stream import run_crosshair

    buf = io.StringIO()
    exit_code, _stats = run_crosshair(
        "check",
        ["-v", "--report_all", target],
        mode="check",
        raw=True,
        quiet=False,
        out=buf,
        timeout_sec=300,
    )
    combined = buf.getvalue().strip()
    print(f"CrossHair output ({target}):\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)
    if exit_code == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
