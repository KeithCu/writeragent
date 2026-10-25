# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for pure scripting helpers (import_policy, config_limits, calc_range)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.deal_shim import DEAL_MAX_ARGV, DEAL_MAX_SHAPE_DIM, DEAL_MAX_TOKEN

from plugin.scripting.import_policy import (
    PYTHON_VENV_SANDBOX_CONTEXT_PREFIX,
    venv_blocked_modules,
    format_venv_import_policy_for_prompt,
)
from plugin.scripting.config_limits import (
    python_exec_timeout_default,
    python_exec_timeout_min,
    python_exec_timeout_max,
    resolve_python_exec_timeout,
    _clamp_timeout,
)
from plugin.scripting.calc_range import (
    ensure_rectangular_2d,
    is_calc_range_payload,
    pack_calc_range_envelope,
    CalcRange,
)
from plugin.scripting.venv.coerce import _dedupe_column_names


def test_import_policy_contracts() -> None:
    blocked = venv_blocked_modules()
    assert isinstance(blocked, tuple)
    assert len(blocked) > 0
    assert "subprocess" in blocked

    prompt = format_venv_import_policy_for_prompt(compact=True)
    assert isinstance(prompt, str)
    assert prompt.startswith(PYTHON_VENV_SANDBOX_CONTEXT_PREFIX)


@given(val=st.one_of(
    st.none(), st.booleans(), st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=DEAL_MAX_TOKEN),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=3),
))
@settings(max_examples=100)
def test_resolve_python_exec_timeout_clamping(val) -> None:
    timeout = resolve_python_exec_timeout(val)
    assert isinstance(timeout, int)
    assert python_exec_timeout_min() <= timeout <= python_exec_timeout_max()


def test_clamp_timeout_overflow_clamps() -> None:
    """A timeout past DEAL_MAX_ARGV is user config. Clamp it; do not PreContract."""
    assert _clamp_timeout(DEAL_MAX_ARGV + 1) == python_exec_timeout_max()
    assert python_exec_timeout_min() <= _clamp_timeout(1) <= python_exec_timeout_max()


def test_resolve_python_exec_timeout_is_total() -> None:
    assert resolve_python_exec_timeout(True) == 1
    assert resolve_python_exec_timeout(False) == 1
    assert resolve_python_exec_timeout(None, configured=True) == 1
    assert resolve_python_exec_timeout(None, configured="20") == 20
    assert resolve_python_exec_timeout(None, configured=float("nan")) == python_exec_timeout_default()
    assert resolve_python_exec_timeout(None, configured=DEAL_MAX_ARGV + 1) == python_exec_timeout_max()
    assert resolve_python_exec_timeout(None, configured=33) == 33
    assert resolve_python_exec_timeout(None, configured=None) == python_exec_timeout_default()
    assert resolve_python_exec_timeout("100") == 100
    assert resolve_python_exec_timeout("bad") == python_exec_timeout_default()
    assert resolve_python_exec_timeout(str(DEAL_MAX_ARGV + 1)) == python_exec_timeout_max()


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
    deduped = _dedupe_column_names(names)
    assert isinstance(deduped, list)
    assert len(deduped) == len(names)
    assert len(set(deduped)) == len(deduped)


def test_dedupe_column_names_suffix_skips_later_raw_header() -> None:
    """A generated suffix must not collide with a header that is already that suffix."""
    assert _dedupe_column_names(["a", "a", "a_1"]) == ["a", "a_1", "a_1_1"]
    assert _dedupe_column_names(["a", "a", "a"]) == ["a", "a_1", "a_2"]


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


def test_pack_calc_range_envelope_ignores_non_callable_pack_inner() -> None:
    envelope = pack_calc_range_envelope([], address=None, pack_inner="")
    assert is_calc_range_payload(envelope) is True
    assert envelope["data"] == []
