# WriterAgent - Python Compute Service worker pool tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import pickle
import threading
import time
from pathlib import Path
from typing import Any

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

    def cap_stderr_log(self) -> None:
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
    survived until the parent exited. Unlink also ran while the parent
    stderr handle was still open. Windows will not delete that file, and
    the error was swallowed.
    """
    import os

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

    seen: list[str] = []
    opened: list[tuple[object, str]] = []
    real_open = worker._open_stderr_log

    def _open() -> tuple[object, str]:
        handle, path = real_open()
        opened.append((handle, path))
        return handle, path

    worker._open_stderr_log = _open  # type: ignore[method-assign]

    def _popen(*_args: object, **kwargs: object) -> _Proc:
        # The window after the unlocked check: shutdown sets the flag and
        # kill() reaps whatever is published. The stderr path is not
        # published until Popen returns, so this kill must not unlink it.
        # The parent handle stays open until Popen returns: the child has
        # to inherit it.
        handle = kwargs["stderr"]
        assert opened
        logged, path = opened[-1]
        assert handle is logged
        assert getattr(handle, "closed", True) is False
        assert Path(path).exists()
        seen.append(path)
        worker._shutting_down = True
        worker.kill()
        assert Path(path).exists()
        return proc

    real_unlink = os.unlink

    def _unlink(path: str) -> None:
        assert opened
        assert getattr(opened[-1][0], "closed", False) is True
        real_unlink(path)

    monkeypatch.setattr("plugin.framework.process_worker.os.unlink", _unlink)
    monkeypatch.setattr("plugin.framework.process_worker.subprocess.Popen", _popen)
    worker.respawn()
    assert worker.process is None
    assert proc.killed
    assert worker._stderr_path is None
    assert seen
    assert not Path(seen[0]).exists()


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
        "plugin.framework.process_worker.read_pickle_frame_with_timeout",
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
        "plugin.framework.process_worker.read_pickle_frame_with_timeout",
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
    ensured = worker._ensure_live_process(_Deadline(0))
    assert isinstance(ensured, dict)
    assert ensured.get("code") == "EXECUTION_TIMEOUT"
    ensured_short = worker._ensure_live_process(_Deadline(0.5))
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

    from compute_service.worker_base import BaseProcessWorker, _Deadline

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
    monkeypatch.setattr(_Deadline, "usable", lambda self: next(answers))

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
            pool._idle[worker] = 0.0  # type: ignore[index]
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
        monkeypatch.setattr("plugin.framework.process_worker.read_pickle_frame_with_timeout", lambda *_args, **_kwargs: None)
    else:
        monkeypatch.setattr("plugin.framework.process_worker.write_packed_frame_with_timeout", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            "plugin.framework.process_worker.read_pickle_frame_with_timeout",
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
    monkeypatch.setattr("plugin.framework.process_worker.read_pickle_frame_with_timeout", lambda *_args, **_kwargs: None)

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
    from plugin.framework import process_worker as worker_base
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
    from compute_service.worker_base import _Deadline

    popen_calls: list[object] = []

    def _popen(*_args: object, **_kwargs: object) -> object:
        popen_calls.append(_args)
        raise AssertionError("Popen when both budgets were passed")

    monkeypatch.setattr(worker_base.subprocess, "Popen", _popen)
    worker = worker_base.BaseProcessWorker(1, "unused.py", worker_name="Budget", start=False)
    with pytest.raises(ValueError, match="not both"):
        worker.respawn(timeout_sec=1.0, deadline=_Deadline(5.0))
    assert popen_calls == []


def test_stderr_log_name_uses_worker_name() -> None:
    """The temp file names the pool, not a generic compute worker.

    What was wrong: Kokoro and vision used ``wa-compute-w{id}-``, so a
    leaked file in ``/tmp`` looked like a formula worker.
    """
    import os
    from typing import IO

    from plugin.framework.process_worker import BaseProcessWorker

    cases = (
        ("Kokoro", 1, "wa-kokoro-w1-"),
        ("Formula worker", 2, "wa-formula-worker-w2-"),
        ("---", 3, "wa-worker-w3-"),
    )
    opened: list[tuple[IO[bytes], str]] = []
    try:
        for name, worker_id, prefix in cases:
            worker = BaseProcessWorker(worker_id, "unused.py", worker_name=name, start=False)
            handle, path = worker._open_stderr_log()
            opened.append((handle, path))
            base = os.path.basename(path)
            assert base.startswith(prefix), base
            assert base.endswith(".stderr")
    finally:
        for handle, path in opened:
            handle.close()
            os.unlink(path)


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

    monkeypatch.setattr(worker_base.subprocess, "Popen", lambda *_args, **_kwargs: _Proc())
    monkeypatch.setattr(worker_base, "read_pickle_frame_with_timeout", _read)
    monkeypatch.setattr(worker_base, "optimize_popen_pipes", lambda _proc: None)
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

    monkeypatch.setattr(worker_base.subprocess, "Popen", lambda *_args, **_kwargs: _Proc())
    monkeypatch.setattr(worker_base, "read_pickle_frame_with_timeout", _read)
    monkeypatch.setattr(worker_base, "optimize_popen_pipes", lambda _proc: None)
    with caplog.at_level(logging.INFO, logger="compute_service.worker"):
        worker.respawn(timeout_sec=2.0)
    matches = [record for record in caplog.records if "spawn handshake was not ready" in record.message]
    assert len(matches) == 1
    assert matches[0].levelno == logging.INFO
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
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

    from plugin.framework import process_worker as worker_base

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
    worker.on_process_exit = lambda _slot, pid: exited.append(pid)
    worker.process = child  # type: ignore[assignment]
    popped: list[object] = []

    def _popen(*_args: object, **_kwargs: object) -> object:
        popped.append(_args)
        raise AssertionError("Popen while the previous pid is still alive")

    monkeypatch.setattr(worker_base.subprocess, "Popen", _popen)
    worker.respawn()
    assert exited == []
    assert worker.process is child
    assert worker.reap_incomplete()
    assert child.killed
    assert popped == []


def test_reap_reports_exit_after_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exit callback runs once wait() has reaped the pid, still holding the lifecycle lock.

    The callback must not call kill, respawn, or request_shutdown. That lock
    is not re-entrant, and dropping it would let the callback start a second child.
    """
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
    exited: list[tuple[int, int]] = []
    held: list[bool] = []

    def _on_exit(slot: object, pid: int) -> None:
        del slot
        held.append(worker._lifecycle_lock.locked())
        exited.append((worker.worker_id, pid))

    worker.on_process_exit = _on_exit
    worker.process = child  # type: ignore[assignment]
    assert worker._reap_previous_process() is True
    assert exited == [(worker.worker_id, 7)]
    assert held == [True]
    assert worker.process is None
    assert not worker.reap_incomplete()


def test_stderr_log_fd_is_append_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawn log is the exclusive-create fd, opened append-only.

    What was wrong: mkstemp's fd was closed and the path reopened. A name
    swap could replace the file, and only the second open set O_APPEND.
    Capping truncates; the child has to inherit append mode.
    """
    import os
    import sys

    worker = _worker_without_spawn(monkeypatch)
    handle, path = worker._open_stderr_log()
    try:
        fd_stat = os.fstat(handle.fileno())
        path_stat = os.stat(path)
        assert fd_stat.st_ino == path_stat.st_ino
        assert fd_stat.st_dev == path_stat.st_dev
        if sys.platform != "win32":
            import fcntl

            flags = fcntl.fcntl(handle.fileno(), fcntl.F_GETFL)
            assert flags & os.O_APPEND
    finally:
        handle.close()
        os.unlink(path)


def test_adopt_publishes_process_and_stderr_together(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child and its stderr file become visible in one lock hold.

    What was wrong: the path was published first. kill() in that gap
    reaped no process, unlinked the file, then adopt stored a live child
    whose diagnostics read as empty.
    """
    worker = _worker_without_spawn(monkeypatch)
    proc = object()
    assert worker._adopt_spawned_process(proc, "/tmp/wa-stderr") is True  # type: ignore[arg-type]
    assert worker.process is proc
    assert worker._stderr_path == "/tmp/wa-stderr"


def test_adopt_refuses_process_and_stderr_when_shutting_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown stores neither the child nor its stderr path."""
    worker = _worker_without_spawn(monkeypatch)
    worker.request_shutdown()
    proc = object()
    assert worker._adopt_spawned_process(proc, "/tmp/wa-stderr") is False  # type: ignore[arg-type]
    assert worker.process is None
    assert worker._stderr_path is None


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
    worker.cap_stderr_log()
    data = path.read_bytes()
    assert len(data) == _STDERR_LOG_CAP
    assert data.endswith(b"END")
    assert worker._stderr_snippet().endswith("END")


def test_cap_stderr_log_retries_a_short_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A short FileIO.write still leaves the last cap bytes.

    What was wrong: the rewrite ignored write()'s count. os.write may
    return early after the file was already truncated to empty.
    """
    from compute_service.worker_base import _STDERR_LOG_CAP

    worker = _worker_without_spawn(monkeypatch)
    path = tmp_path / "w.stderr"
    path.write_bytes(b"x" * (_STDERR_LOG_CAP + 50) + b"END")
    worker._stderr_path = str(path)
    real_open = open

    class _ShortOnce:
        def __init__(self, raw: Any) -> None:
            self._raw = raw
            self._shorted = False

        def __enter__(self) -> _ShortOnce:
            return self

        def __exit__(self, *exc: object) -> None:
            self._raw.close()

        def seek(self, *args: Any, **kwargs: Any) -> Any:
            return self._raw.seek(*args, **kwargs)

        def truncate(self, *args: Any, **kwargs: Any) -> Any:
            return self._raw.truncate(*args, **kwargs)

        def write(self, data: Any) -> int:
            blob = bytes(data)
            if not self._shorted and len(blob) > 1:
                self._shorted = True
                self._raw.write(blob[:1])
                return 1
            return int(self._raw.write(blob))

    def _open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(file, mode, *args, **kwargs)
        if "r+" in mode:
            return _ShortOnce(handle)
        return handle

    monkeypatch.setattr("builtins.open", _open)
    worker.cap_stderr_log()
    data = path.read_bytes()
    assert len(data) == _STDERR_LOG_CAP
    assert data.endswith(b"END")


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
            pool._idle[worker] = time.monotonic()  # type: ignore[index]
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
    from compute_service.worker_base import _Deadline

    worker = _worker_without_spawn(monkeypatch)
    fractional = worker._timeout_message(_Deadline(1.9))
    assert "1.9" in fractional
    assert "1 seconds" not in fractional
    assert worker._timeout_message(_Deadline(1.0)) == "Execution exceeded maximum timeout of 1 second."
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


class _PolledDead:
    """Child whose poll() already reaped it. is_alive must not clear process."""

    def __init__(self) -> None:
        self.pid = 4

    def poll(self) -> int:
        return 1


def _dead_slot_with_stderr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A dead published child and the stderr file poll() used to leave behind."""
    worker = _worker_without_spawn(monkeypatch)
    path = tmp_path / "idle.stderr"
    path.write_bytes(b"crash tail")
    child = _PolledDead()
    worker.process = child  # type: ignore[assignment]
    worker._stderr_path = str(path)
    return worker, child, path


def test_is_alive_unlinks_stderr_when_poll_reaps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An idle poll() drops the stderr file and leaves the dead Popen in place.

    What was wrong: poll() reaped the zombie and did not unlink _stderr_path.
    A slot that died idle and was never leased again kept the temp file until
    the pool shut down. The process stays so a later kill still reports exit.
    """
    worker, child, path = _dead_slot_with_stderr(monkeypatch, tmp_path)
    assert worker.is_alive() is False
    assert not path.exists()
    assert worker._stderr_path is None
    assert worker.process is child


def test_is_alive_keeps_stderr_while_execute_holds_the_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A request that already holds self.lock still gets the crash tail.

    The IPC loop calls is_alive from execute. Unlinking there would empty
    the snippet _fail_request reads before kill(). The lock is not
    re-entrant, so this acquire must not block.
    """
    worker, child, path = _dead_slot_with_stderr(monkeypatch, tmp_path)
    worker.lock.acquire()
    try:
        assert worker.is_alive() is False
    finally:
        worker.lock.release()
    assert path.read_bytes() == b"crash tail"
    assert worker._stderr_path == str(path)
    assert worker.process is child


def test_is_alive_does_not_wait_on_the_lifecycle_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """poll() returns while another thread holds _lifecycle_lock.

    Idle prune calls is_alive while holding the pool condition. kill() holds
    the lifecycle lock across wait() and the exit callback takes that
    condition. A blocking acquire here is the inverse order.
    """
    worker, _child, path = _dead_slot_with_stderr(monkeypatch, tmp_path)
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
    assert path.read_bytes() == b"crash tail"
    assert worker._stderr_path == str(path)
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
    from compute_service.worker_base import _Deadline

    now = 1_000.0
    monkeypatch.setattr("plugin.framework.process_worker.time.monotonic", lambda: now)
    clock = _Deadline.__new__(_Deadline)
    clock.budget_sec = budget
    clock._end = now + remaining
    assert clock.usable() == usable_sec
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


def test_retry_reap_reports_exit_when_the_child_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second reap reports the pid the first wait could not.

    What was wrong: one timed-out SIGKILL left the slot alive, and nothing
    called kill() again, so on_process_exit never ran for that pid.
    """
    import subprocess

    class _DiesOnSecond:
        def __init__(self) -> None:
            self.pid = 42
            self.kills = 0
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.kills += 1

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            if self.kills < 2:
                raise subprocess.TimeoutExpired(cmd="stuck", timeout=1)
            self.returncode = -9
            return -9

    worker = _worker_without_spawn(monkeypatch)
    child = _DiesOnSecond()
    exited: list[int] = []
    worker.on_process_exit = lambda _slot, pid: exited.append(pid)
    worker.process = child  # type: ignore[assignment]
    assert worker.kill() is None
    assert worker.reap_incomplete()
    assert exited == []
    worker.retry_reap(threading.Event())
    assert child.kills == 2
    assert exited == [42]
    assert worker.process is None
    assert not worker.reap_incomplete()


def test_exit_callback_cannot_reenter(monkeypatch: pytest.MonkeyPatch) -> None:
    """kill, respawn, and request_shutdown raise instead of taking the lifecycle lock.

    The callback already holds that lock. Acquiring it again on the same
    thread deadlocks; the ident check runs first.
    """
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

    seen: list[bool] = []

    def _on_exit(slot: object, pid: int) -> None:
        del pid
        assert slot is worker
        # The callback holds _lifecycle_lock. Reading the flag used to
        # acquire it again and deadlock. kill, respawn, and request_shutdown
        # still refuse that re-entry.
        seen.append(worker.is_shutting_down())
        with pytest.raises(RuntimeError, match="re-entered"):
            worker.kill()
        with pytest.raises(RuntimeError, match="re-entered"):
            worker.request_shutdown()
        with pytest.raises(RuntimeError, match="re-entered"):
            worker.respawn()

    worker.on_process_exit = _on_exit
    worker.process = _Exited()  # type: ignore[assignment]
    worker.kill()
    assert seen == [False]
    assert worker.process is None
    assert not worker.is_shutting_down()


def test_release_does_not_idle_unreaped_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that survived kill stays out of idle until the retry reap.

    What was wrong: release idled any live worker. A timeout whose SIGKILL
    had not landed was handed to the next lease on a desynced pipe, and a
    recycle or idle eviction dropped the slot with no second kill.
    """
    deferred: list[object] = []

    def _defer(func: object, *_args: object, **_kwargs: object) -> None:
        deferred.append(func)

    monkeypatch.setattr("compute_service.worker_base.run_in_background", _defer)
    pool = BaseProcessPool(script_path="unused.py", num_workers=0, idle_worker_ttl_sec=None)

    class _SurvivesKill:
        def __init__(self) -> None:
            self.tasks_executed = 0
            self.worker_id = 3
            self.killed = 0
            self._alive = True
            self._incomplete = False

        def is_alive(self) -> bool:
            return self._alive

        def request_shutdown(self) -> None:
            return None

        def kill(self) -> None:
            self.killed += 1
            self._incomplete = True

        def reap_incomplete(self) -> bool:
            return self._incomplete

        def retry_reap(self, stop: threading.Event) -> None:
            del stop
            self.killed += 1
            self._alive = False
            self._incomplete = False

    worker = _SurvivesKill()
    try:
        worker.kill()
        pool.workers.append(worker)  # type: ignore[arg-type]
        pool._leased.add(worker)  # type: ignore[arg-type]
        pool.release_worker(worker)  # type: ignore[arg-type]
        assert worker not in pool._idle
        assert worker not in pool._leased
        assert worker in pool._unreaped
        assert pool.lease_any(timeout_sec=0) is None
        assert len(deferred) == 1
        watch = deferred[0]
        assert callable(watch)
        watch()
        assert worker not in pool._unreaped
        assert not worker.reap_incomplete()
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



