# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unified scripting client — routes trusted scripting helpers to the warm venv worker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.scripting.config_limits import (
    configured_python_exec_timeout,
    long_trusted_worker_timeout_sec,
    VISION_WORKER_TIMEOUT_SEC,
)
from plugin.scripting.trusted_rpc import run_trusted_worker_action
from plugin.vision.vision_common import resolve_engine


def _run_trusted_action(
    ctx: Any,
    session_id: str,
    domain: str,
    helper: str,
    params: dict[str, Any],
    data_range: Any,
    context: dict[str, Any] | None,
    timeout_sec: int,
    error_code: str,
    error_label: str,
    additional_data: dict[str, Any] | None = None,
    *,
    allow_heartbeat: bool = False,
    heartbeat_fn: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute a trusted action packet in the user venv worker."""
    return run_trusted_worker_action(
        ctx,
        domain=domain,
        helper=helper,
        params=params,
        data_range=data_range,
        context=context,
        session_id=session_id,
        timeout_sec=timeout_sec,
        additional_data=additional_data,
        error_code=error_code,
        error_label=error_label,
        allow_heartbeat=allow_heartbeat,
        heartbeat_fn=heartbeat_fn,
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


def _make_spec_runner(
    *,
    session_prefix: str,
    domain: str,
    error_code: str,
    error_label: str,
    long_timeout: bool = False,
):
    """Build a ``run_*(ctx, spec, data, context=)`` client using run_trusted_action RPC."""

    def _runner(
        ctx: Any,
        spec: dict[str, Any] | str,
        data: Any = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout_sec = (
            _resolve_trusted_timeout(ctx, session_prefix)
            if long_timeout
            else configured_python_exec_timeout(ctx)
        )
        if isinstance(spec, str):
            helper = spec
            params: dict[str, Any] = {}
        else:
            helper = spec.get("helper", "")
            params = spec.get("params") or {}

        return _run_trusted_action(
            ctx,
            session_id=session_prefix,
            domain=domain,
            helper=helper,
            params=params,
            data_range=data,
            context=context,
            timeout_sec=timeout_sec,
            error_code=error_code,
            error_label=error_label,
        )

    _runner.__name__ = f"run_{error_label.lower().replace(' ', '_')}"
    _runner.__doc__ = f"Execute a trusted {error_label} helper in the user venv."
    return _runner


run_analysis = _make_spec_runner(
    session_prefix="writeragent:analysis",
    domain="analysis",
    error_code="ANALYSIS_ERROR",
    error_label="Analysis",
)

run_viz = _make_spec_runner(
    session_prefix="writeragent:viz",
    domain="viz",
    error_code="VIZ_ERROR",
    error_label="Viz",
)

run_symbolic = _make_spec_runner(
    session_prefix="writeragent:symbolic",
    domain="symbolic",
    error_code="SYMBOLIC_ERROR",
    error_label="Symbolic",
    long_timeout=True,
)

run_units = _make_spec_runner(
    session_prefix="writeragent:units",
    domain="units",
    error_code="UNITS_ERROR",
    error_label="Units",
)

run_optimize = _make_spec_runner(
    session_prefix="writeragent:optimize",
    domain="optimize",
    error_code="OPTIMIZE_ERROR",
    error_label="Optimization",
)

run_forecast = _make_spec_runner(
    session_prefix="writeragent:forecast",
    domain="forecast",
    error_code="FORECAST_ERROR",
    error_label="Forecast",
)


# --- Quant ---

_QUANT_SESSION_PREFIX = "writeragent:quant"


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
    return _run_trusted_action(
        ctx,
        session_id=_QUANT_SESSION_PREFIX,
        domain="quant",
        helper=helper,
        params=params,
        data_range=data,
        context=context,
        timeout_sec=timeout_sec,
        error_code="QUANT_ERROR",
        error_label="Quant",
    )


# --- Vision ---

_VISION_SESSION_PREFIX = "writeragent:vision"


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
    if isinstance(spec, str):
        helper = spec
        params: dict[str, Any] = {}
    else:
        helper = spec.get("helper", "")
        params = spec.get("params") or {}
    return _run_trusted_action(
        ctx,
        session_id=_VISION_SESSION_PREFIX,
        domain="vision",
        helper=helper,
        params=params,
        data_range=None,
        context=context,
        timeout_sec=timeout_sec,
        error_code="VISION_ERROR",
        error_label="Vision",
        additional_data={"image": image},
    )


# --- DuckDB SQL (folder) ---

_SQL_SESSION_PREFIX = "writeragent:sql"


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
    return _run_trusted_action(
        ctx,
        session_id=_SQL_SESSION_PREFIX,
        domain="sql",
        helper="query_folder_sql",
        params={},
        data_range=None,
        context=None,
        timeout_sec=configured_python_exec_timeout(ctx),
        error_code="DUCKDB_SQL_ERROR",
        error_label="DuckDB SQL",
        additional_data={
            "scoped_dir": scoped_dir,
            "sql": sql,
            "files": files or [] if isinstance(files, list) else (files or {}),
            "preloaded": preloaded or {},
            "flat_files": flat_files or {},
        },
    )


# --- Text Analytics (spaCy + textdescriptives) ---

_TEXT_SESSION_PREFIX = "writeragent:text"


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
    if isinstance(spec, str):
        helper = spec
        params: dict[str, Any] = {}
    else:
        helper = str(spec.get("helper", "") or "")
        params = spec.get("params") or {}
    return _run_trusted_action(
        ctx,
        session_id=_TEXT_SESSION_PREFIX,
        domain="text",
        helper=helper,
        params=params if isinstance(params, dict) else {},
        data_range=None,
        context=context,
        timeout_sec=timeout_sec,
        error_code="TEXT_ANALYTICS_ERROR",
        error_label="Text Analytics",
        additional_data={"text": text},
    )


# --- LanguageTool ---

_LT_SESSION_PREFIX = "writeragent:languagetool"


def run_languagetool_check(ctx: Any, text: str, bcp47: str) -> dict[str, Any]:
    """Execute a trusted LanguageTool check helper inside the user venv worker."""
    return _run_trusted_action(
        ctx,
        session_id=_LT_SESSION_PREFIX,
        domain="languagetool",
        helper="check",
        params={},
        data_range=None,
        context=None,
        timeout_sec=15,
        error_code="LANGUAGETOOL_ERROR",
        error_label="LanguageTool",
        additional_data={"text": text, "bcp47": bcp47},
    )


# --- Vale Style Linter ---

_VALE_SESSION_PREFIX = "writeragent:vale"


def run_vale_check(ctx: Any, text: str, config_dir: str, styles: str) -> dict[str, Any]:
    """Execute a trusted Vale linter helper inside the user venv worker."""
    return _run_trusted_action(
        ctx,
        session_id=_VALE_SESSION_PREFIX,
        domain="vale",
        helper="check",
        params={},
        data_range=None,
        context=None,
        timeout_sec=25,
        error_code="VALE_ERROR",
        error_label="Vale Linter",
        additional_data={"text": text, "config_dir": config_dir, "styles": styles},
    )


# --- Harper Rust Linter (host; implementation lives in harper_host to avoid this module's RPC imports) ---

from plugin.scripting.harper_host import _pump_grammar_status_ui, run_harper_check  # noqa: E402,F401
