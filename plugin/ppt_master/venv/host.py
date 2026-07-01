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
from plugin.scripting.venv_worker import _worker_manager_for_ctx

log = logging.getLogger(__name__)

# Wall-clock budget for a full ppt-master turn (LLM + scripts); longer than default python exec.
_PPT_MASTER_TURN_TIMEOUT_SEC = 1800


def ppt_master_session_id(doc: Any) -> str:
    try:
        url = doc.getURL()
    except Exception:
        url = ""
    return f"ppt_master:{url or 'active'}"


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
) -> dict[str, Any]:
    """Dispatch one sidebar turn to the venv ppt-master runner."""
    apply_data_root_env(uno_ctx)

    manager, err = _worker_manager_for_ctx(uno_ctx)
    if err is not None:
        return err
    assert manager is not None

    configured = configured_python_exec_timeout(uno_ctx)
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

    return manager.execute_ppt_master_turn(
        payload,
        timeout_sec=timeout_sec,
        on_worker_event=on_worker_event,
        stop_checker=stop_checker,
    )
