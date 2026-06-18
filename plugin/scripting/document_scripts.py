# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Document-attached named Python scripts and Calc workbook init scripts (UserDefinedProperties)."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from plugin.doc.document_helpers import (
    get_document_property,
    is_calc,
    is_draw,
    is_writer,
    set_document_property,
)
from plugin.framework.errors import UnoObjectError
from plugin.framework.i18n import _
from plugin.framework.json_utils import safe_json_loads
from plugin.framework.uno_context import get_desktop
from plugin.scripting.session_manager import calc_init_session_id

log = logging.getLogger(__name__)

DOCUMENT_SCRIPTS_UDPROP = "WriterAgentDocumentPythonScripts"
_MAX_DOCUMENT_SCRIPTS_BYTES = 900_000
_SOFT_WARN_SCRIPT_BYTES = 200_000
_ENVELOPE_VERSION = 1

SCRIPT_ORIGIN_USER = "user"
SCRIPT_ORIGIN_DOCUMENT = "document"
SCRIPT_ORIGIN_ANALYSIS = "analysis"
SCRIPT_ORIGIN_VISION = "vision"
SCRIPT_ORIGIN_VIZ = "viz"
SCRIPT_ORIGIN_MATH = "math"
SCRIPT_ORIGIN_UNITS = "units"
SCRIPT_ORIGIN_QUANT = "quant"
SCRIPT_ORIGIN_OPTIMIZE = "optimize"
SCRIPT_ORIGIN_SQL = "sql"

DOC_SCRIPT_DISPLAY_PREFIX = "[Doc] "
ANALYSIS_SCRIPT_DISPLAY_PREFIX = "[Analysis] "
VISION_SCRIPT_DISPLAY_PREFIX = "[Vision] "
VIZ_SCRIPT_DISPLAY_PREFIX = "[Viz] "
MATH_SCRIPT_DISPLAY_PREFIX = "[Math] "
UNITS_SCRIPT_DISPLAY_PREFIX = "[Units] "
QUANT_SCRIPT_DISPLAY_PREFIX = "[Quant] "
OPTIMIZE_SCRIPT_DISPLAY_PREFIX = "[Optimize] "
SQL_SCRIPT_DISPLAY_PREFIX = "[SQL] "


def _normalize_doc_url(url: Any) -> str:
    if not url:
        return ""
    s = str(url).strip()
    if s.endswith("/") and len(s) > 1:
        s = s[:-1]
    return s


def document_scripts_identity(doc: Any) -> str:
    """Stable identity for stale detection (normalized URL or empty for untitled)."""
    try:
        if hasattr(doc, "getURL"):
            return _normalize_doc_url(doc.getURL() or "")
    except Exception:
        log.debug("document_scripts_identity failed", exc_info=True)
    return ""


def get_active_document_for_scripts(ctx: Any) -> Any | None:
    try:
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
    except Exception:
        log.debug("document_scripts: could not resolve active document", exc_info=True)
        return None
    if doc is None:
        return None
    if is_writer(doc) or is_calc(doc) or is_draw(doc):
        return doc
    return None


def is_document_readonly_for_scripts(doc: Any) -> bool:
    if doc is None:
        return True
    try:
        if hasattr(doc, "isReadonly") and doc.isReadonly():
            return True
    except Exception:
        log.debug("document_scripts: isReadonly check failed", exc_info=True)
    return False


def _envelope_to_json(scripts: dict[str, str]) -> str:
    return json.dumps({"version": _ENVELOPE_VERSION, "scripts": scripts}, separators=(",", ":"))


def _envelope_from_json(raw: str) -> dict[str, str] | None:
    if not (raw or "").strip():
        return None
    parsed = safe_json_loads(raw.strip())
    if not isinstance(parsed, dict):
        log.warning("document_scripts: expected object, got %s", type(parsed).__name__)
        return None
    if parsed.get("version") != _ENVELOPE_VERSION:
        log.warning("document_scripts: unsupported version %r", parsed.get("version"))
        return None
    scripts_raw = parsed.get("scripts")
    if not isinstance(scripts_raw, dict):
        log.warning("document_scripts: missing scripts map")
        return None
    out: dict[str, str] = {}
    for key, value in scripts_raw.items():
        if isinstance(key, str) and isinstance(value, str):
            out[key] = value
    return out


def get_document_scripts(doc: Any) -> dict[str, str]:
    if doc is None:
        return {}
    raw = get_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, default=None)
    if raw is None:
        return {}
    scripts = _envelope_from_json(str(raw))
    if scripts is None:
        return {}
    return scripts


def has_document_scripts(doc: Any) -> bool:
    return bool(get_document_scripts(doc))


def _check_payload_size(scripts: dict[str, str]) -> str | None:
    encoded = _envelope_to_json(scripts).encode("utf-8")
    if len(encoded) > _MAX_DOCUMENT_SCRIPTS_BYTES:
        return _("Document scripts are too large to store in the document ({0} bytes).").format(len(encoded))
    for name, code in scripts.items():
        nbytes = len((code or "").encode("utf-8"))
        if nbytes > _SOFT_WARN_SCRIPT_BYTES:
            log.warning("document_scripts: script %r is large (%d bytes)", name, nbytes)
    return None


def set_document_scripts(doc: Any, scripts: dict[str, str]) -> str | None:
    if doc is None:
        return _("No document is open to save scripts.")
    err = _check_payload_size(scripts)
    if err:
        return err
    if is_document_readonly_for_scripts(doc):
        return _(
            "Document is read-only or properties cannot be written. "
            "Script saved to your personal library instead."
        )
    try:
        set_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, _envelope_to_json(scripts))
        return None
    except (UnoObjectError, Exception):
        log.exception("document_scripts: failed to persist on document")
        return _(
            "Document is read-only or properties cannot be written. "
            "Script saved to your personal library instead."
        )


def get_calc_init_script(doc: Any, *, default: str = "") -> str:
    scripts = get_document_scripts(doc)
    return scripts.get("INIT") or scripts.get("Init") or default


def set_calc_init_script(doc: Any, code: str) -> str | None:
    """Persist init script on *doc* under the name 'INIT' or 'Init' in document scripts."""
    scripts = dict(get_document_scripts(doc))
    if code:
        if "Init" in scripts:
            scripts["Init"] = code
        else:
            scripts["INIT"] = code
    else:
        scripts.pop("INIT", None)
        scripts.pop("Init", None)
    return set_document_scripts(doc, scripts)


def get_calc_document_from_ctx(ctx: Any) -> Any | None:
    try:
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
    except Exception:
        log.debug("document_scripts: could not resolve active Calc document", exc_info=True)
        return None
    if doc is None or not is_calc(doc):
        return None
    return doc


def init_script_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def build_python_eval_init_kwargs(doc: Any) -> dict[str, Any]:
    """Kwargs for ``run_code_in_user_venv`` init execution (pass with separate ``session_id=``)."""
    init_code = (get_calc_init_script(doc) or "").strip()
    if not init_code:
        return {}
    return {
        "init_script": init_code,
        "init_session_id": calc_init_session_id(doc),
        "init_script_hash": init_script_hash(init_code),
    }


def attach_document_script(doc: Any, name: str, code: str, *, overwrite: bool = False) -> str | None:
    name = (name or "").strip()
    if not name:
        return _("Script name cannot be empty.")
    scripts = dict(get_document_scripts(doc))
    if name in scripts and not overwrite:
        return _("A script named '{0}' already exists in this document.").format(name)
    scripts[name] = code if code is not None else ""
    return set_document_scripts(doc, scripts)


def delete_document_script(doc: Any, name: str) -> str | None:
    scripts = dict(get_document_scripts(doc))
    scripts.pop(name, None)
    return set_document_scripts(doc, scripts)


def save_document_script(doc: Any, name: str, code: str) -> str | None:
    return attach_document_script(doc, name, code, overwrite=True)


def document_script_display_name(name: str) -> str:
    return f"{DOC_SCRIPT_DISPLAY_PREFIX}{name}"


def parse_document_script_display_name(display: str) -> str | None:
    if display.startswith(DOC_SCRIPT_DISPLAY_PREFIX):
        return display[len(DOC_SCRIPT_DISPLAY_PREFIX) :]
    return None


def analysis_script_display_name(name: str) -> str:
    return f"{ANALYSIS_SCRIPT_DISPLAY_PREFIX}{name}"


def parse_analysis_script_display_name(display: str) -> str | None:
    if display.startswith(ANALYSIS_SCRIPT_DISPLAY_PREFIX):
        return display[len(ANALYSIS_SCRIPT_DISPLAY_PREFIX) :]
    return None


def vision_script_display_name(name: str) -> str:
    return f"{VISION_SCRIPT_DISPLAY_PREFIX}{name}"


def parse_vision_script_display_name(display: str) -> str | None:
    if display.startswith(VISION_SCRIPT_DISPLAY_PREFIX):
        return display[len(VISION_SCRIPT_DISPLAY_PREFIX) :]
    return None


def viz_script_display_name(name: str) -> str:
    return f"{VIZ_SCRIPT_DISPLAY_PREFIX}{name}"


def parse_viz_script_display_name(display: str) -> str | None:
    if display.startswith(VIZ_SCRIPT_DISPLAY_PREFIX):
        return display[len(VIZ_SCRIPT_DISPLAY_PREFIX) :]
    return None


def math_script_display_name(name: str) -> str:
    return f"{MATH_SCRIPT_DISPLAY_PREFIX}{name}"


def parse_math_script_display_name(display: str) -> str | None:
    if display.startswith(MATH_SCRIPT_DISPLAY_PREFIX):
        return display[len(MATH_SCRIPT_DISPLAY_PREFIX) :]
    return None


def units_script_display_name(name: str) -> str:
    return f"{UNITS_SCRIPT_DISPLAY_PREFIX}{name}"


def parse_units_script_display_name(display: str) -> str | None:
    if display.startswith(UNITS_SCRIPT_DISPLAY_PREFIX):
        return display[len(UNITS_SCRIPT_DISPLAY_PREFIX) :]
    return None


def quant_script_display_name(name: str) -> str:
    return f"{QUANT_SCRIPT_DISPLAY_PREFIX}{name}"


def parse_quant_script_display_name(display: str) -> str | None:
    if display.startswith(QUANT_SCRIPT_DISPLAY_PREFIX):
        return display[len(QUANT_SCRIPT_DISPLAY_PREFIX) :]
    return None


def optimize_script_display_name(name: str) -> str:
    return f"{OPTIMIZE_SCRIPT_DISPLAY_PREFIX}{name}"


def parse_optimize_script_display_name(display: str) -> str | None:
    if display.startswith(OPTIMIZE_SCRIPT_DISPLAY_PREFIX):
        return display[len(OPTIMIZE_SCRIPT_DISPLAY_PREFIX) :]
    return None


def sql_script_display_name(name: str) -> str:
    return f"{SQL_SCRIPT_DISPLAY_PREFIX}{name}"


def parse_sql_script_display_name(display: str) -> str | None:
    if display.startswith(SQL_SCRIPT_DISPLAY_PREFIX):
        return display[len(SQL_SCRIPT_DISPLAY_PREFIX) :]
    return None


def resolve_script_picker_entry(display_name: str, origin_map: dict[str, str]) -> tuple[str, str]:
    """Return (real_name, origin) for a listbox/display label."""
    origin = origin_map.get(display_name, SCRIPT_ORIGIN_USER)
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        real = parse_document_script_display_name(display_name)
        return (real or display_name, SCRIPT_ORIGIN_DOCUMENT)
    if origin == SCRIPT_ORIGIN_ANALYSIS:
        real = parse_analysis_script_display_name(display_name)
        return (real or display_name, SCRIPT_ORIGIN_ANALYSIS)
    if origin == SCRIPT_ORIGIN_VISION:
        real = parse_vision_script_display_name(display_name)
        return (real or display_name, SCRIPT_ORIGIN_VISION)
    if origin == SCRIPT_ORIGIN_VIZ:
        real = parse_viz_script_display_name(display_name)
        return (real or display_name, SCRIPT_ORIGIN_VIZ)
    if origin == SCRIPT_ORIGIN_MATH:
        real = parse_math_script_display_name(display_name)
        return (real or display_name, SCRIPT_ORIGIN_MATH)
    if origin == SCRIPT_ORIGIN_UNITS:
        real = parse_units_script_display_name(display_name)
        return (real or display_name, SCRIPT_ORIGIN_UNITS)
    if origin == SCRIPT_ORIGIN_QUANT:
        real = parse_quant_script_display_name(display_name)
        return (real or display_name, SCRIPT_ORIGIN_QUANT)
    if origin == SCRIPT_ORIGIN_OPTIMIZE:
        real = parse_optimize_script_display_name(display_name)
        return (real or display_name, SCRIPT_ORIGIN_OPTIMIZE)
    if origin == SCRIPT_ORIGIN_SQL:
        real = parse_sql_script_display_name(display_name)
        return (real or display_name, SCRIPT_ORIGIN_SQL)
    return (display_name, SCRIPT_ORIGIN_USER)


def _analysis_script_section(doc: Any | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    try:
        if not is_calc(doc):
            return None
    except Exception:
        return None
    from plugin.scripting.analysis import get_analysis_script_templates

    templates = get_analysis_script_templates()
    display_scripts = {analysis_script_display_name(name): code for name, code in templates.items()}
    return {"id": SCRIPT_ORIGIN_ANALYSIS, "title": _("Analysis Helpers"), "scripts": display_scripts}


def _vision_script_section(doc: Any | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    from plugin.vision.vision_runner import supports_vision_manual

    try:
        if not supports_vision_manual(doc):
            return None
    except Exception:
        return None
    from plugin.vision.vision_templates import get_vision_script_templates

    templates = get_vision_script_templates()
    display_scripts = {vision_script_display_name(name): code for name, code in templates.items()}
    return {"id": SCRIPT_ORIGIN_VISION, "title": _("Vision Helpers"), "scripts": display_scripts}


def _viz_script_section(doc: Any | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    from plugin.scripting.viz import supports_viz_manual

    try:
        if not supports_viz_manual(doc):
            return None
    except Exception:
        return None
    from plugin.scripting.viz import get_viz_script_templates

    templates = get_viz_script_templates()
    display_scripts = {viz_script_display_name(name): code for name, code in templates.items()}
    return {"id": SCRIPT_ORIGIN_VIZ, "title": _("Viz Helpers"), "scripts": display_scripts}


def _math_script_section(doc: Any | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    from plugin.scripting.symbolic import supports_symbolic_manual

    try:
        if not supports_symbolic_manual(doc):
            return None
    except Exception:
        return None
    from plugin.scripting.symbolic import get_math_script_templates

    templates = get_math_script_templates()
    display_scripts = {math_script_display_name(name): code for name, code in templates.items()}
    return {"id": SCRIPT_ORIGIN_MATH, "title": _("Math Helpers"), "scripts": display_scripts}


def _units_script_section(doc: Any | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    from plugin.scripting.units import get_units_script_templates, supports_units_manual

    try:
        if not supports_units_manual(doc):
            return None
    except Exception:
        return None

    templates = get_units_script_templates()
    display_scripts = {units_script_display_name(name): code for name, code in templates.items()}
    return {"id": SCRIPT_ORIGIN_UNITS, "title": _("Units Helpers"), "scripts": display_scripts}


def _quant_script_section(doc: Any | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    from plugin.scripting.quant import supports_quant_manual

    try:
        if not supports_quant_manual(doc):
            return None
    except Exception:
        return None
    from plugin.scripting.quant import get_quant_template, HELPER_NAMES

    templates = {name: get_quant_template(name) for name in HELPER_NAMES if get_quant_template(name)}
    display_scripts = {quant_script_display_name(name): code for name, code in templates.items()}
    return {"id": SCRIPT_ORIGIN_QUANT, "title": _("Quant Helpers"), "scripts": display_scripts}


def _optimize_script_section(doc: Any | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    try:
        if not is_calc(doc):
            return None
    except Exception:
        return None
    from plugin.scripting.optimize import get_optimize_template, HELPER_NAMES

    templates = {name: get_optimize_template(name) for name in HELPER_NAMES if get_optimize_template(name)}
    display_scripts = {optimize_script_display_name(name): code for name, code in templates.items()}
    return {"id": SCRIPT_ORIGIN_OPTIMIZE, "title": _("Optimize Helpers"), "scripts": display_scripts}


def _sql_script_section(doc: Any | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    try:
        if not is_calc(doc):
            return None
    except Exception:
        return None
    from plugin.scripting.duckdb_sql import get_sql_script_templates

    templates = get_sql_script_templates()
    display_scripts = {sql_script_display_name(name): code for name, code in templates.items()}
    return {"id": SCRIPT_ORIGIN_SQL, "title": _("SQL Helpers"), "scripts": display_scripts}


def build_xdl_script_picker_state(
    ctx: Any,
    doc: Any | None,
    saved_scripts: dict[str, str],
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Return (dropdown_items, merged_scripts_by_display_key, origin_map)."""
    user_scripts = dict(saved_scripts) if isinstance(saved_scripts, dict) else {}
    doc_scripts = get_document_scripts(doc) if doc is not None else {}
    origin_map: dict[str, str] = {}
    merged: dict[str, str] = {}

    for name in sorted(user_scripts.keys()):
        origin_map[name] = SCRIPT_ORIGIN_USER
        merged[name] = user_scripts[name]

    for name in sorted(doc_scripts.keys()):
        display = document_script_display_name(name)
        origin_map[display] = SCRIPT_ORIGIN_DOCUMENT
        merged[display] = doc_scripts[name]

    analysis_items: list[str] = []
    analysis_section = _analysis_script_section(doc)
    if analysis_section:
        for display_name, code in analysis_section["scripts"].items():
            origin_map[display_name] = SCRIPT_ORIGIN_ANALYSIS
            merged[display_name] = code
            analysis_items.append(display_name)

    sql_items: list[str] = []
    sql_section = _sql_script_section(doc)
    if sql_section:
        for display_name, code in sql_section["scripts"].items():
            origin_map[display_name] = SCRIPT_ORIGIN_SQL
            merged[display_name] = code
            sql_items.append(display_name)

    vision_items: list[str] = []
    vision_section = _vision_script_section(doc)
    if vision_section:
        for display_name, code in vision_section["scripts"].items():
            origin_map[display_name] = SCRIPT_ORIGIN_VISION
            merged[display_name] = code
            vision_items.append(display_name)

    viz_items: list[str] = []
    viz_section = _viz_script_section(doc)
    if viz_section:
        for display_name, code in viz_section["scripts"].items():
            origin_map[display_name] = SCRIPT_ORIGIN_VIZ
            merged[display_name] = code
            viz_items.append(display_name)

    math_items: list[str] = []
    math_section = _math_script_section(doc)
    if math_section:
        for display_name, code in math_section["scripts"].items():
            origin_map[display_name] = SCRIPT_ORIGIN_MATH
            merged[display_name] = code
            math_items.append(display_name)

    units_items: list[str] = []
    units_section = _units_script_section(doc)
    if units_section:
        for display_name, code in units_section["scripts"].items():
            origin_map[display_name] = SCRIPT_ORIGIN_UNITS
            merged[display_name] = code
            units_items.append(display_name)

    quant_items: list[str] = []
    quant_section = _quant_script_section(doc)
    if quant_section:
        for display_name, code in quant_section["scripts"].items():
            origin_map[display_name] = SCRIPT_ORIGIN_QUANT
            merged[display_name] = code
            quant_items.append(display_name)

    optimize_items: list[str] = []
    optimize_section = _optimize_script_section(doc)
    if optimize_section:
        for display_name, code in optimize_section["scripts"].items():
            origin_map[display_name] = SCRIPT_ORIGIN_OPTIMIZE
            merged[display_name] = code
            optimize_items.append(display_name)

    items = (
        sorted(user_scripts.keys())
        + analysis_items
        + sql_items
        + vision_items
        + viz_items
        + math_items
        + units_items
        + quant_items
        + optimize_items
        + [document_script_display_name(n) for n in sorted(doc_scripts.keys())]
    )
    return items, merged, origin_map


def resolve_run_script_selection(
    ctx: Any,
    doc: Any | None,
    saved_scripts: dict[str, str],
) -> tuple[str, str, dict[str, str]]:
    """Return (selected_name, selected_code, merged_scripts) for Run Python Script."""
    from plugin.framework.config import get_config_str
    from plugin.scripting.python_runner import resolve_run_script_name_config_key

    name_config_key = resolve_run_script_name_config_key(doc)
    last_name = get_config_str(ctx, name_config_key)
    names, merged_scripts, _unused_origin_map = build_xdl_script_picker_state(ctx, doc, saved_scripts)
    if not last_name or last_name not in merged_scripts:
        if names:
            last_name = names[0]
        else:
            last_name = ""
    selected_code = merged_scripts.get(last_name, "")
    return last_name, selected_code, merged_scripts


def build_scripts_list_message(
    ctx: Any,
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    status_ok_text: str | None = None,
    status_error_text: str | None = None,
) -> dict[str, Any]:
    from plugin.framework.config import get_config

    user_scripts = get_config(ctx, "saved_python_scripts")
    if not isinstance(user_scripts, dict):
        user_scripts = {}

    doc = session_doc
    if doc is None:
        doc = get_active_document_for_scripts(ctx)

    document_available = doc is not None
    document_stale = False
    document_readonly = is_document_readonly_for_scripts(doc) if doc else False

    if doc is not None and session_doc_url is not None:
        current_id = document_scripts_identity(doc)
        if session_doc_url != current_id:
            document_stale = True
            document_readonly = True

    doc_scripts: dict[str, str] = {}
    if doc is not None and not document_stale:
        doc_scripts = get_document_scripts(doc)

    sections: list[dict[str, Any]] = [
        {"id": SCRIPT_ORIGIN_USER, "title": _("My Scripts"), "scripts": user_scripts},
    ]
    analysis_section = _analysis_script_section(doc)
    if analysis_section:
        sections.append(analysis_section)
    vision_section = _vision_script_section(doc)
    if vision_section:
        sections.append(vision_section)
    viz_section = _viz_script_section(doc)
    if viz_section:
        sections.append(viz_section)
    math_section = _math_script_section(doc)
    if math_section:
        sections.append(math_section)
    units_section = _units_script_section(doc)
    if units_section:
        sections.append(units_section)
    quant_section = _quant_script_section(doc)
    if quant_section:
        sections.append(quant_section)
    optimize_section = _optimize_script_section(doc)
    if optimize_section:
        sections.append(optimize_section)
    sql_section = _sql_script_section(doc)
    if sql_section:
        sections.append(sql_section)
    sections.append({"id": SCRIPT_ORIGIN_DOCUMENT, "title": _("This Document"), "scripts": doc_scripts})

    selected_name, sample_code, _merged_scripts = resolve_run_script_selection(ctx, doc, user_scripts)

    msg: dict[str, Any] = {
        "type": "scripts_list",
        "sections": sections,
        "document_available": document_available,
        "document_readonly": document_readonly,
        "document_stale": document_stale,
        "sample_code": sample_code,
        "selected_script_name": selected_name,
    }
    if status_ok_text:
        msg["status_ok_text"] = status_ok_text
    if status_error_text:
        msg["status_error_text"] = status_error_text
    return msg
