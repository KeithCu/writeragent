# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for pure scripting helpers (import_policy, config_limits, calc_range)."""

from __future__ import annotations

from unittest.mock import patch
from hypothesis import given
from hypothesis import strategies as st

from plugin.framework.deal_shim import DEAL_MAX_ARGV, DEAL_MAX_SHAPE_DIM, DEAL_MAX_TOKEN

from plugin.scripting.config_limits import (
    _bounds,
    _clamp,
    python_exec_timeout,
    python_exec_timeout_default,
)
from plugin.scripting.calc_range import (
    ensure_rectangular_2d,
    is_calc_range_payload,
    pack_calc_range_envelope,
    CalcRange,
)
from plugin.scripting.venv.coerce import dedupe_column_names


def test_clamp_timeout_overflow_clamps() -> None:
    """A timeout past DEAL_MAX_ARGV is user config. Clamp it; do not PreContract."""
    t_min, t_max = _bounds("python_exec_timeout")
    assert _clamp("python_exec_timeout", DEAL_MAX_ARGV + 1) == t_max
    assert t_min <= _clamp("python_exec_timeout", 1) <= t_max
    assert python_exec_timeout(DEAL_MAX_ARGV + 1) == python_exec_timeout(10**9)


def test_python_exec_timeout_is_total() -> None:
    default = python_exec_timeout_default()
    t_max = python_exec_timeout(10**9)
    for raw, expected in (
        ("20", 20),
        (float("nan"), default),
        (DEAL_MAX_ARGV + 1, t_max),
        (33, 33),
        (0, 1),
    ):
        with patch("plugin.framework.config.get_config", return_value=raw):
            assert python_exec_timeout() == expected


@given(grid=st.one_of(
    st.none(),
    st.integers(),
    st.text(),
    st.lists(st.integers()),
    st.lists(st.lists(st.integers())),
))
def test_ensure_rectangular_2d_invariants(grid) -> None:
    res = ensure_rectangular_2d(grid)
    assert isinstance(res, list)
    if res:
        first_len = len(res[0])
        for row in res:
            assert isinstance(row, list)
            assert len(row) == first_len


@given(names=st.lists(st.text(max_size=DEAL_MAX_TOKEN), max_size=DEAL_MAX_SHAPE_DIM))
def test_dedupe_column_names_uniqueness(names: list[str]) -> None:
    deduped = dedupe_column_names(names)
    assert isinstance(deduped, list)
    assert len(deduped) == len(names)
    assert len(set(deduped)) == len(deduped)


def test_dedupe_column_names_suffix_skips_later_raw_header() -> None:
    """A generated suffix must not collide with a header that is already that suffix."""
    assert dedupe_column_names(["a", "a", "a_1"]) == ["a", "a_1", "a_1_1"]
    assert dedupe_column_names(["a", "a", "a"]) == ["a", "a_1", "a_2"]


def test_ensure_rectangular_2d_string_row_is_one_cell() -> None:
    """A string row is a cell, not a sequence of characters."""
    assert ensure_rectangular_2d([["a", "b"], "cd"]) == [["a", "b"], ["cd", None]]
    assert ensure_rectangular_2d([["a", "b"], b"cd"]) == [["a", "b"], [b"cd", None]]
    assert ensure_rectangular_2d(["ab", "cd"]) == [["ab", "cd"]]


@given(raw_val=st.one_of(st.lists(st.integers()), st.lists(st.lists(st.integers()))))
def test_calc_range_packing_contracts(raw_val) -> None:
    envelope = pack_calc_range_envelope(raw_val, address="A1")
    assert is_calc_range_payload(envelope) is True

    cr = CalcRange(raw_val, address="A1")
    assert isinstance(cr.values, list)
    assert cr.nrows == len(cr.values)
    if cr.values:
        assert cr.ncols == len(cr.values[0])
    assert cr.shape == (cr.nrows, cr.ncols)
