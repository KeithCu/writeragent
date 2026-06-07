# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side quant RPC — routes trusted quant helpers to the warm venv worker."""

from __future__ import annotations

from typing import Any

from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import configured_python_exec_timeout
from plugin.scripting.venv_worker import run_code_in_user_venv

_QUANT_SESSION_PREFIX = "writeragent:quant"
_QUANT_STUB = """\
from plugin.scripting.quant import run_quant as _run
result = _run(data["helper"], data["params"], data.get("data"), data.get("context") or {})
"""


def run_quant(
    ctx: Any,
    helper: str,
    params: dict[str, Any],
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted quant helper in the user venv via fixed stub."""
    timeout_sec = configured_python_exec_timeout(ctx)
    payload: dict[str, Any] = {"helper": helper, "params": params, "data": data, "context": context or {}}
    response = run_code_in_user_venv(
        ctx,
        _QUANT_STUB,
        data=payload,
        timeout_sec=timeout_sec,
        session_id=_QUANT_SESSION_PREFIX,
    )
    if response.get("status") != "ok":
        message = str(response.get("message") or "Quant worker failed.")
        raise ToolExecutionError(message, code="QUANT_ERROR", details={"worker": response})
    result = response.get("result")
    if not isinstance(result, dict):
        raise ToolExecutionError(
            "Quant worker returned an unexpected result.",
            code="QUANT_ERROR",
            details={"result_type": type(result).__name__},
        )
    return result
