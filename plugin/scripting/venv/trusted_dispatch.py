# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv dispatch for scripting, vision, embedding encode, and langdetect domains."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _trusted_action_spec(helper: Any, params: Any) -> dict[str, Any]:
    """Build a spec dict for spec-driven venv dispatchers."""
    coerced_params = params if isinstance(params, dict) else {}
    return {"helper": str(helper or ""), "params": coerced_params}


def _trusted_action_context(context: Any) -> dict[str, Any]:
    return context if isinstance(context, dict) else {}


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value


def _require_str_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return [str(item) for item in value]


def dispatch_trusted(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    """Route a run_trusted_action packet to the correct venv compute entry point."""
    domain = str(data.get("domain") or "")
    helper = data.get("helper")
    params = data.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    data_range = data.get("data_range")
    context = _trusted_action_context(data.get("context"))
    spec = _trusted_action_spec(helper, params)

    if domain == "units":
        from plugin.scripting.venv.units import run_units

        return run_units(spec, context=context)
    if domain in ("symbolic", "math"):
        from plugin.scripting.venv.symbolic import run_symbolic

        return run_symbolic(spec, context=context)
    if domain == "viz":
        from plugin.scripting.venv.viz import run_viz

        return run_viz(spec, data_range, context)
    if domain == "analysis":
        from plugin.scripting.venv.analysis import run_analysis

        return run_analysis(spec, data_range, context)
    if domain == "forecast":
        from plugin.scripting.venv.forecast import run_forecast

        return run_forecast(spec, data_range, context)
    if domain == "optimize":
        from plugin.scripting.venv.optimize import run_optimize

        return run_optimize(spec, data_range, context)
    if domain == "quant":
        from plugin.scripting.venv.quant import run_quant

        return run_quant(str(helper or ""), params, data_range, context)
    if domain == "text":
        from plugin.scripting.venv.text_analytics import run_text_analytics

        text = data.get("text") if data.get("text") is not None else data_range
        return run_text_analytics(spec, text, context)
    if domain == "vision":
        from plugin.vision.venv.vision import run_vision

        return run_vision(spec, data.get("image"), context)
    if domain == "sql":
        from plugin.scripting.venv.duckdb_sql import query_folder_sql

        return query_folder_sql(
            data.get("scoped_dir"),
            _require_str(data.get("sql"), "sql"),
            data.get("files"),
            data.get("preloaded"),
            data.get("flat_files"),
        )
    if domain == "languagetool":
        from plugin.scripting.venv.languagetool import run_languagetool_check

        return run_languagetool_check(
            _require_str(data.get("text"), "text"),
            _require_str(data.get("bcp47"), "bcp47"),
        )
    if domain == "vale":
        from plugin.scripting.venv.vale import run_vale_check

        return run_vale_check(
            _require_str(data.get("text"), "text"),
            _require_str(data.get("config_dir"), "config_dir"),
            _require_str(data.get("styles"), "styles"),
        )
    if domain == "harper":
        from plugin.scripting.venv.harper import run_harper_check

        return run_harper_check(
            _require_str(data.get("text"), "text"),
            _require_str(data.get("config_dir"), "config_dir"),
            bcp47=str(data.get("bcp47") or "en-US"),
        )
    if domain == "embedding":
        from plugin.embeddings.venv.embeddings_index import embed_texts

        model = _require_str(data.get("model"), "model")
        texts = _require_str_list(data.get("texts"), "texts")
        return embed_texts(model, texts)
    if domain == "langdetect":
        from plugin.embeddings.venv.langdetect_rpc import detect_lang_batch

        texts = _require_str_list(data.get("texts"), "texts")
        return {"languages": detect_lang_batch(texts)}

    raise ValueError(f"Unknown trusted action domain: {domain}")
