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
