# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side vision RPC — routes trusted helpers to the warm venv worker."""
from __future__ import annotations

from typing import Any

from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import configured_python_exec_timeout
from plugin.scripting.venv_worker import run_code_in_user_venv

_VISION_SESSION_PREFIX = "writeragent:vision"
_VISION_STUB = """\
from plugin.scripting.vision import run_vision as _run
result = _run(data["spec"], data.get("image"), data.get("context") or {})
"""


def _vision_session_id() -> str:
    return _VISION_SESSION_PREFIX


def run_vision(
    ctx: Any,
    spec: dict[str, Any] | str,
    image: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted vision helper in the user venv via fixed stub."""
    timeout_sec = configured_python_exec_timeout(ctx)
    payload: dict[str, Any] = {"spec": spec, "image": image, "context": context or {}}
    response = run_code_in_user_venv(
        ctx,
        _VISION_STUB,
        data=payload,
        timeout_sec=timeout_sec,
        session_id=_vision_session_id(),
    )
    if response.get("status") != "ok":
        message = str(response.get("message") or "Vision worker failed.")
        raise ToolExecutionError(message, code="VISION_ERROR", details={"worker": response})
    result = response.get("result")
    if not isinstance(result, dict):
        raise ToolExecutionError(
            "Vision worker returned an unexpected result.",
            code="VISION_ERROR",
            details={"result_type": type(result).__name__},
        )
    return result
