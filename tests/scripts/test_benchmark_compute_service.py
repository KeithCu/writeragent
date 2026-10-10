# WriterAgent - Tests for scripts/benchmark_compute_service.py
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from tests.harness.strip_bundle import is_release_build

if is_release_build() or not (_SCRIPTS / "benchmark_compute_service.py").is_file():
    pytest.skip("benchmark_compute_service not available in release builds", allow_module_level=True)

try:
    from benchmark_compute_service import (  # noqa: E402
        BenchmarkResult,
        HealthMonitor,
        ManagedBenchmarkServer,
        StressResult,
        format_results_table,
        format_stress_results,
        main,
    )
except ImportError:
    pytest.skip("benchmark_compute_service not importable in release builds", allow_module_level=True)


def test_benchmark_result_fields_and_properties() -> None:
    res = BenchmarkResult(
        workload="pure_python",
        workers=2,
        concurrency=4,
        total_requests=80,
        successful_requests=80,
        failed_requests=0,
        duration_sec=0.5,
        rps=160.0,
        latencies_ms=[10.0, 20.0, 30.0, 40.0, 50.0],
    )
    assert res.workers == 2
    assert res.concurrency == 4
    assert res.mean_ms == pytest.approx(30.0)
    assert res.p50_ms == pytest.approx(30.0)
    assert res.p95_ms == pytest.approx(50.0)
    assert res.p99_ms == pytest.approx(50.0)
    assert res.max_ms == pytest.approx(50.0)


def test_benchmark_result_empty_latencies() -> None:
    res = BenchmarkResult(
        workload="pure_python",
        workers=1,
        concurrency=1,
        total_requests=0,
        successful_requests=0,
        failed_requests=0,
        duration_sec=0.0,
        rps=0.0,
        latencies_ms=[],
    )
    assert res.mean_ms == 0.0
    assert res.p50_ms == 0.0
    assert res.p95_ms == 0.0
    assert res.p99_ms == 0.0
    assert res.max_ms == 0.0


def test_format_results_table() -> None:
    results = [
        BenchmarkResult(
            workload="numpy_vector",
            workers=1,
            concurrency=4,
            total_requests=80,
            successful_requests=80,
            failed_requests=0,
            duration_sec=0.4,
            rps=200.0,
            latencies_ms=[5.0, 5.0],
        ),
        BenchmarkResult(
            workload="numpy_vector",
            workers=2,
            concurrency=4,
            total_requests=80,
            successful_requests=80,
            failed_requests=0,
            duration_sec=0.2,
            rps=400.0,
            latencies_ms=[2.5, 2.5],
        ),
    ]
    table = format_results_table(results)
    assert "Workers" in table
    assert "Clients" in table
    assert "numpy_vector" in table
    assert "200.0" in table
    assert "400.0" in table


@patch("benchmark_compute_service.run_benchmarks")
def test_main_quick_flag(mock_run: MagicMock) -> None:
    mock_run.return_value = []
    code = main(["--quick"])
    assert code == 0
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs["worker_counts"] == [1, 2, 4]
    assert kwargs["concurrencies"] == [4]
    assert kwargs["requests_per_worker"] == 10


@patch("benchmark_compute_service.run_benchmarks")
def test_main_custom_workers_flag(mock_run: MagicMock) -> None:
    mock_run.return_value = []
    code = main(["--workers", "1,2,4,8", "--concurrency", "8", "--requests", "30"])
    assert code == 0
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs["worker_counts"] == [1, 2, 4, 8]
    assert kwargs["concurrencies"] == [8]
    assert kwargs["requests_per_worker"] == 30


@patch("benchmark_compute_service.execute_request")
def test_run_benchmark_scenario_sends_warmup(mock_exec: MagicMock) -> None:
    from benchmark_compute_service import run_benchmark_scenario

    mock_exec.return_value = (True, 5.0)
    res = run_benchmark_scenario(
        target_url="http://127.0.0.1:8000",
        workload_key="pure_python",
        concurrency=2,
        requests_per_worker=3,
        workers=2,
    )
    assert res.workload == "pure_python"
    assert res.total_requests == 6
    assert res.successful_requests == 6
    # 2 warmup requests + (2 clients * 3 reqs) = 8 total execute_request calls
    assert mock_exec.call_count == 8


@patch("compute_service.formula_pool.shutdown_formula_pool")
@patch("benchmark_compute_service.create_wsgi_app")
@patch("benchmark_compute_service.WSGIDualStackServer")
def test_managed_benchmark_server_workers_and_shutdown(
    mock_server_cls: MagicMock,
    mock_app_fn: MagicMock,
    mock_shutdown_pool: MagicMock,
) -> None:
    mock_server = MagicMock()
    mock_server_cls.return_value = mock_server

    server = ManagedBenchmarkServer(max_threads=16, workers=3)
    assert server.settings.workers == 3
    assert server.settings.threads == 3

    with server as url:
        assert url.startswith("http://127.0.0.1:")

    mock_server.shutdown.assert_called_once()
    mock_server.server_close.assert_called_once()
    mock_shutdown_pool.assert_called_once()


def test_stress_result_fields_and_formatting() -> None:
    res = StressResult(
        workers=2,
        concurrency=32,
        total_requests=640,
        successful_requests=640,
        failed_requests=0,
        duration_sec=2.5,
        rps=256.0,
        invariants_passed=True,
        invariant_details=[
            "[PASS] Deterministic Math Accuracy",
            "[PASS] Session stress-session-0: 80/80 sequential",
        ],
        health_probes=120,
        health_failures=0,
        health_p50_ms=1.1,
        health_p95_ms=3.2,
        health_max_ms=8.5,
    )
    formatted = format_stress_results(res)
    assert "32 Clients / 2 Workers" in formatted
    assert "256.0 RPS" in formatted
    assert "ALL INVARIANTS PASSED [OK]" in formatted
    assert "120 total, 0 failures" in formatted


@patch("benchmark_compute_service.run_stress_suite")
def test_main_stress_flag(mock_stress: MagicMock) -> None:
    mock_stress.return_value = StressResult(
        workers=2,
        concurrency=32,
        total_requests=640,
        successful_requests=640,
        failed_requests=0,
        duration_sec=2.0,
        rps=320.0,
        invariants_passed=True,
        invariant_details=["[PASS] Test invariant"],
        health_probes=50,
        health_failures=0,
        health_p50_ms=1.0,
        health_p95_ms=2.0,
        health_max_ms=5.0,
    )
    code = main(["--stress", "--chaos", "hangs", "--requests", "15"])
    assert code == 0
    mock_stress.assert_called_once()
    _, kwargs = mock_stress.call_args
    assert kwargs["workers"] == 2
    assert kwargs["concurrency"] == 32
    assert kwargs["requests_per_worker"] == 15
    assert kwargs["chaos"] == ["hangs"]


def test_health_monitor_metrics() -> None:
    monitor = HealthMonitor("http://127.0.0.1:9999", interval_sec=0.1)
    monitor.latencies_ms = [2.0, 4.0, 6.0, 8.0, 10.0]
    assert monitor.p50_ms == pytest.approx(6.0)
    assert monitor.p95_ms == pytest.approx(10.0)
    assert monitor.max_ms == pytest.approx(10.0)

