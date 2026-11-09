# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import ctypes

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
    assert calls == [(1, 9, 0, 0, 0)]

    killed: list[int] = []
    ppids = [50, 1]
    monkeypatch.setattr(child_runtime.os, "getppid", lambda: ppids.pop(0))
    monkeypatch.setattr(child_runtime.os, "getpid", lambda: 123)
    monkeypatch.setattr(child_runtime.os, "kill", lambda pid, sig: killed.append(sig))
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
    assert calls == [(1, 9, 0, 0, 0)]

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
    assert calls == [(1, 9, 0, 0, 0)]
    assert killed == [9]


def test_die_with_parent_ppid_captured_before_dlopen(monkeypatch) -> None:
    """Parent pid for the race check is taken before libc is loaded."""
    killed: list[int] = []
    ppids = [50, 1]

    class _Libc:
        def prctl(self, *args: object) -> int:
            return 0

    monkeypatch.setattr(child_runtime.sys, "platform", "linux")
    monkeypatch.setattr(child_runtime, "_prctl", None)
    monkeypatch.setattr(child_runtime.os, "getppid", lambda: ppids.pop(0))
    monkeypatch.setattr(child_runtime.os, "kill", lambda pid, sig: killed.append(sig))
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: _Libc())
    child_runtime.die_with_parent()
    assert killed == [9]

