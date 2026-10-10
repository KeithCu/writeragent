# WriterAgent - Python Compute Service worker pool tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import pickle
import threading
from pathlib import Path

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

    def _cap_stderr_log(self) -> None:
        """Idle eviction caps real workers. This stand-in has no log file."""
        return None


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


class _RetiringSlot:
    """Worker that dies on kill and stays dead until execute asks it to respawn."""

    def __init__(self) -> None:
        self.tasks_executed = 1
        self.worker_id = 7
        self._alive = True
        self.killed = 0
        self.respawned = 0
        self.process = None

    def is_alive(self) -> bool:
        return self._alive

    def kill(self) -> None:
        self.killed += 1
        self._alive = False

    def respawn(self, timeout_sec: float = 15.0, deadline: object | None = None) -> None:
        del timeout_sec, deadline
        self.respawned += 1
        self._alive = True
        self.tasks_executed = 0


def test_max_tasks_release_leaves_slot_dead() -> None:
    """max_tasks kills the child and does not respawn it on release.

    The next lease cold-claims the dead slot. Respawning while the slot
    stayed leased made is_alive() false look like a free worker, and the
    same wrapper could end up in both _idle and _leased.
    """
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, max_tasks=1, idle_worker_ttl_sec=None)
    worker = _RetiringSlot()
    pool.workers.append(worker)  # type: ignore[arg-type]
    pool._leased.add(worker)  # type: ignore[arg-type]
    pool.release_worker(worker)  # type: ignore[arg-type]
    assert worker.killed == 1
    assert worker.respawned == 0
    assert not worker.is_alive()
    assert worker not in pool._leased
    assert worker not in pool._idle
    claimed = pool.lease_any(timeout_sec=0.2)
    assert claimed is worker
    assert worker in pool._leased
    assert worker not in pool._idle


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

    worker = BaseProcessWorker(1, "unused.py")
    worker.kill()
    assert not worker.is_alive()
    worker._shutting_down = True

    def mock_respawn(timeout_sec: float = 15.0, deadline: object | None = None) -> None:
        del timeout_sec, deadline
        raise AssertionError("respawn during shutdown")

    worker.respawn = mock_respawn  # type: ignore[assignment]
    res = worker.execute({"code": "result = 1"}, timeout_sec=1)
    assert res.get("code") == "SERVICE_SHUTDOWN"
    assert res.get("status") == "error"
    assert worker.tasks_executed == 0


def test_respawn_discards_child_when_shutdown_wins_during_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown during Popen must not leave an unpublished child running.

    What was wrong: kill() landed after the second _shutting_down check and
    before self.process = proc. Reap saw None, and the new interpreter
    survived until the parent exited.
    """
    from compute_service.worker_base import BaseProcessWorker

    original_respawn = BaseProcessWorker.respawn
    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
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
        # kill() reaps whatever is published. Nothing is, yet. The stderr
        # file opened for this spawn is unlinked with that reap.
        worker._shutting_down = True
        worker.kill()
        return proc

    monkeypatch.setattr("compute_service.worker_base.subprocess.Popen", _popen)
    worker.respawn()
    assert worker.process is None
    assert proc.killed
    assert worker._stderr_path is None


def test_execute_respawn_respects_request_deadline() -> None:
    from compute_service.worker_base import BaseProcessWorker, _Deadline

    worker = BaseProcessWorker(1, "unused.py")
    # Simulate dead process
    worker.kill()
    assert not worker.is_alive()

    respawn_left: list[float] = []

    def mock_respawn(timeout_sec: float = 15.0, deadline: _Deadline | None = None) -> None:
        # timeout_sec is the 15s startup default. execute passes the request
        # clock, and respawn takes the handshake budget from that clock
        # after Popen. Leave the child dead so execute returns
        # WORKER_SPAWN_FAILED without writing the pipe.
        del timeout_sec
        assert deadline is not None
        respawn_left.append(deadline.left())

    worker.respawn = mock_respawn  # type: ignore[method-assign]
    res = worker.execute({"code": "result = 1"}, timeout_sec=0.25)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert respawn_left == []
    # A one-second-or-longer request reaches respawn with that clock, not
    # a fresh 15s spawn budget.
    again = worker.execute({"code": "result = 1"}, timeout_sec=2.0)
    assert again.get("code") == "WORKER_SPAWN_FAILED"
    assert len(respawn_left) == 1
    assert 1.0 <= respawn_left[0] <= 2.0


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
    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.stdin = MagicMock()
    # _write_all rejects a write() that does not return a positive byte count.
    worker.process.stdin.write.side_effect = lambda data: len(data)
    worker.process.stdout = MagicMock()

    def _wait(timeout: float | None = None) -> int:
        del timeout
        # A real Popen sets returncode in wait(); poll() then sees the exit.
        worker.process.poll.return_value = -9
        return -9

    worker.process.wait.side_effect = _wait
    assert worker.tasks_executed == 0

    monkeypatch.setattr(
        "compute_service.worker_base.read_pickle_frame_with_timeout",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="worker", timeout=1.0)),
    )

    res = worker.execute({"code": "time.sleep(10)"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert worker.tasks_executed == 1
    assert worker.process is None


def test_execute_stdin_write_timeout_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged stdin write kills the child and returns.

    What was wrong: write_pickle_frame blocked while the worker lock was held.
    The request never returned, so the slot stayed leased.
    """
    import subprocess
    from unittest.mock import MagicMock
    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.stdin = MagicMock()
    worker.process.stdout = MagicMock()
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "compute_service.worker_base.write_pickle_frame_with_timeout",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="IPC frame", timeout=1.0)),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert killed == [True]
    assert worker.tasks_executed == 1


def test_partial_frame_timeout_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mid-frame deadline is EXECUTION_TIMEOUT and kills the child.

    What was wrong: ConnectionError("timeout mid-frame") fell into the
    generic handler as WORKER_CRASHED. The next read would treat the rest
    of this frame as a new response.
    """
    from unittest.mock import MagicMock
    from compute_service.worker_base import BaseProcessWorker
    from plugin.scripting.ipc import IpcPartialFrameTimeout

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.stdin = MagicMock()
    worker.process.stdin.write.side_effect = lambda data: len(data)
    worker.process.stdout = MagicMock()
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "compute_service.worker_base.read_pickle_frame_with_timeout",
        MagicMock(side_effect=IpcPartialFrameTimeout("IPC frame stream desynchronized: timeout mid-frame")),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert killed == [True]
    assert worker.tasks_executed == 1


def test_execute_payload_too_large_does_not_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized frame is rejected before any byte is written, so the kernel stays."""
    from unittest.mock import MagicMock
    from compute_service.worker_base import BaseProcessWorker
    from plugin.scripting.ipc import IpcPayloadSizeError

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
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


def test_expired_budget_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spent budget must not Popen a child for a 0.01s handshake.

    What was wrong: execute floored timeout_sec at _MIN_BUDGET_SEC before
    the deadline existed, so timeout_sec <= 0 still spawned and then
    SIGKILL'd the child when the handshake could not finish.
    """
    from compute_service.worker_base import BaseProcessWorker, _Deadline

    spawned: list[float] = []

    def _respawn(self: BaseProcessWorker, timeout_sec: float = 0.0) -> None:
        del self
        spawned.append(timeout_sec)

    monkeypatch.setattr(BaseProcessWorker, "respawn", _respawn)
    worker = BaseProcessWorker(1, "unused.py", worker_name="Budget")
    worker.process = None
    # __init__ calls respawn once. Only a later execute/ensure must not.
    spawned.clear()

    res = worker.execute({"code": "result = 1"}, timeout_sec=0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert spawned == []
    assert worker.tasks_executed == 1

    # A positive budget under one second is the same refusal. It must not
    # spawn for a handshake that cannot finish.
    short = worker.execute({"code": "result = 1"}, timeout_sec=0.5)
    assert short.get("code") == "EXECUTION_TIMEOUT"
    assert "at least 1 second" in str(short.get("error"))
    assert spawned == []
    assert worker.tasks_executed == 2

    # The dead-child branch has its own check. execute() returns before
    # that branch when the budget is already spent at entry.
    ensured = worker._ensure_live_process(_Deadline(0))
    assert isinstance(ensured, dict)
    assert ensured.get("code") == "EXECUTION_TIMEOUT"
    ensured_short = worker._ensure_live_process(_Deadline(0.5))
    assert isinstance(ensured_short, dict)
    assert ensured_short.get("code") == "EXECUTION_TIMEOUT"
    assert spawned == []
    assert worker.tasks_executed == 4


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



def test_evict_idle_workers_drops_last_active() -> None:
    """Idle eviction drops the timestamp, same as pruning a dead idle pid.

    The next idle writes a new stamp. Leaving the old one would look
    already expired if the slot were re-idled without that write.
    """
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=3600.0)
    worker = _Slot()
    try:
        with pool._cond:
            pool._idle[worker] = None  # type: ignore[index]
            pool._worker_last_active[worker] = 0.0  # type: ignore[index]
        pool._evict_idle_workers()
        assert worker.killed == 1
        assert worker not in pool._idle
        assert worker not in pool._worker_last_active
    finally:
        pool.shutdown()


def test_spawn_failure_increments_tasks_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slot that never becomes live still counts toward max_tasks."""
    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    worker.process = None
    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "WORKER_SPAWN_FAILED"
    assert worker.tasks_executed == 1


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("pipe", "WORKER_PIPE_BROKEN"),
        ("empty", "EMPTY_RESPONSE"),
        ("crash", "WORKER_CRASHED"),
    ],
)
def test_failed_request_increments_tasks_executed(monkeypatch: pytest.MonkeyPatch, kind: str, code: str) -> None:
    """Pipe, empty, and crash failures count toward max_tasks.

    What was wrong: only timeouts incremented tasks_executed, so a slot
    that kept crashing never reached recycle.
    """
    from unittest.mock import MagicMock

    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.pid = 3
    worker.process.stdin = MagicMock()
    worker.process.stdin.write.side_effect = lambda data: len(data)
    worker.process.stdout = MagicMock()
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]

    if kind == "pipe":
        monkeypatch.setattr(
            "compute_service.worker_base.write_pickle_frame_with_timeout",
            MagicMock(side_effect=BrokenPipeError("closed")),
        )
    elif kind == "empty":
        monkeypatch.setattr("compute_service.worker_base.write_pickle_frame_with_timeout", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("compute_service.worker_base.read_pickle_frame_with_timeout", lambda *_args, **_kwargs: None)
    else:
        monkeypatch.setattr("compute_service.worker_base.write_pickle_frame_with_timeout", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            "compute_service.worker_base.read_pickle_frame_with_timeout",
            MagicMock(side_effect=RuntimeError("died")),
        )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == code
    assert worker.tasks_executed == 1
    assert killed == [True]


def test_subsecond_timeout_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A budget under one second is refused and is not reported as 0 seconds."""
    from unittest.mock import MagicMock
    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 11
    worker.process = proc
    res = worker.execute({"code": "result = 1"}, timeout_sec=0.25)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert "at least 1 second" in str(res.get("error"))
    assert "0.00" not in str(res.get("error"))
    assert worker.process is proc


def _worker_without_spawn(monkeypatch: pytest.MonkeyPatch):
    """A slot whose ``__init__`` does not start a real interpreter."""
    from compute_service.worker_base import BaseProcessWorker

    original = BaseProcessWorker.respawn

    def _no_spawn(self: BaseProcessWorker, timeout_sec: float = 15.0, deadline: object | None = None) -> None:
        del self, timeout_sec, deadline

    monkeypatch.setattr(BaseProcessWorker, "respawn", _no_spawn)
    worker = BaseProcessWorker(1, "unused.py")
    monkeypatch.setattr(BaseProcessWorker, "respawn", original)
    return worker


class _DeadChild:
    """Already-exited child. ``wait`` is where a test spends the deadline."""

    def __init__(self, on_wait) -> None:
        self.pid = 9
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.on_wait = on_wait

    def poll(self) -> int:
        return 0

    def kill(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.on_wait()
        return 0


def test_reap_past_deadline_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reap that leaves under a second does not start a new child.

    What was wrong: the handshake budget was the time left before
    ``_reap_previous_process``, and that wait can block for a second.
    A longer request still called Popen after its deadline was gone.
    """
    from compute_service import worker_base

    clock = {"t": 1000.0}
    monkeypatch.setattr(worker_base.time, "monotonic", lambda: clock["t"])
    popen_calls: list[object] = []

    def _popen(*_args: object, **_kwargs: object) -> object:
        popen_calls.append(_args)
        raise AssertionError("Popen after the deadline")

    monkeypatch.setattr(worker_base.subprocess, "Popen", _popen)
    worker = _worker_without_spawn(monkeypatch)

    def _spend() -> None:
        clock["t"] += 1.5

    worker.process = _DeadChild(_spend)  # type: ignore[assignment]
    res = worker.execute({"code": "result = 1"}, timeout_sec=2.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert popen_calls == []
    assert worker.process is None


def test_handshake_budget_is_time_left_after_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ready-frame read uses the time left after the previous child is reaped."""
    from compute_service import worker_base
    from compute_service.worker_base import _Deadline

    clock = {"t": 1000.0}
    monkeypatch.setattr(worker_base.time, "monotonic", lambda: clock["t"])
    seen: list[float] = []

    class _Proc:
        pid = 4
        stdin = object()
        stdout = object()
        stderr = None

        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

    def _popen(*_args: object, **_kwargs: object) -> _Proc:
        return _Proc()

    def _read(_stdout: object, timeout_sec: float, **_kwargs: object) -> dict[str, object]:
        seen.append(timeout_sec)
        return {"status": "ready", "pid": 4}

    monkeypatch.setattr(worker_base.subprocess, "Popen", _popen)
    monkeypatch.setattr(worker_base, "read_pickle_frame_with_timeout", _read)
    monkeypatch.setattr(worker_base, "optimize_popen_pipes", lambda _proc: None)
    worker = _worker_without_spawn(monkeypatch)

    def _spend() -> None:
        clock["t"] += 0.4

    worker.process = _DeadChild(_spend)  # type: ignore[assignment]
    deadline = _Deadline(5.0)
    try:
        worker.respawn(timeout_sec=5.0, deadline=deadline)
        assert len(seen) == 1
        assert seen[0] == pytest.approx(4.6, abs=0.05)
    finally:
        worker.kill()


def test_popen_past_deadline_skips_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup that overruns the deadline kills the child and does not read a frame.

    Popen is not on the clock. A handshake after that would use the 0.01s
    pipe-wait floor and then SIGKILL.
    """
    from compute_service import worker_base

    clock = {"t": 1000.0}
    monkeypatch.setattr(worker_base.time, "monotonic", lambda: clock["t"])
    reads: list[float] = []

    class _Proc:
        def __init__(self) -> None:
            self.pid = 4
            self.stdin = object()
            self.stdout = object()
            self.stderr = None
            self.killed = False
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = -9
            return -9

    proc = _Proc()

    def _popen(*_args: object, **_kwargs: object) -> _Proc:
        clock["t"] += 3.0
        return proc

    def _read(_stdout: object, timeout_sec: float, **_kwargs: object) -> dict[str, object]:
        reads.append(timeout_sec)
        return {"status": "ready", "pid": 4}

    monkeypatch.setattr(worker_base.subprocess, "Popen", _popen)
    monkeypatch.setattr(worker_base, "read_pickle_frame_with_timeout", _read)
    monkeypatch.setattr(worker_base, "optimize_popen_pipes", lambda _proc: None)
    worker = _worker_without_spawn(monkeypatch)
    res = worker.execute({"code": "result = 1"}, timeout_sec=2.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert reads == []
    assert proc.killed
    assert worker.process is None


def test_partial_handshake_timeout_logs_timeout(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A mid-frame ready handshake is a timeout, not a generic spawn failure.

    What was wrong: IpcPartialFrameTimeout is a ConnectionError, so the
    handshake logged "Failed to spawn" and hid that the deadline fired.
    """
    import logging

    from compute_service import worker_base
    from plugin.scripting.ipc import IpcPartialFrameTimeout

    class _Proc:
        def __init__(self) -> None:
            self.pid = 7
            self.stdin = object()
            self.stdout = object()
            self.stderr = None
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = -9
            return -9

    def _read(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise IpcPartialFrameTimeout("timeout mid-frame")

    monkeypatch.setattr(worker_base.subprocess, "Popen", lambda *_args, **_kwargs: _Proc())
    monkeypatch.setattr(worker_base, "read_pickle_frame_with_timeout", _read)
    monkeypatch.setattr(worker_base, "optimize_popen_pipes", lambda _proc: None)
    worker = _worker_without_spawn(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
        worker.respawn(timeout_sec=2.0)
    assert "spawn handshake timed out" in caplog.text
    assert "Failed to spawn" not in caplog.text
    assert worker.process is None


def test_run_compute_worker_sets_identity_and_payload_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both children share the env vars and the parent pool's frame cap."""
    import os

    from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES
    from compute_service.worker_stdio import run_compute_worker

    seen: dict[str, object] = {}

    def _loop(handler: object, *, max_payload_bytes: int = 0) -> int:
        seen["handler"] = handler
        seen["cap"] = max_payload_bytes
        seen["is_worker"] = os.environ.get("WRITERAGENT_IS_WORKER")
        seen["compute"] = os.environ.get("WRITERAGENT_COMPUTE_WORKER")
        return 0

    monkeypatch.setattr("compute_service.worker_stdio.run_worker_stdio_loop", _loop)
    monkeypatch.delenv("WRITERAGENT_IS_WORKER", raising=False)
    monkeypatch.delenv("WRITERAGENT_COMPUTE_WORKER", raising=False)

    def _handler(_req: dict[str, object]) -> dict[str, str]:
        return {"status": "ok"}

    assert run_compute_worker(_handler) == 0
    assert seen["handler"] is _handler
    assert seen["cap"] == COMPUTE_MAX_PAYLOAD_BYTES
    assert seen["is_worker"] == "1"
    assert seen["compute"] == "1"


def test_reap_timeout_keeps_live_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child still alive after the reap wait is not reported as exited.

    What was wrong: wait()'s timeout was swallowed, the slot was cleared, and
    on_process_exit dropped every session on a pid SIGKILL had not reaped.
    """
    import subprocess

    from compute_service import worker_base

    class _Stuck:
        def __init__(self) -> None:
            self.pid = 42
            self.killed = False

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            raise subprocess.TimeoutExpired(cmd="stuck", timeout=1)

    worker = _worker_without_spawn(monkeypatch)
    child = _Stuck()
    exited: list[int] = []
    worker.on_process_exit = exited.append
    worker.process = child  # type: ignore[assignment]
    popped: list[object] = []

    def _popen(*_args: object, **_kwargs: object) -> object:
        popped.append(_args)
        raise AssertionError("Popen while the previous pid is still alive")

    monkeypatch.setattr(worker_base.subprocess, "Popen", _popen)
    worker.respawn()
    assert exited == []
    assert worker.process is child
    assert child.killed
    assert popped == []


def test_reap_reports_exit_after_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exit callback runs once wait() has reaped the pid."""
    worker = _worker_without_spawn(monkeypatch)

    class _Exited:
        def __init__(self) -> None:
            self.pid = 7
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

    child = _Exited()
    exited: list[int] = []
    worker.on_process_exit = exited.append
    worker.process = child  # type: ignore[assignment]
    assert worker._reap_previous_process() is True
    assert exited == [7]
    assert worker.process is None


def test_stderr_log_keeps_tail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A chatty child does not grow the stderr file without a bound.

    Opening the file ``wb`` at spawn does not help: the child appends for
    its whole life. The host keeps the last ``_STDERR_LOG_CAP`` bytes.
    """
    from compute_service.worker_base import _STDERR_LOG_CAP

    worker = _worker_without_spawn(monkeypatch)
    path = tmp_path / "w.stderr"
    path.write_bytes(b"x" * (_STDERR_LOG_CAP + 50) + b"END")
    worker._stderr_path = str(path)
    worker._cap_stderr_log()
    data = path.read_bytes()
    assert len(data) == _STDERR_LOG_CAP
    assert data.endswith(b"END")
    assert worker._stderr_snippet().endswith("END")


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(pickle.PicklingError("cannot pickle"), id="pickling"),
        pytest.param(ValueError("bad value"), id="value"),
        pytest.param(RecursionError("too deep"), id="recursion"),
    ],
)
def test_execute_unpicklable_request_does_not_kill(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """A pickle error before any byte is written leaves the child up.

    What was wrong: PicklingError escaped execute instead of an error dict.
    ValueError and RecursionError from pickle.dumps did too, and the HTTP
    handler turned them into an unhandled 500. pack_pickle_frame runs before
    the first write, so the pipe stays aligned.
    """
    from unittest.mock import MagicMock

    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 11
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    worker.process = proc
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "compute_service.worker_base.write_pickle_frame_with_timeout",
        MagicMock(side_effect=exc),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "REQUEST_NOT_SERIALIZABLE"
    assert killed == []
    assert worker.tasks_executed == 0
    assert worker.process is proc


def test_reap_timeout_caps_stderr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A child still alive after the reap wait keeps its slot, and the log stays capped.

    The idle reaper does not visit a slot that is neither idle nor leased.
    """
    import subprocess

    from compute_service.worker_base import _STDERR_LOG_CAP

    class _Stuck:
        def __init__(self) -> None:
            self.pid = 42

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            raise subprocess.TimeoutExpired(cmd="stuck", timeout=1)

    worker = _worker_without_spawn(monkeypatch)
    path = tmp_path / "stuck.stderr"
    path.write_bytes(b"a" * (_STDERR_LOG_CAP + 8) + b"TAIL")
    worker._stderr_path = str(path)
    child = _Stuck()
    worker.process = child  # type: ignore[assignment]
    assert worker._reap_previous_process() is False
    assert worker.process is child
    data = path.read_bytes()
    assert len(data) == _STDERR_LOG_CAP
    assert data.endswith(b"TAIL")


def test_idle_scan_caps_stderr_without_evicting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An idle-reaper scan caps a live child's log before idle TTL elapses."""
    import time

    from compute_service.worker_base import BaseProcessPool, _STDERR_LOG_CAP

    worker = _worker_without_spawn(monkeypatch)

    class _Live:
        def poll(self) -> None:
            return None

    worker.process = _Live()  # type: ignore[assignment]
    path = tmp_path / "idle.stderr"
    path.write_bytes(b"b" * (_STDERR_LOG_CAP + 4) + b"END")
    worker._stderr_path = str(path)
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=3600.0)
    try:
        with pool._cond:
            pool._idle[worker] = None  # type: ignore[index]
            pool._worker_last_active[worker] = time.monotonic()  # type: ignore[index]
        pool._evict_idle_workers()
        data = path.read_bytes()
        assert len(data) == _STDERR_LOG_CAP
        assert data.endswith(b"END")
        assert worker in pool._idle
    finally:
        pool.shutdown()


def test_error_dict_keeps_optional_id_and_message() -> None:
    """``id`` is present when passed, including None. ``message`` is opt-in."""
    from compute_service.worker_base import error_dict

    bare = error_dict("PAYLOAD_TOO_LARGE", "too big")
    assert bare == {"status": "error", "code": "PAYLOAD_TOO_LARGE", "error": "too big"}
    with_id = error_dict("QUEUE_TIMEOUT", "expired", req_id=None)
    assert with_id["id"] is None
    assert "message" not in with_id
    with_message = error_dict("EXECUTION_TIMEOUT", "slow", message="slow")
    assert with_message["message"] == "slow"
    assert "id" not in with_message


def test_timeout_message_keeps_fractional_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1.9s budget is not reported as 1 second. Whole seconds stay integers."""
    from compute_service.worker_base import _Deadline

    worker = _worker_without_spawn(monkeypatch)
    fractional = worker._timeout_message(_Deadline(1.9))
    assert "1.9" in fractional
    assert "1 seconds" not in fractional
    assert worker._timeout_message(_Deadline(2.0)) == "Execution exceeded maximum timeout of 2 seconds."


def test_is_alive_snapshots_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second read of process must not run. kill() clears it between the two.

    What was wrong: is_alive checked self.process, then called poll on a
    second load. Shutdown set that to None, and the write path reported
    AttributeError as REQUEST_NOT_SERIALIZABLE.
    """
    worker = _worker_without_spawn(monkeypatch)

    class _Proc:
        def poll(self) -> None:
            return None

    worker.process = _Proc()  # type: ignore[assignment]
    loads = {"n": 0}

    class _SecondReadIsNone(type(worker)):
        def __getattribute__(self, name: str) -> object:
            if name == "process":
                loads["n"] += 1
                if loads["n"] > 1:
                    return None
            return object.__getattribute__(self, name)

    worker.__class__ = _SecondReadIsNone  # type: ignore[assignment]
    assert worker.is_alive() is True
    assert loads["n"] == 1


def test_write_attribute_error_is_not_not_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    """AttributeError from the write is not REQUEST_NOT_SERIALIZABLE.

    What was wrong: the pickle handler also caught AttributeError, so a
    shutdown race on self.process never killed the child and did not count
    the task. PicklingError still maps to that code.
    """
    from unittest.mock import MagicMock

    worker = _worker_without_spawn(monkeypatch)
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 11
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    worker.process = proc
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "compute_service.worker_base.write_pickle_frame_with_timeout",
        MagicMock(side_effect=AttributeError("process")),
    )

    with pytest.raises(AttributeError, match="process"):
        worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert killed == []
    assert worker.tasks_executed == 0


def test_execute_stamps_req_id_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """req_id is set on the error dict. None still omits the key."""
    worker = _worker_without_spawn(monkeypatch)
    res = worker.execute({"code": "result = 1"}, timeout_sec=0.1, req_id="abc")
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert res.get("id") == "abc"
    assert res.get("message")
    bare = worker.execute({"code": "result = 1"}, timeout_sec=0.1)
    assert "id" not in bare


@pytest.mark.parametrize(
    ("budget", "remaining", "too_late", "child_run", "left_sec"),
    [
        (0.0, 0.0, True, 0.0, 0.01),
        (0.5, 0.5, True, 1.0, 0.5),
        (1.0, 0.0, True, 0.0, 0.01),
        (1.0, 0.5, False, 1.0, 0.5),
        (1.0, 1.0, False, 1.0, 1.0),
        (5.0, -0.25, True, 0.0, 0.01),
        (5.0, 0.5, True, 1.0, 0.5),
        (5.0, 2.5, False, 2.5, 2.5),
    ],
)
def test_deadline_budget_table(
    monkeypatch: pytest.MonkeyPatch,
    budget: float,
    remaining: float,
    too_late: bool,
    child_run: float,
    left_sec: float,
) -> None:
    """Lock the spawn / child-run / pipe-wait table. A 1s budget still starts."""
    from compute_service.worker_base import _Deadline

    now = 1_000.0
    monkeypatch.setattr("compute_service.worker_base.time.monotonic", lambda: now)
    clock = _Deadline.__new__(_Deadline)
    clock.budget_sec = budget
    clock._end = now + remaining
    assert clock.too_late_to_spawn() is too_late
    assert clock.child_run_seconds() == child_run
    assert clock.left() == left_sec


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



