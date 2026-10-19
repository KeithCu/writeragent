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
from plugin.framework.process_worker import BaseProcessWorker, Child
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



def test_respawn_adopt_refusal_signals_the_unadopted_child(
    monkeypatch: pytest.MonkeyPatch, reaper_enqueued: list[Any]
) -> None:
    worker = BaseProcessWorker(7, "unused.py", worker_name="Formula", start=False)
    read_calls: list[Any] = []
    monkeypatch.setattr(
        process_worker,
        "read_pickle_frame",
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
