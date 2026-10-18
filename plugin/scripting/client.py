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
    DOCLING_WORKER_TIMEOUT_SEC,
    configured_python_exec_timeout,
    long_trusted_worker_timeout_sec,
    LANGUAGETOOL_WORKER_TIMEOUT_SEC,
    VALE_WORKER_TIMEOUT_SEC,
    VISION_WORKER_TIMEOUT_SEC,
)
from plugin.scripting.sheet_layout import extract_sheet_layout
from plugin.scripting.trusted_rpc import run_trusted_worker_action
from plugin.vision.vision_common import resolve_engine



def _split_spec(spec: Any) -> tuple[str, dict[str, Any]]:
    """Split a spec (str, dict, or non-dict) into (helper, params)."""
    if isinstance(spec, str):
        return spec, {}
    if not isinstance(spec, dict):
        return "", {}
    raw_params = spec.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}
    return str(spec.get("helper") or ""), params


def _make_spec_runner(
    *,
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
            long_trusted_worker_timeout_sec()
            if long_timeout
            else configured_python_exec_timeout()
        )
        helper, params = _split_spec(spec)
        layout = extract_sheet_layout(spec)

        return run_trusted_worker_action(
            ctx,
            domain=domain,
            helper=helper,
            params=params,
            data_range=data,
            context=context,
            timeout_sec=timeout_sec,
            error_code=error_code,
            error_label=error_label,
            headers=layout.get("headers"),
            header_row=layout.get("header_row"),
        )

    _runner.__name__ = f"run_{domain}"
    _runner.__doc__ = f"Execute a trusted {error_label} helper in the user venv."
    return _runner


run_analysis = _make_spec_runner(
    domain="analysis",
    error_code="ANALYSIS_ERROR",
    error_label="Analysis",
)

run_viz = _make_spec_runner(
    domain="viz",
    error_code="VIZ_ERROR",
    error_label="Viz",
)

run_symbolic = _make_spec_runner(
    domain="symbolic",
    error_code="SYMBOLIC_ERROR",
    error_label="Symbolic",
    long_timeout=True,
)

run_units = _make_spec_runner(
    domain="units",
    error_code="UNITS_ERROR",
    error_label="Units",
)

run_optimize = _make_spec_runner(
    domain="optimize",
    error_code="OPTIMIZE_ERROR",
    error_label="Optimization",
)

run_forecast = _make_spec_runner(
    domain="forecast",
    error_code="FORECAST_ERROR",
    error_label="Forecast",
)


# # --- Vision ---


def _resolve_vision_timeout_sec(spec: dict[str, Any] | str) -> int:
    """Vision uses the long budget, with some engine-specific tuning + user override."""
    # Read vision.worker_timeout_sec via get_config_int_safe for both engines.
    # A hardcoded 120s for Paddle ignored the user override, and swallowing
    # the lookup hid config errors.
    from plugin.framework.config import get_config_int_safe

    custom = get_config_int_safe("vision.worker_timeout_sec")
    # DOCLING_WORKER_TIMEOUT_SEC (300) is the schema default in module.yaml.
    # An explicit override (custom != 300 and custom > 0) overrides both engines.
    if custom > 0 and custom != DOCLING_WORKER_TIMEOUT_SEC:
        return int(custom)

    long_budget = long_trusted_worker_timeout_sec()
    helper, params = _split_spec(spec)
    del helper
    if resolve_engine(params) == "paddle":
        return VISION_WORKER_TIMEOUT_SEC  # slightly lighter than full Docling
    return long_budget  # Docling default path uses the long trusted budget


def run_vision(
    ctx: Any,
    spec: dict[str, Any] | str,
    image: Any = None,
    *,
    context: dict[str, Any] | None = None,
    stop_checker: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Execute a trusted vision helper in the user venv."""
    timeout_sec = _resolve_vision_timeout_sec(spec)
    helper, params = _split_spec(spec)
    return run_trusted_worker_action(
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
        stop_checker=stop_checker,
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
    return run_trusted_worker_action(
        ctx,
        domain="sql",
        helper="query_folder_sql",
        params={},
        data_range=None,
        context=None,
        timeout_sec=configured_python_exec_timeout(),
        error_code="DUCKDB_SQL_ERROR",
        error_label="DuckDB SQL",
        additional_data={
            "scoped_dir": scoped_dir,
            "sql": sql,
            "files": files if isinstance(files, list) else (files or {}),
            "preloaded": preloaded or {},
            "flat_files": flat_files or {},
        },
    )


# --- Text Analytics (spaCy + textdescriptives) ---


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
    from plugin.framework.config import get_config_dict

    model = get_config_dict().get("text_analytics_sentiment_model") or None

    timeout_sec = long_trusted_worker_timeout_sec()
    helper, params = _split_spec(spec)
    if model:
        params = dict(params)
        params["model"] = model
    return run_trusted_worker_action(
        ctx,
        domain="text",
        helper=helper,
        params=params,
        data_range=None,
        context=context,
        timeout_sec=timeout_sec,
        error_code="TEXT_ANALYTICS_ERROR",
        error_label="Text Analytics",
        additional_data={"text": text},
    )


# --- LanguageTool ---


def run_languagetool_check(ctx: Any, text: str, bcp47: str) -> dict[str, Any]:
    """Execute a trusted LanguageTool check helper inside the user venv worker."""
    return run_trusted_worker_action(
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
    )


# --- Vale Style Linter ---


def run_vale_check(ctx: Any, text: str, config_dir: str, styles: str) -> dict[str, Any]:
    """Execute a trusted Vale linter helper inside the user venv worker."""
    return run_trusted_worker_action(
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
    )
