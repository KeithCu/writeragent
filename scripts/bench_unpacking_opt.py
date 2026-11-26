#!/usr/bin/env python3
# WriterAgent - benchmark different split_grid unpacking strategies in child process.
# Run outside LibreOffice: python scripts/bench_unpacking_opt.py
"""Compare current list-loop unpacking against advanced NumPy vectorized masking."""
from __future__ import annotations

import argparse
import math
import random
import time
import sys
from typing import Any, Callable

try:
    import numpy as np
except ImportError:
    print("NumPy is required to run this benchmark.")
    sys.exit(1)

# Current standard production implementation
def unpack_current(envelope: dict[str, Any]) -> Any:
    shape = envelope["shape"]
    is_1d = len(shape) == 1
    nrows, ncols = (shape[0], 1) if is_1d else (shape[0], shape[1])
    raw = envelope["buffer"]
    column_kinds = envelope.get("column_kinds", ["float"] * ncols)
    raw_strings = envelope.get("strings", {})
    strings = {int(k): v for k, v in raw_strings.items()} if raw_strings else {}

    flat_list = np.frombuffer(raw, dtype=np.float64).tolist()
    col_is_int = [k == "int" for k in column_kinds]
    any_int = any(col_is_int)

    if not any_int:
        for i, val in enumerate(flat_list):
            if i in strings:
                flat_list[i] = strings[i]
            elif math.isnan(val):
                flat_list[i] = None
    else:
        for i, val in enumerate(flat_list):
            if i in strings:
                flat_list[i] = strings[i]
            elif math.isnan(val):
                flat_list[i] = None
            elif col_is_int[0 if is_1d else i % ncols]:
                flat_list[i] = int(val)

    if is_1d:
        return flat_list
    return [flat_list[r * ncols : (r + 1) * ncols] for r in range(nrows)]


# Proposed Loop Optimization with modulo elimination via cycle
def unpack_cycle_opt(envelope: dict[str, Any]) -> Any:
    from itertools import cycle

    shape = envelope["shape"]
    is_1d = len(shape) == 1
    nrows, ncols = (shape[0], 1) if is_1d else (shape[0], shape[1])
    raw = envelope["buffer"]
    column_kinds = envelope.get("column_kinds", ["float"] * ncols)
    raw_strings = envelope.get("strings", {})
    strings = {int(k): v for k, v in raw_strings.items()} if raw_strings else {}

    flat_list = np.frombuffer(raw, dtype=np.float64).tolist()
    col_is_int = [k == "int" for k in column_kinds]
    any_int = any(col_is_int)

    if not any_int:
        for i, val in enumerate(flat_list):
            if i in strings:
                flat_list[i] = strings[i]
            elif math.isnan(val):
                flat_list[i] = None
    else:
        col_is_int_cycle = cycle(col_is_int)
        for i, (val, col_int) in enumerate(zip(flat_list, col_is_int_cycle)):
            if i in strings:
                flat_list[i] = strings[i]
            elif math.isnan(val):
                flat_list[i] = None
            elif col_int:
                flat_list[i] = int(val)

    if is_1d:
        return flat_list
    return [flat_list[r * ncols : (r + 1) * ncols] for r in range(nrows)]


# Proposed Vectorized NumPy Object-Masking Strategy
def unpack_numpy_opt(envelope: dict[str, Any]) -> Any:
    shape = envelope["shape"]
    is_1d = len(shape) == 1
    nrows, ncols = (shape[0], 1) if is_1d else (shape[0], shape[1])
    raw = envelope["buffer"]
    column_kinds = envelope.get("column_kinds", ["float"] * ncols)
    raw_strings = envelope.get("strings", {})
    strings = {int(k): v for k, v in raw_strings.items()} if raw_strings else {}

    arr = np.frombuffer(raw, dtype=np.float64)
    if not is_1d:
        arr = arr.reshape((nrows, ncols))

    nan_mask = np.isnan(arr)
    obj_arr = arr.astype(object)
    obj_arr[nan_mask] = None

    col_is_int = [k == "int" for k in column_kinds]
    if any(col_is_int):
        for c, is_int in enumerate(col_is_int):
            if is_int:
                col_slice = obj_arr[:, c] if not is_1d else obj_arr
                col_nan_mask = nan_mask[:, c] if not is_1d else nan_mask
                valid_mask = ~col_nan_mask
                col_slice[valid_mask] = col_slice[valid_mask].astype(int)

    if strings:
        flat_obj = obj_arr.ravel()
        for idx, val in strings.items():
            flat_obj[idx] = val

    return obj_arr.tolist()


# Helper to pack standard grid into binary envelope format
def mock_pack(grid: list[Any] | list[list[Any]]) -> dict[str, Any]:
    import array
    if not grid:
        return {
            "__wa_payload__": "split_grid",
            "dtype": "float64",
            "column_kinds": [],
            "shape": [0],
            "strings": {},
            "buffer": b"",
        }

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
    strings: dict[int, str] = {}
    column_kinds = ["int"] * (ncols if is_2d else 1)

    buf_append = buf.append
    idx = 0
    rows = grid if is_2d else [grid]

    for row in rows:
        for c, val in enumerate(row):
            col_idx = c if is_2d else 0
            if val is None:
                buf_append(math.nan)
                column_kinds[col_idx] = "float"
            elif val is True or val is False:
                buf_append(float(val))
            else:
                t = type(val)
                if t is float:
                    buf_append(val)
                    column_kinds[col_idx] = "float"
                elif t is int:
                    buf_append(float(val))
                else:
                    buf_append(math.nan)
                    strings[idx] = val if t is str else str(val)
            idx += 1

    return {
        "__wa_payload__": "split_grid",
        "dtype": "float64",
        "column_kinds": column_kinds,
        "shape": shape,
        "strings": strings,
        "buffer": buf.tobytes(),
    }


# Helper to generate test datasets
def generate_dataset(
    nrows: int,
    ncols: int,
    *,
    string_ratio: float = 0.05,
    none_ratio: float = 0.05,
    int_ratio: float = 0.40,
) -> list[Any] | list[list[Any]]:
    grid: list[list[Any]] = []
    for _ in range(nrows):
        row: list[Any] = []
        for _ in range(ncols):
            r = random.random()
            if r < string_ratio:
                row.append(random.choice(["apple", "banana", "cherry", "pear", "orange"]))
            elif r < string_ratio + none_ratio:
                row.append(None)
            elif r < string_ratio + none_ratio + int_ratio:
                row.append(random.randint(1, 10000))
            else:
                row.append(random.random() * 1000)
        grid.append(row)
    return grid


# Benchmark run wrapper
def run_bench(
    name: str,
    fn: Callable[[dict[str, Any]], Any],
    envelope: dict[str, Any],
    *,
    warmup: int = 5,
    iters: int = 25,
) -> float:
    for _ in range(warmup):
        fn(envelope)

    times = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn(envelope)
        times.append((time.perf_counter() - t0) * 1000)

    # Return median time in milliseconds
    return sorted(times)[len(times) // 2]


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark different split_grid unpacking strategies.")
    parser.add_argument("--iters", type=int, default=30, help="Number of benchmark iterations")
    parser.add_argument("--warmup", type=int, default=5, help="Number of warmup iterations")
    args = parser.parse_args()

    shapes = [
        (10, 10, "Small Grid"),
        (100, 10, "Medium Row-Heavy Grid"),
        (100, 100, "Large Square Grid"),
        (1000, 100, "Huge Grid"),
    ]

    scenarios = [
        {"name": "Sparse Strings (2% Str, 2% None, 40% Int)", "str": 0.02, "none": 0.02, "int": 0.40},
        {"name": "Mixed Dense (15% Str, 10% None, 30% Int)", "str": 0.15, "none": 0.10, "int": 0.30},
        {"name": "Pure Numeric (0% Str, 5% None, 50% Int)", "str": 0.0, "none": 0.05, "int": 0.50},
        {"name": "All Strings (100% Str, 0% None, 0% Int)", "str": 1.0, "none": 0.0, "int": 0.0},
    ]

    print("=" * 110)
    print(f"{'Shape / Dataset':<35} | {'Scenario':<30} | {'Current (ms)':>12} | {'Cycle (ms)':>10} | {'Vector (ms)':>11} | {'Speedup':>9}")
    print("=" * 110)

    for nrows, ncols, shape_desc in shapes:
        for scen in scenarios:
            grid = generate_dataset(
                nrows, ncols,
                string_ratio=scen["str"],
                none_ratio=scen["none"],
                int_ratio=scen["int"]
            )
            envelope = mock_pack(grid)

            t_current = run_bench("Current", unpack_current, envelope, warmup=args.warmup, iters=args.iters)
            t_cycle = run_bench("Cycle", unpack_cycle_opt, envelope, warmup=args.warmup, iters=args.iters)
            t_vector = run_bench("Vector", unpack_numpy_opt, envelope, warmup=args.warmup, iters=args.iters)

            speedup = t_current / t_vector if t_vector > 0 else 0.0

            scen_name = scen["name"]
            shape_label = f"{nrows}x{ncols} ({shape_desc})"

            print(f"{shape_label:<35} | {scen_name:<30} | {t_current:>12.3f} | {t_cycle:>10.3f} | {t_vector:>11.3f} | {speedup:>8.2f}x")
        print("-" * 110)


if __name__ == "__main__":
    main()
