# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at an option) any later version.
"""Comprehensive A/B serialization testing (split_grid vs pure pickle path).

Runs every test grid through BOTH force="auto" and force="never", then verifies:
- Child materialization produces identical results from both paths
- Full ingress → egress cycle (host_pack → child_unpack → child_pack_result → host_unpack)

This file deliberately avoids any LibreOffice/UNO/Calc dependencies.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from plugin.scripting.payload_codec import (
    BINARY_MIN_CELLS,
    ForceBinary,
    child_pack_result,
    child_unpack_data,
    host_pack_data,
    host_unpack_data,
    is_split_grid,
)
from tests.scripting.payload_codec_test_support import (
    MIXED_LABEL_GRID,
    MIXED_WITH_ZIP,
    NUMERIC_4X4,
)


def _make_numeric_grid(rows: int, cols: int) -> list[list[float]]:
    return [[float(r * cols + c + 1) for c in range(cols)] for r in range(rows)]


def _make_mixed_grid() -> list[list[Any]]:
    return [
        [1, "hello", 3.14, True, None],
        [100, "world", 2.71, False, 42],
        [999, "02138", -1.0, True, math.nan],
    ]


def _make_edge_case_grid() -> list[list[Any]]:
    return [
        [0, 1, -1, 1.0, -1.0],
        [True, False, None, "", "   "],
        [1e10, 1e-10, float("inf"), float("-inf"), 0.0],
        ["café", "日本語", "emoji🎉", "zip:90210", "long string value here"],
    ]


def _make_large_numeric(rows: int = 30, cols: int = 15) -> list[list[float]]:
    return [[float(r * cols + c) for c in range(cols)] for r in range(rows)]


def _make_single_column_ints(n: int = 15) -> list[list[int]]:
    return [[i] for i in range(n)]


def _make_1d_mixed() -> list[Any]:
    return [10, "text", 3.14, None, True, "02138", 0, False]


def get_all_ab_test_grids() -> list[tuple[str, list[Any] | list[list[Any]]]]:
    """Return a comprehensive set of rectangular test grids for A/B testing."""
    grids: list[tuple[str, Any]] = [
        ("small_numeric_3x3", _make_numeric_grid(3, 3)),
        ("numeric_4x4", NUMERIC_4X4),
        ("mixed_label", MIXED_LABEL_GRID),
        ("mixed_zip", MIXED_WITH_ZIP),
        ("mixed_complex", _make_mixed_grid()),
        ("edge_cases", _make_edge_case_grid()),
        ("large_30x15", _make_large_numeric(30, 15)),
        ("single_col_ints_15", _make_single_column_ints(15)),
        ("flat_1d_mixed", _make_1d_mixed()),
        ("tiny_below_threshold", [[1, 2], [3, 4]]),  # 4 cells < 10
        ("exactly_threshold", _make_numeric_grid(2, 5)),  # 10 cells
        ("all_bools", [[True, False], [False, True]]),
        ("int_float_mixed_cols", [[1, 2.5], [3, 4.0], [5, 6.5]]),
        ("with_inf_nan", [[1.0, float("inf")], [float("-inf"), math.nan]]),
        ("unicode_strings", [["café", "日本語"], ["emoji🎉", "test"]]),
    ]
    return grids


def run_full_cycle(
    original_grid: list[Any] | list[list[Any]],
    *,
    force: ForceBinary = "auto",
) -> tuple[Any, Any, bool]:
    """
    Simulate full ingress + egress cycle.

    Returns: (child_materialized, final_host_result, used_split_grid)
    """
    # Host → wire
    wire = host_pack_data(original_grid, force=force)
    used_split = is_split_grid(wire)

    # Child ingress
    child_data = child_unpack_data(wire)

    # Simulate "user code" result — here we just round-trip the data as-is
    # (in real use this would be np.sum, model output, etc.)
    result = child_data

    # Child egress
    child_wire = child_pack_result(result, force=force)

    # Host final unpack
    final = host_unpack_data(child_wire)

    return child_data, final, used_split


def assert_results_equivalent(a: Any, b: Any, label: str = "") -> None:
    """Assert two results are semantically equivalent (handles ndarray vs list, NaN, etc.)."""
    # Normalize everything to native Python types first (fixes mixed ndarray/list cases)
    if isinstance(a, np.ndarray):
        a = a.tolist()
    if isinstance(b, np.ndarray):
        b = b.tolist()

    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        assert len(a) == len(b), f"{label}: list length mismatch"
        for i, (x, y) in enumerate(zip(a, b)):
            assert_results_equivalent(x, y, f"{label}[{i}]")
        return

    # Scalars
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return
        assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9), f"{label}: float mismatch {a} vs {b}"
        return

    assert a == b, f"{label}: value mismatch {a!r} vs {b!r}"


# =============================================================================
# Actual Tests
# =============================================================================

@pytest.mark.parametrize("name,grid", get_all_ab_test_grids())
def test_ab_roundtrip_child_materialization(name: str, grid: Any) -> None:
    """Both force paths must produce identical child-side materialization."""
    wire_auto = host_pack_data(grid, force="auto")
    wire_never = host_pack_data(grid, force="never")

    child_auto = child_unpack_data(wire_auto)
    child_never = child_unpack_data(wire_never)

    assert_results_equivalent(
        child_auto, child_never, f"{name} child materialization"
    )


@pytest.mark.parametrize("name,grid", get_all_ab_test_grids())
def test_full_ingress_egress_cycle(name: str, grid: Any) -> None:
    """Full cycle (host_pack → child_unpack → child_pack_result → host_unpack) must be consistent."""
    # Run with split_grid when possible
    child_auto, final_auto, used_auto = run_full_cycle(grid, force="auto")

    # Run pure pickle path
    child_never, final_never, used_never = run_full_cycle(grid, force="never")

    # Child materialization must match between paths
    assert_results_equivalent(child_auto, child_never, f"{name} child")

    # Final host result after full cycle must match
    assert_results_equivalent(final_auto, final_never, f"{name} final host")

    # For grids >= threshold, at least one path should have used split_grid
    ncells = sum(len(row) if isinstance(row, (list, tuple)) else 1 for row in grid)
    if ncells >= BINARY_MIN_CELLS:
        assert used_auto or used_never, f"{name}: expected at least one path to use split_grid"


def test_double_serialization_large_numeric() -> None:
    """Explicit double serialization test on a larger grid (recommended for manual inspection)."""
    grid = _make_large_numeric(30, 15)

    for force in ("auto", "never"):
        wire1 = host_pack_data(grid, force=force)
        child1 = child_unpack_data(wire1)

        # Second serialization from child result
        wire2 = child_pack_result(child1, force=force)
        final = host_unpack_data(wire2)

        assert_results_equivalent(child1, final, f"double_serialization_{force}")


if __name__ == "__main__":
    # Allow running directly for quick manual verification
    pytest.main([__file__, "-q", "--tb=short"])