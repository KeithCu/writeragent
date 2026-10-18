# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Any

import pytest

from plugin.framework import process_worker
from plugin.framework.managed_subprocess import ManagedSubprocess, _instances
from plugin.framework.process_worker import (
    BaseProcessWorker,
    Child,
    Deadline,
    _spawn_budget,
    error_dict,
)
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
    tail: StderrTail | None = None,
    *,
    worker_name: str = "Worker",
    worker_id: int = 1,
) -> Child:
    managed = ManagedSubprocess.wrap_existing(proc, label=f"{worker_name} #{worker_id}", stderr_tail=tail)
    return Child(managed, worker_name=worker_name, worker_id=worker_id)


class _NoJoinThread(threading.Thread):
    def join(self, timeout: float | None = None) -> None:
        raise AssertionError("join must not be called on abandoned child")


class _CountingJoinThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__()
        self.joins = 0

    def join(self, timeout: float | None = None) -> None:
        self.joins += 1


@pytest.fixture(autouse=True)
def reaper_enqueued(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    items: list[Any] = []
    monkeypatch.setattr("plugin.framework.managed_subprocess._enqueue_reaper", items.append)
    return items


@pytest.mark.parametrize(
    ("budget", "remaining", "expected_usable", "expected_spawn"),
    [
        (1.0, 1.0, 1.0, 1.0),
        (1.0, 0.25, 0.25, 0.25),
        (1.0, 0.2, 0.2, None),
        (1.0, 0.02, 0.02, None),
        (1.0, 0.0, None, None),
        (1.0, -0.1, None, None),
        (2.0, 1.0, 1.0, 1.0),
        (2.0, 0.999, None, None),
        (5.0, 2.5, 2.5, 2.5),
        (0.5, 0.5, None, None),
    ],
)
def test_usable_and_spawn_budget_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    budget: float,
    remaining: float,
    expected_usable: float | None,
    expected_spawn: float | None,
) -> None:
    monkeypatch.setattr(process_worker.time, "monotonic", lambda: 1000.0)
    deadline = Deadline.from_absolute(budget, 1000.0 + remaining)
    usable = deadline.usable()
    spawn = _spawn_budget(deadline)

    if expected_usable is None:
        assert usable is None
    else:
        assert usable == pytest.approx(expected_usable)

    if expected_spawn is None:
        assert spawn is None
    else:
        assert spawn == pytest.approx(expected_spawn)


def test_timeout_message_formatting() -> None:
    worker = BaseProcessWorker(1, "unused.py", start=False)
    assert worker._timeout_message(Deadline(0.25)) == "Execution timeout must be at least 1 second."
    assert worker._timeout_message(Deadline(1.0)) == "Execution exceeded maximum timeout of 1 second."
    assert worker._timeout_message(Deadline(1.9)) == "Execution exceeded maximum timeout of 1.9 seconds."
    assert worker._timeout_message(Deadline(2.0)) == "Execution exceeded maximum timeout of 2 seconds."


def test_error_dict_id_and_message() -> None:
    assert error_dict("PAYLOAD_TOO_LARGE", "too big") == {
        "status": "error",
        "code": "PAYLOAD_TOO_LARGE",
        "error": "too big",
    }
    assert error_dict("ERR", "msg", req_id=None) == {
        "status": "error",
        "code": "ERR",
        "error": "msg",
        "id": None,
    }
    assert error_dict("ERR", "msg", req_id=0)["id"] == 0
    assert error_dict("ERR", "msg", req_id="")["id"] == ""
    res = error_dict("ERR", "msg", req_id=7, message=None)
    assert res == {"status": "error", "code": "ERR", "error": "msg", "id": 7}
    assert "message" not in res
    res2 = error_dict("ERR", "msg", message="slow")
    assert res2 == {"status": "error", "code": "ERR", "error": "msg", "message": "slow"}
    assert "id" not in res2
    assert error_dict("ERR", "msg", message="")["message"] == ""


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

    worker = BaseProcessWorker(1, "unused.py", start=False)
    proc2 = _Proc(pid=21)
    worker.process = proc2
    child2 = worker.child
    assert child2 is not None

    child2.begin_io()
    worker_kill_res: list[bool] = []
    t2 = threading.Thread(target=lambda: worker_kill_res.append(worker.kill()))
    t2.start()
    t2.join(timeout=2.0)
    assert not t2.is_alive()
    assert worker_kill_res == [False]
    assert worker.process is proc2

    worker._end_child_io(child2)
    assert proc2.events == ["kill", "stdin", "stdout", "wait"]
    assert worker.process is None


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
    assert proc.stdout.closed is False
    assert proc.stderr.closed is False

    assert child.end_io() is False
    assert proc.events == []
    assert proc.stdin.closed is False
    assert proc.stdout.closed is False
    assert proc.stderr.closed is False


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
    assert reaper_enqueued == [("abandon", child.managed)]

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

    reaped_tail = StderrTail()
    reaped_tail._append("reaped-output\n")
    join_thread = _CountingJoinThread()
    reaped_tail.attach_thread(join_thread)
    reaped_proc = _Proc(pid=61)
    reaped_child = _child(reaped_proc, tail=reaped_tail)
    assert reaped_child.signal_from() is True
    assert join_thread.joins == 1
    assert reaped_child.diagnostic_tail() == "reaped-output"
    assert join_thread.joins == 2


def test_respawn_adopt_refusal_signals_the_unadopted_child(
    monkeypatch: pytest.MonkeyPatch, reaper_enqueued: list[Any]
) -> None:
    worker = BaseProcessWorker(7, "unused.py", worker_name="Formula", start=False)
    read_calls: list[Any] = []
    monkeypatch.setattr(
        process_worker,
        "read_pickle_frame_with_timeout",
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


def test_adopt_refusal_does_not_signal_the_published_child(
    caplog: pytest.LogCaptureFixture, reaper_enqueued: list[Any]
) -> None:
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

    caplog.set_level(logging.WARNING, logger="compute_service.worker")
    stuck_loser_proc = _Proc(pid=3, stuck=True)
    stuck_loser_child = _child(stuck_loser_proc)

    assert worker._adopt(stuck_loser_child) is False
    assert worker.process is published_proc
    assert published_proc.kills == 0

    assert stuck_loser_child.signal_from() is True
    assert stuck_loser_proc.kills == 1
    assert stuck_loser_proc.stdin.closed is True
    assert stuck_loser_proc.stdout.closed is True
    assert stuck_loser_proc.stderr.closed is False
    assert published_proc.kills == 0
    assert worker.child is published_child

    assert ("abandon", stuck_loser_child.managed) in reaper_enqueued
    records = [
        r
        for r in caplog.records
        if r.name == "compute_service.worker" and "still alive after SIGKILL" in r.message
    ]
    assert len(records) == 1
    assert "abandoning" in records[0].message
    assert "pid=3" in records[0].message
