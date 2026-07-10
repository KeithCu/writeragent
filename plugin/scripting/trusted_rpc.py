# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared host RPC for trusted venv worker actions (zero user-code AST path)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_GRACE_S, WORKER_POOL_DEFAULT
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.venv_worker import run_code_in_user_venv

log = logging.getLogger(__name__)


def parse_worker_dict_result(
    response: dict[str, Any],
    *,
    error_code: str,
    error_label: str,
) -> dict[str, Any]:
    """Normalize a warm-worker response into a result dict or raise ToolExecutionError."""
    if response.get("status") != "ok":
        message = str(response.get("message") or f"{error_label} worker failed.")
        raise ToolExecutionError(message, code=error_code, details={"worker": response})
    result = response.get("result")
    if not isinstance(result, dict):
        raise ToolExecutionError(
            f"{error_label} worker returned an unexpected result.",
            code=error_code,
            details={"result_type": type(result).__name__},
        )
    return result


def run_trusted_worker_action(
    ctx: Any,
    *,
    domain: str,
    helper: str = "",
    params: dict[str, Any] | None = None,
    data_range: Any = None,
    context: dict[str, Any] | None = None,
    session_id: str,
    timeout_sec: int,
    worker_pool: str = WORKER_POOL_DEFAULT,
    additional_data: dict[str, Any] | None = None,
    allow_heartbeat: bool = False,
    heartbeat_grace_sec: int | None = None,
    heartbeat_fn: Callable[[dict[str, Any]], None] | None = None,
    error_code: str = "TRUSTED_ACTION_ERROR",
    error_label: str = "Trusted action",
) -> dict[str, Any]:
    """Execute a trusted action in the warm venv worker without user code strings."""
    payload: dict[str, Any] = {
        "domain": domain,
        "helper": helper,
        "params": dict(params or {}),
        "data_range": data_range,
        "context": context or {},
    }
    if additional_data:
        payload.update(additional_data)

    def _on_heartbeat(hb: dict[str, Any]) -> None:
        if heartbeat_fn:
            heartbeat_fn(hb)
        log.debug("trusted action heartbeat domain=%s helper=%s: %s", domain, helper, hb)

    response = run_code_in_user_venv(
        ctx,
        code=None,
        data=payload,
        timeout_sec=timeout_sec,
        session_id=session_id,
        worker_pool=worker_pool,
        action="run_trusted_action",
        allow_heartbeat=allow_heartbeat,
        heartbeat_grace_sec=heartbeat_grace_sec if heartbeat_grace_sec is not None else EMBEDDINGS_HEARTBEAT_GRACE_S,
        on_heartbeat=_on_heartbeat if allow_heartbeat else None,
    )
    return parse_worker_dict_result(response, error_code=error_code, error_label=error_label)
