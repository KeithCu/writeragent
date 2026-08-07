# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair verification for payload_codec policy helpers."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.scripting.payload_codec import (
    cell_count,
    host_pack_split_grid,
    is_numeric_coercible,
    is_numeric_grid,
    wire_cell_count,
)

_CROSSHAIR_ERROR_RE = re.compile(r": error:")
_CROSSHAIR_TARGETS = (
    "plugin.scripting.payload_codec.is_numeric_coercible",
    "plugin.scripting.payload_codec.is_numeric_grid",
    "plugin.scripting.payload_codec.cell_count",
)
# wire_cell_count: deal+Hypothesis only (# crosshair: off — envelope Literal/proxy crashes)

_CELL = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-10_000, max_value=10_000),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(max_size=12),
)


def _find_crosshair() -> str | None:
    crosshair_path = shutil.which("crosshair")
    if crosshair_path:
        return crosshair_path
    venv_bin_ch = Path(".venv/bin/crosshair")
    if venv_bin_ch.exists():
        return str(venv_bin_ch)
    return None


@given(s=st.text(min_size=1).filter(lambda t: bool(t.strip())))
@settings(max_examples=80)
def test_hypothesis_nonempty_strings_never_coercible(s: str) -> None:
    assert is_numeric_coercible(s) is False


@given(ws=st.from_regex(r"[ \t\n\r]*", fullmatch=True))
@settings(max_examples=40)
def test_hypothesis_whitespace_strings_coercible(ws: str) -> None:
    assert is_numeric_coercible(ws) is True


@given(cells=st.lists(_CELL, max_size=8))
@settings(max_examples=50)
def test_hypothesis_numeric_grid_matches_cellwise_1d(cells: list) -> None:
    assert is_numeric_grid(cells) is all(is_numeric_coercible(c) for c in cells)


@given(rows=st.lists(st.lists(_CELL, max_size=5), min_size=1, max_size=5))
@settings(max_examples=40)
def test_hypothesis_numeric_grid_matches_cellwise_2d(rows: list[list]) -> None:
    assert is_numeric_grid(rows) is all(is_numeric_coercible(c) for row in rows for c in row)


@given(
    dims=st.lists(st.integers(min_value=0, max_value=20), min_size=0, max_size=4).map(tuple),
)
@settings(max_examples=50)
def test_hypothesis_cell_count_product(dims: tuple[int, ...]) -> None:
    n = cell_count(dims)
    assert n >= 0
    if not dims:
        assert n == 1
    else:
        expected = 1
        for d in dims:
            expected *= d
        assert n == expected


@given(
    rows=st.lists(st.lists(st.integers(), max_size=6), min_size=1, max_size=6),
)
@settings(max_examples=40)
def test_hypothesis_wire_cell_count_nested_list(rows: list[list[int]]) -> None:
    assert wire_cell_count(rows) == sum(len(row) for row in rows)


def test_zip_code_string_not_coercible() -> None:
    assert is_numeric_coercible("02138") is False
    assert is_numeric_grid([[1.0, "02138"], [2.0, None]]) is False
    assert is_numeric_grid([[1.0, 2.0], [3.0, None]]) is True


def test_wire_cell_count_split_grid_and_none() -> None:
    assert wire_cell_count(None) == 0
    assert wire_cell_count(42) == 1
    assert wire_cell_count([]) == 0
    wire = host_pack_split_grid([[1, 2], [3, 4]])
    assert wire_cell_count(wire) == 4


def test_empty_grid_is_numeric() -> None:
    assert is_numeric_grid([]) is True


@pytest.mark.slow
@pytest.mark.parametrize("target", _CROSSHAIR_TARGETS)
def test_crosshair_payload_codec_policy_fqn_if_available(target: str) -> None:
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
