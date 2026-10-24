# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host entry: run PPT-Master turns in the user venv worker."""

from __future__ import annotations

import logging
from typing import Any, Callable

from plugin.framework.config import get_config_int
from plugin.ppt_master.paths import apply_data_root_env
from plugin.scripting.config_limits import configured_python_exec_timeout, resolve_python_exec_timeout
from plugin.scripting.venv_worker import _get_worker_manager, _worker_error

log = logging.getLogger(__name__)

# Wall-clock budget for a full ppt-master turn (LLM + scripts); longer than default python exec.
_PPT_MASTER_TURN_TIMEOUT_SEC = 1800


def run_ppt_master_venv_turn(
    uno_ctx: Any,
    *,
    query: str,
    history_text: str | None,
    topic: str | None,
    model: str | None,
    session_id: str,
    on_worker_event: Callable[[dict[str, Any]], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
    cancellation_scope: Any | None = None,
) -> dict[str, Any]:
    """Dispatch one sidebar turn to the venv ppt-master runner."""
    apply_data_root_env(uno_ctx)

    manager, err = _get_worker_manager()
    if manager is None:
        return err or _worker_error("WORKER_IPC_ERROR", "Python worker is unavailable.")

    configured = configured_python_exec_timeout()
    timeout_sec = max(resolve_python_exec_timeout(None, configured=configured), _PPT_MASTER_TURN_TIMEOUT_SEC)

    payload = {
        "query": query,
        "history_text": history_text,
        "topic": topic,
        "model": model,
        "session_id": session_id,
        "max_steps": get_config_int("chatbot.max_tool_rounds"),
        "max_tokens": get_config_int("chat_max_tokens"),
    }

    # cancellation_scope is a host object (it registers the live LlmClient).
    # It stays out of payload, which is pickled to the child.
    return manager.execute_ppt_master_turn(
        payload,
        timeout_sec=timeout_sec,
        on_worker_event=on_worker_event,
        stop_checker=stop_checker,
        cancellation_scope=cancellation_scope,
    )
