# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Wire codec for Calc/chat data crossing the LO host (plain Python) and venv (NumPy).

Large dense numeric grids use ``f64_blob``: row-major IEEE float64 bytes (base64 in JSON).
NumPy ingests that via ``frombuffer`` + ``reshape`` — a view over decoded bytes, not
per-element Python floats from a JSON list (which forces ``np.array(list)`` to parse
every number from heap objects).

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

PAYLOAD_F64_BLOB = "f64_blob"
"""Dense numeric payload: host packs with stdlib; child unpacks with NumPy frombuffer."""

PAYLOAD_COLUMN_GRID = "column_grid"
"""Mixed type columns: host packs numeric columns as f64_blob and string columns as json_list."""

# --- When to use binary envelope (default: at least 10 cells) -----------------------

BINARY_MIN_CELLS = 10
"""Use f64_blob when total cell count is at least this (10+ cells)."""

MAX_BENCH_CELLS = 10_000
"""Upper cap for benchmark grids (production may use calc_addin MAX_PYTHON_DATA_CELLS)."""

ForceBinary = Literal["auto", "always", "never"]


def describe_wire_value(obj: Any, *, sample: int = 3) -> str:
    """Short summary for debug logs (avoids dumping huge arrays or base64)."""
    if is_f64_blob(obj):
        b64 = obj.get("b64") or ""
        return (
            f"f64_blob shape={obj.get('shape')} dtype={obj.get('dtype')} "
            f"cells={wire_cell_count(obj)} b64_chars={len(b64)}"
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
    """Return True if policy says pack numeric data as f64_blob instead of JSON lists."""
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
    """Human-readable reason f64_blob was not used; None if blob would be used."""
    if should_use_binary_envelope(shape, min_cells=min_cells, force=force):
        return None
    if force == "never":
        return "force=never"
    ncells = cell_count(shape)
    return f"needs cells >= {min_cells} (got {ncells} in shape {shape})"


def is_numeric_coercible(value: Any) -> bool:
    """True if value can become float64 in f64_blob (None/empty -> NaN)."""
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
    """True when every cell is numeric-coercible (safe for f64_blob pack)."""
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
    """Cell count for size limits; works on lists or f64_blob envelopes."""
    if is_f64_blob(data):
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


def host_pack_f64_blob(grid: list[Any] | list[list[Any]]) -> dict[str, Any]:
    """Build f64_blob envelope from numeric grid (LibreOffice host — no NumPy)."""
    shape, buf = flatten_numeric_grid(grid)
    envelope = {
        "__wa_payload__": PAYLOAD_F64_BLOB,
        "dtype": "float64",
        "shape": list(shape),
        "b64": base64.b64encode(buf.tobytes()).decode("ascii"),
    }
    log.debug(
        "payload_codec host_pack f64_blob ingress shape=%s cells=%s b64_chars=%s",
        shape,
        cell_count(shape),
        len(envelope["b64"]),
    )
    return envelope


def host_pack_column_grid(grid: list[list[Any]]) -> dict[str, Any]:
    """Pack a 2D mixed grid column-by-column to optimize numeric columns (LibreOffice host).
    
    This splits a 2D grid of mixed text/numbers into individual columns. Numeric-only columns 
    are serialized using the fast binary `f64_blob` envelope, while mixed/text columns stay as 
    regular JSON lists. Reconstructed in the child namespace to a standard nested list of lists.
    
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
      3. Measure disk write overhead vs base64 pipe serialization on slow drives vs RAM disks.
    """
    nrows = len(grid)
    ncols = max((len(r) for r in grid), default=0)
    
    columns_payload = []
    for c in range(ncols):
        col_cells = []
        for r in range(nrows):
            if c < len(grid[r]):
                col_cells.append(grid[r][c])
            else:
                col_cells.append(None)
                
        # If the column has cells, and is purely numeric-coercible, pack it as binary
        if is_numeric_grid(col_cells):
            shape, buf = flatten_numeric_grid(col_cells)
            columns_payload.append({
                "type": PAYLOAD_F64_BLOB,
                "dtype": "float64",
                "shape": list(shape),
                "b64": base64.b64encode(buf.tobytes()).decode("ascii"),
            })
        else:
            columns_payload.append({
                "type": "json_list",
                "data": grid_from_nested_list(col_cells),
            })
            
    log.debug(
        "payload_codec host_pack column_grid ingress shape=%s columns=%s",
        [nrows, ncols],
        len(columns_payload),
    )
    return {
        "__wa_payload__": PAYLOAD_COLUMN_GRID,
        "shape": [nrows, ncols],
        "columns": columns_payload,
    }


def host_pack_data(
    grid: list[Any] | list[list[Any]],
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> Any:
    """Pack ``data`` for worker request field (list or f64_blob dict)."""
    try:
        if not is_numeric_grid(grid):
            # It's a mixed grid. Let's see if we can pack it column-wise!
            # It must be a 2D list of lists, having length > 1 and max length of inner lists > 1
            if (
                grid 
                and isinstance(grid[0], (list, tuple)) 
                and len(grid) > 1 
                and max((len(r) for r in grid), default=0) > 1
            ):
                shape = (len(grid), max((len(r) for r in grid), default=0))
                if should_use_binary_envelope(shape, min_cells=min_cells, force=force):
                    return host_pack_column_grid(grid)
            out = grid_from_nested_list(grid)
            log.debug(
                "payload_codec host_pack json_list (non_numeric_grid) %s",
                describe_wire_value(out),
            )
            return out
        shape, _ = flatten_numeric_grid(grid)
        if should_use_binary_envelope(shape, min_cells=min_cells, force=force):
            return host_pack_f64_blob(grid)
        out = grid_from_nested_list(grid)
        skip = binary_envelope_skip_reason(shape, min_cells=min_cells, force=force)
        log.debug(
            "payload_codec host_pack json_list (below_threshold shape=%s cells=%s force=%s reason=%s) %s",
            shape,
            cell_count(shape),
            force,
            skip,
            describe_wire_value(out),
        )
        return out
    except Exception:
        log.exception(
            "payload_codec host_pack failed for grid %s",
            describe_wire_value(grid),
        )
        raise


def host_unpack_f64_blob(envelope: dict[str, Any], *, as_nested_list: bool = True) -> list[Any] | list[list[Any]]:
    """Decode f64_blob on host (stdlib). Optionally expand to nested lists for Calc tools."""
    raw = base64.b64decode(envelope["b64"])
    buf = array.array("d")
    buf.frombytes(raw)
    shape = tuple(int(x) for x in envelope["shape"])
    if len(shape) == 1:
        flat = [_cell_for_json(x) for x in buf]
        return flat
    nrows, ncols = shape[0], shape[1]
    if not as_nested_list:
        return list(buf)
    rows: list[list[Any]] = []
    idx = 0
    for _ in range(nrows):
        row = []
        for _ in range(ncols):
            row.append(_cell_for_json(buf[idx]))
            idx += 1
        rows.append(row)
    return rows


def host_unpack_data(wire: Any, *, as_nested_list: bool = True) -> Any:
    """Unpack worker ``data`` or ``result`` on host (list, scalar, or f64_blob)."""
    if isinstance(wire, dict) and wire.get("__wa_payload__") == PAYLOAD_F64_BLOB:
        return host_unpack_f64_blob(wire, as_nested_list=as_nested_list)
    return wire


def is_f64_blob(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("__wa_payload__") == PAYLOAD_F64_BLOB


def is_column_grid(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("__wa_payload__") == PAYLOAD_COLUMN_GRID


def child_unpack_f64_blob(envelope: dict[str, Any]) -> Any:
    """NumPy ndarray view over blob bytes — fast path for dense numeric ingress."""
    import numpy as np

    try:
        raw = base64.b64decode(envelope["b64"])
        dtype = np.dtype(envelope.get("dtype", "float64"))
        shape = tuple(int(x) for x in envelope["shape"])
        arr = np.frombuffer(raw, dtype=dtype).reshape(shape)
        log.debug(
            "payload_codec child_unpack f64_blob -> ndarray shape=%s dtype=%s",
            arr.shape,
            arr.dtype,
        )
        return arr
    except Exception:
        log.exception(
            "payload_codec child_unpack f64_blob failed for envelope %s",
            describe_wire_value(envelope),
        )
        raise


def child_unpack_column_grid(envelope: dict[str, Any]) -> list[list[Any]]:
    """Decode column_grid envelope in child and reconstruct 2D nested list."""
    try:
        nrows, ncols = envelope["shape"]
        columns_payload = envelope["columns"]
        
        # 1. Decode each column to its 1D list
        decoded_columns = []
        for col_payload in columns_payload:
            if col_payload["type"] == PAYLOAD_F64_BLOB:
                import numpy as np
                raw = base64.b64decode(col_payload["b64"])
                dtype = np.dtype(col_payload.get("dtype", "float64"))
                arr = np.frombuffer(raw, dtype=dtype)
                col_list = [None if np.isnan(x) else x for x in arr]
                decoded_columns.append(col_list)
            else:
                decoded_columns.append(col_payload["data"])
                
        # 2. Reconstruct row-major nested list
        grid = []
        for r in range(nrows):
            row = []
            for c in range(ncols):
                row.append(decoded_columns[c][r])
            grid.append(row)
            
        log.debug(
            "payload_codec child_unpack column_grid reconstructed shape=[%s, %s]",
            nrows,
            ncols,
        )
        return grid
    except Exception:
        log.exception(
            "payload_codec child_unpack column_grid failed for envelope %s",
            describe_wire_value(envelope),
        )
        raise


def child_unpack_data(wire: Any) -> Any:
    """Materialize worker ``data`` in venv (ndarray from blob, or np.array from numeric list)."""
    try:
        if is_f64_blob(wire):
            return child_unpack_f64_blob(wire)
        if is_column_grid(wire):
            return child_unpack_column_grid(wire)
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


def child_pack_f64_blob(arr: Any) -> dict[str, Any]:
    """Pack ndarray as f64_blob for JSON wire (venv)."""
    import numpy as np

    try:
        if not isinstance(arr, np.ndarray):
            arr = np.asarray(arr, dtype=np.float64)
        contiguous = np.ascontiguousarray(arr, dtype=np.float64)
        envelope = {
            "__wa_payload__": PAYLOAD_F64_BLOB,
            "dtype": "float64",
            "shape": list(contiguous.shape),
            "b64": base64.b64encode(contiguous.tobytes()).decode("ascii"),
        }
        log.debug(
            "payload_codec child_pack f64_blob egress shape=%s cells=%s b64_chars=%s",
            contiguous.shape,
            contiguous.size,
            len(envelope["b64"]),
        )
        return envelope
    except Exception:
        log.exception(
            "payload_codec child_pack f64_blob failed for value %s",
            describe_wire_value(arr),
        )
        raise


def child_pack_result(
    result: Any,
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> Any:
    """JSON-safe worker result: scalar/list as-is, ndarray as list or f64_blob."""
    import numpy as np

    try:
        if isinstance(result, np.ndarray):
            shape = tuple(int(x) for x in result.shape)
            if should_use_binary_envelope(shape, min_cells=min_cells, force=force):
                return child_pack_f64_blob(result)
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
            else:
                grid = list(result)
            if is_numeric_grid(grid):
                shape, _ = flatten_numeric_grid(grid)
                if should_use_binary_envelope(shape, min_cells=min_cells, force=force):
                    return host_pack_f64_blob(grid)
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


def child_materialize_list(wire: Any) -> Any:
    """Baseline slow path: np.array after json.loads produced Python lists."""
    import numpy as np

    return np.array(wire, dtype=np.float64)


def child_materialize_blob(envelope: dict[str, Any]) -> Any:
    """Fast path: frombuffer + reshape (same as child_unpack_f64_blob)."""
    return child_unpack_f64_blob(envelope)
