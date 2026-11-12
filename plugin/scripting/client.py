# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unified scripting client — routes trusted scripting helpers to the warm venv worker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from plugin.framework.send_cancellation import SendCancellation

from plugin.scripting.config_limits import (
    DOCLING_WORKER_TIMEOUT_SEC,
    long_trusted_worker_timeout_sec,
    LANGUAGETOOL_WORKER_TIMEOUT_SEC,
    python_exec_timeout,
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


def _run_spec_domain(
    domain: str,
    ctx: Any,
    spec: dict[str, Any] | str,
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from plugin.scripting.domain_registry import get_domain_wiring

    wiring_id = "math" if domain == "symbolic" else domain
    row = get_domain_wiring(wiring_id)
    error_code = row.error_code or f"{domain.upper()}_ERROR"
    error_label = "Optimization" if domain == "optimize" else domain.capitalize()
    timeout_sec = (
        long_trusted_worker_timeout_sec()
        if row.long_timeout
        else python_exec_timeout()
    )
    helper, params = _split_spec(spec)
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
        additional_data={
            key: value
            for key, value in extract_sheet_layout(spec).items()
            if value is not None
        } or None,
    )


def run_analysis(
    ctx: Any,
    spec: dict[str, Any] | str,
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted Analysis helper in the user venv."""
    return _run_spec_domain("analysis", ctx, spec, data, context=context)


def run_viz(
    ctx: Any,
    spec: dict[str, Any] | str,
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted Viz helper in the user venv."""
    return _run_spec_domain("viz", ctx, spec, data, context=context)


def run_symbolic(
    ctx: Any,
    spec: dict[str, Any] | str,
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted Symbolic helper in the user venv."""
    return _run_spec_domain("symbolic", ctx, spec, data, context=context)


def run_optimize(
    ctx: Any,
    spec: dict[str, Any] | str,
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted Optimization helper in the user venv."""
    return _run_spec_domain("optimize", ctx, spec, data, context=context)


def run_forecast(
    ctx: Any,
    spec: dict[str, Any] | str,
    data: Any = None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted Forecast helper in the user venv."""
    return _run_spec_domain("forecast", ctx, spec, data, context=context)


# # --- Vision ---


def _resolve_vision_timeout_sec(params: dict[str, Any]) -> int:
    """Vision uses the long budget, with some engine-specific tuning + user override."""
    # Read vision.worker_timeout_sec via get_config for both engines.
    # A hardcoded 120s for Paddle ignored the user override, and swallowing
    # the lookup hid config errors.
    from plugin.framework.config import get_config

    custom = get_config("vision.worker_timeout_sec")
    # DOCLING_WORKER_TIMEOUT_SEC (300) is the schema default in module.yaml.
    # An explicit override (custom != 300 and custom > 0) overrides both engines.
    if custom > 0 and custom != DOCLING_WORKER_TIMEOUT_SEC:
        return int(custom)

    if resolve_engine(params) == "paddle":
        return VISION_WORKER_TIMEOUT_SEC  # slightly lighter than full Docling
    return long_trusted_worker_timeout_sec()


def run_vision(
    ctx: Any,
    spec: dict[str, Any] | str,
    image: Any = None,
    *,
    context: dict[str, Any] | None = None,
    cancel: SendCancellation | None = None,
) -> dict[str, Any]:
    """Execute a trusted vision helper in the user venv."""
    helper, params = _split_spec(spec)
    timeout_sec = _resolve_vision_timeout_sec(params)
    return run_trusted_worker_action(
        ctx,
        domain="vision",
        helper=helper,
        params=params,
        context=context,
        timeout_sec=timeout_sec,
        error_code="VISION_ERROR",
        error_label="Vision",
        additional_data={"image": image},
        cancel=cancel,
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
        timeout_sec=python_exec_timeout(),
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
    from plugin.framework.config import get_config

    model = get_config("text_analytics_sentiment_model") or None

    helper, params = _split_spec(spec)
    if model:
        params = dict(params)
        params["model"] = model
    return run_trusted_worker_action(
        ctx,
        domain="text",
        helper=helper,
        params=params,
        context=context,
        timeout_sec=long_trusted_worker_timeout_sec(),
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
        timeout_sec=VALE_WORKER_TIMEOUT_SEC,
        error_code="VALE_ERROR",
        error_label="Vale Linter",
        additional_data={"text": text, "config_dir": config_dir, "styles": styles},
    )
