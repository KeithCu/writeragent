# WriterAgent - Python Compute Service Executor
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-process AST sandbox executor for the standalone Python Compute Service."""

from __future__ import annotations

import hashlib
from typing import Any

from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

from compute_service.config import DEFAULT_SETTINGS, clamp_timeout_sec
from compute_service.json_egress import normalize_execute_response
from compute_service.json_forward import validate_session_id


def execute_code(
    code: str,
    data: Any = None,
    session_id: str | None = None,
    timeout_sec: int | None = None,
    *,
    mode: str = "isolated",
    init_script: str | None = None,
    default_timeout_sec: int = DEFAULT_SETTINGS.default_timeout_sec,
    max_timeout_sec: int | None = None,
) -> dict[str, Any]:
    """Execute *code* under AST sandboxing; return §8-shaped dumb-JSON payload.

    The host already clamps request timeouts to configured bounds (e.g. 1800s);
    the worker does not impose a second 600s clamp when max_timeout_sec is None.
    """
    # Always pass an explicit timeout so the sandbox never consults WriterAgent defaults.
    timeout_sec = clamp_timeout_sec(
        timeout_sec,
        default_timeout_sec=default_timeout_sec,
        max_timeout_sec=max_timeout_sec,
    )

    # Shared kernel only when explicitly requested *and* a session id is provided.
    use_session: str | None = None
    if mode == "shared" and isinstance(session_id, str) and session_id.strip():
        use_session = validate_session_id(session_id)

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

    raw = run_sandboxed_code(
        code=code,
        data=data,
        session_id=use_session,
        timeout_sec=timeout_sec,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=init_hash,
    )

    return normalize_execute_response(raw)
