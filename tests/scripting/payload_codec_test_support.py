# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared helpers for payload_codec / split_grid tests."""

from __future__ import annotations

import base64
import math
import pickle
from typing import Any

from plugin.scripting.payload_codec import (
    BINARY_MIN_CELLS,
    PAYLOAD_SPLIT_GRID,
    SPLIT_GRID_WIRE_DTYPE,
    _flatten_grid_to_components,
    _apply_column_kinds_to_ndarray,
    envelope_column_kinds,
    envelope_uniform_column_kind,
    host_pack_split_grid,
    host_unpack_split_grid,
    child_pack_split_grid,
    child_unpack_split_grid,
)


def _host_cell_from_float(val: float, *, kind: str) -> Any:
    """Legacy b64 oracle: NaN becomes None, int columns become int.

    Production unpack does not use this. The oracle is meant to drift from
    the host path so a wire change shows up as a difference.
    """
    if math.isnan(val):
        return None
    return int(val) if kind == "int" else val


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


def legacy_b64_host_pack_split_grid(grid: list[Any] | list[list[Any]]) -> dict[str, Any]:
    """Historical JSON/Base64 split_grid envelope (bench + regression only)."""
    if not grid:
        return {
            "__wa_payload__": PAYLOAD_SPLIT_GRID,
            "dtype": SPLIT_GRID_WIRE_DTYPE,
            "column_kinds": [],
            "shape": [0],
            "strings": {},
            "b64": "",
        }
    buf, strings, column_kinds, shape = _flatten_grid_to_components(grid)
    return {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "dtype": SPLIT_GRID_WIRE_DTYPE,
        "column_kinds": column_kinds,
        "shape": shape,
        "strings": strings,
        "b64": base64.b64encode(buf.tobytes()).decode("ascii"),
    }


def legacy_b64_host_unpack_split_grid(envelope: dict[str, Any]) -> list[Any] | list[list[Any]]:
    """Old base64 decoder kept as the A/B baseline.

    Do not call ``host_unpack_split_grid`` from here. This oracle is supposed
    to drift from production so a wire change shows up as a difference.
    """
    import array

    b64_str = envelope.get("b64", "")
    raw = base64.b64decode(b64_str.encode("ascii"))
    buf = array.array("d")
    buf.frombytes(raw)
    shape = envelope["shape"]
    is_1d = len(shape) == 1
    nrows, ncols = (shape[0], 1) if is_1d else (shape[0], shape[1])
    strings = envelope.get("strings", {})
    uniform = envelope_uniform_column_kind(envelope, ncols=ncols)

    flat_list: list[Any]
    if not strings and uniform is not None:
        if uniform == "int":
            flat_list = [None if math.isnan(v) else int(v) for v in buf]
        else:
            flat_list = [None if math.isnan(v) else v for v in buf]
    else:
        column_kinds = envelope_column_kinds(envelope, ncols=ncols)
        flat_list = [
            strings[str(i)] if str(i) in strings else
            _host_cell_from_float(val, kind=column_kinds[0 if is_1d else i % ncols])
            for i, val in enumerate(buf)
        ]

    if is_1d:
        return flat_list
    return [flat_list[r * ncols : (r + 1) * ncols] for r in range(nrows)]


def legacy_b64_child_pack_split_grid(arr: Any) -> dict[str, Any]:
    import numpy as np

    if not isinstance(arr, np.ndarray):
        arr = np.asarray(arr)
    ncols = int(arr.shape[1]) if arr.ndim == 2 else 1
    if np.issubdtype(arr.dtype, np.integer):
        column_kinds = ["int"] * ncols
    else:
        column_kinds = ["float"] * ncols
    wire_arr = np.ascontiguousarray(arr, dtype=np.float64)
    return {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "dtype": SPLIT_GRID_WIRE_DTYPE,
        "column_kinds": column_kinds,
        "shape": list(wire_arr.shape),
        "strings": {},
        "b64": base64.b64encode(wire_arr.tobytes()).decode("ascii"),
    }


def legacy_b64_child_unpack_split_grid(envelope: dict[str, Any]) -> Any:
    import numpy as np

    shape = envelope["shape"]
    is_1d = len(shape) == 1
    nrows, ncols = (shape[0], 1) if is_1d else (shape[0], shape[1])
    b64_str = envelope.get("b64", "")
    raw = base64.b64decode(b64_str.encode("ascii"))
    uniform = envelope_uniform_column_kind(envelope, ncols=ncols)
    column_kinds = envelope_column_kinds(envelope, ncols=ncols)
    strings = envelope.get("strings", {})

    if not strings:
        arr = np.frombuffer(raw, dtype=np.float64)
        if not is_1d:
            arr = arr.reshape((nrows, ncols))
        return _apply_column_kinds_to_ndarray(arr, uniform=uniform)

    flat_list = np.frombuffer(raw, dtype=np.float64).tolist()
    for i, val in enumerate(flat_list):
        str_idx = str(i)
        if str_idx in strings:
            flat_list[i] = strings[str_idx]
        elif math.isnan(val):
            flat_list[i] = None
        else:
            col = 0 if is_1d else i % ncols
            if column_kinds[col] == "int":
                flat_list[i] = int(val)

    if is_1d:
        return flat_list
    return [flat_list[r * ncols : (r + 1) * ncols] for r in range(nrows)]


__all__ = [
    "NUMERIC_4X4",
    "MIXED_WITH_ZIP",
    "MIXED_LABEL_GRID",
    "pickle5_roundtrip",
    "legacy_b64_host_pack_split_grid",
    "legacy_b64_host_unpack_split_grid",
    "legacy_b64_child_pack_split_grid",
    "legacy_b64_child_unpack_split_grid",
    "host_pack_split_grid",
    "host_unpack_split_grid",
    "child_pack_split_grid",
    "child_unpack_split_grid",
]
