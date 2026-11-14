# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared test helpers and mocks for async_stream tests."""

from __future__ import annotations

import queue
from typing import Any


class DummyToolkit:
    """Mock toolkit for tracking VCL idle event loops."""

    def __init__(self) -> None:
        self.idle_calls: int = 0

    def processEventsToIdle(self) -> None:
        self.idle_calls += 1


class _RecordingRearm:
    """Scheduler that records slices. ``post`` does not run them inline."""

    def __init__(self) -> None:
        self.pending: list[tuple[str, float | None, Any]] = []

    def post(self, fn: Any) -> None:
        self.pending.append(("now", None, fn))

    def close(self) -> None:
        pass

    def post_after(self, delay: float, fn: Any) -> None:
        self.pending.append(("later", delay, fn))

    def pump(self) -> tuple[str, float | None]:
        kind, delay, fn = self.pending.pop(0)
        fn()
        return kind, delay


def _clear_event_drain() -> None:
    """Reset the global event drain session and sentry state."""
    from plugin.framework import async_stream as stream_mod
    from plugin.framework.async_drain_guard import reset_sentry_state

    session = stream_mod._event_drain
    if session is not None and not session.closed:
        session._finish()
    stream_mod._event_drain = None
    reset_sentry_state()


def _start_event_drain(
    q: queue.Queue[Any],
    rearm: _RecordingRearm,
    outcome: Any,
    applied: list[str],
) -> None:
    """Convenience helper to start an event-driven drain loop with RecordingRearm."""
    from plugin.framework.async_stream import run_stream_drain_loop

    run_stream_drain_loop(
        q,
        DummyToolkit(),
        outcome,
        lambda text, _thinking: applied.append(text),
        on_stream_done=lambda _item: True,
        on_stopped=lambda: None,
        on_error=lambda _e: None,
        rearm=rearm,
    )


def _drain(
    items: list[Any],
    *,
    toolkit: Any = None,
    on_error: Any = None,
    on_stopped: Any = None,
    on_stream_done: Any = None,
    cancel: Any = None,
    flush_pending: Any = None,
) -> tuple[Any, list[tuple[str, bool]], list[object], list[bool], list[object]]:
    """Run ``run_stream_drain_loop`` over pre-populated ``items`` and return collected outcomes."""
    from plugin.framework.async_stream import DrainOutcome, run_stream_drain_loop

    q: queue.Queue[Any] = queue.Queue()
    for item in items:
        q.put(item)
    applied: list[tuple[str, bool]] = []
    errors: list[object] = []
    stopped: list[bool] = []
    done: list[object] = []
    outcome = DrainOutcome()

    def _default_error(e: object) -> Any:
        errors.append(e)
        return None

    def _default_stopped() -> None:
        stopped.append(True)

    def _default_done(item: object) -> bool:
        done.append(item)
        return True

    run_stream_drain_loop(
        q,
        toolkit,
        outcome,
        lambda text, is_thinking: applied.append((text, is_thinking)),
        on_stream_done=on_stream_done or _default_done,
        on_stopped=on_stopped or _default_stopped,
        on_error=on_error or _default_error,
        cancel=cancel,
        flush_pending=flush_pending,
    )
    return outcome, applied, errors, stopped, done
