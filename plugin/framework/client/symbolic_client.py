# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side symbolic math RPC — routes trusted SymPy helpers to the warm venv worker."""

from __future__ import annotations

from typing import Any

from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import configured_python_exec_timeout
from plugin.scripting.venv_worker import run_code_in_user_venv

_SYMBOLIC_SESSION_PREFIX = "writeragent:symbolic"
_SYMBOLIC_STUB = """\
from plugin.scripting.symbolic import run_symbolic as _run
result = _run(data["spec"], data.get("data"), data.get("context") or {})
"""


def run_symbolic(
    ctx: Any,
    spec: dict[str, Any] | str,
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted symbolic helper in the user venv via fixed stub."""
    timeout_sec = configured_python_exec_timeout(ctx)
    payload: dict[str, Any] = {"spec": spec, "data": data, "context": context or {}}
    response = run_code_in_user_venv(
        ctx,
        _SYMBOLIC_STUB,
        data=payload,
        timeout_sec=timeout_sec,
        session_id=_SYMBOLIC_SESSION_PREFIX,
    )
    if response.get("status") != "ok":
        message = str(response.get("message") or "Symbolic worker failed.")
        raise ToolExecutionError(message, code="SYMBOLIC_ERROR", details={"worker": response})
    result = response.get("result")
    if not isinstance(result, dict):
        raise ToolExecutionError(
            "Symbolic worker returned an unexpected result.",
            code="SYMBOLIC_ERROR",
            details={"result_type": type(result).__name__},
        )
    return result
