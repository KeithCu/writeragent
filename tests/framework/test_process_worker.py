# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
import pickle
import subprocess
import sys
import time
import threading
from typing import Any

import pytest

from plugin.framework.managed_subprocess import ManagedSubprocess, _instances
from plugin.framework.process_worker import BaseProcessWorker
from plugin.framework.warm_child import Child
from plugin.framework.worker_pool import StderrTail


class _Pipe:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.events.append(self.name)


class _Proc:
    def __init__(
        self,
        pid: int = 42,
        *,
        stuck: bool = False,
        events: list[str] | None = None,
        on_kill: threading.Event | None = None,
    ) -> None:
        self.pid = pid
        self.stuck = stuck
        self.events: list[str] = [] if events is None else events
        self.stdin = _Pipe("stdin", self.events)
        self.stdout = _Pipe("stdout", self.events)
        self.stderr = _Pipe("stderr", self.events)
        self.returncode: int | None = None
        self.kills = 0
        self.on_kill = on_kill

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.kills += 1
        self.events.append("kill")
        if self.on_kill is not None:
            self.on_kill.set()

    def wait(self, timeout: float | None = None) -> int:
        self.events.append("wait")
        if self.stuck:
            raise subprocess.TimeoutExpired(cmd=["proc"], timeout=timeout or 1.0)
        self.returncode = -9
        return -9


def _child(
    proc: Any,
    tail: Any = None,
    *,
    worker_name: str = "Worker",
    worker_id: int = 1,
) -> Child:
    managed = ManagedSubprocess.wrap_existing(proc, label=f"{worker_name} #{worker_id}", stderr_tail=tail)
    return Child(managed, worker_name=worker_name, worker_id=worker_id)


class _NoJoinThread(threading.Thread):
    def join(self, timeout: float | None = None) -> None:
        raise AssertionError("join must not be called on abandoned child")


@pytest.fixture(autouse=True)
def reaper_enqueued(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    items: list[Any] = []
    monkeypatch.setattr("plugin.framework.managed_subprocess._enqueue_reaper", items.append)
    return items


def test_signal_without_an_owner_closes_then_waits() -> None:
    proc = _Proc(pid=10)
    child = _child(proc)
    assert child.signal_from() is True
    assert proc.events == ["kill", "stdin", "stdout", "wait"]
    assert proc.stdin.closed is True
    assert proc.stdout.closed is True
    assert proc.stderr.closed is False
    assert proc.kills == 1

    assert child.signal_from() is True
    assert proc.events == ["kill", "stdin", "stdout", "wait"]
    assert proc.kills == 1
    assert child.end_io() is True
    assert proc.events == ["kill", "stdin", "stdout", "wait"]

    proc2 = _Proc(pid=11)
    child2 = _child(proc2)
    child2.begin_io()
    assert child2.end_io() is False
    assert proc2.events == []
    assert child2.signal_from() is True
    assert proc2.events == ["kill", "stdin", "stdout", "wait"]
    assert proc2.stdin.closed is True
    assert proc2.stdout.closed is True
    assert proc2.stderr.closed is False
    assert proc2.kills == 1


def test_signal_during_io_defers_finish_to_end_io() -> None:
    proc = _Proc(pid=20)
    child = _child(proc)
    child.begin_io()

    sig_res: list[bool] = []
    t = threading.Thread(target=lambda: sig_res.append(child.signal_from()))
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert sig_res == [False]
    assert proc.events == ["kill"]
    assert proc.stdin.closed is False
    assert proc.stdout.closed is False
    assert proc.stderr.closed is False

    assert child.end_io() is True
    assert proc.events == ["kill", "stdin", "stdout", "wait"]
    assert proc.stdin.closed is True
    assert proc.stdout.closed is True
    assert proc.stderr.closed is False


def test_owner_signal_from_reaps_immediately() -> None:
    proc = _Proc(pid=30)
    child = _child(proc)
    child.begin_io()
    assert child.signal_from() is True
    assert proc.events == ["kill", "stdin", "stdout", "wait"]
    assert proc.kills == 1
    assert child.end_io() is True
    assert proc.events == ["kill", "stdin", "stdout", "wait"]
    assert proc.kills == 1


def test_non_owner_end_io_does_not_close() -> None:
    proc = _Proc(pid=40)
    child = _child(proc)
    child.begin_io()

    other_res: list[bool] = []
    t = threading.Thread(target=lambda: other_res.append(child.end_io()))
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert other_res == [False]
    assert proc.events == []
    assert proc.stdin.closed is False

    # The owner's own end_io without a pending signal also leaves the child open.
    assert child.end_io() is False
    assert proc.events == []


def test_owner_end_io_racing_signal_from_reaps_once() -> None:
    owner_started = threading.Event()
    kill_called = threading.Event()

    proc = _Proc(pid=50, on_kill=kill_called)
    child = _child(proc)

    end_res: list[bool] = []
    sig_res: list[bool] = []

    def owner_run() -> None:
        child.begin_io()
        owner_started.set()
        assert kill_called.wait(timeout=2.0)
        end_res.append(child.end_io())

    def killer_run() -> None:
        assert owner_started.wait(timeout=2.0)
        sig_res.append(child.signal_from())

    t_owner = threading.Thread(target=owner_run)
    t_killer = threading.Thread(target=killer_run)

    t_owner.start()
    t_killer.start()

    t_owner.join(timeout=2.0)
    t_killer.join(timeout=2.0)

    assert not t_owner.is_alive()
    assert not t_killer.is_alive()

    assert end_res == [True]
    assert sig_res in ([True], [False])
    assert proc.stdin.closed is True
    assert proc.stdout.closed is True
    assert proc.stderr.closed is False
    assert proc.events.count("wait") == 1
    assert proc.kills == 1


def test_abandoned_after_wait_timeout(
    caplog: pytest.LogCaptureFixture, reaper_enqueued: list[Any]
) -> None:
    caplog.set_level(logging.WARNING, logger="compute_service.worker")

    tail = StderrTail()
    tail._append("still-running\n")
    no_join_thread = _NoJoinThread()
    tail.attach_thread(no_join_thread)

    proc = _Proc(pid=60, stuck=True)
    child = _child(proc, tail=tail)

    assert child.diagnostic_tail() == "still-running"

    assert child.signal_from() is True
    assert proc.events == ["kill", "stdin", "stdout", "wait"]
    assert proc.stderr.closed is False
    assert reaper_enqueued == [child.managed]

    warning_records = [
        r
        for r in caplog.records
        if r.name == "compute_service.worker" and "still alive after SIGKILL" in r.message
    ]
    assert len(warning_records) == 1
    msg = warning_records[0].message
    assert "abandoning" in msg
    assert f"pid={proc.pid}" in msg

    assert child.diagnostic_tail() == "still-running"

    assert child.signal_from() is True
    assert proc.kills == 2
    assert proc.events.count("wait") == 1
    assert len(reaper_enqueued) == 1



def test_respawn_adopt_refusal_signals_the_unadopted_child(
    monkeypatch: pytest.MonkeyPatch, reaper_enqueued: list[Any]
) -> None:
    worker = BaseProcessWorker(7, "unused.py", worker_name="Formula", start=False)
    read_calls: list[Any] = []
    monkeypatch.setattr(
        "plugin.framework.warm_child.read_pickle_frame",
        lambda *args, **kwargs: (read_calls.append(args), {"status": "ready"})[1],
    )
    monkeypatch.setattr(
        "plugin.framework.managed_subprocess.optimize_popen_pipes",
        lambda proc: None,
    )
    monkeypatch.setattr(
        "plugin.framework.managed_subprocess.start_stderr_drain",
        lambda *args, **kwargs: None,
    )

    procs: list[_Proc] = []

    def popen_stub(*args: Any, **kwargs: Any) -> _Proc:
        p = _Proc(pid=77)
        procs.append(p)
        worker.request_shutdown()
        return p

    monkeypatch.setattr(subprocess, "Popen", popen_stub)

    initial_instances = set(_instances)

    worker.respawn()

    assert read_calls == []
    assert len(procs) == 1
    proc = procs[0]
    assert proc.kills == 1
    assert proc.stdin.closed is True
    assert proc.stdout.closed is True
    assert proc.stderr.closed is False
    assert proc.events.count("wait") == 1
    assert reaper_enqueued == []
    assert worker.child is None
    assert worker.process is None
    assert set(_instances) == initial_instances


def test_adopt_refusal_does_not_signal_the_published_child() -> None:
    worker = BaseProcessWorker(1, "unused.py", start=False)
    published_proc = _Proc(pid=1)
    worker.process = published_proc
    published_child = worker.child
    assert published_child is not None

    loser_proc = _Proc(pid=2)
    loser_child = _child(loser_proc)

    assert worker._adopt(loser_child) is False
    assert worker.process is published_proc
    assert published_proc.kills == 0

    assert loser_child.signal_from() is True
    assert loser_proc.kills == 1
    assert loser_proc.stdin.closed is True
    assert loser_proc.stdout.closed is True
    assert loser_proc.stderr.closed is False
    assert published_proc.kills == 0
    assert worker.child is published_child


def _second_spawn_queued_event(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Return an event set when a second compute respawn enters ``Supervisor.spawn``.

    ``Popen`` is serialized on the supervisor spawner thread, so two overlapping
    ``respawn`` calls queue a second spawn while the first is still between
    ``start`` and ``publish``. Tests that need to synchronize inside ``Popen``
    should gate the first ``Popen`` on this event instead.
    """
    from plugin.framework.warm_child import supervisor

    sup = supervisor
    real_spawn = sup.spawn
    attempts = {"n": 0}
    second_queued = threading.Event()

    def patched_spawn(managed: Any, timeout_sec: float | None = None) -> None:
        attempts["n"] += 1
        if attempts["n"] == 2:
            second_queued.set()
        real_spawn(managed, timeout_sec=timeout_sec)

    monkeypatch.setattr(sup, "spawn", patched_spawn)
    return second_queued


def test_execute_does_not_respawn_during_shutdown() -> None:
    """A dead child during pool shutdown must not start a new interpreter.

    Otherwise execute sees a dead process under the lock, spawns a new child
    after shutdown has begun, runs the cell, and then kills it.
    """

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

    Adopt refuses that child, and only that child is signaled; otherwise reap
    sees None and the new interpreter survives until the parent exits.
    """

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
    from plugin.framework.child_runtime import Deadline

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


def test_execute_timeout_does_not_increment_tasks_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout does not count. Only a consumed response moves tasks_executed."""
    from unittest.mock import MagicMock

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
        "plugin.framework.warm_child.read_pickle_frame",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="worker", timeout=1.0)),
    )

    res = worker.execute({"code": "time.sleep(10)"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert worker.tasks_executed == 0
    assert worker.process is None


def test_execute_stdin_write_timeout_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged stdin write kills the child and returns.

    Otherwise a blocking write under the worker lock never returns and the
    slot stays leased.
    """
    from unittest.mock import MagicMock

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    worker.process = MagicMock()
    worker.process.poll.return_value = None
    worker.process.stdin = MagicMock()
    worker.process.stdout = MagicMock()
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "plugin.framework.warm_child.write_packed_frame_with_timeout",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="IPC frame", timeout=1.0)),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert killed == [True]
    assert worker.tasks_executed == 0


def test_partial_frame_timeout_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mid-frame deadline is EXECUTION_TIMEOUT and kills the child.

    Otherwise the next read treats the rest of this frame as a new response.
    """
    from unittest.mock import MagicMock
    from plugin.framework.ipc import IpcPartialFrameTimeout

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
        "plugin.framework.warm_child.read_pickle_frame",
        MagicMock(side_effect=IpcPartialFrameTimeout("IPC frame stream desynchronized: timeout mid-frame")),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert killed == [True]
    assert worker.tasks_executed == 0


def test_execute_payload_too_large_does_not_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized frame is rejected before any byte is written, so the kernel stays."""
    from unittest.mock import MagicMock
    from plugin.framework.ipc import IpcPayloadSizeError

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

    Otherwise timeout_sec <= 0 spawns a child and immediately SIGKILLs it
    when the handshake cannot finish.
    """
    from plugin.framework.child_runtime import Deadline

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
    # Nothing was dispatched; counting this would move a live worker toward max_tasks.
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

    Otherwise flooring a spent deadline at 0.01s starts the write, times out,
    and SIGKILLs a child that never received a byte.
    """
    from unittest.mock import MagicMock

    from plugin.framework.child_runtime import Deadline

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
    monkeypatch.setattr("plugin.framework.warm_child.write_packed_frame_with_timeout", lambda *args, **kwargs: writes.append(args))
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


def test_spawn_failure_does_not_increment_tasks_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slot that never becomes live does not move toward max_tasks.

    The next handshake resets the count, and this failure never ran a cell.
    """

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
            "plugin.framework.warm_child.write_packed_frame_with_timeout",
            MagicMock(side_effect=BrokenPipeError("closed")),
        )
    elif kind == "empty":
        monkeypatch.setattr("plugin.framework.warm_child.write_packed_frame_with_timeout", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", lambda *_args, **_kwargs: None)
    else:
        monkeypatch.setattr("plugin.framework.warm_child.write_packed_frame_with_timeout", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            "plugin.framework.warm_child.read_pickle_frame",
            MagicMock(side_effect=RuntimeError("died")),
        )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == code
    assert worker.tasks_executed == 0
    assert killed == [True]


def test_empty_read_during_shutdown_is_service_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """EOF while the pool is stopping is SERVICE_SHUTDOWN, not a counted crash."""
    from unittest.mock import MagicMock


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
    monkeypatch.setattr("plugin.framework.warm_child.write_packed_frame_with_timeout", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", lambda *_args, **_kwargs: None)

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "SERVICE_SHUTDOWN"
    assert worker.tasks_executed == 0
    assert killed == []


def test_write_broken_during_shutdown_is_service_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pipe error while the pool is stopping is SERVICE_SHUTDOWN, not a kill.

    The child is already being reaped, so this must not kill again.
    """
    from unittest.mock import MagicMock


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
        "plugin.framework.warm_child.write_packed_frame_with_timeout",
        MagicMock(side_effect=BrokenPipeError("closed")),
    )

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "SERVICE_SHUTDOWN"
    assert worker.tasks_executed == 0
    assert killed == []


@pytest.mark.parametrize("kind", ["value", "reset", "timeout"])
def test_read_error_during_shutdown_is_service_shutdown(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """A read failure while the pool is stopping is SERVICE_SHUTDOWN, not a crash.

    Windows raises TimeoutExpired from is_alive before the read sees EOF. The
    child is already being reaped, so this must not kill again.
    """
    from unittest.mock import MagicMock


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
    monkeypatch.setattr("plugin.framework.warm_child.write_packed_frame_with_timeout", lambda *_args, **_kwargs: None)
    if kind == "value":
        exc: BaseException = ValueError("I/O operation on closed file")
    elif kind == "reset":
        exc = ConnectionResetError("reset")
    else:
        exc = subprocess.TimeoutExpired(cmd="IPC frame", timeout=1)
    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", MagicMock(side_effect=exc))

    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "SERVICE_SHUTDOWN"
    assert worker.tasks_executed == 0
    assert killed == []


def test_other_thread_kill_leaves_pipes_until_the_owner_leaves(monkeypatch: pytest.MonkeyPatch) -> None:
    """kill() from another thread only signals while this thread owns the pipes.

    The owner closes stdin and stdout when it leaves; otherwise closing them
    between select and read raises or hits a reused fd number.
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


def test_abandoned_child_is_not_written_and_a_later_spawn_may_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that survives the reap is not sent a request, and the slot may spawn.

    kill() drops that child; otherwise poll() stays None, execute writes to a
    process that never said ready, and the next call refuses to spawn.
    """

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
    monkeypatch.setattr("plugin.framework.warm_child.write_packed_frame_with_timeout", lambda *_args, **_kwargs: written.append(True))
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

    Otherwise ``_reap_previous_process`` can block for a second and Popen
    still runs after the request's deadline is gone.
    """

    clock = {"t": 1000.0}
    monkeypatch.setattr("plugin.framework.child_runtime.time.monotonic", lambda: clock["t"])
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
    from plugin.framework.child_runtime import Deadline

    clock = {"t": 1000.0}
    monkeypatch.setattr("plugin.framework.child_runtime.time.monotonic", lambda: clock["t"])
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
    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", _read)
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


def test_stderr_drain_name_uses_worker_name() -> None:
    """The drain thread names the pool, or falls back to a generic prefix when slugless."""
    for name, worker_id, expected in (
        ("Kokoro", 1, "wa-kokoro-w1-stderr"),
        ("---", 3, "wa-worker-w3-stderr"),
    ):
        worker = BaseProcessWorker(worker_id, "unused.py", worker_name=name, start=False)
        assert worker._drain_name() == expected


def test_popen_past_deadline_skips_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup that overruns the deadline kills the child and does not read a frame.

    The test spends the request clock inside Popen via the monotonic patch,
    while the spawn wait uses wall time. A handshake after that would use the
    0.01s pipe-wait floor and then SIGKILL.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr("plugin.framework.child_runtime.time.monotonic", lambda: clock["t"])
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
    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", _read)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    worker = _worker_without_spawn(monkeypatch)
    res = worker.execute({"code": "result = 1"}, timeout_sec=2.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert reads == []
    assert proc.killed
    assert worker.process is None


def test_partial_handshake_timeout_logs_timeout(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A mid-frame ready handshake is a timeout, not a generic spawn failure.

    IpcPartialFrameTimeout is a ConnectionError, so it must match the timeout
    arm rather than falling through to the generic ConnectionError branch.
    """
    from plugin.framework.ipc import IpcPartialFrameTimeout

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
    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", _read)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    worker = _worker_without_spawn(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
        worker.respawn()
    assert "spawn handshake timed out" in caplog.text
    assert "Failed to spawn" not in caplog.text
    assert worker.process is None


def test_shutdown_handshake_is_not_a_spawn_error(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A ready frame that disappears because shutdown won is not an error.

    kill() during the handshake makes the read return empty; shutting down
    must log at INFO and emit no ERROR record.
    """
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
    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", _read)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    with caplog.at_level(logging.INFO, logger="compute_service.worker"):
        worker.respawn()
    info_records = [record for record in caplog.records if record.levelno == logging.INFO]
    assert len(info_records) == 1
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert worker.process is None


def test_reap_timeout_abandons_and_allows_another_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child still alive after the reap wait is dropped, and the slot may spawn.

    The survivor is handed to the module reaper. It is not this slot's child,
    so the next respawn may Popen.
    """

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
    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", lambda *_args, **_kwargs: {"status": "ready", "pid": 43})
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

    On Windows, leaving those fds open until collection also keeps the child's
    ends open. A child still alive after the wait is abandoned with those pipes
    closed, the slot is cleared, and stderr stays with the drain.
    """

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
    """A 1s budget with ~20ms left does not Popen; usable() still returns it.

    Otherwise respawn spawns with almost no time left for the handshake.
    """
    from plugin.framework import child_runtime as worker_mod

    monkeypatch.setattr("plugin.framework.child_runtime.time.monotonic", lambda: 1000.0)
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

    monkeypatch.setattr("plugin.framework.child_runtime.time.monotonic", _moved)
    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert popen_calls == []
    assert worker.tasks_executed == 0


def test_one_second_budget_above_spawn_floor_still_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1s budget with 0.5s left still reaches Popen. That case stays usable."""

    calls = {"n": 0}

    def _moved() -> float:
        calls["n"] += 1
        if calls["n"] == 1:
            return 1000.0
        return 1000.5

    monkeypatch.setattr("plugin.framework.child_runtime.time.monotonic", _moved)
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
    """Adopt stores the Child in one lock hold; shutdown and a live child refuse.

    Otherwise kill() in the gap unlinks diagnostics before adopt stores the
    live child, or a second adopt overwrites the published process.
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

    Popen runs serially on the supervisor spawner thread, so the first Popen
    waits until the second respawn reaches ``Supervisor.spawn``; otherwise a
    second assignment leaks the first Popen.
    """

    worker = _worker_without_spawn(monkeypatch)
    second_queued = _second_spawn_queued_event(monkeypatch)
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
            if len(children) == 1:
                assert second_queued.wait(timeout=2)
                assert release.wait(timeout=2)
        return proc

    def _read(_stdout: object, _timeout_sec: float, **_kwargs: object) -> dict[str, object]:
        return {"status": "ready", "pid": 1}

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _popen)
    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", _read)
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
    assert second_queued.wait(timeout=2)
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

    On Windows ``is_alive`` runs before EOF, and the drain keeps the text;
    POSIX calls ``is_alive`` only when select times out.
    """
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
    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", _read)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
        worker.respawn()
    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "startup-crash-traceback" in joined
    assert "stderr=<empty>" not in joined
    assert worker.process is None


def test_respawn_failure_before_adopt_leaves_the_other_child(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A respawn that raises before adopt must not kill the published child.

    Otherwise an overlapping caller failing before adopt reaps the other
    call's healthy child.
    """

    worker = _worker_without_spawn(monkeypatch)
    second_queued = _second_spawn_queued_event(monkeypatch)
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
        if mine == 1:
            assert second_queued.wait(timeout=2)
            proc = _Proc()
            winner["proc"] = proc
            return proc
        deadline = time.monotonic() + 2.0
        while worker.process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        raise OSError("second spawn failed")

    def _drain(_stream: object, **_kwargs: object) -> object:
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
    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", _read)
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

    The pipe is not reusable, so the kill stays.
    """
    from unittest.mock import MagicMock

    from plugin.framework.ipc import IpcFrameReadError

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    monkeypatch.setattr("plugin.framework.warm_child.write_packed_frame_with_timeout", lambda *_args, **_kwargs: None)

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
            "plugin.framework.warm_child.read_pickle_frame",
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
    """A child adopt refused, and that will not die, is abandoned; the slot stays.

    The loser is signaled and the published child is not; otherwise a failed
    wait leaks the loser without storing or logging it.
    """

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
    assert loser.signal_from() is True
    assert enqueued == [loser.managed]
    assert worker.process is published
    assert published.killed is False


def test_fail_request_skips_stderr_when_it_does_not_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure that leaves the child must not attach another child's tail.

    kill=False is shutdown, a clock spent before the frame was sent, or a spawn
    that already logged its own tail.
    """
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
    ``STDERR_LOG_CAP``. The snippet attached to an error is the last
    ``_STDERR_SNIPPET`` of that text. One chunk larger than the cap is
    dropped whole, so the child text arrives in smaller writes.
    """
    from plugin.framework.warm_child import STDERR_LOG_CAP, _STDERR_SNIPPET
    tail = StderrTail(max_chars=STDERR_LOG_CAP)
    piece = "x" * 1024
    for _idx in range((STDERR_LOG_CAP // len(piece)) + 2):
        tail._append(piece)
    tail._append("END")
    text = tail.text()
    assert len(text) <= STDERR_LOG_CAP
    assert text.endswith("END")
    worker = _worker_without_spawn(monkeypatch)
    child = _child(object(), tail=tail, worker_name=worker.worker_name, worker_id=worker.worker_id)
    snippet = child.diagnostic_tail()
    assert snippet.endswith("END")
    assert len(snippet) <= _STDERR_SNIPPET


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

    pack_pickle_frame runs before the first write, so the pipe stays aligned;
    otherwise PicklingError/ValueError/RecursionError escapes as an unhandled 500.
    """
    from unittest.mock import MagicMock


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

    Otherwise catching it with pickle.dumps returns REQUEST_NOT_SERIALIZABLE
    and leaves the child on a desynced frame.
    """
    from unittest.mock import MagicMock


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
        "plugin.framework.warm_child.write_packed_frame_with_timeout",
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
    assert enqueued == [child.managed]
    assert "still-running" in child.diagnostic_tail()


def test_timeout_message_keeps_fractional_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1.9s budget is not reported as 1 second. Whole seconds stay integers."""
    from plugin.framework.child_runtime import Deadline

    worker = _worker_without_spawn(monkeypatch)
    fractional = worker._timeout_message(Deadline(1.9))
    assert "1.9" in fractional
    assert "1 seconds" not in fractional
    assert "1 second." in worker._timeout_message(Deadline(1.0))
    assert "2 seconds." in worker._timeout_message(Deadline(2.0))


def test_is_alive_snapshots_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second read of the child must not run; kill() clears it between reads.

    Otherwise shutdown sets the child pointer to None before poll() and the
    write path reports AttributeError as REQUEST_NOT_SERIALIZABLE.
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

    Otherwise a shutdown race on self.process is swallowed without killing the
    child or counting the task; PicklingError still maps to that code.
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
        "plugin.framework.warm_child.write_packed_frame_with_timeout",
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
    assert res.get("error")
    assert "message" not in res
    bare = worker.execute({"code": "result = 1"}, timeout_sec=0.1)
    assert "id" not in bare


def test_after_send_spent_deadline_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a successful write, a spent deadline kills the child (compute only)."""
    from unittest.mock import MagicMock

    from plugin.framework import child_runtime as clock_mod

    worker = _worker_without_spawn(monkeypatch)
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 42
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    worker.process = proc
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True) or True  # type: ignore[method-assign]
    clock = {"t": 1000.0}
    monkeypatch.setattr(clock_mod.time, "monotonic", lambda: clock["t"])

    def _write_ok(*_args: object, **_kwargs: object) -> None:
        clock["t"] = 1001.0

    monkeypatch.setattr(
        "plugin.framework.warm_child.write_packed_frame_with_timeout",
        _write_ok,
    )

    def _read_stuck(*_args, **_kwargs: object) -> dict[str, str]:
        raise AssertionError("read should not run")

    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", _read_stuck)
    res = worker.execute({"code": "result = 1"}, timeout_sec=1.0)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert killed == [True]


def test_respawn_passes_spawn_budget_to_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.framework.child_runtime import Deadline
    from plugin.framework.warm_child import supervisor

    captured_timeouts: list[float | None] = []

    def mock_spawn(managed: Any, timeout_sec: float | None = None) -> None:
        captured_timeouts.append(timeout_sec)

    def mock_handshake(
        managed: Any, *args: Any, start: Any = None, **kwargs: Any
    ) -> tuple[str, Any, Any]:
        if start is not None:
            start(managed)
        return ("refused", None, None)

    monkeypatch.setattr(supervisor, "spawn", mock_spawn)
    monkeypatch.setattr(
        "plugin.framework.process_worker.spawn_managed_child_with_handshake",
        mock_handshake,
    )

    worker = BaseProcessWorker(1, "unused.py", start=False)

    # 1. Respawn with deadline should pass remaining spawn budget
    worker.respawn(deadline=Deadline(5.0))
    assert len(captured_timeouts) == 1
    t = captured_timeouts[0]
    assert t is not None
    assert 4.0 <= t <= 5.0

    # 2. Respawn without deadline should pass None (falls back to SPAWN_READY_TIMEOUT_SEC in supervisor.spawn)
    worker.respawn(deadline=None)
    assert len(captured_timeouts) == 2
    assert captured_timeouts[1] is None


def test_stuck_kill_drops_the_slot_after_one_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """One timed-out SIGKILL drops the slot. The survivor is not killed again here.

    The module reaper keeps waiting. The slot does not.
    """

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


def test_unpicklable_payload_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pack runs before spawn. A pickle error must not start a child."""
    from unittest.mock import MagicMock


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


def test_respawn_popen_kwargs_scrub_session_and_drain(monkeypatch: pytest.MonkeyPatch) -> None:
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

    def _fake_popen(_args: object, **kwargs: Any) -> _Proc:
        recorded_kwargs.update(kwargs)
        return _Proc()

    drain_calls: list[dict[str, Any]] = []

    def _fake_drain(stream: object, **kwargs: Any) -> Any:
        del stream
        drain_calls.append(kwargs)
        return None

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("plugin.framework.managed_subprocess.start_stderr_drain", _fake_drain)
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _p: None)
    monkeypatch.setattr("plugin.framework.warm_child.read_pickle_frame", lambda *_a, **_kw: {"status": "ready", "pid": 1})

    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("PYTHONPATH", "/custom/path")

    worker = BaseProcessWorker(1, "fake_script.py", worker_name="Budget", start=False)
    try:
        worker.respawn()
        env = recorded_kwargs.get("env")
        assert env is not None
        assert "OPENAI_API_KEY" not in env
        assert "PYTHONPATH" not in env
        if sys.platform != "win32":
            assert recorded_kwargs.get("start_new_session") is True
        else:
            assert "creationflags" in recorded_kwargs
            assert "start_new_session" not in recorded_kwargs
        assert len(drain_calls) == 1
    finally:
        worker.kill()
