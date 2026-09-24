# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Comprehensive A/B serialization testing (split_grid vs nested list).

Every parity test compares force=\"always\" (split_grid wire) vs force=\"never\"
(nested list wire) and asserts the same final decoded semantics.

This file deliberately avoids any LibreOffice/UNO dependencies.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings, example, assume, HealthCheck

from plugin.scripting.payload_codec import (
    fast_flatten_grid_1d,
    fast_flatten_grid_2d,
    host_pack_data,
    is_split_grid,
)

def test_cython_active_if_available() -> None:
    """Verify that the Cython accelerator is loaded if we are in a 'native' build environment."""
    import os
    # We only expect this to pass if the user has run 'make native'
    # or if we are in a CI environment that builds native modules.
    if os.path.exists("plugin/contrib/vec_pack/pack.cpython-312-x86_64-linux-gnu.so"):
        assert fast_flatten_grid_2d is not None, "Cython 2D accelerator should be loaded"
        assert fast_flatten_grid_1d is not None, "Cython 1D accelerator should be loaded"
from plugin.scripting.venv_worker import PythonWorkerManager
from tests.scripting.payload_codec_test_support import MIXED_WITH_ZIP, NUMERIC_4X4
from tests.scripting.serialization_ab_support import (
    VENV_CODE_ECHO,
    VENV_CODE_SUM,
    AbGridCase,
    MULTI_RANGE_FIXTURES,
    VenvTransformCase,
    all_codec_ab_cases,
    assert_codec_split_vs_nosplit_parity,
    assert_identity_after_echo,
    assert_venv_always_never_parity,
    codec_child_materialization,
    expect_child_list_not_ndarray,
    flatten_semantic_cells,
    grid_cell_count,
    hypothesis_grid_ok,
    numeric_rectangular_grid,
    multi_range_grid,
    prepare_grid,
    rectangular_grid,
    run_venv_roundtrip,
    venv_expected_cases,
    venv_transform_cases,
    ab_hypothesis_max_examples,
    fancier_result_strategy,
)

_EX = ab_hypothesis_max_examples()


def _case_id(case: AbGridCase) -> str:
    return case.id


def _cases() -> list[AbGridCase]:
    return all_codec_ab_cases()


def _materialization_type_cases() -> list[AbGridCase]:
    """Multi-cell grids only — single-cell inputs unwrap to scalar in the child sandbox."""
    return [case for case in _cases() if grid_cell_count(prepare_grid(case)) > 1]


# =============================================================================
# Tier A — Codec decode (always vs never, no venv)
# =============================================================================


@pytest.mark.parametrize("case", _cases(), ids=_case_id)
def test_codec_child_and_host_decode_parity(case: AbGridCase) -> None:
    """split_grid vs nested list: child unpack and host unpack must match."""
    grid = prepare_grid(case)
    assert_codec_split_vs_nosplit_parity(grid, label=case.id)


@pytest.mark.parametrize("case", _cases(), ids=_case_id)
def test_split_wire_format(case: AbGridCase) -> None:
    """always → split_grid envelope; never → nested list only."""
    grid = prepare_grid(case)
    assert is_split_grid(host_pack_data(grid, force="always"))
    assert not is_split_grid(host_pack_data(grid, force="never"))


@pytest.mark.parametrize("case", _materialization_type_cases(), ids=_case_id)
def test_child_materialization_type(case: AbGridCase) -> None:
    """Under force=always: numeric-only → ndarray; any string → nested list."""
    pytest.importorskip("numpy")
    grid = prepare_grid(case)
    child = codec_child_materialization(grid, force="always")
    if expect_child_list_not_ndarray(grid if isinstance(grid, list) else [grid]):
        assert isinstance(child, list)
        assert not isinstance(child, np.ndarray)
    else:
        assert isinstance(child, np.ndarray)


# =============================================================================
# Tier B — Worker integration (always vs never)
# =============================================================================


@pytest.mark.parametrize("case", venv_transform_cases(), ids=lambda c: c.id)
def test_venv_transform_parity(case: VenvTransformCase) -> None:
    """Worker transforms must agree under force=always vs force=never."""
    assert_venv_always_never_parity(
        case.grid,
        case.code,
        use_subprocess=case.use_subprocess,
        label=case.id,
    )


@pytest.mark.parametrize("case", _cases(), ids=_case_id)
def test_venv_echo_parity(case: AbGridCase) -> None:
    """Venv echo: always vs never on full fixture corpus."""
    grid = prepare_grid(case)
    assert_venv_always_never_parity(grid, VENV_CODE_ECHO, label=case.id)


@pytest.mark.parametrize("case", venv_expected_cases(), ids=lambda c: c.id)
def test_venv_expected_value(case: VenvTransformCase) -> None:
    """Known expected values must match under both force=always and force=never."""
    for force in ("always", "never"):
        result = run_venv_roundtrip(case.grid, case.code, pack_force=force, grid_b=case.grid_b)
        if isinstance(case.expected, float):
            assert result == pytest.approx(case.expected), f"force={force}"
        else:
            assert result == case.expected, f"force={force}"


@pytest.mark.integration
def test_venv_subprocess_numeric_sum() -> None:
    """Full Pickle5 IPC: sum parity always vs never."""
    pytest.importorskip("numpy")
    PythonWorkerManager.shutdown_all()
    try:
        result, _ = assert_venv_always_never_parity(
            NUMERIC_4X4,
            VENV_CODE_SUM,
            use_subprocess=True,
            label="subprocess numeric sum",
        )
        assert result == pytest.approx(264.0)
    finally:
        PythonWorkerManager.shutdown_all()


@pytest.mark.integration
def test_venv_subprocess_mixed_echo() -> None:
    """Full Pickle5 IPC echo: always vs never on mixed zip grid."""
    pytest.importorskip("numpy")
    PythonWorkerManager.shutdown_all()
    try:
        assert_venv_always_never_parity(
            MIXED_WITH_ZIP,
            VENV_CODE_ECHO,
            use_subprocess=True,
            label="subprocess mixed echo",
        )
    finally:
        PythonWorkerManager.shutdown_all()


# =============================================================================
# Tier C — Identity oracle (echo restores input)
# =============================================================================


@pytest.mark.parametrize("case", _cases(), ids=_case_id)
def test_identity_echo_roundtrip(case: AbGridCase) -> None:
    """Echo through venv must restore input for both always and never pack paths."""
    grid = prepare_grid(case)
    for force in ("always", "never"):
        final = run_venv_roundtrip(grid, VENV_CODE_ECHO, pack_force=force)
        assert_identity_after_echo(grid, final, label=f"{case.id} force={force}")


# =============================================================================
# Tier D — Hypothesis (always vs never)
# =============================================================================


@given(grid=rectangular_grid())
@settings(max_examples=_EX["codec"], deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@example([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0], [9.0, 10.0]])
@example(MIXED_WITH_ZIP)
@example([[42.0]])
@example([["02138"]])
@example(["1", "2", "3"])
@example([1, 2, 3.5])
@example([True, False, 1])
@example(["long_string_" * 8])
@example([[1.0, 2.0], [3.0, 4.0]])
def test_hypothesis_codec_decode_parity(grid: list[Any] | list[list[Any]]) -> None:
    """Fuzz: codec child/host decode always vs never."""
    assume(hypothesis_grid_ok(grid))
    assert_codec_split_vs_nosplit_parity(grid, label="hypothesis codec")


@given(grid=rectangular_grid())
@settings(max_examples=_EX["venv_echo"], deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@example([[1.0, 2.0, 3.0, 4.0]])
@example(MIXED_WITH_ZIP)
@example([[42.0]])
@example(["1", "2", "3"])
@example([1, 2, 3.5])
@example([True, False, 1])
@example(["long_string_" * 8])
@example([[1.0, 2.0], [3.0, 4.0]])
def test_hypothesis_venv_echo_parity(grid: list[Any] | list[list[Any]]) -> None:
    """Fuzz: venv echo always vs never."""
    assume(hypothesis_grid_ok(grid))
    assert_venv_always_never_parity(grid, VENV_CODE_ECHO, label="hypothesis venv echo")


@given(grid=numeric_rectangular_grid())
@settings(max_examples=_EX["venv_sum"], deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@example([[float(r * 4 + c + 1) for c in range(4)] for r in range(4)])
def test_hypothesis_venv_sum_parity(grid: list[Any] | list[list[Any]]) -> None:
    """Fuzz: np.sum always vs never on numeric-coercible grids."""
    assume(hypothesis_grid_ok(grid))
    flat: list[Any]
    if grid and isinstance(grid[0], (list, tuple)):
        flat = [cell for row in grid for cell in row]
    else:
        flat = list(grid)
    if not flat or any(isinstance(v, str) for v in flat):
        return
    assert_venv_always_never_parity(grid, VENV_CODE_SUM, label="hypothesis venv sum")


@pytest.mark.parametrize("grids,label", MULTI_RANGE_FIXTURES, ids=[label for _, label in MULTI_RANGE_FIXTURES])
def test_multi_range_venv_echo(grids: list[list[Any] | list[list[Any]]], label: str) -> None:
    """Multi-range varargs: venv receives data as a list of per-range values."""
    from tests.scripting.serialization_ab_support import run_multi_venv_echo

    result = run_multi_venv_echo(grids, pack_force="auto")
    assert len(result) == len(grids), label
    for idx, grid in enumerate(grids):
        assert flatten_semantic_cells(grid) == flatten_semantic_cells(result[idx]), f"{label}[{idx}]"


@given(grids=multi_range_grid())
@settings(max_examples=_EX["multi_range"], deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
def test_hypothesis_multi_range_venv_echo(grids: list[list[Any] | list[list[Any]]]) -> None:
    """Fuzz: multi-range venv echo."""
    from tests.scripting.serialization_ab_support import run_multi_venv_echo

    # Filter grids
    assume(all(hypothesis_grid_ok(g) for g in grids))

    result = run_multi_venv_echo(grids, pack_force="auto")
    assert len(result) == len(grids)
    for idx, grid in enumerate(grids):
        assert flatten_semantic_cells(grid) == flatten_semantic_cells(result[idx]), f"index {idx}"


@given(result=fancier_result_strategy())
@settings(max_examples=_EX.get("fancier_result", 100), deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
def test_hypothesis_fancier_result_roundtrip(result: Any) -> None:
    """Fuzz: roundtrip complex/fancier results through child_pack_result and host_unpack_data."""
    from plugin.scripting.payload_codec import host_unpack_data, child_pack_result

    packed = child_pack_result(result)
    unpacked = host_unpack_data(packed)

    def normalize(val: Any) -> Any:
        if isinstance(val, tuple):
            return [normalize(x) for x in val]
        if isinstance(val, list):
            return [normalize(x) for x in val]
        if isinstance(val, dict):
            return {k: normalize(v) for k, v in val.items()}
        try:
            import math
            if math.isnan(val):
                return "NaN_sentinel"
        except (TypeError, ValueError):
            pass
        return val

    assert normalize(unpacked) == normalize(result)


if __name__ == "__main__":
    pytest.main([__file__, "-q", "--tb=short"])
