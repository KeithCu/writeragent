#!/usr/bin/env python3
# WriterAgent - compute service keep-alive benchmark
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""Small /v1/execute requests: new connection per request vs one reused connection.

Usage: python scripts/benchmark_keepalive.py [--requests 200] [--clients 8]
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.benchmark_compute_service import ManagedBenchmarkServer  # noqa: E402


def _body(n: int) -> bytes:
    return json.dumps({"id": str(n), "code": "result = 1", "mode": "isolated"}).encode()


def _one(conn: http.client.HTTPConnection, n: int, close: bool) -> float:
    headers = {"Content-Type": "application/json"}
    if close:
        headers["Connection"] = "close"
    t0 = time.perf_counter()
    conn.request("POST", "/v1/execute", body=_body(n), headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    dt = (time.perf_counter() - t0) * 1000.0
    assert resp.status == 200, (resp.status, data[:200])
    return dt


def _client(port: int, count: int, reuse: bool, out: list[float]) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    for i in range(count):
        if not reuse:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        out.append(_one(conn, i, close=not reuse))
        if not reuse:
            conn.close()
    conn.close()


def _run(port: int, clients: int, per_client: int, reuse: bool) -> tuple[float, float, float]:
    lats: list[list[float]] = [[] for _ in range(clients)]
    threads = [threading.Thread(target=_client, args=(port, per_client, reuse, lats[i])) for i in range(clients)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    s = sorted(x for lst in lats for x in lst)
    return len(s) / wall, s[len(s) // 2], s[min(int(len(s) * 0.99), len(s) - 1)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()
    with ManagedBenchmarkServer(max_threads=8, workers=args.workers) as url:
        port = int(url.rsplit(":", 1)[1])
        _run(port, 1, 5, False)  # warm-up
        print(f"{'mode':<24}{'req/s':>10}{'p50 ms':>10}{'p99 ms':>10}")
        for label, clients, reuse in (
            ("sequential new-conn", 1, False),
            ("sequential reuse", 1, True),
            (f"{args.clients} clients new-conn", args.clients, False),
            (f"{args.clients} clients reuse", args.clients, True),
        ):
            rps, p50, p99 = _run(port, clients, args.requests // clients, reuse)
            print(f"{label:<24}{rps:>10.1f}{p50:>10.2f}{p99:>10.2f}")


if __name__ == "__main__":
    main()
