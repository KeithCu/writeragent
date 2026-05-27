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

from plugin.scripting.config_limits import python_max_data_cells_default
from plugin.scripting.payload_codec import ForceBinary, host_pack_data, host_pack_multi_data, is_multi_data, is_split_grid, wire_cell_count


def _unwrap_cell(value: Any, true_strings: set[str] | None = None, false_strings: set[str] | None = None) -> Any:
    """Normalize a single cell value from UNO / Calc."""
    if value is None:
        return None
    # PyUNO may wrap values in uno.Any
    if type(value).__name__ == "Any" and hasattr(value, "value"):
        value = value.value

    if isinstance(value, str):
        if value == "":
            return None
        val_stripped = value.strip()
        if true_strings and val_stripped in true_strings:
            return True
        if false_strings and val_stripped in false_strings:
            return False
        return value

    if isinstance(value, (bool, int, float)):
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


def _is_legacy_single_column_range(items: list[Any]) -> bool:
    """True when *items* looks like one column range passed without a varargs outer wrap."""
    if len(items) <= 1:
        return False
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 1:
            return False
    return True


def split_python_addin_data_args(raw: Any) -> list[Any]:
    """Split the ``=PYTHON()`` varargs ``data`` parameter into individual formula arguments.

    Calc packs all trailing arguments into ``sequence<any>``. Unit tests may pass a bare range
  or scalar without the outer sequence wrapper.
    """
    if raw is None:
        return []
    raw = _unwrap_cell(raw)
    if not _is_row_sequence(raw):
        return [raw]
    items = list(raw)
    if not items:
        return []
    if len(items) == 1:
        return [items[0]]
    if _is_legacy_single_column_range(items):
        return [raw]
    return items


def calc_addin_args_from_split(
    args: list[Any],
    true_strings: set[str] | None = None,
    false_strings: set[str] | None = None,
) -> list[Any] | list[list[Any]] | list[list[Any] | list[list[Any]]] | None:
    """Convert pre-split varargs into sandbox ``data``: single shape or list of per-range shapes."""
    if not args:
        return None
    if len(args) == 1:
        return calc_addin_data_to_python(args[0], true_strings, false_strings)
    converted: list[list[Any] | list[list[Any]]] = []
    for arg in args:
        py_range = calc_addin_data_to_python(arg, true_strings, false_strings)
        if py_range is None:
            py_range = []
        converted.append(py_range)
    return converted


def calc_addin_args_to_python(
    raw: Any,
    true_strings: set[str] | None = None,
    false_strings: set[str] | None = None,
) -> list[Any] | list[list[Any]] | list[list[Any] | list[list[Any]]] | None:
    """Convert varargs ``data`` into sandbox ``data``: single shape or list of per-range shapes."""
    return calc_addin_args_from_split(split_python_addin_data_args(raw), true_strings, false_strings)


def calc_addin_data_to_python(
    value: Any,
    true_strings: set[str] | None = None,
    false_strings: set[str] | None = None,
) -> list[Any] | list[list[Any]] | None:
    """Convert a Calc ``=PYTHON()`` second argument into plain Python data.

    - Missing / void → ``None`` (no ``data`` injection).
    - Single cell, row, or column → flat ``[v1, v2, …]`` (``sum(data)``, ``max(data)``, etc.).
    - 2D block (e.g. A1:C5) → ``list[list]`` row-major.
    """
    if value is None:
        return None

    value = _unwrap_cell(value, true_strings, false_strings)

    if not _is_row_sequence(value):
        return [value]

    rows = list(value)
    if not rows:
        return []

    first = rows[0]
    if _is_row_sequence(first):
        grid = [[_unwrap_cell(c, true_strings, false_strings) for c in row] for row in rows]
    else:
        # 1D sequence (single row range from Calc)
        grid = [[_unwrap_cell(c, true_strings, false_strings) for c in rows]]

    return normalize_python_data_shape(grid)


def pack_calc_multi_data_for_wire(
    py_data: list[list[Any] | list[list[Any]]],
    *,
    force: ForceBinary = "auto",
) -> Any:
    """Pack multiple Calc ranges for the venv worker (``multi_data`` envelope)."""
    if not py_data:
        return None
    return host_pack_multi_data(py_data, force=force)


def pack_calc_data_for_wire(
    py_data: list[Any] | list[list[Any]] | None,
    *,
    force: ForceBinary = "auto",
) -> Any:
    """Pack Calc ``data`` for the venv worker (json list or split_grid when dense numeric)."""
    if py_data is None:
        return None
    return host_pack_data(py_data, force=force)


def count_cells(data: Any) -> int:
    """Return number of scalar cells in *data* for size guarding."""
    if is_multi_data(data):
        return wire_cell_count(data)
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


def check_python_multi_data_size(
    data: list[Any],
    *,
    max_cells: int | None = None,
) -> str | None:
    """Return an error message if combined multi-range *data* exceeds *max_cells*."""
    limit = python_max_data_cells_default() if max_cells is None else max_cells
    n = sum(count_cells(item) for item in data)
    if n > limit:
        return f"Data ranges have {n} cells combined; maximum is {limit}."
    return None


def check_python_data_size(
    data: Any,
    *,
    max_cells: int | None = None,
) -> str | None:
    """Return an error message if *data* exceeds *max_cells*, else ``None``.

    *max_cells* defaults to schema default for ``scripting.python_max_data_cells``; callers with
    UNO context should pass ``configured_python_max_data_cells(ctx)``.
    """
    limit = python_max_data_cells_default() if max_cells is None else max_cells
    n = count_cells(data)
    if n > limit:
        return f"Data range has {n} cells; maximum is {limit}."
    return None


def values_from_inspector_range(range_data: list[list[dict]]) -> list[Any] | list[list[Any]]:
    """Strip ``CellInspector.read_range`` dicts to a 2D value list."""
    grid = [[cell.get("value") for cell in row] for row in range_data]
    return normalize_python_data_shape(grid)
