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

    _cond wraps an RLock, so on_process_exit can re-enter it. kill() still
    runs outside that lock: the reap can take seconds, and holding _cond
    across it would stall every lease and release.
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

    def request_shutdown(self) -> None:
        return None

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


def test_recycle_loop_keeps_running_after_a_failed_recycle() -> None:
    """One recycle fault used to kill the daemon. Later slots stayed leased."""
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, max_tasks=1, idle_worker_ttl_sec=None)
    first = _RecyclingSlot(pool)
    second = _RecyclingSlot(pool)
    second.worker_id = 8
    pool.workers.extend([first, second])  # type: ignore[arg-type]
    pool._leased.add(first)  # type: ignore[arg-type]
    pool._leased.add(second)  # type: ignore[arg-type]

    real = pool._recycle_worker_async

    def flaky(worker: _RecyclingSlot) -> None:
        if worker is first:
            raise RuntimeError("recycle blew up")
        real(worker)

    pool._recycle_worker_async = flaky  # type: ignore[method-assign]
    # Open the pauses before enqueue so the second recycle finishes.
    for gate in (*first.gates, *second.gates):
        gate.set()
    try:
        pool._recycle_queue.put(first)  # type: ignore[arg-type]
        pool._recycle_queue.put(second)  # type: ignore[arg-type]
        claimed = pool.lease_any(timeout_sec=2.0)
        assert claimed is second
        assert pool._recycle_thread.is_alive()
    finally:
        for gate in (*first.gates, *second.gates):
            gate.set()
        pool.shutdown()


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


def test_host_unpicklers_share_allowlist_and_keep_policies() -> None:
    """Compute and scripting frames share one unpickler; the allowlists stay different."""
    import pickle

    from compute_service.worker_base import RestrictedUnpickler, unpack_restricted_pickle_frame
    from plugin.scripting.ipc import AllowlistUnpickler, _SafeUnpickler, unpack_pickle_frame

    assert issubclass(RestrictedUnpickler, AllowlistUnpickler)
    assert issubclass(_SafeUnpickler, AllowlistUnpickler)

    class Exploit:
        def __reduce__(self) -> tuple[object, tuple[str]]:
            import os

            return (os.system, ("echo pwned",))

    payload = pickle.dumps(Exploit(), protocol=5)
    with pytest.raises(ValueError, match="forbidden in compute child frames"):
        unpack_restricted_pickle_frame(payload)
    with pytest.raises(ValueError, match="is not allowed"):
        unpack_pickle_frame(payload)


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


def test_execute_does_not_respawn_during_shutdown() -> None:
    """A dead child during pool shutdown must not start a new interpreter.

    What was wrong: execute held the worker lock, saw a dead process, and
    called respawn after shutdown had begun. The new child ran the cell
    and was then killed.
    """
    from compute_service.worker_base import BaseProcessWorker

    worker = BaseProcessWorker(1, "unused.py", recover_on_timeout=False)
    worker.kill()
    assert not worker.is_alive()
    worker._shutting_down = True

    def mock_respawn(timeout_sec: float = 15.0) -> None:
        del timeout_sec
        raise AssertionError("respawn during shutdown")

    worker.respawn = mock_respawn  # type: ignore[assignment]
    res = worker.execute({"code": "result = 1"}, timeout_sec=1)
    assert res.get("code") == "SERVICE_SHUTDOWN"
    assert res.get("status") == "error"


def test_respawn_discards_child_when_shutdown_wins_during_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown during Popen must not leave an unpublished child running.

    What was wrong: kill() landed after the second _shutting_down check and
    before self.process = proc. Reap saw None, and the new interpreter
    survived until the parent exited.
    """
    from compute_service.worker_base import BaseProcessWorker

    original_respawn = BaseProcessWorker.respawn
    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0: None)
    worker = BaseProcessWorker(1, "unused.py")
    monkeypatch.setattr(BaseProcessWorker, "respawn", original_respawn)

    class _Proc:
        def __init__(self) -> None:
            self.pid = 4242
            self.stdin = None
            self.stdout = None
            self.stderr = None
            self.killed = False

        def kill(self) -> None:
            self.killed = True

        def poll(self) -> int | None:
            return 0 if self.killed else None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    proc = _Proc()

    def _popen(*_args: object, **_kwargs: object) -> _Proc:
        # The window after the unlocked check: shutdown sets the flag and
        # kill() reaps whatever is published. Nothing is, yet.
        worker._shutting_down = True
        worker.kill()
        return proc

    drains: list[object] = []
    monkeypatch.setattr("compute_service.worker_base.subprocess.Popen", _popen)
    monkeypatch.setattr(
        "compute_service.worker_base.start_stderr_drain",
        lambda *_args, **_kwargs: drains.append(True),
    )
    worker.respawn()
    assert worker.process is None
    assert proc.killed
    assert drains == []


def test_respawn_does_not_install_stderr_drain_after_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kill between adopt and drain install must not keep the dead child's drain.

    _adopt_spawned_process drops _lifecycle_lock before the drain starts.
    kill() in that window reaps the child and clears _stderr_drain. The
    spawn thread must not store a new drain for that process.
    """
    from compute_service.worker_base import BaseProcessWorker

    original_respawn = BaseProcessWorker.respawn
    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0: None)
    worker = BaseProcessWorker(1, "unused.py")
    monkeypatch.setattr(BaseProcessWorker, "respawn", original_respawn)

    class _Proc:
        def __init__(self) -> None:
            self.pid = 5151
            self.stdin = None
            self.stdout = None
            self.stderr = None
            self.killed = False

        def kill(self) -> None:
            self.killed = True

        def poll(self) -> int | None:
            return 0 if self.killed else None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    proc = _Proc()
    drains: list[object] = []

    def _optimize(popen: object) -> None:
        del popen
        worker.kill()

    monkeypatch.setattr("compute_service.worker_base.subprocess.Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr("compute_service.worker_base.optimize_popen_pipes", _optimize)
    monkeypatch.setattr(
        "compute_service.worker_base.start_stderr_drain",
        lambda *_args, **_kwargs: drains.append(object()) or drains[-1],
    )
    worker.respawn()
    assert worker.process is None
    assert proc.killed
    assert drains == []
    assert worker._stderr_drain is None


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


def test_set_pdeathsig() -> None:
    import sys
    from compute_service.worker_base import set_pdeathsig

    if sys.platform != "linux":
        # SIGKILL is Unix-only; reading it here raises AttributeError on Windows.
        assert set_pdeathsig() is False
    else:
        import signal

        assert set_pdeathsig(signal.SIGKILL) is True
        assert set_pdeathsig() is True


def test_run_worker_stdio_loop_oversized_result_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversized result writes RESULT_TOO_LARGE error frame without crashing the worker."""
    import io
    import sys
    import types
    from compute_service.worker_base import run_worker_stdio_loop
    from plugin.scripting.ipc import pack_pickle_frame, read_pickle_frame

    # Request 1 returns oversized bytes; Request 2 returns normal result
    req1 = {"id": "req-1", "action": "oversized"}
    req2 = {"id": "req-2", "action": "normal"}

    frame1 = pack_pickle_frame(req1)
    frame2 = pack_pickle_frame(req2)
    fake_stdin = io.BytesIO(frame1 + frame2)
    fake_stdout = io.BytesIO()

    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=fake_stdin))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=fake_stdout))

    def handler(req: dict) -> dict:
        if req["action"] == "oversized":
            # Return payload that exceeds 1024 bytes cap
            return {"id": req.get("id"), "status": "ok", "big": b"x" * 2000}
        return {"id": req.get("id"), "status": "ok", "result": 123}

    ret = run_worker_stdio_loop(handler, max_payload_bytes=1024)
    assert ret == 0

    fake_stdout.seek(0)
    ready = read_pickle_frame(fake_stdout, max_payload_bytes=1024)
    assert isinstance(ready, dict)
    assert ready["status"] == "ready"

    res1 = read_pickle_frame(fake_stdout, max_payload_bytes=1024)
    assert isinstance(res1, dict)
    assert res1["status"] == "error"
    assert res1["code"] == "RESULT_TOO_LARGE"
    assert res1["id"] == "req-1"

    res2 = read_pickle_frame(fake_stdout, max_payload_bytes=1024)
    assert isinstance(res2, dict)
    assert res2["status"] == "ok"
    assert res2["result"] == 123
    assert res2["id"] == "req-2"


def test_run_worker_stdio_loop_catches_base_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """BaseException in user code is caught and returned as error frame without killing worker."""
    import io
    import sys
    import types
    from compute_service.worker_base import run_worker_stdio_loop
    from plugin.scripting.ipc import pack_pickle_frame, read_pickle_frame

    req1 = {"id": "req-base-exc"}
    req2 = {"id": "req-normal"}
    fake_stdin = io.BytesIO(pack_pickle_frame(req1) + pack_pickle_frame(req2))
    fake_stdout = io.BytesIO()

    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=fake_stdin))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=fake_stdout))

    def handler(req: dict) -> dict:
        if req.get("id") == "req-base-exc":
            raise SystemExit("cell exited")
        return {"id": req.get("id"), "status": "ok"}

    ret = run_worker_stdio_loop(handler, max_payload_bytes=1024 * 1024)
    assert ret == 0

    fake_stdout.seek(0)
    ready = read_pickle_frame(fake_stdout)
    assert isinstance(ready, dict)
    assert ready["status"] == "ready"

    res1 = read_pickle_frame(fake_stdout)
    assert isinstance(res1, dict)
    assert res1["status"] == "error"
    assert res1["code"] == "WORKER_EXECUTION_ERROR"
    assert "cell exited" in res1["error"]
    assert res1["id"] == "req-base-exc"

    res2 = read_pickle_frame(fake_stdout)
    assert isinstance(res2, dict)
    assert res2["status"] == "ok"
    assert res2["id"] == "req-normal"


def test_execute_timeout_increments_tasks_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker timeouts increment tasks_executed so workers recycle at max_tasks."""
    import subprocess
    from unittest.mock import MagicMock
    from compute_service.worker_base import BaseProcessWorker

    # __init__ calls respawn(). A fake script path would launch a real interpreter.
    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0: None)
    worker = BaseProcessWorker(1, "unused.py", recover_on_timeout=True)
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.stdin = MagicMock()
    # _write_all rejects a write() that does not return a positive byte count.
    worker.process.stdin.write.side_effect = lambda data: len(data)
    worker.process.stdout = MagicMock()
    assert worker.tasks_executed == 0

    monkeypatch.setattr(
        "compute_service.worker_base.read_pickle_frame_with_timeout",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="worker", timeout=1.0)),
    )
    monkeypatch.setattr(worker, "_start_late_drain", MagicMock())

    res = worker.execute({"code": "time.sleep(10)"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert worker.tasks_executed == 1


def test_execute_late_drain_timeout_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Late-drain receives its own timeout budget rather than small leftover (Bug 3)."""
    import subprocess
    from unittest.mock import MagicMock
    from compute_service.worker_base import BaseProcessWorker

    # __init__ calls respawn(). A fake script path would launch a real interpreter.
    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0: None)
    worker = BaseProcessWorker(1, "unused.py", recover_on_timeout=True, default_timeout_sec=30.0)
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.stdin = MagicMock()
    # _write_all rejects a write() that does not return a positive byte count.
    worker.process.stdin.write.side_effect = lambda data: len(data)
    worker.process.stdout = MagicMock()

    drain_calls: list[float] = []
    monkeypatch.setattr(worker, "_start_late_drain", lambda t: drain_calls.append(t))
    monkeypatch.setattr(
        "compute_service.worker_base.read_pickle_frame_with_timeout",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="worker", timeout=0.01)),
    )

    # 1. With explicit drain_timeout_sec:
    worker.execute({"code": "ocr"}, timeout_sec=0.01, drain_timeout_sec=60.0)
    assert len(drain_calls) == 1
    assert drain_calls[0] == 60.0

    # 2. Without drain_timeout_sec: falls back to the original request budget.
    worker.execute({"code": "ocr"}, timeout_sec=0.01)
    assert len(drain_calls) == 2
    assert drain_calls[1] == 0.01


def test_execute_stdin_write_timeout_kills_without_late_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged stdin write kills the child and returns, even when vision would drain a late frame.

    What was wrong: write_pickle_frame blocked while the worker lock was held.
    The request never returned, so the slot stayed leased.
    """
    import subprocess
    from unittest.mock import MagicMock
    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0: None)
    worker = BaseProcessWorker(1, "unused.py", recover_on_timeout=True)
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.stdin = MagicMock()
    worker.process.stdout = MagicMock()
    killed: list[bool] = []
    drained: list[float] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    worker._start_late_drain = lambda timeout_sec: drained.append(timeout_sec)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "compute_service.worker_base.write_pickle_frame_with_timeout",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="IPC frame", timeout=1.0)),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert killed == [True]
    assert drained == []
    assert worker.tasks_executed == 1


@pytest.mark.parametrize("recover", [False, True])
def test_partial_frame_timeout_kills_without_late_drain(monkeypatch: pytest.MonkeyPatch, recover: bool) -> None:
    """A mid-frame deadline is EXECUTION_TIMEOUT and kills. Vision must not drain it.

    What was wrong: ConnectionError("timeout mid-frame") fell into the
    generic handler as WORKER_CRASHED, and recover_on_timeout never ran
    the kill that a desynced pipe needs.
    """
    from unittest.mock import MagicMock
    from compute_service.worker_base import BaseProcessWorker
    from plugin.scripting.ipc import IpcPartialFrameTimeout

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0: None)
    worker = BaseProcessWorker(1, "unused.py", recover_on_timeout=recover)
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.stdin = MagicMock()
    worker.process.stdin.write.side_effect = lambda data: len(data)
    worker.process.stdout = MagicMock()
    killed: list[bool] = []
    drained: list[float] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    worker._start_late_drain = lambda timeout_sec: drained.append(timeout_sec)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "compute_service.worker_base.read_pickle_frame_with_timeout",
        MagicMock(side_effect=IpcPartialFrameTimeout("IPC frame stream desynchronized: timeout mid-frame")),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0, drain_timeout_sec=60.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert killed == [True]
    assert drained == []
    assert worker.tasks_executed == 1


def test_execute_payload_too_large_does_not_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized frame is rejected before any byte is written, so the kernel stays."""
    from unittest.mock import MagicMock
    from compute_service.worker_base import BaseProcessWorker
    from plugin.scripting.ipc import IpcPayloadSizeError

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0: None)
    worker = BaseProcessWorker(1, "unused.py", recover_on_timeout=False)
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.stdin = MagicMock()
    worker.process.stdout = MagicMock()
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "compute_service.worker_base.write_pickle_frame_with_timeout",
        MagicMock(side_effect=IpcPayloadSizeError("too big")),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "PAYLOAD_TOO_LARGE"
    assert killed == []
    assert worker.tasks_executed == 0


def test_reaper_survives_tick_exception() -> None:
    """One bad eviction tick must not kill the reaper.

    What was wrong: _start_reaper called fn() with no handler. An OSError
    from is_alive() or join ended the daemon, and idle workers and shared
    sessions were never evicted again.
    """
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=None)
    calls = {"n": 0}
    second = threading.Event()

    def tick() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("tick failed")
        second.set()

    try:
        pool._start_reaper("test-reaper", 0.02, tick)
        assert second.wait(timeout=2.0)
        assert calls["n"] >= 2
    finally:
        pool.shutdown()


class _LiveProc:
    """Child stand-in whose poll() is None until kill(), so is_alive() is true."""

    def __init__(self) -> None:
        self.pid = 7
        self.stdin = object()
        self.stdout = object()
        self.stderr = None
        self.killed = 0
        self._dead = False

    def poll(self) -> int | None:
        return 0 if self._dead else None

    def kill(self) -> None:
        self.killed += 1
        self._dead = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self._dead = True
        return 0


def _draining_worker(monkeypatch: pytest.MonkeyPatch) -> tuple[object, _LiveProc]:
    """Worker with respawn suppressed and a live fake child."""
    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0: None)
    worker = BaseProcessWorker(1, "unused.py", recover_on_timeout=True, worker_name="Drain")
    proc = _LiveProc()
    worker.process = proc  # type: ignore[assignment]
    return worker, proc


def test_late_drain_finishes_before_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame that arrives before release_worker is consumed, then the caller releases."""
    from compute_service.worker_base import _DrainState

    worker, proc = _draining_worker(monkeypatch)
    reads: list[int] = []
    done = threading.Event()
    original = worker._complete_drain

    def _complete() -> None:
        original()
        done.set()

    worker._complete_drain = _complete  # type: ignore[method-assign]

    def _read(*_args: object, **_kwargs: object) -> dict[str, str]:
        reads.append(1)
        return {"status": "ok"}

    monkeypatch.setattr("compute_service.worker_base.read_pickle_frame_with_timeout", _read)
    worker._start_late_drain(1.0)
    assert done.wait(timeout=2.0)
    assert reads == [1]
    assert proc.killed == 0
    assert worker._drain_state == _DrainState.DRAINED
    called: list[int] = []
    assert worker.defer_release(lambda: called.append(1)) is False
    assert called == []
    assert worker._drain_state == _DrainState.IDLE


def test_release_waits_for_late_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """release_worker during the drain runs its callback once, after the frame."""
    from compute_service.worker_base import _DrainState

    worker, _proc = _draining_worker(monkeypatch)
    started = threading.Event()
    unblock = threading.Event()

    def _read(*_args: object, **_kwargs: object) -> dict[str, str]:
        started.set()
        assert unblock.wait(timeout=2.0)
        return {"status": "ok"}

    monkeypatch.setattr("compute_service.worker_base.read_pickle_frame_with_timeout", _read)
    worker._start_late_drain(1.0)
    assert started.wait(timeout=2.0)
    called: list[int] = []
    finished = threading.Event()

    def _callback() -> None:
        called.append(1)
        finished.set()

    assert worker.defer_release(_callback) is True
    assert worker._drain_state == _DrainState.RELEASE_WAIT
    unblock.set()
    assert finished.wait(timeout=2.0)
    assert called == [1]
    assert worker._drain_state == _DrainState.IDLE


def test_kill_during_late_drain_releases_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kill while the late frame is still unread releases once and does not re-idle."""
    from compute_service.worker_base import _DrainState

    worker, proc = _draining_worker(monkeypatch)
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=None)
    pool._leased.add(worker)
    started = threading.Event()
    unblock = threading.Event()

    def _read(*_args: object, **_kwargs: object) -> dict[str, str]:
        started.set()
        assert unblock.wait(timeout=2.0)
        return {"status": "ok"}

    monkeypatch.setattr("compute_service.worker_base.read_pickle_frame_with_timeout", _read)
    finishes: list[object] = []
    finished = threading.Event()
    original = pool._finish_release

    def _finish(released: object) -> None:
        finishes.append(released)
        original(released)  # type: ignore[arg-type]
        finished.set()

    pool._finish_release = _finish  # type: ignore[method-assign]
    try:
        worker._start_late_drain(1.0)
        assert started.wait(timeout=2.0)
        pool.release_worker(worker)  # type: ignore[arg-type]
        assert worker._drain_state == _DrainState.RELEASE_WAIT
        worker.kill()
        assert not worker.is_alive()
        unblock.set()
        assert finished.wait(timeout=2.0)
        assert finishes == [worker]
        assert worker not in pool._idle
        assert worker not in pool._leased
        assert proc.killed == 1
    finally:
        unblock.set()
        pool.shutdown()


def test_late_drain_base_exception_still_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """An interrupt during the late read must not leave the slot leased.

    _complete_drain runs on the way out, including BaseException. The
    interrupted read may be a partial frame, so the child is killed before
    the RELEASE_WAIT callback runs. The interrupt still propagates so the
    drain thread unwinds.
    """
    from compute_service.worker_base import _DrainState

    worker, proc = _draining_worker(monkeypatch)
    called: list[object] = []

    def _callback() -> None:
        called.append("released")

    def _read(*_args: object, **_kwargs: object) -> dict[str, str]:
        assert worker.defer_release(_callback) is True
        assert worker._drain_state == _DrainState.RELEASE_WAIT
        raise KeyboardInterrupt

    def _inline(func: object, *args: object, **_kwargs: object) -> None:
        assert callable(func)
        try:
            func(*args)
        except KeyboardInterrupt:
            called.append("unwound")

    monkeypatch.setattr("compute_service.worker_base.read_pickle_frame_with_timeout", _read)
    monkeypatch.setattr("compute_service.worker_base.run_in_background", _inline)
    worker._start_late_drain(1.0)
    assert called == ["released", "unwound"]
    assert worker._drain_state == _DrainState.IDLE
    assert proc.killed == 1


def test_subsecond_timeout_message_keeps_fraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A budget under one second must not be reported as 0 seconds."""
    import subprocess
    from unittest.mock import MagicMock
    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0: None)
    worker = BaseProcessWorker(1, "unused.py", recover_on_timeout=False)
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.pid = 11
    worker.process.stdin = MagicMock()
    worker.process.stdin.write.side_effect = lambda data: len(data)
    worker.process.stdout = MagicMock()
    monkeypatch.setattr(
        "compute_service.worker_base.read_pickle_frame_with_timeout",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="worker", timeout=0.25)),
    )
    res = worker.execute({"code": "result = 1"}, timeout_sec=0.25)
    message = str(res.get("error"))
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert "0.25" in message
    assert "0 seconds" not in message


def test_formula_worker_script_handshake() -> None:
    """The production formula script completes the ready handshake and one cell."""
    import json
    from pathlib import Path

    from compute_service.worker_base import BaseProcessWorker

    script = Path(__file__).resolve().parents[2] / "compute_service" / "formula_worker.py"
    worker = BaseProcessWorker(1, str(script), worker_name="Formula")
    try:
        assert worker.is_alive()
        res = worker.execute({"code": "result = 1"}, timeout_sec=30)
        assert res.get("status") == "ok"
        raw = res.get("result_json")
        assert isinstance(raw, (bytes, bytearray))
        assert json.loads(raw).get("result") == 1
    finally:
        worker.kill()



