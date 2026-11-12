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

import importlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from plugin.framework.i18n import N_, _
from plugin.scripting.helper_domain import (
    build_helper_script_template,
    format_elapsed_time,
    is_status_helper_result,
    plot_insert_ok_outcome,
    preview_insert_ok_outcome,
    rps_ok_outcome,
)
from plugin.scripting.helper_registry import HelperSpec, names, specs

log = logging.getLogger("writeragent.scripting")

# --- Script picker origins / prefixes (stable IDs used in origin_map) ---

SCRIPT_ORIGIN_USER = "user"
SCRIPT_ORIGIN_DOCUMENT = "document"
SCRIPT_ORIGIN_ANALYSIS = "analysis"
SCRIPT_ORIGIN_VISION = "vision"

DOC_SCRIPT_DISPLAY_PREFIX = "[Doc] "
ANALYSIS_SCRIPT_DISPLAY_PREFIX = "[Analysis] "
VISION_SCRIPT_DISPLAY_PREFIX = "[Vision] "


def is_reserved_script_name(name: str) -> bool:
    """True when *name* begins with any reserved UI prefix (e.g. '[Doc] ', '[Vision] ')."""
    return any(name.startswith(p) for p in RESERVED_SCRIPT_PREFIXES)


FormatOkKind = Literal["generic", "plot", "symbolic", "units", "vision", "rows"]


def _resolve_module_attr(target: str) -> Any:
    mod_name, attr_name = target.rsplit(":", 1)
    return getattr(importlib.import_module(mod_name), attr_name)


# Opening block tags only. A substring count of "<p" / "<h" would also match
# <pre>, <param>, <html>, <head>, <header>, and <hr>, and the status line
# would over-count structure blocks when metrics omit line_count.
_VISION_BLOCK_OPEN_TAG = re.compile(r"<(?:p|h[1-6]|table)\b", re.IGNORECASE)


def _vision_html_block_count(html: str) -> int:
    """Count ``<p>``, ``<h1>``–``<h6>``, and ``<table>`` start tags in vision HTML."""
    return len(_VISION_BLOCK_OPEN_TAG.findall(html))


# Three tables on purpose: WIRING_TABLE (RPS insert order), POST_VENV_DOMAIN_ORDER
# (is_result scan order), PICKER_WIRING (script-picker UI). Do not collapse them
# or add a fourth registry. Drift is caught by test_domain_registry: same members
# for wiring vs post-venv; picker is not 1:1 (text is wiring-only); a
# post_venv_calc_only domain's picker supports is "plugin.framework.doc_type:is_calc".
@dataclass(frozen=True)
class DomainWiring:
    id: str
    insert_ref: str  # module.path:attr
    display_label: str
    is_result_ref: str | None = None  # module.path:attr
    format_ok_kind: FormatOkKind = "generic"
    post_venv_calc_only: bool = False
    insert_kwargs_from_code: str | None = None
    helper_domain: str | None = None
    rpc_domain: str | None = None
    runner: str | None = None  # "sheet" or "symbolic"
    error_code: str | None = None
    empty_data_message: str | None = None
    require_calc_or_writer: bool = False
    force_queue: bool = False
    long_timeout: bool = False
    result_error_codes: frozenset[str] | None = None
    import_module: str | None = None
    run_name: str | None = None
    data_expr: str = "data"
    context_expr: str = "{}"
    leading_data: bool = False
    invoke: str = "direct"
    extra_comment_lines: tuple[str, ...] = ("# Edit the run call below, then Run.",)

    def insert(self, ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> Any:
        fn = _resolve_module_attr(self.insert_ref)
        # Sheet egress is (doc, uno_ctx, result). Calling it as (ctx, doc, result)
        # would pass the component context to calc_anchor_from_selection, which
        # fails, and the Run Python Script dialog would report an insert error.
        args = (doc, ctx) if self.post_venv_calc_only else (ctx, doc)
        ret = fn(*args, result, **kwargs)
        # An inserter may return a non-integer after a successful insert;
        # int(ret) would raise and the UI would show "Failed to insert result".
        # Return ret only when it is already an int and not a bool, else None.
        if isinstance(ret, int) and not isinstance(ret, bool):
            return ret
        return None

    def format_ok(self, *, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        helper = str(result.get("helper") or self.id)

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
        if self.result_error_codes is not None:
            domain_names = names(self.helper_domain) if self.helper_domain else frozenset()
            return is_status_helper_result(value, domain_names, self.result_error_codes)
        if not self.is_result_ref:
            return False
        try:
            fn = _resolve_module_attr(self.is_result_ref)
        except ImportError as exc:
            # Bundles that omit a domain module (LibrePy drops duckdb_sql) raise
            # ImportError when resolving is_result. An unresolvable module means
            # "not this domain"; raising would abort routing of a plain Calc result.
            log.debug("Domain %s is_result module unavailable: %s", self.id, exc)
            return False
        return fn(value)

    def insert_kwargs(self, ctx: Any, code: str) -> dict[str, Any]:
        ref = self.insert_kwargs_from_code
        if not ref:
            return {}
        return _resolve_module_attr(ref)(ctx, code)

    def _template_body(self, spec: HelperSpec) -> str:
        pos = ("value", "from", "to") if spec.name == "convert_quantity" else ()
        return build_helper_script_template(
            helper=spec.name,
            params=dict(spec.defaults),
            description=spec.description,
            import_module=self.import_module or "",
            run_name=self.run_name,
            data_expr=self.data_expr,
            context_expr=self.context_expr,
            extra_comment_lines=self.extra_comment_lines,
            positional_args=pos,
            leading_data=self.leading_data,
            invoke=self.invoke,
        )

    def templates(self) -> dict[str, str]:
        if not self.helper_domain:
            return {}
        return {
            spec.name: self._template_body(spec)
            for spec in specs(self.helper_domain)
            if spec.ship
        }


WIRING_TABLE: tuple[DomainWiring, ...] = (
    DomainWiring(
        id="vision",
        insert_ref="plugin.vision.vision_egress:insert_vision_result",
        display_label="Vision",
        is_result_ref="plugin.vision.vision_egress:is_vision_result",
        format_ok_kind="vision",
        insert_kwargs_from_code="plugin.vision.vision_runner:extract_vision_insert_kwargs",
    ),
    DomainWiring(
        id="viz",
        insert_ref="plugin.scripting.viz:insert_viz_result_into_doc",
        display_label="Viz",
        is_result_ref="plugin.scripting.viz:is_viz_result",
        format_ok_kind="plot",
        helper_domain="viz",
        rpc_domain="viz",
        runner="sheet",
        error_code="VIZ_ERROR",
        empty_data_message="No data to plot",
        require_calc_or_writer=True,
        import_module="writeragent.scripting.viz",
        run_name="run_viz",
        leading_data=True,
        extra_comment_lines=("# Set the data range in the toolbar (or select cells), then Run.",),
    ),
    DomainWiring(
        id="math",
        insert_ref="plugin.scripting.symbolic:insert_symbolic_result_into_doc",
        display_label="Math",
        format_ok_kind="symbolic",
        result_error_codes=frozenset({"SYMBOLIC_ERROR"}),
        helper_domain="symbolic",
        rpc_domain="symbolic",
        runner="symbolic",
        long_timeout=True,
        import_module="writeragent.scripting.symbolic",
        run_name="run_symbolic",
        data_expr="None",
    ),
    DomainWiring(
        id="units",
        insert_ref="plugin.scripting.units:insert_units_result_into_doc",
        display_label="Units",
        format_ok_kind="units",
        insert_kwargs_from_code="plugin.scripting.units:extract_units_insert_kwargs",
        result_error_codes=frozenset({"UNITS_ERROR"}),
        helper_domain="units",
        import_module="writeragent.scripting.units",
        run_name="run_units",
    ),
    DomainWiring(
        id="text",
        insert_ref="plugin.scripting.text_analytics:insert_text_analytics_result_into_doc",
        display_label="Text analytics",
        is_result_ref="plugin.scripting.text_analytics:is_text_analytics_result",
        format_ok_kind="generic",
        helper_domain="text",
        import_module="writeragent.scripting.text_analytics",
        run_name="run_text_analytics",
        data_expr="text",
        context_expr="document_context",
        invoke="runner",
        extra_comment_lines=("# Works on Writer documents (document text is injected on Run).",),
    ),
    DomainWiring(
        id="quant",
        insert_ref="plugin.calc.quant_egress:insert_quant_result_into_calc",
        display_label="Quant",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        result_error_codes=frozenset(),
        helper_domain="quant",
        import_module="writeragent.scripting.quant",
        run_name="run_quant",
        invoke="runner",
    ),
    DomainWiring(
        id="optimize",
        insert_ref="plugin.scripting.optimize:insert_optimize_result_into_calc",
        display_label="Optimize",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        result_error_codes=frozenset({"OPTIMIZE_ERROR"}),
        helper_domain="optimize",
        rpc_domain="optimize",
        runner="sheet",
        error_code="OPTIMIZE_ERROR",
        empty_data_message="No data to optimize",
        import_module="writeragent.scripting.optimize",
        run_name="run_optimize",
        leading_data=True,
        extra_comment_lines=("# Set the data range in the toolbar (or select cells), then Run.",),
    ),
    DomainWiring(
        id="forecast",
        insert_ref="plugin.scripting.forecast:insert_forecast_result_into_calc",
        display_label="Forecast",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        result_error_codes=frozenset({"FORECAST_ERROR"}),
        helper_domain="forecast",
        rpc_domain="forecast",
        runner="sheet",
        error_code="FORECAST_ERROR",
        empty_data_message="No data to forecast",
        import_module="writeragent.scripting.forecast",
        run_name="run_forecast",
        leading_data=True,
        extra_comment_lines=("# Set the data range in the toolbar (or select cells), then Run.",),
    ),
    DomainWiring(
        id="analysis",
        insert_ref="plugin.calc.analysis_egress:insert_analysis_result_into_calc",
        display_label="Analysis",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        result_error_codes=frozenset({"ANALYSIS_ERROR"}),
        helper_domain="analysis",
        rpc_domain="analysis",
        runner="sheet",
        error_code="ANALYSIS_ERROR",
        empty_data_message="No data to analyze",
        force_queue=True,
        import_module="writeragent.scripting.analysis",
        run_name="run_analysis",
        leading_data=True,
    ),
    DomainWiring(
        id="sql",
        insert_ref="plugin.scripting.duckdb_sql:insert_sql_result_into_calc",
        display_label="SQL",
        is_result_ref="plugin.scripting.duckdb_sql:is_sql_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
    ),
)


def get_domain_wiring(wiring_id: str) -> DomainWiring:
    """Return DomainWiring entry by id."""
    for wiring in WIRING_TABLE:
        if wiring.id == wiring_id:
            return wiring
    raise KeyError(f"Unknown wiring id: {wiring_id!r}")


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
    """Declarative picker section. `origin` is the WIRING_TABLE id whose `templates()` fills the section unless `templates_ref` names a `module:attr` function (vision and sql keep their own template builders)."""

    origin: str
    display_prefix: str
    title: str
    supports_ref: str  # "module.path:attr" (e.g. "plugin.framework.doc_type:is_calc")
    templates_ref: str | None = None  # "module.path:attr" returning dict[str, str]

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
        if self.templates_ref is None:
            return get_domain_wiring(self.origin).templates()
        return _resolve_module_attr(self.templates_ref)()


PICKER_WIRING: tuple[PickerWiring, ...] = (
    PickerWiring(
        origin=SCRIPT_ORIGIN_VISION,
        display_prefix=VISION_SCRIPT_DISPLAY_PREFIX,
        title=N_("Vision Helpers"),
        supports_ref="plugin.vision.vision_runner:supports_vision_manual",
        templates_ref="plugin.vision.vision_templates:get_vision_script_templates",
    ),
    PickerWiring(
        origin="math",
        display_prefix="[Math] ",
        title=N_("Math Helpers"),
        supports_ref="plugin.scripting.helper_domain:supports_calc_or_writer_manual",
    ),
    PickerWiring(
        origin="units",
        display_prefix="[Units] ",
        title=N_("Units Helpers"),
        supports_ref="plugin.scripting.helper_domain:supports_calc_or_writer_manual",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_ANALYSIS,
        display_prefix=ANALYSIS_SCRIPT_DISPLAY_PREFIX,
        title=N_("Analysis Helpers"),
        supports_ref="plugin.framework.doc_type:is_calc",
    ),
    PickerWiring(
        origin="sql",
        display_prefix="[SQL] ",
        title=N_("SQL Helpers"),
        supports_ref="plugin.framework.doc_type:is_calc",
        templates_ref="plugin.scripting.duckdb_sql:get_sql_script_templates",
    ),
    PickerWiring(
        origin="viz",
        display_prefix="[Viz] ",
        title=N_("Viz Helpers"),
        supports_ref="plugin.scripting.helper_domain:supports_calc_or_writer_manual",
    ),
    PickerWiring(
        origin="quant",
        display_prefix="[Quant] ",
        title=N_("Quant Helpers"),
        # quant manual egress is Calc-only (insert_quant_result_into_calc); Writer must not claim supports_quant_manual or users get stringified table dicts.
        supports_ref="plugin.framework.doc_type:is_calc",
    ),
    PickerWiring(
        origin="optimize",
        display_prefix="[Optimize] ",
        title=N_("Optimize Helpers"),
        supports_ref="plugin.framework.doc_type:is_calc",
    ),
    PickerWiring(
        origin="forecast",
        display_prefix="[Forecast] ",
        title=N_("Forecast Helpers"),
        supports_ref="plugin.framework.doc_type:is_calc",
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
    for domain in PICKER_WIRING:
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
