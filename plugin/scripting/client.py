# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unified scripting client — routes trusted scripting helpers to the warm venv worker."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.scripting.config_limits import (
    configured_python_exec_timeout,
    long_trusted_worker_timeout_sec,
    LANGUAGETOOL_WORKER_TIMEOUT_SEC,
    VALE_WORKER_TIMEOUT_SEC,
    VISION_WORKER_TIMEOUT_SEC,
)
from plugin.scripting.trusted_rpc import run_trusted_worker_action
from plugin.vision.vision_common import resolve_engine

log = logging.getLogger(__name__)


def _run_trusted_action(
    ctx: Any,
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
    headers: bool | None = None,
    header_row: int | None = None,
    stop_checker: Callable[[], bool] | None = None,
    send_cancellation: Any | None = None,
) -> dict[str, Any]:
    """Execute a trusted action packet in the user venv worker.

    Trusted helpers are not sandbox sessions. A ``writeragent:*`` session id
    used to be forwarded and never read by ``_handle_trusted_action``.
    """
    return run_trusted_worker_action(
        ctx,
        domain=domain,
        helper=helper,
        params=params,
        data_range=data_range,
        context=context,
        timeout_sec=timeout_sec,
        additional_data=additional_data,
        error_code=error_code,
        error_label=error_label,
        allow_heartbeat=allow_heartbeat,
        heartbeat_fn=heartbeat_fn,
        headers=headers,
        header_row=header_row,
        stop_checker=stop_checker,
        cancellation_scope=send_cancellation,
    )


# --- Long-running trusted helpers (use the single long budget instead of user python_exec_timeout) ---
# Vision is handled in its own resolver (also sources from the long budget for heavy paths).

_LONG_TRUSTED_PREFIXES = frozenset({
    "writeragent:text",
    "writeragent:symbolic",
})


def _resolve_trusted_timeout(ctx: Any, session_prefix: str) -> int:
    """Return the long budget for known slow calls, otherwise the user's standard timeout."""
    if session_prefix in _LONG_TRUSTED_PREFIXES:
        return long_trusted_worker_timeout_sec(ctx)
    return configured_python_exec_timeout(ctx)


def _spec_sheet_layout(spec: dict[str, Any]) -> dict[str, Any]:
    """Copy ``headers`` / ``header_row`` off a spec for the worker packet.

    Those keys sit beside ``helper`` and ``params``. Keeping only helper and
    params dropped them: the worker rebuilt the spec without ``headers``, and
    ``parse_trusted_spec`` defaulted it to true. ``forecast_data`` /
    ``optimize_data`` ``headers=false`` then consumed the first data row as
    column names.
    """
    layout: dict[str, Any] = {}
    if "headers" in spec:
        layout["headers"] = bool(spec["headers"])
    if "header_row" in spec:
        layout["header_row"] = int(spec["header_row"])
    return layout


def _make_spec_runner(
    *,
    session_prefix: str,
    domain: str,
    error_code: str,
    error_label: str,
    long_timeout: bool = False,
) -> Callable[..., dict[str, Any]]:
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
            layout: dict[str, Any] = {}
        else:
            helper = spec.get("helper", "")
            params = spec.get("params") or {}
            layout = _spec_sheet_layout(spec)

        return _run_trusted_action(
            ctx,
            domain=domain,
            helper=helper,
            params=params,
            data_range=data,
            context=context,
            timeout_sec=timeout_sec,
            error_code=error_code,
            error_label=error_label,
            headers=layout["headers"] if "headers" in layout else None,
            header_row=layout["header_row"] if "header_row" in layout else None,
            stop_checker=getattr(ctx, "stop_checker", None),
            send_cancellation=getattr(ctx, "send_cancellation", None),
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

run_quant = _make_spec_runner(
    session_prefix="writeragent:quant",
    domain="quant",
    error_code="QUANT_ERROR",
    error_label="Quant",
)


# --- Vision ---


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
        domain="vision",
        helper=helper,
        params=params,
        data_range=None,
        context=context,
        timeout_sec=timeout_sec,
        error_code="VISION_ERROR",
        error_label="Vision",
        additional_data={"image": image},
        stop_checker=getattr(ctx, "stop_checker", None),
        send_cancellation=getattr(ctx, "send_cancellation", None),
    )


# --- DuckDB SQL (folder) ---


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
    - flat_files: dict name -> full path for direct DuckDB flat files (CSV/TSV, Parquet, JSON/JSONL/NDJSON)
    """
    return _run_trusted_action(
        ctx,
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
            "files": files if isinstance(files, list) else (files or {}),
            "preloaded": preloaded or {},
            "flat_files": flat_files or {},
        },
        stop_checker=getattr(ctx, "stop_checker", None),
        send_cancellation=getattr(ctx, "send_cancellation", None),
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
    # writeragent.json "text_analytics_sentiment_model" used to apply only when
    # spec was a dict. A string helper name ("sentiment") skipped the assignment
    # and the worker kept its hard-coded model.
    model: Any = None
    try:
        from plugin.framework.config import get_config_dict
        cfg = get_config_dict() or {}
        model = cfg.get("text_analytics_sentiment_model") or None
    except Exception:
        log.exception("Could not read text_analytics_sentiment_model")

    timeout_sec = _resolve_trusted_timeout(ctx, _TEXT_SESSION_PREFIX)
    if isinstance(spec, str):
        helper = spec
        params: dict[str, Any] = {}
    else:
        helper = str(spec.get("helper", "") or "")
        params = spec.get("params") or {}
    if model:
        params = dict(params) if isinstance(params, dict) else {}
        params["model"] = model
    return _run_trusted_action(
        ctx,
        domain="text",
        helper=helper,
        params=params if isinstance(params, dict) else {},
        data_range=None,
        context=context,
        timeout_sec=timeout_sec,
        error_code="TEXT_ANALYTICS_ERROR",
        error_label="Text Analytics",
        additional_data={"text": text},
        stop_checker=getattr(ctx, "stop_checker", None),
        send_cancellation=getattr(ctx, "send_cancellation", None),
    )

# --- LanguageTool ---


def run_languagetool_check(ctx: Any, text: str, bcp47: str) -> dict[str, Any]:
    """Execute a trusted LanguageTool check helper inside the user venv worker."""
    return _run_trusted_action(
        ctx,
        domain="languagetool",
        helper="check",
        params={},
        data_range=None,
        context=None,
        timeout_sec=LANGUAGETOOL_WORKER_TIMEOUT_SEC,
        error_code="LANGUAGETOOL_ERROR",
        error_label="LanguageTool",
        additional_data={"text": text, "bcp47": bcp47},
        stop_checker=getattr(ctx, "stop_checker", None),
        send_cancellation=getattr(ctx, "send_cancellation", None),
    )


# --- Vale Style Linter ---


def run_vale_check(ctx: Any, text: str, config_dir: str, styles: str) -> dict[str, Any]:
    """Execute a trusted Vale linter helper inside the user venv worker."""
    return _run_trusted_action(
        ctx,
        domain="vale",
        helper="check",
        params={},
        data_range=None,
        context=None,
        timeout_sec=VALE_WORKER_TIMEOUT_SEC,
        error_code="VALE_ERROR",
        error_label="Vale Linter",
        additional_data={"text": text, "config_dir": config_dir, "styles": styles},
        stop_checker=getattr(ctx, "stop_checker", None),
        send_cancellation=getattr(ctx, "send_cancellation", None),
    )
