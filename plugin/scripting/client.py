# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unified scripting client — routes trusted scripting helpers to the warm venv worker."""

from __future__ import annotations

from typing import Any

from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import (
    configured_python_exec_timeout,
    DOCLING_WORKER_TIMEOUT_SEC,
    VISION_WORKER_TIMEOUT_SEC,
)
from plugin.vision.vision_common import resolve_engine
from plugin.scripting.venv_worker import run_code_in_user_venv


def _run_trusted_helper(
    ctx: Any,
    session_id: str,
    stub: str,
    payload: dict[str, Any],
    timeout_sec: int,
    error_code: str,
    error_label: str,
) -> dict[str, Any]:
    """Execute a trusted helper in the user venv worker via run_code_in_user_venv."""
    response = run_code_in_user_venv(
        ctx,
        stub,
        data=payload,
        timeout_sec=timeout_sec,
        session_id=session_id,
    )
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


# --- Analysis ---

_ANALYSIS_SESSION_PREFIX = "writeragent:analysis"
_ANALYSIS_STUB = """\
from plugin.scripting.analysis import run_analysis as _run
result = _run(data["spec"], data.get("data"), data.get("context") or {})
"""


def run_analysis(
    ctx: Any,
    spec: dict[str, Any] | str,
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted analysis helper in the user venv."""
    timeout_sec = configured_python_exec_timeout(ctx)
    payload = {"spec": spec, "data": data, "context": context or {}}
    return _run_trusted_helper(
        ctx,
        session_id=_ANALYSIS_SESSION_PREFIX,
        stub=_ANALYSIS_STUB,
        payload=payload,
        timeout_sec=timeout_sec,
        error_code="ANALYSIS_ERROR",
        error_label="Analysis",
    )


# --- Quant ---

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
    """Execute a trusted quant helper in the user venv."""
    timeout_sec = configured_python_exec_timeout(ctx)
    payload = {"helper": helper, "params": params, "data": data, "context": context or {}}
    return _run_trusted_helper(
        ctx,
        session_id=_QUANT_SESSION_PREFIX,
        stub=_QUANT_STUB,
        payload=payload,
        timeout_sec=timeout_sec,
        error_code="QUANT_ERROR",
        error_label="Quant",
    )


# --- Viz ---

_VIZ_SESSION_PREFIX = "writeragent:viz"
_VIZ_STUB = """\
from plugin.scripting.viz import run_viz as _run
result = _run(data["spec"], data.get("data"), data.get("context") or {})
"""


def run_viz(
    ctx: Any,
    spec: dict[str, Any] | str,
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted viz helper in the user venv."""
    timeout_sec = configured_python_exec_timeout(ctx)
    payload = {"spec": spec, "data": data, "context": context or {}}
    return _run_trusted_helper(
        ctx,
        session_id=_VIZ_SESSION_PREFIX,
        stub=_VIZ_STUB,
        payload=payload,
        timeout_sec=timeout_sec,
        error_code="VIZ_ERROR",
        error_label="Viz",
    )


# --- Symbolic ---

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
    """Execute a trusted symbolic helper in the user venv."""
    timeout_sec = configured_python_exec_timeout(ctx)
    payload = {"spec": spec, "data": data, "context": context or {}}
    return _run_trusted_helper(
        ctx,
        session_id=_SYMBOLIC_SESSION_PREFIX,
        stub=_SYMBOLIC_STUB,
        payload=payload,
        timeout_sec=timeout_sec,
        error_code="SYMBOLIC_ERROR",
        error_label="Symbolic",
    )


# --- Units ---

_UNITS_SESSION_PREFIX = "writeragent:units"
_UNITS_STUB = """\
from plugin.scripting.units import run_units as _run
result = _run(data["spec"], data.get("data"), data.get("context") or {})
"""


def run_units(
    ctx: Any,
    spec: dict[str, Any] | str,
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted units helper in the user venv."""
    timeout_sec = configured_python_exec_timeout(ctx)
    payload = {"spec": spec, "data": data, "context": context or {}}
    return _run_trusted_helper(
        ctx,
        session_id=_UNITS_SESSION_PREFIX,
        stub=_UNITS_STUB,
        payload=payload,
        timeout_sec=timeout_sec,
        error_code="UNITS_ERROR",
        error_label="Units",
    )


# --- Optimize ---

_OPTIMIZE_SESSION_PREFIX = "writeragent:optimize"
_OPTIMIZE_STUB = """\
from plugin.scripting.optimize import run_optimize as _run
result = _run(data["spec"], data.get("data"), data.get("context") or {})
"""


def run_optimize(
    ctx: Any,
    spec: dict[str, Any] | str,
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted optimize helper in the user venv."""
    timeout_sec = configured_python_exec_timeout(ctx)
    payload = {"spec": spec, "data": data, "context": context or {}}
    return _run_trusted_helper(
        ctx,
        session_id=_OPTIMIZE_SESSION_PREFIX,
        stub=_OPTIMIZE_STUB,
        payload=payload,
        timeout_sec=timeout_sec,
        error_code="OPTIMIZE_ERROR",
        error_label="Optimization",
    )


# --- Vision ---

_VISION_SESSION_PREFIX = "writeragent:vision"
_VISION_STUB = """\
from plugin.vision.venv.vision import run_vision as _run
result = _run(data["spec"], data.get("image"), data.get("context") or {})
"""


def _resolve_vision_timeout_sec(ctx: Any, spec: dict[str, Any] | str) -> int:
    if isinstance(spec, str):
        return DOCLING_WORKER_TIMEOUT_SEC
    if not isinstance(spec, dict):
        return DOCLING_WORKER_TIMEOUT_SEC
    raw_params = spec.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
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
    """Execute a trusted vision helper in the user venv."""
    timeout_sec = _resolve_vision_timeout_sec(ctx, spec)
    payload = {"spec": spec, "image": image, "context": context or {}}
    return _run_trusted_helper(
        ctx,
        session_id=_VISION_SESSION_PREFIX,
        stub=_VISION_STUB,
        payload=payload,
        timeout_sec=timeout_sec,
        error_code="VISION_ERROR",
        error_label="Vision",
    )
