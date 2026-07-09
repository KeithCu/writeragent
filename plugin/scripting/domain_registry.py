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
    if not isinstance(result_data, dict) and not spec.is_result(result_data):
        # Most domains need a dict; is_result already checks type.
        pass
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
    meta = type("M", (), {"helper": helper, "params": {}})()
    return spec.format_ok(meta=meta, result=result_data, t0=t0, row_count=row_count, stdout=stdout)


# --- Domain adapters (lazy imports) ---


def _vision_spec() -> RpsDomainSpec:
    def parse(code: str) -> Any:
        from plugin.vision.vision_templates import parse_vision_script_header

        return parse_vision_script_header(code)

    def supports(doc: Any) -> bool:
        from plugin.vision.vision_runner import supports_vision_manual

        return supports_vision_manual(doc)

    def run(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any], data_range: str | None = None) -> dict[str, Any]:
        from plugin.vision.vision_runner import run_trusted_vision

        return run_trusted_vision(ctx, doc, helper=helper, params=params)

    def insert(ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> None:
        from plugin.vision.vision_egress import insert_vision_result

        # Header path passes params for reviewable edits; post-venv may omit.
        params = kwargs.get("params")
        if params is not None:
            insert_vision_result(ctx, doc, result, params=params)
        else:
            insert_vision_result(ctx, doc, result)

    def prepare(meta: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return _meta_params(meta), {"params": _meta_params(meta)}

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        metrics_raw = result.get("metrics")
        metrics: dict[str, Any] = metrics_raw if isinstance(metrics_raw, dict) else {}
        line_count = metrics.get("line_count")
        helper = _meta_helper(meta) or str(result.get("helper") or "vision")
        if line_count is None and helper == "extract_structure":
            line_count = metrics.get("block_count")
        if line_count is None:
            html = str(result.get("html") or "")
            line_count = html.count("<p") + html.count("<h") + html.count("<table")
        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        if helper == "extract_structure":
            table_count = metrics.get("table_count", 0)
            status_ok = _("Vision '{helper}' completed. Inserted HTML ({blocks} blocks, {tables} tables). (took {time})").format(
                helper=helper,
                blocks=line_count,
                tables=table_count,
                time=formatted_time,
            )
        else:
            status_ok = _("Vision '{helper}' completed. Inserted formatted HTML. (took {time})").format(
                helper=helper,
                time=formatted_time,
            )
        return rps_ok_outcome(status_ok, result=result, stdout=stdout)

    def is_result(value: Any) -> bool:
        from plugin.vision.vision_egress import is_vision_result

        return isinstance(value, dict) and is_vision_result(value)

    return RpsDomainSpec(
        id="vision",
        parse_header=parse,
        supports=supports,
        unsupported_message=_("Vision helpers require a Writer or Calc document."),
        prepare=prepare,
        run_trusted=run,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        fail_log_label="vision",
        helper_failed_fallback=_("Vision helper failed."),
    )


def _viz_spec() -> RpsDomainSpec:
    def parse(code: str) -> Any:
        from plugin.scripting.viz import parse_viz_script_header

        return parse_viz_script_header(code)

    def supports(doc: Any) -> bool:
        from plugin.scripting.viz import supports_viz_manual

        return supports_viz_manual(doc)

    def run(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any], data_range: str | None = None) -> dict[str, Any]:
        from plugin.scripting.viz import run_trusted_viz

        return run_trusted_viz(ctx, doc, helper=helper, params=params, data_range=data_range)

    def insert(ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> None:
        from plugin.scripting.viz import insert_viz_result_into_doc

        insert_viz_result_into_doc(ctx, doc, result)

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        title = str(result.get("title") or _meta_helper(meta) or "Plot")
        helper = _meta_helper(meta) or str(result.get("helper") or "")
        return plot_insert_ok_outcome(helper=helper, title=title, t0=t0, stdout=stdout, result=result)

    def is_result(value: Any) -> bool:
        from plugin.scripting.viz import is_viz_result

        return is_viz_result(value)

    return RpsDomainSpec(
        id="viz",
        parse_header=parse,
        supports=supports,
        unsupported_message=_("Viz helpers require a Writer or Calc document."),
        data_range_mode="calc_required_when_calc",
        data_range_required_message=_(
            "Viz helper requires a data range. Select cells or enter a range in the Data field."
        ),
        run_trusted=run,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        fail_log_label="viz",
        helper_failed_fallback=_("Viz helper failed."),
    )


def _math_spec() -> RpsDomainSpec:
    def parse(code: str) -> Any:
        from plugin.scripting.symbolic import parse_math_script_header

        return parse_math_script_header(code)

    def supports(doc: Any) -> bool:
        from plugin.scripting.symbolic import supports_symbolic_manual

        return supports_symbolic_manual(doc)

    def run(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any], data_range: str | None = None) -> dict[str, Any]:
        from plugin.scripting.symbolic import run_trusted_symbolic

        return run_trusted_symbolic(ctx, doc, helper=helper, params=params)

    def insert(ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> None:
        from plugin.scripting.symbolic import insert_symbolic_result_into_doc

        insert_symbolic_result_into_doc(ctx, doc, result)

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        latex = str(result.get("latex") or result.get("text") or _meta_helper(meta) or "")
        helper = _meta_helper(meta) or str(result.get("helper") or "")
        return symbolic_insert_ok_outcome(helper=helper, latex=latex, t0=t0, stdout=stdout, result=result)

    def is_result(value: Any) -> bool:
        from plugin.scripting.symbolic import is_symbolic_result

        return is_symbolic_result(value)

    return RpsDomainSpec(
        id="math",
        parse_header=parse,
        supports=supports,
        unsupported_message=_("Math helpers require a Writer or Calc document."),
        run_trusted=run,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        fail_log_label="math",
        helper_failed_fallback=_("Math helper failed."),
    )


def _units_spec() -> RpsDomainSpec:
    def parse(code: str) -> Any:
        from plugin.scripting.units import parse_units_script_header

        return parse_units_script_header(code)

    def supports(doc: Any) -> bool:
        from plugin.scripting.units import supports_units_manual

        return supports_units_manual(doc)

    def prepare(meta: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        from plugin.scripting.units import split_helper_params

        clean, output_style = split_helper_params(_meta_params(meta))
        return clean, {"output_style": output_style}

    def run(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any], data_range: str | None = None) -> dict[str, Any]:
        from plugin.scripting.units import run_trusted_units

        return run_trusted_units(ctx, doc, helper=helper, params=params)

    def insert(ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> None:
        from plugin.scripting.units import insert_units_result_into_doc

        insert_units_result_into_doc(ctx, doc, result, output_style=kwargs.get("output_style"))

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        formatted = str(result.get("formatted") or result.get("text") or _meta_helper(meta) or result.get("helper") or "")
        helper = _meta_helper(meta) or str(result.get("helper") or "")
        return units_insert_ok_outcome(helper=helper, formatted=formatted, t0=t0, stdout=stdout, result=result)

    def is_result(value: Any) -> bool:
        from plugin.scripting.units import is_units_result

        return is_units_result(value)

    return RpsDomainSpec(
        id="units",
        parse_header=parse,
        supports=supports,
        unsupported_message=_("Units helpers require a Writer or Calc document."),
        prepare=prepare,
        run_trusted=run,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        fail_log_label="units",
        helper_failed_fallback=_("Units helper failed."),
    )


def _text_spec() -> RpsDomainSpec:
    def parse(code: str) -> Any:
        from plugin.scripting.text_analytics import parse_text_analytics_script_header

        return parse_text_analytics_script_header(code)

    def supports(doc: Any) -> bool:
        from plugin.scripting.text_analytics import supports_text_analytics_manual

        return supports_text_analytics_manual(doc)

    def run(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any], data_range: str | None = None) -> dict[str, Any]:
        from plugin.scripting.text_analytics import run_trusted_text_analytics

        return run_trusted_text_analytics(ctx, doc, helper=helper, params=params)

    def insert(ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> None:
        from plugin.scripting.text_analytics import insert_text_analytics_result_into_doc

        insert_text_analytics_result_into_doc(ctx, doc, result)

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        title = _meta_helper(meta) or str(result.get("helper") or result.get("result", {}).get("meta", {}).get("model") or "text")
        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        return rps_ok_outcome(
            _("Text analytics '{helper}' completed. (took {time})").format(helper=title, time=formatted_time),
            result=result,
            stdout=stdout,
        )

    def is_result(value: Any) -> bool:
        from plugin.scripting.text_analytics import is_text_analytics_result

        return is_text_analytics_result(value)

    return RpsDomainSpec(
        id="text",
        parse_header=parse,
        supports=supports,
        unsupported_message=_("Text analytics helpers require a Writer document."),
        run_trusted=run,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        fail_log_label="text_analytics",
        helper_failed_fallback=_("Text analytics helper failed."),
    )


def _quant_spec() -> RpsDomainSpec:
    def parse(code: str) -> Any:
        from plugin.scripting.quant import parse_quant_script_header

        return parse_quant_script_header(code)

    def run(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any], data_range: str | None = None) -> dict[str, Any]:
        from plugin.scripting.quant import run_trusted_quant

        return run_trusted_quant(ctx, doc, helper=helper, params=params, data_range=data_range)

    def insert(ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> int:
        from plugin.calc.quant_egress import insert_quant_result_into_calc

        return int(insert_quant_result_into_calc(doc, ctx, result) or 0)

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        helper = _meta_helper(meta) or str(result.get("helper") or "quant")
        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        return rps_ok_outcome(
            _("Quant '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                helper=helper, rows=row_count or 0, time=formatted_time
            ),
            result=result,
            stdout=stdout,
        )

    def is_result(value: Any) -> bool:
        from plugin.calc.quant_egress import is_quant_result

        return isinstance(value, dict) and is_quant_result(value)

    return RpsDomainSpec(
        id="quant",
        parse_header=parse,
        require_calc=True,
        data_range_mode="quant",
        data_range_required_message=_(
            "Quant helper requires a data range. Select cells or enter a range in the Data field."
        ),
        run_trusted=run,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        post_venv_calc_only=True,
        fail_log_label="quant",
        helper_failed_fallback=_("Quant failed."),
    )


def _optimize_spec() -> RpsDomainSpec:
    def parse(code: str) -> Any:
        from plugin.scripting.optimize import parse_optimize_script_header

        return parse_optimize_script_header(code)

    def run(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any], data_range: str | None = None) -> dict[str, Any]:
        from plugin.scripting.optimize import run_trusted_optimize

        return run_trusted_optimize(ctx, doc, helper=helper, params=params, data_range=data_range)

    def insert(ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> int:
        from plugin.scripting.optimize import insert_optimize_result_into_calc

        return int(insert_optimize_result_into_calc(doc, ctx, result) or 0)

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        helper = _meta_helper(meta) or str(result.get("helper") or "optimize")
        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        return rps_ok_outcome(
            _("Optimize '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                helper=helper, rows=row_count or 0, time=formatted_time
            ),
            result=result,
            stdout=stdout,
        )

    def is_result(value: Any) -> bool:
        from plugin.scripting.optimize import is_optimize_result

        return isinstance(value, dict) and is_optimize_result(value)

    return RpsDomainSpec(
        id="optimize",
        parse_header=parse,
        require_calc=True,
        data_range_mode="calc_required",
        data_range_required_message=_(
            "Optimization helper requires a data range. Select cells or enter a range in the Data field."
        ),
        run_trusted=run,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        post_venv_calc_only=True,
        fail_log_label="optimize",
        helper_failed_fallback=_("Optimization failed."),
    )


def _forecast_spec() -> RpsDomainSpec:
    def parse(code: str) -> Any:
        from plugin.scripting.forecast import parse_forecast_script_header

        return parse_forecast_script_header(code)

    def run(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any], data_range: str | None = None) -> dict[str, Any]:
        from plugin.scripting.forecast import run_trusted_forecast

        return run_trusted_forecast(ctx, doc, helper=helper, params=params, data_range=data_range)

    def insert(ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> int:
        from plugin.scripting.forecast import insert_forecast_result_into_calc

        return int(insert_forecast_result_into_calc(doc, ctx, result) or 0)

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        helper = _meta_helper(meta) or str(result.get("helper") or "forecast")
        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        return rps_ok_outcome(
            _("Forecast '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                helper=helper, rows=row_count or 0, time=formatted_time
            ),
            result=result,
            stdout=stdout,
        )

    def is_result(value: Any) -> bool:
        from plugin.scripting.forecast import is_forecast_result

        return isinstance(value, dict) and is_forecast_result(value)

    return RpsDomainSpec(
        id="forecast",
        parse_header=parse,
        require_calc=True,
        data_range_mode="calc_required",
        data_range_required_message=_(
            "Forecast helper requires a data range. Select cells or enter a range in the Data field."
        ),
        run_trusted=run,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        post_venv_calc_only=True,
        fail_log_label="forecast",
        helper_failed_fallback=_("Forecast failed."),
    )


def _analysis_spec() -> RpsDomainSpec:
    def parse(code: str) -> Any:
        from plugin.scripting.analysis import parse_analysis_script_header

        return parse_analysis_script_header(code)

    def run(ctx: Any, doc: Any, *, helper: str, params: dict[str, Any], data_range: str | None = None) -> dict[str, Any]:
        from plugin.calc.analysis_runner import run_trusted_analysis

        return run_trusted_analysis(ctx, doc, helper=helper, params=params, data_range=data_range)

    def insert(ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> int:
        from plugin.calc.analysis_egress import insert_analysis_result_into_calc

        return int(insert_analysis_result_into_calc(doc, ctx, result) or 0)

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        helper = _meta_helper(meta) or str(result.get("helper") or "analysis")
        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        return rps_ok_outcome(
            _("Analysis '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                helper=helper, rows=row_count or 0, time=formatted_time
            ),
            result=result,
            stdout=stdout,
        )

    def is_result(value: Any) -> bool:
        from plugin.calc.analysis_egress import is_analysis_result

        return isinstance(value, dict) and is_analysis_result(value)

    return RpsDomainSpec(
        id="analysis",
        parse_header=parse,
        require_calc=True,
        data_range_mode="calc_required",
        data_range_required_message=_(
            "Analysis helper requires a data range. Select cells or enter a range in the Data field."
        ),
        run_trusted=run,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        post_venv_calc_only=True,
        fail_log_label="analysis",
        helper_failed_fallback=_("Analysis failed."),
    )


# Fast-path order: vision → viz → math → units → text → quant → optimize → forecast → analysis
_RPS_BUILDERS: tuple[Callable[[], RpsDomainSpec], ...] = (
    _vision_spec,
    _viz_spec,
    _math_spec,
    _units_spec,
    _text_spec,
    _quant_spec,
    _optimize_spec,
    _forecast_spec,
    _analysis_spec,
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
