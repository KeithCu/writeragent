# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ordered registry of trusted helper domains for RPS and the script picker.

Domain compute / egress stay in domain modules. Callables use lazy imports to avoid cycles.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from plugin.doc.document_helpers import is_calc
from plugin.framework.i18n import _
from plugin.scripting.helper_domain import (
    HelperScriptMeta,
    format_elapsed_time,
    plot_insert_ok_outcome,
    rps_insert_failed_outcome,
    rps_ok_outcome,
    symbolic_insert_ok_outcome,
    units_insert_ok_outcome,
)

log = logging.getLogger("writeragent.scripting")

# --- Script picker origins / prefixes (stable IDs used in origin_map) ---

SCRIPT_ORIGIN_USER = "user"
SCRIPT_ORIGIN_DOCUMENT = "document"
SCRIPT_ORIGIN_ANALYSIS = "analysis"
SCRIPT_ORIGIN_VISION = "vision"
SCRIPT_ORIGIN_VIZ = "viz"
SCRIPT_ORIGIN_MATH = "math"
SCRIPT_ORIGIN_UNITS = "units"
SCRIPT_ORIGIN_QUANT = "quant"
SCRIPT_ORIGIN_OPTIMIZE = "optimize"
SCRIPT_ORIGIN_FORECAST = "forecast"
SCRIPT_ORIGIN_SQL = "sql"

DOC_SCRIPT_DISPLAY_PREFIX = "[Doc] "
ANALYSIS_SCRIPT_DISPLAY_PREFIX = "[Analysis] "
VISION_SCRIPT_DISPLAY_PREFIX = "[Vision] "
VIZ_SCRIPT_DISPLAY_PREFIX = "[Viz] "
MATH_SCRIPT_DISPLAY_PREFIX = "[Math] "
UNITS_SCRIPT_DISPLAY_PREFIX = "[Units] "
QUANT_SCRIPT_DISPLAY_PREFIX = "[Quant] "
OPTIMIZE_SCRIPT_DISPLAY_PREFIX = "[Optimize] "
FORECAST_SCRIPT_DISPLAY_PREFIX = "[Forecast] "
SQL_SCRIPT_DISPLAY_PREFIX = "[SQL] "


@dataclass(frozen=True)
class PickerDomainSpec:
    """One built-in helper group in the Run Python Script picker."""

    origin: str
    display_prefix: str
    title_fn: Callable[[], str]
    supports: Callable[[Any], bool]
    templates: Callable[[], dict[str, str]]


@dataclass
class RpsDomainSpec:
    """Post-venv result routing for one trusted helper domain."""

    id: str
    insert: Callable[..., Any] | None = None
    format_ok: Callable[..., dict[str, Any]] | None = None
    is_result: Callable[[Any], bool] | None = None
    post_venv_calc_only: bool = False


def try_rps_post_venv(
    spec: RpsDomainSpec,
    *,
    ctx: Any,
    doc: Any,
    result_data: Any,
    t0: float,
    stdout: str | None,
    code: str | None = None,
) -> dict[str, Any] | None:
    """Route a generic venv result through domain is_result + insert, or None."""
    if spec.is_result is None or spec.insert is None or spec.format_ok is None:
        return None
    if spec.post_venv_calc_only and not is_calc(doc):
        return None
    if not spec.is_result(result_data):
        return None
    insert_kwargs: dict[str, Any] = {}
    if spec.id == "units" and code:
        from plugin.scripting.helper_domain import parse_run_import_call_params
        from plugin.scripting.units import split_helper_params

        body_params = parse_run_import_call_params(code, run_name="run_units")
        if body_params is not None:
            _, output_style = split_helper_params(body_params)
            if output_style is not None:
                insert_kwargs["output_style"] = output_style
    elif spec.id == "vision" and code:
        from plugin.scripting.helper_domain import parse_run_import_call_spec
        from plugin.vision.vision_common import merge_vision_params

        call_spec = parse_run_import_call_spec(code, run_name="run_vision") or {}
        raw_params = call_spec.get("params") if isinstance(call_spec.get("params"), dict) else None
        insert_kwargs["params"] = merge_vision_params(ctx, raw_params)
    try:
        row_count = spec.insert(ctx, doc, result_data, **insert_kwargs)
    except Exception as e:
        return rps_insert_failed_outcome(e, t0=t0)

    # Synthetic meta for format_ok when no header was used.
    helper = ""
    if isinstance(result_data, dict):
        helper = str(result_data.get("helper") or "")
    meta = HelperScriptMeta(helper=helper, params={})
    return spec.format_ok(meta=meta, result=result_data, t0=t0, row_count=row_count, stdout=stdout)


# --- Domain adapters (lazy imports) ---


# --- Declarative Domain registry wiring ---

@dataclass(frozen=True)
class DomainWiring:
    id: str
    insert: str
    is_result: str
    format_ok_kind: str = "generic"  # generic | plot | symbolic | units | vision | rows
    post_venv_calc_only: bool = False


def _resolve_fn(path: str | None) -> Any:
    if not path:
        return None
    import importlib
    mod_name, attr_name = path.rsplit(".", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, attr_name)


def build_rps_spec(w: DomainWiring) -> RpsDomainSpec:
    def insert(ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> Any:
        fn = _resolve_fn(w.insert)
        if w.id == "units":
            ret = fn(ctx, doc, result, output_style=kwargs.get("output_style"))
        elif w.id == "vision":
            params = kwargs.get("params")
            if params is not None:
                ret = fn(ctx, doc, result, params=params)
            else:
                ret = fn(ctx, doc, result)
        else:
            ret = fn(ctx, doc, result)
        if ret is not None:
            return int(ret)
        return None

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        helper = str(getattr(meta, "helper", "") or "") or str(result.get("helper") or w.id)
        formatted_time = format_elapsed_time(time.perf_counter() - t0)

        if w.format_ok_kind == "plot":
            title = str(result.get("title") or helper or "Plot")
            return plot_insert_ok_outcome(helper=helper, title=title, t0=t0, stdout=stdout, result=result)
        if w.format_ok_kind == "symbolic":
            latex = str(result.get("latex") or result.get("text") or helper or "")
            return symbolic_insert_ok_outcome(helper=helper, latex=latex, t0=t0, stdout=stdout, result=result)
        if w.format_ok_kind == "units":
            formatted = str(result.get("formatted") or result.get("text") or helper or "")
            return units_insert_ok_outcome(helper=helper, formatted=formatted, t0=t0, stdout=stdout, result=result)
        if w.format_ok_kind == "vision":
            metrics_raw = result.get("metrics")
            metrics: dict[str, Any] = metrics_raw if isinstance(metrics_raw, dict) else {}
            line_count = metrics.get("line_count")
            if line_count is None and helper == "extract_structure":
                line_count = metrics.get("block_count")
            if line_count is None:
                html = str(result.get("html") or "")
                line_count = html.count("<p") + html.count("<h") + html.count("<table")
            if helper == "extract_structure":
                table_count = metrics.get("table_count", 0)
                status_ok = _("Vision '{helper}' completed. Inserted HTML ({blocks} blocks, {tables} tables). (took {time})").format(
                    helper=helper, blocks=line_count, tables=table_count, time=formatted_time
                )
            else:
                status_ok = _("Vision '{helper}' completed. Inserted formatted HTML. (took {time})").format(
                    helper=helper, time=formatted_time
                )
            return rps_ok_outcome(status_ok, result=result, stdout=stdout)
        if w.format_ok_kind == "rows":
            return rps_ok_outcome(
                _("{domain} '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                    domain=w.id.capitalize(), helper=helper, rows=row_count or 0, time=formatted_time
                ),
                result=result,
                stdout=stdout,
            )
        return rps_ok_outcome(
            _("{domain} '{helper}' completed. (took {time})").format(
                domain=w.id.capitalize() if w.id != "text" else "Text analytics", helper=helper, time=formatted_time
            ),
            result=result,
            stdout=stdout,
        )

    def is_result(value: Any) -> bool:
        fn = _resolve_fn(w.is_result)
        return fn(value)

    return RpsDomainSpec(
        id=w.id,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        post_venv_calc_only=w.post_venv_calc_only,
    )


WIRING_TABLE: tuple[DomainWiring, ...] = (
    DomainWiring(
        id="vision",
        insert="plugin.vision.vision_egress.insert_vision_result",
        is_result="plugin.vision.vision_egress.is_vision_result",
        format_ok_kind="vision",
    ),
    DomainWiring(
        id="viz",
        insert="plugin.scripting.viz.insert_viz_result_into_doc",
        is_result="plugin.scripting.viz.is_viz_result",
        format_ok_kind="plot",
    ),
    DomainWiring(
        id="math",
        insert="plugin.scripting.symbolic.insert_symbolic_result_into_doc",
        is_result="plugin.scripting.symbolic.is_symbolic_result",
        format_ok_kind="symbolic",
    ),
    DomainWiring(
        id="units",
        insert="plugin.scripting.units.insert_units_result_into_doc",
        is_result="plugin.scripting.units.is_units_result",
        format_ok_kind="units",
    ),
    DomainWiring(
        id="text",
        insert="plugin.scripting.text_analytics.insert_text_analytics_result_into_doc",
        is_result="plugin.scripting.text_analytics.is_text_analytics_result",
        format_ok_kind="generic",
    ),
    DomainWiring(
        id="quant",
        insert="plugin.calc.quant_egress.insert_quant_result_into_calc",
        is_result="plugin.calc.quant_egress.is_quant_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
    ),
    DomainWiring(
        id="optimize",
        insert="plugin.scripting.optimize.insert_optimize_result_into_calc",
        is_result="plugin.scripting.optimize.is_optimize_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
    ),
    DomainWiring(
        id="forecast",
        insert="plugin.scripting.forecast.insert_forecast_result_into_calc",
        is_result="plugin.scripting.forecast.is_forecast_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
    ),
    DomainWiring(
        id="analysis",
        insert="plugin.calc.analysis_egress.insert_analysis_result_into_calc",
        is_result="plugin.calc.analysis_egress.is_analysis_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
    ),
)

def _rps_builder_for(wiring: DomainWiring) -> Callable[[], RpsDomainSpec]:
    return lambda: build_rps_spec(wiring)


_RPS_BUILDERS: tuple[Callable[[], RpsDomainSpec], ...] = tuple(
    _rps_builder_for(wiring) for wiring in WIRING_TABLE
)

_rps_cache: list[RpsDomainSpec] | None = None


def get_rps_domains() -> Sequence[RpsDomainSpec]:
    """Ordered RPS domain specs (cached after first call)."""
    global _rps_cache
    if _rps_cache is None:
        _rps_cache = [builder() for builder in _RPS_BUILDERS]
    return _rps_cache


# Post-venv is_result order differs slightly (symbolic before units, then plot special case, then vision, then calc domains)
# We encode post-venv order as a separate sequence of domain ids.
POST_VENV_DOMAIN_ORDER: tuple[str, ...] = (
    "math",  # symbolic
    "units",
    "text",
    "viz",
    # plot raw is special-cased in python_runner
    "vision",
    "analysis",
    "quant",
    "optimize",
    "forecast",
)


def get_rps_domain_by_id(domain_id: str) -> RpsDomainSpec | None:
    for spec in get_rps_domains():
        if spec.id == domain_id:
            return spec
    return None


def get_post_venv_domains() -> list[RpsDomainSpec]:
    """Domains that participate in post-venv result routing, in order."""
    out: list[RpsDomainSpec] = []
    for domain_id in POST_VENV_DOMAIN_ORDER:
        spec = get_rps_domain_by_id(domain_id)
        if spec is not None and spec.is_result is not None:
            out.append(spec)
    return out


_RUN_IMPORT_DATA_BINDING: dict[str, dict[str, bool]] = {
    "run_analysis": {"calc_only": True},
    "run_viz": {"calc_only": False},
    "run_quant": {"calc_only": True},
    "run_optimize": {"calc_only": True},
    "run_forecast": {"calc_only": True},
}


def script_header_needs_data_binding(code: str, *, doc: Any) -> bool:
    """True when *code* uses a trusted helper that may bind Calc sheet data."""
    import re

    if not code:
        return False
    for run_name, cfg in _RUN_IMPORT_DATA_BINDING.items():
        if not re.search(rf"\b{re.escape(run_name)}\s*\(", code):
            continue
        if cfg.get("calc_only") and not is_calc(doc):
            continue
        return True
    return False


# --- Picker domains (order in build_xdl_script_picker_state) ---


def _picker_calc_only(doc: Any) -> bool:
    try:
        return doc is not None and is_calc(doc)
    except Exception:
        return False


def get_picker_domains() -> list[PickerDomainSpec]:
    """Built-in helper sections for the script picker (lazy templates/supports)."""

    def analysis_supports(doc: Any) -> bool:
        return _picker_calc_only(doc)

    def analysis_templates() -> dict[str, str]:
        from plugin.scripting.analysis import get_analysis_script_templates

        return get_analysis_script_templates()

    def sql_supports(doc: Any) -> bool:
        return _picker_calc_only(doc)

    def sql_templates() -> dict[str, str]:
        from plugin.scripting.duckdb_sql import get_sql_script_templates

        return get_sql_script_templates()

    def vision_supports(doc: Any) -> bool:
        if doc is None:
            return False
        from plugin.vision.vision_runner import supports_vision_manual

        try:
            return supports_vision_manual(doc)
        except Exception:
            return False

    def vision_templates() -> dict[str, str]:
        from plugin.vision.vision_templates import get_vision_script_templates

        return get_vision_script_templates()

    def viz_supports(doc: Any) -> bool:
        if doc is None:
            return False
        from plugin.scripting.viz import supports_viz_manual

        try:
            return supports_viz_manual(doc)
        except Exception:
            return False

    def viz_templates() -> dict[str, str]:
        from plugin.scripting.viz import get_viz_script_templates

        return get_viz_script_templates()

    def math_supports(doc: Any) -> bool:
        if doc is None:
            return False
        from plugin.scripting.symbolic import supports_symbolic_manual

        try:
            return supports_symbolic_manual(doc)
        except Exception:
            return False

    def math_templates() -> dict[str, str]:
        from plugin.scripting.symbolic import get_math_script_templates

        return get_math_script_templates()

    def units_supports(doc: Any) -> bool:
        if doc is None:
            return False
        from plugin.scripting.units import supports_units_manual

        try:
            return supports_units_manual(doc)
        except Exception:
            return False

    def units_templates() -> dict[str, str]:
        from plugin.scripting.units import get_units_script_templates

        return get_units_script_templates()

    def quant_supports(doc: Any) -> bool:
        if doc is None:
            return False
        from plugin.scripting.quant import supports_quant_manual

        try:
            return supports_quant_manual(doc)
        except Exception:
            return False

    def quant_templates() -> dict[str, str]:
        from plugin.scripting.quant import HELPER_NAMES, get_quant_template

        return {name: t for name in HELPER_NAMES if (t := get_quant_template(name))}

    def optimize_supports(doc: Any) -> bool:
        return _picker_calc_only(doc)

    def optimize_templates() -> dict[str, str]:
        from plugin.scripting.optimize import HELPER_NAMES, get_optimize_template

        return {name: t for name in HELPER_NAMES if (t := get_optimize_template(name))}

    def forecast_supports(doc: Any) -> bool:
        return _picker_calc_only(doc)

    def forecast_templates() -> dict[str, str]:
        from plugin.scripting.forecast import HELPER_NAMES, get_forecast_template

        return {name: t for name in HELPER_NAMES if (t := get_forecast_template(name))}

    # Order: analysis, sql, vision, viz, math, units, quant, optimize, forecast
    return [
        PickerDomainSpec(
            origin=SCRIPT_ORIGIN_ANALYSIS,
            display_prefix=ANALYSIS_SCRIPT_DISPLAY_PREFIX,
            title_fn=lambda: _("Analysis Helpers"),
            supports=analysis_supports,
            templates=analysis_templates,
        ),
        PickerDomainSpec(
            origin=SCRIPT_ORIGIN_SQL,
            display_prefix=SQL_SCRIPT_DISPLAY_PREFIX,
            title_fn=lambda: _("SQL Helpers"),
            supports=sql_supports,
            templates=sql_templates,
        ),
        PickerDomainSpec(
            origin=SCRIPT_ORIGIN_VISION,
            display_prefix=VISION_SCRIPT_DISPLAY_PREFIX,
            title_fn=lambda: _("Vision Helpers"),
            supports=vision_supports,
            templates=vision_templates,
        ),
        PickerDomainSpec(
            origin=SCRIPT_ORIGIN_VIZ,
            display_prefix=VIZ_SCRIPT_DISPLAY_PREFIX,
            title_fn=lambda: _("Viz Helpers"),
            supports=viz_supports,
            templates=viz_templates,
        ),
        PickerDomainSpec(
            origin=SCRIPT_ORIGIN_MATH,
            display_prefix=MATH_SCRIPT_DISPLAY_PREFIX,
            title_fn=lambda: _("Math Helpers"),
            supports=math_supports,
            templates=math_templates,
        ),
        PickerDomainSpec(
            origin=SCRIPT_ORIGIN_UNITS,
            display_prefix=UNITS_SCRIPT_DISPLAY_PREFIX,
            title_fn=lambda: _("Units Helpers"),
            supports=units_supports,
            templates=units_templates,
        ),
        PickerDomainSpec(
            origin=SCRIPT_ORIGIN_QUANT,
            display_prefix=QUANT_SCRIPT_DISPLAY_PREFIX,
            title_fn=lambda: _("Quant Helpers"),
            supports=quant_supports,
            templates=quant_templates,
        ),
        PickerDomainSpec(
            origin=SCRIPT_ORIGIN_OPTIMIZE,
            display_prefix=OPTIMIZE_SCRIPT_DISPLAY_PREFIX,
            title_fn=lambda: _("Optimize Helpers"),
            supports=optimize_supports,
            templates=optimize_templates,
        ),
        PickerDomainSpec(
            origin=SCRIPT_ORIGIN_FORECAST,
            display_prefix=FORECAST_SCRIPT_DISPLAY_PREFIX,
            title_fn=lambda: _("Forecast Helpers"),
            supports=forecast_supports,
            templates=forecast_templates,
        ),
    ]


def picker_display_name(prefix: str, name: str) -> str:
    return f"{prefix}{name}"


def parse_picker_display_name(prefix: str, display: str) -> str | None:
    if display.startswith(prefix):
        return display[len(prefix) :]
    return None
