# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-domain trusted venv dispatchers for run_trusted_action.

Each dispatcher, and `run_domain`'s format and units arms, imports its domain
module inside the function. Most venv helper modules (`analysis`, `units`,
`duckdb_sql`, …) import this file at their top, so a top-level import here would
be a cycle. The compute bundle (`scripts/build_compute_bundle.py`) ships this
file without `text_analytics`, `duckdb_sql`, `vale`, `languagetool` or the
embeddings modules.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any

from plugin.scripting.helper_registry import bound, get_spec, names
from plugin.scripting.sheet_layout import extract_sheet_layout
from plugin.scripting.venv.coerce import coerce_bool, coerce_int
from plugin.scripting.venv.results import error_result

log = logging.getLogger(__name__)


def ok_result(helper: str, **payload: Any) -> dict[str, Any]:
    return {"status": "ok", "helper": helper, **payload}


def missing_package_error(helper: str, package: str) -> dict[str, Any]:
    return error_result(
        "MISSING_PACKAGE",
        f"{package} is required for {helper}.",
        helper=helper,
    )


def is_blank_param(value: Any, *, strip: bool = False) -> bool:
    # Ranges must not use ``!= ""`` (numpy truthiness). strip=False keeps
    # whitespace-only quantities parseable; strip=True treats them as blank.
    if value is None:
        return True
    if isinstance(value, str):
        return (value.strip() if strip else value) == ""
    return False


def _sheet_layout_flags(spec_dict: dict[str, Any]) -> tuple[bool, int] | dict[str, Any]:
    """Extract and validate sheet layout flags (headers, header_row) from spec_dict."""
    try:
        headers = coerce_bool(spec_dict.get("headers"), name="headers", default=True)
        header_row = coerce_int(spec_dict["header_row"], name="header_row") if "header_row" in spec_dict else 0
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
    resolved = resolve_spec_helper(spec, helper_names=helper_names)
    if isinstance(resolved, dict):
        return resolved
    helper, spec_dict = resolved

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
) -> tuple[str, dict[str, Any]] | dict[str, Any]:
    """Extract helper name and spec dict from spec, or return an error dict."""
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
    return helper, spec_dict


def _decimals(params: dict[str, Any], default: int, helper: str) -> int | dict[str, Any]:
    try:
        decimals = coerce_int(params.get("decimals", default), name="decimals")
    except ValueError as exc:
        return error_result("INVALID_PARAM", str(exc), helper=helper)
    if decimals < 0:
        return error_result("INVALID_PARAM", f"decimals must be an integer >= 0, got {decimals!r}", helper=helper)
    return decimals


def run_domain(
    domain: str,
    spec: dict[str, Any] | str,
    data: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a trusted helper within *domain*."""
    ctx = context if isinstance(context, dict) else {}

    # 1. Spec parsing
    if domain in ("analysis", "forecast", "optimize", "quant", "symbolic"):
        parsed = parse_trusted_spec(spec, helper_names=names(domain), context=context)
        if isinstance(parsed, dict):
            return parsed
        helper, params, headers, header_row, ctx = parsed
    elif domain == "viz":
        res = resolve_spec_helper(spec, helper_names=names("viz"))
        if isinstance(res, dict):
            return res
        helper, spec_dict = res
        raw_params = spec_dict.get("params")
        if raw_params is None:
            params = {k: v for k, v in spec_dict.items() if k not in ("helper", "headers", "header_row")}
        elif isinstance(raw_params, dict):
            params = raw_params
        else:
            params = {}
        layout = _sheet_layout_flags(spec_dict)
        if isinstance(layout, dict):
            return layout
        headers, header_row = layout
    elif domain == "units":
        res = resolve_spec_helper(spec, helper_names=names("units"))
        if isinstance(res, dict):
            return res
        helper, spec_dict = res
        raw_params = spec_dict.get("params")
        if raw_params is None:
            params = {k: v for k, v in spec_dict.items() if k not in ("helper", "params", "headers", "header_row")}
        elif isinstance(raw_params, dict):
            params = raw_params
        else:
            return error_result("INVALID_SPEC", "params must be a dict", helper=helper)
        clean = dict(params)
        clean.pop("output_style", None)
        params = clean
        from plugin.scripting.venv.units import get_ureg
        if get_ureg() is None:
            return missing_package_error(helper, "pint")
        headers = True
        header_row = 0
    elif domain == "text":
        if isinstance(spec, dict):
            helper = str(spec.get("helper") or spec.get("type") or "full")
            raw_params = spec.get("params")
            params = raw_params if isinstance(raw_params, dict) else {}
        else:
            helper = str(spec or "full")
            params = {}
        # The child accepts only the registry's text names, the same rule as every other domain.
        if helper not in names("text"):
            return error_result("UNKNOWN_HELPER", f"Unknown text analytics helper {helper!r}", helper=helper)
        from plugin.scripting.venv.text_analytics import dispatch_text_analytics
        return dispatch_text_analytics(helper, data, params, context=ctx)
    else:
        return error_result("UNKNOWN_DOMAIN", f"Unknown domain {domain!r}")

    spec_obj = get_spec(domain, helper)
    if spec_obj is None:
        return error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)

    # 2. Pre-execution checks
    for forbid_key, forbid_code, forbid_msg in spec_obj.forbid:
        if forbid_key in params:
            return error_result(forbid_code, forbid_msg, helper=helper)

    if spec_obj.reject_unknown:
        unknown = sorted(set(params) - set(spec_obj.allow))
        if unknown:
            return error_result(
                "INVALID_PARAM",
                f"{helper} does not accept {', '.join(unknown)}. Allowed: {', '.join(sorted(spec_obj.allow))}.",
                helper=helper,
            )

    if spec_obj.required_present:
        if any(k not in params for k in spec_obj.required_present):
            return error_result("MISSING_PARAM", spec_obj.required_message, helper=helper)

    if spec_obj.required_truthy:
        if any(not params.get(k) for k in spec_obj.required_truthy):
            return error_result("MISSING_PARAM", spec_obj.required_message, helper=helper)

    # 3. Invocation according to bind mode
    bind = spec_obj.bind

    def _execute() -> dict[str, Any]:
        fn = bound(domain, helper)
        if bind == "present":
            sig = inspect.signature(fn)
            present_kwargs: dict[str, Any] = {k: params[k] for k in spec_obj.allow if k in params}
            sheet_hint = ctx.get("sheet_name") if isinstance(ctx.get("sheet_name"), str) else None
            if "headers" in sig.parameters:
                present_kwargs["headers"] = headers
            if "header_row" in sig.parameters:
                present_kwargs["header_row"] = header_row
            if "sheet_hint" in sig.parameters:
                present_kwargs["sheet_hint"] = sheet_hint
            return fn(data, **present_kwargs)

        if bind == "format":
            # Lazy (cycle): venv/analysis.py imports this module at its top.
            from plugin.scripting.venv.analysis import format_cells_result

            values = params.get("values", data)
            if helper == "format_currency":
                symbol = params.get("symbol", "$")
                if not isinstance(symbol, str):
                    return error_result("INVALID_PARAM", f"symbol must be a string, got {symbol!r}", helper=helper)
                decimals = _decimals(params, 2, helper)
                if isinstance(decimals, dict):
                    return decimals
                return format_cells_result(helper, fn(values, symbol=symbol, decimals=decimals))
            if helper == "format_percent":
                decimals = _decimals(params, 1, helper)
                if isinstance(decimals, dict):
                    return decimals
                return format_cells_result(helper, fn(values, decimals=decimals))
            return error_result("UNKNOWN_HELPER", f"Unknown format helper {helper!r}", helper=helper)

        if bind == "spec":
            sig = inspect.signature(fn)
            chart_spec = params["spec"] if isinstance(params.get("spec"), dict) else params
            spec_kwargs: dict[str, Any] = {"spec": chart_spec}
            sheet_hint = ctx.get("sheet_name") if isinstance(ctx.get("sheet_name"), str) else None
            if "headers" in sig.parameters:
                spec_kwargs["headers"] = headers
            if "header_row" in sig.parameters:
                spec_kwargs["header_row"] = header_row
            if "sheet_hint" in sig.parameters:
                spec_kwargs["sheet_hint"] = sheet_hint
            return fn(data, **spec_kwargs)

        if bind == "time_series":
            sig = inspect.signature(fn)
            optional = {
                key: str(params[key]) if params.get(key) else None
                for key in ("forecast_col", "lower_col", "upper_col", "historical_value_col")
            }
            ts_kwargs: dict[str, Any] = {
                "date_col": str(params["date_col"]),
                "value_col": str(params["value_col"]),
                **optional,
            }
            sheet_hint = ctx.get("sheet_name") if isinstance(ctx.get("sheet_name"), str) else None
            if "headers" in sig.parameters:
                ts_kwargs["headers"] = headers
            if "header_row" in sig.parameters:
                ts_kwargs["header_row"] = header_row
            if "sheet_hint" in sig.parameters:
                ts_kwargs["sheet_hint"] = sheet_hint
            return fn(data, **ts_kwargs)

        if bind == "params":
            if helper == "fetch_historical_data":
                return fn(params)
            return fn(params, data, headers=headers, header_row=header_row)

        if bind == "symbolic":
            sig = inspect.signature(fn)
            kwargs: dict[str, Any] = {}
            for pname, param in sig.parameters.items():
                value = params.get(pname)
                if value is None:
                    if param.default is not inspect.Parameter.empty:
                        continue
                    value = ""
                kwargs[pname] = value
            if helper == "latex_to_math_object" and is_blank_param(kwargs.get("latex"), strip=True):
                expression = params.get("expression")
                if not is_blank_param(expression, strip=True):
                    kwargs["latex"] = expression
            return fn(**kwargs)

        if bind == "units":
            return fn(params)

        return error_result("UNKNOWN_HELPER", f"Unhandled bind mode {bind!r}", helper=helper)

    # 4. Error wrapping (analysis, forecast, optimize only)
    if domain in ("analysis", "forecast", "optimize"):
        label = "Optimization" if domain == "optimize" else domain.capitalize()
        code = "OPTIMIZATION_FAILED" if domain == "optimize" else f"{domain.upper()}_FAILED"
        try:
            result = _execute()
        except Exception as exc:
            log.exception("%s helper %s failed", label, helper)
            return error_result(code, str(exc), helper=helper)
    else:
        result = _execute()

    # 5. Context stamping
    if isinstance(result, dict) and result.get("status") == "ok":
        if domain in ("analysis", "forecast", "optimize", "quant", "symbolic"):
            if ctx:
                result["context"] = {k: v for k, v in ctx.items() if k in ("sheet_name", "range_a1", "task_hint")}
        elif domain == "units":
            if ctx and "task_hint" in ctx:
                result["task_hint"] = ctx["task_hint"]

    return result


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
    # Reject non-dict params at the packet boundary so malformed tool calls fail fast instead of running helpers with empty defaults.
    params = data.get("params")
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be a dict")
    spec: dict[str, Any] = {"helper": str(data.get("helper") or ""), "params": dict(params or {})}
    spec.update(extract_sheet_layout(data))
    raw_ctx = data.get("context")
    context = raw_ctx if isinstance(raw_ctx, dict) else {}
    return spec, data.get("data_range"), context


# text, vision, sql, languagetool, vale, embedding, and langdetect do not fit
# this (spec, data, context) shape.
def dispatch_spec_domain(data: dict[str, Any]) -> Any:
    domain = data.get("domain")
    mod = importlib.import_module(f"plugin.scripting.venv.{domain}")
    spec, data_range, context = _packet_parts(data)
    return getattr(mod, f"run_{domain}")(spec, data_range, context)


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
