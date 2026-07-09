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

# Origins / display prefixes live in domain_registry; re-export for stable public imports.
from plugin.scripting.domain_registry import (  # noqa: E402, F401
    ANALYSIS_SCRIPT_DISPLAY_PREFIX,
    DOC_SCRIPT_DISPLAY_PREFIX,
    FORECAST_SCRIPT_DISPLAY_PREFIX,
    MATH_SCRIPT_DISPLAY_PREFIX,
    OPTIMIZE_SCRIPT_DISPLAY_PREFIX,
    QUANT_SCRIPT_DISPLAY_PREFIX,
    SCRIPT_ORIGIN_ANALYSIS,
    SCRIPT_ORIGIN_DOCUMENT,
    SCRIPT_ORIGIN_FORECAST,
    SCRIPT_ORIGIN_MATH,
    SCRIPT_ORIGIN_OPTIMIZE,
    SCRIPT_ORIGIN_QUANT,
    SCRIPT_ORIGIN_SQL,
    SCRIPT_ORIGIN_UNITS,
    SCRIPT_ORIGIN_USER,
    SCRIPT_ORIGIN_VISION,
    SCRIPT_ORIGIN_VIZ,
    SQL_SCRIPT_DISPLAY_PREFIX,
    UNITS_SCRIPT_DISPLAY_PREFIX,
    VISION_SCRIPT_DISPLAY_PREFIX,
    VIZ_SCRIPT_DISPLAY_PREFIX,
    get_picker_domains,
    parse_picker_display_name,
    picker_display_name,
)


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
        try:
            comps = desktop.getComponents()
            if comps:
                enum = comps.createEnumeration()
                while enum and enum.hasMoreElements():
                    elem = enum.nextElement()
                    model = None
                    if hasattr(elem, "getURL") and callable(getattr(elem, "getURL")):
                        model = elem
                    elif hasattr(elem, "getController") and elem.getController():
                        model = elem.getController().getModel()
                    if model and is_calc(model):
                        from plugin.framework.thread_guard import guard_uno

                        return guard_uno(model)
        except Exception:
            pass
        return None
    from plugin.framework.thread_guard import guard_uno

    return guard_uno(doc)


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
    return picker_display_name(DOC_SCRIPT_DISPLAY_PREFIX, name)


def parse_document_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(DOC_SCRIPT_DISPLAY_PREFIX, display)


def analysis_script_display_name(name: str) -> str:
    return picker_display_name(ANALYSIS_SCRIPT_DISPLAY_PREFIX, name)


def parse_analysis_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(ANALYSIS_SCRIPT_DISPLAY_PREFIX, display)


def vision_script_display_name(name: str) -> str:
    return picker_display_name(VISION_SCRIPT_DISPLAY_PREFIX, name)


def parse_vision_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(VISION_SCRIPT_DISPLAY_PREFIX, display)


def viz_script_display_name(name: str) -> str:
    return picker_display_name(VIZ_SCRIPT_DISPLAY_PREFIX, name)


def parse_viz_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(VIZ_SCRIPT_DISPLAY_PREFIX, display)


def math_script_display_name(name: str) -> str:
    return picker_display_name(MATH_SCRIPT_DISPLAY_PREFIX, name)


def parse_math_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(MATH_SCRIPT_DISPLAY_PREFIX, display)


def units_script_display_name(name: str) -> str:
    return picker_display_name(UNITS_SCRIPT_DISPLAY_PREFIX, name)


def parse_units_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(UNITS_SCRIPT_DISPLAY_PREFIX, display)


def quant_script_display_name(name: str) -> str:
    return picker_display_name(QUANT_SCRIPT_DISPLAY_PREFIX, name)


def parse_quant_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(QUANT_SCRIPT_DISPLAY_PREFIX, display)


def optimize_script_display_name(name: str) -> str:
    return picker_display_name(OPTIMIZE_SCRIPT_DISPLAY_PREFIX, name)


def parse_optimize_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(OPTIMIZE_SCRIPT_DISPLAY_PREFIX, display)


def forecast_script_display_name(name: str) -> str:
    return picker_display_name(FORECAST_SCRIPT_DISPLAY_PREFIX, name)


def parse_forecast_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(FORECAST_SCRIPT_DISPLAY_PREFIX, display)


def sql_script_display_name(name: str) -> str:
    return picker_display_name(SQL_SCRIPT_DISPLAY_PREFIX, name)


def parse_sql_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(SQL_SCRIPT_DISPLAY_PREFIX, display)


def resolve_script_picker_entry(display_name: str, origin_map: dict[str, str]) -> tuple[str, str]:
    """Return (real_name, origin) for a listbox/display label."""
    origin = origin_map.get(display_name, SCRIPT_ORIGIN_USER)
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        real = parse_document_script_display_name(display_name)
        return (real or display_name, SCRIPT_ORIGIN_DOCUMENT)
    for domain in get_picker_domains():
        if origin == domain.origin:
            real = parse_picker_display_name(domain.display_prefix, display_name)
            return (real or display_name, domain.origin)
    return (display_name, SCRIPT_ORIGIN_USER)


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

    domain_items: list[str] = []
    for domain in get_picker_domains():
        try:
            if not domain.supports(doc):
                continue
        except Exception:
            continue
        try:
            templates = domain.templates()
        except Exception:
            log.debug("picker templates failed for %s", domain.origin, exc_info=True)
            continue
        for name, code in templates.items():
            display_name = picker_display_name(domain.display_prefix, name)
            origin_map[display_name] = domain.origin
            merged[display_name] = code
            domain_items.append(display_name)

    items = (
        sorted(user_scripts.keys())
        + domain_items
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
    last_name = get_config_str(name_config_key)
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

    user_scripts = get_config("saved_python_scripts")
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
    for domain in get_picker_domains():
        try:
            if not domain.supports(doc):
                continue
        except Exception:
            continue
        try:
            templates = domain.templates()
        except Exception:
            log.debug("scripts_list templates failed for %s", domain.origin, exc_info=True)
            continue
        display_scripts = {
            picker_display_name(domain.display_prefix, name): code for name, code in templates.items()
        }
        sections.append({"id": domain.origin, "title": domain.title_fn(), "scripts": display_scripts})
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
