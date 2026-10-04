#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""N=2 flag-shaped sketch for the headless current-component pin.

One ``LOBackend``, two Writer documents, no OpenRouter, no second soffice.
Thread A, inside ``LOBackend.call``, inserts one named shape through
``host_rpc.execute_tool`` — the ``get_active_document(get_ctx())``
lookup a venv ``wa.shape`` RPC uses when the script did not pin a
document. Chat ``run_venv_python_script`` pins ``ctx.doc`` instead.
Thread B, while A is in the non-UNO
sleep that stands in for an LLM wait, ``setString``s its own document.

Pass: B's text is intact, A's exported ``.odt`` contains the shape name,
B's ``.odt`` does not. This does not take the warm-venv ``_io_lock``;
two real flag scripts still run one after another.

Usage (from the repo root; ``make manifest`` once so ``plugin/_manifest.py`` exists):

  .venv/bin/python scripts/prompt_optimization/prove_lo_flag_pin.py

Do not set ``WRITERAGENT_TESTING=1``. A green run is headless only.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import tools_lo
from prove_lo_multi_doc import Span, uno_serial_failures
from tools_lo import LOBackend, export_writer_odt

# A stays asleep long enough for B's setString to land inside that window.
SLEEP_A_SECONDS = 0.8
# After A has entered the sleep, B waits this long so the write cannot
# start before A's span, then calls LOBackend.
SLEEP_B_AFTER_A_SECONDS = 0.05
TOKEN_A = "WAPROOF-FLAG-A"
TOKEN_B = "WAPROOF-FLAG-B"
SHAPE_NAME = "wa-flag-pin-shape"
_PRINT_LOCK = threading.Lock()
_T0 = time.perf_counter()


def _log(msg: str) -> None:
    wall = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    mono = time.perf_counter() - _T0
    with _PRINT_LOCK:
        print(f"[{wall} +{mono:7.3f}s] {msg}", flush=True)


def odt_contains_shape_name(path: str, shape_name: str) -> bool:
    """True when ``content.xml`` names a draw object ``shape_name``.

    Writer stores a created rectangle as ``draw:name`` on the draw element.
    Geometry scoring reads this same zip entry.
    """
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("content.xml").decode("utf-8")
    return f'draw:name="{shape_name}"' in xml or f"draw:name='{shape_name}'" in xml


def _text_lines(text: str) -> list[str]:
    return text.splitlines()


def b_write_during_a_sleep_failures(a_sleep: Span | None, b_write: Span | None) -> list[str]:
    """B's queued ``setString`` must start while A is in the non-UNO sleep."""
    if a_sleep is None or b_write is None:
        return ["missing A sleep span or B setString span"]
    if a_sleep.start <= b_write.start < a_sleep.end:
        return []
    return [
        f"B setString [{b_write.start:.4f},{b_write.end:.4f}] did not start during "
        f"A sleep [{a_sleep.start:.4f},{a_sleep.end:.4f}]"
    ]


def flag_document_failures(
    *,
    a_text: str,
    b_text: str,
    a_has_shape: bool,
    b_has_shape: bool,
) -> list[str]:
    """B keeps its string; the named shape is only in A's exported ``.odt``."""
    failures: list[str] = []
    if _text_lines(a_text) != [TOKEN_A]:
        failures.append(f"A text {a_text!r} != {TOKEN_A!r}")
    if _text_lines(b_text) != [TOKEN_B]:
        failures.append(f"B text {b_text!r} != {TOKEN_B!r}")
    if TOKEN_B in a_text:
        failures.append("A text contains B's token (cross-write)")
    if TOKEN_A in b_text:
        failures.append("B text contains A's token (cross-write)")
    if not a_has_shape:
        failures.append(f"A .odt does not contain shape {SHAPE_NAME!r}")
    if b_has_shape:
        failures.append(f"B .odt contains shape {SHAPE_NAME!r} (cross-write)")
    return failures


def _writer_get_string(doc: Any) -> str:
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    return cursor.getString()


def _same_document(left: Any, right: Any) -> bool:
    from plugin.framework.thread_guard import _unwrap_uno

    raw_left = _unwrap_uno(left)
    raw_right = _unwrap_uno(right)
    if raw_left is raw_right:
        return True
    try:
        left_uid = str(getattr(raw_left, "RuntimeUID", "") or "")
        right_uid = str(getattr(raw_right, "RuntimeUID", "") or "")
    except Exception:
        return False
    return bool(left_uid) and left_uid == right_uid


def _insert_named_shape() -> None:
    """Host-RPC shape insert: the active document must already be this caller.

    ``execute_tool`` builds its own ``ToolContext`` from
    ``get_active_document(get_ctx())``. The pin has to make that the
    caller's Writer doc before this runs. This is the venv ``wa.shape``
    lookup without holding ``PythonWorkerManager`` ``_io_lock``.
    """
    from plugin.framework.thread_guard import _unwrap_uno
    from plugin.framework.uno_context import get_active_document, get_ctx
    from plugin.scripting.host_rpc import execute_tool

    uno_ctx = get_ctx()
    if _unwrap_uno(uno_ctx) is not tools_lo._lo_ctx:
        raise RuntimeError("get_ctx() is not the remote LOBackend context")
    active = get_active_document(uno_ctx)
    own = LOBackend.acquire_document("writer")
    if active is None or not _same_document(active, own):
        raise RuntimeError("get_active_document(get_ctx()) is not the caller Writer document")
    result = execute_tool(
        "shape_upsert",
        {
            "action": "create",
            "shape_type": "rectangle",
            "name": SHAPE_NAME,
            "x": 1000,
            "y": 1000,
            "width": 2000,
            "height": 1000,
        },
    )
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise RuntimeError(f"shape_upsert via host_rpc failed: {result!r}")


class _Run:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.failures: list[str] = []
        self.uno: list[Span] = []
        self.a_sleep: Span | None = None
        self.b_write: Span | None = None
        self.a_text = ""
        self.b_text = ""
        self.a_odt: str | None = None
        self.b_odt: str | None = None

    def fail(self, msg: str) -> None:
        with self.lock:
            self.failures.append(msg)
        _log(f"FAIL {msg}")


_T = TypeVar("_T")


def _lo_section(state: _Run, label: str, func: Callable[[], _T]) -> _T:
    """Run ``func`` on ``_lo_thread`` and time the body, not the queue wait."""

    def _body() -> _T:
        ident = threading.get_ident()
        start = time.perf_counter()
        _log(f"UNO enter {label} lo_thread={ident}")
        try:
            return func()
        finally:
            end = time.perf_counter()
            _log(f"UNO exit  {label} lo_thread={ident}")
            with state.lock:
                state.uno.append(Span(label, start, end, ident))

    return LOBackend.call(_body)


def _worker_a(state: _Run, docs_ready: threading.Barrier, a_asleep: threading.Event) -> None:
    def _open() -> None:
        doc = LOBackend.acquire_document("writer")
        doc.getText().setString(TOKEN_A)

    _lo_section(state, "A-open", _open)
    docs_ready.wait(timeout=120)
    start = time.perf_counter()
    _log(f"SLEEP enter A seconds={SLEEP_A_SECONDS}")
    a_asleep.set()
    time.sleep(SLEEP_A_SECONDS)
    end = time.perf_counter()
    _log("SLEEP exit  A")
    with state.lock:
        state.a_sleep = Span("A-sleep", start, end, threading.get_ident())

    _lo_section(state, "A-shape", _insert_named_shape)

    def _read() -> str:
        return _writer_get_string(LOBackend.acquire_document("writer"))

    state.a_text = _lo_section(state, "A-read", _read)
    state.a_odt = export_writer_odt()
    _log(f"A text {state.a_text!r} odt {state.a_odt}")


def _worker_b(state: _Run, docs_ready: threading.Barrier, a_asleep: threading.Event) -> None:
    def _open() -> None:
        LOBackend.acquire_document("writer")

    _lo_section(state, "B-open", _open)
    docs_ready.wait(timeout=120)
    if not a_asleep.wait(timeout=30):
        state.fail("B timed out waiting for A to sleep")
        return
    time.sleep(SLEEP_B_AFTER_A_SECONDS)

    def _write() -> str:
        doc = LOBackend.acquire_document("writer")
        doc.getText().setString(TOKEN_B)
        return _writer_get_string(doc)

    def _timed_write() -> str:
        ident = threading.get_ident()
        start = time.perf_counter()
        _log(f"UNO enter B-setString lo_thread={ident}")
        try:
            return _write()
        finally:
            end = time.perf_counter()
            _log(f"UNO exit  B-setString lo_thread={ident}")
            span = Span("B-setString", start, end, ident)
            with state.lock:
                state.uno.append(span)
                state.b_write = span

    written = LOBackend.call(_timed_write)
    if _text_lines(written) != [TOKEN_B]:
        state.fail(f"B setString read-back {written!r} != {TOKEN_B!r}")
    state.b_text = written


def _reread_b(state: _Run) -> None:
    """B's string after A's shape task has returned. Must run on B's thread."""

    def _read() -> str:
        return _writer_get_string(LOBackend.acquire_document("writer"))

    state.b_text = _lo_section(state, "B-reread", _read)


def _run_workers() -> _Run:
    """A inserts the shape during its post-sleep UNO section. B writes during the sleep, then re-reads."""
    state = _Run()
    docs_ready = threading.Barrier(2)
    a_asleep = threading.Event()
    a_done = threading.Event()

    def _a_then_signal() -> None:
        try:
            _worker_a(state, docs_ready, a_asleep)
        except threading.BrokenBarrierError as exc:
            state.fail(f"A barrier broken: {exc}")
        except Exception as exc:
            try:
                docs_ready.abort()
            except Exception:
                pass
            state.fail(f"A raised {type(exc).__name__}: {exc}")
            _log(traceback.format_exc().rstrip())
        finally:
            a_done.set()

    def _b_then_reread() -> None:
        try:
            _worker_b(state, docs_ready, a_asleep)
        except threading.BrokenBarrierError as exc:
            state.fail(f"B barrier broken: {exc}")
        except Exception as exc:
            try:
                docs_ready.abort()
            except Exception:
                pass
            state.fail(f"B raised {type(exc).__name__}: {exc}")
            _log(traceback.format_exc().rstrip())
            return
        if not a_done.wait(timeout=180):
            state.fail("B timed out waiting for A to finish")
            return
        if state.failures:
            return
        try:
            _reread_b(state)
            state.b_odt = export_writer_odt()
            _log(f"B reread {state.b_text!r} odt {state.b_odt}")
        except Exception as exc:
            state.fail(f"B reread raised {type(exc).__name__}: {exc}")
            _log(traceback.format_exc().rstrip())

    thread_a = threading.Thread(target=_a_then_signal, name="wa-flag-a", daemon=True)
    thread_b = threading.Thread(target=_b_then_reread, name="wa-flag-b", daemon=True)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=180)
    thread_b.join(timeout=180)
    if thread_a.is_alive():
        state.fail("A still running after 180s")
    if thread_b.is_alive():
        state.fail("B still running after 180s")
    return state


def evaluate_sketch(state: _Run) -> list[str]:
    failures = list(state.failures)
    lo_thread = tools_lo._lo_thread
    lo_ident = getattr(lo_thread, "ident", None)
    if not isinstance(lo_ident, int):
        failures.append("_lo_thread is not running")
        return failures
    if state.a_sleep is not None and state.a_sleep.thread_id == lo_ident:
        failures.append("A sleep ran on _lo_thread; it must stay outside LOBackend.call")
    failures.extend(b_write_during_a_sleep_failures(state.a_sleep, state.b_write))
    failures.extend(uno_serial_failures(state.uno, lo_ident))
    a_has = False
    b_has = False
    if not state.a_odt:
        failures.append("A .odt was not exported")
    else:
        a_has = odt_contains_shape_name(state.a_odt, SHAPE_NAME)
    if not state.b_odt:
        failures.append("B .odt was not exported")
    else:
        b_has = odt_contains_shape_name(state.b_odt, SHAPE_NAME)
    failures.extend(
        flag_document_failures(
            a_text=state.a_text,
            b_text=state.b_text,
            a_has_shape=a_has,
            b_has_shape=b_has,
        )
    )
    return failures


def _delete_odts(state: _Run) -> None:
    for path in (state.a_odt, state.b_odt):
        if not path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    del argv
    if os.environ.get("WRITERAGENT_TESTING"):
        _log("FAIL WRITERAGENT_TESTING must stay unset (it inlines UNO on the caller thread)")
        return 1

    _log("start n=2 flag sketch one LOBackend, no API")
    LOBackend.start()
    state: _Run | None = None
    failures: list[str] = []
    try:
        lo_thread = tools_lo._lo_thread
        _log(f"LOBackend up _lo_thread={getattr(lo_thread, 'ident', None)}")
        state = _run_workers()
        failures = evaluate_sketch(state)
    finally:
        if state is not None:
            _delete_odts(state)
        LOBackend.stop()
        _log("LOBackend stopped")

    if failures:
        seen: list[str] = []
        for msg in failures:
            if msg not in seen:
                seen.append(msg)
                _log(f"FAIL {msg}")
        _log(f"FAIL {len(seen)} criterion(s)")
        return 1
    _log("PASS n=2 B text intact, A .odt has the shape, no cross-write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
