# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ordered registry of trusted helper domains for RPS and the script picker.

Domain compute / egress stay in domain modules. Callables use lazy imports to avoid cycles.

Calc-only inserters are ``(doc, ctx, result)`` (same as ``insert_tabular_result_into_calc``).
Writer/Draw inserters are ``(ctx, doc, result)``. ``DomainWiring.insert`` keeps that split.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from plugin.framework.i18n import N_, _
from plugin.scripting.helper_domain import (
    format_elapsed_time,
    plot_insert_ok_outcome,
    preview_insert_ok_outcome,
    rps_ok_outcome,
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



def is_reserved_script_name(name: str) -> bool:
    """True when *name* begins with any reserved UI prefix (e.g. '[Doc] ', '[Vision] ')."""
    return any(name.startswith(p) for p in RESERVED_SCRIPT_PREFIXES)


FormatOkKind = Literal["generic", "plot", "symbolic", "units", "vision", "rows"]


def _resolve_module_attr(target: str) -> Any:
    import importlib

    mod_name, attr_name = target.rsplit(":", 1)
    return getattr(importlib.import_module(mod_name), attr_name)


# Opening block tags only. A substring count of "<p" / "<h" also matched
# <pre>, <param>, <html>, <head>, <header>, and <hr>, so the status line
# over-counted structure blocks when metrics omitted line_count.
_VISION_BLOCK_OPEN_TAG = re.compile(r"<(?:p|h[1-6]|table)\b", re.IGNORECASE)


def _vision_html_block_count(html: str) -> int:
    """Count ``<p>``, ``<h1>``–``<h6>``, and ``<table>`` start tags in vision HTML."""
    return len(_VISION_BLOCK_OPEN_TAG.findall(html))


# Three tables on purpose: WIRING_TABLE (RPS insert order), POST_VENV_DOMAIN_ORDER
# (is_result scan order), PICKER_WIRING (script-picker UI). Do not collapse them
# or add a fourth registry. Drift is caught by test_domain_registry: same members
# for wiring vs post-venv; picker is not 1:1 (text is wiring-only); a
# post_venv_calc_only domain's picker supports is "plugin.doc.doc_type:is_calc".
@dataclass(frozen=True)
class DomainWiring:
    id: str
    insert_ref: str  # module.path:attr
    is_result_ref: str  # module.path:attr
    display_label: str
    format_ok_kind: FormatOkKind = "generic"
    post_venv_calc_only: bool = False
    insert_kwargs_from_code: str | None = None

    def insert(self, ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> Any:
        fn = _resolve_module_attr(self.insert_ref)
        # Sheet egress is (doc, uno_ctx, result). Calling it as (ctx, doc, result)
        # passed the component context into calc_anchor_from_selection, which then
        # failed and the Run Python Script dialog reported an insert error.
        args = (doc, ctx) if self.post_venv_calc_only else (ctx, doc)
        ret = fn(*args, result, **kwargs)
        # int(ret) raises when an inserter returns a non-integer after a
        # successful insert, and the UI shows "Failed to insert result".
        # Return ret only when it is already an int and not a bool, else None.
        if isinstance(ret, int) and not isinstance(ret, bool):
            return ret
        return None

    def format_ok(self, *, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        helper = str(getattr(meta, "helper", "") or "") or str(result.get("helper") or self.id)

        if self.format_ok_kind == "plot":
            title = str(result.get("title") or helper or "Plot")
            return plot_insert_ok_outcome(helper=helper, title=title, t0=t0, stdout=stdout, result=result)
        if self.format_ok_kind == "symbolic":
            latex = str(result.get("latex") or result.get("text") or helper or "")
            return preview_insert_ok_outcome(domain_label=self.display_label, helper=helper, preview_text=latex, t0=t0, stdout=stdout, result=result)
        if self.format_ok_kind == "units":
            from plugin.scripting.units import units_display_text

            formatted = units_display_text(result) or helper
            return preview_insert_ok_outcome(domain_label=self.display_label, helper=helper, preview_text=formatted, t0=t0, stdout=stdout, result=result)

        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        if self.format_ok_kind == "vision":
            metrics_raw = result.get("metrics")
            metrics: dict[str, Any] = metrics_raw if isinstance(metrics_raw, dict) else {}
            line_count = metrics.get("line_count")
            if line_count is None and helper == "extract_structure":
                line_count = metrics.get("block_count")
            if line_count is None:
                line_count = _vision_html_block_count(str(result.get("html") or ""))
            if helper == "extract_structure":
                table_count = metrics.get("table_count", 0)
                status_ok = _("Vision '{helper}' completed. Inserted HTML ({blocks} blocks, {tables} tables). (took {time})").format(
                    helper=helper, blocks=line_count, tables=table_count, time=formatted_time
                )
            else:
                status_ok = _("Vision '{helper}' completed. Inserted formatted HTML. (took {time})").format(
                    helper=helper, time=formatted_time
                )
        elif self.format_ok_kind == "rows":
            status_ok = _("{domain} '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                domain=self.display_label, helper=helper, rows=row_count or 0, time=formatted_time
            )
        else:
            status_ok = _("{domain} '{helper}' completed. (took {time})").format(
                domain=self.display_label, helper=helper, time=formatted_time
            )
        return rps_ok_outcome(status_ok, result=result, stdout=stdout)

    def is_result(self, value: Any) -> bool:
        try:
            fn = _resolve_module_attr(self.is_result_ref)
        except ImportError as exc:
            # Bundles that omit a domain module (LibrePy drops duckdb_sql) raise
            # ImportError when resolving is_result. That aborted routing of a
            # plain Calc result. An unresolvable module means "not this domain".
            log.debug("Domain %s is_result module unavailable: %s", self.id, exc)
            return False
        return fn(value)

    def insert_kwargs(self, ctx: Any, code: str) -> dict[str, Any]:
        ref = self.insert_kwargs_from_code
        if not ref:
            return {}
        return _resolve_module_attr(ref)(ctx, code)


WIRING_TABLE: tuple[DomainWiring, ...] = (
    DomainWiring(
        id="vision",
        insert_ref="plugin.vision.vision_egress:insert_vision_result",
        is_result_ref="plugin.vision.vision_egress:is_vision_result",
        format_ok_kind="vision",
        display_label="Vision",
        insert_kwargs_from_code="plugin.vision.vision_runner:extract_vision_insert_kwargs",
    ),
    DomainWiring(
        id="viz",
        insert_ref="plugin.scripting.viz:insert_viz_result_into_doc",
        is_result_ref="plugin.scripting.viz:is_viz_result",
        format_ok_kind="plot",
        display_label="Viz",
    ),
    DomainWiring(
        id="math",
        insert_ref="plugin.scripting.symbolic:insert_symbolic_result_into_doc",
        is_result_ref="plugin.scripting.symbolic:is_symbolic_result",
        format_ok_kind="symbolic",
        display_label="Math",
    ),
    DomainWiring(
        id="units",
        insert_ref="plugin.scripting.units:insert_units_result_into_doc",
        is_result_ref="plugin.scripting.units:is_units_result",
        format_ok_kind="units",
        display_label="Units",
        insert_kwargs_from_code="plugin.scripting.units:extract_units_insert_kwargs",
    ),
    DomainWiring(
        id="text",
        insert_ref="plugin.scripting.text_analytics:insert_text_analytics_result_into_doc",
        is_result_ref="plugin.scripting.text_analytics:is_text_analytics_result",
        format_ok_kind="generic",
        display_label="Text analytics",
    ),
    DomainWiring(
        id="quant",
        insert_ref="plugin.calc.quant_egress:insert_quant_result_into_calc",
        is_result_ref="plugin.calc.quant_egress:is_quant_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        display_label="Quant",
    ),
    DomainWiring(
        id="optimize",
        insert_ref="plugin.scripting.optimize:insert_optimize_result_into_calc",
        is_result_ref="plugin.scripting.optimize:is_optimize_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        display_label="Optimize",
    ),
    DomainWiring(
        id="forecast",
        insert_ref="plugin.scripting.forecast:insert_forecast_result_into_calc",
        is_result_ref="plugin.scripting.forecast:is_forecast_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        display_label="Forecast",
    ),
    DomainWiring(
        id="analysis",
        insert_ref="plugin.calc.analysis_egress:insert_analysis_result_into_calc",
        is_result_ref="plugin.calc.analysis_egress:is_analysis_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        display_label="Analysis",
    ),
    DomainWiring(
        id="sql",
        insert_ref="plugin.scripting.duckdb_sql:insert_sql_result_into_calc",
        is_result_ref="plugin.scripting.duckdb_sql:is_sql_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        display_label="SQL",
    ),
)


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
    "sql",
)


def get_post_venv_domains() -> tuple[DomainWiring, ...]:
    """Domains that participate in post-venv result routing, in order."""
    by_id = {wiring.id: wiring for wiring in WIRING_TABLE}
    return tuple(by_id[domain_id] for domain_id in POST_VENV_DOMAIN_ORDER)


# --- Picker domains (order in build_xdl_script_picker_state) ---


@dataclass(frozen=True)
class PickerWiring:
    """Declarative picker section: supports/templates resolved lazily via module:attr strings."""

    origin: str
    display_prefix: str
    title: str
    supports_ref: str  # "module.path:attr" (e.g. "plugin.doc.doc_type:is_calc")
    templates_ref: str  # "module.path:attr" returning dict[str, str]

    def supports(self, doc: Any) -> bool:
        # None must not reach is_calc: @main_thread_only runs before the None return.
        if doc is None:
            return False
        try:
            return bool(_resolve_module_attr(self.supports_ref)(doc))
        except Exception:
            log.debug("script picker supports check failed for %s", self.supports_ref, exc_info=True)
            return False

    def templates(self) -> dict[str, str]:
        return dict(_resolve_module_attr(self.templates_ref)())

    def title_fn(self) -> str:
        return _(self.title)


PICKER_WIRING: tuple[PickerWiring, ...] = (
    PickerWiring(
        origin=SCRIPT_ORIGIN_VISION,
        display_prefix=VISION_SCRIPT_DISPLAY_PREFIX,
        title=N_("Vision Helpers"),
        supports_ref="plugin.vision.vision_runner:supports_vision_manual",
        templates_ref="plugin.vision.vision_templates:get_vision_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_MATH,
        display_prefix=MATH_SCRIPT_DISPLAY_PREFIX,
        title=N_("Math Helpers"),
        supports_ref="plugin.scripting.helper_domain:supports_calc_or_writer_manual",
        templates_ref="plugin.scripting.symbolic:get_math_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_UNITS,
        display_prefix=UNITS_SCRIPT_DISPLAY_PREFIX,
        title=N_("Units Helpers"),
        supports_ref="plugin.scripting.helper_domain:supports_calc_or_writer_manual",
        templates_ref="plugin.scripting.units:get_units_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_ANALYSIS,
        display_prefix=ANALYSIS_SCRIPT_DISPLAY_PREFIX,
        title=N_("Analysis Helpers"),
        supports_ref="plugin.doc.doc_type:is_calc",
        templates_ref="plugin.scripting.analysis:get_analysis_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_SQL,
        display_prefix=SQL_SCRIPT_DISPLAY_PREFIX,
        title=N_("SQL Helpers"),
        supports_ref="plugin.doc.doc_type:is_calc",
        templates_ref="plugin.scripting.duckdb_sql:get_sql_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_VIZ,
        display_prefix=VIZ_SCRIPT_DISPLAY_PREFIX,
        title=N_("Viz Helpers"),
        supports_ref="plugin.scripting.helper_domain:supports_calc_or_writer_manual",
        templates_ref="plugin.scripting.viz:get_viz_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_QUANT,
        display_prefix=QUANT_SCRIPT_DISPLAY_PREFIX,
        title=N_("Quant Helpers"),
        # What was wrong: supports_quant_manual is Calc-or-Writer, but quant is
        # post_venv_calc_only and the only inserter is insert_quant_result_into_calc.
        # On Writer, try_rps_post_venv returned None and format_result_for_writer
        # stringified the nested table dict.
        # How: the picker copied the symbolic/viz supports alias when the registry
        # was introduced. Egress was never a Writer inserter.
        # Why: "plugin.doc.doc_type:is_calc" matches analysis, sql, optimize, and forecast.
        supports_ref="plugin.doc.doc_type:is_calc",
        templates_ref="plugin.scripting.quant:get_quant_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_OPTIMIZE,
        display_prefix=OPTIMIZE_SCRIPT_DISPLAY_PREFIX,
        title=N_("Optimize Helpers"),
        supports_ref="plugin.doc.doc_type:is_calc",
        templates_ref="plugin.scripting.optimize:get_optimize_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_FORECAST,
        display_prefix=FORECAST_SCRIPT_DISPLAY_PREFIX,
        title=N_("Forecast Helpers"),
        supports_ref="plugin.doc.doc_type:is_calc",
        templates_ref="plugin.scripting.forecast:get_forecast_script_templates",
    ),
)

# Built from the picker rows so a new section cannot ship without a reserved
# display prefix. [Doc] is not a picker domain. is_reserved_script_name reads
# this at call time.
RESERVED_SCRIPT_PREFIXES = (DOC_SCRIPT_DISPLAY_PREFIX, *(row.display_prefix for row in PICKER_WIRING))


def picker_display_name(prefix: str, name: str) -> str:
    return f"{prefix}{name}"


def parse_picker_display_name(prefix: str, display: str) -> str | None:
    if display.startswith(prefix):
        return display[len(prefix) :]
    return None


def domain_template_entries(doc: Any | None) -> list[tuple[Any, dict[str, str]]]:
    """Return [(domain, {display_name: code})] for picker domains supporting *doc*."""
    results: list[tuple[Any, dict[str, str]]] = []
    domains: Any = getattr(PICKER_WIRING, "return_value", None)
    if domains is None:
        domains = PICKER_WIRING
    for domain in domains:
        if not domain.supports(doc):
            continue
        try:
            templates = domain.templates()
        except Exception:
            log.debug("picker templates failed for %s", domain.origin, exc_info=True)
            continue
        display_scripts = {
            picker_display_name(domain.display_prefix, name): code
            for name, code in templates.items()
        }
        results.append((domain, display_scripts))
    return results
