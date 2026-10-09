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
    def __init__(self, max_threads: int = 32, workers: int = 1, worker_max_tasks: int = 500) -> None:
        self.port = _get_free_port()
        self.max_threads = max_threads
        self.workers = workers
        self.worker_max_tasks = worker_max_tasks
        # Listener threads inside settings follow workers. This bench sizes its
        # own HTTP pool.
        self.settings = ComputeSettings(
            host="127.0.0.1",
            port=self.port,
            workers=self.workers,
            worker_max_tasks=self.worker_max_tasks,
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


@dataclass
class RequestResponse:
    success: bool
    status_code: int
    duration_ms: float
    data: dict[str, Any] | None
    error: str | None = None


def execute_request_detail(
    url: str,
    payload_bytes: bytes,
    timeout: float = 30.0,
) -> RequestResponse:
    """Send one POST request and return detailed status, data, and latency."""
    start_t = time.perf_counter()
    req = urllib.request.Request(
        url,
        data=payload_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            duration_ms = (time.perf_counter() - start_t) * 1000.0
            if resp.status == 200:
                parsed = json.loads(data.decode("utf-8"))
                return RequestResponse(
                    success=parsed.get("status") == "ok",
                    status_code=resp.status,
                    duration_ms=duration_ms,
                    data=parsed,
                )
            return RequestResponse(
                success=False,
                status_code=resp.status,
                duration_ms=duration_ms,
                data=None,
                error=f"HTTP {resp.status}",
            )
    except urllib.error.HTTPError as exc:
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        try:
            body = exc.read().decode("utf-8")
            parsed = json.loads(body)
        except Exception:
            parsed = None
        return RequestResponse(
            success=False,
            status_code=exc.code,
            duration_ms=duration_ms,
            data=parsed,
            error=str(exc),
        )
    except Exception as exc:
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        return RequestResponse(
            success=False,
            status_code=599,
            duration_ms=duration_ms,
            data=None,
            error=str(exc),
        )


def execute_request(url: str, payload_bytes: bytes) -> tuple[bool, float]:
    """Send one POST request to url (e.g. /v1/execute[?session_id=...]) and return (success, latency_ms)."""
    resp = execute_request_detail(url, payload_bytes)
    return (resp.success, resp.duration_ms)


class HealthMonitor:
    """Background liveness prober for /health during concurrency stress."""

    def __init__(self, base_url: str, interval_sec: float = 0.02) -> None:
        self.url = f"{base_url.rstrip('/')}/health"
        self.interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.probes_total = 0
        self.probes_success = 0
        self.probes_failed = 0
        self.latencies_ms: list[float] = []
        self.failures: list[str] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(self.url, timeout=5.0) as resp:
                    lat_ms = (time.perf_counter() - t0) * 1000.0
                    self.probes_total += 1
                    if resp.status == 200:
                        self.probes_success += 1
                        self.latencies_ms.append(lat_ms)
                    else:
                        self.probes_failed += 1
                        self.failures.append(f"HTTP {resp.status}")
            except Exception as exc:
                self.probes_total += 1
                self.probes_failed += 1
                self.failures.append(str(exc))
            self._stop_event.wait(self.interval_sec)

    @property
    def p50_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return sorted(self.latencies_ms)[int(len(self.latencies_ms) * 0.50)]

    @property
    def p95_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        return s[min(int(len(s) * 0.95), len(s) - 1)]

    @property
    def max_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0


EXPECTED_MATH_RESULTS: dict[str, Any] = {
    "pure_python": "checksum_7869722",
    "tabular_stats": {"count": 200, "filtered_count": 67, "sum": 9949.5},
    "numpy_vector": 43781380.5625,
}


@dataclass
class StressResult:
    workers: int
    concurrency: int
    total_requests: int
    successful_requests: int
    failed_requests: int
    duration_sec: float
    rps: float
    invariants_passed: bool
    invariant_details: list[str]
    health_probes: int
    health_failures: int
    health_p50_ms: float
    health_p95_ms: float
    health_max_ms: float


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


def run_stress_suite(
    *,
    workers: int = 2,
    concurrency: int = 32,
    requests_per_worker: int = 20,
    server_threads: int = 32,
    reset_interval: int = 7,
    recycle_tasks: int = 500,
    target_url: str | None = None,
    chaos: list[str] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> StressResult:
    """Hammer compute_service workers with concurrent client threads and assert invariants.

    Invariants checked:
    1. Deterministic math outputs match expected results (no race conditions / frame corruption).
    2. Shared session increments monotonically without duplicate or lost updates across threads.
    3. Periodic session reset: repeatedly resets session every N requests, asserting state restarts at 1.
    4. Error handling returns standard 200 error payloads without destabilizing workers.
    5. Health probe liveness: /health responds with 200 OK throughout the high-concurrency run.
    6. Chaos resilience (if requested): timeout/kill recovery and session reset signaling.
    """
    chaos_set = {c.strip().lower() for c in (chaos or []) if c.strip()}
    invariant_details: list[str] = []
    invariants_passed = True

    def _execute_stress(base_url: str) -> StressResult:
        nonlocal invariants_passed
        health_monitor = HealthMonitor(base_url, interval_sec=0.02)
        health_monitor.start()

        successful_requests = 0
        failed_requests = 0
        all_latencies_ms: list[float] = []
        lock = threading.Lock()

        start_time = time.perf_counter()

        try:
            # -------------------------------------------------------------
            # Phase 1: High-concurrency deterministic math accuracy
            # -------------------------------------------------------------
            if progress_callback:
                progress_callback(
                    f"Phase 1: Deterministic math accuracy ({concurrency} threads hammering {workers} workers)..."
                )

            math_workloads = ["pure_python", "tabular_stats", "numpy_vector"]
            math_mismatches = 0
            phase1_total = 0

            def math_worker(worker_id: int) -> None:
                nonlocal phase1_total, math_mismatches, successful_requests, failed_requests
                local_succ = 0
                local_fail = 0
                local_mismatches = 0
                local_lats: list[float] = []

                for req_idx in range(requests_per_worker):
                    workload_key = math_workloads[(worker_id + req_idx) % len(math_workloads)]
                    spec = WORKLOADS[workload_key]
                    payload_bytes = json.dumps({
                        "code": spec["code"],
                        "data": spec.get("data"),
                        "mode": "isolated",
                    }).encode("utf-8")

                    resp = execute_request_detail(f"{base_url}/v1/execute", payload_bytes)
                    local_lats.append(resp.duration_ms)

                    if not resp.success or not resp.data:
                        local_fail += 1
                        continue

                    res_val = resp.data.get("result")
                    expected = EXPECTED_MATH_RESULTS[workload_key]

                    matched = False
                    if workload_key == "numpy_vector" and isinstance(res_val, (int, float)):
                        matched = abs(float(res_val) - float(expected)) < 0.1
                    elif workload_key == "tabular_stats" and isinstance(res_val, dict):
                        matched = res_val == expected
                    elif workload_key == "pure_python":
                        matched = res_val == expected

                    if matched:
                        local_succ += 1
                    else:
                        local_fail += 1
                        local_mismatches += 1

                with lock:
                    phase1_total += requests_per_worker
                    successful_requests += local_succ
                    failed_requests += local_fail
                    math_mismatches += local_mismatches
                    all_latencies_ms.extend(local_lats)

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(math_worker, i) for i in range(concurrency)]
                for f in futures:
                    f.result()

            if math_mismatches == 0:
                invariant_details.append(
                    f"[PASS] Deterministic Math Accuracy: {phase1_total}/{phase1_total} requests exact match"
                )
            else:
                invariants_passed = False
                invariant_details.append(
                    f"[FAIL] Deterministic Math Accuracy: {math_mismatches}/{phase1_total} result mismatches"
                )

            # -------------------------------------------------------------
            # Phase 2: Shared Session Monotonicity (Interleaved Threads)
            # -------------------------------------------------------------
            if progress_callback:
                progress_callback(
                    f"Phase 2: Shared session monotonicity (4 shared sessions across {concurrency} threads)..."
                )

            num_sessions = 4
            session_ids = [f"stress-session-{s}" for s in range(num_sessions)]
            session_vals: dict[str, list[int]] = {sid: [] for sid in session_ids}
            session_lock = threading.Lock()
            phase2_reqs_per_thread = max(5, requests_per_worker // 2)

            inc_payload = json.dumps({
                "code": "try:\n    counter += 1\nexcept NameError:\n    counter = 1\nresult = counter",
                "mode": "shared",
            }).encode("utf-8")

            def session_worker(worker_id: int) -> None:
                nonlocal successful_requests, failed_requests
                sid = session_ids[worker_id % num_sessions]
                req_url = f"{base_url}/v1/execute?session_id={sid}"

                local_succ = 0
                local_fail = 0
                local_vals: list[int] = []
                local_lats: list[float] = []

                for _ in range(phase2_reqs_per_thread):
                    resp = execute_request_detail(req_url, inc_payload)
                    local_lats.append(resp.duration_ms)
                    if resp.success and resp.data:
                        val = resp.data.get("result")
                        if isinstance(val, int):
                            local_vals.append(val)
                            local_succ += 1
                        else:
                            local_fail += 1
                    else:
                        local_fail += 1

                with session_lock:
                    session_vals[sid].extend(local_vals)
                with lock:
                    successful_requests += local_succ
                    failed_requests += local_fail
                    all_latencies_ms.extend(local_lats)

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(session_worker, i) for i in range(concurrency)]
                for f in futures:
                    f.result()

            for sid, vals in session_vals.items():
                expected_count = (concurrency // num_sessions) * phase2_reqs_per_thread
                expected_seq = list(range(1, expected_count + 1))
                actual_sorted = sorted(vals)
                if actual_sorted == expected_seq:
                    invariant_details.append(
                        f"[PASS] Session {sid}: {len(vals)}/{expected_count} sequential updates (0 lost, 0 dupes)"
                    )
                else:
                    invariants_passed = False
                    invariant_details.append(
                        f"[FAIL] Session {sid}: expected 1..{expected_count}, got {len(vals)} items (unique={len(set(vals))})"
                    )

            # -------------------------------------------------------------
            # Phase 3: Periodic Session Resets (Resetting every N requests)
            # -------------------------------------------------------------
            if reset_interval > 0:
                if progress_callback:
                    progress_callback(
                        f"Phase 3: Periodic session resets (every {reset_interval} requests via /v1/session/reset)..."
                    )

                reset_sid = "stress-periodic-reset-session"
                reset_cycles = 4
                reset_cycle_failures = 0

                for _ in range(reset_cycles):
                    cycle_vals: list[int] = []
                    for _req_num in range(reset_interval):
                        resp = execute_request_detail(
                            f"{base_url}/v1/execute?session_id={reset_sid}",
                            inc_payload,
                        )
                        with lock:
                            all_latencies_ms.append(resp.duration_ms)
                            if resp.success and resp.data and isinstance(resp.data.get("result"), int):
                                cycle_vals.append(resp.data["result"])
                                successful_requests += 1
                            else:
                                failed_requests += 1

                    expected_cycle = list(range(1, reset_interval + 1))
                    if sorted(cycle_vals) != expected_cycle:
                        reset_cycle_failures += 1

                    reset_resp = execute_request_detail(
                        f"{base_url}/v1/session/reset?session_id={reset_sid}",
                        b"{}",
                    )
                    with lock:
                        all_latencies_ms.append(reset_resp.duration_ms)
                        if reset_resp.success and reset_resp.status_code == 200:
                            successful_requests += 1
                        else:
                            failed_requests += 1
                            reset_cycle_failures += 1

                # Send 1 final check request: must restart at 1
                after_reset_resp = execute_request_detail(
                    f"{base_url}/v1/execute?session_id={reset_sid}",
                    inc_payload,
                )
                with lock:
                    all_latencies_ms.append(after_reset_resp.duration_ms)
                    if (
                        after_reset_resp.success
                        and after_reset_resp.data
                        and after_reset_resp.data.get("result") == 1
                    ):
                        successful_requests += 1
                    else:
                        failed_requests += 1
                        reset_cycle_failures += 1

                if reset_cycle_failures == 0:
                    invariant_details.append(
                        f"[PASS] Periodic Session Resets: {reset_cycles} cycles of {reset_interval} requests + reset verified (restarted at 1)"
                    )
                else:
                    invariants_passed = False
                    invariant_details.append(
                        f"[FAIL] Periodic Session Resets: {reset_cycle_failures} cycle failures during resets"
                    )

            # -------------------------------------------------------------
            # Phase 4: Error Resilience under Concurrency
            # -------------------------------------------------------------
            if progress_callback:
                progress_callback(
                    f"Phase 4: Error resilience under load ({concurrency} concurrent error calls)..."
                )

            error_payloads = [
                json.dumps({"code": "result = 100 / 0", "mode": "isolated"}).encode("utf-8"),
                json.dumps({"code": "def bad_syntax(:", "mode": "isolated"}).encode("utf-8"),
            ]
            error_fails = 0

            def error_worker(worker_id: int) -> None:
                nonlocal error_fails, successful_requests, failed_requests
                err_payload = error_payloads[worker_id % len(error_payloads)]
                resp = execute_request_detail(f"{base_url}/v1/execute", err_payload)
                with lock:
                    all_latencies_ms.append(resp.duration_ms)
                    if (
                        resp.status_code == 200
                        and resp.data
                        and resp.data.get("status") == "error"
                        and ("ZeroDivisionError" in str(resp.data.get("error"))
                             or "SyntaxError" in str(resp.data.get("error")))
                    ):
                        successful_requests += 1
                    else:
                        error_fails += 1
                        failed_requests += 1

                # Immediately follow up with valid math on the same worker
                ok_payload = json.dumps({"code": "result = 42 * 2", "mode": "isolated"}).encode("utf-8")
                ok_resp = execute_request_detail(f"{base_url}/v1/execute", ok_payload)
                with lock:
                    all_latencies_ms.append(ok_resp.duration_ms)
                    if ok_resp.success and ok_resp.data and ok_resp.data.get("result") == 84:
                        successful_requests += 1
                    else:
                        error_fails += 1
                        failed_requests += 1

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(error_worker, i) for i in range(concurrency)]
                for f in futures:
                    f.result()

            if error_fails == 0:
                invariant_details.append(
                    f"[PASS] Error Resilience: {concurrency} eval errors handled cleanly, workers recovered"
                )
            else:
                invariants_passed = False
                invariant_details.append(
                    f"[FAIL] Error Resilience: {error_fails} errors failed to handle or recover"
                )

            # -------------------------------------------------------------
            # Phase 5: Pluggable Chaos Scenarios (if enabled)
            # -------------------------------------------------------------
            if "hangs" in chaos_set:
                if progress_callback:
                    progress_callback("Phase 5 [Chaos]: Injecting timeout hangs...")
                hang_payload = json.dumps({
                    "code": "import time\ntime.sleep(2.0)\nresult = 1",
                    "mode": "isolated",
                    "timeout_ms": 500,
                }).encode("utf-8")
                hang_resp = execute_request_detail(f"{base_url}/v1/execute", hang_payload)
                normal_resp = execute_request_detail(
                    f"{base_url}/v1/execute",
                    json.dumps({"code": "result = 7", "mode": "isolated"}).encode("utf-8"),
                )
                if (
                    hang_resp.data
                    and hang_resp.data.get("status") == "error"
                    and normal_resp.success
                    and normal_resp.data
                    and normal_resp.data.get("result") == 7
                ):
                    invariant_details.append(
                        "[PASS] Chaos Hangs: timeout handled cleanly and pool recovered"
                    )
                else:
                    invariants_passed = False
                    invariant_details.append(
                        f"[FAIL] Chaos Hangs: hang_status={hang_resp.status_code}, normal_success={normal_resp.success}"
                    )

            if "crashes" in chaos_set:
                if progress_callback:
                    progress_callback("Phase 4 [Chaos]: Injecting worker crash...")
                crash_sid = "stress-chaos-crash-session"
                # Seed the session
                execute_request_detail(
                    f"{base_url}/v1/execute?session_id={crash_sid}",
                    json.dumps({"code": "result = 10", "mode": "shared"}).encode("utf-8"),
                )
                from compute_service.formula_pool import get_formula_pool

                pool = get_formula_pool()
                target_worker = None
                with pool._cond:
                    target_worker, *_ = pool._select_shared_worker(crash_sid)
                if target_worker:
                    target_worker.kill()

                recover_payload = json.dumps({
                    "code": "result = 42",
                    "mode": "shared",
                }).encode("utf-8")
                recover_resp = execute_request_detail(
                    f"{base_url}/v1/execute?session_id={crash_sid}",
                    recover_payload,
                )
                if (
                    recover_resp.success
                    and recover_resp.data
                    and recover_resp.data.get("session_reset") is True
                ):
                    invariant_details.append(
                        "[PASS] Chaos Crashes: worker killed, auto-respawned with session_reset=True"
                    )
                else:
                    invariants_passed = False
                    invariant_details.append(
                        f"[FAIL] Chaos Crashes: recover_reset={recover_resp.data.get('session_reset') if recover_resp.data else None}"
                    )

        finally:
            health_monitor.stop()

        duration = time.perf_counter() - start_time
        total_requests = successful_requests + failed_requests
        rps = total_requests / duration if duration > 0 else 0.0

        if health_monitor.probes_failed == 0 and health_monitor.probes_total > 0:
            invariant_details.append(
                f"[PASS] Health Liveness: {health_monitor.probes_total} probes, 0 failures, "
                f"p50={health_monitor.p50_ms:.2f}ms, p95={health_monitor.p95_ms:.2f}ms, max={health_monitor.max_ms:.2f}ms"
            )
        else:
            invariants_passed = False
            invariant_details.append(
                f"[FAIL] Health Liveness: {health_monitor.probes_failed}/{health_monitor.probes_total} health probes failed!"
            )

        return StressResult(
            workers=workers,
            concurrency=concurrency,
            total_requests=total_requests,
            successful_requests=successful_requests,
            failed_requests=failed_requests,
            duration_sec=duration,
            rps=rps,
            invariants_passed=invariants_passed,
            invariant_details=invariant_details,
            health_probes=health_monitor.probes_total,
            health_failures=health_monitor.probes_failed,
            health_p50_ms=health_monitor.p50_ms,
            health_p95_ms=health_monitor.p95_ms,
            health_max_ms=health_monitor.max_ms,
        )

    if target_url:
        return _execute_stress(target_url)
    with ManagedBenchmarkServer(max_threads=server_threads, workers=workers, worker_max_tasks=recycle_tasks) as url:
        return _execute_stress(url)


def format_stress_results(result: StressResult) -> str:
    lines = [
        "=" * 80,
        f"Python Compute Service Stress & Invariant Suite ({result.concurrency} Clients / {result.workers} Workers)",
        "=" * 80,
        f"Duration:     {result.duration_sec:.2f}s",
        f"Total Reqs:   {result.total_requests}",
        f"Success Reqs: {result.successful_requests}",
        f"Failed Reqs:  {result.failed_requests}",
        f"Throughput:   {result.rps:.1f} RPS",
        "-" * 80,
        "Health Probe Liveness:",
        f"  Probes:     {result.health_probes} total, {result.health_failures} failures",
        f"  Latency:    p50={result.health_p50_ms:.2f}ms | p95={result.health_p95_ms:.2f}ms | max={result.health_max_ms:.2f}ms",
        "-" * 80,
        "Invariant Verifications:",
    ]
    for detail in result.invariant_details:
        lines.append(f"  {detail}")
    lines.append("-" * 80)
    if result.invariants_passed:
        lines.append("STATUS: ALL INVARIANTS PASSED [OK]")
    else:
        lines.append("STATUS: ONE OR MORE INVARIANTS FAILED [FAIL]")
    lines.append("=" * 80)
    return "\n".join(lines)


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
        help="HTTP listener threads for this bench (default: 32). Not a service setting.",
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
    parser.add_argument(
        "--stress",
        action="store_true",
        help="Run 32-threads hammering 2-workers stress and invariant suite",
    )
    parser.add_argument(
        "--chaos",
        default="",
        help="Comma-separated chaos injection modes (options: hangs, crashes)",
    )
    parser.add_argument(
        "--reset-interval",
        type=int,
        default=7,
        help="Session reset frequency: resets shared session every N requests (default: 7)",
    )
    parser.add_argument(
        "--recycle-tasks",
        type=int,
        default=500,
        help="Worker process recycling frequency: terminates and respawns worker every N tasks (default: 500; pass e.g. 7 for rapid restarts)",
    )

    args = parser.parse_args(argv)

    if args.stress:
        worker_count = int(args.workers.split(",")[0].strip()) if args.workers != "1" else 2
        concurrency = int(args.concurrency.split(",")[0].strip()) if args.concurrency != "1,2,4,8,16,32" else 32
        reqs = args.requests if args.requests != 50 else 20
        chaos_modes = [c.strip() for c in args.chaos.split(",") if c.strip()]

        print("=" * 80)
        print("Python Compute Service Stress & Invariant Runner")
        print(f"Workers:        {worker_count}")
        print(f"Concurrency:    {concurrency}")
        print(f"Requests/w:     {reqs}")
        print(f"Reset Interval: {args.reset_interval}")
        print(f"Recycle Tasks:  {args.recycle_tasks}")
        print(f"Chaos:          {chaos_modes or 'none'}")
        print("=" * 80)

        stress_result = run_stress_suite(
            workers=worker_count,
            concurrency=concurrency,
            requests_per_worker=reqs,
            server_threads=args.threads,
            reset_interval=args.reset_interval,
            recycle_tasks=args.recycle_tasks,
            target_url=args.target_url,
            chaos=chaos_modes,
            progress_callback=lambda msg: print(f"  [+] {msg}"),
        )
        print("\n" + format_stress_results(stress_result))
        return 0 if stress_result.invariants_passed else 1

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
