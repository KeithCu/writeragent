# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side vision RPC — routes trusted helpers to the warm venv worker."""
from __future__ import annotations

from typing import Any

from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import DOCLING_WORKER_TIMEOUT_SEC, VISION_WORKER_TIMEOUT_SEC
from plugin.scripting.vision_common import resolve_engine
from plugin.scripting.venv_worker import run_code_in_user_venv

_VISION_SESSION_PREFIX = "writeragent:vision"
_VISION_STUB = """\
from plugin.scripting.vision import run_vision as _run
result = _run(data["spec"], data.get("image"), data.get("context") or {})
"""


def _vision_session_id() -> str:
    return _VISION_SESSION_PREFIX


def _resolve_vision_timeout_sec(ctx: Any, spec: dict[str, Any] | str) -> int:
    if isinstance(spec, str):
        return DOCLING_WORKER_TIMEOUT_SEC
    if not isinstance(spec, dict):
        return DOCLING_WORKER_TIMEOUT_SEC
    params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
    if resolve_engine(params) == "paddle":
        return VISION_WORKER_TIMEOUT_SEC
    if ctx is not None:
        try:
            from plugin.framework.config import get_config_int

            custom = get_config_int(ctx, "vision.worker_timeout_sec")
            if custom > 0:
                return int(custom)
        except Exception:
            pass
    return DOCLING_WORKER_TIMEOUT_SEC


def run_vision(
    ctx: Any,
    spec: dict[str, Any] | str,
    image: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted vision helper in the user venv via fixed stub."""
    timeout_sec = _resolve_vision_timeout_sec(ctx, spec)
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
