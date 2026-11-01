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

A bare column ``((1.0,), (2.0,))`` is one range; two 1×1 varargs
``(((1.0,),), ((2.0,),))`` stay two. ``_is_legacy_single_column_range``
decides; do not "simplify" it away.
"""

# crosshair: off
from __future__ import annotations

from typing import Any

from plugin.scripting.calc_range import ensure_rectangular_2d, pack_calc_range_envelope
from plugin.scripting.payload_codec import ForceBinary, host_pack_data, PAYLOAD_MULTI_DATA, wire_cell_count


from plugin.framework.deal_shim import deal
from plugin.framework.errors import CalcError, reraise_if_disposed


# Logical string coercion targets. Static baseline sets are used instead of
# XFormulaOpCodeMapper discovery: XFormulaOpCodeMapper in LibreOffice does
# not expose getAvailableSymbolTokens (only getMappings/getAvailableMappings)
# and FormulaToken lacks a .Token attribute, so dynamic mapping was dead code
# that always failed at runtime and fell back to these English defaults.
_TRUE_STRINGS: set[str] = {"=TRUE()", "TRUE", "True"}
_FALSE_STRINGS: set[str] = {"=FALSE()", "FALSE", "False"}


def _unwrap_cell(value: Any) -> Any:
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
        if val_stripped in _TRUE_STRINGS:
            return True
        if val_stripped in _FALSE_STRINGS:
            return False

    return value


def _is_row_sequence(value: Any) -> bool:
    """Plain list/tuple rows only; dict/set/generator/namedtuple is one scalar cell."""
    return type(value) is list or type(value) is tuple


@deal.post(lambda result: result is None or isinstance(result, list))
def finalize_python_data(raw: Any) -> list[list[Any]] | None:
    """Normalize tool/API ``data`` that may already be a nested list from the LLM."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return [[raw]]
    # Plain list/tuple only — namedtuple subclasses tuple and must not be treated as a grid
    # (CrossHair found SplitResultBytes → TypeError iterating int fields in ensure_rectangular_2d).
    if type(raw) is not list and type(raw) is not tuple:
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
    return len(items) > 1 and all(
        isinstance(item, (list, tuple)) and len(item) == 1 and not isinstance(item[0], (list, tuple))
        for item in items
    )


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
    if _is_legacy_single_column_range(items):
        return [raw]
    return items


def calc_addin_args_from_split(args: list[Any]) -> list[list[Any]] | list[list[list[Any]]] | None:
    """Convert pre-split varargs into sandbox grids: one 2D grid or list of 2D grids."""
    if not args:
        return None
    if len(args) == 1:
        return calc_addin_data_to_python(args[0])
    return [calc_addin_data_to_python(arg) or [] for arg in args]


@deal.post(lambda result: result is None or isinstance(result, list))
def calc_addin_data_to_python(value: Any) -> list[list[Any]] | None:
    """Convert a Calc ``=PYTHON()`` second argument into a rectangular 2D grid.

    - Missing / void → ``None`` (no ``data`` injection).
    - Single cell → ``[[v]]``
    - Single row → ``[[v1, v2, …]]``
    - Single column → ``[[v1], [v2], …]``
    - 2D block → ``list[list]`` row-major
    """
    if value is None:
        return None

    value = _unwrap_cell(value)

    if not _is_row_sequence(value):
        return [[value]]

    rows = list(value)
    if not rows:
        return []
    # A flat sequence is one Calc row. A sequence of sequences is already 2D.
    if not _is_row_sequence(rows[0]):
        rows = [rows]
    grid = [[_unwrap_cell(c) for c in row] for row in rows]
    return ensure_rectangular_2d(grid)


def pack_calc_multi_data_for_wire(py_data: list[list[list[Any]]], *, force: ForceBinary = "auto", addresses: list[str | None] | None = None) -> Any:
    """Pack multiple Calc ranges as ``multi_data`` of ``calc_range`` envelopes."""
    if not py_data:
        return None
    addrs = list(addresses) if addresses else []
    if len(addrs) < len(py_data):
        addrs.extend([None] * (len(py_data) - len(addrs)))
    items = [pack_calc_data_for_wire(grid, force=force, address=addr) for grid, addr in zip(py_data, addrs)]
    return {"__wa_payload__": PAYLOAD_MULTI_DATA, "items": items}


def pack_calc_data_for_wire(py_data: list[list[Any]] | list[Any] | None, *, force: ForceBinary = "auto", address: str | None = None) -> Any:
    """Pack one Calc range as a ``calc_range`` envelope (inner list or split_grid)."""
    if py_data is None:
        return None
    # pack_calc_range_envelope runs ensure_rectangular_2d and sets shape from
    # that grid. Doing it here first copied every cell again on the =PY()
    # pack path. materialize_calc_range already stopped the matching double
    # walk on the way back in.
    envelope = pack_calc_range_envelope(py_data, address=address)
    envelope["data"] = host_pack_data(envelope["data"], force=force)
    return envelope


def check_python_data_size(data: Any, *, max_cells: int, multi: bool = False) -> str | None:
    """Return an error message if *data* exceeds *max_cells*, else ``None``."""
    if multi:
        n = sum(wire_cell_count(item) for item in data)
        if n > max_cells:
            return f"Data ranges have {n} cells combined; maximum is {max_cells}."
        return None
    n = wire_cell_count(data)
    if n > max_cells:
        return f"Data range has {n} cells; maximum is {max_cells}."
    return None


def values_from_inspector_range(range_data: list[list[dict[str, Any]]]) -> list[list[Any]]:
    """Strip ``CellInspector.read_range`` dicts to a rectangular 2D value grid."""
    grid = [[cell.get("value") for cell in row] for row in range_data]
    return ensure_rectangular_2d(grid)


def _normalize_data_range_addresses(data_range: Any) -> list[str]:
    """Normalize chat/RPS ``data_range`` into one or more A1 addresses.

    A string (optionally comma/semicolon-separated) or a list/tuple of strings; non-string members are ignored.
    """
    if data_range is None:
        return []
    from plugin.calc.python.formula_edit import parse_data_binding_text

    items = data_range if isinstance(data_range, (list, tuple)) else [str(data_range)]
    out: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        parts = parse_data_binding_text(item)
        out.extend(parts if parts else [item.strip()])
    return out


def _resolve_python_data(doc: Any, *, data_range: Any = None, data: Any = None) -> tuple[Any | None, str | None]:  # pyright: ignore[reportUnusedFunction]
    """Return (py_data, error_message). Calc only; ``data_range`` wins over ``data`` when both set.

    ``data_range`` may be one A1 string, a comma/semicolon-separated string, or a list of addresses.
    Multiple ranges pack as ``multi_data`` of ``calc_range`` (same as ``=PY()`` varargs).
    """
    from plugin.calc.bridge import CalcBridge
    from plugin.calc.inspector import CellInspector
    from plugin.scripting.config_limits import configured_python_max_data_cells

    addresses = _normalize_data_range_addresses(data_range)
    max_cells = configured_python_max_data_cells()

    grids: list[list[list[Any]]] | None = None
    if addresses:
        try:
            bridge = CalcBridge(doc)
            inspector = CellInspector(bridge)
            grids = [values_from_inspector_range(inspector.read_range(addr)) for addr in addresses]
        except CalcError as e:
            return None, str(e)
        except Exception as e:
            reraise_if_disposed(e)
            return None, f"Failed to read data_range: {e}"
    elif data is not None:
        one = finalize_python_data(data)
        if one is not None:
            grids = [one]
    if grids is None:
        return None, None
    is_multi = len(grids) > 1
    size_err = check_python_data_size(grids if is_multi else grids[0], max_cells=max_cells, multi=is_multi)
    if size_err:
        return None, size_err
    if not is_multi:
        return pack_calc_data_for_wire(grids[0], address=addresses[0] if addresses else None), None
    return pack_calc_multi_data_for_wire(grids, addresses=list(addresses)), None
