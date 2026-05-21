# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Convert Calc add-in range arguments into JSON-serializable Python structures."""

from __future__ import annotations

from typing import Any

from plugin.scripting.payload_codec import host_pack_data, is_split_grid, wire_cell_count

# Cap passed to venv subprocess (JSON size / eval time); ~500x500 sheet.
MAX_PYTHON_DATA_CELLS = 250_000


def _unwrap_cell(value: Any) -> Any:
    """Normalize a single cell value from UNO / Calc."""
    if value is None:
        return None
    # PyUNO may wrap values in uno.Any
    if type(value).__name__ == "Any" and hasattr(value, "value"):
        value = value.value
    if isinstance(value, str) and value == "":
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    return value


def _is_row_sequence(value: Any) -> bool:
    if isinstance(value, (str, bytes)):
        return False
    try:
        iter(value)
    except TypeError:
        return False
    return True


def normalize_python_data_shape(grid: list[list[Any]]) -> list[Any] | list[list[Any]]:
    """Shape range data for beginner-friendly formulas like ``sum(data)``.

    - Single row, single column, or one cell → flat ``[v1, v2, …]`` so ``sum(data)`` works.
    - True 2D block (both dimensions > 1) → row-major ``list[list]`` (see docs for summing).
    """
    if not grid:
        return []
    nrows = len(grid)
    ncols = max((len(row) for row in grid), default=0)
    if nrows > 1 and ncols > 1:
        return grid
    if nrows == 1:
        return list(grid[0])
    if ncols == 1:
        return [row[0] if row else None for row in grid]
    return list(grid[0]) if grid else []


def finalize_python_data(raw: Any) -> list[Any] | list[list[Any]] | None:
    """Normalize tool/API ``data`` that may already be a nested list from the LLM."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, (list, tuple)):
        return [raw]
    if not raw:
        return []
    first = raw[0]
    if isinstance(first, (list, tuple)):
        return normalize_python_data_shape([list(row) for row in raw])
    return list(raw)


def calc_addin_data_to_python(value: Any) -> list[Any] | list[list[Any]] | None:
    """Convert a Calc ``=PYTHON()`` second argument into plain Python data.

    - Missing / void → ``None`` (no ``data`` injection).
    - Single cell, row, or column → flat ``[v1, v2, …]`` (``sum(data)``, ``max(data)``, etc.).
    - 2D block (e.g. A1:C5) → ``list[list]`` row-major.
    """
    if value is None:
        return None

    value = _unwrap_cell(value)

    if not _is_row_sequence(value):
        return [value]

    rows = list(value)
    if not rows:
        return []

    first = rows[0]
    if _is_row_sequence(first):
        grid = [[_unwrap_cell(c) for c in row] for row in rows]
    else:
        # 1D sequence (single row range from Calc)
        grid = [[_unwrap_cell(c) for c in rows]]

    return normalize_python_data_shape(grid)


def pack_calc_data_for_wire(py_data: list[Any] | list[list[Any]] | None) -> Any:
    """Pack Calc ``data`` for the venv worker (json list or split_grid when dense numeric)."""
    if py_data is None:
        return None
    return host_pack_data(py_data)


def count_cells(data: Any) -> int:
    """Return number of scalar cells in *data* for size guarding."""
    if is_split_grid(data):
        return wire_cell_count(data)
    if data is None:
        return 0
    if not isinstance(data, (list, tuple)):
        return 1
    if not data:
        return 0
    first = data[0]
    if isinstance(first, (list, tuple)):
        return sum(len(row) for row in data)
    return len(data)


def check_python_data_size(data: Any, *, max_cells: int = MAX_PYTHON_DATA_CELLS) -> str | None:
    """Return an error message if *data* exceeds *max_cells*, else ``None``."""
    n = count_cells(data)
    if n > max_cells:
        return f"Data range has {n} cells; maximum is {max_cells}."
    return None


def values_from_inspector_range(range_data: list[list[dict]]) -> list[Any] | list[list[Any]]:
    """Strip ``CellInspector.read_range`` dicts to a 2D value list."""
    grid = [[cell.get("value") for cell in row] for row in range_data]
    return normalize_python_data_shape(grid)
