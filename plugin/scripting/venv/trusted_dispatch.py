# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-domain trusted venv dispatchers for run_trusted_action."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import Any

from plugin.scripting.sheet_layout import extract_sheet_layout
from plugin.scripting.venv.coerce import coerce_bool, coerce_int

log = logging.getLogger(__name__)


def ok_result(helper: str, **payload: Any) -> dict[str, Any]:
    return {"status": "ok", "helper": helper, **payload}


def error_result(
    code: str,
    message: str,
    *,
    helper: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "code": code, "message": message}
    if helper:
        out["helper"] = helper
    if details:
        out["details"] = details
    return out


def missing_package_error(helper: str, package: str) -> dict[str, Any]:
    return error_result(
        "MISSING_PACKAGE",
        f"{package} is required for {helper}.",
        helper=helper,
    )


def _sheet_layout_flags(spec_dict: dict[str, Any]) -> tuple[bool, int] | dict[str, Any]:
    """Extract and validate sheet layout flags (headers, header_row) from spec_dict."""
    try:
        headers = coerce_bool(spec_dict.get("headers"), name="headers", default=True)
        if "header_row" in spec_dict:
            header_row = coerce_int(spec_dict["header_row"], name="header_row")
        else:
            header_row = 0
    except ValueError as exc:
        return error_result("INVALID_SPEC", str(exc))
    return headers, header_row


def parse_trusted_spec(
    spec: dict[str, Any] | str,
    *,
    helper_names: frozenset[str] | set[str],
    context: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], bool, int, dict[str, Any]] | dict[str, Any]:
    """Shared preamble for run_analysis / run_forecast / run_optimize.

    Returns ``(helper, params, headers, header_row, context)`` or an error dict.
    """
    if isinstance(spec, str):
        spec_dict: dict[str, Any] = {"helper": spec}
    elif isinstance(spec, dict):
        spec_dict = spec
    else:
        return error_result("INVALID_SPEC", "spec must be a dict or helper name string")

    helper = str(spec_dict.get("helper") or "").strip()
    if not helper:
        return error_result("MISSING_HELPER", "spec.helper is required")
    if helper not in helper_names:
        return error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)

    params: dict[str, Any] = spec_dict["params"] if isinstance(spec_dict.get("params"), dict) else {}
    layout = _sheet_layout_flags(spec_dict)
    if isinstance(layout, dict):
        return layout
    headers, header_row = layout
    ctx = context if isinstance(context, dict) else {}
    return helper, params, headers, header_row, ctx


def resolve_spec_helper(
    spec: dict[str, Any] | str,
    *,
    helper_names: frozenset[str] | set[str],
    missing_code: str = "MISSING_HELPER",
) -> tuple[str, dict[str, Any]] | dict[str, Any]:
    """Extract helper name and spec dict from spec, or return an error dict."""
    if isinstance(spec, str):
        spec_dict: dict[str, Any] = {"helper": spec}
    elif isinstance(spec, dict):
        spec_dict = spec
    else:
        return error_result("INVALID_SPEC", "spec must be a dict or helper name")

    helper = str(spec_dict.get("helper") or "").strip()
    if not helper:
        return error_result(missing_code, "helper is required")
    if helper not in helper_names:
        return error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)
    return helper, spec_dict


def dispatch_common_kwargs(
    context: dict[str, Any],
    *,
    headers: bool,
    header_row: int,
) -> dict[str, Any]:
    """Shared kwargs dictionary for analysis, forecast, optimize, and viz dispatch."""
    sheet_hint = context.get("sheet_name") if isinstance(context.get("sheet_name"), str) else None
    return {"headers": headers, "header_row": header_row, "sheet_hint": sheet_hint}


def run_trusted_spec(
    spec: dict[str, Any] | str,
    data: Any,
    context: dict[str, Any] | None = None,
    *,
    helper_names: frozenset[str] | set[str],
    dispatch_fn: Any,
    domain: str,
    error_code: str,
) -> dict[str, Any]:
    """Shared execution body for run_analysis / run_forecast / run_optimize."""
    parsed = parse_trusted_spec(spec, helper_names=helper_names, context=context)
    if isinstance(parsed, dict):
        return parsed
    helper, params, headers, header_row, ctx = parsed

    try:
        result = dispatch_fn(helper, data, params, headers=headers, header_row=header_row, context=ctx)
    except Exception as exc:
        log.exception("%s helper %s failed", domain, helper)
        return error_result(error_code, str(exc), helper=helper)

    if isinstance(result, dict) and result.get("status") == "ok" and ctx:
        result["context"] = {k: v for k, v in ctx.items() if k in ("sheet_name", "range_a1", "task_hint")}

    return result


def _trusted_action_spec(helper: Any, params: Any, packet: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a spec dict for spec-driven venv dispatchers."""
    # What was wrong: non-dict params became {}, so a malformed packet ran
    # the helper with defaults and reported a misleading helper-level error.
    # How it happened: the boundary coerced any non-dict value instead of rejecting it.
    # Why this change: fail at the boundary like _require_str does for bad fields.
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be a dict")
    coerced_params = dict(params or {})
    spec: dict[str, Any] = {"helper": str(helper or ""), "params": coerced_params}
    spec.update(extract_sheet_layout(packet))
    return spec


def _require_str(value: Any, label: str) -> str:
    # Blank and whitespace-only values are rejected. The original string is
    # returned: LanguageTool and Vale offsets point into this text, and
    # strip() would move those spans. Callers that need a trimmed token
    # strip it themselves.
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value


def _require_str_list(value: Any, label: str) -> list[str]:
    # Every element must already be a str. str(None) or str(1) would hide a bad packet.
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{label} elements must be strings, got {type(item).__name__}")
    return list(value)


def _packet_parts(data: dict[str, Any]) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    """Return ``(spec, data_range, context)`` from a trusted-action packet."""
    spec = _trusted_action_spec(data.get("helper"), data.get("params"), data)
    raw_ctx = data.get("context")
    context = raw_ctx if isinstance(raw_ctx, dict) else {}
    return spec, data.get("data_range"), context


def _dispatch_spec_data(run_fn: Callable[..., Any], data: dict[str, Any]) -> Any:
    """Adapter for ``run_*(spec, data_range, context)`` domain entry points."""
    spec, data_range, context = _packet_parts(data)
    return run_fn(spec, data_range, context)


# Same seven domains as the spec rows in trusted_action_registry.
# text, vision, sql, languagetool, vale, embedding, and langdetect do not fit.
_SPEC_DOMAIN_RUNNERS: dict[str, tuple[str, str]] = {
    "units": ("plugin.scripting.venv.units", "run_units"),
    "symbolic": ("plugin.scripting.venv.symbolic", "run_symbolic"),
    "viz": ("plugin.scripting.venv.viz", "run_viz"),
    "analysis": ("plugin.scripting.venv.analysis", "run_analysis"),
    "forecast": ("plugin.scripting.venv.forecast", "run_forecast"),
    "optimize": ("plugin.scripting.venv.optimize", "run_optimize"),
    "quant": ("plugin.scripting.venv.quant", "run_quant"),
}


def dispatch_spec_domain(data: dict[str, Any]) -> Any:
    domain = data.get("domain")
    target = _SPEC_DOMAIN_RUNNERS.get(domain) if isinstance(domain, str) else None
    # Harness lookup used this same domain string. A miss is a direct call.
    assert target is not None, domain
    module_name, attr_name = target
    mod = importlib.import_module(module_name)
    return _dispatch_spec_data(getattr(mod, attr_name), data)


def dispatch_text(data: dict[str, Any]) -> Any:
    from plugin.scripting.venv.text_analytics import run_text_analytics

    spec, data_range, context = _packet_parts(data)
    raw_text = data.get("text")
    # Empty string is real text. Only a missing value falls back to data_range.
    text = raw_text if raw_text is not None else data_range
    return run_text_analytics(spec, text, context)


def dispatch_vision(data: dict[str, Any]) -> Any:
    from plugin.vision.venv.vision import run_vision

    spec, _unused, context = _packet_parts(data)
    return run_vision(spec, data.get("image"), context)


def dispatch_sql(data: dict[str, Any]) -> Any:
    from plugin.scripting.venv.duckdb_sql import query_folder_sql

    # One implementation. `helper` is not a selector. Do not pass session_id:
    # it is not on this packet, run_folder_sql does not send one, and
    # trusted routing ids are not DuckDB kernels. Each call stays per-request.
    return query_folder_sql(
        data.get("scoped_dir"),
        _require_str(data.get("sql"), "sql"),
        data.get("files"),
        data.get("preloaded"),
        data.get("flat_files"),
    )


def dispatch_languagetool(data: dict[str, Any]) -> Any:
    from plugin.scripting.venv.languagetool import run_languagetool_check

    return run_languagetool_check(
        _require_str(data.get("text"), "text"),
        _require_str(data.get("bcp47"), "bcp47"),
    )


def dispatch_vale(data: dict[str, Any]) -> Any:
    from plugin.scripting.venv.vale import run_vale_check

    return run_vale_check(
        _require_str(data.get("text"), "text"),
        _require_str(data.get("config_dir"), "config_dir"),
        _require_str(data.get("styles"), "styles"),
    )


def dispatch_embedding(data: dict[str, Any]) -> Any:
    from plugin.embeddings.venv.embeddings_index import embed_texts

    model = _require_str(data.get("model"), "model")
    texts = _require_str_list(data.get("texts"), "texts")
    return embed_texts(model, texts)


def dispatch_langdetect(data: dict[str, Any]) -> Any:
    from plugin.embeddings.venv.langdetect_rpc import detect_lang_batch

    texts = _require_str_list(data.get("texts"), "texts")
    return {"languages": detect_lang_batch(texts)}
