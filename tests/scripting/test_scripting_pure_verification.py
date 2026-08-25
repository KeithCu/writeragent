# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for pure scripting helpers (import_policy, config_limits, calc_range)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import deal
from plugin.framework.deal_shim import DEAL_MAX_ARGV, DEAL_MAX_SHAPE_DIM, DEAL_MAX_TOKEN
from tests.strip_bundle import deal_pre_present

from plugin.scripting.import_policy import (
    PYTHON_VENV_SANDBOX_CONTEXT_PREFIX,
    venv_authorized_top_level_modules,
    venv_blocked_modules,
    inprocess_authorized_modules,
    format_venv_import_policy_for_prompt,
)
from plugin.scripting.config_limits import (
    python_exec_timeout_min,
    python_exec_timeout_max,
    resolve_python_exec_timeout,
    _clamp_timeout,
)
from plugin.scripting.calc_range import (
    column_vector_as_2d,
    ensure_rectangular_2d,
    is_calc_range_payload,
    pack_calc_range_envelope,
    _dedupe_column_names,
    CalcRange,
)


@given(vals=st.lists(st.integers(), max_size=DEAL_MAX_SHAPE_DIM))
def test_column_vector_as_2d_contracts(vals: list[int]) -> None:
    res = column_vector_as_2d(vals)
    assert isinstance(res, list)
    assert len(res) == len(vals)
    assert all(isinstance(row, list) and len(row) == 1 and row[0] == val for row, val in zip(res, vals))


def test_column_vector_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(column_vector_as_2d):
        pytest.skip("@deal.pre stripped in release bundle")
    with pytest.raises(deal.PreContractError):
        column_vector_as_2d([0] * (DEAL_MAX_SHAPE_DIM + 1))


def test_import_policy_contracts() -> None:
    auth = venv_authorized_top_level_modules()
    assert isinstance(auth, tuple)
    assert len(auth) > 0
    assert "numpy" in auth or "math" in auth

    blocked = venv_blocked_modules()
    assert isinstance(blocked, tuple)
    assert len(blocked) > 0
    assert "subprocess" in blocked or "os" in blocked

    inproc = inprocess_authorized_modules()
    assert isinstance(inproc, tuple)
    assert len(inproc) > 0

    prompt = format_venv_import_policy_for_prompt(compact=True)
    assert isinstance(prompt, str)
    assert prompt.startswith(PYTHON_VENV_SANDBOX_CONTEXT_PREFIX)


@given(
    val=st.one_of(
        st.integers(min_value=-DEAL_MAX_ARGV, max_value=DEAL_MAX_ARGV),
        st.floats(min_value=-float(DEAL_MAX_ARGV), max_value=float(DEAL_MAX_ARGV), allow_nan=False, allow_infinity=False),
        st.text(max_size=DEAL_MAX_TOKEN),
        st.none(),
    )
)
@settings(max_examples=100)
def test_resolve_python_exec_timeout_clamping(val: float | int | str | None) -> None:
    timeout = resolve_python_exec_timeout(val)
    assert isinstance(timeout, int)
    assert python_exec_timeout_min() <= timeout <= python_exec_timeout_max()


def test_clamp_timeout_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(_clamp_timeout):
        pytest.skip("@deal.pre stripped in release bundle")
    with pytest.raises(deal.PreContractError):
        _clamp_timeout(DEAL_MAX_ARGV + 1)
    assert python_exec_timeout_min() <= _clamp_timeout(1) <= python_exec_timeout_max()


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
