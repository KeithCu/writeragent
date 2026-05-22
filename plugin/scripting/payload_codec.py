# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Wire codec for Calc/chat data crossing the LO host (plain Python) and venv (NumPy).

Large 2D grids (numeric or mixed numeric-text) use Strategy 3 ``split_grid``: the entire
grid is serialized as a single contiguous double-precision flat float64 array (base64-encoded)
plus a parallel sparse JSON strings dictionary. When the strings dictionary is empty,
NumPy in the child process ingests that via C-speed ``frombuffer`` + ``reshape`` — a direct
memory view over decoded bytes without any Python list/loop transpositions.

Adjust thresholds below if product policy changes; bench and production share this module.
"""
from __future__ import annotations

import array
import base64
import logging
import math
from typing import Any, Literal, cast

log = logging.getLogger(__name__)

SERIALIZATION = "pickle"  # Can be "pickle" or "json"

# --- Wire kind (JSON-safe dict tag) -----------------------------------------------

PAYLOAD_SPLIT_GRID = "split_grid"
"""Unified 2D grids: dense numeric flat float64 array and sparse strings dictionary."""

# --- When to use binary envelope (default: at least 10 cells) -----------------------

BINARY_MIN_CELLS = 10
"""Use split_grid when total cell count is at least this (10+ cells)."""

MAX_BENCH_CELLS = 10_000
"""Upper cap for benchmark grids (production may use calc_addin MAX_PYTHON_DATA_CELLS)."""

ForceBinary = Literal["auto", "always", "never"]
SPLIT_GRID_WIRE_DTYPE = "float64"
ColumnKind = Literal["int", "float"]


def column_kinds_for_grid(grid: list[Any] | list[list[Any]]) -> list[ColumnKind]:
    """Policy helper (tests): per-column int/float from source types; mirrors host_pack_split_grid."""
    if not grid:
        return []
    is_2d = isinstance(grid[0], (list, tuple))
    if is_2d:
        ncols = max((len(r) for r in grid), default=0)
        kinds = cast("list[ColumnKind]", ["int"] * ncols)
        for row in grid:
            for c, val in enumerate(row):
                if isinstance(val, float) or val is None:
                    kinds[c] = "float"
        return kinds
    return cast("list[ColumnKind]", ["float" if any(isinstance(val, float) or val is None for val in grid) else "int"])


def _uniform_column_kind(kinds: list[ColumnKind]) -> ColumnKind | None:
    """Return the kind when every column matches; else None (mixed columns)."""
    if not kinds:
        return None
    first = kinds[0]
    return first if all(k == first for k in kinds) else None


def envelope_column_kinds(envelope: dict[str, Any], *, ncols: int) -> list[ColumnKind]:
    """Per-column unpack kinds from wire ``column_kinds``."""
    kinds = envelope.get("column_kinds")
    if isinstance(kinds, list) and len(kinds) == ncols:
        return cast("list[ColumnKind]", ["int" if k == "int" else "float" for k in kinds])
    return cast("list[ColumnKind]", ["float"] * ncols)


def envelope_uniform_column_kind(envelope: dict[str, Any], *, ncols: int) -> ColumnKind | None:
    """Decode-only: all-int or all-float fast path when ``column_kinds`` are uniform; None if mixed."""
    return _uniform_column_kind(envelope_column_kinds(envelope, ncols=ncols))


def _host_cell_from_float(val: float, *, column_kind: ColumnKind) -> Any:
    if math.isnan(val):
        return None
    return int(val) if column_kind == "int" else val


def _apply_column_kinds_to_ndarray(
    arr: Any,
    column_kinds: list[ColumnKind],
    *,
    ncols: int,
    is_1d: bool,
    uniform: ColumnKind | None = None,
) -> Any:
    """Cast float64 ndarray columns to int64 where pack declared int (NumPy trusts column metadata)."""
    import numpy as np

    if uniform is None:
        uniform = _uniform_column_kind(column_kinds)
    if uniform == "int":
        return arr.astype(np.int64)
    if uniform == "float":
        return arr
    if is_1d:
        return arr.astype(np.int64) if column_kinds[0] == "int" else arr

    out = arr.copy()
    for c, kind in enumerate(column_kinds):
        if kind == "int":
            out[:, c] = out[:, c].astype(np.int64)
    return out


def describe_wire_value(obj: Any, *, sample: int = 3) -> str:
    """Short summary for debug logs (avoids dumping huge arrays or base64)."""
    if is_split_grid(obj):
        b64 = obj.get("b64") or ""
        strings = obj.get("strings") or {}
        return (
            f"split_grid shape={obj.get('shape')} cells={wire_cell_count(obj)} "
            f"column_kinds={obj.get('column_kinds')} strings={len(strings)} b64_chars={len(b64)}"
        )
    if obj is None:
        return "None"
    if isinstance(obj, (str, int, float, bool)):
        return f"{type(obj).__name__}={obj!r}"
    if isinstance(obj, dict):
        if "__wa_payload__" in obj:
            return f"dict(payload={obj.get('__wa_payload__')!r} keys={list(obj)})"
        keys = list(obj.keys())[:sample]
        return f"dict(keys={keys}{'…' if len(obj) > sample else ''})"
    if isinstance(obj, (list, tuple)):
        n = len(obj)
        if n == 0:
            return "list[]"
        first = obj[0]
        if isinstance(first, (list, tuple)):
            ncols = max((len(r) for r in obj), default=0)
            return f"list[{n}x{ncols}] sample_row={list(first)[:sample]!r}"
        return f"list[{n}] sample={list(obj)[:sample]!r}"
    return f"{type(obj).__name__}={repr(obj)[:120]}"


def cell_count(shape: tuple[int, ...]) -> int:
    n = 1
    for d in shape:
        n *= d
    return n


def should_use_binary_envelope(
    shape: tuple[int, ...],
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> bool:
    """Return True if policy says pack data as split_grid instead of JSON lists."""
    if force == "always":
        return True
    if force == "never":
        return False
    return bool(shape) and cell_count(shape) >= min_cells


def binary_envelope_skip_reason(
    shape: tuple[int, ...],
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> str | None:
    """Human-readable reason split_grid was not used; None if envelope would be used."""
    if should_use_binary_envelope(shape, min_cells=min_cells, force=force):
        return None
    if force == "never":
        return "force=never"
    ncells = cell_count(shape)
    return f"needs cells >= {min_cells} (got {ncells} in shape {shape})"


def is_numeric_coercible(value: Any) -> bool:
    """True if value can become float64 in split_grid (None/empty -> NaN)."""
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return True
        try:
            float(s)
            return True
        except ValueError:
            return False
    return False


def is_numeric_grid(grid: list[Any] | list[list[Any]]) -> bool:
    """True when every cell is numeric-coercible (safe for numeric-only split_grid fast-path)."""
    if not grid:
        return True
    if isinstance(grid[0], (list, tuple)):
        return all(is_numeric_coercible(cell) for row in grid for cell in row)
    return all(is_numeric_coercible(cell) for cell in grid)


def wire_cell_count(data: Any) -> int:
    """Cell count for size limits; works on lists or split_grid envelopes."""
    if is_split_grid(data):
        return cell_count(tuple(int(x) for x in data["shape"]))
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


def grid_from_nested_list(grid: list[Any] | list[list[Any]]) -> list[Any] | list[list[Any]]:
    """Normalize to flat or 2D Python lists (JSON path, no envelope)."""
    if not grid:
        return []
    if isinstance(grid[0], (list, tuple)):
        return [[_cell_for_json(c) for c in row] for row in grid]
    return [_cell_for_json(x) for x in grid]


def _cell_for_json(value: Any) -> Any:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def _flatten_grid_to_components(
    grid: list[Any] | list[list[Any]]
) -> tuple[array.array, dict[str, str], list[ColumnKind], list[int]]:
    """Flatten 1D/2D grid to float64 array, strings dict, column kinds, and shape."""
    if not grid:
        return array.array("d"), {}, [], [0]

    first = grid[0]
    is_2d = isinstance(first, (list, tuple))
    if is_2d:
        nrows = len(grid)
        ncols = max((len(r) for r in grid), default=0)
        shape = [nrows, ncols]
    else:
        nrows = 1
        ncols = len(grid)
        shape = [ncols]

    buf = array.array("d")
    strings: dict[str, str] = {}
    column_kinds = cast("list[ColumnKind]", ["int"] * (ncols if is_2d else 1))

    idx = 0
    # Process rows (or single pseudo-row if 1D)
    rows = grid if is_2d else [grid]
    for row in rows:
        row_len = len(row)
        for c in range(ncols):
            val = row[c] if c < row_len else None
            col_idx = c if is_2d else 0

            if val is None:
                buf.append(math.nan)
                column_kinds[col_idx] = "float"
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                buf.append(float(val))
                if isinstance(val, float):
                    column_kinds[col_idx] = "float"
            elif isinstance(val, bool):
                buf.append(float(val))
            else:
                buf.append(math.nan)
                strings[str(idx)] = val if isinstance(val, str) else str(val)
            idx += 1

    return buf, strings, column_kinds, shape


def host_pack_split_grid(
    grid: list[Any] | list[list[Any]],
    *,
    use_b64: bool | None = None,
) -> dict[str, Any]:
    """Pack a 1D flat list or 2D mixed grid using Strategy 3: Split-Grid Serialization.

    The entire grid is flattened into a single contiguous double-precision float array
    where all numbers are preserved, and empty cells or non-numeric strings are replaced with NaN.
    A separate sparse dictionary mapping flat cell indexes to their string value is passed in parallel.
    """
    if use_b64 is None:
        use_b64 = (SERIALIZATION == "json")

    if not grid:
        empty_envelope: dict[str, Any] = {
            "__wa_payload__": PAYLOAD_SPLIT_GRID,
            "dtype": SPLIT_GRID_WIRE_DTYPE,
            "column_kinds": [],
            "shape": [0],
            "strings": {},
        }
        if use_b64:
            empty_envelope["b64"] = ""
        else:
            empty_envelope["buffer"] = b""
        return empty_envelope

    buf, strings, column_kinds, shape = _flatten_grid_to_components(grid)

    envelope: dict[str, Any] = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "dtype": SPLIT_GRID_WIRE_DTYPE,
        "column_kinds": column_kinds,
        "shape": shape,
        "strings": strings,
    }

    if use_b64:
        envelope["b64"] = base64.b64encode(buf.tobytes()).decode("ascii")
        log.debug(
            "payload_codec host_pack split_grid column_kinds=%s shape=%s cells=%s strings=%s b64_chars=%s",
            column_kinds,
            shape,
            len(buf),
            len(strings),
            len(envelope["b64"]),
        )
    else:
        envelope["buffer"] = buf.tobytes()
        log.debug(
            "payload_codec host_pack split_grid column_kinds=%s shape=%s cells=%s strings=%s raw_bytes=%s",
            column_kinds,
            shape,
            len(buf),
            len(strings),
            len(envelope["buffer"]),
        )

    return envelope


def host_pack_data(
    grid: list[Any] | list[list[Any]],
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
    use_b64: bool | None = None,
) -> Any:
    """Pack ``data`` for worker request field (list or split_grid dict)."""
    try:
        if grid:
            is_2d = isinstance(grid[0], (list, tuple))
            grid_shape: tuple[int, ...] = (len(grid), max((len(r) for r in grid), default=0)) if is_2d else (len(grid),)
            if should_use_binary_envelope(grid_shape, min_cells=min_cells, force=force):
                return host_pack_split_grid(grid, use_b64=use_b64)
        out = grid_from_nested_list(grid)
        log.debug("payload_codec host_pack json_list %s", describe_wire_value(out))
        return out
    except Exception:
        log.exception("payload_codec host_pack failed for grid %s", describe_wire_value(grid))
        raise


def host_unpack_split_grid(envelope: dict[str, Any], *, as_nested_list: bool = True) -> list[Any] | list[list[Any]]:
    """Decode split_grid envelope on host (stdlib only). Reconstructs list or list of lists."""
    buf = array.array("d")
    if "buffer" in envelope:
        buf.frombytes(envelope["buffer"])
    else:
        buf.frombytes(base64.b64decode(envelope.get("b64", "")))
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
            _host_cell_from_float(val, column_kind=column_kinds[0 if is_1d else i % ncols])
            for i, val in enumerate(buf)
        ]

    if not as_nested_list or is_1d:
        return flat_list

    return [flat_list[r * ncols : (r + 1) * ncols] for r in range(nrows)]


def host_unpack_data(wire: Any, *, as_nested_list: bool = True) -> Any:
    """Unpack worker ``data`` or ``result`` on host (list, scalar, or split_grid)."""
    if is_split_grid(wire):
        return host_unpack_split_grid(wire, as_nested_list=as_nested_list)
    return wire


def is_split_grid(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("__wa_payload__") == PAYLOAD_SPLIT_GRID


def child_unpack_split_grid(envelope: dict[str, Any]) -> Any:
    """Decode split_grid envelope in child. Returns ndarray if purely numeric, else nested lists/lists."""
    try:
        shape = envelope["shape"]
        is_1d = len(shape) == 1
        nrows, ncols = (shape[0], 1) if is_1d else (shape[0], shape[1])

        import numpy as np

        if "buffer" in envelope:
            raw = envelope["buffer"]
        else:
            raw = base64.b64decode(envelope.get("b64", ""))
        uniform = envelope_uniform_column_kind(envelope, ncols=ncols)
        column_kinds = envelope_column_kinds(envelope, ncols=ncols)
        strings = envelope.get("strings", {})

        if not strings:
            arr = np.frombuffer(raw, dtype=np.float64)
            if not is_1d:
                arr = arr.reshape((nrows, ncols))
            arr = _apply_column_kinds_to_ndarray(
                arr, column_kinds, ncols=ncols, is_1d=is_1d, uniform=uniform
            )
            log.debug("payload_codec child_unpack split_grid optimized -> ndarray shape=%s dtype=%s", arr.shape, arr.dtype)
            return arr

        # Path for mixed-type grids with strings
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
    except Exception:
        log.exception("payload_codec child_unpack split_grid failed for envelope %s", describe_wire_value(envelope))
        raise



def child_unpack_data(wire: Any) -> Any:
    """Materialize worker ``data`` in venv (ndarray/list from split_grid, or np.array from numeric list)."""
    try:
        if is_split_grid(wire):
            return child_unpack_split_grid(wire)
        if isinstance(wire, (list, tuple)):
            grid: list[Any] | list[list[Any]]
            if wire and isinstance(wire[0], (list, tuple)):
                grid = [list(row) for row in wire]
            else:
                grid = list(wire)
            if is_numeric_grid(grid):
                import numpy as np

                arr = np.array(grid, dtype=np.float64)
                log.debug(
                    "payload_codec child_unpack json_list -> ndarray shape=%s",
                    arr.shape,
                )
                return arr
            log.debug("payload_codec child_unpack json_list as-is %s", describe_wire_value(wire))
        return wire
    except Exception:
        log.exception(
            "payload_codec child_unpack failed for wire %s",
            describe_wire_value(wire),
        )
        raise


def child_pack_split_grid(arr: Any, *, use_b64: bool | None = None) -> dict[str, Any]:
    """Pack ndarray as split_grid for JSON wire (venv). Numeric lane is always float64 bytes."""
    import numpy as np

    if use_b64 is None:
        use_b64 = (SERIALIZATION == "json")

    try:
        if not isinstance(arr, np.ndarray):
            arr = np.asarray(arr)
        ncols = int(arr.shape[1]) if arr.ndim == 2 else 1
        if np.issubdtype(arr.dtype, np.integer):
            column_kinds = cast("list[ColumnKind]", ["int"] * ncols)
        else:
            column_kinds = cast("list[ColumnKind]", ["float"] * ncols)
        wire_arr = np.ascontiguousarray(arr, dtype=np.float64)
        envelope: dict[str, Any] = {
            "__wa_payload__": PAYLOAD_SPLIT_GRID,
            "dtype": SPLIT_GRID_WIRE_DTYPE,
            "column_kinds": column_kinds,
            "shape": list(wire_arr.shape),
            "strings": {},
        }
        if use_b64:
            envelope["b64"] = base64.b64encode(wire_arr.tobytes()).decode("ascii")
            log.debug(
                "payload_codec child_pack split_grid column_kinds=%s shape=%s cells=%s b64_chars=%s",
                column_kinds,
                wire_arr.shape,
                wire_arr.size,
                len(envelope["b64"]),
            )
        else:
            envelope["buffer"] = wire_arr.tobytes()
            log.debug(
                "payload_codec child_pack split_grid column_kinds=%s shape=%s cells=%s raw_bytes=%s",
                column_kinds,
                wire_arr.shape,
                wire_arr.size,
                len(envelope["buffer"]),
            )
        return envelope
    except Exception:
        log.exception(
            "payload_codec child_pack split_grid failed for value %s",
            describe_wire_value(arr),
        )
        raise


def child_pack_result(
    result: Any,
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
    use_b64: bool | None = None,
) -> Any:
    """JSON-safe worker result: scalar/list as-is, ndarray as list or split_grid."""
    import numpy as np

    if use_b64 is None:
        use_b64 = (SERIALIZATION == "json")

    try:
        if isinstance(result, np.ndarray):
            shape = tuple(int(x) for x in result.shape)
            if should_use_binary_envelope(shape, min_cells=min_cells, force=force):
                return child_pack_split_grid(result, use_b64=use_b64)
            log.debug(
                "payload_codec child_pack json_list egress ndarray shape=%s (below_threshold)",
                shape,
            )
            return result.tolist()
        if isinstance(result, (np.integer,)):
            return int(result)
        if isinstance(result, (np.floating,)):
            return float(result)
        if isinstance(result, np.bool_):
            return bool(result)
        if isinstance(result, (list, tuple)):
            if result and isinstance(result[0], (list, tuple)):
                grid = [list(row) for row in result]
                grid_shape: tuple[int, ...] = (len(grid), max((len(r) for r in grid), default=0))
            else:
                grid = list(result)
                grid_shape = (len(grid),)
            if should_use_binary_envelope(grid_shape, min_cells=min_cells, force=force):
                return host_pack_split_grid(grid, use_b64=use_b64)
            out = grid_from_nested_list(grid)
            log.debug("payload_codec child_pack json_list egress %s", describe_wire_value(out))
            return out
        return result
    except Exception:
        log.exception(
            "payload_codec child_pack_result failed for value %s",
            describe_wire_value(result),
        )
        raise



