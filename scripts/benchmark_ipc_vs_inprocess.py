#!/usr/bin/env python3
# WriterAgent - Benchmark IPC Subprocess vs In-Process Execution
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Performance comparison benchmark:
Measures the real-world latency and throughput difference between:
1. In-Process Threaded Execution (direct Python function calls)
2. Subprocess Pipe IPC Execution (warm child process via Pickle5 frames over stdin/stdout)
"""

from __future__ import annotations

import os
import sys
import time
from statistics import mean, median

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.formula_worker import execute_code


# Standalone micro-worker for Pickle IPC benchmarking
from compute_service.formula_pool import FormulaProcessPool


SCENARIOS = {
    "1. Micro Calculation (x = 1 + 2)": {
        "code": "result = 1 + 2",
        "data": None,
    },
    "2. NumPy Vector Math (1,000 floats)": {
        "code": "import numpy as np\narr = np.array(data, dtype=np.float64)\nresult = float(np.sum(arr ** 2) / arr.size)",
        "data": [[float(i) for i in range(1000)]],
    },
    "3. Tabular 2D Grid (100x10 matrix)": {
        "code": "import numpy as np\narr = np.array(data, dtype=np.float64)\nresult = [float(np.mean(arr[:, col])) for col in range(arr.shape[1])]",
        "data": [[float(r * 10 + c) for c in range(10)] for r in range(100)],
    },
}


def run_benchmark(iterations: int = 100) -> None:
    print("=" * 80)
    print("Execution Architecture Benchmark: In-Process vs Subprocess Pickle IPC")
    print(f"Iterations per scenario: {iterations}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print("=" * 80)

    pickle_pool = FormulaProcessPool(num_workers=1, default_timeout_sec=30)
    time.sleep(0.2)  # warm up

    try:
        for name, scen in SCENARIOS.items():
            code = scen["code"]
            data = scen["data"]

            # Warmup runs
            execute_code(code, data)
            pickle_pool.execute(code, data)

            # Benchmark In-Process
            inproc_times_ms: list[float] = []
            for _ in range(iterations):
                t0 = time.perf_counter()
                res = execute_code(code, data)
                t1 = time.perf_counter()
                assert res.get("status") == "ok", f"In-proc failed: {res}"
                inproc_times_ms.append((t1 - t0) * 1000.0)

            # Benchmark Pickle Subprocess IPC
            pickle_times_ms: list[float] = []
            for _ in range(iterations):
                t0 = time.perf_counter()
                res = pickle_pool.execute(code, data)
                t1 = time.perf_counter()
                assert res.get("status") == "ok", f"Pickle IPC failed: {res}"
                pickle_times_ms.append((t1 - t0) * 1000.0)

            in_mean = mean(inproc_times_ms)
            in_p50 = median(inproc_times_ms)

            pickle_mean = mean(pickle_times_ms)
            pickle_p50 = median(pickle_times_ms)

            diff_ms = pickle_mean - in_mean
            diff_pct = (diff_ms / in_mean) * 100.0 if in_mean > 0 else 0.0

            print(f"\n{name}:")
            print(f"  In-Process:            Mean = {in_mean:6.2f} ms | p50 = {in_p50:6.2f} ms")
            print(f"  Subprocess Pickle IPC: Mean = {pickle_mean:6.2f} ms | p50 = {pickle_p50:6.2f} ms")
            print(f"  Difference:            {diff_ms:+6.2f} ms ({diff_pct:+5.1f}% overhead)")
    finally:
        pickle_pool.shutdown()

    print("\n" + "=" * 80)


if __name__ == "__main__":
    run_benchmark(iterations=100)
