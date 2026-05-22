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
from typing import Any, Literal

log = logging.getLogger(__name__)

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


def _iter_grid_cells(grid: list[Any] | list[list[Any]]):
    if not grid:
        return
    if isinstance(grid[0], (list, tuple)):
        for row in grid:
            for val in row:
                yield val
    else:
        for val in grid:
            yield val


def _cell_forces_float_column(val: Any) -> bool:
    """True when this column must use the float64 lane (floats and empty/None → NaN)."""
    return isinstance(val, float) or val is None


def column_kinds_for_grid(grid: list[Any] | list[list[Any]]) -> list[ColumnKind]:
    """Policy helper (tests): per-column int/float from source types; mirrors host_pack_split_grid."""
    if not grid:
        return []
    if isinstance(grid[0], (list, tuple)):
        ncols = max((len(r) for r in grid), default=0)
        kinds: list[ColumnKind] = ["int"] * ncols
        for row in grid:
            for c in range(ncols):
                val = row[c] if c < len(row) else None
                if _cell_forces_float_column(val):
                    kinds[c] = "float"
        return kinds
    kinds_1d: list[ColumnKind] = ["int"]
    for val in grid:
        if _cell_forces_float_column(val):
            kinds_1d[0] = "float"
    return kinds_1d


def _uniform_column_kind(kinds: list[ColumnKind]) -> ColumnKind | None:
    """Return the kind when every column matches; else None (mixed columns)."""
    if not kinds:
        return None
    first = kinds[0]
    if all(k == first for k in kinds):
        return first
    return None


def envelope_column_kinds(envelope: dict[str, Any], *, ncols: int) -> list[ColumnKind]:
    """Per-column unpack kinds from wire ``column_kinds``."""
    kinds = envelope.get("column_kinds")
    if isinstance(kinds, list) and len(kinds) == ncols:
        out: list[ColumnKind] = []
        for k in kinds:
            out.append("int" if k == "int" else "float")
        return out
    return ["float"] * ncols


def envelope_uniform_column_kind(envelope: dict[str, Any], *, ncols: int) -> ColumnKind | None:
    """Decode-only: all-int or all-float fast path when ``column_kinds`` are uniform; None if mixed."""
    return _uniform_column_kind(envelope_column_kinds(envelope, ncols=ncols))


def _host_cell_from_float(val: float, *, column_kind: ColumnKind) -> Any:
    if math.isnan(val):
        return None
    if column_kind == "int":
        return int(val)
    return val


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
        if column_kinds[0] == "int":
            return arr.astype(np.int64)
        return arr
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
    if not shape:
        return False
    return cell_count(shape) >= min_cells


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
    if value is None:
        return True
    if isinstance(value, (bool, int, float)):
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
        for row in grid:
            for cell in row:
                if not is_numeric_coercible(cell):
                    return False
        return True
    for cell in grid:
        if not is_numeric_coercible(cell):
            return False
    return True


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


def _coerce_float(value: Any) -> float:
    if value is None:
        return math.nan
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value
    if isinstance(value, str) and value.strip() == "":
        return math.nan
    return float(value)


def flatten_numeric_grid(grid: list[Any] | list[list[Any]]) -> tuple[tuple[int, ...], array.array]:
    """Row-major float64 bytes for a 1D flat list or 2D nested list (host, stdlib only)."""
    if not grid:
        return (0,), array.array("d")
    if isinstance(grid[0], (list, tuple)):
        rows = grid
        nrows = len(rows)
        ncols = max((len(r) for r in rows), default=0)
        buf = array.array("d")
        for row in rows:
            for c in range(ncols):
                if c < len(row):
                    buf.append(_coerce_float(row[c]))
                else:
                    buf.append(math.nan)
        return (nrows, ncols), buf
    buf = array.array("d", (_coerce_float(x) for x in grid))
    return (len(buf),), buf


def grid_from_nested_list(grid: list[Any] | list[list[Any]]) -> list[Any] | list[list[Any]]:
    """Normalize to flat or 2D Python lists (JSON path, no envelope)."""
    if not grid:
        return []
    if isinstance(grid[0], (list, tuple)):
        return [[_cell_for_json(c) for c in row] for row in grid]
    return [_cell_for_json(x) for x in grid]


def _cell_for_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def host_pack_split_grid(grid: list[Any] | list[list[Any]]) -> dict[str, Any]:
    """Pack a 1D flat list or 2D mixed grid using Strategy 3: Split-Grid Serialization.
    
    The entire grid is flattened into a single contiguous double-precision float array
    where all numbers are preserved, and empty cells or non-numeric strings are replaced with NaN.
    A separate sparse dictionary mapping flat cell indexes to their string value is passed in parallel.
    
    NOTE TO DEVELOPERS FOR STRATEGY 2 (SQLite database option):
    ----------------------------------------------------------
    An alternative optimization strategy for mixed-type grids (Strategy 2) is to use local 
    SQLite temp files instead of parsing/re-assembling individual columns over the JSON line pipe.
    
    Why it could be better:
      1. Zero JSON parsing/base64 overhead on stdout/stdin lines.
      2. Native, high-performance database C-engine (sqlite3) handles cell coercions and types 
         (INTEGER, REAL, TEXT, NULL) in a fraction of a millisecond.
      3. No non-stdlib dependency on the LibreOffice host (sqlite3 is bundled in Python's stdlib!).
      
    How to implement Strategy 2:
      1. On the LO host:
         ```python
         import tempfile, sqlite3
         fd, db_path = tempfile.mkstemp(suffix=".db")
         conn = sqlite3.connect(db_path)
         cur = conn.cursor()
         # Create a table calc_data with columns dynamically typed or simple TEXT/REAL
         cur.execute("CREATE TABLE calc_data (col_0, col_1, ...)")
         cur.executemany("INSERT INTO calc_data VALUES (?, ?, ...)", grid)
         conn.commit()
         conn.close()
         ```
      2. Pass `"__wa_payload__": "sqlite_db"` and `"path": db_path` over the JSON pipe.
      3. In the child (venv):
         ```python
         import sqlite3
         conn = sqlite3.connect(db_path)
         # Reconstruct list of lists or DataFrame directly:
         cur = conn.cursor()
         cur.execute("SELECT * FROM calc_data")
         grid = [list(r) for r in cur.fetchall()]
         ```
      4. Ensure database cleanup on worker request completion or process shutdown/timeout.
      
    How to performance test Strategy 2:
      1. Measure round-trip time for grids ranging from 1,000 to 250,000 cells.
      2. Profiler checklist:
         - Time spent writing the SQLite DB on host (Leg A) vs. list transposition in Strategy 1.
         - String payload size / base64 decode time vs. file I/O latency.
         - Child reconstruction time (rebuilding nested lists from SQL cursor vs transposing lists).
         - Measure disk write overhead vs base64 pipe serialization on slow drives vs RAM disks.
     """
    if not grid:
        return {
            "__wa_payload__": PAYLOAD_SPLIT_GRID,
            "dtype": SPLIT_GRID_WIRE_DTYPE,
            "column_kinds": [],
            "shape": [0],
            "b64": "",
            "strings": {},
        }

    buf = array.array("d")
    strings: dict[str, str] = {}
    is_2d = isinstance(grid[0], (list, tuple))

    if is_2d:
        nrows = len(grid)
        ncols = max((len(r) for r in grid), default=0)
        column_kinds: list[ColumnKind] = ["int"] * ncols
        idx = 0
        for r in range(nrows):
            row = grid[r]
            row_len = len(row)
            for c in range(ncols):
                val = row[c] if c < row_len else None
                if _cell_forces_float_column(val):
                    column_kinds[c] = "float"
                if val is None:
                    buf.append(math.nan)
                elif isinstance(val, bool):
                    buf.append(float(int(val)))
                elif isinstance(val, (int, float)):
                    buf.append(float(val))
                elif isinstance(val, str):
                    buf.append(math.nan)
                    strings[str(idx)] = val
                else:
                    buf.append(math.nan)
                    strings[str(idx)] = str(val)
                idx += 1
        shape = [nrows, ncols]
    else:
        column_kinds = ["int"]
        for idx, val in enumerate(grid):
            if _cell_forces_float_column(val):
                column_kinds[0] = "float"
            if val is None:
                buf.append(math.nan)
            elif isinstance(val, bool):
                buf.append(float(int(val)))
            elif isinstance(val, (int, float)):
                buf.append(float(val))
            elif isinstance(val, str):
                buf.append(math.nan)
                strings[str(idx)] = val
            else:
                buf.append(math.nan)
                strings[str(idx)] = str(val)
        shape = [len(grid)]

    envelope = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "dtype": SPLIT_GRID_WIRE_DTYPE,
        "column_kinds": column_kinds,
        "shape": shape,
        "b64": base64.b64encode(buf.tobytes()).decode("ascii"),
        "strings": strings,
    }

    log.debug(
        "payload_codec host_pack split_grid column_kinds=%s shape=%s cells=%s strings=%s b64_chars=%s",
        column_kinds,
        shape,
        len(buf),
        len(strings),
        len(envelope["b64"]),
    )
    return envelope


def host_pack_data(
    grid: list[Any] | list[list[Any]],
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> Any:
    """Pack ``data`` for worker request field (list or split_grid dict)."""
    try:
        if grid:
            is_2d = isinstance(grid[0], (list, tuple))
            if is_2d:
                grid_shape: tuple[int, ...] = (len(grid), max((len(r) for r in grid), default=0))
            else:
                grid_shape = (len(grid),)
            if should_use_binary_envelope(grid_shape, min_cells=min_cells, force=force):
                return host_pack_split_grid(grid)
        out = grid_from_nested_list(grid)
        log.debug(
            "payload_codec host_pack json_list %s",
            describe_wire_value(out),
        )
        return out
    except Exception:
        log.exception(
            "payload_codec host_pack failed for grid %s",
            describe_wire_value(grid),
        )
        raise


def host_unpack_split_grid(envelope: dict[str, Any], *, as_nested_list: bool = True) -> list[Any] | list[list[Any]]:
    """Decode split_grid envelope on host (stdlib only). Reconstructs list or list of lists."""
    raw = base64.b64decode(envelope["b64"])
    buf = array.array("d")
    buf.frombytes(raw)
    shape = envelope["shape"]
    if len(shape) == 1:
        nrows = shape[0]
        ncols = 1
        is_1d = True
    else:
        nrows, ncols = shape[0], shape[1]
        is_1d = False

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
        flat_list = []
        for i in range(len(buf)):
            val = buf[i]
            str_idx = str(i)
            col = 0 if is_1d else i % ncols
            if str_idx in strings:
                flat_list.append(strings[str_idx])
            else:
                flat_list.append(_host_cell_from_float(val, column_kind=column_kinds[col]))

    if not as_nested_list or is_1d:
        return flat_list
        
    rows = []
    for r in range(nrows):
        rows.append(flat_list[r * ncols : (r + 1) * ncols])
    return rows



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
        if len(shape) == 1:
            nrows = shape[0]
            ncols = 1
            is_1d = True
        else:
            nrows, ncols = shape[0], shape[1]
            is_1d = False
            
        import numpy as np
        import math
        
        raw = base64.b64decode(envelope["b64"])
        uniform = envelope_uniform_column_kind(envelope, ncols=ncols)
        column_kinds = envelope_column_kinds(envelope, ncols=ncols)

        strings = envelope.get("strings", {})
        if not strings:
            if is_1d:
                arr = np.frombuffer(raw, dtype=np.float64)
            else:
                arr = np.frombuffer(raw, dtype=np.float64).reshape((nrows, ncols))
            arr = _apply_column_kinds_to_ndarray(
                arr, column_kinds, ncols=ncols, is_1d=is_1d, uniform=uniform
            )
            log.debug(
                "payload_codec child_unpack split_grid optimized -> ndarray shape=%s dtype=%s uniform=%s",
                arr.shape,
                arr.dtype,
                uniform,
            )
            return arr

        arr = np.frombuffer(raw, dtype=np.float64)
        flat_list = arr.tolist()

        if uniform is not None and not strings:
            for i in range(len(flat_list)):
                val = flat_list[i]
                if math.isnan(val):
                    flat_list[i] = None
                elif uniform == "int":
                    flat_list[i] = int(val)
        else:
            for i in range(len(flat_list)):
                val = flat_list[i]
                str_idx = str(i)
                col = 0 if is_1d else i % ncols
                if str_idx in strings:
                    flat_list[i] = strings[str_idx]
                elif math.isnan(val):
                    flat_list[i] = None
                elif column_kinds[col] == "int":
                    flat_list[i] = int(val)

        if is_1d:
            log.debug(
                "payload_codec child_unpack split_grid reconstructed 1D list size=%s strings=%s",
                len(flat_list),
                len(strings),
            )
            return flat_list
            
        grid = []
        for r in range(nrows):
            grid.append(flat_list[r * ncols : (r + 1) * ncols])
            
        log.debug(
            "payload_codec child_unpack split_grid reconstructed shape=[%s, %s] strings=%s",
            nrows,
            ncols,
            len(strings),
        )
        return grid
    except Exception:
        log.exception(
            "payload_codec child_unpack split_grid failed for envelope %s",
            describe_wire_value(envelope),
        )
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


def child_pack_split_grid(arr: Any) -> dict[str, Any]:
    """Pack ndarray as split_grid for JSON wire (venv). Numeric lane is always float64 bytes."""
    import numpy as np

    try:
        if not isinstance(arr, np.ndarray):
            arr = np.asarray(arr)
        ncols = int(arr.shape[1]) if arr.ndim == 2 else 1
        if np.issubdtype(arr.dtype, np.integer):
            column_kinds: list[ColumnKind] = ["int"] * ncols
        else:
            column_kinds = ["float"] * ncols
        wire_arr = np.ascontiguousarray(arr, dtype=np.float64)
        envelope = {
            "__wa_payload__": PAYLOAD_SPLIT_GRID,
            "dtype": SPLIT_GRID_WIRE_DTYPE,
            "column_kinds": column_kinds,
            "shape": list(wire_arr.shape),
            "b64": base64.b64encode(wire_arr.tobytes()).decode("ascii"),
            "strings": {},
        }
        log.debug(
            "payload_codec child_pack split_grid column_kinds=%s shape=%s cells=%s b64_chars=%s",
            column_kinds,
            wire_arr.shape,
            wire_arr.size,
            len(envelope["b64"]),
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
            if result and isinstance(result[0], (list, tuple)):
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



