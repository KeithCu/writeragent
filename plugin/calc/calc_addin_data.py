# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Convert Calc add-in range arguments into rectangular Python grids.

Every range is preserved as ``list[list]`` (including 1×N, N×1, and 1×1).
Wire packing wraps each range in a ``calc_range`` envelope; ``split_grid``
remains a private storage optimization inside that envelope.
"""

from __future__ import annotations

from typing import Any

from plugin.scripting.calc_range import (
    ensure_rectangular_2d,
    is_calc_range_payload,
    pack_calc_range_envelope,
)
from plugin.scripting.config_limits import python_max_data_cells_default
from plugin.scripting.payload_codec import ForceBinary, host_pack_data, is_multi_data, is_split_grid, wire_cell_count


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


def normalize_python_data_shape(grid: list[list[Any]], *, as_column: bool = False) -> list[list[Any]]:
    """Preserve rectangular 2D orientation for every Calc range.

    - True 2D block → row-major ``list[list]``
    - Single row → ``[[v1, v2, …]]``
    - Single column → ``[[v1], [v2], …]`` when *as_column* or input was columnar
    - Empty → ``[]``
    """
    if not grid:
        return []
    return ensure_rectangular_2d(grid)


def finalize_python_data(raw: Any) -> list[list[Any]] | None:
    """Normalize tool/API ``data`` that may already be a nested list from the LLM."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return [[raw]]
    if not isinstance(raw, (list, tuple)):
        return [[raw]]
    if not raw:
        return []
    return ensure_rectangular_2d(raw)


def _is_legacy_single_column_range(items: list[Any]) -> bool:
    """True when *items* looks like one column range passed without a varargs outer wrap.

    A bare column is ``((1.0,), (2.0,), …)`` — each row is a length-1 sequence of a
    **scalar**. Two separate single-cell varargs look like ``(((1.0,),), ((2.0,),))``
    and must stay multi-range (inner value is itself a sequence).
    """
    if len(items) <= 1:
        return False
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 1:
            return False
        inner = item[0]
        if isinstance(inner, (list, tuple)) and not isinstance(inner, (str, bytes)):
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
) -> list[list[Any]] | list[list[list[Any]]] | None:
    """Convert pre-split varargs into sandbox grids: one 2D grid or list of 2D grids."""
    if not args:
        return None
    if len(args) == 1:
        return calc_addin_data_to_python(args[0], true_strings, false_strings)
    converted: list[list[list[Any]]] = []
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
) -> list[list[Any]] | list[list[list[Any]]] | None:
    """Convert varargs ``data`` into sandbox grids: one 2D grid or list of 2D grids."""
    return calc_addin_args_from_split(split_python_addin_data_args(raw), true_strings, false_strings)


def calc_addin_data_to_python(
    value: Any,
    true_strings: set[str] | None = None,
    false_strings: set[str] | None = None,
) -> list[list[Any]] | None:
    """Convert a Calc ``=PYTHON()`` second argument into a rectangular 2D grid.

    - Missing / void → ``None`` (no ``data`` injection).
    - Single cell → ``[[v]]``
    - Single row → ``[[v1, v2, …]]``
    - Single column → ``[[v1], [v2], …]``
    - 2D block → ``list[list]`` row-major
    """
    if value is None:
        return None

    value = _unwrap_cell(value, true_strings, false_strings)

    if not _is_row_sequence(value):
        return [[value]]

    rows = list(value)
    if not rows:
        return []

    first = rows[0]
    if _is_row_sequence(first):
        grid = [[_unwrap_cell(c, true_strings, false_strings) for c in row] for row in rows]
        # Column vector from Calc arrives as ((1,), (2,), …)
        if len(grid) > 1 and all(len(r) == 1 for r in grid):
            return grid
        return ensure_rectangular_2d(grid)

    # 1D sequence (single row range from Calc)
    return [ [_unwrap_cell(c, true_strings, false_strings) for c in rows] ]


def pack_calc_multi_data_for_wire(
    py_data: list[list[list[Any]]],
    *,
    force: ForceBinary = "auto",
) -> Any:
    """Pack multiple Calc ranges as ``multi_data`` of ``calc_range`` envelopes."""
    if not py_data:
        return None
    from plugin.scripting.payload_codec import PAYLOAD_MULTI_DATA

    items = [pack_calc_data_for_wire(grid, force=force) for grid in py_data]
    return {
        "__wa_payload__": PAYLOAD_MULTI_DATA,
        "items": items,
    }


def pack_calc_data_for_wire(
    py_data: list[list[Any]] | list[Any] | None,
    *,
    force: ForceBinary = "auto",
    address: str | None = None,
) -> Any:
    """Pack one Calc range as a ``calc_range`` envelope (inner list or split_grid)."""
    if py_data is None:
        return None
    grid = ensure_rectangular_2d(py_data)
    return pack_calc_range_envelope(
        grid,
        address=address,
        pack_inner=lambda g: host_pack_data(g, force=force),
    )


def count_cells(data: Any) -> int:
    """Return number of scalar cells in *data* for size guarding."""
    if is_calc_range_payload(data):
        shape = data.get("shape") or [0, 0]
        return int(shape[0]) * int(shape[1]) if len(shape) == 2 else wire_cell_count(data.get("data"))
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


def values_from_inspector_range(range_data: list[list[dict]]) -> list[list[Any]]:
    """Strip ``CellInspector.read_range`` dicts to a rectangular 2D value grid."""
    grid = [[cell.get("value") for cell in row] for row in range_data]
    return ensure_rectangular_2d(grid)


def _resolve_python_data(ctx: Any, *, data_range: str | None, data: Any) -> tuple[Any | None, str | None]:
    """Return (py_data, error_message). Calc only; ``data_range`` wins over ``data`` when both set."""
    from plugin.calc.bridge import CalcBridge
    from plugin.calc.inspector import CellInspector
    from plugin.scripting.config_limits import configured_python_max_data_cells

    py_data: Any | None = None
    address: str | None = None
    if data_range and str(data_range).strip():
        try:
            bridge = CalcBridge(ctx.doc)
            inspector = CellInspector(bridge)
            addr = str(data_range).strip()
            range_data = inspector.read_range(addr)
            py_data = values_from_inspector_range(range_data)
            address = addr
        except Exception as e:
            return None, f"Failed to read data_range: {e}"
    elif data is not None:
        py_data = finalize_python_data(data)

    if py_data is not None:
        size_err = check_python_data_size(py_data, max_cells=configured_python_max_data_cells(ctx.ctx))
        if size_err:
            return None, size_err
        py_data = pack_calc_data_for_wire(py_data, address=address)
    return py_data, None


def resolve_python_data_on_main_thread(ctx: Any, *, data_range: str | None, data: Any) -> tuple[Any | None, str | None]:
    """Marshal Calc range reads to the LO main thread (``is_async`` tools run on workers)."""
    from plugin.framework.queue_executor import execute_on_main_thread

    return execute_on_main_thread(_resolve_python_data, ctx, data_range=data_range, data=data)
