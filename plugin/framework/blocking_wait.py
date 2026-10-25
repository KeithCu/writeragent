# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Callers that block the caller while a chat stream can still be running.

Speech-to-text transcription pumps the UI (``pump_idle=True``) so Stop and an
in-flight stream stay alive, and does not return until transcription finishes.
Notebook cells and ``=PROMPT()`` pass ``pump_idle=False``: the caller blocks,
and this wait must not return to the VCL loop. This is not the chat stream
drain.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from plugin.framework.queue_executor import pump_main_thread_work_queue, pump_ui_idle
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)

_gc_suspend_lock = threading.Lock()
_gc_suspend_depth = 0
_gc_suspend_was_enabled = False


@dataclass(slots=True)
class _WorkerResult:
    value: Any = None
    error: BaseException | None = None


@contextlib.contextmanager
def automatic_gc_suspended() -> Iterator[None]:
    """Keep automatic cyclic GC off while the caller holds SolarMutex and waits.

    A wait that never returns to the VCL loop keeps SolarMutex. If a worker
    thread runs an automatic collection that frees a cycle holding a PyUNO
    proxy, the proxy's C++ destructor (SvxShape, SwX*) blocks in
    ``SolarMutex::doAcquire``. When the caller is waiting for that worker,
    nothing ever finishes: the 90s freeze, outside the event-driven chat
    drain. Refcount frees are unaffected; deferred collections run here on
    the caller thread, where SolarMutex is recursive.

    A module-level lock and depth counter own this state: only the caller that
    takes the depth from 0 disables GC, and only the caller that returns it to
    0 re-enables it. A nested or concurrent call never re-enables GC while
    another wait is still running, and GC ends in the state it started in.
    """
    global _gc_suspend_depth, _gc_suspend_was_enabled
    with _gc_suspend_lock:
        if _gc_suspend_depth == 0:
            _gc_suspend_was_enabled = gc.isenabled()
            gc.disable()
        _gc_suspend_depth += 1
    try:
        yield
    finally:
        with _gc_suspend_lock:
            _gc_suspend_depth -= 1
            restore = _gc_suspend_depth == 0 and _gc_suspend_was_enabled
            if restore:
                # Enable under the lock: a caller entering right after the depth hits 0
                # must see GC already on, or it records was_enabled=False and GC stays off.
                gc.enable()
        if restore:
            gc.collect()


def run_blocking_in_thread(ctx: Any, func: Any, *args: Any, pump_idle: bool = True, **kwargs: Any) -> Any:
    """
    Run a blocking function in a background thread.

    When *pump_idle* is True (default), pump UNO events on the caller thread so
    the UI stays responsive (STT). When False, wait without
    ``processEventsToIdle`` — required for Calc ``=PROMPT()`` because pumping
    inside recalc re-enters the formula engine (``#VALUE!``), and for notebook
    cell execute because ``LayoutIdle`` livelocks on documents with many
    in-flow form controls. ``=PY()`` already avoids this helper for the recalc
    reason.

    This wait is not the chat stream drain. Do not turn it into the
    event-driven slice scheduler: ``pump_idle=False`` (notebook cells,
    ``=PROMPT()``) must not return to the VCL loop, and ``pump_idle=True``
    still has to block the caller until *func* returns.

    Never runs *func* on the caller thread: a missing Toolkit used to fall back
    to a synchronous call, which blocked recalc with no worker isolation.
    """
    # crosshair: off
    q: queue.Queue[_WorkerResult] = queue.Queue()

    def worker() -> None:
        try:
            result = func(*args, **kwargs)
            q.put(_WorkerResult(value=result))
        except BaseException as e:
            q.put(_WorkerResult(error=e))

    toolkit = None
    if pump_idle:
        try:
            # get_toolkit asserts the caller is the UI thread and returns a
            # guard_uno wrapper, same as the other UNO boundaries. Building
            # the toolkit inline skipped both.
            from plugin.framework.uno_context import get_toolkit

            toolkit = get_toolkit(ctx)
        except Exception as e:
            log.warning("run_blocking_in_thread: Failed to create toolkit, waiting without pump. %s", e)
            toolkit = None

    pump_vcl = pump_idle and toolkit is not None
    pump = (lambda: pump_ui_idle(toolkit)) if pump_vcl else pump_main_thread_work_queue
    # GC off before the worker starts when this wait will not return to VCL.
    with (automatic_gc_suspended() if not pump_vcl else contextlib.nullcontext()):
        run_in_background(worker, daemon=True, name="blocking-thread", dedicated=True)
        return _wait_for_worker(q, pump)


def _wait_for_worker(q: queue.Queue[_WorkerResult], pump: Callable[[], Any]) -> Any:
    # crosshair: off

    # Do not take drain_owner_scope here: this helper may run under an active stream
    # drain. pump_ui_idle remains the owner-safe VCL pump path.
    # Always wait 0.1s, never timeout=None (#1632). With no timeout this thread
    # only wakes when the worker finishes, so it never drains the marshal
    # queue. That deadlocked when pump_idle was True and get_toolkit failed:
    # the worker sat in execute_on_main_thread waiting for this thread. Do not
    # add a flag that skips the poll in some cases; that flag caused the bug.
    while True:
        # Only the get waits on the queue. A worker that raised queue.Empty
        # must propagate: catching it here and calling get again with
        # timeout=None blocks forever, because that worker has already exited.
        try:
            result = q.get(timeout=0.1)
        except queue.Empty:
            pump()
            continue
        if result.error is not None:
            raise result.error
        return result.value

