
import time
import cProfile
import pstats
import sys
from pathlib import Path
import math
import array
from typing import Any, cast

# Mocking parts of payload_codec
SPLIT_GRID_WIRE_DTYPE = "float64"
PAYLOAD_SPLIT_GRID = "split_grid"

def _flatten_update_column_state(column_states: list[int], c: int, val: Any) -> None:
    st = column_states[c]
    if st == 3: return
    if val is True or val is False:
        if st == 0: column_states[c] = 1
        return
    tv = type(val)
    if tv is float:
        column_states[c] = 3
        return
    if tv is int:
        if st < 2: column_states[c] = 2
        return
    # ... (skipping numpy/other for mock)

def _flatten_append_cell_slow(val, c, idx, buf_append, strings, column_states, column_has_none, nan):
    if val is None:
        buf_append(nan)
        column_has_none[c] = True
    elif val is True or val is False:
        buf_append(float(val))
        if column_states[c] == 0: column_states[c] = 1
    elif type(val) is int:
        buf_append(float(val))
        if column_states[c] < 2: column_states[c] = 2
    elif type(val) is float:
        buf_append(val)
        column_states[c] = 3
    else:
        buf_append(nan)
        strings[idx] = val if type(val) is str else str(val)

def original_flatten(grid):
    first = grid[0]
    is_2d = type(first) in (list, tuple)
    if is_2d:
        grid_2d = grid
        nrows = len(grid_2d)
        ncols = max((len(r) for r in grid_2d), default=0)
        shape = [nrows, ncols]
    else:
        nrows = 1
        ncols = len(grid)
        shape = [ncols]

    buf = array.array("d")
    strings = {}
    buf_append = buf.append
    nan = math.nan

    num_cols = ncols if is_2d else 1
    column_states = [0] * num_cols
    column_has_none = [False] * num_cols
    has_non_numeric = False

    def _append_cell_slow(val: Any, c: int, idx: int) -> None:
        _flatten_append_cell_slow(
            val, c, idx, buf_append=buf_append, strings=strings,
            column_states=column_states, column_has_none=column_has_none, nan=nan,
        )

    if is_2d:
        grid_2d = grid
        idx = 0
        for row in grid_2d:
            for c, val in enumerate(row):
                if type(val) is str:
                    has_non_numeric = True
                    _append_cell_slow(val, c, idx)
                elif not has_non_numeric:
                    try:
                        fval = float(val)
                        buf_append(fval)
                        if column_states[c] != 3:
                            _flatten_update_column_state(column_states, c, val)
                    except (TypeError, ValueError):
                        has_non_numeric = True
                        _append_cell_slow(val, c, idx)
                else:
                    _append_cell_slow(val, c, idx)
                idx += 1
    # ... (1d omitted)
    return buf, strings, column_states, shape

def optimized_flatten(grid):
    if not grid: return array.array("d"), {}, [], [0]
    first = grid[0]
    is_2d = type(first) in (list, tuple)
    
    buf = array.array("d")
    strings = {}
    buf_append = buf.append
    nan = math.nan

    if is_2d:
        grid_2d = grid
        nrows = len(grid_2d)
        ncols = len(grid_2d[0]) if nrows > 0 else 0
        shape = [nrows, ncols]
        column_states = [0] * ncols
        column_has_none = [False] * ncols
        
        idx = 0
        for row in grid_2d:
            if len(row) != ncols: raise ValueError("Jagged")
            for c, val in enumerate(row):
                tval = type(val)
                if tval is float:
                    buf_append(val)
                    if column_states[c] != 3: column_states[c] = 3
                elif tval is int:
                    buf_append(float(val))
                    if column_states[c] < 2: column_states[c] = 2
                elif val is None:
                    buf_append(nan)
                    column_has_none[c] = True
                elif tval is bool:
                    buf_append(float(val))
                    if column_states[c] == 0: column_states[c] = 1
                elif tval is str:
                    buf_append(nan)
                    strings[idx] = val
                else:
                    # Rare types (numpy scalars etc)
                    _flatten_append_cell_slow(val, c, idx, buf_append, strings, column_states, column_has_none, nan)
                idx += 1
    else:
        # 1D ...
        pass
    return buf, strings, column_states, shape

def bench():
    nrows = 20000
    ncols = 5
    grid = [[float(i + j) if (i+j) % 10 != 0 else None for j in range(ncols)] for i in range(nrows)]
    
    print("Original flatten (with Nones)...")
    t0 = time.perf_counter()
    for _ in range(10):
        original_flatten(grid)
    print(f"Time: {(time.perf_counter() - t0)*100:.2f} ms")

    print("Optimized flatten (with Nones)...")
    t0 = time.perf_counter()
    for _ in range(10):
        optimized_flatten(grid)
    print(f"Time: {(time.perf_counter() - t0)*100:.2f} ms")

if __name__ == "__main__":
    bench()
