#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""N-document overlap proof for one headless LOBackend.

Two or more agent threads share one ``LOBackend`` / one soffice. Each thread
owns one Writer document (``_lo_docs`` is keyed by the caller id stashed in
``LOBackend.call``). UNO runs only inside ``call``, on ``_lo_thread``. The
sleep that stands in for an LLM wait stays on the agent thread, outside
``call``, so those waits can overlap while UNO stays serial.

No LoLane rewrite, no second process, no OpenRouter. Headless only — a green
run says nothing about headed AFC.

Usage (from the repo root):

  .venv/bin/python scripts/prompt_optimization/prove_lo_multi_doc.py
  .venv/bin/python scripts/prompt_optimization/prove_lo_multi_doc.py --n 4
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import tools_lo
from tools_lo import LOBackend, _caller_tid, _lo_docs

# Stands in for request_with_tools. Long enough that a late sleeper still
# overlaps the others after the post-write barrier, short enough to stay a proof.
SLEEP_SECONDS = 0.5
N_MIN = 2
N_MAX = 5
_PRINT_LOCK = threading.Lock()
_T0 = time.perf_counter()


@dataclass(frozen=True)
class Span:
    """One timed region. ``start``/``end`` are ``time.perf_counter`` values."""

    label: str
    start: float
    end: float
    thread_id: int


@dataclass(frozen=True)
class WorkerResult:
    slot: int
    thread_id: int
    token_a: str
    token_b: str
    readback: str


def _log(msg: str) -> None:
    wall = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    mono = time.perf_counter() - _T0
    with _PRINT_LOCK:
        print(f"[{wall} +{mono:7.3f}s] {msg}", flush=True)


def _tokens(slot: int) -> tuple[str, str]:
    # Fixed width so slot 1 is not a substring of slot 10 if the cap is raised.
    return (f"WAPROOF-{slot:02d}-T1", f"WAPROOF-{slot:02d}-T2")


def _writer_get_string(doc: Any) -> str:
    """Same cursor read as ``tools_lo.get_content`` (Writer ``getString`` via a cursor)."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    return cursor.getString()


def sleep_overlap_failures(spans: list[Span]) -> list[str]:
    """Fail unless every sleep shares one positive-duration window.

    Same shape as the research note: each start is before every other end.
    For intervals that is ``max(start) < min(end)``.
    """
    if len(spans) < 2:
        return [f"need at least 2 sleep spans, got {len(spans)}"]
    latest_start = max(span.start for span in spans)
    earliest_end = min(span.end for span in spans)
    if latest_start < earliest_end:
        return []
    detail = ", ".join(f"{span.label}[{span.start:.4f},{span.end:.4f}]" for span in spans)
    return [f"sleeps do not overlap (each start must be before every other end): {detail}"]


def uno_serial_failures(spans: list[Span], lo_ident: int) -> list[str]:
    """Fail if queued UNO bodies overlap or run off ``_lo_thread``."""
    if not spans:
        return ["no UNO enter/exit spans were logged"]
    failures: list[str] = []
    for span in spans:
        if span.thread_id != lo_ident:
            failures.append(f"UNO section {span.label} ran on thread {span.thread_id}, not _lo_thread {lo_ident}")
        if span.end < span.start:
            failures.append(f"UNO section {span.label} ended before it started")
    ordered = sorted(spans, key=lambda span: (span.start, span.end, span.label))
    for prev, nxt in zip(ordered, ordered[1:]):
        # Touching at an endpoint is serial. Any earlier start of nxt is overlap.
        if nxt.start < prev.end:
            failures.append(
                f"UNO sections overlap: {prev.label} [{prev.start:.6f},{prev.end:.6f}] "
                f"and {nxt.label} [{nxt.start:.6f},{nxt.end:.6f}]"
            )
    return failures


def document_isolation_failures(results: list[WorkerResult]) -> list[str]:
    """Each read-back is exactly that worker's two tokens, nothing else."""
    if len(results) < 2:
        return [f"need at least 2 document read-backs, got {len(results)}"]
    failures: list[str] = []
    by_slot = {result.slot: result for result in results}
    if len(by_slot) != len(results):
        failures.append("duplicate worker slots in read-backs")
    for result in results:
        expected = [result.token_a, result.token_b]
        # splitlines drops a trailing newline Writer may keep; the tokens stay whole lines.
        got = result.readback.splitlines()
        if got != expected:
            failures.append(f"doc slot {result.slot} read-back {result.readback!r} != {expected!r}")
        for other in results:
            if other.slot == result.slot:
                continue
            if other.token_a in result.readback or other.token_b in result.readback:
                failures.append(f"doc slot {result.slot} contains slot {other.slot} token (cross-talk)")
    return failures


def doc_map_failures(n_docs: int, keys: tuple[int, ...], worker_ids: set[int], n: int) -> list[str]:
    """After every first write, ``_lo_docs`` holds one live slot per agent thread."""
    failures: list[str] = []
    if n_docs != n:
        failures.append(f"len(_lo_docs)={n_docs}, expected {n}")
    if set(keys) != set(worker_ids) or len(keys) != n:
        failures.append(f"_lo_docs keys {list(keys)} != worker thread ids {sorted(worker_ids)}")
    return failures


def sleep_off_lo_thread_failures(spans: list[Span], lo_ident: int) -> list[str]:
    """The simulated LLM wait must not run on the UNO thread."""
    failures: list[str] = []
    for span in spans:
        if span.thread_id == lo_ident:
            failures.append(f"sleep {span.label} ran on _lo_thread; it must stay outside LOBackend.call")
        if span.end - span.start < SLEEP_SECONDS * 0.5:
            failures.append(f"sleep {span.label} was shorter than expected ({span.end - span.start:.3f}s)")
    return failures


class _Run:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.failures: list[str] = []
        self.uno: list[Span] = []
        self.sleeps: list[Span] = []
        self.results: list[WorkerResult] = []
        self.worker_ids: dict[int, int] = {}
        self.doc_count: int | None = None
        self.doc_keys: tuple[int, ...] = ()

    def fail(self, msg: str) -> None:
        with self.lock:
            self.failures.append(msg)
        _log(f"FAIL {msg}")


_T = TypeVar("_T")


def _lo_section(state: _Run, label: str, func: Callable[[], _T]) -> _T:
    """Run ``func`` on ``_lo_thread`` and log enter/exit around that body.

    Timestamps are taken inside the queued closure. Logging around the
    ``LOBackend.call`` return on the agent would include queue wait, and those
    waits overlap even when the UNO bodies do not.
    """

    def _body():
        ident = threading.get_ident()
        caller = _caller_tid()
        start = time.perf_counter()
        _log(f"UNO enter {label} lo_thread={ident} caller_tid={caller}")
        try:
            return func()
        finally:
            end = time.perf_counter()
            _log(f"UNO exit  {label} lo_thread={ident} caller_tid={caller}")
            with state.lock:
                state.uno.append(Span(label, start, end, ident))

    return LOBackend.call(_body)


def _snapshot_doc_map(state: _Run, n: int) -> None:
    """Barrier action: both (all) first writes have returned; nobody is in UNO."""

    def _read():
        keys = tuple(sorted(_lo_docs))
        return len(_lo_docs), keys

    count, keys = _lo_section(state, "doc-map", _read)
    state.doc_count = count
    state.doc_keys = keys
    _log(f"doc-map len(_lo_docs)={count} keys={list(keys)} (expect {n})")


def _worker(state: _Run, slot: int, n: int, phase1: threading.Barrier) -> None:
    token_a, token_b = _tokens(slot)
    ident = threading.get_ident()
    with state.lock:
        state.worker_ids[slot] = ident
    passed_barrier = False
    try:
        def _write_first() -> None:
            doc = LOBackend.acquire_document("writer")
            doc.getText().setString(token_a)

        _lo_section(state, f"w{slot}-write1", _write_first)
        phase1.wait(timeout=120)
        passed_barrier = True

        # Outside LOBackend.call on purpose: this is the overlapped non-UNO wait.
        start = time.perf_counter()
        _log(f"SLEEP enter w{slot} thread={ident} seconds={SLEEP_SECONDS}")
        time.sleep(SLEEP_SECONDS)
        end = time.perf_counter()
        _log(f"SLEEP exit  w{slot} thread={ident}")
        with state.lock:
            state.sleeps.append(Span(f"w{slot}-sleep", start, end, ident))

        def _write_second() -> str:
            doc = LOBackend.acquire_document("writer")
            current = _writer_get_string(doc)
            if current != token_a:
                raise RuntimeError(f"w{slot} doc changed during sleep: {current!r}")
            doc.getText().setString(token_a + "\n" + token_b)
            return _writer_get_string(doc)

        written = _lo_section(state, f"w{slot}-write2", _write_second)

        def _read_back() -> str:
            doc = LOBackend.acquire_document("writer")
            return _writer_get_string(doc)

        readback = _lo_section(state, f"w{slot}-readback", _read_back)
        if readback != written:
            state.fail(f"w{slot} read-back {readback!r} != write2 result {written!r}")
        with state.lock:
            state.results.append(WorkerResult(slot, ident, token_a, token_b, readback))
        _log(f"w{slot} read-back {readback!r}")
    except threading.BrokenBarrierError as exc:
        state.fail(f"w{slot} barrier broken before sleep ({n} workers): {exc}")
    except Exception as exc:
        if not passed_barrier:
            phase1.abort()
        state.fail(f"w{slot} raised {type(exc).__name__}: {exc}")
        _log(traceback.format_exc().rstrip())


def _evaluate(state: _Run, n: int) -> list[str]:
    failures = list(state.failures)
    lo_thread = tools_lo._lo_thread
    lo_ident = getattr(lo_thread, "ident", None)
    if not isinstance(lo_ident, int):
        failures.append("_lo_thread is not running")
        return failures
    failures.extend(sleep_overlap_failures(state.sleeps))
    failures.extend(sleep_off_lo_thread_failures(state.sleeps, lo_ident))
    failures.extend(uno_serial_failures(state.uno, lo_ident))
    if state.doc_count is None:
        failures.append("len(_lo_docs) was not read after the first writes")
    else:
        failures.extend(doc_map_failures(state.doc_count, state.doc_keys, set(state.worker_ids.values()), n))
    failures.extend(document_isolation_failures(state.results))
    if len(state.results) != n:
        failures.append(f"got {len(state.results)} read-backs, expected {n}")
    return failures


def run_proof(n: int) -> list[str]:
    state = _Run()
    phase1 = threading.Barrier(n, action=lambda: _snapshot_doc_map(state, n))
    threads = [
        threading.Thread(target=_worker, name=f"wa-proof-{slot}", args=(state, slot, n, phase1), daemon=True)
        for slot in range(n)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
        if thread.is_alive():
            state.fail(f"{thread.name} still running after 180s")
    return _evaluate(state, n)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prove N Writer docs can share one headless LOBackend while non-UNO waits overlap.")
    parser.add_argument(
        "--n",
        type=int,
        default=N_MIN,
        help=f"Agent threads, each with its own Writer document (default {N_MIN}, allowed {N_MIN}..{N_MAX}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.n < N_MIN or args.n > N_MAX:
        _log(f"FAIL --n must be {N_MIN}..{N_MAX}, got {args.n}")
        return 1
    # QueueExecutor would run marshalled UNO on the caller if this is set.
    if os.environ.get("WRITERAGENT_TESTING"):
        _log("FAIL WRITERAGENT_TESTING must stay unset (it inlines UNO on the caller thread)")
        return 1

    _log(f"start n={args.n} sleep={SLEEP_SECONDS}s one LOBackend, no API")
    LOBackend.start()
    try:
        lo_thread = tools_lo._lo_thread
        _log(f"LOBackend up _lo_thread={getattr(lo_thread, 'ident', None)}")
        failures = run_proof(args.n)
    finally:
        LOBackend.stop()
        _log("LOBackend stopped")

    if failures:
        # Workers already printed their own FAIL lines. Repeat the criterion list once.
        seen: list[str] = []
        for msg in failures:
            if msg not in seen:
                seen.append(msg)
                _log(f"FAIL {msg}")
        _log(f"FAIL {len(seen)} criterion(s) n={args.n}")
        return 1
    _log(f"PASS n={args.n} sleeps overlapped, documents isolated, UNO serial on _lo_thread")
    return 0


if __name__ == "__main__":
    sys.exit(main())
