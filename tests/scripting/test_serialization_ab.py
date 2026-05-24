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
from hypothesis import given, settings, example

from plugin.scripting.payload_codec import host_pack_data, is_split_grid
from plugin.scripting.python_worker_manager import PythonWorkerManager
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
)


def _case_id(case: AbGridCase) -> str:
    return case.id


def _cases() -> list[AbGridCase]:
    return all_codec_ab_cases()


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


@pytest.mark.parametrize("case", _cases(), ids=_case_id)
def test_child_materialization_type(case: AbGridCase) -> None:
    """Under force=always: numeric-only → ndarray; any string → nested list."""
    pytest.importorskip("numpy")
    grid = prepare_grid(case)
    if grid_cell_count(grid) <= 1:
        pytest.skip("single-cell inputs unwrap to scalar in child")
    if not grid:
        pytest.skip("empty grid")
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
@settings(max_examples=100, deadline=None)
@example([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0], [9.0, 10.0]])
@example(MIXED_WITH_ZIP)
@example([[42.0]])
@example([["02138"]])
@example(["1", "2", "3"])
@example([1, 2, 3.5])
@example([True, False, 1])
@example(["long_string_" * 50])
@example([[float(i) for i in range(10)] for _ in range(10)])
def test_hypothesis_codec_decode_parity(grid: list[Any] | list[list[Any]]) -> None:
    """Fuzz: codec child/host decode always vs never."""
    if not hypothesis_grid_ok(grid):
        return
    assert_codec_split_vs_nosplit_parity(grid, label="hypothesis codec")


@given(grid=rectangular_grid())
@settings(max_examples=100, deadline=None)
@example([[float(i + 1) for i in range(10)]])
@example(MIXED_WITH_ZIP)
@example([[42.0]])
@example(["1", "2", "3"])
@example([1, 2, 3.5])
@example([True, False, 1])
@example(["long_string_" * 50])
@example([[float(i) for i in range(10)] for _ in range(10)])
def test_hypothesis_venv_echo_parity(grid: list[Any] | list[list[Any]]) -> None:
    """Fuzz: venv echo always vs never."""
    if not hypothesis_grid_ok(grid):
        return
    assert_venv_always_never_parity(grid, VENV_CODE_ECHO, label="hypothesis venv echo")


@given(grid=numeric_rectangular_grid())
@settings(max_examples=80, deadline=None)
@example([[float(r * 4 + c + 1) for c in range(4)] for r in range(4)])
def test_hypothesis_venv_sum_parity(grid: list[Any] | list[list[Any]]) -> None:
    """Fuzz: np.sum always vs never on numeric-coercible grids."""
    if not hypothesis_grid_ok(grid):
        return
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
@settings(max_examples=50, deadline=None)
def test_hypothesis_multi_range_venv_echo(grids: list[list[Any] | list[list[Any]]]) -> None:
    """Fuzz: multi-range venv echo."""
    from tests.scripting.serialization_ab_support import run_multi_venv_echo

    # Filter grids
    if not all(hypothesis_grid_ok(g) for g in grids):
        return

    result = run_multi_venv_echo(grids, pack_force="auto")
    assert len(result) == len(grids)
    for idx, grid in enumerate(grids):
        assert flatten_semantic_cells(grid) == flatten_semantic_cells(result[idx]), f"index {idx}"


if __name__ == "__main__":
    pytest.main([__file__, "-q", "--tb=short"])
