#!/usr/bin/env python3
"""
Benchmark Warm Process vs In-Process NumPy/SymPy execution.
Finds the 1000th prime and performs matrix multiplication.
Compares both JSON and Pickle warm-process execution dynamically side-by-side.
"""

import sys
import os
import time
import json
from pathlib import Path

# Add project root to sys.path to allow importing from plugin
project_root = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(project_root))

from plugin.scripting.python_worker_manager import PythonWorkerManager
from plugin.scripting.venv_probe import resolve_venv_python, resolve_libreoffice_python


def find_config_path():
    """Find writeragent.json in standard LibreOffice profile locations."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", "")) / "LibreOffice" / "4" / "user"
        paths = [base / "writeragent.json"]
    elif sys.platform == "darwin":
        base = Path("~/Library/Application Support/LibreOffice/4/user").expanduser()
        paths = [base / "writeragent.json"]
    else:
        # Linux
        paths = [
            Path("~/.config/libreoffice/4/user/config/writeragent.json").expanduser(),
            Path("~/.config/libreoffice/4/user/writeragent.json").expanduser(),
            Path("~/.config/libreoffice/24/user/config/writeragent.json").expanduser(),
            Path("~/.config/libreoffice/24/user/writeragent.json").expanduser(),
        ]
    
    for p in paths:
        if p.exists():
            return p
    return None


def get_venv_path_from_config():
    config_path = find_config_path()
    if not config_path:
        return None
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            return config.get("scripting.python_venv_path")
    except Exception:
        return None


def prime_code(n):
    return f"""
import sympy
result = int(sympy.prime({n}))
"""


def matrix_code(size):
    return f"""
import numpy as np
a = np.random.rand({size}, {size})
b = np.random.rand({size}, {size})
c = np.dot(a, b)
result = float(np.sum(c))
"""


def benchmark_in_process_prime(n, iterations=10):
    try:
        import sympy
        # Warmup
        sympy.prime(n)
        
        times = []
        for _ in range(iterations):
            start = time.perf_counter()
            res = int(sympy.prime(n))
            end = time.perf_counter()
            times.append(end - start)
        return res, sum(times) / len(times), min(times)
    except ImportError:
        return None, None, None


def benchmark_in_process_matrix(size, iterations=10):
    try:
        import numpy as np
        # Warmup
        a = np.random.rand(size, size)
        b = np.random.rand(size, size)
        np.dot(a, b)
        
        times = []
        for _ in range(iterations):
            start = time.perf_counter()
            a = np.random.rand(size, size)
            b = np.random.rand(size, size)
            c = np.dot(a, b)
            res = float(np.sum(c))
            end = time.perf_counter()
            times.append(end - start)
        return res, sum(times) / len(times), min(times)
    except ImportError:
        return None, None, None


def benchmark_worker(mgr, code, iterations=10):
    # Warmup
    mgr.execute(code)
    
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        resp = mgr.execute(code)
        end = time.perf_counter()
        times.append(end - start)
    
    if resp["status"] == "ok":
        return resp["result"], sum(times) / len(times), min(times)
    return None, None, None


def run_benchmark_for_pickle(exe, code, iterations=10):
    """Run worker benchmark with production Pickle5 protocol."""
    mgr = PythonWorkerManager.get(exe, {})
    
    # 1. Cold start
    mgr._terminate_worker()
    start_cold = time.perf_counter()
    mgr.execute(code)
    end_cold = time.perf_counter()
    cold_sec = end_cold - start_cold
    
    # 2. Warm start
    res, avg_sec, min_sec = benchmark_worker(mgr, code, iterations)
    
    return cold_sec, avg_sec, min_sec


def main():
    print("--- WriterAgent Warm Process Benchmark (with Warmup) ---")
    
    venv_path = get_venv_path_from_config()
    if venv_path:
        print(f"Using configured venv: {venv_path}")
        exe = resolve_venv_python(venv_path)
    else:
        print("No venv configured in writeragent.json, falling back to LO/System python.")
        exe = resolve_libreoffice_python()
    
    if not exe:
        print("Error: Could not resolve Python executable.")
        sys.exit(1)
    
    print(f"Python Executable: {exe}")
    
    n_prime = 1000
    p_code = prime_code(n_prime)
    m_size = 1000
    m_code = matrix_code(m_size)
    iters = 10

    # 1. Prime Task
    print(f"\n[Task 1: 1000th Prime ({iters} iterations)]")
    res_in, avg_in, min_in = benchmark_in_process_prime(n_prime, iters)
    if avg_in is not None:
        print(f"  In-Process: Avg: {avg_in:.6f}s | Min: {min_in:.6f}s")
    else:
        print("  In-Process: N/A (SymPy not installed)")

    # Run Pickle mode
    cold_pickle, avg_pickle, min_pickle = run_benchmark_for_pickle(exe, p_code, iters)
    print(f"  Pickle Mode: Cold: {cold_pickle:.6f}s | Warm Avg: {avg_pickle:.6f}s | Warm Min: {min_pickle:.6f}s")

    # 2. Matrix Task
    print(f"\n[Task 2: {m_size}x{m_size} Matrix ({iters} iterations)]")
    res_in_m, avg_in_m, min_in_m = benchmark_in_process_matrix(m_size, iters)
    if avg_in_m is not None:
        print(f"  In-Process: Avg: {avg_in_m:.6f}s | Min: {min_in_m:.6f}s")
    else:
        print("  In-Process: N/A (NumPy not installed)")

    # Run Pickle mode
    cold_pickle_m, avg_pickle_m, min_pickle_m = run_benchmark_for_pickle(exe, m_code, iters)
    print(f"  Pickle Mode: Cold: {cold_pickle_m:.6f}s | Warm Avg: {avg_pickle_m:.6f}s | Warm Min: {min_pickle_m:.6f}s")

    # Teardown workers
    PythonWorkerManager.shutdown_all()
    print("\nBenchmark complete.")


if __name__ == "__main__":
    main()

