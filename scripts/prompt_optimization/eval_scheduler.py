# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dual-lane eval scheduler: string pool overlapping one LibreOffice lane.

``--backend`` used to be run-global. Each dataset row can now declare
``backend`` (``string`` or ``lo``). ``auto`` honors that declaration.

From the moment work is submitted:

- String examples run on a thread pool (``-j`` / ``string_workers``).
- Every example that must touch UNO is queued FIFO on one lane thread.
- The lane starts before the pool waits, so LO work is in flight while
  string tasks are still running. Wall clock approaches
  ``max(string_parallel, lo_serial)``, not the sum.
- A string example never blocks on the LO queue. Only an example whose
  own backend is ``lo`` waits for its turn on that lane.

One soffice stays behind ``tools_lo._lo_thread``. This lane is the task
queue in front of it: two LO examples never run at once, even when
several model workers submit together.
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

BACKEND_STRING = "string"
BACKEND_LO = "lo"
BACKEND_AUTO = "auto"
CLI_BACKENDS = (BACKEND_STRING, BACKEND_LO, BACKEND_AUTO)


def example_task_id(example: Any) -> str:
    if isinstance(example, dict):
        return str(example.get("task_id") or "")
    return str(getattr(example, "task_id", "") or "")


def declared_backend(example: Any) -> str:
    """Per-example backend. Missing field stays ``string`` (the original 17)."""
    if isinstance(example, dict):
        raw = example.get("backend")
    else:
        raw = getattr(example, "backend", None)
    if raw in (BACKEND_STRING, BACKEND_LO):
        return str(raw)
    return BACKEND_STRING


def resolve_backend(example: Any, cli_backend: str) -> str:
    """CLI ``string`` / ``lo`` force that backend. ``auto`` uses the row."""
    if cli_backend == BACKEND_AUTO:
        return declared_backend(example)
    if cli_backend == BACKEND_LO:
        return BACKEND_LO
    return BACKEND_STRING


def pack_needs_lo(examples: list[Any], cli_backend: str) -> bool:
    return any(resolve_backend(ex, cli_backend) == BACKEND_LO for ex in examples)


@dataclass
class PackSelection:
    """Examples to run, plus notes for rows the CLI mode cannot honestly run."""

    examples: list[Any]
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def select_pack(
    examples: list[Any],
    *,
    cli_backend: str,
    explicit: bool,
    student: str,
) -> PackSelection:
    """Drop rows this invocation cannot run without faking a backend.

    ``--backend string`` (the ranking default) skips ``backend=lo`` rows so
    the original string board stays the same size. Asking for that row with
    ``-e`` is an error: the string simulator has no venv/python/shapes.

    ``--student scripted`` only replays tasks that have a script. The flag
    has none — it has to be a live model on LO.
    """
    notes: list[str] = []
    rows = list(examples)
    if cli_backend == BACKEND_STRING:
        kept: list[Any] = []
        skipped: list[str] = []
        for ex in rows:
            if declared_backend(ex) == BACKEND_LO:
                skipped.append(example_task_id(ex) or "?")
            else:
                kept.append(ex)
        if skipped and explicit:
            ids = ", ".join(skipped)
            return PackSelection(
                [],
                error=(
                    f"{ids} declares backend=lo and cannot run on the string "
                    "simulator (no run_venv_python_script / domain=python). "
                    "Use --backend auto or --backend lo."
                ),
            )
        if skipped:
            notes.append(
                "Skipping "
                + ", ".join(skipped)
                + " (backend=lo). OpenRouter-only --backend string cannot run "
                "that row. Use --backend auto for the mixed pack."
            )
        rows = kept
    elif cli_backend == BACKEND_LO and not explicit:
        # =PY dest rows are string-world process oracles. Same filter as before.
        rows = [ex for ex in rows if not example_task_id(ex).startswith("py_")]

    if student == "scripted":
        from scripted_student import SCRIPTS

        kept = []
        skipped = []
        for ex in rows:
            tid = example_task_id(ex)
            if tid in SCRIPTS:
                kept.append(ex)
            else:
                skipped.append(tid or "?")
        if skipped and explicit:
            ids = ", ".join(skipped)
            return PackSelection(
                [],
                notes,
                error=(
                    f"No scripted student for {ids}. "
                    "python_shapes_flag needs a live model on headless LO: "
                    "python scripts/prompt_optimization/run_eval.py "
                    "--backend auto -e python_shapes_flag"
                ),
            )
        if skipped:
            notes.append(
                "Skipping "
                + ", ".join(skipped)
                + " (no scripted replay; live model on headless LO required)."
            )
        rows = kept
    return PackSelection(rows, notes)


class LoLane:
    """Single FIFO worker for LO-backed examples. Safe to share across models."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[Callable[[], Any], Future[Any]] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="eval-lo-lane", daemon=True)
        self._closed = False
        self._thread.start()

    def submit(self, fn: Callable[[], Any]) -> Future[Any]:
        if self._closed:
            raise RuntimeError("LO lane is closed")
        fut: Future[Any] = Future()
        self._queue.put((fn, fut))
        return fut

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, fut = item
            try:
                fut.set_result(fn())
            except Exception as exc:
                fut.set_exception(exc)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join()


def run_dual_lane(
    jobs: list[tuple[str, Callable[[], Any]]],
    *,
    string_workers: int = 1,
    lo_lane: LoLane | None = None,
) -> list[Any]:
    """Run ``jobs`` as ``(backend, fn)`` pairs. Results stay in input order.

    LO callables are queued before any string callable runs, and the lane
    thread is already alive, so the first LO job starts while string work
    is still in flight. String callables are not joined to that queue.
    """
    n = len(jobs)
    results: list[Any] = [None] * n
    lo_indexes = [i for i, (backend, _fn) in enumerate(jobs) if backend == BACKEND_LO]
    string_indexes = [i for i, (backend, _fn) in enumerate(jobs) if backend != BACKEND_LO]
    lane = lo_lane
    own_lane = False
    if lo_indexes and lane is None:
        lane = LoLane()
        own_lane = True
    lo_futs: list[tuple[int, Future[Any]]] = []
    try:
        if lane is not None:
            for index in lo_indexes:
                lo_futs.append((index, lane.submit(jobs[index][1])))
        workers = max(1, int(string_workers))
        if workers == 1:
            for index in string_indexes:
                results[index] = jobs[index][1]()
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = {pool.submit(jobs[index][1]): index for index in string_indexes}
                for fut in as_completed(pending):
                    results[pending[fut]] = fut.result()
        for index, fut in lo_futs:
            results[index] = fut.result()
    finally:
        if own_lane and lane is not None:
            lane.close()
    return results
