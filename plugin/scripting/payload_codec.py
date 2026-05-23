# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Wire codec for Calc/chat data crossing the LO host (plain Python) and venv (NumPy).

Large 2D grids (numeric or mixed numeric-text) use Strategy 3 ``split_grid``: the entire
grid is serialized as a single contiguous double-precision flat float64 array (stored as raw
binary bytes) plus a parallel sparse integer-keyed strings dictionary. When the strings
dictionary is empty, NumPy in the child process ingests that via C-speed ``frombuffer`` +
``reshape`` — a direct zero-copy memory view over raw buffer bytes without any Python list/loop
transpositions or Base64 decoding overhead.

Adjust thresholds below if product policy changes; bench and production share this module.
"""
from __future__ import annotations

import array
import logging
import math
from typing import Any, Literal, cast

log = logging.getLogger(__name__)

import importlib

deal: Any
try:
    deal = importlib.import_module("deal")
except ImportError:
    class _DummyDeal:
        def __getattr__(self, name: str) -> Any:
            return lambda *args, **kwargs: lambda f: f
    deal = _DummyDeal()

# --- Wire kind (JSON-safe dict tag) -----------------------------------------------

PAYLOAD_SPLIT_GRID = "split_grid"
"""Unified 2D grids: dense numeric flat float64 array and sparse strings dictionary."""

# --- When to use binary envelope (default: at least 10 cells) -----------------------

BINARY_MIN_CELLS = 10
"""Use split_grid when total cell count is at least this (10+ cells)."""

MAX_BENCH_CELLS = 100_000
"""Upper cap for benchmark grids (scripts/bench_serialization.py; production cap is scripting.python_max_data_cells)."""

ForceBinary = str
SPLIT_GRID_WIRE_DTYPE = "float64"
ColumnKind = Literal["int", "float", "bool"]


def column_kinds_for_grid(grid: list[Any] | list[list[Any]]) -> list[ColumnKind]:
    """Policy helper (tests): per-column int/float/bool from source types; mirrors host_pack_split_grid."""
    try:
        _, _, kinds, _ = _flatten_grid_to_components(grid)
        return kinds
    except Exception:
        return []


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
        return cast("list[ColumnKind]", ["int" if k == "int" else ("bool" if k == "bool" else "float") for k in kinds])
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
    if uniform == "bool":
        return arr.astype(np.bool_)
    if uniform == "float":
        return arr
    if is_1d:
        if column_kinds[0] == "int":
            return arr.astype(np.int64)
        if column_kinds[0] == "bool":
            return arr.astype(np.bool_)
        return arr

    # If it's a mixed 2D ndarray, it must remain float64 to hold float columns.
    # Casting individual columns is a no-op (coerced back to float64 on assignment).
    # We can just return the float64 array directly, saving a massive arr.copy() allocation!
    return arr


def describe_wire_value(obj: Any, *, sample: int = 3) -> str:
    """Short summary for debug logs (avoids dumping huge arrays or base64)."""
    if is_split_grid(obj):
        buf = obj.get("buffer") or b""
        strings = obj.get("strings") or {}
        return (
            f"split_grid shape={obj.get('shape')} cells={wire_cell_count(obj)} "
            f"column_kinds={obj.get('column_kinds')} strings={len(strings)} raw_bytes={len(buf)}"
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
    """True when a cell is numeric-only for ``is_numeric_grid`` / ``np.array(list)`` paths.

    Non-empty strings are never coercible here — even ``\"02138\"`` parses as a float — so
    mixed grids stay lists after child split_grid unpack (zip codes and labels preserved).
    Empty strings match Calc empty cells (``None``).
    """
    if value is None or isinstance(value, (bool, int, float)):
        return True
    # Fast direct type inspection for NumPy scalar types on the child side without module-level imports
    tname = type(value).__name__
    if tname.startswith(("int", "float", "bool", "uint")):
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def is_numeric_grid(grid: list[Any] | list[list[Any]]) -> bool:
    """True when every cell is numeric-coercible (safe for numeric-only split_grid fast-path)."""
    if not grid:
        return True
    if type(grid[0]) in (list, tuple):
        return all(is_numeric_coercible(cell) for row in grid for cell in row)
    return all(is_numeric_coercible(cell) for cell in grid)


def wire_cell_count(data: Any) -> int:
    """Cell count for size limits; works on lists or split_grid envelopes."""
    if is_split_grid(data):
        return cell_count(tuple(int(x) for x in data["shape"]))
    if data is None:
        return 0
    if type(data) not in (list, tuple):
        return 1
    if not data:
        return 0
    first = data[0]
    if type(first) in (list, tuple):
        return sum(len(row) for row in data)
    return len(data)


def grid_from_nested_list(grid: list[Any] | list[list[Any]]) -> list[Any] | list[list[Any]]:
    """Normalize to flat or 2D Python lists (JSON path, no envelope)."""
    if not grid:
        return []
    if type(grid[0]) in (list, tuple):
        return [[_cell_for_json(c) for c in row] for row in grid]
    return [_cell_for_json(x) for x in grid]


def _cell_for_json(value: Any) -> Any:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


@deal.pre(lambda grid: type(grid) in (list, tuple))
@deal.post(lambda result: isinstance(result, tuple) and len(result) == 4 and isinstance(result[0], array.array) and isinstance(result[1], dict) and isinstance(result[2], list) and isinstance(result[3], list))
@deal.ensure(lambda grid, result: (not grid) == (len(result[0]) == 0 and result[1] == {} and result[2] == [] and result[3] == [0]))
@deal.ensure(lambda grid, result: all(isinstance(k, int) for k in result[1].keys()))
@deal.ensure(lambda grid, result: len(result[2]) == (0 if not grid else (result[3][1] if len(result[3]) == 2 else 1)))
@deal.raises(ValueError)
def _flatten_grid_to_components(
    grid: list
) -> tuple[array.array, dict[int, str], list[ColumnKind], list[int]]:
    """Flatten 1D/2D grid to float64 array, strings dict, column kinds, and shape."""
    if not grid:
        return array.array("d"), {}, [], [0]

    first = grid[0]
    is_2d = type(first) in (list, tuple)
    if is_2d:
        grid_2d = cast("list[list[Any]]", grid)
        nrows = len(grid_2d)
        ncols = max((len(r) for r in grid_2d), default=0)
        shape = [nrows, ncols]
        row_lens = [len(row) for row in grid_2d]
        if len(set(row_lens)) > 1:
            # Uneven nested-list row lengths should never happen for Calc ranges (rectangular UNO blocks).
            log.error(
                "payload_codec: uneven row lengths %s in 2D grid (expected rectangular data from Calc or tools)",
                row_lens,
            )
            raise ValueError(
                f"Uneven row lengths in data grid: {row_lens} (all rows must have the same width)"
            )
    else:
        nrows = 1
        ncols = len(grid)
        shape = [ncols]

    buf = array.array("d")
    strings: dict[int, str] = {}
    buf_append = buf.append

    # --- Fast path setup -------------------------------------------------
    num_cols = ncols if is_2d else 1
    column_states = [0] * num_cols          # 0=None, 1=bool, 2=int, 3=float
    column_has_none = [False] * num_cols

    def process_cell(val: Any, c: int, idx: int) -> None:
        """Append value and update per-column type state (identity checks first)."""
        if val is None:
            buf_append(math.nan)
            column_has_none[c] = True
        elif val is True or val is False:
            buf_append(float(val))
            if column_states[c] == 0:
                column_states[c] = 1
        elif type(val) is int:
            buf_append(float(val))
            if column_states[c] < 2:
                column_states[c] = 2
        elif type(val) is float:
            buf_append(val)
            column_states[c] = 3
        else:
            t = type(val)
            tname = t.__name__
            if tname.startswith("bool"):
                buf_append(float(cast("Any", val)))
                if column_states[c] == 0:
                    column_states[c] = 1
            elif tname.startswith(("int", "uint")):
                buf_append(float(cast("Any", val)))
                if column_states[c] < 2:
                    column_states[c] = 2
            elif tname.startswith("float"):
                buf_append(float(cast("Any", val)))
                column_states[c] = 3
            else:
                buf_append(math.nan)
                strings[idx] = cast("str", val) if t is str else str(val)

    # --- Main flattening loops -------------------------------------------
    if is_2d:
        grid_2d = cast("list[list[Any]]", grid)
        # After the rectangular validation above, all rows have identical length.
        # Use direct enumeration without per-cell is_2d branching.
        idx = 0
        for row in grid_2d:
            for c, val in enumerate(row):
                process_cell(val, c, idx)
                idx += 1
    else:
        grid_1d = cast("list[Any]", grid)
        for idx, val in enumerate(grid_1d):
            process_cell(val, 0, idx)

    # Map the final column states to ColumnKind strings with single-pass promotions
    column_kinds: list[ColumnKind] = []
    for c in range(num_cols):
        state = column_states[c]
        if state == 3:
            kind: ColumnKind = "float"
        elif state == 1:
            kind = "bool"
        else:
            kind = "int"

        # If purely numeric grid (strings is empty), any column with None must be promoted to "float"
        # to avoid NumPy casting errors on NaN values.
        if not strings and column_has_none[c]:
            kind = "float"

        column_kinds.append(kind)

    return buf, strings, column_kinds, shape


@deal.pre(lambda grid: type(grid) in (list, tuple))
@deal.post(lambda result: isinstance(result, dict))
@deal.ensure(lambda grid, result: result.get("__wa_payload__") == PAYLOAD_SPLIT_GRID and result.get("dtype") == SPLIT_GRID_WIRE_DTYPE and isinstance(result.get("column_kinds"), list) and isinstance(result.get("shape"), list) and isinstance(result.get("strings"), dict) and isinstance(result.get("buffer"), bytes))
@deal.raises(ValueError)
def host_pack_split_grid(
    grid: list,
) -> dict[str, Any]:
    """Pack a 1D flat list or 2D mixed grid using Strategy 3: Split-Grid Serialization.

    The entire grid is flattened into a single contiguous double-precision float array
    where all numbers are preserved, and empty cells or non-numeric strings are replaced with NaN.
    A separate sparse dictionary mapping flat cell indexes to their string value is passed in parallel.
    """
    if not grid:
        return {
            "__wa_payload__": PAYLOAD_SPLIT_GRID,
            "dtype": SPLIT_GRID_WIRE_DTYPE,
            "column_kinds": [],
            "shape": [0],
            "strings": {},
            "buffer": b"",
        }

    buf, strings, column_kinds, shape = _flatten_grid_to_components(grid)

    envelope: dict[str, Any] = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "dtype": SPLIT_GRID_WIRE_DTYPE,
        "column_kinds": column_kinds,
        "shape": shape,
        "strings": strings,
        "buffer": buf.tobytes(),
    }

    log.debug(
        "payload_codec host_pack split_grid column_kinds=%s shape=%s cells=%s strings=%s raw_bytes=%s",
        column_kinds,
        shape,
        len(buf),
        len(strings),
        len(envelope["buffer"]),
    )

    return envelope


@deal.pre(lambda grid, min_cells=BINARY_MIN_CELLS, force="auto": type(grid) in (list, tuple))
@deal.post(lambda result: (
    isinstance(result, list) or
    (isinstance(result, dict) and result.get("__wa_payload__") == PAYLOAD_SPLIT_GRID)
))
@deal.raises(ValueError)
def host_pack_data(
    grid: list,
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> Any:
    """Pack ``data`` for worker request field (list or split_grid dict)."""
    try:
        if grid:
            is_2d = type(grid[0]) in (list, tuple)
            grid_shape: tuple[int, ...] = (len(grid), max((len(r) for r in grid), default=0)) if is_2d else (len(grid),)
            if should_use_binary_envelope(grid_shape, min_cells=min_cells, force=force):
                return host_pack_split_grid(grid)
        out = grid_from_nested_list(grid)
        log.debug("payload_codec host_pack json_list %s", describe_wire_value(out))
        return out
    except Exception:
        log.exception("payload_codec host_pack failed for grid %s", describe_wire_value(grid))
        raise


@deal.pre(lambda envelope, as_nested_list=True: (
    type(envelope) is dict and
    envelope.get("__wa_payload__") == PAYLOAD_SPLIT_GRID and
    (isinstance(envelope.get("buffer"), bytes) or isinstance(envelope.get("b64"), str)) and
    isinstance(envelope.get("shape"), list)
))
@deal.post(lambda result: isinstance(result, list))
def host_unpack_split_grid(envelope: dict[str, Any], *, as_nested_list: bool = True) -> list[Any] | list[list[Any]]:
    """Decode split_grid envelope on host (stdlib only). Reconstructs list or list of lists."""
    buf = array.array("d")
    if "buffer" in envelope:
        buf.frombytes(envelope["buffer"])
    elif "b64" in envelope:
        import base64
        buf.frombytes(base64.b64decode(envelope["b64"].encode("ascii")))
    else:
        raise ValueError("Missing payload binary buffer or b64 representation")
    shape = envelope["shape"]
    is_1d = len(shape) == 1
    nrows, ncols = (shape[0], 1) if is_1d else (shape[0], shape[1])

    # Convert keys of strings to integers in case standard JSON/Base64 test harness sent stringified keys
    raw_strings = envelope.get("strings", {})
    strings = {int(k): v for k, v in raw_strings.items()} if raw_strings else {}
    uniform = envelope_uniform_column_kind(envelope, ncols=ncols)

    flat_list: list[Any]
    if not strings and uniform is not None:
        if uniform == "int":
            flat_list = [None if math.isnan(v) else int(v) for v in buf]
        elif uniform == "bool":
            flat_list = [None if math.isnan(v) else (v == 1.0) for v in buf]
        else:
            flat_list = [None if math.isnan(v) else v for v in buf]
    else:
        column_kinds = envelope_column_kinds(envelope, ncols=ncols)
        col_kind = [column_kinds[0 if is_1d else i % ncols] for i in range(len(buf))]
        flat_list = [
            strings[i] if i in strings else 
            (None if math.isnan(val) else (
                True if col_kind[i] == "bool" and val == 1.0 else
                False if col_kind[i] == "bool" and val == 0.0 else
                int(val) if col_kind[i] == "int" else val
            ))
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
    return (
        isinstance(obj, dict) and
        obj.get("__wa_payload__") == PAYLOAD_SPLIT_GRID and
        (isinstance(obj.get("buffer"), bytes) or isinstance(obj.get("b64"), str)) and
        isinstance(obj.get("shape"), list)
    )


@deal.pre(lambda envelope: (
    type(envelope) is dict and
    envelope.get("__wa_payload__") == PAYLOAD_SPLIT_GRID and
    (isinstance(envelope.get("buffer"), bytes) or isinstance(envelope.get("b64"), str)) and
    isinstance(envelope.get("shape"), list)
))
@deal.post(lambda result: result is not None)
@deal.raises(ValueError, TypeError, AttributeError)
def child_unpack_split_grid(envelope: dict[str, Any]) -> Any:
    """Decode split_grid envelope in child. Returns ndarray if purely numeric, else nested lists/lists."""
    try:
        shape = envelope["shape"]
        is_1d = len(shape) == 1
        nrows, ncols = (shape[0], 1) if is_1d else (shape[0], shape[1])

        import numpy as np

        if "buffer" in envelope:
            raw = envelope["buffer"]
        elif "b64" in envelope:
            import base64
            raw = base64.b64decode(envelope["b64"].encode("ascii"))
        else:
            raise ValueError("Missing payload binary buffer or b64 representation")
        uniform = envelope_uniform_column_kind(envelope, ncols=ncols)
        column_kinds = envelope_column_kinds(envelope, ncols=ncols)
        raw_strings = envelope.get("strings", {})
        strings = {int(k): v for k, v in raw_strings.items()} if raw_strings else {}

        if not strings:
            arr = np.frombuffer(raw, dtype=np.float64)
            if not is_1d:
                arr = arr.reshape((nrows, ncols))
            arr = _apply_column_kinds_to_ndarray(
                arr, column_kinds, ncols=ncols, is_1d=is_1d, uniform=uniform
            )
            log.debug("payload_codec child_unpack split_grid optimized -> ndarray shape=%s dtype=%s", arr.shape, arr.dtype)
            return arr

        # Path for mixed-type grids with strings: Vectorized Object-Masking Strategy.
        #
        # --- Why this Vectorized Object-Masking Strategy? ---
        # Homogeneous numeric arrays (float64) cannot natively store Python 'None' or
        # string types. Converting the array to an 'object' type array at C-speed
        # (arr.astype(object)) allows holding arbitrary Python types. We then use
        # vectorized boolean masks to perform C-level bulk modifications, bypassing
        # slow cell-by-cell loops, modulo operations, and manual type-coercion in Python.
        arr = np.frombuffer(raw, dtype=np.float64)
        if not is_1d:
            arr = arr.reshape((nrows, ncols))

        # 1. Bulk-replace NaN values with None using a C-level boolean mask
        nan_mask = np.isnan(arr)
        obj_arr = arr.astype(object)
        obj_arr[nan_mask] = None

        # 2. Vectorized Column-Wise Casting
        # Rather than checking index-level column types inside the main cell iteration
        # (which requires modulo index maths 'i % ncols'), we iterate once per column.
        # We then cast only the valid (non-None) elements in that column at C-speed.
        col_is_int = [k == "int" for k in column_kinds]
        col_is_bool = [k == "bool" for k in column_kinds]
        if any(col_is_int) or any(col_is_bool):
            for c, (is_int, is_bool) in enumerate(zip(col_is_int, col_is_bool)):
                col_slice = obj_arr[:, c] if not is_1d else obj_arr
                col_nan_mask = nan_mask[:, c] if not is_1d else nan_mask
                valid_mask = ~col_nan_mask
                if is_int:
                    # Vectorized astype(int) casts valid float objects to Python ints in C
                    col_slice[valid_mask] = col_slice[valid_mask].astype(int)
                elif is_bool:
                    # Vectorized astype(bool) casts valid float objects to Python bools in C
                    col_slice[valid_mask] = col_slice[valid_mask].astype(bool)

        # 3. Sparse Strings Overlay
        # The 'strings' dictionary is sparse and indexes values row-major (flat 1D).
        # We get a flat 1D view of the object array (zero-copy ravel) to execute
        # the direct, low-overhead string insertions without coordinate math.
        if strings:
            flat_obj = obj_arr.ravel()
            for idx, val in strings.items():
                flat_obj[idx] = val

        # 4. Instant 2D list materialization at C-speed
        return obj_arr.tolist()
    except Exception:
        log.exception("payload_codec child_unpack split_grid failed for envelope %s", describe_wire_value(envelope))
        raise


@deal.pre(lambda wire: (
    (not isinstance(wire, list) or type(wire) is list) and
    (not isinstance(wire, tuple) or type(wire) is tuple)
))
@deal.post(lambda result: result is not None)
@deal.raises(ValueError, TypeError, AttributeError)
def child_unpack_data(wire: Any) -> Any:
    """Materialize worker ``data`` in venv (ndarray/list from split_grid, or np.array from numeric list)."""
    try:
        unpacked = child_unpack_split_grid(wire) if is_split_grid(wire) else wire

        # Automatically unpack single-cell or single-entry inputs into their scalar representation
        import numpy as np
        if isinstance(unpacked, np.ndarray):
            if unpacked.size == 1:
                val = unpacked.item()
                if isinstance(val, float) and val.is_integer():
                    return int(val)
                return val
        elif isinstance(unpacked, (list, tuple)):
            if len(unpacked) == 1 and not (type(unpacked[0]) in (list, tuple)):
                val = unpacked[0]
                if isinstance(val, float) and val.is_integer():
                    return int(val)
                return val

            grid: list[Any] | list[list[Any]]
            if unpacked and (type(unpacked[0]) in (list, tuple)):
                grid = [list(row) for row in unpacked]
            else:
                grid = list(unpacked)
            if is_numeric_grid(grid):
                arr = np.array(grid, dtype=np.float64)
                log.debug(
                    "payload_codec child_unpack json_list -> ndarray shape=%s",
                    arr.shape,
                )
                return arr
            log.debug("payload_codec child_unpack json_list as-is %s", describe_wire_value(unpacked))
            return grid
        return unpacked
    except Exception:
        log.exception(
            "payload_codec child_unpack failed for wire %s",
            describe_wire_value(wire),
        )
        raise


@deal.pre(lambda arr: type(arr).__name__ == "ndarray")
@deal.post(lambda result: isinstance(result, dict) and result.get("__wa_payload__") == PAYLOAD_SPLIT_GRID and result.get("dtype") == SPLIT_GRID_WIRE_DTYPE and isinstance(result.get("column_kinds"), list) and isinstance(result.get("shape"), list) and isinstance(result.get("strings"), dict) and isinstance(result.get("buffer"), bytes))
@deal.raises(ValueError, TypeError, AttributeError)
def child_pack_split_grid(arr: Any) -> dict[str, Any]:
    """Pack ndarray as split_grid for JSON wire (venv). Numeric lane is always float64 bytes."""
    import numpy as np

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
            "buffer": wire_arr.tobytes(),
        }
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


@deal.pre(lambda result, min_cells=BINARY_MIN_CELLS, force="auto": (
    (not isinstance(result, list) or type(result) is list) and
    (not isinstance(result, tuple) or type(result) is tuple)
))
@deal.post(lambda result: result is not None)
@deal.raises(ValueError, TypeError, AttributeError)
def child_pack_result(
    result: Any,
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> Any:
    """JSON-safe worker result: scalar/list as-is, ndarray as list or split_grid."""
    import numpy as np

    try:
        if isinstance(result, np.ndarray):
            shape = tuple(int(x) for x in result.shape)
            if should_use_binary_envelope(shape, min_cells=min_cells, force=force):
                return child_pack_split_grid(result)
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
            if result and (type(result[0]) in (list, tuple)):
                grid = [list(row) for row in result]
                grid_shape: tuple[int, ...] = (len(grid), max((len(r) for r in grid), default=0))
            else:
                grid = list(result)
                grid_shape = (len(grid),)
            if should_use_binary_envelope(grid_shape, min_cells=min_cells, force=force):
                return host_pack_split_grid(grid)
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



