# WriterAgent - Python Compute Service worker pool tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import threading

import pytest

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


class _RecyclingSlot:
    """Worker whose process is dead for the whole kill and respawn."""

    def __init__(self, pool: BaseProcessPool) -> None:
        self.tasks_executed = 1
        self.worker_id = 7
        self._alive = True
        self._pool = pool
        self.killed = 0
        self.respawned = 0
        self.phase = ""
        self.entered = threading.Event()
        self.gates = (threading.Event(), threading.Event())

    def is_alive(self) -> bool:
        return self._alive

    def defer_release(self, _callback: object) -> bool:
        return False

    def _pause(self, name: str) -> None:
        # on_process_exit takes the pool lock. Doing that while _finish_release
        # still holds it deadlocks; the pause is the window lease_any used to
        # cold-claim this same wrapper.
        self.phase = name
        with self._pool._cond:
            self.entered.set()
        gate = self.gates[0] if name == "kill" else self.gates[1]
        assert gate.wait(timeout=2.0)

    def kill(self) -> None:
        self.killed += 1
        self._alive = False
        self._pause("kill")

    def respawn(self) -> None:
        self._alive = False
        self._pause("respawn")
        self.respawned += 1
        self._alive = True
        self.tasks_executed = 0


def _assert_recycle_exclusive(pool: BaseProcessPool, worker: _RecyclingSlot) -> None:
    """A dead pid that is still being recycled is not a free slot."""
    assert worker.entered.wait(timeout=2.0)
    claimed = pool.lease_any(timeout_sec=0.05)
    assert claimed is None
    assert worker in pool._leased
    assert worker not in pool._idle


def test_recycle_cannot_be_leased_until_respawn_finishes() -> None:
    """max_tasks recycle used to drop _leased before kill/respawn.

    is_alive() is false for that whole stretch, so lease_any cold-claimed
    the wrapper. The next request then ran against a process that was
    exiting or not yet ready, and release could put the same wrapper in
    both _idle and _leased.
    """
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, max_tasks=1, idle_worker_ttl_sec=None)
    worker = _RecyclingSlot(pool)
    pool.workers.append(worker)  # type: ignore[arg-type]
    pool._leased.add(worker)  # type: ignore[arg-type]

    releaser = threading.Thread(target=pool.release_worker, args=(worker,), daemon=True)
    releaser.start()
    try:
        # release_worker returns immediately off the request path
        releaser.join(timeout=2.0)
        assert not releaser.is_alive()
        # Background recycle thread is active and holds exclusive lease
        _assert_recycle_exclusive(pool, worker)
        assert worker.phase == "kill"
        worker.entered.clear()
        worker.gates[0].set()
        _assert_recycle_exclusive(pool, worker)
        assert worker.phase == "respawn"
        worker.gates[1].set()
        # Wait for background recycle to complete and worker to become available
        claimed = pool.lease_any(timeout_sec=2.0)
        assert claimed is worker
        assert worker.killed == 1
        assert worker.respawned == 1
        assert worker in pool._leased
        assert worker not in pool._idle
    finally:
        worker.gates[0].set()
        worker.gates[1].set()
        releaser.join(timeout=2.0)


def test_restricted_unpickler_blocks_arbitrary_globals() -> None:
    import pickle
    import pytest
    from compute_service.worker_base import unpack_restricted_pickle_frame

    # Safe builtins
    safe_data = {"id": "123", "status": "ok", "numbers": [1, 2, 3], "bytes": b"hello", "flag": True}
    packed = pickle.dumps(safe_data, protocol=5)
    unpacked = unpack_restricted_pickle_frame(packed)
    assert unpacked == safe_data

    # Malicious or dangerous global: os.system
    class Exploit:
        def __reduce__(self):
            import os
            return (os.system, ("echo pwned",))

    dangerous = pickle.dumps(Exploit(), protocol=5)
    with pytest.raises(ValueError, match="forbidden"):
        unpack_restricted_pickle_frame(dangerous)

    # NumPy globals should also be blocked on compute child frames
    class NumpyExploit:
        def __reduce__(self):
            import numpy as np
            return (np.zeros, (5,))

    np_dangerous = pickle.dumps(NumpyExploit(), protocol=5)
    with pytest.raises(ValueError, match="forbidden"):
        unpack_restricted_pickle_frame(np_dangerous)


def test_run_worker_stdio_loop_breaks_on_decode_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import struct
    import sys
    from compute_service.worker_base import run_worker_stdio_loop

    # Provide a frame with invalid/corrupt pickle bytes
    corrupt_body = b"not-a-valid-pickle-stream"
    corrupt_frame = struct.pack("!I", len(corrupt_body)) + corrupt_body

    import types
    fake_stdin = io.BytesIO(corrupt_frame)
    fake_stdout = io.BytesIO()

    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=fake_stdin))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=fake_stdout))

    calls: list[dict] = []
    ret = run_worker_stdio_loop(lambda req: calls.append(req) or {"status": "ok"})
    # Must exit cleanly with 0 (break on decode error instead of looping desynced)
    assert ret == 0
    assert len(calls) == 0


def test_execute_respawn_respects_request_deadline() -> None:
    from compute_service.worker_base import BaseProcessWorker

    worker = BaseProcessWorker(1, "unused.py", recover_on_timeout=False)
    # Simulate dead process
    worker.kill()
    assert not worker.is_alive()

    respawn_timeouts: list[float] = []

    def mock_respawn(timeout_sec: float = 15.0) -> None:
        respawn_timeouts.append(timeout_sec)
        # Leave not alive so execute returns WORKER_SPAWN_FAILED without trying to write to pipe

    worker.respawn = mock_respawn  # type: ignore[assignment]
    res = worker.execute({"code": "result = 1"}, timeout_sec=0.25)
    assert res.get("code") == "WORKER_SPAWN_FAILED"
    assert len(respawn_timeouts) == 1
    assert respawn_timeouts[0] <= 0.25
