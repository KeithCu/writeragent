# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for pure scripting helpers (import_policy, config_limits, calc_range)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

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
    _clamp_timeout,
    resolve_python_exec_timeout,
)
from plugin.scripting.calc_range import (
    ensure_rectangular_2d,
    is_calc_range_payload,
    pack_calc_range_envelope,
    _dedupe_column_names,
    CalcRange,
)


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


@given(val=st.one_of(st.integers(), st.floats(), st.text(), st.none()))
@settings(max_examples=100)
def test_resolve_python_exec_timeout_clamping(val: float | int | str | None) -> None:
    timeout = resolve_python_exec_timeout(val)
    assert isinstance(timeout, int)
    assert python_exec_timeout_min() <= timeout <= python_exec_timeout_max()


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


@given(names=st.lists(st.text()))
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
