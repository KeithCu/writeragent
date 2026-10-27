# WriterAgent - Python Compute Service worker pool tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import pickle
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from compute_service.worker_base import BaseProcessPool
from plugin.framework.process_worker import Child


def _child(proc: object, *, worker_name: str, worker_id: int, tail: object = None) -> Child:
    from plugin.framework.managed_subprocess import ManagedSubprocess

    managed = ManagedSubprocess.wrap_existing(proc, label=f"{worker_name} #{worker_id}", stderr_tail=tail)  # type: ignore[arg-type]
    return Child(managed, worker_name=worker_name, worker_id=worker_id)


class _Slot:
    """Stand-in worker. kill() can re-enter the pool condition."""

    def __init__(self) -> None:
        self.tasks_executed = 0
        self.worker_id = 1
        self.killed = 0
        self.pending_exit = False
        self._on_kill = lambda: None

    def is_alive(self) -> bool:
        return True

    def kill(self) -> None:
        self.killed += 1
        self._on_kill()


def test_finish_release_kills_after_releasing_pool_lock() -> None:
    """Shutdown between the unlocked check and the locked block must not kill under _cond.

    kill() runs outside that lock: the reap can take seconds, and holding
    _cond across it would stall every lease and release.
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
        self.pending_exit = False
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


def test_pending_exit_kills_and_does_not_idle() -> None:
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, max_tasks=500, idle_worker_ttl_sec=None)
    worker = _Slot()
    worker.pending_exit = True
    pool.workers.append(worker)  # type: ignore[arg-type]
    pool._leased.add(worker)  # type: ignore[arg-type]
    pool.release_worker(worker)  # type: ignore[arg-type]
    assert worker.killed == 1
    assert worker not in pool._idle
    assert worker not in pool._leased
    assert worker.pending_exit is False


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


def test_recycle_kill_exception_drops_lease(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    class _FailingRecycleSlot:
        def __init__(self) -> None:
            self.tasks_executed = 1
            self.worker_id = 7
            self.pending_exit = False

        def is_alive(self) -> bool:
            return True

        def kill(self) -> None:
            raise RuntimeError("reap failed")

    pool = BaseProcessPool(script_path="unused.py", num_workers=0, max_tasks=1, idle_worker_ttl_sec=None)
    worker = _FailingRecycleSlot()
    pool.workers.append(worker)  # type: ignore[arg-type]
    pool._leased.add(worker)  # type: ignore[arg-type]
    with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
        pool.release_worker(worker)  # type: ignore[arg-type]

    assert worker not in pool._leased
    assert worker not in pool._idle
    assert "Failed to reap" in caplog.text
    assert "during recycle" in caplog.text


def test_shutdown_kill_exception_continues(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    second_killed = 0

    class _FirstSlot:
        worker_id = 1

        def request_shutdown(self) -> None:
            pass

        def kill(self) -> None:
            raise RuntimeError("first reap failed")

    class _SecondSlot:
        worker_id = 2

        def request_shutdown(self) -> None:
            pass

        def kill(self) -> None:
            nonlocal second_killed
            second_killed += 1

    pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=None)
    first = _FirstSlot()
    second = _SecondSlot()
    pool.workers.extend([first, second])  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
        pool.shutdown()

    assert second_killed == 1
    assert "during shutdown" in caplog.text
    assert pool._is_shutdown is True


def test_max_tasks_is_at_least_one() -> None:
    """A non-positive max_tasks is the same floor config already requires.

    What was wrong: max_tasks=0 made should_recycle_worker true on every
    release, because tasks_executed >= 0 is always true.
    """
    for raw in (0, -3):
        pool = BaseProcessPool(script_path="unused.py", num_workers=0, max_tasks=raw, idle_worker_ttl_sec=None)
        try:
            assert pool.max_tasks == 1
            worker = _RetiringSlot()
            worker.tasks_executed = 0
            assert not pool.should_recycle_worker(worker)  # type: ignore[arg-type]
        finally:
            pool.shutdown()


def test_singleton_discards_pool_built_during_shutdown() -> None:
    """Shutdown during factory() does not publish that pool.

    What was wrong: get() held the singleton lock across factory(), so
    shutdown blocked for every child spawn and then tore down a pool that
    had already been installed.
    """
    from compute_service.worker_base import PoolSingleton

    started = threading.Event()
    release = threading.Event()
    discarded: list[BaseProcessPool] = []

    def factory() -> BaseProcessPool:
        started.set()
        assert release.wait(timeout=2.0)
        pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=None)
        original = pool.shutdown

        def _shutdown() -> None:
            discarded.append(pool)
            original()

        pool.shutdown = _shutdown  # type: ignore[method-assign]
        return pool

    singleton: PoolSingleton[BaseProcessPool] = PoolSingleton()
    errors: list[BaseException] = []

    def _get() -> None:
        try:
            singleton.get(factory)
        except BaseException as exc:
            errors.append(exc)

    getter = threading.Thread(target=_get, daemon=True)
    getter.start()
    assert started.wait(timeout=2.0)

    def _stop() -> None:
        singleton.shutdown(permanent=True)

    stopper = threading.Thread(target=_stop, daemon=True)
    stopper.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not singleton._closed:
        time.sleep(0.01)
    assert singleton._closed
    assert stopper.is_alive()
    release.set()
    stopper.join(timeout=2.0)
    getter.join(timeout=2.0)
    assert not stopper.is_alive()
    assert not getter.is_alive()
    assert singleton._pool is None
    assert len(discarded) == 1
    assert discarded[0]._is_shutdown
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "shut down" in str(errors[0])


def test_singleton_retries_after_non_permanent_shutdown_during_build() -> None:
    """A non-permanent shutdown during factory() does not fail the builder.

    What was wrong: the in-flight builder raised "Compute pool is shut down."
    after shutdown(permanent=False) discarded its pool, while the next get
    built a replacement.
    """
    from compute_service.worker_base import PoolSingleton

    started = threading.Event()
    release = threading.Event()
    builds = {"n": 0}

    def factory() -> BaseProcessPool:
        builds["n"] += 1
        if builds["n"] == 1:
            started.set()
            assert release.wait(timeout=2.0)
        return BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=None)

    singleton: PoolSingleton[BaseProcessPool] = PoolSingleton()
    outcome: list[BaseProcessPool | BaseException] = []

    def _get() -> None:
        try:
            outcome.append(singleton.get(factory))
        except BaseException as exc:
            outcome.append(exc)

    getter = threading.Thread(target=_get, daemon=True)
    getter.start()
    assert started.wait(timeout=2.0)

    def _stop() -> None:
        singleton.shutdown(permanent=False)

    stopper = threading.Thread(target=_stop, daemon=True)
    stopper.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and singleton._epoch == 0:
        time.sleep(0.01)
    assert singleton._epoch == 1
    assert stopper.is_alive()
    release.set()
    stopper.join(timeout=2.0)
    getter.join(timeout=2.0)
    assert not stopper.is_alive()
    assert not getter.is_alive()
    assert builds["n"] == 2
    assert len(outcome) == 1
    pool = outcome[0]
    assert isinstance(pool, BaseProcessPool)
    assert pool is singleton._pool
    assert not pool._is_shutdown
    pool.shutdown()


def test_singleton_does_not_return_pool_shut_down_after_publish() -> None:
    """Permanent shutdown after publish does not hand that pool back.

    What was wrong: get() returned the pool it had just published even when
    shutdown(permanent=True) had already nulled _pool and called
    pool.shutdown(). Callers relied on execute checking _is_shutdown.
    """
    from compute_service.worker_base import PoolSingleton

    singleton: PoolSingleton[BaseProcessPool] = PoolSingleton()
    entered = threading.Event()
    release = threading.Event()
    built: list[BaseProcessPool] = []

    def factory() -> BaseProcessPool:
        pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=None)
        built.append(pool)
        return pool

    def _before_handout(pool: BaseProcessPool) -> None:
        del pool
        entered.set()
        assert release.wait(timeout=2.0)

    singleton._before_handout = _before_handout  # type: ignore[method-assign]
    outcome: list[BaseProcessPool | BaseException] = []

    def _get() -> None:
        try:
            outcome.append(singleton.get(factory))
        except BaseException as exc:
            outcome.append(exc)

    getter = threading.Thread(target=_get, daemon=True)
    getter.start()
    assert entered.wait(timeout=2.0)
    stopper = threading.Thread(target=lambda: singleton.shutdown(permanent=True), daemon=True)
    stopper.start()
    stopper.join(timeout=2.0)
    assert not stopper.is_alive()
    release.set()
    getter.join(timeout=2.0)
    assert not getter.is_alive()
    assert len(built) == 1
    assert built[0]._is_shutdown
    assert singleton._pool is None
    assert len(outcome) == 1
    assert isinstance(outcome[0], RuntimeError)
    assert "shut down" in str(outcome[0])


def test_singleton_rebuilds_after_non_permanent_shutdown_following_publish() -> None:
    """A non-permanent shutdown after publish makes get() build a replacement.

    The published pool is already being torn down. Returning it would hand
    the caller a pool whose workers shutdown() is reaping.
    """
    from compute_service.worker_base import PoolSingleton

    singleton: PoolSingleton[BaseProcessPool] = PoolSingleton()
    entered = threading.Event()
    release = threading.Event()
    built: list[BaseProcessPool] = []
    handouts = {"n": 0}

    def factory() -> BaseProcessPool:
        pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=None)
        built.append(pool)
        return pool

    def _before_handout(pool: BaseProcessPool) -> None:
        del pool
        handouts["n"] += 1
        if handouts["n"] == 1:
            entered.set()
            assert release.wait(timeout=2.0)

    singleton._before_handout = _before_handout  # type: ignore[method-assign]
    outcome: list[BaseProcessPool | BaseException] = []

    def _get() -> None:
        try:
            outcome.append(singleton.get(factory))
        except BaseException as exc:
            outcome.append(exc)

    getter = threading.Thread(target=_get, daemon=True)
    getter.start()
    assert entered.wait(timeout=2.0)
    stopper = threading.Thread(target=lambda: singleton.shutdown(permanent=False), daemon=True)
    stopper.start()
    stopper.join(timeout=2.0)
    assert not stopper.is_alive()
    release.set()
    getter.join(timeout=2.0)
    assert not getter.is_alive()
    assert len(built) == 2
    assert built[0]._is_shutdown
    assert len(outcome) == 1
    pool = outcome[0]
    assert isinstance(pool, BaseProcessPool)
    assert pool is built[1]
    assert pool is singleton._pool
    assert not pool._is_shutdown
    pool.shutdown()


def test_pool_init_kills_workers_when_a_later_spawn_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A constructor that fails mid-loop reaps the children already started.

    What was wrong: the exception left __init__ with live Popen objects
    that no pool reference kept.
    """
    from compute_service.worker_base import BaseProcessWorker

    made: list[BaseProcessWorker] = []
    killed: list[int] = []
    real_init = BaseProcessWorker.__init__

    def _init(self: BaseProcessWorker, *args: object, **kwargs: object) -> None:
        if made:
            raise RuntimeError("second worker failed")
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]
        original = self.kill

        def _kill() -> None:
            killed.append(self.worker_id)
            original()

        self.kill = _kill  # type: ignore[method-assign]
        made.append(self)

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=15.0, deadline=None: None)
    monkeypatch.setattr(BaseProcessWorker, "__init__", _init)
    with pytest.raises(RuntimeError, match="second worker failed"):
        BaseProcessPool(script_path="unused.py", num_workers=2, idle_worker_ttl_sec=None)
    assert len(made) == 1
    assert killed == [1]
    assert made[0]._shutting_down
    assert made[0].is_shutting_down()


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


@pytest.mark.parametrize(
    "payload",
    [
        b"\x80\x02cbuiltins\nbytes\n" + b"J" + struct.pack("<i", 1000) + b"\x85R.",
        b"\x80\x02cbuiltins\nbytearray\n" + b"J" + struct.pack("<i", 1000) + b"\x85R.",
        b"(cbuiltins\nbytes\nI1000\no.",
        b"(cbuiltins\nbytearray\nI1000\no.",
        b"(I1000\nibuiltins\nbytes\n.",
        b"(I1000\nibuiltins\nbytearray\n.",
        b"\x80\x02cbuiltins\nbytes\nM" + struct.pack("<H", 1000) + b"\x85\x81.",
        b"\x80\x02cbuiltins\nbytearray\nM" + struct.pack("<H", 1000) + b"\x85\x81.",
        b"\x80\x04cbuiltins\nbytes\n)\x7d\x8c\x06source" + b"M" + struct.pack("<H", 1000) + b"s\x92.",
    ],
)
def test_restricted_unpickler_rejects_sized_buffer_alloc(payload: bytes) -> None:
    """bytes(n) and bytearray(n) constructors are refused on all opcodes."""
    from compute_service.worker_base import unpack_restricted_pickle_frame

    with pytest.raises(ValueError, match="bytes allocation from an int"):
        unpack_restricted_pickle_frame(payload)


def test_restricted_unpickler_accepts_valid_bytearray() -> None:
    """Legitimate protocol 4 and 5 bytearrays are accepted."""
    import pickle
    from compute_service.worker_base import unpack_restricted_pickle_frame

    p4 = pickle.dumps(bytearray(b"xyz"), protocol=4)
    assert unpack_restricted_pickle_frame(p4) == bytearray(b"xyz")
    p5 = pickle.dumps({"b": b"hi", "ba": bytearray(b"yo")}, protocol=5)
    assert unpack_restricted_pickle_frame(p5) == {"b": b"hi", "ba": bytearray(b"yo")}


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
    before the child was published. Reap saw None, and the new interpreter
    survived until the parent exited. Adopt now refuses that child, and
    only that child is signaled.
    """
    import subprocess

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
    saw_pipe: list[bool] = []

    def _popen(*_args: object, **kwargs: object) -> _Proc:
        # The window after the unlocked check: shutdown sets the flag and
        # kill() reaps whatever is published. This child is not published
        # until Popen returns, so that kill must not see it.
        saw_pipe.append(kwargs.get("stderr") is subprocess.PIPE)
        worker._shutting_down = True
        assert worker.kill() is True
        assert worker.process is None
        return proc

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    worker.respawn()
    assert worker.process is None
    assert proc.killed
    assert saw_pipe == [True]


def test_execute_respawn_respects_request_deadline() -> None:
    from compute_service.worker_base import BaseProcessWorker, Deadline

    worker = BaseProcessWorker(1, "unused.py")
    # Simulate dead process
    worker.kill()
    assert not worker.is_alive()

    respawn_left: list[float] = []

    def mock_respawn(timeout_sec: float = 15.0, deadline: Deadline | None = None) -> None:
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


def test_execute_timeout_does_not_increment_tasks_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout does not count. Only a consumed response moves tasks_executed."""
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
        "plugin.framework.process_worker.read_pickle_frame",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="worker", timeout=1.0)),
    )

    res = worker.execute({"code": "time.sleep(10)"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert worker.tasks_executed == 0
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
        "plugin.framework.process_worker.write_packed_frame_with_timeout",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="IPC frame", timeout=1.0)),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert killed == [True]
    assert worker.tasks_executed == 0


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
        "plugin.framework.process_worker.read_pickle_frame",
        MagicMock(side_effect=IpcPartialFrameTimeout("IPC frame stream desynchronized: timeout mid-frame")),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert killed == [True]
    assert worker.tasks_executed == 0


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
        "plugin.framework.process_worker.pack_pickle_frame",
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
    from compute_service.worker_base import BaseProcessWorker, Deadline

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
    # Nothing was dispatched. Counting this used to move a live worker
    # toward max_tasks.
    assert worker.tasks_executed == 0

    # A positive budget under one second is the same refusal. It must not
    # spawn for a handshake that cannot finish.
    short = worker.execute({"code": "result = 1"}, timeout_sec=0.5)
    assert short.get("code") == "EXECUTION_TIMEOUT"
    assert "at least 1 second" in str(short.get("error"))
    assert spawned == []
    assert worker.tasks_executed == 0

    # The dead-child branch has its own check. execute() returns before
    # that branch when the budget is already spent at entry.
    ensured = worker._ensure_live_process(Deadline(0))
    assert isinstance(ensured, dict)
    assert ensured.get("code") == "EXECUTION_TIMEOUT"
    ensured_short = worker._ensure_live_process(Deadline(0.5))
    assert isinstance(ensured_short, dict)
    assert ensured_short.get("code") == "EXECUTION_TIMEOUT"
    assert spawned == []
    assert worker.tasks_executed == 0


def test_expired_deadline_does_not_write_or_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clock that expires after the child is live is not written and not killed.

    What was wrong: left() floors a spent deadline at 0.01s, so the write
    started, timed out, and SIGKILL'd a child that had not received a byte.
    """
    from unittest.mock import MagicMock

    from compute_service.worker_base import BaseProcessWorker, Deadline

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.pid = 4
    worker.process.stdin = MagicMock()
    worker.process.stdout = MagicMock()
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    writes: list[object] = []
    monkeypatch.setattr("plugin.framework.process_worker.write_packed_frame_with_timeout", lambda *args, **kwargs: writes.append(args))
    # The entry check still sees time left, so the live child is kept.
    # The check before the write is the one that must stop.
    answers = iter((30.0, None))
    monkeypatch.setattr(Deadline, "usable", lambda self: next(answers))

    res = worker.execute({"code": "result = 1"}, timeout_sec=30.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert killed == []
    assert writes == []
    assert worker.tasks_executed == 0
    assert worker.process is not None


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
            pool._idle[worker] = time.monotonic() - (pool.idle_worker_ttl_sec + 10.0)  # type: ignore[index]
        pool._evict_idle_workers()
        assert worker.killed == 1
        assert worker not in pool._idle
    finally:
        pool.shutdown()


def test_spawn_failure_does_not_increment_tasks_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slot that never becomes live does not move toward max_tasks.

    The next handshake resets the count, and this failure never ran a cell.
    """
    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    worker.process = None
    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "WORKER_SPAWN_FAILED"
    assert worker.tasks_executed == 0


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("pipe", "WORKER_PIPE_BROKEN"),
        ("empty", "EMPTY_RESPONSE"),
        ("crash", "WORKER_CRASHED"),
    ],
)
def test_failed_request_does_not_increment_tasks_executed(monkeypatch: pytest.MonkeyPatch, kind: str, code: str) -> None:
    """Pipe, empty, and crash failures do not count toward max_tasks.

    A killing failure already reaps the child. Counting it cannot retire
    that process, and the next handshake starts the count at 0.
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
            "plugin.framework.process_worker.write_packed_frame_with_timeout",
            MagicMock(side_effect=BrokenPipeError("closed")),
        )
    elif kind == "empty":
        monkeypatch.setattr("plugin.framework.process_worker.write_packed_frame_with_timeout", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("plugin.framework.process_worker.read_pickle_frame", lambda *_args, **_kwargs: None)
    else:
        monkeypatch.setattr("plugin.framework.process_worker.write_packed_frame_with_timeout", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            "plugin.framework.process_worker.read_pickle_frame",
            MagicMock(side_effect=RuntimeError("died")),
        )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == code
    assert worker.tasks_executed == 0
    assert killed == [True]


def test_empty_read_during_shutdown_is_service_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """EOF while the pool is stopping is SERVICE_SHUTDOWN, not a counted crash."""
    from unittest.mock import MagicMock

    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.pid = 3
    worker.process.stdin = MagicMock()
    worker.process.stdout = MagicMock()
    worker._shutting_down = True
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    monkeypatch.setattr("plugin.framework.process_worker.write_packed_frame_with_timeout", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("plugin.framework.process_worker.read_pickle_frame", lambda *_args, **_kwargs: None)

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "SERVICE_SHUTDOWN"
    assert worker.tasks_executed == 0
    assert killed == []


def test_write_broken_during_shutdown_is_service_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pipe error while the pool is stopping is SERVICE_SHUTDOWN, not a kill.

    What was wrong: kill() landing mid-write was WORKER_PIPE_BROKEN. The
    child is already being reaped, so this must not kill again.
    """
    from unittest.mock import MagicMock

    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.pid = 3
    worker.process.stdin = MagicMock()
    worker.process.stdout = MagicMock()
    worker._shutting_down = True
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "plugin.framework.process_worker.write_packed_frame_with_timeout",
        MagicMock(side_effect=BrokenPipeError("closed")),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "SERVICE_SHUTDOWN"
    assert worker.tasks_executed == 0
    assert killed == []


@pytest.mark.parametrize("kind", ["value", "reset", "timeout"])
def test_read_error_during_shutdown_is_service_shutdown(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """A read failure while the pool is stopping is SERVICE_SHUTDOWN, not a crash.

    What was wrong: ValueError was WORKER_IPC_ERROR, ConnectionResetError
    was WORKER_CRASHED, and TimeoutExpired was EXECUTION_TIMEOUT. Windows
    raises that timeout from is_alive before the read sees EOF. The child
    is already being reaped, so this must not kill again.
    """
    import subprocess
    from unittest.mock import MagicMock

    from compute_service.worker_base import BaseProcessWorker

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.pid = 3
    worker.process.stdin = MagicMock()
    worker.process.stdout = MagicMock()
    worker._shutting_down = True
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    monkeypatch.setattr("plugin.framework.process_worker.write_packed_frame_with_timeout", lambda *_args, **_kwargs: None)
    if kind == "value":
        exc: BaseException = ValueError("I/O operation on closed file")
    elif kind == "reset":
        exc = ConnectionResetError("reset")
    else:
        exc = subprocess.TimeoutExpired(cmd="IPC frame", timeout=1)
    monkeypatch.setattr("plugin.framework.process_worker.read_pickle_frame", MagicMock(side_effect=exc))

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "SERVICE_SHUTDOWN"
    assert worker.tasks_executed == 0
    assert killed == []


def test_other_thread_kill_leaves_pipes_until_the_owner_leaves(monkeypatch: pytest.MonkeyPatch) -> None:
    """kill() from another thread only signals while this thread owns the pipes.

    What was wrong: the shutdown thread closed stdin and stdout while
    execute was between select and read. The read then raised, or could
    hit a reused fd number. The owner closes them when it leaves.
    """
    worker = _worker_without_spawn(monkeypatch)

    class _Reaped:
        def __init__(self) -> None:
            self.pid = 3
            self.stdin = _PipeEnd()
            self.stdout = _PipeEnd()
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

    proc = _Reaped()
    worker.process = proc  # type: ignore[assignment]
    child = worker.child
    assert child is not None
    child.begin_io()
    result: dict[str, bool] = {}

    def _kill() -> None:
        result["dropped"] = worker.kill()

    killer = threading.Thread(target=_kill)
    killer.start()
    killer.join(timeout=2)
    assert not killer.is_alive()
    assert result["dropped"] is False
    assert proc.stdin.closed is False
    assert proc.stdout.closed is False
    assert worker.process is proc
    worker._end_child_io(child)
    assert proc.stdin.closed is True
    assert proc.stdout.closed is True
    assert worker.process is None


def test_signal_from_rereads_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second ``_io_owner`` load is a new read.

    The first look can still see another thread. The owner clears before
    the second look, and this thread reaps. Two looks that both see
    another thread leave the pipes open for that thread's ``end_io``.
    """
    from plugin.framework.process_worker import Child

    other = threading.get_ident() + 1

    class _Loads:
        def __init__(self) -> None:
            self.values: list[int | None] = []
            self.seen: list[int | None] = []

        def __set__(self, obj: object, value: object) -> None:
            del obj, value

        def __get__(self, obj: object, objtype: type | None = None) -> int | None:
            if obj is None:
                return None
            value = self.values[min(len(self.seen), len(self.values) - 1)]
            self.seen.append(value)
            return value

    class _Proc:
        def __init__(self) -> None:
            self.pid = 7
            self.stdin = _PipeEnd()
            self.stdout = _PipeEnd()
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

    loads = _Loads()
    # _io_owner is set on the instance. The descriptor has to live on the
    # class, and it is not there until this test installs it.
    monkeypatch.setattr(Child, "_io_owner", loads, raising=False)
    loads.values = [other, None]
    reaped = _Proc()
    child = _child(reaped, worker_name="Worker", worker_id=1)
    assert child.signal_from() is True
    assert loads.seen[:2] == [other, None]
    assert reaped.stdin.closed is True
    assert reaped.stdout.closed is True

    loads.values = [other, other]
    loads.seen.clear()
    held = _Proc()
    owner = _child(held, worker_name="Worker", worker_id=1)
    assert owner.signal_from() is False
    assert loads.seen == [other, other]
    assert held.stdin.closed is False
    assert held.stdout.closed is False


def test_abandoned_child_is_not_written_and_a_later_spawn_may_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that survives the reap is not sent a request, and the slot may spawn.

    What was wrong: poll() was still None, so execute wrote to a process
    that had never said ready, and the next call refused to spawn forever.
    kill() drops that child. The next execute may call respawn again.
    """
    import subprocess


    enqueued: list[object] = []
    monkeypatch.setattr("plugin.framework.managed_subprocess._enqueue_reaper", lambda item: enqueued.append(item))
    worker = _worker_without_spawn(monkeypatch)

    class _Stuck:
        def __init__(self) -> None:
            self.pid = 4
            self.stdin = _PipeEnd()
            self.stdout = _PipeEnd()
            self.killed = False

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            raise subprocess.TimeoutExpired(cmd="stuck", timeout=1)

    stuck = _Stuck()
    worker.process = stuck  # type: ignore[assignment]
    assert worker.kill() is True
    assert stuck.killed
    assert stuck.stdin.closed is True
    assert stuck.stdout.closed is True
    assert worker.process is None
    assert len(enqueued) == 1

    spawned: list[bool] = []
    written: list[bool] = []

    def _respawn(timeout_sec: float | None = None, deadline: object | None = None) -> None:
        del timeout_sec, deadline
        spawned.append(True)

    worker.respawn = _respawn  # type: ignore[method-assign]
    monkeypatch.setattr("plugin.framework.process_worker.write_packed_frame_with_timeout", lambda *_args, **_kwargs: written.append(True))
    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "WORKER_SPAWN_FAILED"
    assert spawned == [True]
    assert written == []
    again = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert again.get("code") == "WORKER_SPAWN_FAILED"
    assert spawned == [True, True]
    assert written == []


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
    from plugin.framework import process_worker as worker_base

    clock = {"t": 1000.0}
    monkeypatch.setattr(worker_base.time, "monotonic", lambda: clock["t"])
    popen_calls: list[object] = []

    def _popen(*_args: object, **_kwargs: object) -> object:
        popen_calls.append(_args)
        raise AssertionError("Popen after the deadline")

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
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
    from plugin.framework import process_worker as worker_base
    from compute_service.worker_base import Deadline

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

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    monkeypatch.setattr(worker_base, "read_pickle_frame", _read)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    worker = _worker_without_spawn(monkeypatch)

    def _spend() -> None:
        clock["t"] += 0.4

    worker.process = _DeadChild(_spend)  # type: ignore[assignment]
    deadline = Deadline(5.0)
    try:
        worker.respawn(deadline=deadline)
        assert len(seen) == 1
        assert seen[0] == pytest.approx(4.6, abs=0.05)
    finally:
        worker.kill()


def test_respawn_rejects_timeout_and_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit handshake cap and a request clock cannot both apply.

    What was wrong: deadline replaced timeout_sec with no error, so a
    caller that passed both thought the cap was in force.
    """
    from plugin.framework import process_worker as worker_base
    from compute_service.worker_base import Deadline

    popen_calls: list[object] = []

    def _popen(*_args: object, **_kwargs: object) -> object:
        popen_calls.append(_args)
        raise AssertionError("Popen when both budgets were passed")

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    worker = worker_base.BaseProcessWorker(1, "unused.py", worker_name="Budget", start=False)
    with pytest.raises(ValueError, match="not both"):
        worker.respawn(timeout_sec=1.0, deadline=Deadline(5.0))
    assert popen_calls == []


def test_stderr_drain_name_uses_worker_name() -> None:
    """The drain thread names the pool, not a generic compute worker.

    What was wrong: Kokoro and vision used a generic compute prefix, so a
    stuck drain could not be told apart from a formula worker.
    """
    from plugin.framework.process_worker import BaseProcessWorker

    cases = (
        ("Kokoro", 1, "wa-kokoro-w1-stderr"),
        ("Formula worker", 2, "wa-formula-worker-w2-stderr"),
        ("---", 3, "wa-worker-w3-stderr"),
    )
    for name, worker_id, expected in cases:
        worker = BaseProcessWorker(worker_id, "unused.py", worker_name=name, start=False)
        assert worker._drain_name() == expected


def test_popen_past_deadline_skips_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup that overruns the deadline kills the child and does not read a frame.

    Popen is not on the clock. A handshake after that would use the 0.01s
    pipe-wait floor and then SIGKILL.
    """
    from plugin.framework import process_worker as worker_base

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

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    monkeypatch.setattr(worker_base, "read_pickle_frame", _read)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
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

    from plugin.framework import process_worker as worker_base
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

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", lambda *_args, **_kwargs: _Proc())
    monkeypatch.setattr(worker_base, "read_pickle_frame", _read)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    worker = _worker_without_spawn(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
        worker.respawn(timeout_sec=2.0)
    assert "spawn handshake timed out" in caplog.text
    assert "Failed to spawn" not in caplog.text
    assert worker.process is None


def test_shutdown_handshake_is_not_a_spawn_error(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A ready frame that disappears because shutdown won is not an error.

    What was wrong: kill() during the handshake made the read return
    empty, and respawn logged "spawn handshake was not ready" at error.
    """
    import logging

    from plugin.framework import process_worker as worker_base

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

    worker = _worker_without_spawn(monkeypatch)

    def _read(*_args: object, **_kwargs: object) -> None:
        worker._shutting_down = True
        return None

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", lambda *_args, **_kwargs: _Proc())
    monkeypatch.setattr(worker_base, "read_pickle_frame", _read)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    with caplog.at_level(logging.INFO, logger="compute_service.worker"):
        worker.respawn(timeout_sec=2.0)
    matches = [record for record in caplog.records if "spawn handshake was not ready" in record.message]
    assert len(matches) == 1
    assert matches[0].levelno == logging.INFO
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert worker.process is None


def test_run_compute_worker_sets_identity_and_payload_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both children share identity env vars, single-threaded math defaults, and frame cap."""
    import os

    from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES
    from compute_service.worker_stdio import COMPUTE_THREAD_ENV_VARS, run_compute_worker

    seen: dict[str, object] = {}

    def _loop(handler: object, *, max_payload_bytes: int = 0) -> int:
        seen["handler"] = handler
        seen["cap"] = max_payload_bytes
        seen["is_worker"] = os.environ.get("WRITERAGENT_IS_WORKER")
        seen["compute"] = os.environ.get("WRITERAGENT_COMPUTE_WORKER")
        for var_name in COMPUTE_THREAD_ENV_VARS:
            seen[var_name] = os.environ.get(var_name)
        return 0

    monkeypatch.setattr("compute_service.worker_stdio.run_worker_stdio_loop", _loop)
    monkeypatch.delenv("WRITERAGENT_IS_WORKER", raising=False)
    monkeypatch.delenv("WRITERAGENT_COMPUTE_WORKER", raising=False)
    for var_name in COMPUTE_THREAD_ENV_VARS:
        monkeypatch.delenv(var_name, raising=False)

    def _handler(_req: dict[str, object]) -> dict[str, str]:
        return {"status": "ok"}

    assert run_compute_worker(_handler) == 0
    assert seen["handler"] is _handler
    assert seen["cap"] == COMPUTE_MAX_PAYLOAD_BYTES
    assert seen["is_worker"] == "1"
    assert seen["compute"] == "1"
    for var_name in COMPUTE_THREAD_ENV_VARS:
        assert seen[var_name] == "1"


def test_configure_compute_worker_env_preserves_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-existing math thread environment variables are not overwritten."""
    import os

    from compute_service.worker_stdio import COMPUTE_THREAD_ENV_VARS, configure_compute_worker_env

    for var_name in COMPUTE_THREAD_ENV_VARS:
        monkeypatch.setenv(var_name, "4")

    configure_compute_worker_env()

    for var_name in COMPUTE_THREAD_ENV_VARS:
        assert os.environ.get(var_name) == "4"


def test_reap_timeout_abandons_and_allows_another_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child still alive after the reap wait is dropped, and the slot may spawn.

    The survivor is handed to the module reaper. It is not this slot's child,
    so the next respawn may Popen.
    """
    import subprocess

    from plugin.framework import process_worker as worker_mod

    enqueued: list[object] = []
    monkeypatch.setattr("plugin.framework.managed_subprocess._enqueue_reaper", lambda item: enqueued.append(item))

    class _Stuck:
        def __init__(self) -> None:
            self.pid = 42
            self.killed = False
            self.stdin = None
            self.stdout = None
            self.stderr = None

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            raise subprocess.TimeoutExpired(cmd="stuck", timeout=1)

    class _Ready:
        def __init__(self) -> None:
            self.pid = 43
            self.stdin = object()
            self.stdout = object()
            self.stderr = None
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = -9
            return -9

    worker = _worker_without_spawn(monkeypatch)
    stuck = _Stuck()
    worker.process = stuck  # type: ignore[assignment]
    ready = _Ready()

    def _popen(*_args: object, **_kwargs: object) -> _Ready:
        return ready

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    monkeypatch.setattr(worker_mod, "read_pickle_frame", lambda *_args, **_kwargs: {"status": "ready", "pid": 43})
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    try:
        worker.respawn()
        assert stuck.killed
        assert len(enqueued) == 1
        assert worker.process is ready
        assert worker.child is not None
        assert worker.child.managed.popen is ready
    finally:
        worker.kill()


def test_reap_clears_the_slot_after_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """wait() that reaps the child clears the slot.

    There is no exit callback under the lifecycle lock. The session scan
    sees the child pointer change.
    """
    worker = _worker_without_spawn(monkeypatch)

    class _Exited:
        def __init__(self) -> None:
            self.pid = 7
            self.stdin = None
            self.stdout = None
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
    worker.process = child  # type: ignore[assignment]
    assert worker.kill() is True
    assert worker.process is None
    assert worker.child is None


class _PipeEnd:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_kill_closes_stdio_then_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller with no pipe owner closes stdin and stdout, then waits.

    What was wrong: kill() left those fds on the Popen until collection.
    On Windows that also keeps the child's ends open. A child still alive
    after the wait is abandoned with those pipes already closed, and the
    slot is empty. stderr stays with the drain.
    """
    import subprocess


    enqueued: list[object] = []
    monkeypatch.setattr("plugin.framework.managed_subprocess._enqueue_reaper", lambda item: enqueued.append(item))
    worker = _worker_without_spawn(monkeypatch)

    class _Reaped:
        def __init__(self) -> None:
            self.pid = 3
            self.stdin = _PipeEnd()
            self.stdout = _PipeEnd()
            self.stderr = _PipeEnd()
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

    reaped = _Reaped()
    worker.process = reaped  # type: ignore[assignment]
    assert worker.kill() is True
    assert reaped.stdin.closed
    assert reaped.stdout.closed
    assert reaped.stderr.closed is False
    assert worker.process is None

    class _Stuck:
        def __init__(self) -> None:
            self.pid = 4
            self.stdin = _PipeEnd()
            self.stdout = _PipeEnd()
            self.stderr = _PipeEnd()

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            raise subprocess.TimeoutExpired(cmd="stuck", timeout=1)

    stuck = _Stuck()
    worker.process = stuck  # type: ignore[assignment]
    assert worker.kill() is True
    assert stuck.stdin.closed
    assert stuck.stdout.closed
    assert stuck.stderr.closed is False
    assert worker.process is None
    assert len(enqueued) == 1


def test_short_one_second_remainder_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1s budget with ~20ms left does not Popen. usable() still returns it.

    What was wrong: usable() lets a one-second budget start after the clock
    has moved, so the pool can lift that remainder. respawn used the same
    check, spawned, and the handshake was given the leftover.
    """
    from plugin.framework import process_worker as worker_mod

    monkeypatch.setattr(worker_mod.time, "monotonic", lambda: 1000.0)
    popen_calls: list[object] = []

    def _popen(*_args: object, **_kwargs: object) -> object:
        popen_calls.append(_args)
        raise AssertionError("Popen with nothing left for the handshake")

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    worker = _worker_without_spawn(monkeypatch)
    clock = worker_mod.Deadline.from_absolute(1.0, 1000.02)
    assert clock.usable() == pytest.approx(0.02)
    worker.respawn(deadline=clock)
    assert popen_calls == []

    calls = {"n": 0}

    def _moved() -> float:
        calls["n"] += 1
        if calls["n"] == 1:
            return 1000.0
        return 1000.98

    monkeypatch.setattr(worker_mod.time, "monotonic", _moved)
    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert popen_calls == []
    assert worker.tasks_executed == 0


def test_one_second_budget_above_spawn_floor_still_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1s budget with 0.5s left still reaches Popen. That case stays usable."""
    from plugin.framework import process_worker as worker_mod

    calls = {"n": 0}

    def _moved() -> float:
        calls["n"] += 1
        if calls["n"] == 1:
            return 1000.0
        return 1000.5

    monkeypatch.setattr(worker_mod.time, "monotonic", _moved)
    popen_calls: list[object] = []

    def _popen(*_args: object, **_kwargs: object) -> object:
        popen_calls.append(_args)
        raise OSError("stop after Popen")

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    worker = _worker_without_spawn(monkeypatch)
    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert popen_calls
    assert res.get("code") == "WORKER_SPAWN_FAILED"


def test_popen_failure_leaves_the_slot_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Popen raising leaves no child. Stderr is a pipe, not a file to unlink."""
    import subprocess


    worker = _worker_without_spawn(monkeypatch)
    saw_pipe: list[bool] = []

    def _popen(*_args: object, **kwargs: object) -> object:
        saw_pipe.append(kwargs.get("stderr") is subprocess.PIPE)
        raise OSError("spawn failed")

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    worker.respawn()
    assert saw_pipe == [True]
    assert worker.process is None
    assert worker.child is None


def test_adopt_publishes_one_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adopt stores the Child in one lock hold. Shutdown and a live child refuse.

    What was wrong: the stderr path was published first. kill() in that gap
    unlinked the file, then adopt stored a live child whose diagnostics
    read as empty. A second adopt also overwrote the published process.
    """

    worker = _worker_without_spawn(monkeypatch)
    first_proc = object()
    first = _child(first_proc, worker_name=worker.worker_name, worker_id=worker.worker_id)
    assert worker._adopt(first) is True
    assert worker.process is first_proc
    assert worker.child is first

    worker.request_shutdown()
    refused = _child(object(), worker_name=worker.worker_name, worker_id=worker.worker_id)
    # Shutdown refuses a new child. The one already published stays until kill.
    assert worker._adopt(refused) is False
    assert worker.child is first

    fresh = _worker_without_spawn(monkeypatch)
    published = object()
    fresh.process = published  # type: ignore[assignment]
    second = _child(object(), worker_name=fresh.worker_name, worker_id=fresh.worker_id)
    assert fresh._adopt(second) is False
    assert fresh.process is published
    assert fresh.child is not second


def test_concurrent_respawn_keeps_one_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two overlapping respawns publish one child and kill the other.

    What was wrong: both threads passed the reap before either adopt, so
    the second assignment leaked the first Popen.
    """
    from plugin.framework import process_worker as worker_mod

    worker = _worker_without_spawn(monkeypatch)
    both_in_popen = threading.Event()
    release = threading.Event()
    children: list[object] = []
    children_lock = threading.Lock()

    class _Proc:
        def __init__(self) -> None:
            self.pid = id(self) & 0x7FFFFFFF
            self.stdin = None
            self.stdout = object()
            self.stderr = None
            self.killed = False
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            if self.returncode is None:
                self.returncode = -9
            return self.returncode

    def _popen(*_args: object, **_kwargs: object) -> _Proc:
        proc = _Proc()
        with children_lock:
            children.append(proc)
            if len(children) == 2:
                both_in_popen.set()
        assert both_in_popen.wait(timeout=2)
        assert release.wait(timeout=2)
        return proc

    def _read(_stdout: object, _timeout_sec: float, **_kwargs: object) -> dict[str, object]:
        return {"status": "ready", "pid": 1}

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    monkeypatch.setattr(worker_mod, "read_pickle_frame", _read)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            worker.respawn()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _idx in range(2)]
    for thread in threads:
        thread.start()
    assert both_in_popen.wait(timeout=2)
    release.set()
    try:
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert errors == []
        assert len(children) == 2
        published = worker.process
        assert published in children
        losers = [child for child in children if child is not published]
        assert len(losers) == 1
        assert losers[0].killed  # type: ignore[attr-defined]
        assert published.killed is False  # type: ignore[attr-defined]
    finally:
        worker.kill()


def test_handshake_crash_logs_the_drain_tail(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A dead child during the ready read still logs the stderr drain.

    What was wrong: respawn passes is_alive into the handshake. On Windows
    that runs before EOF, and the failure logged stderr=<empty>. The drain
    keeps the text. POSIX calls is_alive only when select times out.
    """
    import logging

    from plugin.framework import process_worker as worker_mod
    from plugin.framework.worker_pool import StderrTail

    worker = _worker_without_spawn(monkeypatch)
    tail = StderrTail()

    class _Dead:
        def __init__(self) -> None:
            self.pid = 41
            self.stdin = None
            self.stdout = object()
            self.stderr = object()
            self.returncode: int | None = 1

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            if self.returncode is None:
                self.returncode = -9
            return self.returncode

    def _popen(*_args: object, **_kwargs: object) -> _Dead:
        return _Dead()

    def _drain(_stream: object, **_kwargs: object) -> StderrTail:
        return tail

    def _read(_stdout: object, _timeout_sec: float, **kwargs: object) -> None:
        tail._append("startup-crash-traceback\n")
        alive = kwargs["is_alive"]
        assert callable(alive)
        alive()
        return None

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    monkeypatch.setattr("plugin.framework.managed_subprocess.start_stderr_drain", _drain)
    monkeypatch.setattr(worker_mod, "read_pickle_frame", _read)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
        worker.respawn()
    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "startup-crash-traceback" in joined
    assert "stderr=<empty>" not in joined
    assert worker.process is None


def test_respawn_failure_before_adopt_leaves_the_other_child(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A respawn that raises before adopt must not kill the published child.

    What was wrong: the except path always called kill(). Two unlocked
    callers can overlap. The one that fails before adopt reaped the
    other call's healthy child.
    """
    import logging

    from plugin.framework import process_worker as worker_mod

    worker = _worker_without_spawn(monkeypatch)
    both_in_popen = threading.Event()
    adopted = threading.Event()
    hold = threading.Event()
    calls = {"n": 0}
    calls_lock = threading.Lock()
    winner: dict[str, Any] = {}

    class _Proc:
        def __init__(self) -> None:
            self.pid = 77
            self.stdin = None
            self.stdout = object()
            self.stderr = None
            self.killed = False
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            if self.returncode is None:
                self.returncode = -9
            return self.returncode

    def _popen(*_args: object, **_kwargs: object) -> _Proc:
        with calls_lock:
            calls["n"] += 1
            mine = calls["n"]
            if calls["n"] == 2:
                both_in_popen.set()
        assert both_in_popen.wait(timeout=2)
        if mine == 1:
            proc = _Proc()
            winner["proc"] = proc
            return proc
        deadline = time.monotonic() + 2.0
        while worker.process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        raise OSError("second spawn failed")

    def _drain(_stream: object, **_kwargs: object) -> object:
        from plugin.framework.worker_pool import StderrTail

        return StderrTail()

    def _read(_stdout: object, _timeout_sec: float, **_kwargs: object) -> dict[str, object]:
        current = worker.child
        if current is not None and current.managed.stderr_tail is not None:
            current.managed.stderr_tail._append("winner-still-healthy\n")
        adopted.set()
        hold.wait(timeout=2)
        return {"status": "ready", "pid": 77}

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    monkeypatch.setattr("plugin.framework.managed_subprocess.start_stderr_drain", _drain)
    monkeypatch.setattr(worker_mod, "read_pickle_frame", _read)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            worker.respawn()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _idx in range(2)]
    with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
        for thread in threads:
            thread.start()
        try:
            assert adopted.wait(timeout=2)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and sum(not thread.is_alive() for thread in threads) < 1:
                time.sleep(0.01)
            assert sum(not thread.is_alive() for thread in threads) == 1
            published = winner["proc"]
            assert worker.process is published
            assert published.killed is False
            hold.set()
            for thread in threads:
                thread.join(timeout=2)
                assert not thread.is_alive()
            assert errors == []
            joined = "\n".join(record.getMessage() for record in caplog.records)
            assert "second spawn failed" in joined
            assert "winner-still-healthy" not in joined
            assert worker.process is published
            assert published.killed is False
        finally:
            hold.set()
            for thread in threads:
                thread.join(timeout=2)
            worker.kill()


def test_response_frame_error_is_ipc_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad response frame is WORKER_IPC_ERROR and still kills the child.

    What was wrong: IpcFrameError and a restricted-unpickler ValueError
    fell through to WORKER_CRASHED. The pipe is not reusable, so the
    kill stays. The code was the part that lied.
    """
    from unittest.mock import MagicMock

    from compute_service.worker_base import BaseProcessWorker
    from plugin.scripting.ipc import IpcFrameReadError

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    monkeypatch.setattr("plugin.framework.process_worker.write_packed_frame_with_timeout", lambda *_args, **_kwargs: None)

    def _run(exc: BaseException) -> dict[str, Any]:
        worker = BaseProcessWorker(1, "unused.py")
        worker.process = MagicMock()
        worker.process.poll.return_value = None
        worker.process.pid = 3
        worker.process.stdin = MagicMock()
        worker.process.stdout = MagicMock()
        killed: list[bool] = []
        worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
        monkeypatch.setattr(
            "plugin.framework.process_worker.read_pickle_frame",
            MagicMock(side_effect=exc),
        )
        res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
        assert killed == [True]
        assert worker.tasks_executed == 0
        return res

    oversized = _run(IpcFrameReadError("Invalid IPC frame size: 99"))
    assert oversized.get("code") == "WORKER_IPC_ERROR"
    assert "WORKER_CRASHED" not in str(oversized.get("code"))
    rejected = _run(ValueError("builtin is forbidden"))
    assert rejected.get("code") == "WORKER_IPC_ERROR"
    assert "forbidden" in str(rejected.get("error"))


def test_unadopted_child_that_survives_kill_is_abandoned(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A child adopt refused, and that will not die, is abandoned. The slot stays.

    What was wrong: a failed wait returned and the child was neither stored
    nor mentioned above debug. The loser is signaled. The published child
    is not.
    """
    import logging
    import subprocess


    enqueued: list[tuple[str, object]] = []
    monkeypatch.setattr("plugin.framework.managed_subprocess._enqueue_reaper", lambda item: enqueued.append(item))
    worker = _worker_without_spawn(monkeypatch)

    class _Live:
        def __init__(self) -> None:
            self.pid = 1
            self.killed = False
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

    class _Stuck:
        def __init__(self) -> None:
            self.pid = 99
            self.stdin = None
            self.stdout = None

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            raise subprocess.TimeoutExpired(cmd="stuck", timeout=1)

    published = _Live()
    worker.process = published  # type: ignore[assignment]
    loser = _child(_Stuck(), worker_name=worker.worker_name, worker_id=worker.worker_id)
    assert worker._adopt(loser) is False
    with caplog.at_level(logging.WARNING, logger="compute_service.worker"):
        assert loser.signal_from() is True
    assert any("abandoning" in record.getMessage() for record in caplog.records)
    assert enqueued == [("abandon", loser.managed)]
    assert worker.process is published
    assert published.killed is False


def test_fail_request_skips_stderr_when_it_does_not_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure that leaves the child must not attach another child's tail.

    What was wrong: _fail_request always read the stderr tail. kill=False
    is shutdown, a clock spent before the frame was sent, or a spawn that
    already logged its own tail.
    """
    from plugin.framework.worker_pool import StderrTail

    worker = _worker_without_spawn(monkeypatch)

    class _Proc:
        pid = 3

        def poll(self) -> None:
            return None

    tail = StderrTail()
    tail._append("previous-child-tail\n")
    worker.child = _child(_Proc(), worker_name=worker.worker_name, worker_id=worker.worker_id, tail=tail)
    killed: list[bool] = []

    def _kill() -> bool:
        killed.append(True)
        return True

    worker.kill = _kill  # type: ignore[method-assign]
    quiet = worker._fail_request("SERVICE_SHUTDOWN", "stopping", pid=None, kill=False)
    assert "previous-child-tail" not in str(quiet.get("error"))
    assert killed == []
    loud = worker._fail_request("WORKER_CRASHED", "died", pid=3, kill=True)
    assert "previous-child-tail" in str(loud.get("error"))
    assert killed == [True]


def test_stderr_drain_keeps_a_bounded_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chatty child does not grow the diagnostic tail without a bound.

    The drain drops the oldest chunks once the kept text passes
    ``_STDERR_LOG_CAP``. The snippet attached to an error is the last
    ``_STDERR_SNIPPET`` of that text. One chunk larger than the cap is
    dropped whole, so the child text arrives in smaller writes.
    """
    from plugin.framework.process_worker import _STDERR_LOG_CAP, _STDERR_SNIPPET
    from plugin.framework.worker_pool import StderrTail

    tail = StderrTail(max_chars=_STDERR_LOG_CAP)
    piece = "x" * 1024
    for _idx in range((_STDERR_LOG_CAP // len(piece)) + 2):
        tail._append(piece)
    tail._append("END")
    text = tail.text()
    assert len(text) <= _STDERR_LOG_CAP
    assert text.endswith("END")
    worker = _worker_without_spawn(monkeypatch)
    child = _child(object(), tail=tail, worker_name=worker.worker_name, worker_id=worker.worker_id)
    snippet = child.diagnostic_tail()
    assert snippet.endswith("END")
    assert len(snippet) <= _STDERR_SNIPPET


def test_stderr_drain_stays_alive_while_the_write_end_is_open() -> None:
    """A pipe with no EOF keeps the drain thread after finish_text gives up.

    A grandchild that inherited stderr is that pipe. The join is bounded.
    Closing the write end lets the daemon thread leave.
    """
    import os

    from plugin.framework.worker_pool import start_stderr_drain

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    tail = start_stderr_drain(stream, name="wa-test-w1-stderr")
    assert tail is not None
    try:
        assert tail.finish_text(timeout=0.05) == ""
        assert tail.is_alive is True
    finally:
        os.close(write_fd)
        tail.join(timeout=2.0)
    assert tail.is_alive is False


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
        "plugin.framework.process_worker.pack_pickle_frame",
        MagicMock(side_effect=exc),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "REQUEST_NOT_SERIALIZABLE"
    assert killed == []
    assert worker.tasks_executed == 0
    assert worker.process is proc


def test_pipe_value_error_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ValueError from the pipe write kills the child.

    What was wrong: ValueError was caught with pickle.dumps, so a pipe that
    raised it returned REQUEST_NOT_SERIALIZABLE and left the child on a
    desynced frame.
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
        "plugin.framework.process_worker.write_packed_frame_with_timeout",
        MagicMock(side_effect=ValueError("short write")),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "WORKER_PIPE_BROKEN"
    assert killed == [True]
    assert worker.tasks_executed == 0


def test_abandoned_diagnostic_does_not_join_the_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that survives the reap is dropped. Its drain is not joined.

    Joining would wait for text that cannot arrive while the pipe is open.
    The live tail is still readable.
    """
    import subprocess

    from plugin.framework.worker_pool import StderrTail

    enqueued: list[tuple[str, object]] = []
    monkeypatch.setattr("plugin.framework.managed_subprocess._enqueue_reaper", lambda item: enqueued.append(item))

    class _Blocked:
        def join(self, timeout: float | None = None) -> None:
            del timeout
            raise AssertionError("abandoned diagnostic must not join the drain")

        def is_alive(self) -> bool:
            return True

    class _Stuck:
        def __init__(self) -> None:
            self.pid = 42
            self.stdin = None
            self.stdout = None

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            raise subprocess.TimeoutExpired(cmd="stuck", timeout=1)

    tail = StderrTail()
    tail._append("still-running\n")
    tail.attach_thread(_Blocked())  # type: ignore[arg-type]
    worker = _worker_without_spawn(monkeypatch)
    child = _child(_Stuck(), tail=tail, worker_name=worker.worker_name, worker_id=worker.worker_id)
    worker.child = child
    assert worker.kill() is True
    assert worker.child is None
    assert enqueued == [("abandon", child.managed)]
    assert "still-running" in child.diagnostic_tail()


def test_idle_scan_before_ttl_does_not_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle-reaper tick before the TTL leaves the live child in idle."""
    import time

    from compute_service.worker_base import BaseProcessPool

    worker = _worker_without_spawn(monkeypatch)

    class _Live:
        def __init__(self) -> None:
            self.kills = 0
            self.stdin = None
            self.stdout = None

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.kills += 1

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    live = _Live()
    worker.process = live  # type: ignore[assignment]
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=3600.0)
    try:
        with pool._cond:
            pool._idle[worker] = time.monotonic()  # type: ignore[index]
        pool._evict_idle_workers()
        assert live.kills == 0
        assert worker in pool._idle
        assert worker.is_alive()
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


def test_usable_budget_truth_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1s budget may start with under a second left. A longer one may not.

    The chained compare hid that. A 2s request with 0.5s left must refuse;
    a 1s request with the same remainder must start.
    """
    from plugin.framework import process_worker as worker_mod

    now = 1000.0
    monkeypatch.setattr(worker_mod.time, "monotonic", lambda: now)
    cases = (
        (1.0, 0.5, 0.5),
        (2.0, 0.5, None),
        (0.5, 0.5, None),
        (1.0, 0.0, None),
        (2.0, 1.0, 1.0),
    )
    for budget, remaining, expected in cases:
        clock = worker_mod.Deadline.from_absolute(budget, now + remaining)
        assert clock.usable() == expected, (budget, remaining)


def test_timeout_message_keeps_fractional_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1.9s budget is not reported as 1 second. Whole seconds stay integers."""
    from compute_service.worker_base import Deadline

    worker = _worker_without_spawn(monkeypatch)
    fractional = worker._timeout_message(Deadline(1.9))
    assert "1.9" in fractional
    assert "1 seconds" not in fractional
    assert worker._timeout_message(Deadline(1.0)) == "Execution exceeded maximum timeout of 1 second."
    assert worker._timeout_message(Deadline(2.0)) == "Execution exceeded maximum timeout of 2 seconds."


def test_is_alive_snapshots_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second read of the child must not run. kill() clears it between the two.

    What was wrong: is_alive loaded the process, then called poll on a
    second load. Shutdown set that to None, and the write path reported
    AttributeError as REQUEST_NOT_SERIALIZABLE. The child pointer is the
    load that has to happen once.
    """
    worker = _worker_without_spawn(monkeypatch)

    class _Proc:
        def poll(self) -> None:
            return None

    worker.process = _Proc()  # type: ignore[assignment]
    loads = {"n": 0}

    class _SecondReadIsNone(type(worker)):
        def __getattribute__(self, name: str) -> object:
            if name == "child":
                loads["n"] += 1
                if loads["n"] > 1:
                    return None
            return object.__getattribute__(self, name)

    worker.__class__ = _SecondReadIsNone  # type: ignore[assignment]
    assert worker.is_alive() is True
    assert loads["n"] == 1


class _PolledDead:
    """Child whose poll() already reaped it. is_alive must not clear process."""

    def __init__(self) -> None:
        self.pid = 4

    def poll(self) -> int:
        return 1


def _dead_slot(monkeypatch: pytest.MonkeyPatch):
    """A published child whose poll() has already reaped it."""
    worker = _worker_without_spawn(monkeypatch)
    child = _PolledDead()
    worker.process = child  # type: ignore[assignment]
    return worker, child


def test_is_alive_leaves_a_reaped_child_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """poll() that has reaped the child does not clear the slot.

    The session scan treats that poll as a dead kernel. is_alive only
    reports it. A later kill still sees this process.
    """
    worker, child = _dead_slot(monkeypatch)
    assert worker.is_alive() is False
    assert worker.process is child


def test_is_alive_while_execute_holds_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request that already holds self.lock can still call is_alive.

    The IPC loop calls is_alive from execute. The lock is not re-entrant,
    so this acquire must not block, and the dead child stays put.
    """
    worker, child = _dead_slot(monkeypatch)
    worker.lock.acquire()
    try:
        assert worker.is_alive() is False
    finally:
        worker.lock.release()
    assert worker.process is child


def test_is_alive_does_not_wait_on_the_lifecycle_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """poll() returns while another thread holds _lifecycle_lock.

    Idle prune calls is_alive while holding the pool condition. kill()
    takes the lifecycle lock only to drop the pointer. A blocking acquire
    here would invert that order.
    """
    worker, child = _dead_slot(monkeypatch)
    held = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        worker._lifecycle_lock.acquire()
        held.set()
        assert release.wait(timeout=2)
        worker._lifecycle_lock.release()

    holder = threading.Thread(target=_hold)
    holder.start()
    assert held.wait(timeout=2)
    try:
        started = time.monotonic()
        assert worker.is_alive() is False
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        holder.join(timeout=2)
    assert worker.process is child
    assert not holder.is_alive()


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
        "plugin.framework.process_worker.write_packed_frame_with_timeout",
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
    ("budget", "remaining", "usable_sec", "left_sec"),
    [
        (0.0, 0.0, None, 0.0),
        (0.5, 0.5, None, 0.5),
        (1.0, 0.0, None, 0.0),
        (1.0, 0.5, 0.5, 0.5),
        (1.0, 1.0, 1.0, 1.0),
        (5.0, -0.25, None, -0.25),
        (5.0, 0.5, None, 0.5),
        (5.0, 2.5, 2.5, 2.5),
    ],
)
def test_deadline_budget_table(
    monkeypatch: pytest.MonkeyPatch,
    budget: float,
    remaining: float,
    usable_sec: float | None,
    left_sec: float,
) -> None:
    """usable() is the only start gate. left() is the true remainder."""
    from compute_service.worker_base import Deadline

    now = 1_000.0
    monkeypatch.setattr("plugin.framework.process_worker.time.monotonic", lambda: now)
    clock = Deadline.__new__(Deadline)
    clock.budget_sec = budget
    clock._end = now + remaining
    assert clock.usable() == usable_sec
    assert clock.left() == left_sec


def test_formula_worker_script_handshake() -> None:
    """The production formula script completes the ready handshake and one cell."""
    import json

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


def test_stuck_kill_drops_the_slot_after_one_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """One timed-out SIGKILL drops the slot. The survivor is not killed again here.

    The module reaper keeps waiting. The slot does not.
    """
    import subprocess


    enqueued: list[object] = []
    monkeypatch.setattr("plugin.framework.managed_subprocess._enqueue_reaper", lambda item: enqueued.append(item))

    class _Stuck:
        def __init__(self) -> None:
            self.pid = 42
            self.kills = 0
            self.stdin = None
            self.stdout = None

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.kills += 1

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            raise subprocess.TimeoutExpired(cmd="stuck", timeout=1)

    worker = _worker_without_spawn(monkeypatch)
    child = _Stuck()
    worker.process = child  # type: ignore[assignment]
    assert worker.kill() is True
    assert child.kills == 1
    assert worker.process is None
    assert len(enqueued) == 1


def test_release_does_not_idle_a_dead_worker() -> None:
    """A worker that is not alive after release is not idled, and can be cold-claimed.

    What was wrong: release idled any worker that still answered is_alive.
    A child kill could not reap was handed to the next lease. kill() now
    drops that child, so the slot is dead and the next lease respawns it.
    """
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=None)

    class _Dead:
        def __init__(self) -> None:
            self.tasks_executed = 0
            self.worker_id = 3
            self.pending_exit = False

        def is_alive(self) -> bool:
            return False

        def request_shutdown(self) -> None:
            return None

        def kill(self) -> bool:
            return True

    worker = _Dead()
    try:
        pool.workers.append(worker)  # type: ignore[arg-type]
        pool._leased.add(worker)  # type: ignore[arg-type]
        pool.release_worker(worker)  # type: ignore[arg-type]
        assert worker not in pool._idle
        assert worker not in pool._leased
        assert pool.lease_any(timeout_sec=0) is worker
    finally:
        pool.shutdown()


def test_unpicklable_payload_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pack runs before spawn. A pickle error must not start a child."""
    import pickle
    from unittest.mock import MagicMock

    from compute_service.worker_base import BaseProcessWorker

    worker = _worker_without_spawn(monkeypatch)
    spawned: list[bool] = []

    def _spawn(self: BaseProcessWorker, timeout_sec: float | None = None, deadline: object | None = None) -> None:
        del self, timeout_sec, deadline
        spawned.append(True)

    monkeypatch.setattr(BaseProcessWorker, "respawn", _spawn)
    monkeypatch.setattr(
        "plugin.framework.process_worker.pack_pickle_frame",
        MagicMock(side_effect=pickle.PicklingError("no")),
    )
    worker.process = None
    res = worker.execute({"code": "result = 1"}, timeout_sec=5)
    assert res.get("code") == "REQUEST_NOT_SERIALIZABLE"
    assert spawned == []


def test_install_image_pkgs_adds_sibling_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pkgs directory next to the service is inserted once.

    The image publishes wheels through PYTHONPATH, and the worker scrub
    removes that variable. The directory itself has to be added by path.
    A missing directory leaves sys.path alone.
    """
    import sys

    from compute_service.worker_base import install_image_pkgs

    pkgs = tmp_path / "pkgs"
    pkgs.mkdir()
    monkeypatch.setattr(sys, "path", ["/usr/lib/python"])
    install_image_pkgs(str(tmp_path))
    assert sys.path[0] == str(pkgs)
    install_image_pkgs(str(tmp_path))
    assert sys.path.count(str(pkgs)) == 1

    monkeypatch.setattr(sys, "path", ["/usr/lib/python"])
    install_image_pkgs(str(tmp_path / "no-such-root"))
    assert sys.path == ["/usr/lib/python"]


def test_respawn_popen_kwargs_scrub_session_and_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    import sys
    from plugin.framework.process_worker import BaseProcessWorker

    recorded_args: list[object] = []
    recorded_kwargs: dict[str, Any] = {}

    class _Proc:
        def __init__(self) -> None:
            self.pid = 1
            self.stdin = object()
            self.stdout = object()
            self.stderr = object()
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            if self.returncode is None:
                self.returncode = -9
            return self.returncode

    def _fake_popen(args: object, **kwargs: Any) -> _Proc:
        recorded_args.append(args)
        recorded_kwargs.update(kwargs)
        return _Proc()

    drain_calls: list[dict[str, Any]] = []

    def _fake_drain(stream: object, **kwargs: Any) -> Any:
        drain_calls.append(kwargs)
        return None

    opt_calls: list[object] = []

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("plugin.framework.managed_subprocess.start_stderr_drain", _fake_drain)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda p: opt_calls.append(p))
    monkeypatch.setattr("plugin.framework.process_worker.read_pickle_frame", lambda *_a, **_kw: {"status": "ready", "pid": 1})

    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("PYTHONPATH", "/custom/path")

    worker = BaseProcessWorker(1, "fake_script.py", worker_name="Budget", start=False)
    try:
        worker.respawn()
        assert recorded_args == [[sys.executable, "fake_script.py"]]
        env = recorded_kwargs.get("env")
        assert env is not None
        assert "OPENAI_API_KEY" not in env
        assert "PYTHONPATH" not in env
        assert env.get("PYTHONUTF8") == "1"
        assert recorded_kwargs.get("stderr") is subprocess.PIPE
        assert recorded_kwargs.get("bufsize") == 0
        if sys.platform != "win32":
            assert recorded_kwargs.get("start_new_session") is True
        else:
            assert "creationflags" in recorded_kwargs
            assert "start_new_session" not in recorded_kwargs
        assert len(opt_calls) == 1
        assert len(drain_calls) == 1
        assert drain_calls[0].get("name") == "wa-budget-w1-stderr"
        assert drain_calls[0].get("max_tail_chars") == 65536
    finally:
        worker.kill()


def test_explicit_child_env_is_not_scrubbed_again(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.framework.process_worker import BaseProcessWorker

    recorded_kwargs: dict[str, Any] = {}

    def _fake_popen(_args: object, **kwargs: Any) -> Any:
        recorded_kwargs.update(kwargs)
        raise OSError("stop after recording Popen kwargs")

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _fake_popen)
    monkeypatch.setenv("LEAKED_VAR", "parent_secret")

    custom_env = {"PLAIN": "1", "OPENAI_API_KEY": "keep"}
    worker = BaseProcessWorker(1, "unused.py", env=custom_env, start=False)
    try:
        worker.respawn()
    except Exception:
        pass
    env = recorded_kwargs.get("env")
    assert env == custom_env
    assert "LEAKED_VAR" not in env
    assert env.get("OPENAI_API_KEY") == "keep"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_child_signal_kills_grandchild() -> None:
    import os
    import signal
    import sys
    import time
    from plugin.framework.managed_subprocess import ManagedSubprocess

    code = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(120)\n"
        "    os._exit(0)\n"
        "sys.stdout.write('GRANDCHILD %s\\n' % pid)\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n"
    )
    managed = ManagedSubprocess([sys.executable, "-c", code])
    managed.start()
    child = Child(managed, worker_name="Formula", worker_id=1)
    gpid: int | None = None
    try:
        assert managed.stdout is not None
        line = managed.stdout.readline()
        text = line.decode()
        assert text.startswith("GRANDCHILD "), text
        gpid = int(text.split()[1])

        leader_pid = managed.pid
        assert leader_pid is not None
        assert os.getpgid(leader_pid) == leader_pid

        assert child.signal_from() is True

        def _is_alive(pid: int) -> bool:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _is_alive(gpid):
            time.sleep(0.05)
        assert not _is_alive(gpid), f"grandchild {gpid} was still alive after 5s"
    finally:
        if gpid is not None:
            try:
                os.kill(gpid, signal.SIGKILL)
            except OSError:
                pass
        managed.close()




