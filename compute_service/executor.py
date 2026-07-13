# WriterAgent - Python Compute Service Executor
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-process AST sandbox executor for the standalone Python Compute Service."""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

# Ensure repo root is on sys.path to resolve plugin.* imports
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from plugin.framework.uno_bootstrap import register_alias_importer

register_alias_importer()

from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

from compute_service.json_egress import normalize_execute_response

# Per-session locks so concurrent shared-kernel requests do not race LocalPythonExecutor.
_SESSION_RUN_LOCKS: dict[str, threading.Lock] = {}
_SESSION_RUN_LOCKS_GUARD = threading.Lock()

_MAX_TIMEOUT_SEC = 600
_DEFAULT_TIMEOUT_SEC = 30


def _session_lock(session_id: str) -> threading.Lock:
    with _SESSION_RUN_LOCKS_GUARD:
        lock = _SESSION_RUN_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _SESSION_RUN_LOCKS[session_id] = lock
        return lock


def clamp_timeout_sec(timeout_sec: float | int | None) -> int:
    if timeout_sec is None:
        return _DEFAULT_TIMEOUT_SEC
    try:
        sec = int(timeout_sec)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SEC
    return max(1, min(_MAX_TIMEOUT_SEC, sec))


def timeout_ms_to_sec(timeout_ms: Any) -> int:
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
        return _DEFAULT_TIMEOUT_SEC
    if timeout_ms <= 0:
        return _DEFAULT_TIMEOUT_SEC
    # Round up so 1500ms → 2s, not 1s
    return clamp_timeout_sec((int(timeout_ms) + 999) // 1000)


def execute_code(
    code: str,
    data: Any = None,
    session_id: str | None = None,
    timeout_sec: int | None = None,
    *,
    mode: str = "isolated",
    init_script: str | None = None,
) -> dict[str, Any]:
    """Execute *code* under AST sandboxing; return §8-shaped dumb-JSON payload."""
    timeout_sec = clamp_timeout_sec(timeout_sec)

    # Shared kernel only when explicitly requested *and* a session id is provided.
    use_session: str | None = None
    if mode == "shared" and isinstance(session_id, str) and session_id.strip():
        use_session = session_id.strip()

    def _run() -> dict[str, Any]:
        return run_sandboxed_code(
            code=code,
            data=data,
            session_id=use_session,
            timeout_sec=timeout_sec,
            init_script=init_script,
        )

    if use_session is not None:
        with _session_lock(use_session):
            raw = _run()
    else:
        raw = _run()

    return normalize_execute_response(raw)
