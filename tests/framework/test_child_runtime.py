# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import ctypes

import pytest

import plugin.framework.child_runtime as child_runtime


def test_die_with_parent_linux_prctl_and_ppid_race(monkeypatch) -> None:
    calls: list[tuple] = []

    class _Libc:
        def prctl(self, *args: object) -> int:
            calls.append(args)
            return 0

    monkeypatch.setattr(child_runtime.sys, "platform", "linux")
    monkeypatch.setattr(child_runtime, "_prctl", None)
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: _Libc())
    monkeypatch.setattr(child_runtime.os, "getppid", lambda: 50)
    child_runtime.die_with_parent()
    assert len(calls) == 1

    killed: list[int] = []
    ppids = [50, 1]
    monkeypatch.setattr(child_runtime.os, "getppid", lambda: ppids.pop(0))
    monkeypatch.setattr(child_runtime.os, "getpid", lambda: 123)
    monkeypatch.setattr(child_runtime.os, "kill", lambda pid, sig: killed.append(sig))
    # Reset cached _prctl so ppid capture is also exercised when CDLL runs during die_with_parent.
    monkeypatch.setattr(child_runtime, "_prctl", None)
    child_runtime.die_with_parent()
    assert killed == [9]

    killed.clear()
    subreaper_ppids = [50, 999]
    monkeypatch.setattr(child_runtime.os, "getppid", lambda: subreaper_ppids.pop(0))
    child_runtime.die_with_parent()
    assert killed == [9]

    monkeypatch.setattr(child_runtime.sys, "platform", "win32")
    calls.clear()
    child_runtime.die_with_parent()
    assert calls == []


def test_announce_ready_roundtrip() -> None:
    import io
    import os

    from plugin.framework import ipc
    from plugin.framework.child_runtime import announce_ready, is_ready_payload

    buf = io.BytesIO()
    announce_ready(buf)
    buf.seek(0)
    data = ipc.read_pickle_frame(buf)
    assert data == {"status": "ready", "pid": os.getpid()}
    assert is_ready_payload(data) is True
    assert is_ready_payload(None) is False
    assert is_ready_payload([]) is False
    assert is_ready_payload({"status": "ok"}) is False


def test_bind_pdeathsig_and_pdeathsig_preexec(monkeypatch) -> None:
    calls: list[tuple] = []

    class _Libc:
        def prctl(self, *args: object) -> int:
            calls.append(args)
            return 0

    monkeypatch.setattr(child_runtime.sys, "platform", "linux")
    monkeypatch.setattr(child_runtime, "_prctl", None)
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: _Libc())
    monkeypatch.setattr(child_runtime.os, "getpid", lambda: 100)
    monkeypatch.setattr(child_runtime.os, "getppid", lambda: 100)
    monkeypatch.setattr(child_runtime.os, "kill", lambda pid, sig: None)
    assert child_runtime.bind_pdeathsig() is True
    assert calls == []

    # Second call reuses cached prctl without calling CDLL again
    def _fail_cdll(*args: object, **kwargs: object) -> None:
        raise AssertionError("CDLL called twice")

    monkeypatch.setattr(ctypes, "CDLL", _fail_cdll)
    assert child_runtime.bind_pdeathsig() is True

    # Preexec arms prctl
    child_runtime.pdeathsig_preexec()
    assert len(calls) == 1

    # When prctl was not bound, preexec does nothing (musl hosts, non-Linux).
    monkeypatch.setattr(child_runtime, "_prctl", None)
    calls.clear()
    child_runtime.pdeathsig_preexec()
    assert calls == []


def test_bind_pdeathsig_oserror_falls_back(monkeypatch) -> None:
    def _raise_cdll(*_args: object, **_kwargs: object) -> None:
        raise OSError("cannot open shared object file: No such file or directory")

    monkeypatch.setattr(child_runtime.sys, "platform", "linux")
    monkeypatch.setattr(child_runtime, "_prctl", None)
    monkeypatch.setattr(ctypes, "CDLL", _raise_cdll)
    assert child_runtime.bind_pdeathsig() is False
    assert child_runtime._prctl is None
    # Preexec safely does nothing when prctl could not be bound
    child_runtime.pdeathsig_preexec()
    assert child_runtime.die_with_parent() is False


def test_pdeathsig_preexec_parent_died_before_exec(monkeypatch) -> None:
    calls: list[tuple] = []
    killed: list[int] = []

    class _Libc:
        def prctl(self, *args: object) -> int:
            calls.append(args)
            return 0

    monkeypatch.setattr(child_runtime.sys, "platform", "linux")
    monkeypatch.setattr(child_runtime, "_prctl", None)
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: _Libc())
    # Parent binds and records its pid as 100
    monkeypatch.setattr(child_runtime.os, "getpid", lambda: 100)
    child_runtime.bind_pdeathsig()

    # In child after fork, parent is dead so getppid() is init/subreaper (1)
    monkeypatch.setattr(child_runtime.os, "getppid", lambda: 1)
    monkeypatch.setattr(child_runtime.os, "kill", lambda pid, sig: killed.append(sig))
    child_runtime.pdeathsig_preexec()
    assert len(calls) == 1
    assert killed == [9]


def test_die_with_parent() -> None:
    import sys
    from plugin.framework.child_runtime import die_with_parent

    if sys.platform != "linux":
        assert die_with_parent() is False
    else:
        assert die_with_parent() is True


def test_error_dict_keeps_optional_id() -> None:
    """``id`` is present when passed, including None. No ``message`` key."""
    from plugin.framework.child_runtime import error_dict

    bare = error_dict("PAYLOAD_TOO_LARGE", "too big")
    assert bare == {"status": "error", "code": "PAYLOAD_TOO_LARGE", "error": "too big"}
    with_id = error_dict("QUEUE_TIMEOUT", "expired", req_id=None)
    assert with_id["id"] is None
    assert "message" not in with_id


def test_usable_budget_truth_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1s budget may start with under a second left. A longer one may not.

    The chained compare hid that. A 2s request with 0.5s left must refuse;
    a 1s request with the same remainder must start.
    """
    from plugin.framework import child_runtime as worker_mod

    now = 1000.0
    monkeypatch.setattr("plugin.framework.child_runtime.time.monotonic", lambda: now)
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
    from plugin.framework.child_runtime import Deadline

    now = 1_000.0
    monkeypatch.setattr("plugin.framework.child_runtime.time.monotonic", lambda: now)
    clock = Deadline.__new__(Deadline)
    clock.budget_sec = budget
    clock._end = now + remaining
    assert clock.usable() == usable_sec
    assert clock.left() == left_sec
