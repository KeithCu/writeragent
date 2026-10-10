# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Document-attached named Python scripts and Calc workbook init scripts (UserDefinedProperties).

Venv code can import library defs via ``wa.scripts.<Name>`` (My Scripts) and
``wa.doc.<Name>`` (This Document). See ``plugin.scripting.named_scripts``.
``run_venv_python_script`` stays blocked (would re-enter the warm worker).
"""

# =========================================================================================
# WARNING: PARITY INVARIANT WITH MONACO JAVASCRIPT FRONTEND
# If you modify script sections, envelope structures, or script list IPC payloads here,
# you MUST also update the corresponding JavaScript / Python consumers:
#   - JS Script Manager:        plugin/contrib/scripting/assets/editor/scripts_manager.js
#   - Python UI Dialog:         plugin/scripting/python_runner_ui.py
#   - IPC Host Bridge:          plugin/scripting/editor_host.py
# =========================================================================================

from __future__ import annotations

from enum import Enum
import hashlib
import json
import logging
from typing import Any, Callable

from plugin.doc.doc_type import is_calc, is_draw, is_writer
from plugin.doc.udprops import get_document_property, set_document_property
from plugin.framework import config
from plugin.framework.errors import DocumentDisposedError
from plugin.framework.i18n import _
from plugin.framework.json_utils import safe_json_loads
from plugin.framework.uno_context import get_active_document, get_desktop, normalize_doc_url
from plugin.scripting.domain_registry import (
    DOC_SCRIPT_DISPLAY_PREFIX,
    SCRIPT_ORIGIN_DOCUMENT,
    SCRIPT_ORIGIN_USER,
    get_picker_domains,
    parse_picker_display_name,
    picker_display_name,
)
from plugin.scripting.session_manager import calc_init_session_id

log = logging.getLogger(__name__)

DOCUMENT_SCRIPTS_UDPROP = "WriterAgentDocumentPythonScripts"
_MAX_DOCUMENT_SCRIPTS_BYTES = 900_000
_SOFT_WARN_SCRIPT_BYTES = 200_000
_ENVELOPE_VERSION = 1
# Workbook init lives in the same map as named document scripts. The picker and
# wa.doc must not expose it; deleting "[Doc] INIT" used to wipe the init script.
_CALC_INIT_SCRIPT_NAMES = frozenset({"INIT", "Init"})


class DocumentScriptErrorCode(str, Enum):
    """Distinguishable error codes for document and user script persistence."""

    NO_DOCUMENT = "no_document"
    TOO_LARGE = "too_large"
    READONLY = "readonly"
    RESERVED_NAME = "reserved_name"
    ALREADY_EXISTS = "already_exists"
    EMPTY_NAME = "empty_name"
    NOT_FOUND = "not_found"


class DocumentScriptError(str):
    """String subclass carrying a machine-readable error code."""

    code: DocumentScriptErrorCode

    def __new__(cls, message: str, code: DocumentScriptErrorCode) -> DocumentScriptError:
        obj = super().__new__(cls, message)
        obj.code = code
        return obj


def document_scripts_identity(doc: Any) -> str:
    """Stable identity for stale detection (normalized URL or empty for untitled)."""
    try:
        if hasattr(doc, "getURL"):
            return normalize_doc_url(doc.getURL() or "")
    except Exception:
        log.debug("document_scripts_identity failed", exc_info=True)
    return ""


def get_active_document_for_scripts(ctx: Any) -> Any | None:
    """Active Writer, Calc, or Draw model.

    Uses ``get_active_document`` so a disposed document is not reported as
    Start Center (``None``). ``None`` remains the answer when nothing is open
    or the active component is another type.
    """
    doc = get_active_document(ctx)
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
    return json.dumps(
        {"version": _ENVELOPE_VERSION, "scripts": scripts},
        separators=(",", ":"),
        ensure_ascii=False,
    )


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


def _check_payload_size(scripts: dict[str, str]) -> DocumentScriptError | None:
    encoded = _envelope_to_json(scripts).encode("utf-8")
    if len(encoded) > _MAX_DOCUMENT_SCRIPTS_BYTES:
        return DocumentScriptError(
            _("Document scripts are too large to store in the document ({0} bytes).").format(len(encoded)),
            DocumentScriptErrorCode.TOO_LARGE,
        )
    for name, code in scripts.items():
        nbytes = len((code or "").encode("utf-8"))
        if nbytes > _SOFT_WARN_SCRIPT_BYTES:
            log.warning("document_scripts: script %r is large (%d bytes)", name, nbytes)
    return None


def set_document_scripts(doc: Any, scripts: dict[str, str]) -> DocumentScriptError | None:
    if doc is None:
        return DocumentScriptError(
            _("No document is open to save scripts."),
            DocumentScriptErrorCode.NO_DOCUMENT,
        )
    err = _check_payload_size(scripts)
    if err:
        return err
    if is_document_readonly_for_scripts(doc):
        # The previous sentence claimed a My Scripts write that only two
        # callers perform. Save As, New, and Monaco attach show this string
        # and do not write My Scripts.
        return DocumentScriptError(
            _("Document is read-only or properties cannot be written."),
            DocumentScriptErrorCode.READONLY,
        )
    try:
        payload = _envelope_to_json(scripts)
        set_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, payload)
    except DocumentDisposedError:
        raise
    except Exception as exc:
        # A disposed document is not "read-only". Callers that swallow the
        # message used to keep writing as if the file were still open.
        from plugin.framework.errors import is_disposed_exception

        if is_disposed_exception(exc):
            raise
        log.exception("document_scripts: failed to persist on document")
        # Same false My Scripts claim as the read-only return above.
        return DocumentScriptError(
            _("Document is read-only or properties cannot be written."),
            DocumentScriptErrorCode.READONLY,
        )
    # set_document_property returns None after a real write and also when the
    # document has no UserDefinedProperties bag (that path does not raise).
    # The missing bag used to look like success while the next read was empty.
    stored = get_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, default=None)
    if stored != payload:
        log.error("document_scripts: persist did not store %s", DOCUMENT_SCRIPTS_UDPROP)
        return DocumentScriptError(
            _("Document properties cannot be written (missing or unwriteable UserDefinedProperties)."),
            DocumentScriptErrorCode.READONLY,
        )
    return None


def get_calc_init_script(doc: Any, *, default: str = "") -> str:
    """Return the workbook init script on *doc*, checking INIT first then Init."""
    scripts = get_document_scripts(doc)
    # ``scripts.get("INIT") or scripts.get("Init")`` treats an empty "INIT"
    # as missing and falls through to a stale "Init". Check key presence so
    # an explicit empty "INIT" is kept.
    if "INIT" in scripts:
        return scripts["INIT"]
    if "Init" in scripts:
        return scripts["Init"]
    return default


def set_calc_init_script(doc: Any, code: str) -> DocumentScriptError | None:
    """Persist init script on *doc* under the name 'INIT' in document scripts."""
    scripts = dict(get_document_scripts(doc))
    # If both 'INIT' and 'Init' exist, a write to 'Init' while reads prefer
    # 'INIT' makes saves look like no-ops. Drop both keys and store one 'INIT'.
    scripts.pop("INIT", None)
    scripts.pop("Init", None)
    if code:
        scripts["INIT"] = code
    # =PY() reads the init script from the calling document on each UI-thread
    # evaluation, so there is no host-side cache to refresh here.
    return set_document_scripts(doc, scripts)


def _enumerate_calc_documents(desktop: Any) -> list[Any]:
    """Calc models from desktop.getComponents(), in enumeration order."""
    matches: list[Any] = []
    try:
        comps = desktop.getComponents()
        if not comps:
            return matches
        enum = comps.createEnumeration()
        if not enum:
            return matches
        while True:
            try:
                has_more = enum.hasMoreElements()
            except Exception:
                break
            if not has_more:
                break
            try:
                elem = enum.nextElement()
            except Exception:
                continue
            try:
                model = None
                if hasattr(elem, "getURL") and callable(getattr(elem, "getURL")):
                    model = elem
                elif hasattr(elem, "getController") and elem.getController():
                    model = elem.getController().getModel()
                if model and is_calc(model):
                    matches.append(model)
            except Exception:
                # Catch per element so one disposed or broken element does not abort enumeration
                log.debug("document_scripts: skipping invalid component during enumeration", exc_info=True)
                continue
    except Exception:
        log.debug("document_scripts: Calc component enumeration failed", exc_info=True)
    return matches


def _select_enumerated_calc_document(matches: list[Any]) -> Any | None:
    """The only open Calc model, or None when there are zero or several.

    UNO component order is not the focused file, so with several workbooks
    open no enumerated match is safe to pick.
    """
    if len(matches) == 1:
        from plugin.framework.thread_guard import guard_uno

        return guard_uno(matches[0])
    return None


def get_calc_document_from_ctx(ctx: Any) -> Any | None:
    try:
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
    except Exception:
        log.debug("document_scripts: could not resolve active Calc document", exc_info=True)
        return None
    if doc is not None and (is_writer(doc) or is_draw(doc)):
        # With Writer focused and a Calc file open, enumerating the desktop
        # bound that Calc. The Python deck already refuses this.
        return None
    if doc is None or not is_calc(doc):
        return _select_enumerated_calc_document(_enumerate_calc_documents(desktop))
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


def attach_document_script(
    doc: Any,
    name: str,
    code: str,
    *,
    overwrite: bool = False,
) -> DocumentScriptError | None:
    name = (name or "").strip()
    if not name:
        return DocumentScriptError(
            _("Script name cannot be empty."),
            DocumentScriptErrorCode.EMPTY_NAME,
        )
    from plugin.scripting.domain_registry import is_reserved_script_name

    # Reject names that collide with display prefixes (e.g. "[Doc] ", "[Vision] ")
    if is_reserved_script_name(name):
        return DocumentScriptError(
            _("'{0}' starts with a reserved prefix.").format(name),
            DocumentScriptErrorCode.RESERVED_NAME,
        )
    # INIT is the hidden workbook init script in this same property map.
    # Saving it overwrites that body, and the picker hides the row.
    if is_calc_init_script_name(name):
        return DocumentScriptError(
            _("'{0}' is reserved for the workbook init script.").format(name),
            DocumentScriptErrorCode.RESERVED_NAME,
        )
    scripts = dict(get_document_scripts(doc))
    if name in scripts and not overwrite:
        return DocumentScriptError(
            _("A script named '{0}' already exists in this document.").format(name),
            DocumentScriptErrorCode.ALREADY_EXISTS,
        )
    scripts[name] = code if code is not None else ""
    return set_document_scripts(doc, scripts)


def delete_document_script(doc: Any, name: str) -> DocumentScriptError | None:
    # INIT shares this property map with named scripts. The picker hides it,
    # but deleting the storage name used to pop the entry and wipe the
    # workbook init script. attach_document_script already rejects this name.
    if is_calc_init_script_name(name):
        return DocumentScriptError(
            _("'{0}' is reserved for the workbook init script.").format(name),
            DocumentScriptErrorCode.RESERVED_NAME,
        )
    scripts = dict(get_document_scripts(doc))
    # Deleting a script that is not there must not report success or rewrite
    # document properties. Return NOT_FOUND and skip set_document_scripts.
    if name not in scripts:
        return DocumentScriptError(
            _("Script '{0}' does not exist in this document.").format(name),
            DocumentScriptErrorCode.NOT_FOUND,
        )
    del scripts[name]
    return set_document_scripts(doc, scripts)


def save_document_script(doc: Any, name: str, code: str) -> DocumentScriptError | None:
    return attach_document_script(doc, name, code, overwrite=True)


def document_script_display_name(name: str) -> str:
    return picker_display_name(DOC_SCRIPT_DISPLAY_PREFIX, name)


def parse_document_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(DOC_SCRIPT_DISPLAY_PREFIX, display)


def is_calc_init_script_name(name: str) -> bool:
    """True for the workbook init entry stored beside named document scripts."""
    return name in _CALC_INIT_SCRIPT_NAMES


def script_origin_is_library(origin: str) -> bool:
    """True for My Scripts and this-document rows. Builtin templates are read-only."""
    return origin in ("", SCRIPT_ORIGIN_USER, SCRIPT_ORIGIN_DOCUMENT)


def document_scripts_write_is_stale(session_doc: Any | None, session_doc_url: str | None) -> bool:
    """True when the document opened with the editor is no longer that same file.

    The picker used to save onto whichever window was focused. Writes stay on
    the launch document, and are refused when that document's identity changed.
    """
    if session_doc is None or session_doc_url is None:
        return False
    # An untitled file captures "". After File → Save the same component has
    # a URL. That is not a different document. Go stale only when a non-empty
    # captured URL no longer matches.
    if session_doc_url == "":
        return False
    return document_scripts_identity(session_doc) != session_doc_url


def picker_document_scripts(scripts: dict[str, str]) -> dict[str, str]:
    """Document scripts shown in the picker and ``wa.doc`` (init script omitted)."""
    return {name: code for name, code in scripts.items() if not is_calc_init_script_name(name)}


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


def _domain_template_entries(doc: Any | None) -> list[tuple[Any, dict[str, str]]]:
    """Return [(domain, {display_name: code})] for picker domains supporting *doc*."""
    results: list[tuple[Any, dict[str, str]]] = []
    for domain in get_picker_domains():
        try:
            if not domain.supports(doc):
                continue
        except Exception:
            # Swallowed exception logged per AGENTS.md / cleanups
            log.debug("domain %s supports() check failed", domain.origin, exc_info=True)
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


def build_xdl_script_picker_state(
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

    for name in sorted(picker_document_scripts(doc_scripts)):
        display = document_script_display_name(name)
        origin_map[display] = SCRIPT_ORIGIN_DOCUMENT
        merged[display] = doc_scripts[name]

    domain_items: list[str] = []
    for domain, display_scripts in _domain_template_entries(doc):
        for display_name, code in display_scripts.items():
            origin_map[display_name] = domain.origin
            merged[display_name] = code
            domain_items.append(display_name)

    items = (
        sorted(user_scripts.keys())
        + [document_script_display_name(n) for n in sorted(picker_document_scripts(doc_scripts))]
        + domain_items
    )
    return items, merged, origin_map


def resolve_run_script_selection(
    ctx: Any,
    doc: Any | None,
    saved_scripts: dict[str, str],
    *,
    picker_state: tuple[list[str], dict[str, str], dict[str, str]] | None = None,
) -> tuple[str, str, dict[str, str]]:
    """Return (selected_name, selected_code, merged_scripts) for Run Python Script."""
    from plugin.scripting.python_runner import resolve_run_script_name_config_key

    name_config_key = resolve_run_script_name_config_key(doc)
    last_name = config.get_config_str(name_config_key)
    if picker_state is not None:
        names, merged_scripts, _unused_origin_map = picker_state
    else:
        names, merged_scripts, _unused_origin_map = build_xdl_script_picker_state(doc, saved_scripts)
    # A raw document name from an older list still selects that script when
    # the picker key is the [Doc] display name and no user script uses the raw name.
    if last_name and last_name not in merged_scripts:
        display = document_script_display_name(last_name)
        if display in merged_scripts:
            last_name = display
    if not last_name or last_name not in merged_scripts:
        if names:
            last_name = names[0]
        else:
            last_name = ""
        if last_name:
            config.set_config(name_config_key, last_name)
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
    user_scripts = get_user_scripts()

    doc = session_doc
    if doc is None:
        doc = get_active_document_for_scripts(ctx)

    document_available = doc is not None
    document_stale = False
    document_readonly = is_document_readonly_for_scripts(doc) if doc else False

    # Same predicate as save/attach/delete. An untitled capture of "" becomes
    # a file URL on File -> Save; that is not a different document. Pass doc,
    # not session_doc: when session_doc is None this function already
    # substituted the active document, and None would hide a real URL change.
    if document_scripts_write_is_stale(doc, session_doc_url):
        document_stale = True
        document_readonly = True

    doc_scripts: dict[str, str] = {}
    if doc is not None and not document_stale:
        # Same keys as build_xdl_script_picker_state. Raw names collided with
        # My Scripts and did not match selected_script_name ([Doc] …), so
        # reopen loaded the wrong row and Save wrote a different script.
        # scripts_manager.js documentScriptListKey must probe these keys for
        # the New / Save As overwrite check. The typed name is not a key.
        doc_scripts = {
            document_script_display_name(name): code
            for name, code in picker_document_scripts(get_document_scripts(doc)).items()
        }

    sections: list[dict[str, Any]] = [
        {"id": SCRIPT_ORIGIN_USER, "title": _("My Scripts"), "scripts": user_scripts},
        {"id": SCRIPT_ORIGIN_DOCUMENT, "title": _("This Document"), "scripts": doc_scripts},
    ]

    domain_entries = _domain_template_entries(doc)
    for domain, display_scripts in domain_entries:
        sections.append({
            "id": domain.origin,
            "title": domain.title_fn(),
            "scripts": display_scripts,
        })

    # A stale document must not be passed to resolve_run_script_selection.
    # That returned a '[Doc] X' selection missing from sections and wrote a
    # selection config entry against the stale doc. Pass None so selection
    # only considers user scripts and templates.
    selection_doc = None if document_stale else doc
    selected_name, _selected_code, _merged_scripts = resolve_run_script_selection(
        ctx, selection_doc, user_scripts
    )

    msg: dict[str, Any] = {
        "type": "scripts_list",
        "sections": sections,
        "document_available": document_available,
        "document_readonly": document_readonly,
        "document_stale": document_stale,
        "selected_script_name": selected_name,
    }
    if status_ok_text:
        msg["status_ok_text"] = status_ok_text
    if status_error_text:
        msg["status_error_text"] = status_error_text
    return msg


SCRIPT_PICKER_MESSAGE_TYPES = frozenset(
    {
        "request_scripts",
        "select_script",
        "save_script",
        "attach_script",
        "copy_script_to_user",
        "delete_script",
    }
)


def _script_code_from_message(msg: dict[str, Any]) -> str:
    raw = msg.get("code", "")
    return raw if isinstance(raw, str) else ""


def _document_script_storage_name(name: str) -> str:
    """Property key for a picker label. ``[Doc] Regional`` stores as ``Regional``."""
    return parse_document_script_display_name(name) or name


def get_user_scripts() -> dict[str, str]:
    scripts = config.get_config("saved_python_scripts")
    if not isinstance(scripts, dict):
        return {}
    return dict(scripts)


def save_user_script(name: str, code: str) -> str | None:
    from plugin.scripting.domain_registry import is_reserved_script_name

    # Names starting with a reserved prefix such as "[Doc] " collide with
    # document-script display keys. Reject them with ValueError.
    if is_reserved_script_name(name):
        raise ValueError(f"Script name {name!r} starts with a reserved prefix")
    scripts = get_user_scripts()
    scripts[name] = code
    config.set_config("saved_python_scripts", scripts)
    return None


def delete_user_script(name: str) -> DocumentScriptError | None:
    scripts = get_user_scripts()
    # Deleting a user script that is not there must not report success or
    # rewrite config. Return NOT_FOUND and skip set_config.
    if name not in scripts:
        return DocumentScriptError(
            _("Script '{0}' does not exist in My Scripts.").format(name),
            DocumentScriptErrorCode.NOT_FOUND,
        )
    del scripts[name]
    config.set_config("saved_python_scripts", scripts)
    return None


def is_picker_template_name(name: str) -> bool:
    """True for a read-only built-in picker label such as ``[Vision] …``."""
    return any(parse_picker_display_name(domain.display_prefix, name) for domain in get_picker_domains())


def save_selected_script(
    doc: Any | None,
    display_name: str,
    code: str,
    *,
    allow_builtin_skip: bool = False,
    is_builtin_fn: Callable[[str], bool] | None = None,
) -> str | None:
    """Save *code* to user scripts or *doc* matching *display_name*.

    Returns None on success or when *display_name* is a built-in template and
    *allow_builtin_skip* is True. Returns an error message string otherwise.
    """
    if not display_name:
        return None
    user_scripts = get_user_scripts()
    if display_name in user_scripts:
        save_err = save_user_script(display_name, code)
        if isinstance(save_err, str) and save_err:
            return save_err
        return None
    doc_scripts = get_document_scripts(doc) if doc is not None else {}
    real_doc_name = parse_document_script_display_name(display_name) or display_name
    if real_doc_name in doc_scripts:
        return save_document_script(doc, real_doc_name, code)
    check_builtin = is_builtin_fn if is_builtin_fn is not None else is_picker_template_name
    if check_builtin(display_name):
        if allow_builtin_skip:
            return None
        return _("Built-in helpers are read-only. Use Copy to My Scripts to customize.")
    return _("Script '{0}' is not in My Scripts or this document, so it was not saved.").format(display_name)


def _closed_document_scripts_list(status_error_text: str) -> dict[str, Any]:
    """Script list when the document is already gone and cannot be read again."""
    return {
        "type": "scripts_list",
        "sections": [
            {"id": SCRIPT_ORIGIN_USER, "title": _("My Scripts"), "scripts": {}},
            {"id": SCRIPT_ORIGIN_DOCUMENT, "title": _("This Document"), "scripts": {}},
        ],
        "document_available": False,
        "document_readonly": True,
        "document_stale": True,
        "selected_script_name": "",
        "status_error_text": status_error_text,
    }


def _require_writable_session(
    session_doc: Any | None,
    session_doc_url: str | None,
    *,
    action_label: str = "",
) -> str | None:
    """Validate that the session document is open and not stale. Return error text or None."""
    if session_doc is None:
        if action_label == "save":
            return _("No document is open to save scripts.")
        if action_label == "attach":
            return _("No document is open to attach scripts.")
        return _("No document is open.")
    if document_scripts_write_is_stale(session_doc, session_doc_url):
        return _("Document changed — close and reopen Run Python Script to edit document scripts.")
    return None


def _remember_selection(session_doc: Any | None, name: str) -> None:
    from plugin.scripting.python_runner import resolve_run_script_name_config_key

    name_config_key = resolve_run_script_name_config_key(session_doc)
    config.set_config(name_config_key, name)


def _on_request_scripts(
    msg: dict[str, Any],
    *,
    send_list: Callable[..., None],
    **_unused: Any,
) -> bool:
    log.info("scripts picker: request_scripts")
    send_list()
    return True


def _on_select_script(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    **_unused: Any,
) -> bool:
    name = str(msg.get("name", "") or "").strip()
    _remember_selection(session_doc, name)
    return True


def _on_save_script(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
    **_unused: Any,
) -> bool:
    name = str(msg.get("name", "") or "").strip()
    script_code = _script_code_from_message(msg)
    origin = str(msg.get("origin", "") or "").strip()
    if not name:
        send_list(status_error_text=_("Script name cannot be empty."))
        return True
    if not script_origin_is_library(origin):
        send_list(
            status_error_text=_(
                "Built-in helpers are read-only. Use Copy to My Scripts to customize."
            )
        )
        return True
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        err = _require_writable_session(session_doc, session_doc_url, action_label="save")
        if err:
            send_list(status_error_text=err)
            return True
        storage_name = _document_script_storage_name(name)
        save_err = save_document_script(session_doc, storage_name, script_code)
        if save_err:
            # Fall back to My Scripts only for READONLY (read-only document or
            # unwritable properties). Other save errors, including 'INIT' and
            # too-large, go back to the user. A blanket fallback skipped validation.
            if getattr(save_err, "code", None) == DocumentScriptErrorCode.READONLY:
                if storage_name in get_user_scripts() and not bool(msg.get("overwrite")):
                    send_list(
                        status_error_text=_(
                            "A script named '{0}' already exists in My Scripts. {1}"
                        ).format(storage_name, save_err)
                    )
                    return True
                save_user_script(storage_name, script_code)
                _remember_selection(session_doc, storage_name)
                send_list(
                    status_ok_text=_("Saved script '{0}' to My Scripts. {1}").format(storage_name, save_err),
                )
                return True
            send_list(status_error_text=str(save_err))
            return True
        display_name = document_script_display_name(storage_name)
        _remember_selection(session_doc, display_name)
        send_list(status_ok_text=_("Saved script '{0}' to this document.").format(storage_name))
        return True
    save_user_script(name, script_code)
    _remember_selection(session_doc, name)
    log.info("scripts picker: save_script '%s' (user)", name)
    send_list(status_ok_text=_("Saved script '{0}'.").format(name))
    return True


def _on_attach_script(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
    **_unused: Any,
) -> bool:
    name = str(msg.get("name", "") or "").strip()
    script_code = _script_code_from_message(msg)
    overwrite = bool(msg.get("overwrite"))
    err = _require_writable_session(session_doc, session_doc_url, action_label="attach")
    if err:
        send_list(status_error_text=err)
        return True
    storage_name = _document_script_storage_name(name)
    attach_err = attach_document_script(session_doc, storage_name, script_code, overwrite=overwrite)
    if attach_err:
        send_list(status_error_text=str(attach_err))
        return True
    send_list(status_ok_text=_("Attached script '{0}' to this document.").format(storage_name))
    return True


def _on_copy_script_to_user(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    send_list: Callable[..., None],
    **_unused: Any,
) -> bool:
    name = str(msg.get("name", "") or "").strip()
    script_code = _script_code_from_message(msg)
    overwrite = bool(msg.get("overwrite"))
    if not name:
        send_list(status_error_text=_("Script name cannot be empty."))
        return True
    scripts = get_user_scripts()
    if name in scripts and not overwrite:
        send_list(
            status_error_text=_("A script named '{0}' already exists in My Scripts.").format(name)
        )
        return True
    save_user_script(name, script_code)
    _remember_selection(session_doc, name)
    send_list(status_ok_text=_("Copied script '{0}' to My Scripts.").format(name))
    return True


def _on_delete_script(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
    **_unused: Any,
) -> bool:
    name = str(msg.get("name", "") or "").strip()
    origin = str(msg.get("origin", "") or "").strip()
    if not name:
        send_list(status_error_text=_("Script name cannot be empty."))
        return True
    if not script_origin_is_library(origin):
        send_list(
            status_error_text=_(
                "Built-in helpers are read-only. Use Copy to My Scripts to customize."
            )
        )
        return True
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        err = _require_writable_session(session_doc, session_doc_url)
        if err:
            send_list(status_error_text=err)
            return True
        storage_name = _document_script_storage_name(name)
        del_err = delete_document_script(session_doc, storage_name)
        if del_err:
            send_list(status_error_text=str(del_err))
            return True
        send_list(status_ok_text=_("Deleted document script '{0}'.").format(storage_name))
        return True
    del_err = delete_user_script(name)
    if del_err:
        send_list(status_error_text=str(del_err))
        return True
    log.info("scripts picker: delete_script '%s' (user)", name)
    send_list(status_ok_text=_("Deleted script '{0}'.").format(name))
    return True


_PICKER_HANDLERS: dict[str, Callable[..., bool]] = {
    "request_scripts": _on_request_scripts,
    "select_script": _on_select_script,
    "save_script": _on_save_script,
    "attach_script": _on_attach_script,
    "copy_script_to_user": _on_copy_script_to_user,
    "delete_script": _on_delete_script,
}


def _apply_script_picker_message(
    kind: str,
    msg: dict[str, Any],
    *,
    ctx: Any,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
) -> bool:
    """Picker message body. Disposal is caught by ``handle_editor_script_message``."""
    handler = _PICKER_HANDLERS.get(kind)
    if handler is None:
        return False
    return handler(
        msg,
        ctx=ctx,
        session_doc=session_doc,
        session_doc_url=session_doc_url,
        send_list=send_list,
    )


def handle_editor_script_message(
    kind: str,
    msg: dict[str, Any],
    *,
    ctx: Any,
    session_doc: Any | None,
    session_doc_url: str | None,
    send: Callable[[dict[str, Any]], None],
) -> bool:
    """Apply a Monaco script-picker IPC message. Return True if *kind* was handled."""

    def _send_list(*, status_ok_text: str | None = None, status_error_text: str | None = None) -> None:
        send(
            build_scripts_list_message(
                ctx,
                session_doc=session_doc,
                session_doc_url=session_doc_url,
                status_ok_text=status_ok_text,
                status_error_text=status_error_text,
            )
        )

    try:
        return _apply_script_picker_message(
            kind,
            msg,
            ctx=ctx,
            session_doc=session_doc,
            session_doc_url=session_doc_url,
            send_list=_send_list,
        )
    except DocumentDisposedError:
        # set_document_scripts and get_active_document_for_scripts re-raise
        # DocumentDisposedError. An uncaught raise looks like a dead child to
        # the pipe reader, which terminate()s Monaco. Save/close in editor_host
        # swallow handler failures and answer the webview, so disposal becomes
        # a script-list error and this message stays handled. A second disposal
        # while rebuilding the list uses a payload that does not touch the document.
        if kind not in SCRIPT_PICKER_MESSAGE_TYPES:
            return False
        log.exception("scripts picker: document disposed during %s", kind)
        closed = _("The document was closed.")
        try:
            _send_list(status_error_text=closed)
        except DocumentDisposedError:
            log.exception("scripts picker: list refresh failed after the document closed")
            send(_closed_document_scripts_list(closed))
        return True
