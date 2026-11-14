# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared helpers for payload_codec / split_grid tests."""

from __future__ import annotations

import pickle
from typing import Any

from plugin.scripting.payload_codec import (
    BINARY_MIN_CELLS,
    host_pack_split_grid,
    host_unpack_split_grid,
    child_pack_split_grid,
    child_unpack_split_grid,
)


def pickle5_roundtrip(envelope: dict[str, Any]) -> dict[str, Any]:
    """Mirror worker IPC: Pickle protocol 5 preserves raw buffer bytes."""
    return pickle.loads(pickle.dumps(envelope, protocol=5))


def rect_shape_for_cell_count(n: int) -> tuple[int, int]:
    """Return ``(rows, cols)`` for a rectangular grid with exactly ``n`` cells (fewest rows)."""
    if n <= 0:
        return 0, 0
    best: tuple[int, int] | None = None
    for cols in range(1, n + 1):
        if n % cols != 0:
            continue
        rows = n // cols
        if best is None or rows < best[0] or (rows == best[0] and cols < best[1]):
            best = (rows, cols)
    if best is None:
        raise ValueError(f"no rectangular shape for {n} cells")
    return best


def grid_with_cell_count(n: int) -> list[list[float]]:
    """Row-major grid with values ``1..n`` (inclusive)."""
    rows, cols = rect_shape_for_cell_count(n)
    return [[float(r * cols + c + 1) for c in range(cols)] for r in range(rows)]


def sequential_grid_sum(n: int) -> float:
    """Sum of ``1..n`` (matches :func:`grid_with_cell_count`)."""
    return float(n * (n + 1) // 2)


# Rectangular grids used across payload_codec tests (Calc-realistic shapes).
NUMERIC_4X4 = [[float(r * 10 + c) for c in range(4)] for r in range(4)]
NUMERIC_AT_THRESHOLD = grid_with_cell_count(BINARY_MIN_CELLS)
NUMERIC_BELOW_THRESHOLD = grid_with_cell_count(max(1, BINARY_MIN_CELLS - 1))

MIXED_WITH_ZIP = [
    [100, "02138", 1.5],
    [101, "90210", 2.5],
    [102, "10001", 3.5],
    [103, "60601", 4.5],
]

MIXED_LABEL_GRID = [
    [1.0, "apple", 10.0],
    [2.0, "banana", 20.0],
    [3.0, "cherry", 30.0],
    [4.0, "date", 40.0],
]


__all__ = [
    "NUMERIC_4X4",
    "MIXED_WITH_ZIP",
    "MIXED_LABEL_GRID",
    "pickle5_roundtrip",
    "host_pack_split_grid",
    "host_unpack_split_grid",
    "child_pack_split_grid",
    "child_unpack_split_grid",
]
