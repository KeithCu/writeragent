# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-domain trusted venv dispatchers for run_trusted_action (scripting, vision, encode, langdetect)."""

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


def _packet_parts(data: dict[str, Any]) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    """Return ``(spec, data_range, context)`` from a trusted-action packet."""
    params = data.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    spec = _trusted_action_spec(data.get("helper"), params)
    context = _trusted_action_context(data.get("context"))
    return spec, data.get("data_range"), context


def _dispatch_spec_data(run_fn: Callable[..., Any], data: dict[str, Any]) -> Any:
    """Adapter for ``run_*(spec, data_range, context)`` domain entry points."""
    spec, data_range, context = _packet_parts(data)
    return run_fn(spec, data_range, context)


def dispatch_units(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.scripting.venv.units import run_units

    spec, _, context = _packet_parts(data)
    return run_units(spec, context=context)


def dispatch_symbolic(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.scripting.venv.symbolic import run_symbolic

    spec, _, context = _packet_parts(data)
    return run_symbolic(spec, context=context)


def dispatch_viz(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.scripting.venv.viz import run_viz

    return _dispatch_spec_data(run_viz, data)


def dispatch_analysis(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.scripting.venv.analysis import run_analysis

    return _dispatch_spec_data(run_analysis, data)


def dispatch_forecast(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.scripting.venv.forecast import run_forecast

    return _dispatch_spec_data(run_forecast, data)


def dispatch_optimize(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.scripting.venv.optimize import run_optimize

    return _dispatch_spec_data(run_optimize, data)


def dispatch_quant(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.scripting.venv.quant import run_quant

    return _dispatch_spec_data(run_quant, data)


def dispatch_text(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.scripting.venv.text_analytics import run_text_analytics

    spec, data_range, context = _packet_parts(data)
    text = data.get("text") if data.get("text") is not None else data_range
    return run_text_analytics(spec, text, context)


def dispatch_vision(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.vision.venv.vision import run_vision

    spec, _, context = _packet_parts(data)
    return run_vision(spec, data.get("image"), context)


def dispatch_sql(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.scripting.venv.duckdb_sql import query_folder_sql

    return query_folder_sql(
        data.get("scoped_dir"),
        _require_str(data.get("sql"), "sql"),
        data.get("files"),
        data.get("preloaded"),
        data.get("flat_files"),
    )


def dispatch_languagetool(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.scripting.venv.languagetool import run_languagetool_check

    return run_languagetool_check(
        _require_str(data.get("text"), "text"),
        _require_str(data.get("bcp47"), "bcp47"),
    )


def dispatch_vale(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.scripting.venv.vale import run_vale_check

    return run_vale_check(
        _require_str(data.get("text"), "text"),
        _require_str(data.get("config_dir"), "config_dir"),
        _require_str(data.get("styles"), "styles"),
    )


def dispatch_harper(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    # Non-grammar callers only: realtime grammar uses harper_host in-process so users
    # without a configured Python venv still get Harper (warm worker requires a venv).
    from plugin.scripting.venv.harper import run_harper_check

    return run_harper_check(
        _require_str(data.get("text"), "text"),
        _require_str(data.get("config_dir"), "config_dir"),
        bcp47=str(data.get("bcp47") or "en-US"),
        heartbeat_fn=heartbeat_fn,
    )


def dispatch_embedding(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.embeddings.venv.embeddings_index import embed_texts

    model = _require_str(data.get("model"), "model")
    texts = _require_str_list(data.get("texts"), "texts")
    return embed_texts(model, texts)


def dispatch_langdetect(data: dict[str, Any], *, heartbeat_fn: Callable[[dict[str, Any]], None] | None = None) -> Any:
    del heartbeat_fn
    from plugin.embeddings.venv.langdetect_rpc import detect_lang_batch

    texts = _require_str_list(data.get("texts"), "texts")
    return {"languages": detect_lang_batch(texts)}
