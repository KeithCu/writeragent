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
    long_trusted_worker_timeout_sec,
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
    timeout_sec = _resolve_trusted_timeout(ctx, _SYMBOLIC_SESSION_PREFIX)
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


# --- Long-running trusted helpers (use the single long budget instead of user python_exec_timeout) ---
# Vision is handled in its own resolver (also sources from the long budget for heavy paths).

_LONG_TRUSTED_PREFIXES = frozenset({
    "writeragent:text",
    "writeragent:symbolic",
})


def _resolve_trusted_timeout(ctx: Any, session_id: str) -> int:
    """Return the long budget for known slow calls, otherwise the user's standard timeout."""
    if session_id in _LONG_TRUSTED_PREFIXES:
        return long_trusted_worker_timeout_sec(ctx)
    return configured_python_exec_timeout(ctx)


# --- Vision ---

_VISION_SESSION_PREFIX = "writeragent:vision"
_VISION_STUB = """\
from plugin.vision.venv.vision import run_vision as _run
result = _run(data["spec"], data.get("image"), data.get("context") or {})
"""


def _resolve_vision_timeout_sec(ctx: Any, spec: dict[str, Any] | str) -> int:
    """Vision uses the long budget, with some engine-specific tuning + user override."""
    long_budget = long_trusted_worker_timeout_sec(ctx)
    if isinstance(spec, str):
        return long_budget
    if not isinstance(spec, dict):
        return long_budget
    raw_params = spec.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    if resolve_engine(params) == "paddle":
        return VISION_WORKER_TIMEOUT_SEC  # slightly lighter than full Docling
    if ctx is not None:
        try:
            from plugin.framework.config import get_config_int

            custom = get_config_int("vision.worker_timeout_sec")
            if custom > 0:
                return int(custom)
        except Exception:
            pass
    return long_budget  # Docling default path uses the long trusted budget


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


# --- DuckDB SQL (folder) ---

_SQL_SESSION_PREFIX = "writeragent:sql"
_SQL_STUB = """\
from plugin.scripting.duckdb_sql import query_folder_sql as _run
result = _run(data.get("scoped_dir"), data.get("sql"), data.get("files"), data.get("preloaded"), data.get("flat_files"))
"""


def run_folder_sql(
    ctx: Any,
    scoped_dir: str | None,
    sql: str,
    files: list[str] | dict[str, str] | None = None,
    preloaded: dict[str, Any] | None = None,
    flat_files: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute trusted SQL helper in the user venv (read-only, scoped to folder).

    Supports:
    - preloaded: grids from ranges or office files (key = table name)
    - files: list (legacy) or dict name->spec for folder files
    - flat_files: dict name -> full path for direct DuckDB flat files (CSV/Parquet)
    """
    timeout_sec = configured_python_exec_timeout(ctx)
    payload = {
        "scoped_dir": scoped_dir,
        "sql": sql,
        "files": files or [] if isinstance(files, list) else (files or {}),
        "preloaded": preloaded or {},
        "flat_files": flat_files or {},
    }
    # Reuse the common trusted helper runner (expects {"status":"ok", "result": ...} from worker)
    response = run_code_in_user_venv(
        ctx,
        _SQL_STUB,
        data=payload,
        timeout_sec=timeout_sec,
        session_id=_SQL_SESSION_PREFIX,
    )
    if response.get("status") != "ok":
        message = str(response.get("message") or "DuckDB SQL worker failed.")
        raise ToolExecutionError(message, code="DUCKDB_SQL_ERROR", details={"worker": response})
    # For direct trusted SQL the worker result is already the dict from query_folder_sql
    # (status inside). Return as-is so callers see the helper shape.
    result = response.get("result")
    if isinstance(result, dict):
        return result
    # Fallback shape
    return {"status": "ok", "result": result}


# --- Text Analytics (spaCy + textdescriptives) ---

_TEXT_SESSION_PREFIX = "writeragent:text"
_TEXT_STUB = """\
from plugin.scripting.text_analytics import run_text_analytics as _run
result = _run(data.get("spec"), data.get("text"), data.get("context") or {})
"""


def run_text_analytics(
    ctx: Any,
    spec: dict[str, Any] | str,
    text: str | list[str] | None = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute high-quality multilingual text analytics in the user venv.

    The heavy lifting (model load + processing) happens in the warm worker.
    For sentiment: uses transformers + a multilingual model (default: XLM-RoBERTa based).
    Requires `spacy` + `textdescriptives` for other helpers; `transformers` + `torch` (CPU) for sentiment.
    """
    # Read JSON config overrides so users can change the model via writeragent.json
    # (e.g. for a different multilingual model or future engine). For now only "transformers" is supported.
    try:
        from plugin.framework.config import get_config_dict
        cfg = get_config_dict() or {}
        model = cfg.get("text_analytics_sentiment_model")
        if model:
            if isinstance(spec, dict):
                p = spec.setdefault("params", {})
                p["model"] = model
            else:
                # spec is str like "sentiment" — wrap temporarily for consistency
                # (callers that pass str will still work; model is best passed in dict form).
                pass
    except Exception:
        pass  # config optional; fall back to hard-coded default in _extract_sentiment

    timeout_sec = _resolve_trusted_timeout(ctx, _TEXT_SESSION_PREFIX)
    payload = {"spec": spec, "text": text, "context": context or {}}
    return _run_trusted_helper(
        ctx,
        session_id=_TEXT_SESSION_PREFIX,
        stub=_TEXT_STUB,
        payload=payload,
        timeout_sec=timeout_sec,
        error_code="TEXT_ANALYTICS_ERROR",
        error_label="Text Analytics",
    )
