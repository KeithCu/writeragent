# WriterAgent - Python Compute Service worker pool tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import threading

from compute_service.worker_base import BaseProcessPool


class _Slot:
    """Stand-in worker. kill() runs the pool's process-exit callback."""

    def __init__(self) -> None:
        self.tasks_executed = 0
        self.worker_id = 1
        self.killed = 0
        self._on_kill = lambda: None

    def is_alive(self) -> bool:
        return True

    def kill(self) -> None:
        self.killed += 1
        self._on_kill()


def test_finish_release_kills_after_releasing_pool_lock() -> None:
    """Shutdown between the unlocked check and the locked block must not kill under _cond.

    _cond wraps a non-reentrant Lock. kill() reaps the child and runs
    on_process_exit, which takes that same lock to drop formula sessions.
    Killing while the release thread still holds _cond deadlocks.
    """
    dropped: list[int] = []
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=None)

    def on_exit(pid: int) -> None:
        with pool._cond:
            dropped.append(pid)

    worker = _Slot()
    worker._on_kill = lambda: on_exit(4242)
    pool._leased.add(worker)

    def flip(_worker: _Slot) -> bool:
        # The first _is_shutdown check already passed. Shutdown lands
        # before the locked block, which is the race that deadlocked.
        pool._is_shutdown = True
        return False

    pool.should_recycle_worker = flip  # type: ignore[method-assign]
    thread = threading.Thread(target=pool._finish_release, args=(worker,), daemon=True)
    thread.start()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert worker.killed == 1
    assert dropped == [4242]
    assert worker not in pool._idle
    assert worker not in pool._leased
