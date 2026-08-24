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

import deal
from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, DEAL_MAX_SHAPE_RANK
from plugin.scripting.payload_codec import (
    PAYLOAD_CALC_RANGE,
    PAYLOAD_DATAFRAME,
    PAYLOAD_IMAGE,
    PAYLOAD_MULTI_DATA,
    PAYLOAD_SPLIT_GRID,
    cell_count,
    host_pack_split_grid,
    is_calc_range_payload,
    is_dataframe_payload,
    is_image_payload,
    is_multi_data,
    is_numeric_coercible,
    is_numeric_grid,
    is_split_grid,
    should_use_binary_envelope,
    wire_cell_count,
)
from tests.strip_bundle import deal_pre_present
from tests.vhs_budget import vhs_max_examples

_CROSSHAIR_ERROR_RE = re.compile(r": error:")
_CROSSHAIR_TARGETS = (
    "plugin.scripting.payload_codec.is_numeric_coercible",
    "plugin.scripting.payload_codec.is_numeric_grid",
    "plugin.scripting.payload_codec.cell_count",
    "plugin.scripting.payload_codec.is_split_grid",
    "plugin.scripting.payload_codec.is_multi_data",
    "plugin.scripting.payload_codec.is_image_payload",
    "plugin.scripting.payload_codec.is_dataframe_payload",
    "plugin.scripting.payload_codec.is_calc_range_payload",
)
# wire_cell_count: deal+Hypothesis only (# crosshair: off — envelope Literal/proxy crashes)

_DETECTORS = (
    is_split_grid,
    is_multi_data,
    is_image_payload,
    is_dataframe_payload,
    is_calc_range_payload,
)

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


@given(s=st.text(min_size=1, max_size=40).filter(lambda t: bool(t.strip())))
@settings(max_examples=vhs_max_examples(80, 800), deadline=None)
def test_hypothesis_nonempty_strings_never_coercible(s: str) -> None:
    assert is_numeric_coercible(s) is False


def test_zero_dim_shape_cell_count() -> None:
    # () represents a 0-dimensional scalar (1 element), matching cell_count @deal.ensure contract: len(shape) != 0 or result == 1
    assert cell_count(()) == 1
    assert cell_count((0,)) == 0
    assert cell_count((0, 5)) == 0
    assert cell_count((5, 0)) == 0
    assert cell_count((1, 1, 1, 1)) == 1


def test_cell_count_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(cell_count):
        pytest.skip("@deal.pre stripped in release bundle")
    with pytest.raises(deal.PreContractError):
        cell_count((DEAL_MAX_SHAPE_DIM + 1,))
    with pytest.raises(deal.PreContractError):
        cell_count(tuple([1] * (DEAL_MAX_SHAPE_RANK + 1)))
    with pytest.raises(deal.PreContractError):
        should_use_binary_envelope((1,), min_cells=DEAL_MAX_SHAPE_DIM + 1)
    with pytest.raises(deal.PreContractError):
        is_numeric_grid([0] * (DEAL_MAX_SHAPE_DIM + 1))
    with pytest.raises(deal.PreContractError):
        is_numeric_grid([[0] * (DEAL_MAX_SHAPE_DIM + 1)])
    assert is_numeric_grid([0] * DEAL_MAX_SHAPE_DIM) is True


def test_host_pack_split_grid_empty() -> None:
    packed = host_pack_split_grid([])
    assert isinstance(packed, dict)
    assert packed.get("__wa_payload__") == "split_grid"
    assert packed.get("shape") == [0]


@given(ws=st.from_regex(r"[ \t\n\r]*", fullmatch=True))
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_whitespace_strings_coercible(ws: str) -> None:
    assert is_numeric_coercible(ws) is True


@given(cells=st.lists(_CELL, max_size=8))
@settings(max_examples=vhs_max_examples(50, 500), deadline=None)
def test_hypothesis_numeric_grid_matches_cellwise_1d(cells: list) -> None:
    assert is_numeric_grid(cells) is all(is_numeric_coercible(c) for c in cells)


@given(rows=st.lists(st.lists(_CELL, max_size=5), min_size=1, max_size=5))
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
def test_hypothesis_numeric_grid_matches_cellwise_2d(rows: list[list]) -> None:
    assert is_numeric_grid(rows) is all(is_numeric_coercible(c) for row in rows for c in row)


@given(
    dims=st.lists(st.integers(min_value=0, max_value=20), min_size=0, max_size=4).map(tuple),
)
@settings(max_examples=vhs_max_examples(50, 500), deadline=None)
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
@settings(max_examples=vhs_max_examples(40, 400), deadline=None)
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


def test_envelope_detectors_minimal_valid() -> None:
    """Each wire family matches exactly one public detector."""
    split_env = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "shape": [0],
        "buffer": b"",
    }
    multi_env = {"__wa_payload__": PAYLOAD_MULTI_DATA, "items": []}
    image_env = {"__wa_payload__": PAYLOAD_IMAGE, "data": b"\x89PNG", "format": "png"}
    df_env = {"__wa_payload__": PAYLOAD_DATAFRAME, "columns": ["a"], "data": [[1]]}
    cr_env = {"__wa_payload__": PAYLOAD_CALC_RANGE, "shape": [1, 1], "data": [[1]]}

    cases = (
        (split_env, is_split_grid),
        (multi_env, is_multi_data),
        (image_env, is_image_payload),
        (df_env, is_dataframe_payload),
        (cr_env, is_calc_range_payload),
    )
    for env, expected in cases:
        for det in _DETECTORS:
            assert det(env) is (det is expected)


def test_envelope_detectors_reject_malformed() -> None:
    assert is_split_grid({"__wa_payload__": PAYLOAD_SPLIT_GRID, "shape": [1]}) is False
    assert is_split_grid({"__wa_payload__": PAYLOAD_SPLIT_GRID, "shape": [1], "buffer": "x"}) is False
    assert is_multi_data({"__wa_payload__": PAYLOAD_MULTI_DATA}) is False
    assert is_multi_data({"__wa_payload__": PAYLOAD_MULTI_DATA, "items": "nope"}) is False
    assert is_image_payload({"__wa_payload__": PAYLOAD_IMAGE, "data": b"x"}) is False
    assert is_dataframe_payload({"__wa_payload__": PAYLOAD_DATAFRAME, "columns": [1], "data": []}) is False
    assert is_calc_range_payload({"__wa_payload__": PAYLOAD_CALC_RANGE, "shape": [1], "data": []}) is False
    assert is_calc_range_payload({"__wa_payload__": PAYLOAD_CALC_RANGE, "shape": [1, 1]}) is False


@given(
    value=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=20),
        st.lists(st.integers(), max_size=4),
        st.dictionaries(st.text(max_size=8), st.integers(), max_size=4),
        st.fixed_dictionaries({"__wa_payload__": st.sampled_from(["", "nope", "grid", "img"])}),
        st.fixed_dictionaries(
            {
                "__wa_payload__": st.just(PAYLOAD_SPLIT_GRID),
                "shape": st.lists(st.integers(min_value=-2, max_value=3), max_size=3),
            }
        ),
    )
)
@settings(max_examples=vhs_max_examples(60, 400), deadline=None)
def test_hypothesis_garbage_never_true_detector(value: object) -> None:
    """Random non-envelopes must not satisfy any payload detector."""
    assert not any(det(value) for det in _DETECTORS)


def test_host_pack_split_grid_is_split_grid() -> None:
    packed = host_pack_split_grid([[1.0, 2.0], [3.0, 4.0]])
    assert is_split_grid(packed) is True
    assert is_multi_data(packed) is False


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
