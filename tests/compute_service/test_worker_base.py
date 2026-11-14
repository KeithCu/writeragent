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
from plugin.framework.warm_child import Child


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
        # The first _is_shutdown check already passed; shutdown lands before the locked block,
        # where killing under _cond would deadlock.
        pool._is_shutdown = True
        return False

    pool.should_recycle_worker = flip  # type: ignore[method-assign]
    thread = threading.Thread(target=pool.release_worker, args=(worker,), daemon=True)
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
    pool.release_worker(worker)  # type: ignore[arg-type]

    assert worker not in pool._leased
    assert worker not in pool._idle


def test_shutdown_kill_exception_continues() -> None:
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

    pool.shutdown()

    assert second_killed == 1
    assert pool._is_shutdown is True


def test_max_tasks_is_at_least_one() -> None:
    """A non-positive max_tasks is the same floor config already requires.

    Otherwise max_tasks=0 makes should_recycle_worker true on every release,
    because tasks_executed >= 0 is always true.
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

    Otherwise holding the singleton lock across factory() blocks shutdown for
    every child spawn and tears down an already-installed pool.
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
    stopper.join(timeout=2.0)
    assert not stopper.is_alive()
    release.set()
    getter.join(timeout=2.0)
    assert not getter.is_alive()
    assert singleton._pool is None
    assert len(discarded) == 1
    assert discarded[0]._is_shutdown
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "shut down" in str(errors[0])


def test_pool_init_kills_workers_when_a_later_spawn_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A constructor that fails mid-loop reaps the children already started.

    Otherwise the exception leaves __init__ with live Popen objects that no
    pool reference tracks.
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
    from compute_service.worker_stdio import unpack_restricted_pickle_frame

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
    from compute_service.worker_stdio import unpack_restricted_pickle_frame

    with pytest.raises(ValueError, match="bytes allocation from an int"):
        unpack_restricted_pickle_frame(payload)


def test_restricted_unpickler_accepts_valid_bytearray() -> None:
    """Legitimate protocol 4 and 5 bytearrays are accepted."""
    import pickle
    from compute_service.worker_stdio import unpack_restricted_pickle_frame

    p4 = pickle.dumps(bytearray(b"xyz"), protocol=4)
    assert unpack_restricted_pickle_frame(p4) == bytearray(b"xyz")
    p5 = pickle.dumps({"b": b"hi", "ba": bytearray(b"yo")}, protocol=5)
    assert unpack_restricted_pickle_frame(p5) == {"b": b"hi", "ba": bytearray(b"yo")}


def test_restricted_unpickler_rejects_oversized_bytearray8() -> None:
    """BYTEARRAY8 length exceeding available frame data is rejected without allocation."""
    import tracemalloc
    from compute_service.worker_stdio import unpack_restricted_pickle_frame

    frame_body = pickle.BYTEARRAY8 + struct.pack("<Q", 4_000_000)
    payload = b"\x80\x05\x95" + struct.pack("<Q", len(frame_body)) + frame_body

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        with pytest.raises(ValueError):
            unpack_restricted_pickle_frame(payload)
        _current, peak = tracemalloc.get_traced_memory()
        assert peak < 1_000_000
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize(
    "payload",
    [
        b"\x80\x05J\x01",
        b"\x80\x05M\x01",
        b"\x80\x05G\x01\x02\x03",
        b"\x80\x05\x95\x01",
        b"\x80\x02J\x01",
    ],
)
def test_restricted_unpickler_maps_struct_error_to_value_error(payload: bytes) -> None:
    """Truncated headers raising struct.error are mapped to ValueError."""
    from compute_service.worker_stdio import unpack_restricted_pickle_frame

    with pytest.raises(ValueError, match="unpack requires a buffer"):
        unpack_restricted_pickle_frame(payload)


def test_host_unpicklers_share_allowlist_and_keep_policies() -> None:
    """Compute and scripting frames share one unpickler; the allowlists stay different."""
    import pickle

    from compute_service.worker_stdio import RestrictedUnpickler, unpack_restricted_pickle_frame
    from plugin.framework.ipc import AllowlistUnpickler, _SafeUnpickler, unpack_pickle_frame

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
    from compute_service.worker_stdio import run_worker_stdio_loop

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


def test_run_worker_stdio_loop_oversized_result_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversized result writes RESULT_TOO_LARGE error frame without crashing the worker."""
    import io
    import types
    from compute_service.worker_stdio import run_worker_stdio_loop
    from plugin.framework.ipc import pack_pickle_frame, read_pickle_frame

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
    import types
    from compute_service.worker_stdio import run_worker_stdio_loop
    from plugin.framework.ipc import pack_pickle_frame, read_pickle_frame

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


def test_reaper_survives_tick_exception() -> None:
    """One bad eviction tick must not kill the reaper.

    Otherwise an unhandled OSError from is_alive() or join ends the daemon,
    and idle workers and shared sessions are never evicted again.
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


def test_run_compute_worker_sets_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compute children mark themselves as workers so host tool calls are refused."""
    import os

    from compute_service.worker_stdio import run_compute_worker

    seen: dict[str, str | None] = {}

    def _loop(handler: object, *, max_payload_bytes: int = 0) -> int:
        del handler, max_payload_bytes
        seen["is_worker"] = os.environ.get("WRITERAGENT_IS_WORKER")
        seen["compute"] = os.environ.get("WRITERAGENT_COMPUTE_WORKER")
        return 0

    monkeypatch.setattr("compute_service.worker_stdio.run_worker_stdio_loop", _loop)
    monkeypatch.delenv("WRITERAGENT_IS_WORKER", raising=False)
    monkeypatch.delenv("WRITERAGENT_COMPUTE_WORKER", raising=False)

    assert run_compute_worker(lambda _req: {"status": "ok"}) == 0
    assert seen["is_worker"] == "1"
    assert seen["compute"] == "1"


def test_configure_compute_worker_env_preserves_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-existing math thread environment variables are not overwritten."""
    import os

    from compute_service.worker_stdio import COMPUTE_THREAD_ENV_VARS, configure_compute_worker_env

    for var_name in COMPUTE_THREAD_ENV_VARS:
        monkeypatch.setenv(var_name, "4")

    configure_compute_worker_env()

    for var_name in COMPUTE_THREAD_ENV_VARS:
        assert os.environ.get(var_name) == "4"


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


def test_compute_worker_spawn_runs_on_supervisor_spawner_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial spawn and respawn run Popen on the supervisor spawner thread."""
    from plugin.framework.managed_subprocess import ManagedSubprocess
    from plugin.framework.process_worker import BaseProcessWorker
    from plugin.framework.warm_child import supervisor

    supervisor_obj = supervisor
    start_idents: list[int] = []
    orig_start = ManagedSubprocess.start

    def tracked_start(self: ManagedSubprocess) -> None:
        start_idents.append(threading.get_ident())
        return orig_start(self)

    monkeypatch.setattr(ManagedSubprocess, "start", tracked_start)

    script = Path(__file__).resolve().parents[2] / "compute_service" / "formula_worker.py"
    worker = BaseProcessWorker(1, str(script), worker_name="Formula")
    try:
        assert supervisor_obj.spawner_ident is not None
        assert start_idents == [supervisor_obj.spawner_ident]

        caller_ident: list[int] = []
        done = threading.Event()

        def respawn_from_caller() -> None:
            caller_ident.append(threading.get_ident())
            worker.kill()
            worker.respawn()
            done.set()

        thread = threading.Thread(target=respawn_from_caller)
        thread.start()
        assert done.wait(timeout=30)
        assert caller_ident[0] != supervisor_obj.spawner_ident
        assert start_idents == [supervisor_obj.spawner_ident, supervisor_obj.spawner_ident]
    finally:
        worker.kill()


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


def test_release_does_not_idle_a_dead_worker() -> None:
    """A worker that is not alive after release is not idled, and can be cold-claimed.

    kill() drops an unreapable child so the next lease respawns it; otherwise
    release hands a dead or stuck worker to the next lease.
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


def test_install_image_pkgs_adds_sibling_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pkgs directory next to the service is inserted once.

    The image publishes wheels through PYTHONPATH, and the worker scrub
    removes that variable. The directory itself has to be added by path.
    A missing directory leaves sys.path alone.
    """

    from compute_service.worker_stdio import install_image_pkgs

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


def test_run_compute_worker_puts_image_pkgs_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import compute_service.worker_stdio as ws

    pkgs = tmp_path / "pkgs"
    pkgs.mkdir()
    monkeypatch.setattr(ws, "_SERVICE_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "path", ["/usr/lib/python"])

    seen_path_zero: list[str] = []

    def fake_loop(handler: Any, *, max_payload_bytes: int = 0) -> int:
        seen_path_zero.append(sys.path[0])
        return 0

    monkeypatch.setattr(ws, "run_worker_stdio_loop", fake_loop)
    ret = ws.run_compute_worker(lambda req: {})
    assert ret == 0
    assert seen_path_zero == [str(pkgs)]


def _delay_worker(tmp_path):
    """Stdio worker that sleeps for ``delay`` seconds, then answers with its pid."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    script = tmp_path / "delay_worker.py"
    script.write_text(
        "\n".join(
            [
                "import os, sys, time",
                f"sys.path.insert(0, {str(repo)!r})",
                "from compute_service.worker_stdio import run_worker_stdio_loop",
                "def handle(req):",
                "    time.sleep(float(req.get('delay') or 0))",
                "    return {'status': 'ok', 'pid': os.getpid()}",
                "if __name__ == '__main__':",
                "    raise SystemExit(run_worker_stdio_loop(handle))",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return script


def test_read_timeout_kills_and_next_execute_respawns(tmp_path) -> None:
    """A clean read timeout kills the child. The next execute gets a new pid.

    The timed-out client already has EXECUTION_TIMEOUT. Keeping the process
    to finish that frame is what the late-drain state machine existed for.
    """
    from compute_service.worker_base import BaseProcessPool

    pool = BaseProcessPool(str(_delay_worker(tmp_path)), num_workers=1, worker_name="Worker")
    try:
        worker = pool.lease_any(2)
        assert worker is not None
        assert worker.process is not None
        pid = worker.process.pid
        res = worker.execute({"delay": 30}, timeout_sec=1.0)
        assert res.get("code") == "EXECUTION_TIMEOUT"
        assert not worker.is_alive()
        pool.release_worker(worker)

        again = pool.lease_any(2)
        assert again is worker
        nxt = again.execute({"delay": 0}, timeout_sec=2)
        assert nxt.get("status") == "ok"
        assert nxt.get("pid") != pid
        pool.release_worker(again)
    finally:
        pool.shutdown()
