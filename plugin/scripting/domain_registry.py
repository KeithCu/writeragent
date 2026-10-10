# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ordered registry of trusted helper domains for RPS and the script picker.

Domain compute / egress stay in domain modules. Callables use lazy imports to avoid cycles.

Calc-only inserters are ``(doc, ctx, result)`` (same as ``insert_tabular_result_into_calc``).
Writer/Draw inserters are ``(ctx, doc, result)``. ``build_rps_spec`` keeps that split.
"""

from __future__ import annotations

import functools
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from plugin.doc.doc_type import is_calc
from plugin.framework.i18n import N_, _
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

RESERVED_SCRIPT_PREFIXES = (
    DOC_SCRIPT_DISPLAY_PREFIX,
    ANALYSIS_SCRIPT_DISPLAY_PREFIX,
    VISION_SCRIPT_DISPLAY_PREFIX,
    VIZ_SCRIPT_DISPLAY_PREFIX,
    MATH_SCRIPT_DISPLAY_PREFIX,
    UNITS_SCRIPT_DISPLAY_PREFIX,
    QUANT_SCRIPT_DISPLAY_PREFIX,
    OPTIMIZE_SCRIPT_DISPLAY_PREFIX,
    FORECAST_SCRIPT_DISPLAY_PREFIX,
    SQL_SCRIPT_DISPLAY_PREFIX,
)


def is_reserved_script_name(name: str) -> bool:
    """True when *name* begins with any reserved UI prefix (e.g. '[Doc] ', '[Vision] ')."""
    return any(name.startswith(p) for p in RESERVED_SCRIPT_PREFIXES)


@dataclass(frozen=True)
class PickerDomainSpec:
    """One built-in helper group in the Run Python Script picker."""

    origin: str
    display_prefix: str
    title_fn: Callable[[], str]
    supports: Callable[[Any], bool]
    templates: Callable[[], dict[str, str]]


@dataclass(frozen=True)
class RpsDomainSpec:
    """Post-venv result routing for one trusted helper domain."""

    id: str
    insert: Callable[..., Any]
    format_ok: Callable[..., dict[str, Any]]
    is_result: Callable[[Any], bool]
    post_venv_calc_only: bool = False
    insert_kwargs_fn: Callable[[Any, str], dict[str, Any]] | None = None


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
    if spec.post_venv_calc_only and not is_calc(doc):
        return None
    if not spec.is_result(result_data):
        return None
    insert_kwargs: dict[str, Any] = {}
    if spec.insert_kwargs_fn is not None and code:
        insert_kwargs = spec.insert_kwargs_fn(ctx, code)
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
# Three tables on purpose: WIRING_TABLE (RPS insert order), POST_VENV_DOMAIN_ORDER
# (is_result scan order), PICKER_WIRING (script-picker UI). Do not collapse them
# or add a fourth registry. Drift is caught by test_domain_registry (same members
# for wiring vs post-venv; picker is not 1:1 — text is wiring-only).
@dataclass(frozen=True)
class DomainWiring:
    id: str
    insert: str  # module.path:attr
    is_result: str  # module.path:attr
    format_ok_kind: str = "generic"  # generic | plot | symbolic | units | vision | rows
    post_venv_calc_only: bool = False
    display_label: str = ""
    insert_kwargs_from_code: str | None = None


@functools.cache
def _import_module(mod_name: str) -> Any:
    import importlib

    return importlib.import_module(mod_name)


def _resolve_module_attr(target: str) -> Any:
    mod_name, attr_name = target.rsplit(":", 1)
    return getattr(_import_module(mod_name), attr_name)


def _resolve_fn(path: str | None) -> Any:
    if not path:
        return None
    return _resolve_module_attr(path)


# Opening block tags only. A substring count of "<p" / "<h" also matched
# <pre>, <param>, <html>, <head>, <header>, and <hr>, so the status line
# over-counted structure blocks when metrics omitted line_count.
_VISION_BLOCK_OPEN_TAG = re.compile(r"<(?:p|h[1-6]|table)\b", re.IGNORECASE)


def _vision_html_block_count(html: str) -> int:
    """Count ``<p>``, ``<h1>``–``<h6>``, and ``<table>`` start tags in vision HTML."""
    return len(_VISION_BLOCK_OPEN_TAG.findall(html))


def build_rps_spec(w: DomainWiring) -> RpsDomainSpec:
    insert_kwargs_fn = None
    if w.insert_kwargs_from_code:
        insert_kwargs_fn = _resolve_fn(w.insert_kwargs_from_code)

    def insert(ctx: Any, doc: Any, result: dict[str, Any], **kwargs: Any) -> Any:
        fn = _resolve_fn(w.insert)
        if w.post_venv_calc_only:
            # Sheet egress is (doc, uno_ctx, result). Calling it as (ctx, doc, result)
            # passed the component context into calc_anchor_from_selection, which then
            # failed and the Run Python Script dialog reported an insert error.
            ret = fn(doc, ctx, result)
        elif kwargs:
            ret = fn(ctx, doc, result, **kwargs)
        else:
            ret = fn(ctx, doc, result)
        # int(ret) raises when an inserter returns a non-integer after a
        # successful insert, and the UI shows "Failed to insert result".
        # Return ret only when it is already an int and not a bool, else None.
        if isinstance(ret, int) and not isinstance(ret, bool):
            return ret
        return None

    def format_ok(*, meta: Any, result: dict[str, Any], t0: float, row_count: Any = None, stdout: str | None = None) -> dict[str, Any]:
        helper = str(getattr(meta, "helper", "") or "") or str(result.get("helper") or w.id)
        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        domain_label = w.display_label or (
            w.id.upper() if w.id == "sql" else (w.id.capitalize() if w.id != "text" else "Text analytics")
        )

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
            return rps_ok_outcome(status_ok, result=result, stdout=stdout)
        if w.format_ok_kind == "rows":
            return rps_ok_outcome(
                _("{domain} '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                    domain=domain_label, helper=helper, rows=row_count or 0, time=formatted_time
                ),
                result=result,
                stdout=stdout,
            )
        return rps_ok_outcome(
            _("{domain} '{helper}' completed. (took {time})").format(
                domain=domain_label, helper=helper, time=formatted_time
            ),
            result=result,
            stdout=stdout,
        )

    def is_result(value: Any) -> bool:
        try:
            fn = _resolve_fn(w.is_result)
        except ImportError as exc:
            # Bundles that omit a domain module (LibrePy drops duckdb_sql) raise
            # ImportError when resolving is_result. That aborted routing of a
            # plain Calc result. An unresolvable module means "not this domain".
            log.debug("Domain %s is_result module unavailable: %s", w.id, exc)
            return False
        return fn(value) if fn is not None else False

    return RpsDomainSpec(
        id=w.id,
        insert=insert,
        format_ok=format_ok,
        is_result=is_result,
        post_venv_calc_only=w.post_venv_calc_only,
        insert_kwargs_fn=insert_kwargs_fn,
    )


WIRING_TABLE: tuple[DomainWiring, ...] = (
    DomainWiring(
        id="vision",
        insert="plugin.vision.vision_egress:insert_vision_result",
        is_result="plugin.vision.vision_egress:is_vision_result",
        format_ok_kind="vision",
        display_label="Vision",
        insert_kwargs_from_code="plugin.vision.vision_runner:extract_vision_insert_kwargs",
    ),
    DomainWiring(
        id="viz",
        insert="plugin.scripting.viz:insert_viz_result_into_doc",
        is_result="plugin.scripting.viz:is_viz_result",
        format_ok_kind="plot",
        display_label="Viz",
    ),
    DomainWiring(
        id="math",
        insert="plugin.scripting.symbolic:insert_symbolic_result_into_doc",
        is_result="plugin.scripting.symbolic:is_symbolic_result",
        format_ok_kind="symbolic",
        display_label="Math",
    ),
    DomainWiring(
        id="units",
        insert="plugin.scripting.units:insert_units_result_into_doc",
        is_result="plugin.scripting.units:is_units_result",
        format_ok_kind="units",
        display_label="Units",
        insert_kwargs_from_code="plugin.scripting.units:extract_units_insert_kwargs",
    ),
    DomainWiring(
        id="text",
        insert="plugin.scripting.text_analytics:insert_text_analytics_result_into_doc",
        is_result="plugin.scripting.text_analytics:is_text_analytics_result",
        format_ok_kind="generic",
        display_label="Text analytics",
    ),
    DomainWiring(
        id="quant",
        insert="plugin.calc.quant_egress:insert_quant_result_into_calc",
        is_result="plugin.calc.quant_egress:is_quant_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        display_label="Quant",
    ),
    DomainWiring(
        id="optimize",
        insert="plugin.scripting.optimize:insert_optimize_result_into_calc",
        is_result="plugin.scripting.optimize:is_optimize_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        display_label="Optimize",
    ),
    DomainWiring(
        id="forecast",
        insert="plugin.scripting.forecast:insert_forecast_result_into_calc",
        is_result="plugin.scripting.forecast:is_forecast_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        display_label="Forecast",
    ),
    DomainWiring(
        id="analysis",
        insert="plugin.calc.analysis_egress:insert_analysis_result_into_calc",
        is_result="plugin.calc.analysis_egress:is_analysis_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        display_label="Analysis",
    ),
    DomainWiring(
        id="sql",
        insert="plugin.scripting.duckdb_sql:insert_sql_result_into_calc",
        is_result="plugin.scripting.duckdb_sql:is_sql_result",
        format_ok_kind="rows",
        post_venv_calc_only=True,
        display_label="SQL",
    ),
)


_rps_cache: list[RpsDomainSpec] | None = None
_rps_by_id: dict[str, RpsDomainSpec] | None = None


def get_rps_domains() -> Sequence[RpsDomainSpec]:
    """Ordered RPS domain specs (cached after first call)."""
    global _rps_cache
    if _rps_cache is None:
        _rps_cache = [build_rps_spec(wiring) for wiring in WIRING_TABLE]
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
    "sql",
)


def get_rps_domain_by_id(domain_id: str) -> RpsDomainSpec | None:
    global _rps_by_id
    if _rps_by_id is None:
        _rps_by_id = {spec.id: spec for spec in get_rps_domains()}
    return _rps_by_id.get(domain_id)


def get_post_venv_domains() -> list[RpsDomainSpec]:
    """Domains that participate in post-venv result routing, in order."""
    out: list[RpsDomainSpec] = []
    for domain_id in POST_VENV_DOMAIN_ORDER:
        spec = get_rps_domain_by_id(domain_id)
        if spec is not None:
            out.append(spec)
    return out


_RUN_IMPORT_DATA_BINDING: dict[str, dict[str, bool]] = {
    "run_analysis": {"calc_only": True},
    "run_viz": {"calc_only": False},
    "run_quant": {"calc_only": True},
    "run_optimize": {"calc_only": True},
    "run_forecast": {"calc_only": True},
}

# Current templates call the helper directly (``result = describe_data(...)``),
# not ``run_analysis(``. The import plus that call is what should show Data.
_IMPORT_DATA_BINDING: tuple[tuple[str, bool], ...] = (
    ("writeragent.scripting.analysis", True),
    ("writeragent.scripting.viz", False),
    ("writeragent.scripting.quant", True),
    ("writeragent.scripting.optimize", True),
    ("writeragent.scripting.forecast", True),
    ("writeragent.scripting.duckdb_sql", True),
)


def script_header_needs_data_binding(code: str, *, doc: Any) -> bool:
    """True when *code* uses a trusted helper that may bind Calc sheet data.

    Parse the AST once. A per-module walk re-parsed the same source for every
    entry in _IMPORT_DATA_BINDING and missed ``import ... as`` plus
    ``from writeragent.scripting import analysis``. Track direct function
    imports and module aliases, then check whether any imported helper is called.
    """
    if not code:
        return False
    calc = is_calc(doc)
    for run_name, cfg in _RUN_IMPORT_DATA_BINDING.items():
        if re.search(rf"\b{re.escape(run_name)}\s*\(", code):
            if not cfg.get("calc_only") or calc:
                return True
    if calc and re.search(r"\bquery_folder_sql\s*\(", code):
        return True

    try:
        import ast

        tree = ast.parse(code)
    except (SyntaxError, TypeError, ValueError):
        for module, calc_only in _IMPORT_DATA_BINDING:
            if calc_only and not calc:
                continue
            if module in code:
                return True
        return False

    imported_names: set[str] = set()
    alias_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for module, calc_only in _IMPORT_DATA_BINDING:
                if calc_only and not calc:
                    continue
                if mod == module:
                    for alias in node.names:
                        bound = alias.asname or alias.name
                        if bound and bound != "*":
                            imported_names.add(bound)
                elif mod == "writeragent.scripting":
                    sub = module.split(".")[-1]
                    for alias in node.names:
                        if alias.name == sub:
                            bound = alias.asname or alias.name
                            alias_modules.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for module, calc_only in _IMPORT_DATA_BINDING:
                    if calc_only and not calc:
                        continue
                    if alias.name == module:
                        bound = alias.asname or alias.name
                        alias_modules.add(bound)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in imported_names:
            return True
        if isinstance(node.func, ast.Attribute):
            target = node.func.value
            if isinstance(target, ast.Name) and target.id in alias_modules:
                return True
    return False


# --- Picker domains (order in build_xdl_script_picker_state) ---


@dataclass(frozen=True)
class PickerWiring:
    """Declarative picker section: supports/templates resolved lazily via module:attr strings."""

    origin: str
    display_prefix: str
    title: str
    supports: str  # "calc_only" or "module.path:attr"
    templates: str  # "module.path:attr" returning dict[str, str]


def _picker_calc_only(doc: Any) -> bool:
    try:
        return doc is not None and is_calc(doc)
    except Exception:
        return False


def _picker_supports_fn(supports: str) -> Callable[[Any], bool]:
    if supports == "calc_only":
        return _picker_calc_only

    def _supports(doc: Any) -> bool:
        if doc is None:
            return False
        try:
            return bool(_resolve_module_attr(supports)(doc))
        except Exception:
            log.debug("script picker supports check failed for %s", supports, exc_info=True)
            return False

    return _supports


def _picker_templates_fn(templates: str) -> Callable[[], dict[str, str]]:
    def _templates() -> dict[str, str]:
        return dict(_resolve_module_attr(templates)())

    return _templates


def build_picker_spec(wiring: PickerWiring) -> PickerDomainSpec:
    title = wiring.title
    return PickerDomainSpec(
        origin=wiring.origin,
        display_prefix=wiring.display_prefix,
        title_fn=lambda: _(title),
        supports=_picker_supports_fn(wiring.supports),
        templates=_picker_templates_fn(wiring.templates),
    )


PICKER_WIRING: tuple[PickerWiring, ...] = (
    PickerWiring(
        origin=SCRIPT_ORIGIN_VISION,
        display_prefix=VISION_SCRIPT_DISPLAY_PREFIX,
        title=N_("Vision Helpers"),
        supports="plugin.vision.vision_runner:supports_vision_manual",
        templates="plugin.vision.vision_templates:get_vision_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_MATH,
        display_prefix=MATH_SCRIPT_DISPLAY_PREFIX,
        title=N_("Math Helpers"),
        supports="plugin.scripting.symbolic:supports_symbolic_manual",
        templates="plugin.scripting.symbolic:get_math_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_UNITS,
        display_prefix=UNITS_SCRIPT_DISPLAY_PREFIX,
        title=N_("Units Helpers"),
        supports="plugin.scripting.units:supports_units_manual",
        templates="plugin.scripting.units:get_units_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_ANALYSIS,
        display_prefix=ANALYSIS_SCRIPT_DISPLAY_PREFIX,
        title=N_("Analysis Helpers"),
        supports="calc_only",
        templates="plugin.scripting.analysis:get_analysis_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_SQL,
        display_prefix=SQL_SCRIPT_DISPLAY_PREFIX,
        title=N_("SQL Helpers"),
        supports="calc_only",
        templates="plugin.scripting.duckdb_sql:get_sql_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_VIZ,
        display_prefix=VIZ_SCRIPT_DISPLAY_PREFIX,
        title=N_("Viz Helpers"),
        supports="plugin.scripting.viz:supports_viz_manual",
        templates="plugin.scripting.viz:get_viz_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_QUANT,
        display_prefix=QUANT_SCRIPT_DISPLAY_PREFIX,
        title=N_("Quant Helpers"),
        supports="plugin.scripting.quant:supports_quant_manual",
        templates="plugin.scripting.quant:get_quant_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_OPTIMIZE,
        display_prefix=OPTIMIZE_SCRIPT_DISPLAY_PREFIX,
        title=N_("Optimize Helpers"),
        supports="calc_only",
        templates="plugin.scripting.optimize:get_optimize_script_templates",
    ),
    PickerWiring(
        origin=SCRIPT_ORIGIN_FORECAST,
        display_prefix=FORECAST_SCRIPT_DISPLAY_PREFIX,
        title=N_("Forecast Helpers"),
        supports="calc_only",
        templates="plugin.scripting.forecast:get_forecast_script_templates",
    ),
)


_picker_cache: list[PickerDomainSpec] | None = None


def get_picker_domains() -> list[PickerDomainSpec]:
    """Built-in helper sections for the script picker (lazy templates/supports).

    Cached like ``get_rps_domains``. ``title_fn`` still calls ``_()`` on use,
    so a later catalog load is not frozen into the cached specs.
    """
    global _picker_cache
    if _picker_cache is None:
        _picker_cache = [build_picker_spec(wiring) for wiring in PICKER_WIRING]
    return _picker_cache


def picker_display_name(prefix: str, name: str) -> str:
    return f"{prefix}{name}"


def parse_picker_display_name(prefix: str, display: str) -> str | None:
    if display.startswith(prefix):
        return display[len(prefix) :]
    return None
