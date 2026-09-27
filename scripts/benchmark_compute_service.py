#!/usr/bin/env python3
# WriterAgent - Python Compute Service Benchmark CLI
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Performance and concurrency benchmarking suite for the Python Compute Service.

Evaluates throughput (RPS), latency percentiles, and multi-core scaling efficiency
across realistic spreadsheet calculation archetypes:
1. numpy_vector: Heavy vectorized numeric math (GIL-releasing)
2. tabular_stats: 2D table filtering & aggregation (Mixed C/Python)
3. pure_python: CPU-bound string/math loop (GIL-holding)
4. stateful_session: Multi-tenant shared session updates (mode='shared')

Usage:
    python scripts/benchmark_compute_service.py --help
    python scripts/benchmark_compute_service.py --quick
    python scripts/benchmark_compute_service.py --workers 1,2,4 --concurrency 4
    python scripts/benchmark_compute_service.py --concurrency 1,2,4,8,16,32 --threads 32
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Ensure repo root is on sys.path
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from compute_service.config import ComputeSettings
from compute_service.server import WSGIDualStackServer, create_wsgi_app

WORKLOADS: dict[str, dict[str, Any]] = {
    "numpy_vector": {
        "description": "NumPy Vector Matrix Math (GIL Released)",
        "code": "import numpy as np\narr = np.array(data, dtype=np.float64)\nmat = np.outer(arr, arr)\nresult = float(np.linalg.norm(np.dot(mat, mat)))",
        "data": [float(i) * 0.05 for i in range(200)],
        "mode": "isolated",
    },
    "tabular_stats": {
        "description": "2D Table Filtering & Summary Stats (Mixed C/Python)",
        "code": "filtered = [r[2] for r in data if r[3]]\nresult = {'count': len(data), 'filtered_count': len(filtered), 'sum': sum(filtered)}",
        "data": [[i, f"item_{i}", float(i) * 1.5, i % 3 == 0] for i in range(200)],
        "mode": "isolated",
    },
    "pure_python": {
        "description": "Pure-Python CPU Loop & String Formatting (GIL Held)",
        "code": "tot = 0\nfor i in range(1500):\n    tot += (i * 7) ^ (i % 13)\nresult = f'checksum_{tot}'",
        "data": None,
        "mode": "isolated",
    },
    "stateful_session": {
        "description": "Multi-Tenant Shared Session Recalculations (mode='shared')",
        "code": "try:\n    counter += 1\nexcept NameError:\n    counter = 1\nresult = counter",
        "data": None,
        "mode": "shared",
    },
}


@dataclass
class BenchmarkResult:
    workload: str
    workers: int
    concurrency: int
    total_requests: int
    successful_requests: int
    failed_requests: int
    duration_sec: float
    rps: float
    latencies_ms: list[float]

    @property
    def mean_ms(self) -> float:
        return sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def p50_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        return s[int(len(s) * 0.50)]

    @property
    def p95_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        return s[min(int(len(s) * 0.95), len(s) - 1)]

    @property
    def p99_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        return s[min(int(len(s) * 0.99), len(s) - 1)]

    @property
    def max_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ManagedBenchmarkServer:
    def __init__(self, max_threads: int = 32, workers: int = 1) -> None:
        self.port = _get_free_port()
        self.max_threads = max_threads
        self.workers = workers
        # ComputeSettings is a dataclass whose field is ``threads``.
        # ``max_threads`` is a read-only alias property, so passing it as a
        # constructor keyword raises TypeError. The listener still uses
        # ``max_threads`` (WSGIDualStackServer); map it onto ``threads`` here.
        self.settings = ComputeSettings(
            host="127.0.0.1",
            port=self.port,
            threads=self.max_threads,
            workers=self.workers,
            log_level="WARN",
        )
        self.server = WSGIDualStackServer("127.0.0.1", self.port, max_threads=self.max_threads)
        self.server.set_app(create_wsgi_app(self.settings))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        time.sleep(0.15)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        from compute_service.formula_pool import shutdown_formula_pool

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        shutdown_formula_pool()


def execute_request(url: str, payload_bytes: bytes) -> tuple[bool, float]:
    """Send one POST request to url (e.g. /v1/execute[?session_id=...]) and return (success, latency_ms)."""
    start_t = time.perf_counter()
    req = urllib.request.Request(
        url,
        data=payload_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            data = resp.read()
            duration_ms = (time.perf_counter() - start_t) * 1000.0
            if resp.status == 200:
                parsed = json.loads(data.decode("utf-8"))
                return (parsed.get("status") == "ok", duration_ms)
            return (False, duration_ms)
    except Exception:
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        return (False, duration_ms)


def run_benchmark_scenario(
    target_url: str,
    workload_key: str,
    concurrency: int,
    requests_per_worker: int,
    workers: int = 1,
) -> BenchmarkResult:
    spec = WORKLOADS[workload_key]
    is_shared = spec.get("mode") == "shared"
    payload = {
        "code": spec["code"],
        "data": spec.get("data"),
        "mode": spec.get("mode", "isolated"),
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    # Pre-warm worker subprocesses so initial module imports (e.g. numpy C-extensions)
    # and bytecode compilation are excluded from timed calculation throughput.
    warmup_count = max(1, min(workers, 8))
    with ThreadPoolExecutor(max_workers=warmup_count) as warmup_pool:
        warmup_futures = [
            warmup_pool.submit(
                execute_request,
                (
                    f"{target_url}/v1/execute?session_id=bench-warmup-{i}"
                    if is_shared
                    else f"{target_url}/v1/execute"
                ),
                payload_bytes,
            )
            for i in range(warmup_count)
        ]
        for f in warmup_futures:
            try:
                f.result()
            except Exception:
                pass

    latencies: list[float] = []
    success_count = 0
    fail_count = 0
    lock = threading.Lock()

    def worker_job(worker_id: int) -> None:
        nonlocal success_count, fail_count
        # Build URL and payload (for shared sessions, pass session_id as URL query parameter)
        req_url = (
            f"{target_url}/v1/execute?session_id=bench-worker-{worker_id}"
            if is_shared
            else f"{target_url}/v1/execute"
        )

        worker_latencies: list[float] = []
        worker_succ = 0
        worker_fail = 0

        for _ in range(requests_per_worker):
            ok, lat_ms = execute_request(req_url, payload_bytes)
            worker_latencies.append(lat_ms)
            if ok:
                worker_succ += 1
            else:
                worker_fail += 1

        with lock:
            latencies.extend(worker_latencies)
            success_count += worker_succ
            fail_count += worker_fail

    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker_job, i) for i in range(concurrency)]
        for f in futures:
            f.result()
    wall_duration = time.perf_counter() - wall_start

    total_reqs = success_count + fail_count
    rps = total_reqs / wall_duration if wall_duration > 0 else 0.0

    return BenchmarkResult(
        workload=workload_key,
        workers=workers,
        concurrency=concurrency,
        total_requests=total_reqs,
        successful_requests=success_count,
        failed_requests=fail_count,
        duration_sec=wall_duration,
        rps=rps,
        latencies_ms=latencies,
    )


def format_results_table(results: list[BenchmarkResult]) -> str:
    lines: list[str] = []
    header = f"{'Workload':<18} | {'Workers':<7} | {'Clients':<7} | {'RPS':>9} | {'Mean (ms)':>9} | {'p50 (ms)':>8} | {'p95 (ms)':>8} | {'p99 (ms)':>8} | {'Max (ms)':>8} | {'Errors':>6}"
    sep = "-" * len(header)
    lines.append(sep)
    lines.append(header)
    lines.append(sep)

    current_workload = ""
    for r in results:
        if current_workload and r.workload != current_workload:
            lines.append(sep)
        current_workload = r.workload
        lines.append(
            f"{r.workload:<18} | {r.workers:<7} | {r.concurrency:<7} | {r.rps:>9.1f} | {r.mean_ms:>9.2f} | {r.p50_ms:>8.2f} | {r.p95_ms:>8.2f} | {r.p99_ms:>8.2f} | {r.max_ms:>8.2f} | {r.failed_requests:>6}"
        )
    lines.append(sep)
    return "\n".join(lines)


def run_benchmarks(
    *,
    workloads: list[str] | None = None,
    concurrencies: list[int] | None = None,
    requests_per_worker: int = 50,
    server_threads: int = 32,
    worker_counts: list[int] | None = None,
    target_url: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> list[BenchmarkResult]:
    selected_workloads = workloads or list(WORKLOADS.keys())
    selected_concurrencies = concurrencies or [1, 2, 4, 8, 16, 32]
    selected_workers = worker_counts or [1]
    all_results: list[BenchmarkResult] = []

    def _execute_suite(base_url: str, active_workers: int) -> None:
        for w in selected_workloads:
            if w not in WORKLOADS:
                continue
            for c in selected_concurrencies:
                if progress_callback:
                    progress_callback(
                        f"Running {w} (workers={active_workers}, concurrency={c}, reqs/worker={requests_per_worker})..."
                    )
                res = run_benchmark_scenario(
                    base_url,
                    w,
                    c,
                    requests_per_worker,
                    workers=active_workers,
                )
                all_results.append(res)

    if target_url:
        _execute_suite(target_url, active_workers=selected_workers[0] if selected_workers else 1)
    else:
        for w_count in selected_workers:
            with ManagedBenchmarkServer(max_threads=server_threads, workers=w_count) as base_url:
                _execute_suite(base_url, active_workers=w_count)

    return all_results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark concurrency and throughput of the Python Compute Service",
    )
    parser.add_argument(
        "--workloads",
        default="all",
        help="Comma-separated workload names or 'all' (options: numpy_vector, tabular_stats, pure_python, stateful_session)",
    )
    parser.add_argument(
        "--workers",
        default="1",
        help="Comma-separated server formula worker counts to benchmark (default: 1; e.g. 1,2,4)",
    )
    parser.add_argument(
        "--concurrency",
        default="1,2,4,8,16,32",
        help="Comma-separated concurrency levels (default: 1,2,4,8,16,32)",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=50,
        help="Number of requests per client worker (default: 50)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=32,
        help="Server max_threads capacity (default: 32)",
    )
    parser.add_argument(
        "--target-url",
        default=None,
        help="Optional existing server URL (e.g. http://127.0.0.1:8000). If omitted, an embedded server is started.",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Optional path to write raw JSON benchmark results",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run a quick formula worker scaling benchmark (workers 1, 2, 4; concurrency 4; 10 reqs/client)",
    )

    args = parser.parse_args(argv)

    if args.quick:
        worker_counts = [1, 2, 4]
        concurrencies = [4]
        reqs = 10
    else:
        worker_counts = [int(x.strip()) for x in args.workers.split(",") if x.strip()]
        concurrencies = [int(x.strip()) for x in args.concurrency.split(",") if x.strip()]
        reqs = args.requests

    if args.workloads == "all":
        workloads = list(WORKLOADS.keys())
    else:
        workloads = [x.strip() for x in args.workloads.split(",") if x.strip()]

    print("=" * 80)
    print("Python Compute Service Benchmark Suite")
    print(f"Workloads:   {', '.join(workloads)}")
    print(f"Workers:     {worker_counts}")
    print(f"Concurrency: {concurrencies}")
    print(f"Requests/w:  {reqs} (Total requests per scenario: {[c * reqs for c in concurrencies]})")
    print(f"Max threads: {args.threads}")
    print("=" * 80)

    results = run_benchmarks(
        workloads=workloads,
        concurrencies=concurrencies,
        requests_per_worker=reqs,
        server_threads=args.threads,
        worker_counts=worker_counts,
        target_url=args.target_url,
        progress_callback=lambda msg: print(f"  [+] {msg}"),
    )

    print("\nBenchmark Results:")
    table = format_results_table(results)
    print(table)

    if args.json_out:
        out_data = [
            {
                "workload": r.workload,
                "workers": r.workers,
                "concurrency": r.concurrency,
                "total_requests": r.total_requests,
                "successful_requests": r.successful_requests,
                "failed_requests": r.failed_requests,
                "duration_sec": r.duration_sec,
                "rps": r.rps,
                "mean_ms": r.mean_ms,
                "p50_ms": r.p50_ms,
                "p95_ms": r.p95_ms,
                "p99_ms": r.p99_ms,
                "max_ms": r.max_ms,
            }
            for r in results
        ]
        Path(args.json_out).write_text(json.dumps(out_data, indent=2), encoding="utf-8")
        print(f"\nRaw results saved to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
