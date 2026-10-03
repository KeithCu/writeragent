# WriterAgent - Python Compute Service Executor
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-process AST sandbox executor for the standalone Python Compute Service."""

from __future__ import annotations

import hashlib
import math
import os
import sys
import threading
from typing import Any

# Ensure repo root is on sys.path to resolve plugin.* imports
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

from compute_service.config import DEFAULT_SETTINGS
from compute_service.json_egress import normalize_execute_response

# Per-session locks so concurrent shared-kernel requests do not race LocalPythonExecutor.
# If a worker process is killed mid-reset, release_session_lock may not be reached,
# leaving at most one stale Lock per dead session; cleared on next worker respawn.
_SESSION_RUN_LOCKS: dict[str, threading.Lock] = {}
_SESSION_RUN_LOCKS_GUARD = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    with _SESSION_RUN_LOCKS_GUARD:
        lock = _SESSION_RUN_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _SESSION_RUN_LOCKS[session_id] = lock
        return lock


def release_session_lock(session_id: str) -> None:
    """Drop the run lock for *session_id* after the sandbox session is reset.

    Workers that still hold any sticky session skip process recycle, so this
    map would otherwise keep one Lock per historical session until that
    process exits. Reset runs on the worker thread while the pool holds the
    lease, so the lock is not in use.
    """
    with _SESSION_RUN_LOCKS_GUARD:
        _SESSION_RUN_LOCKS.pop(session_id, None)


def clamp_timeout_sec(timeout_sec: float | int | None, *, default_timeout_sec: int = DEFAULT_SETTINGS.default_timeout_sec, max_timeout_sec: int = DEFAULT_SETTINGS.max_timeout_sec) -> int:
    if timeout_sec is None:
        return default_timeout_sec
    # In Python, int(float('inf')) raises OverflowError, which does not inherit from ValueError.
    # Non-finite floats (inf/nan) must fall back to default_timeout_sec.
    if isinstance(timeout_sec, float) and not math.isfinite(timeout_sec):
        return default_timeout_sec
    try:
        sec = int(timeout_sec)
    except (TypeError, ValueError, OverflowError):
        return default_timeout_sec
    return max(1, min(max_timeout_sec, sec))


def timeout_ms_to_sec(timeout_ms: Any, *, default_timeout_sec: int = DEFAULT_SETTINGS.default_timeout_sec, max_timeout_sec: int = DEFAULT_SETTINGS.max_timeout_sec) -> int:
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
        return default_timeout_sec
    # +Infinity is a float that passes ``> 0`` and then OverflowError in int().
    # Multipart meta rejects it; this keeps a direct caller from escaping the handler.
    if isinstance(timeout_ms, float) and not math.isfinite(timeout_ms):
        return default_timeout_sec
    if timeout_ms <= 0:
        return default_timeout_sec
    # Round up so 1500ms → 2s, not 1s
    return clamp_timeout_sec((int(timeout_ms) + 999) // 1000, default_timeout_sec=default_timeout_sec, max_timeout_sec=max_timeout_sec)


def execute_code(
    code: str, data: Any = None, session_id: str | None = None, timeout_sec: int | None = None, *, mode: str = "isolated", init_script: str | None = None, default_timeout_sec: int = DEFAULT_SETTINGS.default_timeout_sec, max_timeout_sec: int = DEFAULT_SETTINGS.max_timeout_sec
) -> dict[str, Any]:
    """Execute *code* under AST sandboxing; return §8-shaped dumb-JSON payload."""
    # Always pass an explicit timeout so the sandbox never consults WriterAgent defaults.
    timeout_sec = clamp_timeout_sec(timeout_sec, default_timeout_sec=default_timeout_sec, max_timeout_sec=max_timeout_sec)

    # Shared kernel only when explicitly requested *and* a session id is provided.
    use_session: str | None = None
    if mode == "shared" and isinstance(session_id, str) and session_id.strip():
        use_session = session_id.strip()

    # Stable init_session_id so run_sandboxed_code runs init once per worker and
    # seeds later cells from that namespace (hash change replaces the snapshot).
    init_sid: str | None = None
    init_code = init_script if isinstance(init_script, str) and init_script.strip() else None
    init_hash: str | None = None
    if init_code is not None:
        init_hash = hashlib.sha256(init_code.encode("utf-8")).hexdigest()
        if use_session is not None:
            init_sid = f"{use_session}:init"
        else:
            init_sid = f"isolated:{init_hash}:init"

    def _run() -> dict[str, Any]:
        return run_sandboxed_code(code=code, data=data, session_id=use_session, timeout_sec=timeout_sec, init_script=init_code, init_session_id=init_sid, init_script_hash=init_hash)

    if use_session is not None:
        with _session_lock(use_session):
            raw = _run()
    else:
        raw = _run()

    return normalize_execute_response(raw)
