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
        execute_request_detail,
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


@patch("urllib.request.urlopen")
def test_execute_request_detail_custom_content_type(mock_urlopen: MagicMock) -> None:
    """execute_request_detail forwards custom Content-Type (e.g. multipart/form-data)."""
    mock_cm = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{"status": "ok", "result": 123}'
    mock_cm.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_cm

    res = execute_request_detail(
        "http://127.0.0.1:8000/v1/execute",
        b"--boundary...",
        content_type="multipart/form-data; boundary=boundary",
    )
    assert res.success is True
    assert res.data == {"status": "ok", "result": 123}
    req = mock_urlopen.call_args[0][0]
    assert req.headers["Content-type"] == "multipart/form-data; boundary=boundary"


def test_health_monitor_metrics() -> None:
    monitor = HealthMonitor("http://127.0.0.1:9999", interval_sec=0.1)
    monitor.latencies_ms = [2.0, 4.0, 6.0, 8.0, 10.0]
    assert monitor.p50_ms == pytest.approx(6.0)
    assert monitor.p95_ms == pytest.approx(10.0)
    assert monitor.max_ms == pytest.approx(10.0)


@patch("benchmark_compute_service.os.cpu_count", return_value=6)
def test_default_worker_counts_ends_at_cpu_count(_cpu: MagicMock) -> None:
    from benchmark_compute_service import _default_worker_counts

    assert _default_worker_counts() == [1, 2, 4, 6]
