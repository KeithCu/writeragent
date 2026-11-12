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
import json
import logging
from typing import Any, Callable

from plugin.framework.doc_type import is_calc, is_draw, is_writer
from plugin.doc.udprops import get_document_property, set_document_property
from plugin.framework import config
from plugin.framework.errors import DocumentDisposedError, is_disposal
from plugin.framework.i18n import _
from plugin.framework.uno_context import enumeration_should_continue, get_desktop, normalize_doc_url
from plugin.scripting.domain_registry import (
    DOC_SCRIPT_DISPLAY_PREFIX,
    PICKER_WIRING,
    SCRIPT_ORIGIN_DOCUMENT,
    SCRIPT_ORIGIN_USER,
    domain_template_entries,
    is_reserved_script_name,
    parse_picker_display_name,
    picker_display_name,
)
from plugin.scripting.named_scripts import script_body_hash
from plugin.scripting.session_manager import calc_init_session_id

log = logging.getLogger(__name__)

DOCUMENT_SCRIPTS_UDPROP = "WriterAgentDocumentPythonScripts"
_MAX_DOCUMENT_SCRIPTS_BYTES = 900_000
_ENVELOPE_VERSION = 1
# Workbook init lives in the same map as named document scripts. The picker and
# wa.doc must not expose INIT; deleting that entry would clear the workbook init script stored in the same map.
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
    UNREADABLE = "unreadable"


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


def is_document_readonly_for_scripts(doc: Any) -> bool:
    if doc is None:
        return True
    try:
        if hasattr(doc, "isReadonly") and doc.isReadonly():
            return True
    except Exception:
        log.debug("document_scripts: isReadonly check failed", exc_info=True)
    return False


def _fail_unreadable(message: str, *args: object, exc_info: bool = False) -> tuple[dict[str, str], DocumentScriptError]:
    log.warning(message, *args, exc_info=exc_info)
    return {}, DocumentScriptError(
        _("Stored document scripts are unreadable, so they were left unchanged."),
        DocumentScriptErrorCode.UNREADABLE,
    )


def _readonly_document_error() -> DocumentScriptError:
    """Document is read-only or properties cannot be written.

    No saver falls back to My Scripts on this error; the native dialog,
    the Monaco picker and Monaco Run / Save show this sentence and write
    nothing (see save_script_to_origin).
    """
    return DocumentScriptError(
        _("Document is read-only or properties cannot be written."),
        DocumentScriptErrorCode.READONLY,
    )


def _load_document_script_map(doc: Any) -> tuple[dict[str, str], DocumentScriptError | None]:
    if doc is None:
        return {}, None
    raw = get_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, default=None)
    if raw is None:
        return {}, None
    text = str(raw).strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return _fail_unreadable("document_scripts: failed to parse JSON envelope", exc_info=True)
    if not isinstance(parsed, dict):
        return _fail_unreadable("document_scripts: expected object, got %s", type(parsed).__name__)
    if "version" not in parsed:
        return _fail_unreadable("document_scripts: missing version")
    if parsed.get("version") != _ENVELOPE_VERSION:
        version = parsed.get("version")
        log.warning("document_scripts: unsupported version %r", version)
        return {}, DocumentScriptError(
            _("Stored document scripts are in an unsupported format (version {0}), so they were left unchanged.").format(
                version
            ),
            DocumentScriptErrorCode.UNREADABLE,
        )
    scripts_raw = parsed.get("scripts")
    if not isinstance(scripts_raw, dict):
        return _fail_unreadable("document_scripts: missing scripts map")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in scripts_raw.items()):
        return _fail_unreadable("document_scripts: non-string script entry")
    return dict(scripts_raw), None


def get_document_scripts(doc: Any) -> dict[str, str]:
    scripts, _err = _load_document_script_map(doc)
    return scripts


def set_document_scripts(doc: Any, scripts: dict[str, str]) -> DocumentScriptError | None:
    if doc is None:
        return DocumentScriptError(
            _("No document is open to save scripts."),
            DocumentScriptErrorCode.NO_DOCUMENT,
        )
    payload = json.dumps(
        {"version": _ENVELOPE_VERSION, "scripts": scripts},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    nbytes = len(payload.encode("utf-8"))
    if nbytes > _MAX_DOCUMENT_SCRIPTS_BYTES:
        return DocumentScriptError(
            _("Document scripts are too large to store in the document ({0} bytes).").format(nbytes),
            DocumentScriptErrorCode.TOO_LARGE,
        )
    if is_document_readonly_for_scripts(doc):
        # No saver falls back to My Scripts on this error; every saver reports it and writes nothing.
        return _readonly_document_error()
    try:
        set_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, payload)
    except Exception as exc:
        # A disposed document is not "read-only": re-raise so callers stop
        # saving to a closed document instead of showing a read-only error.
        if is_disposal(exc):
            raise
        log.exception("document_scripts: failed to persist on document")
        # Any other write failure reports the same read-only error.
        return _readonly_document_error()
    # set_document_property returns None after a real write and also when the
    # document has no UserDefinedProperties bag (that path does not raise).
    # Verify the property round-trips so a missing UserDefinedProperties bag does not look like a successful save.
    stored = get_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, default=None)
    if stored != payload:
        log.error("document_scripts: persist did not store %s", DOCUMENT_SCRIPTS_UDPROP)
        return DocumentScriptError(
            _("Document properties cannot be written (missing or unwriteable UserDefinedProperties)."),
            DocumentScriptErrorCode.READONLY,
        )
    return None


def get_calc_init_script(doc: Any) -> str:
    """Return the workbook init script on *doc*, checking INIT first then Init."""
    scripts = get_document_scripts(doc)
    # .get keeps an explicit empty "INIT". `or` would treat "" as missing and return a stale "Init".
    return scripts.get("INIT", scripts.get("Init", ""))


def set_calc_init_script(doc: Any, code: str) -> DocumentScriptError | None:
    """Persist init script on *doc* under the name 'INIT' in document scripts."""
    scripts, err = _load_document_script_map(doc)
    if err:
        return err
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
        # enumeration_should_continue caps desktop enumeration so a broken nextElement() cannot spin the UI thread forever.
        seen = 0
        while True:
            if not enumeration_should_continue(enum, seen, label="document_scripts"):
                break
            seen += 1
            try:
                elem = enum.nextElement()
            except Exception:
                log.debug("document_scripts: nextElement failed; trying later components", exc_info=True)
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


def get_calc_document_from_ctx(ctx: Any) -> Any | None:
    from plugin.framework.thread_guard import guard_uno

    try:
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
    except Exception:
        log.debug("document_scripts: could not resolve active Calc document", exc_info=True)
        return None
    if doc is not None and (is_writer(doc) or is_draw(doc)):
        # Writer and Impress (is_draw) skip Calc init binding when focused; Base/Math still enumerate Calc documents.
        return None
    if doc is None or not is_calc(doc):
        matches = _enumerate_calc_documents(desktop)
        # UNO order is not the focused file, so 0 or several workbooks is not a safe pick.
        if len(matches) != 1:
            return None
        return guard_uno(matches[0])
    return guard_uno(doc)


def build_python_eval_init_kwargs(doc: Any) -> dict[str, Any]:
    """Kwargs for ``run_code_in_user_venv`` init execution (pass with separate ``session_id=``)."""
    init_code = get_calc_init_script(doc).strip()
    if not init_code:
        return {}
    return {
        "init_script": init_code,
        "init_session_id": calc_init_session_id(doc),
        "init_script_hash": script_body_hash(init_code),
    }


def new_script_name_error(name: str, *, attach: bool) -> DocumentScriptError | None:
    """Storage sentence for a stripped *name*, or None when that name is allowed.

    Does not strip. A whitespace-only string is not empty; callers strip.
    ``INIT`` and ``Init`` are the workbook init keys in the document property
    map, so they are rejected only when *attach* is true. The same spellings
    are legal My Scripts names. ``init`` is not in ``_CALC_INIT_SCRIPT_NAMES``.
    """
    if not name:
        return DocumentScriptError(_("Script name cannot be empty."), DocumentScriptErrorCode.EMPTY_NAME)
    if is_reserved_script_name(name):
        return DocumentScriptError(_("'{0}' starts with a reserved prefix.").format(name), DocumentScriptErrorCode.RESERVED_NAME)
    if attach and is_calc_init_script_name(name):
        return DocumentScriptError(_("'{0}' is reserved for the workbook init script.").format(name), DocumentScriptErrorCode.RESERVED_NAME)
    return None


def attach_document_script(
    doc: Any,
    name: str,
    code: str,
    *,
    overwrite: bool = False,
) -> DocumentScriptError | None:
    name = (name or "").strip()
    name_err = new_script_name_error(name, attach=True)
    if name_err is not None:
        return name_err
    scripts, err = _load_document_script_map(doc)
    if err:
        return err
    if name in scripts and not overwrite:
        return DocumentScriptError(
            _("A script named '{0}' already exists in this document.").format(name),
            DocumentScriptErrorCode.ALREADY_EXISTS,
        )
    scripts[name] = code if code is not None else ""
    return set_document_scripts(doc, scripts)


def delete_document_script(doc: Any, name: str) -> DocumentScriptError | None:
    # INIT shares this property map with named scripts. The picker hides it,
    # Deleting INIT by storage name is blocked like attach_document_script so the workbook init script is not wiped accidentally.
    if is_calc_init_script_name(name):
        return new_script_name_error(name, attach=True)
    scripts, err = _load_document_script_map(doc)
    if err:
        return err
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


def script_origin_is_writable(origin: str) -> bool:
    """True for writable script origins ("", My Scripts, this-document).

    An unspecified origin ("") stays writable so default scripts take the My Scripts branch.
    Builtin origins (vision, math, and other picker domain origins) are read-only.
    """
    return origin in ("", SCRIPT_ORIGIN_USER, SCRIPT_ORIGIN_DOCUMENT)


def document_scripts_write_is_stale(session_doc: Any | None, session_doc_url: str | None) -> bool:
    """True when the document opened with the editor is no longer that same file.

    Writes target the document that opened the editor; stale session identity refuses save when the file URL changed.
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
    for domain in PICKER_WIRING:
        if origin == domain.origin:
            real = parse_picker_display_name(domain.display_prefix, display_name)
            return (real or display_name, domain.origin)
    return (display_name, SCRIPT_ORIGIN_USER)


def _picker_state_from_parts(
    user_scripts: dict[str, str],
    doc_scripts: dict[str, str],
    domain_entries: list[tuple[Any, dict[str, str]]],
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    origin_map: dict[str, str] = {}
    merged: dict[str, str] = {}
    items: list[str] = []

    for name in sorted(user_scripts.keys()):
        origin_map[name] = SCRIPT_ORIGIN_USER
        merged[name] = user_scripts[name]
        items.append(name)

    for name in sorted(picker_document_scripts(doc_scripts)):
        display = document_script_display_name(name)
        origin_map[display] = SCRIPT_ORIGIN_DOCUMENT
        merged[display] = doc_scripts[name]
        items.append(display)

    for domain, display_scripts in domain_entries:
        for display_name, code in display_scripts.items():
            origin_map[display_name] = domain.origin
            merged[display_name] = code
            items.append(display_name)

    return items, merged, origin_map


def build_xdl_script_picker_state(
    doc: Any | None,
    saved_scripts: dict[str, str],
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Return (dropdown_items, merged_scripts_by_display_key, origin_map)."""
    user_scripts = dict(saved_scripts) if isinstance(saved_scripts, dict) else {}
    doc_scripts = get_document_scripts(doc)
    domain_entries = domain_template_entries(doc)
    return _picker_state_from_parts(user_scripts, doc_scripts, domain_entries)


def resolve_run_script_name_config_key(doc: Any) -> str:
    """Return the config key for persisting the last selected Run Python Script name for *doc*."""
    if doc:
        if is_calc(doc):
            return "last_python_script_name_calc"
        if is_draw(doc):
            return "last_python_script_name_draw"
    return "last_python_script_name_writer"


def resolve_run_script_selection(
    doc: Any | None,
    picker_state: tuple[list[str], dict[str, str], dict[str, str]],
) -> tuple[str, str, dict[str, str]]:
    """Return (selected_name, selected_code, merged_scripts) for Run Python Script."""
    name_config_key = resolve_run_script_name_config_key(doc)
    last_name = config.get_config(name_config_key)
    names, merged_scripts, _unused_origin_map = picker_state
    # A raw document name from an older list still selects that script when
    # the picker key is the [Doc] display name and no user script uses the raw name.
    if last_name and last_name not in merged_scripts:
        display = document_script_display_name(last_name)
        if display in merged_scripts:
            last_name = display
    if not last_name or last_name not in merged_scripts:
        last_name = names[0] if names else ""
        if last_name:
            config.save({name_config_key: last_name})
    selected_code = merged_scripts.get(last_name, "")
    return last_name, selected_code, merged_scripts


def build_scripts_list_message(
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    status_ok_text: str | None = None,
    status_error_text: str | None = None,
) -> dict[str, Any]:
    user_scripts = get_user_scripts()

    doc = session_doc

    document_available = doc is not None
    document_stale = False
    document_readonly = is_document_readonly_for_scripts(doc)

    # The list and the writes both use the launch document;
    # None is not a document, so it is not stale and not writable.
    if document_scripts_write_is_stale(doc, session_doc_url):
        document_stale = True
        document_readonly = True

    loaded: dict[str, str] = {}
    load_err: DocumentScriptError | None = None
    doc_scripts: dict[str, str] = {}
    if doc is not None and not document_stale:
        # Same keys as build_xdl_script_picker_state. Raw names collided with
        # My Scripts and did not match selected_script_name ([Doc] …), so
        # reopen loaded the wrong row and Save wrote a different script.
        # scripts_manager.js documentScriptListKey must probe these keys for
        # the New / Save As overwrite check. The typed name is not a key.
        loaded, load_err = _load_document_script_map(doc)
        if load_err is None:
            doc_scripts = {
                document_script_display_name(name): code
                for name, code in picker_document_scripts(loaded).items()
            }
        else:
            if not status_ok_text and not status_error_text:
                status_error_text = str(load_err)

    sections: list[dict[str, Any]] = [
        {"id": SCRIPT_ORIGIN_USER, "title": _("My Scripts"), "scripts": user_scripts},
        {"id": SCRIPT_ORIGIN_DOCUMENT, "title": _("This Document"), "scripts": doc_scripts},
    ]

    domain_entries = domain_template_entries(doc)
    for domain, display_scripts in domain_entries:
        sections.append({
            "id": domain.origin,
            "title": _(domain.title),
            "scripts": display_scripts,
        })

    # A stale document must not be passed to resolve_run_script_selection.
    # That returned a '[Doc] X' selection missing from sections and wrote a
    # selection config entry against the stale doc. Pass None so selection
    # only considers user scripts and templates.
    selection_doc = None if document_stale else doc

    # Reuse one property read and template expansion for picker_state so Monaco does not double-load document scripts on each scripts_list message.
    if selection_doc is None:
        picker_state = _picker_state_from_parts(
            user_scripts, {}, domain_template_entries(None)
        )
    else:
        picker_state = _picker_state_from_parts(
            user_scripts, loaded, domain_entries
        )

    selected_name, _selected_code, _merged_scripts = resolve_run_script_selection(
        selection_doc, picker_state
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


def get_user_scripts() -> dict[str, str]:
    scripts = config.get_config("saved_python_scripts")
    if not isinstance(scripts, dict):
        return {}
    return dict(scripts)


def read_only_helper_message() -> str:
    return _("Built-in helpers are read-only. Use Save As to copy one into My Scripts.")


def save_user_script(name: str, code: str) -> DocumentScriptError | None:
    # Reserved script names return DocumentScriptError instead of ValueError so Monaco picker shows translated errors.
    if is_reserved_script_name(name):
        return new_script_name_error(name, attach=False)
    scripts = get_user_scripts()
    scripts[name] = code
    config.save({"saved_python_scripts": scripts})
    return None


def delete_user_script(name: str) -> DocumentScriptError | None:
    scripts = get_user_scripts()
    # Deleting a user script that is not there must not report success or
    # rewrite config. Return NOT_FOUND and skip save.
    if name not in scripts:
        return DocumentScriptError(
            _("Script '{0}' does not exist in My Scripts.").format(name),
            DocumentScriptErrorCode.NOT_FOUND,
        )
    del scripts[name]
    config.save({"saved_python_scripts": scripts})
    return None


def save_script_to_origin(doc: Any, name: str, origin: str, code: str) -> DocumentScriptError | None:
    """Save *code* as storage *name* in the store *origin* names, and nowhere else.

    SCRIPT_ORIGIN_DOCUMENT writes the document property map (*name* is the raw
    storage name, not the "[Doc] " label); any other writable origin writes My
    Scripts. Callers check script_origin_is_writable first. A read-only document
    returns its READONLY error and nothing is written to My Scripts, so the native
    dialog, the Monaco picker and Monaco Run / Save all fail with the same sentence.
    A None *doc* returns NO_DOCUMENT from set_document_scripts.
    """
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        return save_document_script(doc, name, code)
    return save_user_script(name, code)


def script_saved_message(name: str, origin: str) -> str:
    """Success sentence both script pickers show after save_script_to_origin."""
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        return _("Script '%s' saved to this document.") % name
    return _("Script '%s' saved successfully.") % name


def delete_script_from_origin(doc: Any, name: str, origin: str) -> DocumentScriptError | None:
    """Delete storage *name* from the store *origin* names (document map or My Scripts).

    delete_document_script on a None document would report the name as missing,
    so the no-document case is answered here first.
    """
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        if doc is None:
            return DocumentScriptError(_("No document is open."), DocumentScriptErrorCode.NO_DOCUMENT)
        return delete_document_script(doc, name)
    return delete_user_script(name)


def is_picker_template_name(name: str) -> bool:
    """True for a read-only built-in picker label such as ``[Vision] …``."""
    return any(parse_picker_display_name(domain.display_prefix, name) for domain in PICKER_WIRING)


def save_selected_script(
    doc: Any | None,
    display_name: str,
    code: str,
    *,
    allow_builtin_skip: bool = False,
) -> str | None:
    """Save *code* to user scripts or *doc* matching *display_name*.

    Returns None on success or when *display_name* is a built-in template and
    *allow_builtin_skip* is True. Returns an error message string otherwise.
    A read-only document returns its READONLY error; nothing is written to My Scripts.
    """
    if not display_name:
        return None
    user_scripts = get_user_scripts()
    if display_name in user_scripts:
        return save_user_script(display_name, code)
    stored, load_err = _load_document_script_map(doc)
    if is_picker_template_name(display_name):
        if allow_builtin_skip:
            return None
        return read_only_helper_message()
    if load_err is not None:
        return str(load_err)
    real_doc_name = parse_document_script_display_name(display_name) or display_name
    if real_doc_name in stored:
        return save_document_script(doc, real_doc_name, code)
    return _("Script '{0}' is not in My Scripts or this document, so it was not saved.").format(display_name)


def _closed_document_scripts_list(status_error_text: str) -> dict[str, Any]:
    """Script list when the document is already gone and cannot be read again."""
    # The second disposal happens while rebuilding the list. User scripts
    # are config and need no document. Template supports() can touch the
    # closed document, so they are omitted.
    return {
        "type": "scripts_list",
        "sections": [
            {"id": SCRIPT_ORIGIN_USER, "title": _("My Scripts"), "scripts": get_user_scripts()},
            {"id": SCRIPT_ORIGIN_DOCUMENT, "title": _("This Document"), "scripts": {}},
        ],
        "document_available": False,
        "document_readonly": True,
        "document_stale": True,
        "selected_script_name": "",
        "status_error_text": status_error_text,
    }


def _on_request_scripts(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
) -> bool:
    log.info("scripts picker: request_scripts")
    send_list()
    return True


def _on_select_script(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
) -> bool:
    name = str(msg.get("name", "") or "").strip()
    config.save({resolve_run_script_name_config_key(session_doc): name})
    return True


def _validate_script_target(
    msg: dict[str, Any],
    send_list: Callable[..., None],
    *,
    session_doc: Any | None = None,
    session_doc_url: str | None = None,
    action_label: str = "delete",
) -> tuple[str, str] | None:
    name = str(msg.get("name", "") or "").strip()
    origin = str(msg.get("origin", "") or "").strip()
    if not name:
        send_list(status_error_text=_("Script name cannot be empty."))
        return None
    if not script_origin_is_writable(origin):
        send_list(status_error_text=read_only_helper_message())
        return None
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        if session_doc is None:
            err = _("No document is open to save scripts.") if action_label == "save" else _("No document is open.")
            send_list(status_error_text=err)
            return None
        if document_scripts_write_is_stale(session_doc, session_doc_url):
            send_list(status_error_text=_("Document changed — close and reopen Run Python Script to edit document scripts."))
            return None
        storage_name = parse_document_script_display_name(name) or name
    else:
        storage_name = name
    return origin, storage_name


def _on_save_script(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
) -> bool:
    target = _validate_script_target(
        msg,
        send_list,
        session_doc=session_doc,
        session_doc_url=session_doc_url,
        action_label="save",
    )
    if target is None:
        return True
    origin, storage_name = target
    raw_code = msg.get("code", "")
    script_code = raw_code if isinstance(raw_code, str) else ""

    err = save_script_to_origin(session_doc, storage_name, origin, script_code)
    if err:
        send_list(status_error_text=str(err))
        return True

    selected = document_script_display_name(storage_name) if origin == SCRIPT_ORIGIN_DOCUMENT else storage_name
    config.save({resolve_run_script_name_config_key(session_doc): selected})
    log.info("scripts picker: save_script '%s' (%s)", storage_name, origin or SCRIPT_ORIGIN_USER)
    send_list(status_ok_text=script_saved_message(storage_name, origin))
    return True


def _on_delete_script(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
) -> bool:
    target = _validate_script_target(msg, send_list, session_doc=session_doc, session_doc_url=session_doc_url)
    if target is None:
        return True
    origin, storage_name = target
    del_err = delete_script_from_origin(session_doc, storage_name, origin)
    if del_err:
        send_list(status_error_text=str(del_err))
        return True
    log.info("scripts picker: delete_script '%s' (%s)", storage_name, origin or SCRIPT_ORIGIN_USER)
    send_list(status_ok_text=_("Script '%s' deleted.") % storage_name)
    return True


_PICKER_HANDLERS: dict[str, Callable[..., bool]] = {
    "request_scripts": _on_request_scripts,
    "select_script": _on_select_script,
    "save_script": _on_save_script,
    "delete_script": _on_delete_script,
}

# editor_host._dispatch_incoming routes on this set. It is the handler
# map's keys so a new picker message cannot be handled here and dropped there.
SCRIPT_PICKER_MESSAGE_TYPES = frozenset(_PICKER_HANDLERS)


def handle_editor_script_message(
    kind: str,
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send: Callable[[dict[str, Any]], None],
) -> bool:
    """Apply a Monaco script-picker IPC message. Return True if *kind* was handled."""

    def _send_list(*, status_ok_text: str | None = None, status_error_text: str | None = None) -> None:
        send(
            build_scripts_list_message(
                session_doc=session_doc,
                session_doc_url=session_doc_url,
                status_ok_text=status_ok_text,
                status_error_text=status_error_text,
            )
        )

    handler = _PICKER_HANDLERS.get(kind)
    if handler is None:
        return False
    try:
        return handler(
            msg,
            session_doc=session_doc,
            session_doc_url=session_doc_url,
            send_list=_send_list,
        )
    except DocumentDisposedError:
        # Known handlers and set_document_scripts re-raise DocumentDisposedError.
        # An unknown kind never enters this except because the handler lookup
        # returns False first. A second disposal uses _closed_document_scripts_list,
        # which does not touch the document.
        log.exception("scripts picker: document disposed during %s", kind)
        closed = _("The document was closed.")
        try:
            _send_list(status_error_text=closed)
        except DocumentDisposedError:
            log.exception("scripts picker: list refresh failed after the document closed")
            send(_closed_document_scripts_list(closed))
        return True
