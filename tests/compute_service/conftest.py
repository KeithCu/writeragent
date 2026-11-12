# WriterAgent - Tests for Python Compute Service
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fixtures and utilities for compute service tests."""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest

from compute_service.config import ComputeSettings
from compute_service.formula_pool import FormulaProcessPool
from compute_service.worker_base import BaseProcessWorker
from plugin.framework.child_runtime import Deadline
from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.warm_child import Child


def get_free_port() -> int:
    """Return an available loopback port, attempting IPv6 first then IPv4."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]
    except OSError:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]


def decode_result_json(envelope: dict[str, Any]) -> dict[str, Any]:
    """Parse result_json bytes into a dictionary when present, otherwise return envelope."""
    raw = envelope.get("result_json")
    if isinstance(raw, (bytes, bytearray)):
        parsed = json.loads(bytes(raw).decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
    return envelope


def run_cell(pool: FormulaProcessPool, code: str, *, data: Any = None, data_json: bytes | None = None, session_id: str | None = None, init_script: str | None = None, req_id: str | None = None, timeout_sec: float | None = None, deadline: Deadline | None = None) -> dict[str, Any]:
    """Run formula execution on pool and decode result_json."""
    if data is not None and data_json is None:
        data_json = json.dumps(data, allow_nan=False).encode("utf-8")
    clock = deadline if deadline is not None else Deadline(float(pool.default_timeout_sec if timeout_sec is None else timeout_sec))
    envelope = pool.execute(code, data_json=data_json, session_id=session_id, init_script=init_script, req_id=req_id, deadline=clock)
    return decode_result_json(envelope)


def session_worker(pool: FormulaProcessPool, session_id: str) -> BaseProcessWorker | None:
    """Return worker owning session_id if row exists and is alive."""
    with pool._cond:
        row = pool._sessions.get(session_id)
        if row is not None and pool._is_session_live(row):
            return row.worker
        return None


def worker_sessions(pool: FormulaProcessPool, worker: BaseProcessWorker) -> list[str]:
    """Return live session IDs associated with worker."""
    with pool._cond:
        return [sid for sid, s in pool._sessions.items() if s.worker is worker and pool._is_session_live(s)]


class _FakePopen:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.stdin = None
        self.stdout = None

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0


def fake_child(worker: Any, *, alive: bool = True) -> Child:
    """Wrap a fake subprocess into a Child for worker tests."""
    rc = None if alive else 0
    proc = _FakePopen(returncode=rc)
    wname = worker.worker_name
    wid = worker.worker_id
    return Child(ManagedSubprocess.wrap_existing(proc, label=f"{wname} #{wid}"), worker_name=wname, worker_id=wid)


def pool_without_children(monkeypatch: pytest.MonkeyPatch, **settings: Any) -> FormulaProcessPool:
    """Create a FormulaProcessPool with no initial child processes spawned."""
    monkeypatch.setattr("compute_service.worker_base.BaseProcessWorker.respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    cfg_kwargs: dict[str, Any] = {"workers": 1, "idle_worker_ttl_sec": 0}
    cfg_kwargs.update(settings)
    pool = FormulaProcessPool(settings=ComputeSettings(**cfg_kwargs))
    pool.workers.clear()
    return pool
