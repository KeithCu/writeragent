# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / CrossHair / Hypothesis verification for calc address_utils.

CrossHair marked slow (excluded from default ``make test``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.calc.address_utils import (
    column_to_index,
    format_address,
    index_to_column,
    parse_address,
    parse_range_string,
)
from plugin.framework.deal_shim import DEAL_MAX_COL_INDEX, DEAL_MAX_ROW_INDEX
from tests.vhs_budget import vhs_max_examples

CROSSHAIR_MODULE = "plugin/calc/address_utils.py"
_CROSSHAIR_ERROR_RE = re.compile(r": error:")

# Bound column width so Hypothesis stays fast (Excel max is wider; invariant holds generally).
_col_letters = st.text(alphabet=st.characters(min_codepoint=ord("A"), max_codepoint=ord("Z")), min_size=1, max_size=3)
_col_index = st.integers(min_value=0, max_value=DEAL_MAX_COL_INDEX)
_row_index = st.integers(min_value=0, max_value=DEAL_MAX_ROW_INDEX)


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


def test_column_index_round_trip_named() -> None:
    for col in ("A", "Z", "AA", "AB", "ZZ", "ZZZ", "abc"):
        assert index_to_column(column_to_index(col)) == col.upper()
    assert column_to_index("ZZZ") == 18277
    assert index_to_column(18277) == "ZZZ"


@given(col=_col_letters)
@settings(max_examples=vhs_max_examples(100, 1000), deadline=None)
def test_hypothesis_column_letter_round_trip(col: str) -> None:
    assert index_to_column(column_to_index(col)) == col.upper()


@given(index=_col_index)
@settings(max_examples=vhs_max_examples(100, 1000), deadline=None)
def test_hypothesis_column_index_round_trip(index: int) -> None:
    assert column_to_index(index_to_column(index)) == index


@given(col=_col_index, row=_row_index)
@settings(max_examples=vhs_max_examples(80, 800), deadline=None)
def test_hypothesis_format_parse_round_trip(col: int, row: int) -> None:
    addr = format_address(col, row)
    assert parse_address(addr) == (col, row)


def test_format_address_row_overflow_pre_fails_closed() -> None:
    import deal
    from tests.strip_bundle import deal_pre_present

    if not deal_pre_present(format_address):
        pytest.skip("@deal.pre stripped in release bundle")
    with pytest.raises(deal.PreContractError):
        format_address(0, DEAL_MAX_ROW_INDEX + 1)
    assert format_address(0, DEAL_MAX_ROW_INDEX) == f"A{DEAL_MAX_ROW_INDEX + 1}"


def test_parse_address_raises_on_invalid() -> None:
    try:
        import deal
        pre_err = (ValueError, deal.PreContractError)
    except Exception:
        pre_err = (ValueError,)

    for ascii_invalid in ("A0", "A00", "Invalid"):
        with pytest.raises(ValueError):
            parse_address(ascii_invalid)

    for non_ascii_invalid in ("A🯰", "Ａ１", "A١"):
        with pytest.raises(pre_err):
            parse_address(non_ascii_invalid)

    for invalid_range in ("A1:Z", "A1:B0", "A0:B1"):
        with pytest.raises(ValueError):
            parse_range_string(invalid_range)


@given(s=st.text())
@settings(max_examples=vhs_max_examples(100, 1000), deadline=None)
def test_hypothesis_parse_address_row_non_negative_or_raises(s: str) -> None:
    try:
        col, row = parse_address(s)
        assert col >= 0
        assert row >= 0
    except Exception:
        pass


@pytest.mark.slow
def test_crosshair_address_utils_if_available() -> None:
    crosshair_path = _find_crosshair()
    if not crosshair_path:
        pytest.skip("CrossHair concolic execution engine is not installed.")

    result = subprocess.run(
        [crosshair_path, "check", "-v", "--report_all", CROSSHAIR_MODULE],
        capture_output=True,
        text=True,
        timeout=600,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    print(f"CrossHair output:\n{combined}")
    errors = [line for line in combined.splitlines() if _CROSSHAIR_ERROR_RE.search(line)]
    assert not errors, "CrossHair counterexamples found:\n" + "\n".join(errors)
    if result.returncode == 2:
        pytest.fail(f"CrossHair internal error (exit 2):\n{combined}")
