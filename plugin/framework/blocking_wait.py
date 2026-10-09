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
drain. Re-exported from ``async_stream``.
"""

from __future__ import annotations

import logging
import queue
from enum import Enum
from typing import Any, Callable, TypeAlias

from plugin.framework.queue_executor import pump_ui_idle
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)


class BlockingPumpKind(str, Enum):
    """Tags for :func:`run_blocking_in_thread` queue (not the stream drain protocol)."""

    DONE = "done"
    ERROR = "error"


class BlockingWaitStopped(Exception):
    """``stop_checker`` fired while waiting for a background func (no VCL pump)."""


BlockingPumpQueueItem: TypeAlias = tuple[BlockingPumpKind, Any]


def run_blocking_in_thread(ctx: Any, func: Any, *args: Any, pump_idle: bool = True, stop_checker: Callable[[], bool] | None = None, **kwargs: Any) -> Any:
    """
    Run a blocking function in a background thread.

    When *pump_idle* is True (default), pump UNO events on the caller thread so
    the UI stays responsive (STT). When False, wait without
    ``processEventsToIdle`` — required for Calc ``=PROMPT()`` because pumping
    inside recalc re-enters the formula engine (``#VALUE!``), and for notebook
    cell execute because ``LayoutIdle`` livelocks on documents with many
    in-flow form controls. ``=PY()`` already avoids this helper for the recalc
    reason.

    *stop_checker* (notebook Stop): poll the queue with a short timeout and
    return via :class:`BlockingWaitStopped` when the predicate is true. Never
    pumps VCL for that poll — same LayoutIdle livelock as ``pump_idle=False``.

    This wait is not the chat stream drain. Do not turn it into the
    event-driven slice scheduler: ``pump_idle=False`` (notebook cells,
    ``=PROMPT()``) must not return to the VCL loop, and ``pump_idle=True``
    still has to block the caller until *func* returns.

    Never runs *func* on the caller thread: a missing Toolkit used to fall back
    to a synchronous call, which blocked recalc with no worker isolation.

    The internal queue uses :class:`BlockingPumpKind` as the first tuple
    element only (same contract as :class:`StreamQueueKind` for the stream drain).
    """
    # crosshair: off
    q: "queue.Queue[BlockingPumpQueueItem]" = queue.Queue()

    def worker() -> None:
        try:
            result = func(*args, **kwargs)
            q.put((BlockingPumpKind.DONE, result))
        except BaseException as e:
            q.put((BlockingPumpKind.ERROR, e))

    toolkit = None
    if pump_idle:
        try:
            # get_toolkit asserts the caller is the UI thread and returns a
            # guard_uno wrapper, same as the other UNO boundaries. Building
            # the toolkit inline skipped both.
            from plugin.framework.uno_context import get_toolkit

            toolkit = get_toolkit(ctx)
        except Exception as e:
            log.warning("run_blocking_with_pump: Failed to create toolkit, waiting without pump. %s", e)
            toolkit = None

    run_in_background(worker, daemon=True, name="blocking-thread", dedicated=True)

    # Do not take drain_owner_scope here: this helper may run under an active stream
    # drain. pump_ui_idle remains the owner-safe VCL pump path.
    poll = (pump_idle and toolkit is not None) or stop_checker is not None
    while True:
        # Only the get waits on the queue. A worker that raised queue.Empty
        # must propagate: catching it here and calling get again with
        # timeout=None blocks forever, because that worker has already exited.
        try:
            item = q.get(timeout=0.1 if (poll or not pump_idle) else None)
        except queue.Empty:
            if stop_checker is not None and stop_checker():
                raise BlockingWaitStopped("stopped")
            if pump_idle and toolkit is not None:
                pump_ui_idle(toolkit)
            elif not pump_idle:
                from plugin.framework.queue_executor import pump_main_thread_work_queue
                pump_main_thread_work_queue()
            continue
        kind, data = item
        if not isinstance(kind, BlockingPumpKind):
            ek = TypeError("blocking pump queue item kind must be BlockingPumpKind, got %s" % (type(kind).__name__,))
            log.error("Invalid blocking pump tag: %s", ek)
            raise ek
        if kind == BlockingPumpKind.DONE:
            return data
        if kind == BlockingPumpKind.ERROR:
            raise data

