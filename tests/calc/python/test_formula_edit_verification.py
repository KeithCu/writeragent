# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair verification for formula_edit (+ preprocess)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import deal
from plugin.calc.python.formula_edit import (
    _find_matching_paren,
    _parse_quoted_string,
    _parse_unquoted_code_arg,
    escape_code_for_formula,
    format_data_binding_display,
    format_data_binding_text,
    normalize_formula_string,
    parse_data_binding_text,
    parse_python_formula,
    rebuild_python_formula,
    rebuild_python_formula_with_data,
    sanitize_inline_py_code,
)
from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse
from plugin.framework.deal_shim import DEAL_MAX_SOURCE
from tests.strip_bundle import deal_pre_present, expect_pre_or_body
from tests.vhs_budget import vhs_max_examples

_CROSSHAIR_ERROR_RE = re.compile(r": error:")
_CROSSHAIR_TARGETS = (
    "plugin.calc.python.formula_edit._parse_quoted_string",
    "plugin.calc.python.formula_edit.parse_python_formula",
    "plugin.calc.python.formula_edit.escape_code_for_formula",
    "plugin.calc.spreadsheet_import.preprocess.normalize_lo_formula_for_parse",
)

# Avoid Hypothesis inventing NULs / unpaired surrogates that confuse quote lexers.
_CODE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), max_codepoint=127, blacklist_characters="\x00"),
    max_size=40,
)


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


@given(code=_CODE_TEXT)
@settings(max_examples=vhs_max_examples(60, 600), deadline=None)
def test_hypothesis_escape_embed_parse_round_trip(code: str) -> None:
    """escape → embed in =PY("…") → parse recovers post-sanitize code."""
    escaped = escape_code_for_formula(code)
    formula = f'=PY("{escaped}")'
    parts = parse_python_formula(formula)
    assert parts is not None
    assert parts.code == sanitize_inline_py_code(code)
    assert parts.data_suffix == ")"


@given(code=_CODE_TEXT)
@settings(max_examples=vhs_max_examples(50, 500), deadline=None)
def test_hypothesis_rebuild_parse_round_trip(code: str) -> None:
    """rebuild(parse(=PY(\"…\"))) preserves sanitized code (Phase 8 #1)."""
    escaped = escape_code_for_formula(code)
    formula = f'=PY("{escaped}")'
    parts = parse_python_formula(formula)
    assert parts is not None
    rebuilt = rebuild_python_formula(parts, parts.code)
    again = parse_python_formula(rebuilt)
    assert again is not None
    assert again.code == parts.code
    assert again.data_suffix == parts.data_suffix
    assert rebuilt.startswith('=PY("')


@given(formula=_CODE_TEXT)
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_normalize_idempotent_after_first(formula: str) -> None:
    once = normalize_formula_string(formula)
    assert normalize_formula_string(once) == once
    assert "\u201c" not in once and "\u201d" not in once


@given(inner=_CODE_TEXT.filter(lambda s: not s.startswith('"')))
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_unquoted_code_never_starts_with_quote(inner: str) -> None:
    result = _parse_unquoted_code_arg(inner)
    assert result is None or not result.startswith('"')


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


def test_find_matching_paren_rejects_negative_open_idx() -> None:
    """CrossHair found IndexError on open_idx=-1 with empty s; pre + body guard reject it.

    With deal installed (dev venv), pre raises; under LibreOffice deal_shim the body
    returns -1. Either way we must not IndexError.
    """
    expect_pre_or_body(lambda: _find_matching_paren("", -1), body_result=-1)
    assert _find_matching_paren("(a)", 0) == 2


def test_parse_rebuild_preserves_code_and_data_suffix() -> None:
    original = '=PYTHON("result = 1"; A1:B10)'
    parts = parse_python_formula(original)
    assert parts is not None
    rebuilt = rebuild_python_formula(parts, parts.code)
    again = parse_python_formula(rebuilt)
    assert again is not None
    assert again.code == parts.code
    assert again.data_suffix == parts.data_suffix
    assert rebuilt.startswith('=PY("')


def test_normalize_lo_preserves_semicolon_inside_quotes() -> None:
    raw = '=SUM("a;b"; C1)'
    out = normalize_lo_formula_for_parse(raw)
    assert '"a;b"' in out
    assert out.count(";") == 1  # only the one inside quotes
    assert ", C1)" in out or ",C1)" in out.replace(" ", "")


def test_normalize_lo_curly_and_semicolon() -> None:
    formula = '=PY(\u201cx=1\u201d; A1)'
    out = normalize_lo_formula_for_parse(formula)
    assert "\u201c" not in out and "\u201d" not in out
    assert "; A1)" not in out
    assert ", A1)" in out or ",A1)" in out.replace(" ", "")


def test_data_binding_format_and_parse() -> None:
    assert format_data_binding_display("; A1:B10)") == "A1:B10"
    assert format_data_binding_display(")") == ""
    assert parse_data_binding_text("A1, B1:C5") == ["A1", "B1:C5"]
    assert format_data_binding_text(["A1", "B1:C5"]) == "A1, B1:C5"


def test_formula_edit_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(rebuild_python_formula_with_data):
        pytest.skip("@deal.pre stripped in release bundle")
    too_long = "x" * (DEAL_MAX_SOURCE + 1)
    with pytest.raises(deal.PreContractError):
        rebuild_python_formula_with_data(too_long, [])
    with pytest.raises(deal.PreContractError):
        format_data_binding_display(too_long)
    with pytest.raises(deal.PreContractError):
        normalize_formula_string(too_long)
    with pytest.raises(deal.PreContractError):
        parse_python_formula(too_long)
    with pytest.raises(deal.PreContractError):
        normalize_lo_formula_for_parse(too_long)


@given(suffix=st.text(max_size=30))
@settings(max_examples=vhs_max_examples(50, 500), deadline=None)
def test_hypothesis_format_data_binding_display_invariants(suffix: str) -> None:
    res = format_data_binding_display(suffix)
    assert isinstance(res, str)
    if res:
        assert not res.startswith(";") and not res.startswith(",") and not res.endswith(")")



@pytest.mark.slow
@pytest.mark.parametrize("target", _CROSSHAIR_TARGETS)
def test_crosshair_formula_edit_fqn_if_available(target: str) -> None:
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")
    result = subprocess.run(
        [crosshair_path, "check", "-v", "--report_all", target],
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output ({target}):\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)
    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
