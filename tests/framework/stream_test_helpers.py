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
    job_done: list[bool],
    applied: list[str],
) -> None:
    """Convenience helper to start an event-driven drain loop with RecordingRearm."""
    from plugin.framework.async_stream import run_stream_drain_loop

    run_stream_drain_loop(
        q,
        DummyToolkit(),
        job_done,
        lambda text, _thinking: applied.append(text),
        on_stream_done=lambda _item: True,
        on_stopped=lambda: None,
        on_error=lambda _e: None,
        rearm=rearm,
    )
