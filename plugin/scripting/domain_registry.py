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
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.scripting.editor_ipc import exception_traceback
from plugin.scripting.helper_domain import (
    HelperScriptMeta,
    format_elapsed_time,
    plot_insert_ok_outcome,
    rps_error_outcome,
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
    """One trusted-helper fast path (header) + optional post-venv result routing."""

    id: str
    parse_header: Callable[[str], Any | None]
    # Soft skip when not Calc (header ignored → fall through to generic venv).
    require_calc: bool = False
    # Hard fail when False (wrong app type for this header).
    supports: Callable[[Any], bool] | None = None
    unsupported_message: str = ""
    # Optional prepare: (meta) -> (params, insert_kwargs). Error outcomes use a separate error dict path.
    prepare: Callable[[Any], tuple[dict[str, Any], dict[str, Any]]] | None = None
    # Data range: never | calc_optional | calc_required | quant (required except fetch_historical_data)
    data_range_mode: str = "never"
    data_range_required_message: str = ""
    run_trusted: Callable[..., dict[str, Any]] | None = None
    insert: Callable[..., Any] | None = None
    format_ok: Callable[..., dict[str, Any]] | None = None
    is_result: Callable[[Any], bool] | None = None
    # After generic venv: only attempt is_result/insert when Calc.
    post_venv_calc_only: bool = False
    # log label when fast path raises
    fail_log_label: str = ""
    helper_failed_fallback: str = ""


def _meta_helper(meta: Any) -> str:
    return str(getattr(meta, "helper", "") or "")


def _meta_params(meta: Any) -> dict[str, Any]:
    raw = getattr(meta, "params", None)
    return dict(raw) if isinstance(raw, dict) else {}


def try_rps_fast_path(
    spec: RpsDomainSpec,
    *,
    ctx: Any,
    doc: Any,
    code: str,
    t0: float,
    resolve_data_range: Callable[[], str | None],
) -> dict[str, Any] | None:
    """Run one domain's header fast path, or return None if header does not match / soft-skip."""
    meta = spec.parse_header(code)
    if meta is None:
        return None
    if spec.require_calc and not is_calc(doc):
        return None

    if spec.supports is not None and not spec.supports(doc):
        return {"ok": False, "message": spec.unsupported_message or _("Unsupported document type.")}

    insert_kwargs: dict[str, Any] = {}
    params = _meta_params(meta)
    if spec.prepare is not None:
        params, insert_kwargs = spec.prepare(meta)

    data_range: str | None = None
    mode = spec.data_range_mode
    if mode == "calc_optional":
        data_range = resolve_data_range() if is_calc(doc) else None
    elif mode == "calc_required_when_calc":
        # Viz: Writer needs no range; Calc requires selection / Data field.
        if is_calc(doc):
            data_range = resolve_data_range()
            if not data_range:
                return {
                    "ok": False,
                    "message": spec.data_range_required_message
                    or _("Helper requires a data range. Select cells or enter a range in the Data field."),
                }
    elif mode == "calc_required":
        data_range = resolve_data_range()
        if not data_range:
            return {
                "ok": False,
                "message": spec.data_range_required_message
                or _("Helper requires a data range. Select cells or enter a range in the Data field."),
            }
    elif mode == "quant":
        data_range = resolve_data_range()
        if not data_range and _meta_helper(meta) != "fetch_historical_data":
            return {
                "ok": False,
                "message": spec.data_range_required_message
                or _("Quant helper requires a data range. Select cells or enter a range in the Data field."),
            }

    assert spec.run_trusted is not None and spec.insert is not None and spec.format_ok is not None
    label = spec.fail_log_label or spec.id
    try:
        result = spec.run_trusted(
            ctx,
            doc,
            helper=_meta_helper(meta),
            params=params,
            data_range=data_range,
        )
    except ToolExecutionError as exc:
        return rps_error_outcome(str(exc), t0=t0)
    except Exception as e:
        log.exception("execute_and_insert_result %s fast path failed", label)
        return rps_error_outcome(str(e), t0=t0, traceback=exception_traceback(e))

    if result.get("status") == "error":
        message = str(result.get("message") or spec.helper_failed_fallback or _("Helper failed."))
        return rps_error_outcome(message, t0=t0)

    try:
        row_count = spec.insert(ctx, doc, result, **insert_kwargs)
    except Exception as e:
        return rps_insert_failed_outcome(e, t0=t0)

    return spec.format_ok(meta=meta, result=result, t0=t0, row_count=row_count, stdout=None)


def try_rps_post_venv(
    spec: RpsDomainSpec,
    *,
    ctx: Any,
    doc: Any,
    result_data: Any,
    t0: float,
    stdout: str | None,
) -> dict[str, Any] | None:
    """Route a generic venv result through domain is_result + insert, or None."""
    if spec.is_result is None or spec.insert is None or spec.format_ok is None:
        return None
    if spec.post_venv_calc_only and not is_calc(doc):
        return None
    if not spec.is_result(result_data):
        return None
    try:
        row_count = spec.insert(ctx, doc, result_data)
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
    parse_header: str
    run_trusted: str
    insert: str
    is_result: str
    supports: str | None = None
    format_ok_kind: str = "generic"  # generic | plot | symbolic | units | vision | rows
    data_range_mode: str = "never"
    require_calc: bool = False
    post_venv_calc_only: bool = False
    unsupported_message: Callable[[], str] | None = None
    data_range_required_message: Callable[[], str] | None = None
    fail_log_label: str = ""
    helper_failed_fallback: Callable[[], str] | None = None


def _resolve_fn(path: str | None) -> Any:
    if not path:
        return None
    import importlib
    mod_name, attr_name = path.rsplit(".", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, attr_name)


def build_rps_spec(w: DomainWiring) -> RpsDomainSpec:
    def parse(code: str) -> Any:
        return _resolve_fn(w.parse_header)(code)

    def supports(doc: Any) -> bool:
        if w.supports:
            return _resolve_fn(w.supports)(doc)
        return True

    def run(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any], data_range: str | None = None) -> dict[str, Any]:
        fn = _resolve_fn(w.run_trusted)
        import inspect
        sig = inspect.signature(fn)
        if "data_range" in sig.parameters:
            return fn(ctx, doc, helper=helper, params=params, data_range=data_range)
        return fn(ctx, doc, helper=helper, params=params)

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

    def prepare(meta: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if w.id == "units":
            from plugin.scripting.units import split_helper_params
            clean, output_style = split_helper_params(_meta_params(meta))
            return clean, {"output_style": output_style}
        if w.id == "vision":
            return _meta_params(meta), {"params": _meta_params(meta)}
        return _meta_params(meta), {}

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        helper = _meta_helper(meta) or str(result.get("helper") or w.id)
        formatted_time = format_elapsed_time(time.perf_counter() - t0)

        if w.format_ok_kind == "plot":
            title = str(result.get("title") or helper or "Plot")
            return plot_insert_ok_outcome(helper=helper, title=title, t0=t0, stdout=stdout, result=result)
        elif w.format_ok_kind == "symbolic":
            latex = str(result.get("latex") or result.get("text") or helper or "")
            return symbolic_insert_ok_outcome(helper=helper, latex=latex, t0=t0, stdout=stdout, result=result)
        elif w.format_ok_kind == "units":
            formatted = str(result.get("formatted") or result.get("text") or helper or "")
            return units_insert_ok_outcome(helper=helper, formatted=formatted, t0=t0, stdout=stdout, result=result)
        elif w.format_ok_kind == "vision":
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
        elif w.format_ok_kind == "rows":
            return rps_ok_outcome(
                _("{domain} '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                    domain=w.id.capitalize(), helper=helper, rows=row_count or 0, time=formatted_time
                ),
                result=result,
                stdout=stdout,
            )
        else:
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
        parse_header=parse,
        require_calc=w.require_calc,
        supports=supports if w.supports else None,
        unsupported_message=w.unsupported_message() if w.unsupported_message else "",
        prepare=prepare if (w.id in ("units", "vision")) else None,
        data_range_mode=w.data_range_mode,
        data_range_required_message=w.data_range_required_message() if w.data_range_required_message else "",
        run_trusted=run,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        post_venv_calc_only=w.post_venv_calc_only,
        fail_log_label=w.fail_log_label,
        helper_failed_fallback=w.helper_failed_fallback() if w.helper_failed_fallback else "",
    )


WIRING_TABLE: tuple[DomainWiring, ...] = (
    DomainWiring(
        id="vision",
        parse_header="plugin.vision.vision_templates.parse_vision_script_header",
        run_trusted="plugin.vision.vision_runner.run_trusted_vision",
        insert="plugin.vision.vision_egress.insert_vision_result",
        is_result="plugin.vision.vision_egress.is_vision_result",
        supports="plugin.vision.vision_runner.supports_vision_manual",
        format_ok_kind="vision",
        unsupported_message=lambda: _("Vision helpers require a Writer or Calc document."),
        fail_log_label="vision",
        helper_failed_fallback=lambda: _("Vision helper failed."),
    ),
    DomainWiring(
        id="viz",
        parse_header="plugin.scripting.viz.parse_viz_script_header",
        run_trusted="plugin.scripting.viz.run_trusted_viz",
        insert="plugin.scripting.viz.insert_viz_result_into_doc",
        is_result="plugin.scripting.viz.is_viz_result",
        supports="plugin.scripting.viz.supports_viz_manual",
        format_ok_kind="plot",
        data_range_mode="calc_required_when_calc",
        unsupported_message=lambda: _("Viz helpers require a Writer or Calc document."),
        data_range_required_message=lambda: _("Viz helper requires a data range. Select cells or enter a range in the Data field."),
        fail_log_label="viz",
        helper_failed_fallback=lambda: _("Viz helper failed."),
    ),
    DomainWiring(
        id="math",
        parse_header="plugin.scripting.symbolic.parse_math_script_header",
        run_trusted="plugin.scripting.symbolic.run_trusted_symbolic",
        insert="plugin.scripting.symbolic.insert_symbolic_result_into_doc",
        is_result="plugin.scripting.symbolic.is_symbolic_result",
        supports="plugin.scripting.symbolic.supports_symbolic_manual",
        format_ok_kind="symbolic",
        unsupported_message=lambda: _("Math helpers require a Writer or Calc document."),
        fail_log_label="math",
        helper_failed_fallback=lambda: _("Math helper failed."),
    ),
    DomainWiring(
        id="units",
        parse_header="plugin.scripting.units.parse_units_script_header",
        run_trusted="plugin.scripting.units.run_trusted_units",
        insert="plugin.scripting.units.insert_units_result_into_doc",
        is_result="plugin.scripting.units.is_units_result",
        supports="plugin.scripting.units.supports_units_manual",
        format_ok_kind="units",
        unsupported_message=lambda: _("Units helpers require a Writer or Calc document."),
        fail_log_label="units",
        helper_failed_fallback=lambda: _("Units helper failed."),
    ),
    DomainWiring(
        id="text",
        parse_header="plugin.scripting.text_analytics.parse_text_analytics_script_header",
        run_trusted="plugin.scripting.text_analytics.run_trusted_text_analytics",
        insert="plugin.scripting.text_analytics.insert_text_analytics_result_into_doc",
        is_result="plugin.scripting.text_analytics.is_text_analytics_result",
        supports="plugin.scripting.text_analytics.supports_text_analytics_manual",
        format_ok_kind="generic",
        unsupported_message=lambda: _("Text analytics helpers require a Writer document."),
        fail_log_label="text_analytics",
        helper_failed_fallback=lambda: _("Text analytics helper failed."),
    ),
    DomainWiring(
        id="quant",
        parse_header="plugin.scripting.quant.parse_quant_script_header",
        run_trusted="plugin.scripting.quant.run_trusted_quant",
        insert="plugin.calc.quant_egress.insert_quant_result_into_calc",
        is_result="plugin.calc.quant_egress.is_quant_result",
        format_ok_kind="rows",
        data_range_mode="quant",
        require_calc=True,
        post_venv_calc_only=True,
        data_range_required_message=lambda: _("Quant helper requires a data range. Select cells or enter a range in the Data field."),
        fail_log_label="quant",
        helper_failed_fallback=lambda: _("Quant failed."),
    ),
    DomainWiring(
        id="optimize",
        parse_header="plugin.scripting.optimize.parse_optimize_script_header",
        run_trusted="plugin.scripting.optimize.run_trusted_optimize",
        insert="plugin.scripting.optimize.insert_optimize_result_into_calc",
        is_result="plugin.scripting.optimize.is_optimize_result",
        format_ok_kind="rows",
        data_range_mode="calc_required",
        require_calc=True,
        post_venv_calc_only=True,
        data_range_required_message=lambda: _("Optimization helper requires a data range. Select cells or enter a range in the Data field."),
        fail_log_label="optimize",
        helper_failed_fallback=lambda: _("Optimization failed."),
    ),
    DomainWiring(
        id="forecast",
        parse_header="plugin.scripting.forecast.parse_forecast_script_header",
        run_trusted="plugin.scripting.forecast.run_trusted_forecast",
        insert="plugin.scripting.forecast.insert_forecast_result_into_calc",
        is_result="plugin.scripting.forecast.is_forecast_result",
        format_ok_kind="rows",
        data_range_mode="calc_required",
        require_calc=True,
        post_venv_calc_only=True,
        data_range_required_message=lambda: _("Forecast helper requires a data range. Select cells or enter a range in the Data field."),
        fail_log_label="forecast",
        helper_failed_fallback=lambda: _("Forecast failed."),
    ),
    DomainWiring(
        id="analysis",
        parse_header="plugin.scripting.analysis.parse_analysis_script_header",
        run_trusted="plugin.calc.analysis_runner.run_trusted_analysis",
        insert="plugin.calc.analysis_egress.insert_analysis_result_into_calc",
        is_result="plugin.calc.analysis_egress.is_analysis_result",
        format_ok_kind="rows",
        data_range_mode="calc_required",
        require_calc=True,
        post_venv_calc_only=True,
        data_range_required_message=lambda: _("Analysis helper requires a data range. Select cells or enter a range in the Data field."),
        fail_log_label="analysis",
        helper_failed_fallback=lambda: _("Analysis failed."),
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


def script_header_needs_data_binding(code: str, *, doc: Any) -> bool:
    """True when *code* contains a trusted header for a domain that may use Calc data binding."""
    for spec in get_rps_domains():
        if spec.data_range_mode == "never":
            continue
        if spec.parse_header(code) is None:
            continue
        if spec.require_calc and not is_calc(doc):
            continue
        if spec.supports is not None:
            try:
                if not spec.supports(doc):
                    continue
            except Exception:
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
